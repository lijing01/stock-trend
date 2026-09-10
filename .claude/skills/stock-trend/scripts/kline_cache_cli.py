#!/usr/bin/env python3
"""Inventory and migrate legacy K-line caches into the managed namespace.

The managed namespace is ``.cache/stock-trend/klines/v1/...``; the legacy
layout was root ``kline_{ts_code}_{freq}_{adj}.json`` files plus per-security
``{code}/kline.json`` output files.  This CLI only migrates cache files it can
verify: invalid JSON, mismatched identity, and unverifiable adjustments are
reported instead of being promoted.

Commands:
    inventory  Dry-run report of legacy candidates (no writes).
    migrate    Publish verified legacy files into the managed namespace and
               move their source to a backup directory.

Usage:
    python3 kline_cache_cli.py inventory [--cache-dir DIR] [--coverage-min N]
    python3 kline_cache_cli.py migrate --backup-dir DIR [--cache-dir DIR] [--coverage-min N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.kline_cache import (  # noqa: E402
    KlineCoverage,
    KlineIdentity,
    inventory_legacy_kline_cache,
    migrate_legacy_kline_cache,
    _default_cache_dir,
)
from core.resolve_code import resolve_adj, resolve_asset  # noqa: E402

_KNOWN_SOURCES = {"eastmoney", "tushare_sdk", "tushare_http", "tencent_a", "tencent_hk", "baostock"}
_ROOT_KEY = re.compile(r"^kline_(.+?)_([DW])_(qfq|hfq|none)\.json$")


def _safe_ts_code(raw: str) -> str | None:
    text = str(raw or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{5,8}\.(SH|SZ|HK|BJ)", text):
        return text
    return None


def _source_from_meta(meta: object) -> str | None:
    if not isinstance(meta, dict):
        return None
    source = str(meta.get("data_source") or "").lower()
    return source if source in _KNOWN_SOURCES else None


def _derive_identity(path: Path, meta: object) -> KlineIdentity | None:
    """Derive a verifiable identity for a legacy file, or None when ambiguous."""
    source = _source_from_meta(meta)
    if source is None:
        return None
    if isinstance(meta, dict):
        ts_code = _safe_ts_code(meta.get("ts_code"))
    else:
        ts_code = None
    if ts_code is None:
        return None
    meta_freq = str(meta.get("freq") or "").upper() if isinstance(meta, dict) else ""
    meta_adj = str(meta.get("adj") or "").lower() if isinstance(meta, dict) else ""
    asset = str(meta.get("asset") or "").upper() if isinstance(meta, dict) else ""
    if asset not in {"E", "FD"}:
        asset = resolve_asset(ts_code)
    freq = meta_freq if meta_freq in {"D", "W"} else ("W" if meta_freq == "W" else "D")
    if meta_adj not in {"qfq", "hfq", "none"}:
        meta_adj = resolve_adj(ts_code)
        if source == "tushare_http":
            meta_adj = "none"
    return KlineIdentity(ts_code, asset, freq, meta_adj, source)


def _discover(cache_dir: Path) -> list[dict]:
    """Return candidate records {path, identity, size} in deterministic order."""
    candidates = []
    if cache_dir.exists():
        for root_path in sorted(cache_dir.glob("kline_*.json")):
            if not _ROOT_KEY.fullmatch(root_path.name):
                continue
            meta = _load_meta(root_path)
            candidates.append({
                "path": root_path,
                "identity": _derive_identity(root_path, meta),
                "size": _file_size(root_path),
            })
        for security_dir in sorted(cache_dir.iterdir()):
            if not security_dir.is_dir():
                continue
            legacy = security_dir / "kline.json"
            if not legacy.exists():
                continue
            meta = _load_meta(legacy)
            candidates.append({
                "path": legacy,
                "identity": _derive_identity(legacy, meta),
                "size": _file_size(legacy),
            })
    return candidates


def _load_meta(path: Path) -> object:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    meta = document.get("meta")
    return meta if isinstance(meta, dict) else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _coverage(args) -> KlineCoverage:
    if getattr(args, "coverage_min", None):
        return KlineCoverage(min_records=args.coverage_min)
    return None


def _identity_override(identity: KlineIdentity) -> dict:
    """Map an identity onto the meta keys publish validation checks.

    The managed authority file stores ``data_source`` in meta, while
    ``KlineIdentity`` spells the same field ``source``; publish also enforces
    ts_code/asset/freq/adj consistency.  Supplying these as overrides makes a
    legacy file that lacks them publishable under a verified identity.
    """
    return {
        "ts_code": identity.ts_code,
        "asset": identity.asset,
        "freq": identity.freq,
        "adj": identity.adj,
        "data_source": identity.source,
    }


def _run_inventory(cache_dir: Path, coverage: KlineCoverage | None) -> int:
    candidates = _discover(cache_dir)
    if not candidates:
        print(json.dumps({"candidates": 0, "total_bytes": 0, "files": []},
                         ensure_ascii=False, indent=2))
        return 0
    reports = []
    for candidate in candidates:
        if candidate["identity"] is None:
            reports.append({
                "path": str(candidate["path"]),
                "size": candidate["size"],
                "identity": None,
                "status": "unknown_identity",
            })
            continue
        report = inventory_legacy_kline_cache(
            [candidate["path"]],
            identity=candidate["identity"],
            coverage=coverage,
            cache_dir=cache_dir,
            meta_overrides=_identity_override(candidate["identity"]),
        )[0]
        reports.append({
            "path": str(candidate["path"]),
            "size": candidate["size"],
            "identity": candidate["identity"].as_dict(),
            "status": report["status"],
            "reasons": report.get("reasons", []),
        })
    summary = {
        "candidates": len(reports),
        "total_bytes": sum(candidate["size"] for candidate in candidates),
        "would_migrate": sum(1 for r in reports if r["status"] == "would_migrate"),
        "unknown_identity": sum(1 for r in reports if r["status"] == "unknown_identity"),
        "invalid": sum(1 for r in reports if r["status"] == "invalid"),
        "files": reports,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _run_migrate(cache_dir: Path, backup_dir: Path, coverage: KlineCoverage | None) -> int:
    candidates = _discover(cache_dir)
    results = []
    migrated_bytes = 0
    for candidate in candidates:
        record = {"path": str(candidate["path"]), "size": candidate["size"]}
        if candidate["identity"] is None:
            record.update({"status": "unknown_identity", "reasons": ["cannot_derive_identity"]})
            results.append(record)
            continue
        report = migrate_legacy_kline_cache(
            candidate["path"],
            identity=candidate["identity"],
            coverage=coverage,
            cache_dir=cache_dir,
            backup_dir=backup_dir,
            meta_overrides=_identity_override(candidate["identity"]),
        )
        record.update({
            "status": report["status"],
            "target": report.get("target"),
            "reasons": report.get("reasons", []),
            "backup": report.get("backup"),
        })
        if report["status"] == "migrated":
            migrated_bytes += candidate["size"]
        results.append(record)
    summary = {
        "migrated": sum(1 for r in results if r["status"] == "migrated"),
        "migrated_bytes": migrated_bytes,
        "conflict": sum(1 for r in results if r["status"] == "conflict"),
        "skipped": sum(1 for r in results if r["status"] in {"invalid", "unknown_identity"}),
        "backup_dir": str(backup_dir),
        "files": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=None,
                        help="Cache directory (default: STOCK_TREND_CACHE_DIR or project default)")
    parser.add_argument("--coverage-min", type=int, default=0,
                        help="Minimum verified records for a candidate to migrate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory", help="Dry-run report of legacy candidates")
    migrate_parser = subparsers.add_parser("migrate", help="Publish and back up legacy candidates")
    migrate_parser.add_argument("--backup-dir", required=True, help="Directory for moved legacy files")
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir) if args.cache_dir else _default_cache_dir()
    coverage = KlineCoverage(min_records=args.coverage_min) if args.coverage_min else None
    if args.command == "inventory":
        return _run_inventory(cache_dir, coverage)
    return _run_migrate(cache_dir, Path(args.backup_dir), coverage)


if __name__ == "__main__":
    sys.exit(main())
