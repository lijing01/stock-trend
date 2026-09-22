"""Freeze six-dimension analysis for the user-maintained observation list.

The YAML list defines the universe and order.  This module does not select
recommendations or consume the candidate scanner's observation bucket.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from analysis import market_regime
from scans import stock_scanner
from core.resolve_code import resolve_suffix

SCHEMA = "yaml-observation-analysis/v1"
DIMENSIONS = (
    "momentum", "volume_price", "capital", "fundamental",
    "sector_strength", "wyckoff",
)
DEFAULT_YAML = market_regime.OBSERVATION_LIST_FILE
CACHE_DIR = Path(os.environ.get(
    "STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
ARTIFACT_DIR = CACHE_DIR / "observation_analyses"
SECTOR_SNAPSHOT_DIR = CACHE_DIR / "sector_stocks" / "history"
RANKING_CACHE = CACHE_DIR / "sector_rankings_cache.json"


def artifact_path_for(data_date, artifact_dir=None):
    """Constrain an artifact name to a verified ISO trading date."""
    try:
        verified = date.fromisoformat(str(data_date))
    except ValueError as exc:
        raise ValueError("invalid data_date") from exc
    if verified.isoformat() != data_date:
        raise ValueError("invalid data_date")
    return Path(artifact_dir or ARTIFACT_DIR) / f"{data_date}.json"


def _config_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def load_observation_config(yaml_path=None):
    """Use the daily-review YAML parser and add strict code diagnostics."""
    path = Path(yaml_path or DEFAULT_YAML)
    parsed = market_regime.load_observation_list(path)
    result = {
        **parsed, "path": str(path), "sha256": _config_digest(path),
        "items": [],
    }
    seen = set()
    for source in parsed.get("items", []):
        item = dict(source)
        code = str(item.get("code", ""))
        if not item.get("error"):
            if not re.fullmatch(r"\d{6}", code) or not resolve_suffix(code) \
                    or code.startswith(("5", "15")):
                item["error"] = "无效 A 股代码"
            elif code in seen:
                item["error"] = "重复代码"
        seen.add(code)
        result["items"].append(item)
    return result


def _read_exact_date_rankings(data_date):
    """A current ranking is never evidence for a different report date."""
    try:
        payload = json.loads(RANKING_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("data_date") != data_date:
        return {}
    rankings = payload.get("rankings") or {}
    meta = rankings.get("meta") or {}
    if meta.get("complete") is False or meta.get("provider") not in (
            None, "", "eastmoney"):
        return {}
    return {
        sector.get("code"): sector
        for sector in rankings.get("sectors", [])
        if isinstance(sector, dict) and sector.get("code")
    }


def build_candidates(codes, data_date):
    """Build inputs from exact-date constituent snapshots, with audit errors.

    Existing snapshot rows supply stock metadata and membership provenance.
    A snapshot is accepted only when its embedded date equals ``data_date``.
    Sector rankings are likewise accepted only for that date.  This keeps a
    historical replay from borrowing today's industry ranking or membership.
    """
    wanted = set(codes)
    found = {code: [] for code in codes}
    snapshots = SECTOR_SNAPSHOT_DIR / data_date
    rankings = _read_exact_date_rankings(data_date)
    for path in sorted(snapshots.glob("*.json")):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", path.stem):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("data_date") != data_date:
            continue
        sector_code = path.stem
        ranking = rankings.get(sector_code, {})
        for stock in payload.get("stocks", []):
            if not isinstance(stock, dict) or stock.get("code") not in wanted:
                continue
            code = stock["code"]
            membership = stock_scanner.build_sector_membership(
                sector_code, ranking.get("name", sector_code),
                context={
                    **ranking,
                    "ranking_data_date": data_date if ranking else "",
                    "ranking_source": "eastmoney" if ranking else "",
                    "ranking_quality": "same_date" if ranking else "unknown",
                    "sector_actionable": False,
                },
                stock={
                    **stock, "membership_source": "historical_snapshot",
                    "membership_data_date": data_date,
                    "membership_quality": "historical_verified",
                    "membership_provider": payload.get("provider", ""),
                    "membership_fetch_evidence": {
                        "status": "cache_valid", "source": str(path),
                        "data_date": data_date,
                    },
                },
            )
            found[code].append((stock, membership))
    output = {}
    for code in codes:
        matches = found.get(code) or []
        if not matches:
            output[code] = {
                "candidate": {
                    "code": code, "ts_code": code + resolve_suffix(code),
                    "name": code, "sector_code": "", "sector_name": "",
                    "sector_memberships": [], "sector_actionable": False,
                },
                "metadata_source": "unavailable", "sector_status": "missing",
                "error": "缺少本依据日证券元数据/行业成分快照",
            }
            continue
        stock, _ = matches[0]
        name = stock.get("name")
        memberships = [membership for _, membership in matches]
        primary = stock_scanner.select_primary_sector_membership(memberships)
        candidate = {
            **stock, "code": code, "ts_code": code + resolve_suffix(code),
            "name": name or code, "sector_memberships": memberships,
            "sector_code": primary.get("code", ""),
            "sector_name": primary.get("name", ""),
            "sector_hot_score": primary.get("hot_score", 50),
            "sector_actionable": False,
        }
        output[code] = {
            "candidate": candidate,
            "metadata_source": "sector_constituent_snapshot",
            "error": "证券名称缺失" if not name else "",
            "sector_status": "ready" if any(
                m.get("ranking_data_date") == data_date for m in memberships
            ) else "ranking_missing",
        }
    return output


def _row(source, data_date, candidate_result=None, scored=None):
    """Keep an observation row even when its market analysis fails."""
    candidate_result = candidate_result or {}
    scored = scored if isinstance(scored, dict) else {}
    raw = scored.get("raw_dimensions") or {}
    dimensions = {key: raw.get(key) for key in DIMENSIONS}
    reasons = []
    if source.get("error"):
        reasons.append(source["error"])
    if candidate_result.get("error"):
        reasons.append(candidate_result["error"])
    if not scored and not reasons:
        reasons.append("六维分析未返回结果或 K 线不足")
    if candidate_result.get("sector_status") in ("ranking_missing", "missing"):
        if candidate_result.get("sector_status") == "ranking_missing":
            reasons.append("缺少本依据日行业排行证据")
        dimensions["sector_strength"] = None
    quality = scored.get("data_quality") or {}
    if quality and not quality.get("eligible", False):
        reasons.extend(str(reason) for reason in quality.get("reasons", []))
    if scored.get("wyckoff") and not scored["wyckoff"].get("signal", {}).get(
            "is_buy_signal", False):
        reasons.append("未确认维科夫买点")
    complete = all(dimensions[key] is not None for key in DIMENSIONS)
    raw_composite = scored.get("raw_composite_score") if complete else None
    adjusted = scored.get("quality_adjusted_score") if complete else None
    return {
        "code": source.get("code", ""), "date": source.get("date", ""),
        "entry_phase": source.get("entry_phase", ""),
        "data_date": data_date, "name": scored.get("name") or
        (candidate_result.get("candidate") or {}).get("name", ""),
        "status": "ready" if complete and not reasons else "degraded",
        "reasons": list(dict.fromkeys(reasons)),
        "raw_dimensions": dimensions,
        "dimensions": dimensions,
        "raw_composite_score": raw_composite,
        "composite_score": raw_composite,
        "quality_adjusted_score": adjusted,
        "wyckoff": scored.get("wyckoff") or {},
        "data_quality": quality,
        "source_evidence": scored.get("source_evidence") or {},
        "sector_memberships": scored.get("sector_memberships") or
        (candidate_result.get("candidate") or {}).get("sector_memberships", []),
        "metadata_source": candidate_result.get("metadata_source", ""),
    }


def analyze_observation_list(data_date, yaml_path=None, artifact_path=None,
                             candidate_builder=None, analyzer=None,
                             capital_expected_date=None, save=True):
    """Analyze YAML entries and optionally atomically freeze their artifact.

    ``candidate_builder(codes, data_date)`` returns a code-keyed mapping with
    ``candidate`` or ``error``. ``analyzer(candidates, **kwargs)`` has the
    ``stock_scanner.run_phase2`` contract; injections support deterministic
    offline tests.  Each valid candidate is scored separately, ensuring one
    failed provider cannot erase another YAML row.
    """
    artifact_path_for(data_date)  # Validate before network or filesystem work.
    config = load_observation_config(yaml_path)
    entries = config["items"]
    result = {
        "schema": SCHEMA, "status": "ready" if config["status"] == "ready"
        else "unavailable", "reason": config.get("reason", ""),
        "data_date": data_date, "generated_at": datetime.now().isoformat(),
        "config_path": config["path"], "config_sha256": config["sha256"],
        "items": [],
    }
    if config["status"] != "ready":
        return result
    valid_codes = [item["code"] for item in entries if not item.get("error")]
    try:
        built = (candidate_builder or build_candidates)(valid_codes, data_date)
        if not isinstance(built, dict):
            raise TypeError("candidate builder did not return a mapping")
    except Exception as exc:
        built = {code: {"error": f"候选输入构造失败: {type(exc).__name__}"}
                 for code in valid_codes}
    analyzer = analyzer or stock_scanner.run_phase2
    for entry in entries:
        candidate_result = built.get(entry["code"], {}) if not entry.get("error") else {}
        candidate = candidate_result.get("candidate") if isinstance(
            candidate_result, dict) else None
        scored = None
        if candidate:
            try:
                rows = analyzer(
                    [candidate], enable_wyckoff=True,
                    require_wyckoff_gate=False,
                    as_of_date=data_date,
                    capital_expected_date=capital_expected_date or data_date,
                    top=1, min_candidates=1, disable_early_stop=True)
                scored = next((row for row in rows
                               if row.get("code") == entry["code"]), None)
            except Exception as exc:
                candidate_result = {
                    **candidate_result,
                    "error": f"六维分析失败: {type(exc).__name__}",
                }
        result["items"].append(_row(entry, data_date, candidate_result, scored))
    if any(item["status"] != "ready" for item in result["items"]):
        result["status"] = "degraded"
    if save:
        save_artifact(result, artifact_path)
    return result


def save_artifact(payload, artifact_path=None):
    """Atomically save one same-date YAML analysis."""
    if payload.get("schema") != SCHEMA:
        raise ValueError("invalid observation schema")
    path = Path(artifact_path) if artifact_path else artifact_path_for(
        payload.get("data_date"))
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def load_artifact(data_date, artifact_path=None, yaml_path=None):
    """Reject a stale artifact or an artifact for a changed YAML file."""
    path = Path(artifact_path) if artifact_path else artifact_path_for(data_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "本依据日 YAML 观察分析未生成", "items": []}
    config = load_observation_config(yaml_path)
    if payload.get("schema") != SCHEMA or payload.get("data_date") != data_date:
        return {"status": "unavailable", "reason": "观察分析 schema/依据日不匹配", "items": []}
    if not config["sha256"] or payload.get("config_sha256") != config["sha256"]:
        return {"status": "unavailable", "reason": "观察列表配置已变更", "items": []}
    if [item.get("code") for item in payload.get("items", [])] != [
            item.get("code") for item in config["items"]]:
        return {"status": "unavailable", "reason": "观察分析行与 YAML 不一致", "items": []}
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-date", required=True)
    parser.add_argument("--yaml", default=None)
    parser.add_argument("--artifact", default=None)
    parser.add_argument("--capital-expected-date", default=None)
    args = parser.parse_args(argv)
    result = analyze_observation_list(
        args.data_date, yaml_path=args.yaml, artifact_path=args.artifact,
        capital_expected_date=args.capital_expected_date)
    print(json.dumps({
        "status": result["status"], "data_date": result["data_date"],
        "artifact_path": str(args.artifact or artifact_path_for(args.data_date)),
        "item_count": len(result["items"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
