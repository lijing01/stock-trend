#!/usr/bin/env python3
"""Capture a verified full-market sector snapshot after the close.

This job deliberately has no stock scan or report-generation side effects.
It only validates the session/date, fetches the full East Money ranking, and
persists the existing sector caches and candidate-universe history.
"""

import argparse
import json
import sys
from datetime import datetime
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


CLOSE_CONFIRMATION_MINUTES = 15 * 60 + 10
DEFAULT_MIN_STOCKS = 10
DEFAULT_MIN_UP_RATIO = 0.15
MINIMUM_COVERAGE_DAYS = 2


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


def snapshot_status(as_of_date: str, days: int = 10) -> dict:
    """Report complete candidate-history coverage without network or writes."""
    history = load_candidate_sector_history(days=days)
    coverage = sum(
        isinstance(record, dict)
        and record.get("complete") is True
        and record.get("quality") == "good"
        and bool(record.get("sectors"))
        for date_key, record in history.items()
        if date_key <= as_of_date
    )
    return {
        "as_of_date": as_of_date,
        "coverage_days": coverage,
        "minimum_days": MINIMUM_COVERAGE_DAYS,
        "days_needed": max(0, MINIMUM_COVERAGE_DAYS - coverage),
        "classification_ready": coverage >= MINIMUM_COVERAGE_DAYS,
    }


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
                                         days=args.days)
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
