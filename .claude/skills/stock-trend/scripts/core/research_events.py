"""Shared event assignment and date-weighted research statistics.

Research records are immutable observations.  A later signal for the same
security is not an independent event when its frozen holding interval overlaps
the earlier one.  This module keeps that identity rule in one place so
diagnostics, attribution and monitoring cannot silently use different samples.
"""

import math
from datetime import date


def _normalize_date(value, field):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        except ValueError as exc:
            raise ValueError(f"invalid_{field}") from exc
    try:
        parsed = date.fromisoformat(text[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field}") from exc
    if parsed.isoformat() != text[:10] or len(text) < 10:
        raise ValueError(f"invalid_{field}")
    return parsed.isoformat()


def _calendar_sessions(record, market_sessions):
    """Resolve a frozen trading calendar for one market, if supplied."""
    source = market_sessions
    if source is None and isinstance(record, dict):
        source = record.get("market_sessions")
    if isinstance(source, dict):
        market = str(record.get("market") or "").strip().upper()
        source = source.get(market) or source.get("*")
    if source is None:
        return None
    normalized = set()
    try:
        for value in source:
            normalized.add(_normalize_date(value, "market_session"))
    except (TypeError, ValueError):
        return set()
    return normalized


def assign_research_events(records, window, market_sessions=None):
    """Assign stable event ids to frozen records for one evaluation window.

    ``records`` must already contain the actual trading-session endpoints.  If
    a frozen ``market_sessions`` list (or per-market mapping) is supplied, both
    endpoints must be members of that calendar. Without one, weekends are
    rejected conservatively; callers with exchange holidays should provide
    the relevant calendar. A record with a missing/invalid endpoint is returned
    in ``invalid`` and never counted as an event. Overlap is inclusive: an
    entry on an earlier event's exit date belongs to that event. Market is part
    of identity, so the same code on different markets never merges.
    """
    if not window:
        raise ValueError("window_required")
    ordered = []
    invalid = []
    for original in records or []:
        if not isinstance(original, dict):
            invalid.append(original)
            continue
        record = dict(original)
        required = ("market", "code", "entry_date", "exit_date", "record_id")
        if any(not record.get(key) for key in required):
            invalid.append(record)
            continue
        try:
            entry = _normalize_date(record["entry_date"], "entry_date")
            exit_date = _normalize_date(record["exit_date"], "exit_date")
        except ValueError:
            invalid.append(record)
            continue
        if entry > exit_date:
            invalid.append(record)
            continue
        record["market"] = str(record["market"]).strip().upper()
        record["code"] = str(record["code"]).strip()
        if not record["market"] or not record["code"]:
            invalid.append(record)
            continue
        sessions = _calendar_sessions(record, market_sessions)
        if sessions is not None:
            if entry not in sessions or exit_date not in sessions:
                invalid.append(record)
                continue
        elif (date.fromisoformat(entry).weekday() >= 5
              or date.fromisoformat(exit_date).weekday() >= 5):
            invalid.append(record)
            continue
        record["entry_date"] = entry
        record["exit_date"] = exit_date
        record["window"] = str(window)
        ordered.append(record)

    ordered.sort(key=lambda row: (
        str(row.get("market", "")), str(row.get("code", "")),
        row["entry_date"], str(row.get("recommendation_date", "")),
        str(row.get("record_id", "")),
    ))
    events = []
    mapping = {}
    active = {}
    for record in ordered:
        key = (str(record["market"]), str(record["code"]))
        previous = active.get(key)
        if previous and record["entry_date"] <= previous["exit_date"]:
            mapping[str(record["record_id"])] = previous["event_id"]
            continue
        event_id = f"{window}:{record['record_id']}"
        event = {**record, "event_id": event_id}
        events.append(event)
        active[key] = event
        mapping[str(record["record_id"])] = event_id
    return {"events": events, "record_to_event": mapping, "invalid": invalid}


def summarize_daily_alpha(rows):
    """Average stocks equally within a date, then dates equally.

    Incomplete, non-finite and non-numeric rows are reported as missing rather
    than silently converted to zero.  The returned ``daily`` mapping is useful
    for audit output and is deterministic for the same input.
    """
    grouped = {}
    missing = 0
    for row in rows or []:
        if not isinstance(row, dict):
            missing += 1
            continue
        value = row.get("hs300_alpha")
        status = row.get("evaluation_status") or row.get("status")
        day = row.get("recommendation_date")
        if (status != "complete" or not day or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            missing += 1
            continue
        try:
            normalized_day = _normalize_date(day, "recommendation_date")
        except ValueError:
            missing += 1
            continue
        grouped.setdefault(normalized_day, []).append(float(value))
    daily = {
        day: sum(values) / len(values)
        for day, values in sorted(grouped.items())
    }
    mean_alpha = sum(daily.values()) / len(daily) if daily else None
    return {
        "daily": daily,
        "mature_dates": len(daily),
        "mean_alpha": mean_alpha,
        "missing_records": missing,
    }


__all__ = ["assign_research_events", "summarize_daily_alpha"]
