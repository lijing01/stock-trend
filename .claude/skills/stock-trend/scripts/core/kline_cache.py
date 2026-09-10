#!/usr/bin/env python3
"""Authoritative file cache for K-line series.

The cache identity describes the data that is stored. Requested date ranges and
row limits describe coverage requirements and deliberately do not create more
copies of the same series.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # pragma: no cover - Windows is not a supported runtime, keep import safe.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SCHEMA = "stock-trend-kline-cache/v1"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_ADJUSTMENTS = {"qfq", "hfq", "none"}
_LAST_CLEANUP = 0.0
_CLEANUP_INTERVAL = 300.0


def _default_cache_dir() -> Path:
    configured = os.environ.get("STOCK_TREND_CACHE_DIR")
    if configured:
        return Path(configured)
    walk = Path(__file__).resolve().parent
    for candidate in (walk, *walk.parents):
        if (candidate / ".claude").exists():
            return candidate / ".cache" / "stock-trend"
    return Path.cwd() / ".cache" / "stock-trend"


def _component(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_COMPONENT.fullmatch(text) or text in {".", ".."}:
        raise ValueError(f"invalid K-line cache {field}: {value!r}")
    return text


def _date_key(value: object | None) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"invalid K-line date: {value!r}")
    datetime.strptime(text, "%Y%m%d")
    return text


@dataclass(frozen=True)
class KlineIdentity:
    """Fields that uniquely identify one K-line series."""

    ts_code: str
    asset: str
    freq: str
    adj: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _component(self.ts_code.upper(), "ts_code"))
        object.__setattr__(self, "asset", _component(self.asset.upper(), "asset"))
        object.__setattr__(self, "freq", _component(self.freq.upper(), "freq"))
        normalized_adj = _component(self.adj.lower(), "adj")
        if normalized_adj not in _VALID_ADJUSTMENTS:
            raise ValueError(f"invalid K-line cache adj: {self.adj!r}")
        object.__setattr__(self, "adj", normalized_adj)
        object.__setattr__(self, "source", _component(self.source.lower(), "source"))

    def as_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "asset": self.asset,
            "freq": self.freq,
            "adj": self.adj,
            "source": self.source,
        }

    @property
    def cache_key(self) -> str:
        return "kline:" + ":".join(self.as_dict().values())


@dataclass(frozen=True)
class KlineCoverage:
    """Requirements a cached series must satisfy."""

    min_records: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    expected_date: str | None = None
    limit: int | None = None
    ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        for field in ("min_records", "limit", "ttl_seconds"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{field} must be a non-negative integer")
        for field in ("min_records", "limit"):
            if getattr(self, field) == 0:
                raise ValueError(f"{field} must be greater than zero")
        object.__setattr__(self, "start_date", _date_key(self.start_date))
        object.__setattr__(self, "end_date", _date_key(self.end_date))
        object.__setattr__(self, "expected_date", _date_key(self.expected_date))
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")


def get_kline_cache_path(
    identity: KlineIdentity, cache_dir: str | os.PathLike[str] | None = None
) -> Path:
    root = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
    return (
        root / "klines" / "v1" / identity.ts_code / identity.asset
        / identity.freq / identity.adj / f"{identity.source}.json"
    )


def _valid_rows(payload: Mapping) -> list[tuple[str, dict]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            day = _date_key(row.get("trade_date") or row.get("date"))
        except ValueError:
            continue
        if not day:
            continue
        try:
            prices = [float(row[key]) for key in ("open", "high", "low", "close")]
            volume = float(row.get("vol", row.get("volume")))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (*prices, volume)):
            continue
        if any(value <= 0 for value in prices) or volume < 0:
            continue
        if "amount" in row:
            try:
                amount = float(row["amount"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amount) or amount < 0:
                continue
        if prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]):
            continue
        rows.append((day, row))
    # A trading date is one bar. Keep the last valid duplicate deterministically.
    unique = {day: row for day, row in rows}
    return sorted(unique.items(), key=lambda item: item[0])


def validate_kline_payload(
    payload: Mapping | None,
    coverage: KlineCoverage | None = None,
    *,
    cache_timestamp: float | None = None,
    now: float | None = None,
) -> dict:
    """Return coverage facts and whether ``payload`` satisfies a request."""
    coverage = coverage or KlineCoverage()
    reasons = []
    if not isinstance(payload, Mapping):
        return {"valid": False, "reasons": ["invalid_payload"], "record_count": 0}
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        reasons.append("invalid_meta")
    elif meta.get("data_source") in {None, "", "error"}:
        reasons.append("error_source")

    rows = _valid_rows(payload)
    dates = [day for day, _ in rows]
    if not dates:
        reasons.append("no_valid_rows")
    earliest = dates[0] if dates else None
    latest = dates[-1] if dates else None
    selected = [item for item in rows if not coverage.start_date or item[0] >= coverage.start_date]
    selected = [item for item in selected if not coverage.end_date or item[0] <= coverage.end_date]

    required_count = coverage.min_records
    if required_count is not None and len(selected) < required_count:
        reasons.append("insufficient_records")
    if coverage.start_date and (not earliest or earliest > coverage.start_date):
        reasons.append("start_date_not_covered")
    if coverage.end_date and (not latest or latest < coverage.end_date):
        reasons.append("end_date_not_covered")
    if coverage.expected_date and (not latest or latest < coverage.expected_date):
        reasons.append("expected_date_not_covered")
    if (coverage.start_date or coverage.end_date) and not selected:
        reasons.append("no_rows_in_requested_range")
    age_seconds = None
    if coverage.ttl_seconds is not None:
        if not isinstance(cache_timestamp, (int, float)) or cache_timestamp <= 0:
            reasons.append("missing_cache_timestamp")
        else:
            age_seconds = max(0.0, (time.time() if now is None else now) - cache_timestamp)
            if age_seconds >= coverage.ttl_seconds:
                reasons.append("expired")

    return {
        "valid": not reasons,
        "reasons": reasons,
        "record_count": len(rows),
        "selected_count": len(selected),
        "earliest_date": earliest,
        "latest_date": latest,
        "age_seconds": age_seconds,
    }


def _identity_matches(stored: object, expected: KlineIdentity) -> bool:
    return isinstance(stored, Mapping) and all(
        stored.get(key) == value for key, value in expected.as_dict().items()
    )


def _load_document(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def _payload_from_document(document: Mapping) -> dict:
    return copy.deepcopy({
        key: value for key, value in document.items()
        if key not in {"cache_timestamp", "cache_key", "kline_cache"}
    })


def _crop_payload(payload: dict, coverage: KlineCoverage) -> dict:
    if not any((coverage.start_date, coverage.end_date, coverage.limit)):
        return payload
    rows = _valid_rows(payload)
    selected = [row for day, row in rows if not coverage.start_date or day >= coverage.start_date]
    if coverage.end_date:
        selected = [row for row in selected if _date_key(row.get("trade_date") or row.get("date")) <= coverage.end_date]
    if coverage.limit:
        selected = selected[-coverage.limit:]
    payload["data"] = copy.deepcopy(selected)
    if isinstance(payload.get("meta"), dict):
        payload["meta"]["record_count"] = len(selected)
        if "data_points" in payload["meta"]:
            payload["meta"]["data_points"] = len(selected)
        if selected:
            payload["meta"]["start_date"] = _date_key(
                selected[0].get("trade_date") or selected[0].get("date"))
            payload["meta"]["end_date"] = _date_key(
                selected[-1].get("trade_date") or selected[-1].get("date"))
    return payload


def load_kline_cache(
    identity: KlineIdentity,
    coverage: KlineCoverage | None = None,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> dict | None:
    """Load an exact cache identity when all coverage requirements pass."""
    coverage = coverage or KlineCoverage()
    document = _load_document(get_kline_cache_path(identity, cache_dir))
    if not document:
        return None
    cache_meta = document.get("kline_cache")
    if not isinstance(cache_meta, Mapping) or cache_meta.get("schema") != SCHEMA:
        return None
    if not _identity_matches(cache_meta.get("identity"), identity):
        return None
    payload = _payload_from_document(document)
    validation = validate_kline_payload(
        payload, coverage, cache_timestamp=document.get("cache_timestamp"), now=now
    )
    if not validation["valid"]:
        return None
    return _crop_payload(payload, coverage)


def load_best_kline_cache(
    identities: Sequence[KlineIdentity],
    coverage: KlineCoverage | None = None,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> tuple[dict, KlineIdentity] | None:
    """Load the first usable identity, preserving caller source preference."""
    for identity in identities:
        payload = load_kline_cache(identity, coverage, cache_dir=cache_dir, now=now)
        if payload is not None:
            return payload, identity
    return None


def migrate_legacy_kline_cache(
    legacy_path: str | os.PathLike[str],
    identity: KlineIdentity,
    *,
    coverage: KlineCoverage | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    backup_dir: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    meta_overrides: Mapping | None = None,
) -> dict:
    """Validate and promote one legacy JSON file into the managed namespace.

    The operation is deliberately explicit: callers can inventory with
    ``dry_run`` first, and a backup directory is opt-in so migration never
    destroys the only legacy copy accidentally.  A successful publish is
    re-read through the managed loader before the legacy file is moved.

    ``meta_overrides`` supplies identity fields (ts_code/asset/freq/adj/
    data_source) that the legacy file's own metadata does not carry, so the
    published authority file records a verified identity instead of a guess.
    """
    path = Path(legacy_path)
    result = {"path": str(path), "target": str(get_kline_cache_path(identity, cache_dir))}
    if not path.exists():
        return {**result, "status": "missing"}
    payload = _load_document(path)
    if payload is None:
        return {**result, "status": "invalid_json"}
    if meta_overrides:
        normalized = copy.deepcopy(payload)
        if isinstance(normalized.get("meta"), dict):
            normalized["meta"] = {**normalized["meta"], **dict(meta_overrides)}
        else:
            normalized["meta"] = dict(meta_overrides)
        payload = normalized
    try:
        timestamp = float(payload.get("cache_timestamp") or path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        timestamp = path.stat().st_mtime if path.exists() else 0.0
    validation = validate_kline_payload(payload, coverage)
    result["validation"] = validation
    result["fetched_at"] = timestamp
    if not validation["valid"]:
        return {**result, "status": "invalid", "reasons": validation.get("reasons", [])}
    if dry_run:
        return {**result, "status": "would_migrate"}
    if not publish_kline_cache(identity, payload, cache_dir=cache_dir, fetched_at=timestamp):
        return {**result, "status": "conflict"}
    verified = load_kline_cache(identity, coverage, cache_dir=cache_dir)
    if verified is None:
        return {**result, "status": "verification_failed"}
    if backup_dir is None:
        return {**result, "status": "migrated", "backup": None}
    backup_root = Path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / path.name
    if backup_path.exists():
        backup_path = backup_root / f"{path.stem}-{int(timestamp)}{path.suffix}"
    os.replace(path, backup_path)
    return {**result, "status": "migrated", "backup": str(backup_path)}


def inventory_legacy_kline_cache(
    paths: Iterable[str | os.PathLike[str]],
    *,
    identity: KlineIdentity,
    coverage: KlineCoverage | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    meta_overrides: Mapping | None = None,
) -> list[dict]:
    """Return a dry-run migration report for legacy candidates."""
    return [migrate_legacy_kline_cache(
        path, identity, coverage=coverage, cache_dir=cache_dir,
        dry_run=True, meta_overrides=meta_overrides,
    ) for path in paths]


@contextmanager
def _publish_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _should_replace(existing: Mapping | None, incoming_validation: Mapping) -> bool:
    if not existing:
        return True
    current_payload = _payload_from_document(existing)
    current = validate_kline_payload(current_payload)
    if not current["valid"]:
        return True
    if incoming_validation["latest_date"] < current["latest_date"]:
        return False
    if incoming_validation["record_count"] < current["record_count"]:
        return False
    if incoming_validation["earliest_date"] > current["earliest_date"]:
        return False
    return True


def publish_kline_cache(
    identity: KlineIdentity,
    payload: Mapping,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    fetched_at: float | None = None,
) -> bool:
    """Atomically publish a successful series without replacing better data."""
    validation = validate_kline_payload(payload)
    if not validation["valid"]:
        return False
    # A published snapshot must be wholly usable.  Silently dropping malformed
    # rows during validation would otherwise leave corrupt bars in the
    # authority file and make record_count/coverage misleading.
    raw_rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(raw_rows, list) or len(_valid_rows(payload)) != len(raw_rows):
        return False
    meta = payload.get("meta", {})
    identity_fields = {
        "ts_code": identity.ts_code,
        "asset": identity.asset,
        "freq": identity.freq,
        "adj": identity.adj,
    }
    for key, expected in identity_fields.items():
        actual = meta.get(key)
        if actual is not None and str(actual).lower() != expected.lower():
            return False
    if str(meta.get("data_source", "")).lower() != identity.source:
        return False

    timestamp = time.time() if fetched_at is None else float(fetched_at)
    path = get_kline_cache_path(identity, cache_dir)
    document = {
        **copy.deepcopy(dict(payload)),
        "cache_timestamp": timestamp,
        "cache_key": identity.cache_key,
        "kline_cache": {
            "schema": SCHEMA,
            "identity": identity.as_dict(),
            "fetched_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "coverage": {
                "earliest_date": validation["earliest_date"],
                "latest_date": validation["latest_date"],
                "record_count": validation["record_count"],
            },
        },
    }
    try:
        with _publish_lock(path):
            existing = _load_document(path)
            if not _should_replace(existing, validation):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        global _LAST_CLEANUP
        now = time.time()
        if now - _LAST_CLEANUP >= _CLEANUP_INTERVAL:
            _LAST_CLEANUP = now
            clean_kline_cache(cache_dir=cache_dir, protected_paths=[path])
        return True
    except (OSError, TypeError, ValueError):
        return False


def clean_kline_cache(
    max_size_mb: float | None = None,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    protected_paths: Iterable[str | os.PathLike[str]] = (),
) -> dict:
    """Evict oldest managed series to 80% of the configured byte budget."""
    if max_size_mb is None:
        configured = os.environ.get("STOCK_TREND_KLINE_CACHE_MAX_MB", "200")
        try:
            max_size_mb = float(configured)
        except ValueError:
            max_size_mb = 200.0
    if max_size_mb < 0:
        raise ValueError("max_size_mb must be non-negative")
    root = (Path(cache_dir) if cache_dir is not None else _default_cache_dir()) / "klines" / "v1"
    protected = {Path(item).resolve() for item in protected_paths}
    files = []
    total = 0
    if root.exists():
        for path in root.rglob("*.json"):
            try:
                size = path.stat().st_size
                document = _load_document(path)
                timestamp = document.get("cache_timestamp", 0) if document else 0
                files.append((path, size, float(timestamp or 0)))
                total += size
            except (OSError, TypeError, ValueError):
                continue
    initial = total
    budget = int(max_size_mb * 1024 * 1024)
    target = int(budget * 0.8)
    removed = []
    if total > budget:
        for path, size, timestamp in sorted(files, key=lambda item: (item[2], str(item[0]))):
            if path.resolve() in protected:
                continue
            try:
                with _publish_lock(path):
                    if not path.exists():
                        continue
                    current_stat = path.stat()
                    current_document = _load_document(path)
                    current_timestamp = float(
                        (current_document or {}).get("cache_timestamp", 0) or 0)
                    if current_stat.st_size != size or current_timestamp != float(timestamp or 0):
                        continue
                    path.unlink()
            except OSError:
                continue
            removed.append(str(path))
            total -= size
            if total <= target:
                break
    return {
        "initial_bytes": initial,
        "remaining_bytes": total,
        "budget_bytes": budget,
        "target_bytes": target,
        "removed_count": len(removed),
        "removed_files": removed,
        "within_budget": total <= budget,
    }
