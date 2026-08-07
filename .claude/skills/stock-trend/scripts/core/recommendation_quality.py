#!/usr/bin/env python3
"""Data-quality policy for promoting scan candidates to recommendations."""
from datetime import datetime


WEIGHTS = {"kline": 0.55, "capital": 0.25, "fundamental": 0.20}
MIN_COVERAGE = 0.70


def _iso_date(value):
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def latest_data_date(payload):
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    dates = [
        _iso_date(row.get("trade_date") or row.get("date"))
        for row in rows
        if isinstance(row, dict)
    ]
    valid = [value for value in dates if value]
    return max(valid) if valid else ""


def _dimension(name, payload, expected_date="", require_date=False):
    returned = isinstance(payload, dict) and bool(payload)
    available = returned
    quality = "missing"
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    if available:
        quality = (
            payload.get("summary", {}).get("data_quality")
            or payload.get("data_quality")
            or "good"
        )
        if meta.get("data_source") == "error" \
                or meta.get("error") or payload.get("error"):
            quality = "error"
        if name == "capital" and not latest_data_date(payload):
            quality = "error"
        if quality == "error":
            available = False
    data_date = latest_data_date(payload)
    source = meta.get("data_source") or meta.get("source")
    fetched_at = meta.get("fetch_time") or meta.get("fetched_at")
    if isinstance(payload, dict):
        source = source or payload.get("source", "")
        fetched_at = fetched_at or payload.get("fetched_at", "")
    source = source or ""
    fetched_at = fetched_at or ""
    fresh = available
    if require_date:
        fresh = available and bool(data_date) and data_date >= expected_date
    elif name == "fundamental" and expected_date:
        fetched_date = _iso_date(str(fetched_at)[:8])
        fresh = available and bool(fetched_date) and fetched_date >= expected_date
    stale_reason = ""
    if returned and quality == "error":
        stale_reason = f"{name}_error"
    elif require_date and available and not data_date:
        stale_reason = f"{name}_date_missing"
    elif available and not fresh:
        stale_reason = f"{name}_stale"
    return {
        "returned": returned,
        "available": available,
        "fresh": fresh,
        "data_date": data_date,
        "fetched_at": fetched_at,
        "source": source,
        "quality": quality,
        "stale_reason": stale_reason,
    }


def assess_candidate_data(kline, capital, fundamental, as_of_date=""):
    normalized_as_of = _iso_date(as_of_date)
    kline_date = latest_data_date(kline)
    expected = normalized_as_of or kline_date
    dimensions = {
        "kline": _dimension("kline", kline, expected, require_date=True),
        "capital": _dimension("capital", capital, expected, require_date=True),
        "fundamental": _dimension("fundamental", fundamental, expected),
    }
    coverage = round(sum(
        WEIGHTS[name]
        for name, status in dimensions.items()
        if status["available"] and status["fresh"]
    ), 2)
    reasons = []
    if not dimensions["kline"]["fresh"]:
        reasons.append("kline_stale")
    if coverage < MIN_COVERAGE:
        reasons.append("coverage_below_70pct")
    secondary_available = any(
        dimensions[name]["available"] for name in ("capital", "fundamental")
    )
    if not secondary_available:
        reasons.append("secondary_data_missing")
    returned_errors = [
        f"{name}_error" for name in ("capital", "fundamental")
        if dimensions[name]["returned"] and dimensions[name]["quality"] == "error"
    ]
    reasons.extend(returned_errors)
    has_stale_or_error = any(
        status["stale_reason"] for status in dimensions.values()
    )
    freshness_factor = 0.5 if has_stale_or_error else 1.0
    coverage_factor = coverage
    return {
        "as_of_date": expected,
        "coverage": coverage,
        "coverage_factor": coverage_factor,
        "freshness_factor": freshness_factor,
        "confidence": round(coverage_factor * freshness_factor, 2),
        "eligible": (
            dimensions["kline"]["fresh"]
            and coverage >= MIN_COVERAGE
            and secondary_available
            and not returned_errors
        ),
        "dimensions": dimensions,
        "reasons": reasons,
    }
