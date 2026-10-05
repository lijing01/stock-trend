#!/usr/bin/env python3
"""Capture a verified full-market sector snapshot after the close.

This job deliberately has no stock scan or report-generation side effects.
It only validates the session/date, fetches the full East Money ranking, and
persists the existing sector caches and candidate-universe history.
"""

import argparse
import hashlib
import json
import math
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from fetchers.sector_data import (  # noqa: E402
    append_daily_snapshot,
    commit_candidate_sector_snapshot,
    get_last_trading_day,
    get_sector_rankings,
    load_candidate_sector_history,
    rank_hot_sectors,
    save_rankings_cache,
    _verified_trading_date,
)
from fetchers.sector_kline import batch_fetch_kline  # noqa: E402
from analysis.sector_persistence import (  # noqa: E402
    analyze_frozen_artifact,
    sector_identity,
)


CLOSE_CONFIRMATION_MINUTES = 15 * 60 + 10
DEFAULT_MIN_STOCKS = 10
DEFAULT_MIN_UP_RATIO = 0.15
MINIMUM_COVERAGE_DAYS = 2
SECTOR_PERSISTENCE_SCHEMA = "sector-persistence/v1"
SECTOR_KLINE_RECORD_VERSION = "eastmoney-sector-kline/v1"


def _status_result(status: str, data_date: str = "", **extra) -> dict:
    result = {"status": status, "written": False}
    if data_date:
        result["data_date"] = data_date
    result.update(extra)
    return result


def _safe_errors(meta: dict) -> list[str]:
    errors = meta.get("errors", []) if isinstance(meta, dict) else []
    if isinstance(errors, str):
        errors = [errors]
    if not isinstance(errors, list):
        return []
    return [str(error) for error in errors if error]


def _error_result(stage: str, exc: Exception, data_date: str = "") -> dict:
    """Return a machine-readable error without exposing a traceback."""
    return _status_result(
        "error",
        data_date=data_date,
        errors=[f"{stage}:{type(exc).__name__}"],
    )


def _digest(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _calendar_identity(calendar_evidence: dict | None) -> dict:
    evidence = calendar_evidence if isinstance(calendar_evidence, dict) else {}
    return {
        "source": str(evidence.get("source") or "unknown"),
        "fetched_at": str(evidence.get("fetched_at") or ""),
        "evidence_digest": _digest(evidence) if evidence else "",
    }


def _sector_binding(sectors: list[dict], basis_date: str,
                    trading_dates: list[str],
                    calendar_evidence: dict | None = None,
                    run_as_of: datetime | None = None) -> str:
    return _digest({
        "basis_date": basis_date,
        "trading_dates": list(trading_dates),
        "calendar": _calendar_identity(calendar_evidence),
        "run_as_of": run_as_of.isoformat() if run_as_of is not None else None,
        "sectors": [{**sector_identity(item),
                     "roles": list(item.get("roles") or [])}
                    for item in sectors],
    })


def _artifact_digest_valid(artifact: dict) -> bool:
    expected = artifact.get("content_digest")
    if not isinstance(expected, str) or not expected:
        return False
    content = dict(artifact)
    content.pop("content_digest", None)
    return _digest(content) == expected


def _parse_aware_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return value if math.isfinite(numeric) else None


def _freeze_sector_fields(sector: dict, identity: dict) -> dict:
    frozen = {
        **identity,
        "roles": [str(role) for role in sector.get("roles") or [] if role],
    }
    for key in ("change_pct", "up_count", "down_count", "flat_count",
                "total_count"):
        value = _finite_number(sector.get(key))
        if value is not None:
            frozen[key] = value
    return frozen


def _load_bound_artifact(path: Path, *, binding: str, basis_date: str,
                         cutoff: datetime | None) -> tuple[dict | None, str]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "artifact_missing"
    except (OSError, ValueError, TypeError):
        return None, "artifact_invalid"
    artifact_time = _parse_aware_datetime(artifact.get("fetched_at")) \
        if isinstance(artifact, dict) else None
    if (not isinstance(artifact, dict)
            or artifact.get("schema_version") != SECTOR_PERSISTENCE_SCHEMA
            or artifact.get("basis_date") != basis_date
            or artifact.get("binding_digest") != binding
            or not _artifact_digest_valid(artifact)
            or artifact_time is None
            or (cutoff is not None and artifact_time > cutoff)):
        return None, "artifact_binding_mismatch"
    return artifact, ""


def _bounded_fetch(fetcher, codes: list[str], *, total_timeout: float,
                   max_workers: int) -> tuple[dict, bool, str]:
    result_queue = queue.Queue(maxsize=1)

    def run():
        try:
            result_queue.put((fetcher(
                codes, min_records=80,
                max_workers=max(1, min(int(max_workers), 4)),
                total_timeout=total_timeout,
            ), ""))
        except Exception as exc:
            result_queue.put(({}, f"fetch_error:{type(exc).__name__}"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=max(0.0, total_timeout))
    if thread.is_alive():
        return {}, True, "fetch_timeout"
    try:
        result, error = result_queue.get_nowait()
    except queue.Empty:
        return {}, False, "fetch_empty"
    return result if isinstance(result, dict) else {}, False, error


def freeze_sector_persistence(sectors: list[dict], basis_date: str,
                              trading_dates: list[str], artifact_path,
                              *, refresh: bool,
                              fetcher=batch_fetch_kline,
                              total_timeout: float = 12.0,
                              max_workers: int = 4,
                              fetched_at: str | None = None,
                              as_of: str | datetime | None = None,
                              run_as_of: str | datetime | None = None,
                              calendar_evidence: dict | None = None) -> dict:
    """Freeze or load the exact artifact bound to one report invocation.

    ``refresh=False`` is strictly offline: it reads only ``artifact_path`` and
    never searches a latest cache or invokes a calendar/provider loader.
    """
    path = Path(artifact_path)
    run_cutoff = run_as_of if isinstance(run_as_of, datetime) \
        else _parse_aware_datetime(run_as_of)
    if isinstance(run_cutoff, datetime) and (
            run_cutoff.tzinfo is None or run_cutoff.utcoffset() is None):
        run_cutoff = None
    binding = _sector_binding(
        sectors, basis_date, trading_dates, calendar_evidence, run_cutoff)
    cutoff = as_of if isinstance(as_of, datetime) \
        else _parse_aware_datetime(as_of)
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None \
            or cutoff.utcoffset() is None:
        cutoff = datetime.now(timezone.utc)
    artifact, artifact_error = _load_bound_artifact(
        path, binding=binding, basis_date=basis_date, cutoff=cutoff)
    if artifact is not None:
        return analyze_frozen_artifact(artifact)
    if not refresh:
        if artifact_error == "artifact_missing":
            return {"status": "missing", "reasons": ["artifact_missing"],
                    "items": [], "basis_date": basis_date}
        if artifact_error == "artifact_invalid":
            return {"status": "invalid", "reasons": ["artifact_invalid"],
                    "items": [], "basis_date": basis_date}
        return {"status": "mismatched", "reasons": [artifact_error],
                "items": [], "basis_date": basis_date}

    codes = list(dict.fromkeys(
        sector_identity(item)["code"] for item in sectors
        if sector_identity(item)["code"]
    ))
    fetched, timed_out, error = _bounded_fetch(
        fetcher, codes, total_timeout=total_timeout,
        max_workers=max_workers,
    )
    reasons = [reason for reason in ("fetch_timeout" if timed_out else "", error)
               if reason]
    frozen_sectors = []
    for sector in sectors:
        identity = sector_identity(sector)
        normalized_records = []
        for raw in fetched.get(identity["code"], []) or []:
            if not isinstance(raw, dict):
                continue
            raw_date = str(raw.get("trade_date") or "").replace("-", "")
            if len(raw_date) != 8 or raw_date > basis_date.replace("-", ""):
                continue
            close = _finite_number(raw.get("close"))
            if close is None or close <= 0:
                continue
            record = {"trade_date": raw_date, "close": close}
            for key in ("sector_id", "classification_version", "source",
                        "price_type"):
                record[key] = str(raw.get(key) or identity[key])
            normalized_records.append(record)
        normalized_records.sort(key=lambda item: str(item.get("trade_date") or ""))
        frozen_sectors.append({
            **_freeze_sector_fields(sector, identity),
            "record_version": SECTOR_KLINE_RECORD_VERSION,
            "records": normalized_records,
        })
    calendar_identity = _calendar_identity(calendar_evidence)
    actual_fetched_at = fetched_at \
        if _parse_aware_datetime(fetched_at) is not None \
        else datetime.now(timezone.utc).isoformat()
    artifact = {
        "schema_version": SECTOR_PERSISTENCE_SCHEMA,
        "basis_date": basis_date,
        "fetched_at": actual_fetched_at,
        "run_as_of": run_cutoff.isoformat() if run_cutoff is not None else None,
        "binding_digest": binding,
        "trading_dates": list(trading_dates),
        "calendar_source": calendar_identity["source"],
        "calendar_fetched_at": calendar_identity["fetched_at"],
        "calendar_evidence_digest": calendar_identity["evidence_digest"],
        "reasons": reasons,
        "sectors": frozen_sectors,
    }
    artifact["content_digest"] = _digest(artifact)
    _atomic_json_write(path, artifact)
    analysis = analyze_frozen_artifact(artifact)
    if reasons:
        analysis["status"] = "degraded"
        analysis["reasons"] = reasons
    return analysis


def _unwrap_rankings(result):
    if not isinstance(result, dict):
        return None
    payload = result.get("payload", result)
    return payload if isinstance(payload, dict) else None


def _validate_payload(payload: dict, data_date: str) -> list[str]:
    """Validate the source contract before any persistence is attempted."""
    if not isinstance(payload, dict):
        return ["ranking_payload_invalid"]
    meta = payload.get("meta")
    sectors = payload.get("sectors")
    if not isinstance(meta, dict):
        return ["ranking_meta_invalid"]
    if meta.get("complete") is not True:
        return _safe_errors(meta) or ["ranking_incomplete"]
    if not isinstance(sectors, list) or not sectors:
        return ["ranking_sectors_empty"]

    source = meta.get("source", "eastmoney")
    if source not in ("eastmoney", "realtime"):
        return [f"unsupported_source:{source}"]

    sources = meta.get("sources")
    if sources is not None:
        if not isinstance(sources, dict) or not all(
                sources.get(name) == "ok"
                for name in ("industry", "concept")):
            return ["ranking_subsource_incomplete"]

    upstream_date = meta.get("data_date", "")
    if upstream_date:
        if _verified_trading_date(upstream_date) != upstream_date:
            return ["ranking_data_date_invalid"]
        if upstream_date != data_date:
            return ["ranking_data_date_mismatch"]

    active = sum(
        1 for sector in sectors
        if isinstance(sector, dict)
        and ((sector.get("up_count", 0) or 0) > 0
             or (sector.get("down_count", 0) or 0) > 0)
    )
    if active == 0:
        return ["ranking_no_active_sectors"]
    return []


def capture_snapshot(now=None, expected_date: str = "",
                     dry_run: bool = False) -> dict:
    """Capture and persist one full sector snapshot.

    ``expected_date`` is an assertion about today's session, not a backfill
    switch.  A past date, a holiday, or an upstream date mismatch never
    creates a new history record.
    """
    current = now if now is not None else datetime.now()
    today = current.strftime("%Y-%m-%d")

    if current.weekday() >= 5:
        return _status_result("market_closed")
    if current.hour * 60 + current.minute < CLOSE_CONFIRMATION_MINUTES:
        return _status_result("not_closed")

    if expected_date:
        if _verified_trading_date(expected_date) != expected_date:
            return _status_result(
                "date_mismatch", expected_date=expected_date)
        if expected_date != today:
            return _status_result(
                "date_mismatch", expected_date=expected_date,
                data_date=today)

    try:
        trading_date, date_source = get_last_trading_day(now=current)
    except Exception as exc:
        return _error_result("trading_calendar", exc, data_date=today)

    if trading_date != today:
        return _status_result(
            "market_closed", date_source=date_source)
    if expected_date and expected_date != trading_date:
        return _status_result(
            "date_mismatch", data_date=trading_date,
            date_source=date_source, expected_date=expected_date)
    data_date = expected_date or trading_date

    try:
        fetched = get_sector_rankings(with_evidence=True)
    except Exception as exc:
        return _error_result("ranking_fetch", exc, data_date=data_date)
    payload = _unwrap_rankings(fetched)
    errors = _validate_payload(payload, data_date)
    if errors:
        return _status_result(
            "incomplete", data_date=data_date, errors=errors)

    meta = payload.setdefault("meta", {})
    meta["data_date"] = data_date
    meta.setdefault("source", "eastmoney")

    if dry_run:
        ranked = rank_hot_sectors(
            payload, top_n=None,
            min_stocks=DEFAULT_MIN_STOCKS,
            min_up_ratio=DEFAULT_MIN_UP_RATIO,
        )
        if not ranked:
            return _status_result(
                "incomplete", data_date=data_date,
                errors=["candidate_ranking_empty"])
        return {
            "status": "validated",
            "written": False,
            "data_date": data_date,
            "universe_count": len(payload.get("sectors", [])),
        }

    try:
        result = commit_candidate_sector_snapshot(
            payload, data_date=data_date,
        )
    except (OSError, TypeError, ValueError) as exc:
        return _error_result("candidate_snapshot", exc, data_date=data_date)
    if not isinstance(result, dict) or result.get("status") != "saved":
        result = dict(result) if isinstance(result, dict) else {
            "status": "incomplete"
        }
        result["written"] = False
        result.setdefault("data_date", data_date)
        return result

    warnings = []
    for stage, writer, kwargs in (
            ("ranking_cache", save_rankings_cache,
             {"data_date": data_date}),
            ("sector_snapshot", append_daily_snapshot,
             {"override_date": data_date})):
        try:
            writer(payload, **kwargs)
        except (OSError, TypeError, ValueError) as exc:
            warnings.append(f"{stage}:{type(exc).__name__}")

    output = dict(result)
    output["written"] = True
    if warnings:
        output["warnings"] = warnings
    return output


def snapshot_status(as_of_date: str, days: int = 10,
                    include_details: bool = False) -> dict:
    """Report complete candidate-history coverage without network or writes."""
    history = load_candidate_sector_history(days=days)
    observations = []
    for date_key, record in history.items():
        if date_key > as_of_date:
            continue
        valid = (isinstance(record, dict)
                 and record.get("complete") is True
                 and record.get("quality") == "good"
                 and bool(record.get("sectors")))
        observations.append({"date": date_key, "status": "complete" if valid else (
            "partial" if isinstance(record, dict) and record.get("sectors") else "missing")})
    coverage = sum(item["status"] == "complete" for item in observations)
    result = {
        "as_of_date": as_of_date,
        "coverage_days": coverage,
        "minimum_days": MINIMUM_COVERAGE_DAYS,
        "days_needed": max(0, MINIMUM_COVERAGE_DAYS - coverage),
        "classification_ready": coverage >= MINIMUM_COVERAGE_DAYS,
    }
    if include_details:
        result["observations"] = sorted(observations, key=lambda item: item["date"])
        result["missing_dates"] = [item["date"] for item in observations
                                    if item["status"] != "complete"]
        result["evidence_note"] = (
            "coverage_days counts only complete good full-market snapshots; "
            "missing/partial dates are not zero-hot days")
    return result


def _exit_code(status: str) -> int:
    if status in ("saved", "validated"):
        return 0
    if status in ("not_closed", "market_closed"):
        return 2
    return 1


def _print_human(result: dict) -> None:
    fields = [f"status={result.get('status', 'error')}"]
    for key in ("data_date", "coverage_days", "days_needed", "warnings"):
        if key in result:
            fields.append(f"{key}={result[key]}")
    print(" ".join(fields))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="收盘后采集完整东方财富板块快照")
    parser.add_argument(
        "--date", dest="expected_date", default="",
        help="显式指定当天交易日 YYYY-MM-DD，不支持历史回填")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="拉取并验证，但不写入缓存或历史")
    parser.add_argument(
        "--status", action="store_true",
        help="仅检查本地完整快照覆盖，不联网、不写入")
    parser.add_argument("--days", type=int, default=10,
                        help="状态检查窗口，默认10天")
    parser.add_argument("--json", action="store_true",
                        help="输出单个 JSON 对象")
    args = parser.parse_args(argv)

    try:
        if args.status:
            as_of_date = args.expected_date or datetime.now().strftime(
                "%Y-%m-%d")
            if _verified_trading_date(as_of_date) != as_of_date:
                result = {"status": "error", "errors": ["invalid_status_date"]}
            else:
                result = snapshot_status(as_of_date=as_of_date,
                                         days=args.days,
                                         include_details=True)
        else:
            result = capture_snapshot(
                expected_date=args.expected_date,
                dry_run=args.dry_run,
            )
    except Exception as exc:
        result = _error_result("cli", exc)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _print_human(result)
    return _exit_code(result.get("status", "error"))


if __name__ == "__main__":
    sys.exit(main())
