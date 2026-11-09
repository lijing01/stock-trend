"""Pure market-level classification for standalone V4 analysis results.

The classifier deliberately calls successful V4 signals research candidates:
the frozen V4 models have not earned production recommendation status.  T1
and T2 are ranked independently because their percentile scores are not
comparable across signal types.
"""
from __future__ import annotations

import copy
from datetime import date
import math
from typing import Iterable, Mapping

from .v4_shadow_scoring import TYPE_CONFIG, v4_ready_sort_key


SCHEMA_VERSION = "daily-weak-to-strong/v4-market/v2"
ACTIVE_STAGES = {
    "PREPARING",
    "EARLY_REPAIR",
    "LOCAL_CONFIRMATION",
    "RANGE_BREAKOUT",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _reason_codes(result: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    reason = _text(result.get("reason"))
    if reason:
        reasons.append(reason)
    prediction = result.get("prediction")
    if isinstance(prediction, Mapping):
        fallback = _text(prediction.get("fallback_reason"))
        if fallback:
            reasons.append(fallback)
    v3 = result.get("v3_result")
    if isinstance(v3, Mapping):
        for item in v3.get("reason_codes") or ():
            value = _text(item)
            if value:
                reasons.append(value)
        if v3.get("evidence_status") != "ready":
            reasons.append("v3_evidence_not_ready")
        if v3.get("current_bar_evaluated") is not True:
            reasons.append("v3_current_bar_not_evaluated")
    if not reasons:
        reasons.append("v4_result_not_classifiable")
    return list(dict.fromkeys(reasons))


def _active_v3_structure(result: Mapping[str, object]) -> bool:
    v3 = result.get("v3_result")
    return bool(
        isinstance(v3, Mapping)
        and v3.get("evidence_status") == "ready"
        and v3.get("current_bar_evaluated") is True
        and v3.get("lifecycle_status") == "active"
        and _text(v3.get("stage")).upper() in ACTIVE_STAGES
    )


def _complete_v3_evidence(result: Mapping[str, object]) -> bool:
    v3 = result.get("v3_result")
    return bool(
        isinstance(v3, Mapping)
        and v3.get("evidence_status") == "ready"
        and v3.get("current_bar_evaluated") is True
    )


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _early_observation(
    result: Mapping[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    """Return one active T1/T2 probe observation without scoring it as V4."""
    v3 = result.get("v3_result")
    if not isinstance(v3, Mapping):
        return None, None
    probe = v3.get("probe")
    selected = probe.get("selected") if isinstance(probe, Mapping) else None
    if not isinstance(selected, Mapping):
        return None, None
    pattern = _text(selected.get("pattern")).upper()
    signal_type = "T1" if pattern.startswith("T1") else "T2" if pattern.startswith("T2") else ""
    status = _text(selected.get("status")).lower()
    if not signal_type or status not in {"candidate", "confirmed"}:
        return None, None
    if probe.get("status") != "ready":
        return None, "early_observation_probe_evidence_not_ready"
    as_of = _text(result.get("as_of_date"))
    if _text(v3.get("evaluation_date")) != as_of:
        return None, "early_observation_evaluation_date_mismatch"
    candidate_date = _text(selected.get("candidate_date"))
    try:
        if date.fromisoformat(candidate_date) > date.fromisoformat(as_of):
            raise ValueError
    except ValueError:
        return None, "early_observation_candidate_date_invalid"
    age = selected.get("candidate_age_sessions")
    if (isinstance(age, bool) or not isinstance(age, int) or age < 0
            or (status == "candidate" and age > 5) or age > 20):
        return None, "early_observation_candidate_expired"
    if not _text(selected.get("candidate_id")):
        return None, "early_observation_probe_identity_missing"
    candidate_high = _finite(selected.get("high"))
    candidate_low = _finite(selected.get("low"))
    if (candidate_high is None or candidate_low is None
            or candidate_low <= 0 or candidate_high < candidate_low):
        return None, "early_observation_probe_levels_invalid"
    if status == "confirmed" and not any(
        isinstance(event, Mapping) and event.get("status") == "confirmed"
        for event in selected.get("status_events") or ()
    ):
        return None, "early_observation_confirmation_evidence_missing"
    if not _active_v3_structure(result):
        return None, None
    # A close-confirmed V3 event on the requested date belongs to the existing
    # scored/fallback V4 path, not the pre-confirmation observation bucket.
    if (_text(v3.get("signal_date")) == _text(result.get("as_of_date"))
            and v3.get("confirmation") == "close_confirmed"):
        return None, None
    structure = v3.get("structure")
    structure = structure if isinstance(structure, Mapping) else {}
    metrics = v3.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    failure_line = _finite(structure.get("failure_line"))
    if failure_line is None or failure_line <= 0:
        return None, "early_observation_failure_line_missing"
    current_close = _finite(metrics.get("close"))
    if current_close is None:
        return None, "early_observation_current_close_missing"
    # This is the detector's exact structure invalidation condition. Equality
    # remains valid because daily_weak_to_strong_v2 invalidates only below it.
    if current_close < failure_line:
        return None, "early_observation_structure_breached"
    observation = {
        "signal_type": signal_type,
        "probe_pattern": pattern,
        "probe_status": status,
        "candidate_date": candidate_date,
        "candidate_age_sessions": selected.get("candidate_age_sessions"),
        "candidate_high": candidate_high,
        "candidate_low": candidate_low,
        "current_close": current_close,
        "structure_stage": _text(v3.get("stage")).upper(),
        "structure_failure_line": failure_line,
        "provisional": v3.get("provisional") is True,
        "confirmation_state": (
            "probe_unconfirmed" if status == "candidate"
            else "probe_confirmed_no_new_event"
        ),
    }
    item = _copy_with_classification(
        result, "early_observation", [f"active_{signal_type.lower()}_probe"],
    )
    item["early_observation"] = observation
    item["research_only"] = True
    return item, None


def _copy_with_classification(
    result: Mapping[str, object], classification: str, reasons: list[str],
) -> dict[str, object]:
    item = copy.deepcopy(dict(result))
    item["market_classification"] = classification
    item["classification_reasons"] = list(reasons)
    return item


def classify_v4_market_results(
    results: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Classify already-analysed V4 results without I/O or input mutation.

    Ready T1/T2 predictions become type-local research candidates.  A result
    that cannot be V4-ranked may still be retained as a structural observation
    when its V3 evidence proves a currently active structure.  Everything else
    is reported as data insufficient with explicit reasons.
    """
    candidates: dict[str, list[dict[str, object]]] = {
        signal_type: [] for signal_type in TYPE_CONFIG
    }
    early_observations: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    no_active_signal: list[dict[str, object]] = []
    insufficient: list[dict[str, object]] = []
    dates: set[str] = set()

    for raw in results:
        if not isinstance(raw, Mapping):
            insufficient.append({
                "market_classification": "data_insufficient",
                "classification_reasons": ["v4_result_invalid"],
                "raw_type": type(raw).__name__,
            })
            continue
        result = dict(raw)
        as_of = _text(result.get("as_of_date"))
        if as_of:
            dates.add(as_of)
        prediction = result.get("prediction")
        if isinstance(prediction, Mapping):
            ranked = copy.deepcopy(dict(prediction))
            ranked.setdefault("symbol", result.get("symbol"))
            signal_type = _text(ranked.get("signal_type")).upper()
            if (result.get("status") == "ready"
                    and ranked.get("score_status") == "ready"
                    and signal_type in candidates):
                item = _copy_with_classification(
                    result, "research_candidate", ["v4_score_ready"],
                )
                item["prediction"] = ranked
                item["research_only"] = True
                candidates[signal_type].append(item)
                continue
        early, early_error = _early_observation(result)
        if early is not None:
            early_observations.append(early)
            continue
        reasons = _reason_codes(result)
        if early_error:
            reasons = list(dict.fromkeys([*reasons, early_error]))
            insufficient.append(_copy_with_classification(
                result, "data_insufficient", reasons,
            ))
            continue
        if (result.get("status") == "fallback"
                or (isinstance(prediction, Mapping)
                    and prediction.get("score_status") != "ready")):
            insufficient.append(_copy_with_classification(
                result, "data_insufficient", reasons,
            ))
        elif _active_v3_structure(result):
            observations.append(_copy_with_classification(
                result, "structural_observation", reasons,
            ))
        elif _complete_v3_evidence(result):
            no_active_signal.append(_copy_with_classification(
                result, "no_active_signal", reasons,
            ))
        else:
            insufficient.append(_copy_with_classification(
                result, "data_insufficient", reasons,
            ))

    if len(dates) > 1:
        raise ValueError("V4 market ranking requires a single as_of_date")

    for signal_type, items in candidates.items():
        items.sort(key=lambda item: v4_ready_sort_key(item["prediction"]))
        for rank, item in enumerate(items, 1):
            item["v4_type_rank"] = rank
            item["ranking_basis"] = "expected_net_return_then_win_rate"

    early_observations.sort(key=lambda item: _text(item.get("symbol")))
    early_observations.sort(
        key=lambda item: _text(
            (item.get("early_observation") or {}).get("candidate_date")
        ),
        reverse=True,
    )
    observations.sort(key=lambda item: (
        _text(item.get("symbol")), _text(item.get("as_of_date"))))
    no_active_signal.sort(key=lambda item: (
        _text(item.get("symbol")), _text(item.get("as_of_date"))))
    insufficient.sort(key=lambda item: (
        _text(item.get("symbol")), _text(item.get("as_of_date"))))
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_date": next(iter(dates)) if len(dates) == 1 else None,
        "research_candidates": candidates,
        "early_observations": early_observations,
        "structural_observations": observations,
        "no_active_signal": no_active_signal,
        "data_insufficient": insufficient,
        "summary": {
            "t1_research_candidates": len(candidates.get("T1", ())),
            "t2_research_candidates": len(candidates.get("T2", ())),
            "early_observations": len(early_observations),
            "structural_observations": len(observations),
            "no_active_signal": len(no_active_signal),
            "data_insufficient": len(insufficient),
        },
    }


__all__ = ["SCHEMA_VERSION", "classify_v4_market_results"]
