"""Collect independent US context or supplement an existing daily-review report."""

import argparse
import copy
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parents[3]
sys.path.insert(0, str(SCRIPTS))

from core.report_file import atomic_write_text

SCHEMA = "us-market-summary/v2"
TIME_VERSION = "a-share-completed-close/v2"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _observed_now():
    return datetime.now(SHANGHAI)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def resolve_anchor(as_of, cache_dir, *, no_refresh=False, frozen_closes=None,
                   explicit_date=None):
    """Locate local evidence before locating any US cache; never infer holidays."""
    cutoff = as_of.astimezone(SHANGHAI)
    directory = Path(cache_dir) / "review_time"
    if not no_refresh:
        from fetchers.sector_data import _load_authoritative_trading_dates
        dates = sorted(_load_authoritative_trading_dates(cutoff))
        if dates:
            evidence = {"schema": "review-time/v1", "trading_dates": dates,
                        "coverage": {"start": dates[0], "end": dates[-1]},
                        "source": "AKShare/tool_trade_date_hist_sina",
                        "fetched_at": _observed_now().isoformat(),
                        "confirmation_time": "15:10", "anchor_time": "15:00",
                        "timezone": "Asia/Shanghai"}
            atomic_write_text(directory / f"{_digest(evidence)}.json",
                              json.dumps(evidence, ensure_ascii=False, indent=2))
    eligible = []
    for path in directory.glob("*.json"):
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            frozen = datetime.fromisoformat(evidence["fetched_at"])
            dates = evidence["trading_dates"]
            if (evidence.get("schema") != "review-time/v1" or frozen.tzinfo is None
                    or frozen > as_of or _digest(evidence) != path.stem
                    or evidence.get("confirmation_time") != "15:10"
                    or evidence.get("anchor_time") != "15:00"
                    or evidence.get("timezone") != "Asia/Shanghai"
                    or not evidence.get("source") or not dates
                    or dates != sorted(set(dates))):
                continue
            parsed = [date.fromisoformat(day) for day in dates]
            coverage = evidence["coverage"]
            if coverage != {"start": dates[0], "end": dates[-1]}:
                continue
            if not parsed[0] <= cutoff.date() <= parsed[-1]:
                continue
            candidates = [{"date": day, "confirmed_at": datetime.combine(
                date.fromisoformat(day), time(15, 10), SHANGHAI).isoformat(),
                "completed": datetime.combine(date.fromisoformat(day), time(15, 10), SHANGHAI) <= cutoff}
                for day in [value for value in dates if value <= cutoff.date().isoformat()][-2:]]
            completed = [item["date"] for item in candidates if item["completed"]]
            if completed:
                selected = explicit_date or max(completed)
                if explicit_date:
                    confirmed = datetime.combine(date.fromisoformat(explicit_date), time(15, 10), SHANGHAI)
                    if explicit_date not in dates or confirmed > cutoff:
                        continue
                    if explicit_date not in [item["date"] for item in candidates]:
                        candidates.append({"date": explicit_date, "confirmed_at": confirmed.isoformat(),
                                           "completed": True, "selection": "explicit"})
                eligible.append((frozen, {"a_share_anchor_date": selected,
                    "status": "explicit" if explicit_date else "verified",
                    "latest_confirmed": selected == max(completed),
                    "evidence_sha256": path.stem, "evidence_path": str(path.resolve()),
                    "candidates": candidates, "source": evidence["source"]}))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if eligible:
        return max(eligible, key=lambda item: item[0])[1]
    # A caller may supply explicitly verified frozen *completed close* records.
    completed = []
    for row in frozen_closes or []:
        try:
            day = date.fromisoformat(row["data_date"])
            frozen = datetime.fromisoformat(row["frozen_at"])
            confirmed = datetime.combine(day, time(15, 10), SHANGHAI)
            close = float(row["close"])
            if (row.get("date_origin") == "provider" and row.get("completed_close") is True
                    and row.get("source") and row.get("code")
                    and math.isfinite(close) and close > 0 and frozen.tzinfo is not None
                    and confirmed <= frozen <= as_of
                    and (explicit_date is None or row["data_date"] == explicit_date)):
                completed.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    if completed:
        row = max(completed, key=lambda item: item["data_date"])
        return {"a_share_anchor_date": row["data_date"], "status": "degraded",
                "latest_confirmed": False, "reason": "最近交易日未确认，仅有已完成冻结K线",
                "evidence_sha256": _digest(row), "candidates": completed,
                "source": row["source"]}
    return {"a_share_anchor_date": None, "status": "unavailable",
            "latest_confirmed": False, "reason": "无覆盖截止时刻的冻结A股日历或已完成K线证据"}


def unavailable(basis_date, as_of, reason):
    return {"schema_version": SCHEMA, "basis_date": basis_date,
            "a_share_anchor_date": None, "time_version": TIME_VERSION,
            "as_of": as_of.isoformat(), "data_quality": "unavailable",
            "status": "unavailable", "groups": {}, "errors": {"collection": reason}}


def collect_summary(basis_date, *, as_of=None, no_refresh=False, cache_dir=None,
                    anchor_mode="basis", a_share_anchor_date=None, frozen_closes=None,
                    calendar_prepared=False):
    """Freeze the cutoff once; never write this context into A-share score history."""
    root = Path(cache_dir or os.environ.get(
        "STOCK_TREND_CACHE_DIR", str(ROOT / ".cache" / "stock-trend")))
    preparation_error = None
    if as_of is None and (anchor_mode == "completed" or a_share_anchor_date) and not no_refresh:
        # Acquire calendar evidence before freezing the run cutoff. Its actual
        # acquisition time must not be relabelled as an earlier replay time.
        try:
            resolve_anchor(_observed_now(), root, no_refresh=False)
        except Exception as exc:
            preparation_error = f"{type(exc).__name__}: {exc}"
        calendar_prepared = True
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    as_of = as_of or _observed_now()
    if as_of.tzinfo is None:
        raise ValueError("as_of must include timezone")
    if anchor_mode not in ("basis", "completed"):
        raise ValueError("unknown anchor mode")
    try:
        from analysis.us_market_summary import resolve_window, load_watchlist, build_summary
        root = Path(cache_dir or os.environ.get(
            "STOCK_TREND_CACHE_DIR", str(ROOT / ".cache" / "stock-trend")))
        if a_share_anchor_date:
            explicit_date = date.fromisoformat(a_share_anchor_date).isoformat()
            anchor = resolve_anchor(as_of, root, no_refresh=no_refresh or calendar_prepared,
                                    frozen_closes=frozen_closes, explicit_date=explicit_date)
            if not anchor["a_share_anchor_date"]:
                return unavailable(basis_date, as_of, preparation_error or "explicit_anchor_not_verified_completed_session")
            anchor["selection"] = "explicit"
        elif anchor_mode == "completed":
            anchor = resolve_anchor(as_of, root, no_refresh=no_refresh or calendar_prepared,
                                    frozen_closes=frozen_closes)
            if not anchor["a_share_anchor_date"]:
                result = unavailable(basis_date, as_of, preparation_error or anchor["reason"])
                result["anchor_evidence"] = anchor
                return result
        else:
            anchor = {"a_share_anchor_date": basis_date, "status": "legacy_basis",
                      "source": "report_basis_date", "latest_confirmed": False}
        window = resolve_window(basis_date, as_of, a_share_anchor_date=anchor["a_share_anchor_date"],
                                anchor_evidence=anchor)
        config = load_watchlist()
        config_hash = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        identity = {"schema": SCHEMA, "basis_date": basis_date,
                    "time_version": TIME_VERSION, "anchor_mode": anchor_mode,
                    "anchor_origin": anchor["status"],
                    "a_share_anchor_date": anchor["a_share_anchor_date"],
                    "anchor_evidence": anchor.get("evidence_sha256"),
                    "latest": window.get("latest_completed_session"),
                    "baseline": window.get("baseline_session"),
                    "end": window.get("expected_end_session"),
                    "calendar": window.get("calendar_version"), "config": config_hash,
                    "adjustment": "adj_close"}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = root / "us_market"
        pointer = directory / f"{key}.json"
        if no_refresh:
            try:
                result = json.loads(pointer.read_text(encoding="utf-8"))
                if result.get("cache_identity") != identity:
                    raise ValueError("cache identity mismatch")
                if result.get("schema_version") != SCHEMA:
                    raise ValueError("cache schema mismatch")
                if (datetime.fromisoformat(result["as_of"]) > as_of
                        or datetime.fromisoformat(result["frozen_at"]) > as_of):
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
        if window.get("status") in ("complete", "empty") and window.get("latest_completed_session"):
            from fetchers.us_market import fetch_us_market
            start = min(window["baseline_session"], window["latest_previous_session"])
            payload = fetch_us_market(symbols, start, window["latest_completed_session"], timeout=25)
        result = build_summary(window, config, payload)
        result.update(schema_version=SCHEMA, config_sha256=config_hash, cache_identity=identity,
                      source_status="fetched", basis_date=basis_date,
                      a_share_anchor_date=anchor["a_share_anchor_date"], anchor_evidence=anchor,
                      time_version=TIME_VERSION, frozen_at=_observed_now().isoformat())
        result["price_evidence"] = payload.get("rows", {})
        if preparation_error:
            result["errors"]["calendar_preparation"] = preparation_error
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
    parser.add_argument("--a-share-anchor-date", help="显式独立已完成A股锚点；默认保留报告依据日15:00语义")
    args = parser.parse_args()
    from reporting.us_market_summary import update_reports
    # Binding validation happens before network access as well as under the update lock.
    import re
    header = re.search(r"<h1[^>]*>[^<]*今日复盘\s+(\d{4}-\d{2}-\d{2})", args.html_path.read_text(encoding="utf-8"))
    if not header or header.group(1) != args.basis_date:
        parser.error("报告依据日与 --basis-date 不一致")
    summary = collect_summary(args.basis_date, as_of=args.as_of, no_refresh=args.no_refresh,
                              a_share_anchor_date=args.a_share_anchor_date)
    result = update_reports(args.html_path, summary)
    print(json.dumps({"update": result, "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
