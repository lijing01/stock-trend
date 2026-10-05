#!/usr/bin/env python3
"""Pure calculations for bounded daily-review sector persistence evidence."""

from __future__ import annotations

import math
from datetime import date


CLASSIFICATION_VERSION = "eastmoney-industry-concept/v1"
PRICE_TYPE = "forward_adjusted_close"
SOURCE = "eastmoney"


def _iso_date(value) -> str | None:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError):
        return None


def sector_identity(sector: dict) -> dict:
    code = str(sector.get("code") or sector.get("provider_code") or "")
    kind = str(sector.get("type") or "")
    source = str(sector.get("provider") or sector.get("source") or SOURCE)
    sector_id = str(
        sector.get("sector_id") or f"{source}:{kind}:{code}"
    )
    return {
        "code": code,
        "name": str(sector.get("name") or code),
        "type": kind,
        "source": source,
        "sector_id": sector_id,
        "classification_version": str(
            sector.get("classification_version") or CLASSIFICATION_VERSION
        ),
        "price_type": str(sector.get("price_type") or PRICE_TYPE),
    }


def select_displayed_sectors(strongest, weakest, observation=()) -> list[dict]:
    """Return the bounded, stable union of top10, bottom3 and observed sectors."""
    selected: list[dict] = []
    positions: dict[str, int] = {}
    for role, rows, limit in (
        ("strongest", strongest or (), 10),
        ("weakest", weakest or (), 3),
        ("observation", observation or (), None),
    ):
        bounded = list(rows)[:limit] if limit is not None else list(rows)
        for raw in bounded:
            if not isinstance(raw, dict):
                continue
            identity = sector_identity(raw)
            if not identity["code"] or not identity["type"]:
                continue
            key = identity["sector_id"]
            if key in positions:
                target = selected[positions[key]]
                if role not in target["roles"]:
                    target["roles"].append(role)
                continue
            item = dict(raw)
            item.update(identity)
            item["roles"] = [role]
            positions[key] = len(selected)
            selected.append(item)
    return selected


def _window_return(by_date: dict, expected_dates: list[str], periods: int):
    required = expected_dates[-(periods + 1):]
    if len(required) != periods + 1:
        return None, f"window_{periods}d_insufficient"
    if any(day not in by_date for day in required):
        available = [day for day in required if day in by_date]
        if available and available == required[-len(available):]:
            return None, f"window_{periods}d_insufficient"
        return None, f"window_{periods}d_not_contiguous"
    start = by_date[required[0]]["close"]
    end = by_date[required[-1]]["close"]
    if start <= 0 or end <= 0:
        return None, f"window_{periods}d_invalid_close"
    return end / start - 1.0, ""


def _constituent_up_ratio(sector: dict):
    values = [sector.get(key) for key in ("up_count", "down_count", "flat_count")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(float(value)) or value < 0 for value in values):
        return None
    covered = sum(values)
    total = sector.get("total_count")
    if (isinstance(total, bool) or not isinstance(total, (int, float))
            or not math.isfinite(float(total)) or total <= 0
            or covered != total):
        return None
    return values[0] / total


def analyze_sector_series(sector: dict, records: list[dict], *,
                          trading_dates: list[str], basis_date: str) -> dict:
    """Calculate 5/20-session returns only from verified contiguous endpoints."""
    identity = sector_identity(sector)
    basis = _iso_date(basis_date)
    authority = sorted({day for value in trading_dates
                        if (day := _iso_date(value)) and day <= (basis or "")})
    expected = authority[-21:]
    reasons: list[str] = []
    if not basis or not authority or authority[-1] != basis:
        reasons.append("authority_calendar_unverified")

    by_date: dict[str, dict] = {}
    mixed = set()
    expected_fields = {
        "sector_id": identity["sector_id"],
        "classification_version": identity["classification_version"],
        "source": identity["source"],
        "price_type": identity["price_type"],
    }
    for raw in records or ():
        if not isinstance(raw, dict):
            continue
        for field, expected_value in expected_fields.items():
            actual = raw.get(field)
            if str(actual or "") != expected_value:
                mixed.add(field)
        day = _iso_date(raw.get("trade_date"))
        close = raw.get("close")
        if (not day or day > (basis or "") or isinstance(close, bool)
                or not isinstance(close, (int, float))
                or not math.isfinite(float(close)) or close <= 0):
            continue
        if day in by_date:
            reasons.append("duplicate_trade_date")
        by_date[day] = {"trade_date": day, "close": float(close)}
    reasons.extend(f"mixed_{field}" for field in sorted(mixed))

    return_5d = return_20d = None
    if not reasons:
        return_5d, reason_5d = _window_return(by_date, expected, 5)
        return_20d, reason_20d = _window_return(by_date, expected, 20)
        reasons.extend(reason for reason in (reason_5d, reason_20d) if reason)

    consecutive = None
    consecutive_lower_bound = False
    if not mixed and "duplicate_trade_date" not in reasons \
            and authority and authority[-1] in by_date:
        consecutive = 0
        for index in range(len(authority) - 1, 0, -1):
            current = by_date.get(authority[index])
            previous = by_date.get(authority[index - 1])
            if not current:
                break
            if not previous:
                consecutive_lower_bound = consecutive > 0
                break
            if current["close"] <= previous["close"]:
                break
            consecutive += 1

    status = "complete" if return_20d is not None else (
        "partial" if return_5d is not None else "missing")
    return {
        **identity,
        "roles": list(sector.get("roles") or []),
        "status": status,
        "basis_date": basis_date,
        "return_5d": return_5d,
        "return_20d": return_20d,
        "consecutive_up_days": consecutive,
        "consecutive_up_days_lower_bound": consecutive_lower_bound,
        "constituent_up_ratio": _constituent_up_ratio(sector),
        "constituent_coverage": {
            key: sector.get(key) for key in
            ("up_count", "down_count", "flat_count", "total_count")
        },
        "ranking_scope": "today_displayed",
        "reasons": list(dict.fromkeys(reasons)),
    }


def analyze_frozen_artifact(artifact: dict) -> dict:
    items = []
    dates = artifact.get("trading_dates") or []
    basis = artifact.get("basis_date") or ""
    for frozen in artifact.get("sectors") or []:
        if not isinstance(frozen, dict):
            continue
        items.append(analyze_sector_series(
            frozen, frozen.get("records") or [],
            trading_dates=dates, basis_date=basis,
        ))
    statuses = {item["status"] for item in items}
    status = "complete" if items and statuses == {"complete"} else (
        "degraded" if items else "missing")
    return {
        "schema_version": "sector-persistence-analysis/v1",
        "status": status,
        "basis_date": basis,
        "artifact_digest": artifact.get("content_digest", ""),
        "ranking_scope": "today_displayed",
        "items": items,
        "reasons": list(artifact.get("reasons") or []),
    }
