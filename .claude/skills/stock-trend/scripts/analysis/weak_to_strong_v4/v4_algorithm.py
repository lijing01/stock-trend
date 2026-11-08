"""Standalone, point-in-time V4 research algorithm.

This module composes the frozen V3 detector with the frozen T1/T2 V4 scoring
bundle.  It owns no I/O and does not mutate production ranking or trading
state.  Callers must supply a previously trained monthly bundle and explicit
close/calendar evidence for the requested as-of date.
"""
from __future__ import annotations

from datetime import date
from typing import Mapping

from .v3_score_features import extract_v3_score_features
from .v4_shadow_scoring import (
    build_feature_snapshot,
    score_v4_shadow_record,
    validate_v4_shadow_bundle,
)
from .signal_record import signal_record
from .weak_to_strong import analyze_daily_weak_to_strong
from .weak_to_strong_v3 import analyze_daily_weak_to_strong_v3


SCHEMA_VERSION = "daily-weak-to-strong/v4-research"
ALGORITHM_VERSION = "4.0.0-shadow.1"


def _day(value: object, field: str) -> str:
    text = str(value or "").strip().replace("/", "-")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _probe_type(selected: Mapping[str, object] | None) -> str | None:
    pattern = str((selected or {}).get("pattern") or "").upper()
    if pattern.startswith("T1"):
        return "T1"
    if pattern.startswith("T2"):
        return "T2"
    return None


def adapt_v3_result_to_v4_record(
    kline_payload: object,
    v3_result: Mapping[str, object],
    *,
    as_of_date: object,
    v1_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Convert one newly confirmed V3 event into an auditable V4 input.

    The adapter refuses stale events and missing identities.  Feature extraction
    reads only bars through the explicit signal/as-of date.
    """
    as_of = _day(as_of_date, "as_of_date")
    record = signal_record(v3_result, v1_result=v1_result)
    if record["signal_date"] != as_of:
        raise ValueError("V4 requires a newly confirmed V3 event on as_of_date")
    eligibility = record.get("eligibility") or {}
    if eligibility.get("eligible") is not True:
        reasons = ",".join(str(item) for item in eligibility.get("reasons") or [])
        raise ValueError(f"V3 signal is not eligible: {reasons or 'unknown'}")
    score = v3_result.get("score")
    if not isinstance(score, Mapping) or score.get("status") != "ready":
        raise ValueError("V3 score is not ready")
    old_score = score.get("total")
    if isinstance(old_score, bool) or not isinstance(old_score, (int, float)):
        raise ValueError("V3 score total is invalid")
    probe = v3_result.get("probe")
    selected = probe.get("selected") if isinstance(probe, Mapping) else None
    selected = selected if isinstance(selected, Mapping) else None
    signal_type = _probe_type(selected)
    if signal_type is None:
        raise ValueError("V4 requires a selected T1 or T2 probe")
    features = extract_v3_score_features(
        kline_payload,
        signal_date=as_of,
        selected_probe=selected,
        v3_result=v3_result,
    )
    if features.get("feature_asof") != as_of:
        raise ValueError("V4 feature_asof must equal as_of_date")
    symbol = str(record.get("code") or v3_result.get("code") or "").strip()
    if not symbol:
        raise ValueError("V4 requires a symbol")
    return {
        "schema_version": "v4-standalone-input/v1",
        "symbol": symbol,
        "signal_date": as_of,
        "cycle_id": str(record["cycle_id"]),
        "event_id": str(record["event_id"]),
        "signal_type": signal_type,
        "probe_pattern": signal_type,
        "probe_status": str((selected or {}).get("status") or "").upper(),
        "old_score": float(old_score),
        "v3_score": float(old_score),
        "features": features,
        "signal": record,
    }


def analyze_daily_weak_to_strong_v4(
    kline_payload: object,
    *,
    bundle: Mapping[str, object],
    as_of_date: object,
    code: str,
    name: str = "",
    close_evidence: Mapping[str, object] | None = None,
    calendar_evidence: Mapping[str, object] | None = None,
    config: Mapping[str, object] | None = None,
    v1_config: Mapping[str, object] | None = None,
    v1_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Run V3 detection and V4 T1/T2 scoring as one standalone call."""
    as_of = _day(as_of_date, "as_of_date")
    validate_v4_shadow_bundle(bundle)
    v1 = dict(v1_result) if isinstance(v1_result, Mapping) else analyze_daily_weak_to_strong(
        kline_payload,
        as_of_date=as_of,
        close_evidence=close_evidence,
        calendar_evidence=calendar_evidence,
        config=v1_config,
    )
    v3 = analyze_daily_weak_to_strong_v3(
        kline_payload,
        as_of_date=as_of,
        code=code,
        name=name,
        close_evidence=close_evidence,
        calendar_evidence=calendar_evidence,
        config=config,
        v1_result=v1,
    )
    base = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "as_of_date": as_of,
        "symbol": str(code),
        "model_version": str(bundle.get("model_version") or ""),
        "production_changed": False,
        "production_eligible": False,
    }
    try:
        adapted = adapt_v3_result_to_v4_record(
            kline_payload, v3, as_of_date=as_of, v1_result=v1,
        )
    except ValueError as exc:
        return {
            **base,
            "status": "not_scored",
            "reason": str(exc),
            "signal": None,
            "v4_input": None,
            "feature_snapshot": None,
            "prediction": None,
            "v3_result": v3,
        }
    snapshot = build_feature_snapshot(adapted)
    prediction = score_v4_shadow_record(bundle, adapted, feature_snapshot=snapshot)
    prediction.update(
        symbol=adapted["symbol"],
        cycle_id=adapted["cycle_id"],
        event_id=adapted["event_id"],
    )
    return {
        **base,
        "status": "ready" if prediction["score_status"] == "ready" else "fallback",
        "reason": prediction.get("fallback_reason"),
        "signal": adapted["signal"],
        "v4_input": adapted,
        "feature_snapshot": snapshot,
        "prediction": prediction,
        "v3_result": v3,
    }
