#!/usr/bin/env python3
"""Shared pure validation helpers for stock-scanner cache payloads."""


def cache_verdict(reasons):
    """Return the common, JSON-safe result for pure cache validators."""
    reasons = list(dict.fromkeys(reasons))
    error_reasons = {
        "invalid_payload", "source_missing", "source_error",
        "payload_error", "quality_missing", "quality_error",
        "insufficient_data", "flow_metrics_missing",
    }
    return {
        "valid": not reasons,
        "reasons": reasons,
        "stale": any(reason in {
            "wrong_trading_date", "cache_expired",
        } for reason in reasons),
        "error": any(reason in error_reasons for reason in reasons),
    }


def payload_validation_reasons(payload, allow_nonfatal_errors=False):
    """Validate shared source, quality, and error metadata."""
    if not isinstance(payload, dict) or not payload:
        return ["invalid_payload"]
    meta = payload.get("meta", {})
    summary = payload.get("summary", {})
    meta = meta if isinstance(meta, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    source = (
        meta.get("data_source") or meta.get("source")
        or payload.get("source")
    )
    reasons = []
    if not source:
        reasons.append("source_missing")
    elif str(source).lower() == "error":
        reasons.append("source_error")
    if any((
        payload.get("error"),
        meta.get("error"), meta.get("errors"), meta.get("refresh_error"),
        summary.get("error"), summary.get("errors"),
    )):
        reasons.append("payload_error")
    if payload.get("errors") and not allow_nonfatal_errors:
        reasons.append("payload_error")
    quality = (
        summary.get("data_quality") or payload.get("data_quality")
        or meta.get("data_quality")
    )
    if quality == "error":
        reasons.append("quality_error")
    cache_validation = meta.get("cache_validation")
    if isinstance(cache_validation, dict) and not cache_validation.get(
            "valid", False):
        reasons.extend(cache_validation.get("reasons") or ["payload_error"])
    return reasons


def cache_status(payload, verdict=None, cached=None):
    """Return an explicit cache state without treating diagnostics as data."""
    if isinstance(verdict, dict) and verdict.get("valid"):
        return "cache_valid"
    if cached is None:
        validation = payload.get("meta", {}).get("cache_validation", {}) \
            if isinstance(payload, dict) and isinstance(
                payload.get("meta", {}), dict) else {}
        cached = validation.get("cache_present") if isinstance(
            validation, dict) and "cache_present" in validation else payload
    return "cache_stale" if isinstance(cached, dict) and bool(cached) \
        else "cache_miss"


def evidence_status(source, payload, attempt, *, known_statuses,
                    cache_probe=False, usable=None):
    """Normalize adapter/scheduler evidence to the public status vocabulary."""
    del source  # Kept in the signature so source-specific adapters can evolve.
    attempt = attempt if isinstance(attempt, dict) else {}
    status = str(attempt.get("status") or "")
    if status in known_statuses:
        return status
    if cache_probe or not attempt.get("attempted"):
        if usable is not None and usable(payload):
            return "cache_valid"
        validation = payload.get("meta", {}).get("cache_validation", {}) \
            if isinstance(payload, dict) and isinstance(
                payload.get("meta", {}), dict) else {}
        if isinstance(validation, dict) and validation.get("stale"):
            return "cache_stale"
        cache_present = isinstance(validation, dict) and validation.get(
            "cache_present") is True
        if cache_present or (isinstance(payload, dict) and bool(payload)):
            return "cache_stale"
        return "cache_miss"
    if attempt.get("reason"):
        return str(attempt["reason"])
    if usable is None or usable(payload):
        return "live_success"
    return "empty"
