#!/usr/bin/env python3
"""Pure window and return calculations for the U.S. market report section."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALENDAR = SKILL_ROOT / "data" / "us_market_calendar.json"
DEFAULT_WATCHLIST = SKILL_ROOT / "data" / "us_market_watchlist.yaml"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field}") from exc


def _parse_aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("invalid_as_of") from exc
    else:
        raise ValueError("invalid_as_of")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of_must_be_timezone_aware")
    return parsed


def _load_calendar(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"version", "timezone", "regular_close", "coverage", "holidays", "early_closes"}
    if not isinstance(raw, dict) or not required.issubset(raw):
        raise ValueError("invalid_calendar_config")
    coverage = raw["coverage"]
    if (not isinstance(coverage, dict) or not isinstance(raw["holidays"], list)
            or not isinstance(raw["early_closes"], dict)):
        raise ValueError("invalid_calendar_config")
    start = _parse_date(coverage.get("start"), "calendar_start")
    end = _parse_date(coverage.get("end"), "calendar_end")
    if start > end:
        raise ValueError("invalid_calendar_coverage")
    holidays = {_parse_date(item, "holiday") for item in raw["holidays"]}
    early_closes = {
        _parse_date(day, "early_close"): str(close)
        for day, close in raw["early_closes"].items()
    }
    if any(day.weekday() >= 5 or day in holidays for day in early_closes):
        raise ValueError("invalid_early_close_session")
    try:
        ZoneInfo(str(raw["timezone"]))
        _clock(str(raw["regular_close"]))
        for close in early_closes.values():
            _clock(close)
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid_calendar_config") from exc
    raw["_coverage"] = (start, end)
    raw["_holidays"] = holidays
    raw["_early_closes"] = early_closes
    return raw


def _clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_calendar_close_time") from exc


def _is_session(day: date, calendar: dict) -> bool:
    return day.weekday() < 5 and day not in calendar["_holidays"]


def _session_close(day: date, calendar: dict) -> datetime:
    close_value = calendar["_early_closes"].get(day, calendar["regular_close"])
    return datetime.combine(day, _clock(close_value), ZoneInfo(calendar["timezone"]))


def _unavailable_window(anchor_at: datetime, as_of: datetime, calendar: dict | None,
                        reason: str, status: str = "calendar_unavailable") -> dict:
    return {
        "anchor_at": anchor_at.isoformat(),
        "as_of": as_of.isoformat(),
        "baseline_session": None,
        "expected_end_session": None,
        "sessions": [],
        "calendar_version": calendar.get("version") if calendar else None,
        "status": status,
        "reason": reason,
    }


def resolve_window(basis_date: Any, as_of: Any, calendar_path: Any = None) -> dict:
    """Resolve completed NYSE sessions after the A-share close anchor.

    The annual calendar is deliberately bounded. A required baseline or scan day
    outside that coverage produces ``calendar_unavailable`` instead of inferring
    a session from weekdays alone.
    """
    basis = _parse_date(basis_date, "basis_date")
    cutoff = _parse_aware_datetime(as_of)
    anchor_at = datetime.combine(basis, time(15, 0), SHANGHAI)
    try:
        calendar = _load_calendar(Path(calendar_path or DEFAULT_CALENDAR))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _unavailable_window(anchor_at, cutoff, None, str(exc))

    if cutoff < anchor_at:
        return _unavailable_window(
            anchor_at, cutoff, calendar, "as_of_before_anchor", status="invalid_window")

    start, end = calendar["_coverage"]
    anchor_ny = anchor_at.astimezone(ZoneInfo(calendar["timezone"]))
    cutoff_ny = cutoff.astimezone(ZoneInfo(calendar["timezone"]))
    scan_start = max(start, anchor_ny.date() - timedelta(days=10))
    scan_end = cutoff_ny.date()
    if scan_end > end or scan_end < start:
        return _unavailable_window(anchor_at, cutoff, calendar, "calendar_coverage_insufficient")

    completed_before_anchor = []
    completed_after_anchor = []
    day = scan_start
    while day <= scan_end:
        if _is_session(day, calendar):
            close_at = _session_close(day, calendar)
            if close_at <= anchor_at:
                completed_before_anchor.append(day)
            elif anchor_at < close_at <= cutoff:
                completed_after_anchor.append(day)
        day += timedelta(days=1)

    if not completed_before_anchor:
        return _unavailable_window(anchor_at, cutoff, calendar, "baseline_session_unavailable")
    baseline = completed_before_anchor[-1]
    sessions = [item.isoformat() for item in completed_after_anchor]
    return {
        "anchor_at": anchor_at.isoformat(),
        "as_of": cutoff.isoformat(),
        "baseline_session": baseline.isoformat(),
        "expected_end_session": sessions[-1] if sessions else None,
        "sessions": sessions,
        "calendar_version": calendar["version"],
        "status": "complete" if sessions else "empty",
    }


def load_watchlist(path: Any = None) -> dict:
    config_path = Path(path or DEFAULT_WATCHLIST)
    content = config_path.read_bytes()
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise ValueError("invalid_watchlist_config")
    result = {
        "schema_version": raw.get("schema_version"),
        "version": raw.get("version"),
        "config_sha256": hashlib.sha256(content).hexdigest(),
    }
    seen = set()
    for group in ("indices", "sectors", "stocks"):
        entries = raw.get(group)
        if not isinstance(entries, list):
            raise ValueError(f"invalid_watchlist_{group}")
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict) or not all(entry.get(key) for key in ("symbol", "name", "sector")):
                raise ValueError(f"invalid_watchlist_{group}_entry")
            symbol = str(entry["symbol"]).strip().upper()
            if symbol in seen:
                raise ValueError(f"duplicate_watchlist_symbol:{symbol}")
            seen.add(symbol)
            normalized.append({
                "symbol": symbol,
                "name": str(entry["name"]).strip(),
                "sector": str(entry["sector"]).strip(),
            })
        result[group] = normalized
    return result


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _pct(current: float, previous: float) -> float:
    return round((current / previous - 1.0) * 100.0, 6)


def _normalize_prices(records: Any, allowed_dates: set[str]) -> tuple[dict[str, float], list[str]]:
    if not isinstance(records, list):
        return {}, ["missing_rows"]
    prices = {}
    errors = []
    for record in records:
        if not isinstance(record, dict):
            errors.append("invalid_record")
            continue
        try:
            day = _parse_date(record.get("date"), "price_date").isoformat()
        except ValueError:
            errors.append("invalid_date")
            continue
        if day not in allowed_dates:
            continue
        if day in prices:
            errors.append(f"duplicate_date:{day}")
            continue
        price = _finite_positive(record.get("adj_close"))
        if price is None:
            errors.append(f"missing_or_invalid_adj_close:{day}")
            continue
        prices[day] = price
    return prices, errors


def _build_row(entry: dict, window: dict, payload: dict, spy_interval: float | None) -> dict:
    symbol = entry["symbol"]
    baseline = window["baseline_session"]
    expected_end = window["expected_end_session"]
    sessions = list(window["sessions"])
    required_dates = {baseline, *sessions}
    prices, validation_errors = _normalize_prices(
        (payload.get("rows") or {}).get(symbol), required_dates)
    source_error = (payload.get("errors") or {}).get(symbol)
    reasons = list(validation_errors)
    if source_error:
        reasons.append(f"provider_error:{source_error}")

    baseline_price = prices.get(baseline)
    end_price = prices.get(expected_end) if expected_end else None
    endpoint_ok = baseline_price is not None and end_price is not None
    interval_pct = _pct(end_price, baseline_price) if endpoint_ok else None

    daily = []
    previous_day = baseline
    for day in sessions:
        price = prices.get(day)
        previous_price = prices.get(previous_day)
        change = _pct(price, previous_price) if price is not None and previous_price is not None else None
        daily.append({"date": day, "price": price, "change_pct": change})
        if price is None:
            reasons.append(f"missing_adj_close:{day}")
        if previous_price is None:
            reasons.append(f"missing_previous_adj_close:{previous_day}")
        previous_day = day

    actual_dates = [day for day in sessions if day in prices]
    actual_end = actual_dates[-1] if actual_dates else None
    daily_pct = daily[-1]["change_pct"] if daily else None
    hard_invalid = any(reason.startswith(("duplicate_date:", "invalid_")) for reason in reasons)
    if hard_invalid:
        status = "invalid"
        interval_pct = daily_pct = None
        endpoint_ok = False
    elif not endpoint_ok:
        status = "unavailable"
    elif reasons or any(item["change_pct"] is None for item in daily):
        status = "partial"
    else:
        status = "complete"

    relative = None
    if interval_pct is not None and spy_interval is not None:
        relative = round(interval_pct - spy_interval, 6)
    return {
        **entry,
        "interval_pct": interval_pct,
        "daily_pct": daily_pct,
        "relative_spy_pp": relative,
        "baseline_price": baseline_price if not hard_invalid else None,
        "end_price": end_price if not hard_invalid else None,
        "actual_end_session": actual_end,
        "status": status,
        "daily": daily,
        "reasons": sorted(set(reasons)),
    }


def build_summary(window: dict, config: dict, payload: dict) -> dict:
    """Build a deterministic artifact from an already-fetched adjusted-close payload."""
    artifact = {
        "schema_version": "us-market-summary/v1",
        "basis_date": window.get("anchor_at", "")[:10] or None,
        "anchor_at": window.get("anchor_at"),
        "as_of": window.get("as_of"),
        "baseline_session": window.get("baseline_session"),
        "expected_end_session": window.get("expected_end_session"),
        "actual_end_session": None,
        "sessions": list(window.get("sessions") or []),
        "calendar_version": window.get("calendar_version"),
        "config_sha256": config.get("config_sha256"),
        "provider": payload.get("provider"),
        "fetched_at": payload.get("fetched_at"),
        "adjustment_mode": "adj_close",
        "status": window.get("status"),
        "data_quality": "unavailable",
        "groups": {"indices": [], "sectors": [], "stocks": []},
        "errors": dict(payload.get("errors") or {}),
    }
    if window.get("status") != "complete":
        artifact["data_quality"] = "empty" if window.get("status") == "empty" else "unavailable"
        if window.get("reason"):
            artifact["errors"]["calendar"] = window["reason"]
        return artifact

    spy_entry = next((entry for entry in config.get("indices", []) if entry["symbol"] == "SPY"), None)
    spy_probe = _build_row(spy_entry, window, payload, None) if spy_entry else None
    spy_interval = spy_probe["interval_pct"] if spy_probe else None
    all_rows = []
    for group in ("indices", "sectors", "stocks"):
        for entry in config.get(group, []):
            row = _build_row(entry, window, payload, spy_interval)
            artifact["groups"][group].append(row)
            all_rows.append(row)

    actual_dates = [row["actual_end_session"] for row in all_rows if row["actual_end_session"]]
    artifact["actual_end_session"] = max(actual_dates) if actual_dates else None
    statuses = {row["status"] for row in all_rows}
    if statuses == {"complete"}:
        artifact["data_quality"] = "complete"
    elif any(row["interval_pct"] is not None for row in all_rows):
        artifact["data_quality"] = "partial"
    else:
        artifact["data_quality"] = "unavailable"
    artifact["status"] = artifact["data_quality"]
    return artifact
