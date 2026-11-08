"""Build the auditable signal identity consumed by V4 scoring."""

from __future__ import annotations

import copy
import math
from typing import Mapping


def _matching_event(result: Mapping[str, object]) -> Mapping[str, object] | None:
    signal_date = str(result.get("signal_date") or "")
    cycle_id = str(result.get("cycle_id") or "")
    matches = [
        event
        for event in result.get("events") or []
        if isinstance(event, Mapping)
        and str(event.get("date") or "") == signal_date
        and (not cycle_id or str(event.get("cycle_id") or "") == cycle_id)
    ]
    return matches[-1] if matches else None


def signal_record(
    v2_result: Mapping[str, object],
    *,
    v1_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Convert a point-in-time detector result into a validated V4 signal."""
    result = copy.deepcopy(dict(v2_result))
    signal_date = str(result.get("signal_date") or "")
    event = _matching_event(result)
    origin = str(result.get("origin_pattern_type") or "")
    raw_structure = result.get("structure")
    structure = raw_structure if isinstance(raw_structure, Mapping) else {}
    structure_low = structure.get("signal_base_low")
    if structure_low is None and origin == "short_pullback" and isinstance(v1_result, Mapping):
        structure_low = v1_result.get("L")

    reasons: list[str] = []
    if result.get("evidence_status") != "ready":
        reasons.append("evidence_not_ready")
    if result.get("confirmation") != "close_confirmed":
        reasons.append("close_not_confirmed")
    if not signal_date:
        reasons.append("not_a_signal_event")
    if structure_low is None:
        reasons.append("structure_low_missing")
    elif (
        isinstance(structure_low, bool)
        or not isinstance(structure_low, (int, float))
        or not math.isfinite(float(structure_low))
        or float(structure_low) <= 0
    ):
        reasons.append("structure_low_invalid")
    if event is None:
        reasons.append("event_identity_missing")
    elif not event.get("event_id"):
        reasons.append("event_id_missing")
    if not result.get("cycle_id"):
        reasons.append("cycle_id_missing")

    return {
        "schema_version": "weak-to-strong-backtest-signal/v1",
        "code": str(result.get("code") or ""),
        "signal_date": signal_date,
        "cycle_id": str(result.get("cycle_id") or ""),
        "event_id": str(event.get("event_id") or "") if event else "",
        "event_type": str(event.get("event_type") or "") if event else "",
        "candidate_role": "primary",
        "origin_pattern_type": origin,
        "pattern_type": str(result.get("pattern_type") or ""),
        "stage": str(result.get("stage") or ""),
        "structure_low": structure_low,
        "eligibility": {"eligible": not reasons, "reasons": reasons},
        "signal_audit": {
            "as_of_date": str(result.get("as_of_date") or ""),
            "evaluation_date": str(result.get("evaluation_date") or ""),
            "detector_version": str(result.get("detector_version") or ""),
            "parameter_hash": str(result.get("parameter_hash") or ""),
            "metrics": copy.deepcopy(result.get("metrics") or {}),
            "checks": copy.deepcopy(result.get("checks") or {}),
            "risk_flags": copy.deepcopy(result.get("risk_flags") or []),
            "structure": copy.deepcopy(structure),
            "event": copy.deepcopy(dict(event)) if event else None,
            "secondary_matches": copy.deepcopy(result.get("secondary_matches") or []),
            "kline_evidence": copy.deepcopy(result.get("kline_evidence") or {}),
        },
    }


__all__ = ["signal_record"]
