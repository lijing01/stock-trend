#!/usr/bin/env python3
"""Data-quality policy for promoting scan candidates to recommendations."""
from datetime import datetime


WEIGHTS = {"kline": 0.55, "capital": 0.25, "fundamental": 0.20}
MIN_COVERAGE = 0.70
NON_PROVIDER_STATUSES = frozenset({
    "cache_miss", "cache_stale", "not_selected_for_enrichment",
    "not_started_deadline",
})
SUCCESS_STATUSES = frozenset({"live_success", "cache_valid"})


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
    if not isinstance(rows, list):
        return ""
    dates = [
        _iso_date(row.get("trade_date") or row.get("date"))
        for row in rows
        if isinstance(row, dict)
    ]
    valid = [value for value in dates if value]
    return max(valid) if valid else ""


def _dimension(name, payload, expected_date="", require_date=False,
               evidence=None):
    evidence = evidence if isinstance(evidence, dict) else {}
    source_status = str(evidence.get("status") or "")
    returned = isinstance(payload, dict) and bool(payload)
    available = returned
    quality = "missing"
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    malformed = bool(
        isinstance(payload, dict)
        and (
            ("meta" in payload and not isinstance(meta, dict))
            or ("summary" in payload and not isinstance(summary, dict))
            or (
                # Row-based dimensions (kline/capital) must expose a list of
                # rows. Fundamental is metrics-in-summary with a placeholder
                # `data: {}` — a non-list here is its normal shape, not
                # malformed.
                name in ("kline", "capital")
                and "data" in payload
                and not isinstance(payload.get("data"), list)
            )
        )
    )
    meta = meta if isinstance(meta, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    cache_validation = meta.get("cache_validation", {}) \
        if isinstance(meta, dict) else {}
    if available:
        quality = (
            summary.get("data_quality")
            or payload.get("data_quality")
            or "good"
        )
        if malformed:
            quality = "error"
        if meta.get("data_source") == "error" \
                or meta.get("error") or payload.get("error"):
            quality = "error"
        if isinstance(cache_validation, dict) \
                and cache_validation.get("valid") is False:
            quality = (
                "error" if cache_validation.get("error") else "stale"
            )
        if name == "capital" and not latest_data_date(payload):
            quality = "error"
        if quality in ("error", "stale"):
            available = False
    if source_status in NON_PROVIDER_STATUSES:
        # A scheduler/cache status is evidence about what was (or was not)
        # requested, not a provider failure.  Keep the dimension unavailable
        # so it can never promote a candidate through a diagnostic wrapper.
        available = False
        quality = "stale" if source_status == "cache_stale" else "missing"
    elif source_status and source_status not in SUCCESS_STATUSES:
        # A live attempt that returned an invalid payload or failed outright
        # remains a genuine source error even when a stale fallback object is
        # attached for diagnostics.
        available = False
        quality = "error" if returned or name == "capital" else "missing"
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
    if source_status in NON_PROVIDER_STATUSES:
        stale_reason = (
            f"{name}_stale" if source_status == "cache_stale"
            else source_status
        )
    elif source_status and source_status not in SUCCESS_STATUSES \
            and (returned or name == "capital"):
        stale_reason = f"{name}_error"
    elif returned and quality == "error":
        stale_reason = f"{name}_error"
    elif returned and quality == "stale":
        stale_reason = f"{name}_stale"
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
        "source_status": source_status,
    }


def assess_candidate_data(kline, capital, fundamental, as_of_date="",
                          source_evidence=None, capital_evidence=None):
    source_evidence = source_evidence if isinstance(source_evidence, dict) else {}
    if capital_evidence is not None:
        source_evidence = {
            **source_evidence,
            "capital": capital_evidence,
        }
    normalized_as_of = _iso_date(as_of_date)
    kline_date = latest_data_date(kline)
    expected = normalized_as_of or kline_date
    dimensions = {
        "kline": _dimension(
            "kline", kline, expected, require_date=True,
            evidence=source_evidence.get("kline")),
        "capital": _dimension(
            "capital", capital, expected, require_date=True,
            evidence=source_evidence.get("capital")),
        "fundamental": _dimension(
            "fundamental", fundamental, expected,
            evidence=source_evidence.get("fundamental")),
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
    capital_status = dimensions["capital"].get("source_status", "")
    if not dimensions["capital"]["fresh"]:
        if capital_status in NON_PROVIDER_STATUSES:
            reasons.append(capital_status)
        elif capital_status:
            reasons.append("capital_error")
        elif not dimensions["capital"]["returned"]:
            reasons.append("capital_missing")
    returned_errors = [
        f"{name}_error" for name in ("capital", "fundamental")
        if dimensions[name]["returned"] and dimensions[name]["quality"] == "error"
    ]
    reasons.extend(returned_errors)
    returned_stale = [
        f"{name}_stale" for name in ("capital", "fundamental")
        if dimensions[name]["returned"]
        and dimensions[name]["stale_reason"] == f"{name}_stale"
    ]
    reasons.extend(returned_stale)
    evidence_errors = [
        f"{name}_error" for name in ("capital", "fundamental")
        if dimensions[name].get("source_status")
        and dimensions[name].get("source_status") not in (
            *NON_PROVIDER_STATUSES, *SUCCESS_STATUSES)
    ]
    reasons.extend(evidence_errors)
    reasons = list(dict.fromkeys(reasons))
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
            and dimensions["capital"]["fresh"]
            and coverage >= MIN_COVERAGE
            and secondary_available
            and not returned_errors
            and not returned_stale
        ),
        "dimensions": dimensions,
        "reasons": reasons,
    }
