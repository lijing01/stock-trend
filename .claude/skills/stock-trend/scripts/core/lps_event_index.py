#!/usr/bin/env python3
"""Compact daily index of formal, healthy SOS-to-LPS observations."""

import copy
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .cache_utils import CACHE_DIR


SCHEMA_VERSION = "formal-lps-event-index/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "lps_event_history"
MAX_PRIOR_TRADING_DAYS = 5


def _day(value):
    text = str(value or "").strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat()


def _event_identity(event):
    if not isinstance(event, dict):
        return {}
    return {
        "event": event.get("type", ""),
        "event_date": event.get("event_date", ""),
        "confirmation_date": event.get("detected_date", ""),
        "range_id": event.get("range_id", ""),
        "status": event.get("status", ""),
        "event_index": event.get("event_index"),
        "detected_index": event.get("detected_index"),
    }


def _identity_matches(left, right):
    return bool(left) and all(
        left.get(key) == right.get(key)
        for key in (
            "event", "event_date", "confirmation_date", "range_id",
            "status", "event_index", "detected_index",
        )
    )


def _matching_formal_events(wyckoff):
    """Return the current healthy LPS and its exact confirmed SOS parent."""
    if not isinstance(wyckoff, dict):
        return None
    confirmed = wyckoff.get("confirmed_event") or {}
    health = wyckoff.get("event_health") or {}
    if (confirmed.get("type") != "lps"
            or confirmed.get("status") != "confirmed"
            or health.get("event_type") != "lps"
            or health.get("state") != "confirmed_holding"):
        return None

    history = wyckoff.get("_formal_event_history") or []
    lps_identity = _event_identity(confirmed)
    lps_matches = [
        event for event in history
        if isinstance(event, dict)
        and _identity_matches(lps_identity, _event_identity(event))
    ]
    if len(lps_matches) != 1:
        return None
    lps = lps_matches[0]
    if not _identity_matches(
            lps_identity, health.get("event_identity") or {}):
        return None

    parent_index = lps.get("parent_event_index")
    parent_type = lps.get("parent_event")
    parent_matches = [
        event for event in history
        if isinstance(event, dict)
        and event.get("type") == "sos"
        and event.get("status") == "confirmed"
        and event.get("event_index") == parent_index
        and event.get("range_id") == lps.get("range_id")
    ]
    if (parent_type != "sos" or not isinstance(parent_index, int)
            or len(parent_matches) != 1):
        return None
    sos = parent_matches[0]
    sos_detected_day = _day(sos.get("detected_date"))
    lps_event_day = _day(lps.get("event_date"))
    if (not _day(sos.get("event_date")) or not sos_detected_day
            or not lps_event_day or not _day(lps.get("detected_date"))
            or sos_detected_day > lps_event_day):
        return None
    return copy.deepcopy(lps), copy.deepcopy(sos), copy.deepcopy(health)


def _event_key(code, sos, lps):
    return "|".join(str(value or "") for value in (
        code, sos.get("event_date"), sos.get("detected_date"),
        lps.get("event_date"), lps.get("detected_date"),
        lps.get("structure_level"),
    ))


def _record(candidate, as_of_date):
    match = _matching_formal_events(candidate.get("wyckoff") or {})
    if match is None:
        return None
    lps, sos, health = match
    event_dates = [
        _day(event.get(key))
        for event in (sos, lps)
        for key in ("event_date", "detected_date")
    ]
    if any(not event_day or event_day > as_of_date for event_day in event_dates):
        return None
    code = str(candidate.get("code") or "").strip()
    if not code:
        return None
    event_key = _event_key(code, sos, lps)
    return {
        "event_key": event_key,
        "code": code,
        "name": str(candidate.get("name") or ""),
        "ts_code": str(candidate.get("ts_code") or ""),
        "as_of_date": as_of_date,
        "events": {
            "sos": sos,
            "lps": lps,
            "event_health": health,
        },
        "sector_memberships": copy.deepcopy(
            candidate.get("sector_memberships") or []),
    }


def _stored_record_valid(row, expected_day):
    if (not isinstance(row, dict)
            or _day(row.get("as_of_date")) != expected_day
            or not row.get("code")
            or not isinstance(row.get("sector_memberships"), list)):
        return False
    events = row.get("events") or {}
    lps = events.get("lps") or {}
    sos = events.get("sos") or {}
    reconstructed = {
        "confirmed_event": lps,
        "event_health": events.get("event_health") or {},
        "_formal_event_history": [sos, lps],
    }
    event_dates = [
        _day(event.get(key))
        for event in (sos, lps)
        for key in ("event_date", "detected_date")
    ]
    return (
        _matching_formal_events(reconstructed) is not None
        and all(value and value <= expected_day for value in event_dates)
        and row.get("event_key") == _event_key(row["code"], sos, lps)
    )


def build_daily_index(scored, as_of_date, scope_complete=True):
    """Build one day's fail-closed index from the formal scored population."""
    day = _day(as_of_date)
    if not day:
        raise ValueError("invalid as_of_date")
    complete = bool(scope_complete)
    records = {}
    if complete:
        for candidate in scored or []:
            if not isinstance(candidate, dict):
                continue
            record = _record(candidate, day)
            if record is not None:
                records[record["event_key"]] = record
    ordered_records = sorted(
        records.values(), key=lambda row: (row["code"], row["event_key"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope_complete": complete,
        "status": "complete" if complete else "incomplete_scope",
        "record_count": len(ordered_records),
        "records": ordered_records,
    }


def save_daily_index(index, root=DEFAULT_ROOT):
    """Atomically overwrite a daily runtime observation index."""
    if not isinstance(index, dict) or index.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid LPS event index")
    day = _day(index.get("as_of_date"))
    records = index.get("records")
    if not day or not isinstance(records, list):
        raise ValueError("invalid LPS event index")
    if not index.get("scope_complete") and records:
        raise ValueError("incomplete scope must not contain LPS records")

    target_root = Path(root)
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{day}.json"
    if not index.get("scope_complete") and target.exists():
        try:
            with open(target, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, UnicodeError, ValueError, TypeError):
            existing = None
        if (isinstance(existing, dict)
                and existing.get("schema_version") == SCHEMA_VERSION
                and _day(existing.get("as_of_date")) == day
                and existing.get("scope_complete") is True
                and isinstance(existing.get("records"), list)
                and existing.get("record_count") == len(existing["records"])
                and all(_stored_record_valid(row, day)
                        for row in existing["records"])):
            return {
                "status": "preserved_complete",
                "path": str(target),
                "as_of_date": day,
                "scope_complete": True,
                "record_count": len(existing["records"]),
            }
    fd, temporary = tempfile.mkstemp(dir=target_root, prefix=f".{day}.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {
        "status": "saved",
        "path": str(target),
        "as_of_date": day,
        "scope_complete": bool(index.get("scope_complete")),
        "record_count": len(records),
    }


def load_prior_lps(as_of_date, trading_dates, root=DEFAULT_ROOT):
    """Load and deduplicate formal LPS observations from five prior sessions."""
    day = _day(as_of_date)
    if not day:
        raise ValueError("invalid as_of_date")
    prior_days = sorted({
        normalized for value in (trading_dates or [])
        if (normalized := _day(value)) and normalized < day
    })[-MAX_PRIOR_TRADING_DAYS:]

    loaded_days = []
    missing_days = []
    incomplete_days = []
    invalid_days = []
    observations = []
    target_root = Path(root)
    for prior_day in prior_days:
        path = target_root / f"{prior_day}.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                index = json.load(handle)
        except FileNotFoundError:
            missing_days.append(prior_day)
            continue
        except (OSError, UnicodeError, ValueError, TypeError):
            invalid_days.append(prior_day)
            continue
        records = index.get("records") if isinstance(index, dict) else None
        if (not isinstance(index, dict)
                or index.get("schema_version") != SCHEMA_VERSION
                or _day(index.get("as_of_date")) != prior_day
                or not isinstance(records, list)
                or index.get("record_count") != len(records)
                or (not index.get("scope_complete") and bool(records))):
            invalid_days.append(prior_day)
            continue
        if not index.get("scope_complete"):
            incomplete_days.append(prior_day)
            continue
        if not all(_stored_record_valid(row, prior_day) for row in records):
            invalid_days.append(prior_day)
            continue
        valid_records = copy.deepcopy(records)
        loaded_days.append(prior_day)
        observations.extend(valid_records)

    deduplicated = {}
    for row in sorted(observations, key=lambda item: item.get("as_of_date", "")):
        key = row["event_key"]
        previous = deduplicated.get(key)
        observed_dates = list((previous or {}).get("observed_dates") or [])
        observed_day = _day(row.get("as_of_date"))
        if observed_day and observed_day not in observed_dates:
            observed_dates.append(observed_day)
        row["observed_dates"] = observed_dates
        row["observation_count"] = len(observed_dates)
        deduplicated[key] = row
    records = sorted(
        deduplicated.values(),
        key=lambda row: (row.get("code", ""), row.get("event_key", "")),
    )
    calendar_complete = len(prior_days) == MAX_PRIOR_TRADING_DAYS
    unavailable = missing_days + incomplete_days + invalid_days
    metadata = {
        "status": "complete" if calendar_complete and not unavailable else (
            "partial" if loaded_days else "unavailable"),
        "as_of_date": day,
        "calendar_complete": calendar_complete,
        "requested_dates": prior_days,
        "loaded_dates": loaded_days,
        "missing_dates": missing_days,
        "incomplete_dates": incomplete_days,
        "invalid_dates": invalid_days,
        "observation_count": len(observations),
        "record_count": len(records),
        "unique_code_count": len({row["code"] for row in records}),
    }
    return records, metadata
