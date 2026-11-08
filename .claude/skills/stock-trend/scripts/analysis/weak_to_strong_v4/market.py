"""Pure market-level classification for standalone V4 analysis results.

The classifier deliberately calls successful V4 signals research candidates:
the frozen V4 models have not earned production recommendation status.  T1
and T2 are ranked independently because their percentile scores are not
comparable across signal types.
"""
from __future__ import annotations

import copy
from typing import Iterable, Mapping

from .v4_shadow_scoring import TYPE_CONFIG, v4_ready_sort_key


SCHEMA_VERSION = "daily-weak-to-strong/v4-market/v1"
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
        reasons = _reason_codes(result)
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
        "structural_observations": observations,
        "no_active_signal": no_active_signal,
        "data_insufficient": insufficient,
        "summary": {
            "t1_research_candidates": len(candidates.get("T1", ())),
            "t2_research_candidates": len(candidates.get("T2", ())),
            "structural_observations": len(observations),
            "no_active_signal": len(no_active_signal),
            "data_insufficient": len(insufficient),
        },
    }


__all__ = ["SCHEMA_VERSION", "classify_v4_market_results"]
