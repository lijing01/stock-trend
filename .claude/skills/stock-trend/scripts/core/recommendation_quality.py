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


def _dimension(payload, expected_date="", require_date=False):
    available = isinstance(payload, dict) and bool(payload)
    quality = "missing"
    if available:
        quality = payload.get("summary", {}).get("data_quality") or "good"
        if quality == "error":
            available = False
    data_date = latest_data_date(payload)
    fresh = available
    if require_date:
        fresh = available and bool(data_date) and data_date >= expected_date
    return {
        "available": available,
        "fresh": fresh,
        "data_date": data_date,
        "quality": quality,
    }


def assess_candidate_data(kline, capital, fundamental, as_of_date=""):
    normalized_as_of = _iso_date(as_of_date)
    kline_date = latest_data_date(kline)
    expected = normalized_as_of or kline_date
    dimensions = {
        "kline": _dimension(kline, expected, require_date=True),
        "capital": _dimension(capital, expected, require_date=True),
        "fundamental": _dimension(fundamental),
    }
    coverage = round(sum(
        WEIGHTS[name]
        for name, status in dimensions.items()
        if status["available"] and (name == "fundamental" or status["fresh"])
    ), 2)
    reasons = []
    if not dimensions["kline"]["fresh"]:
        reasons.append("kline_stale")
    if coverage < MIN_COVERAGE:
        reasons.append("coverage_below_70pct")
    return {
        "as_of_date": expected,
        "coverage": coverage,
        "eligible": dimensions["kline"]["fresh"] and coverage >= MIN_COVERAGE,
        "dimensions": dimensions,
        "reasons": reasons,
    }
