"""Collect independent US context or supplement an existing daily-review report."""

import argparse
import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parents[3]
sys.path.insert(0, str(SCRIPTS))

from core.report_file import atomic_write_text

SCHEMA = "us-market-summary/v1"


def unavailable(basis_date, as_of, reason):
    return {"schema_version": SCHEMA, "basis_date": basis_date,
            "as_of": as_of.isoformat(), "data_quality": "unavailable",
            "status": "unavailable", "groups": {}, "errors": {"collection": reason}}


def collect_summary(basis_date, *, as_of=None, no_refresh=False, cache_dir=None):
    """Freeze the cutoff once; never write this context into A-share score history."""
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    as_of = as_of or datetime.now(ZoneInfo("Asia/Shanghai"))
    if as_of.tzinfo is None:
        raise ValueError("as_of must include timezone")
    try:
        from analysis.us_market_summary import resolve_window, load_watchlist, build_summary
        window = resolve_window(basis_date, as_of)
        config = load_watchlist()
        config_hash = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        identity = {"schema": SCHEMA, "basis_date": basis_date,
                    "baseline": window.get("baseline_session"),
                    "end": window.get("expected_end_session"),
                    "calendar": window.get("calendar_version"), "config": config_hash,
                    "adjustment": "adj_close"}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = Path(cache_dir or os.environ.get(
            "STOCK_TREND_CACHE_DIR", str(ROOT / ".cache" / "stock-trend"))) / "us_market"
        pointer = directory / f"{key}.json"
        if no_refresh:
            try:
                result = json.loads(pointer.read_text(encoding="utf-8"))
                if result.get("cache_identity") != identity:
                    raise ValueError("cache identity mismatch")
                if datetime.fromisoformat(result["as_of"]) > as_of:
                    raise ValueError("cache cutoff is in the future")
                result = copy.deepcopy(result)
                result["source_status"] = "cached"
                result["requested_as_of"] = as_of.isoformat()
                return result
            except (OSError, ValueError, KeyError, TypeError):
                return unavailable(basis_date, as_of, "no_qualified_us_cache")
        symbols = [row["symbol"] for group in ("indices", "sectors", "stocks")
                   for row in config.get(group, [])]
        payload = {"provider": "Yahoo Finance / yfinance", "rows": {}, "errors": {}}
        if window.get("status") not in ("empty", "unavailable") and window.get("sessions"):
            from fetchers.us_market import fetch_us_market
            start = (datetime.fromisoformat(window["baseline_session"]) - timedelta(days=7)).date().isoformat()
            payload = fetch_us_market(symbols, start, window["expected_end_session"], timeout=25)
        result = build_summary(window, config, payload)
        result.update(schema_version=SCHEMA, config_sha256=config_hash, cache_identity=identity,
                      source_status="fetched")
        result["price_evidence"] = payload.get("rows", {})
        # Store each run under its content digest; later data revisions create a new artifact.
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        artifact = directory / "artifacts" / f"{digest}.json"
        if not artifact.exists():
            atomic_write_text(artifact, serialized)
        result["artifact_path"] = str(artifact.resolve())
        atomic_write_text(pointer, json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return result
    except Exception as exc:
        return unavailable(basis_date, as_of, f"{type(exc).__name__}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="补充已有每日复盘的美股区间概要")
    parser.add_argument("--html-path", type=Path, required=True)
    parser.add_argument("--basis-date", required=True)
    parser.add_argument("--as-of", help="带时区的ISO截止时间；默认本次执行时间")
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()
    from reporting.us_market_summary import update_reports
    # Binding validation happens before network access as well as under the update lock.
    import re
    header = re.search(r"<h1[^>]*>[^<]*今日复盘\s+(\d{4}-\d{2}-\d{2})", args.html_path.read_text(encoding="utf-8"))
    if not header or header.group(1) != args.basis_date:
        parser.error("报告依据日与 --basis-date 不一致")
    summary = collect_summary(args.basis_date, as_of=args.as_of, no_refresh=args.no_refresh)
    result = update_reports(args.html_path, summary)
    print(json.dumps({"update": result, "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
