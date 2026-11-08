"""Research scoring overlay for the causal daily weak-to-strong v2 detector."""
from __future__ import annotations

import copy
import math

from .weak_to_strong import _canonical_digest, _normalise_rows
from .weak_to_strong_v2 import analyze_daily_weak_to_strong_v2

SCHEMA_VERSION = "daily-weak-to-strong/v3"
DETECTOR_VERSION = "3.0.0-research.2"
LABELS = {"EARLY_REPAIR": "早期修复", "LOCAL_CONFIRMATION": "局部确认",
          "RANGE_BREAKOUT": "区间突破"}
# Frozen research assumptions, not calibrated probabilities.
SCORING_CONFIG = {"stage": {"EARLY_REPAIR": 12, "LOCAL_CONFIRMATION": 17,
                            "RANGE_BREAKOUT": 20},
                  "risk": {"extended": 5, "weak_volume": 3, "parent_overhead": 2},
                  "risk_cap": 8, "probe_cap": 8}


def score_v2_result(result, probe_bonus=0):
    """Score a v2 snapshot without mutating it; missing evidence fails closed."""
    stage = result.get("stage")
    score = {"status": "not_applicable", "base": None, "probe_bonus": 0,
             "risk_penalty": None, "total": None, "components": {}, "missing": []}
    if stage not in LABELS:
        return score
    if result.get("lifecycle_status") in {"invalidated", "expired", "completed"}:
        return score
    metrics = result.get("metrics") or {}
    structure = result.get("structure") or {}
    required = ("close", "close_strength", "volume_prior5_ratio", "ma10", "ma20",
                "ma10_slope_3", "ma20_slope_3", "ma_flat_tolerance", "structure_age")
    missing = [key for key in required if not isinstance(metrics.get(key), (int, float))
               or isinstance(metrics.get(key), bool) or not math.isfinite(metrics[key])]
    if not isinstance(structure.get("anchor_high"), (int, float)) or not structure["anchor_high"] > 0:
        missing.append("anchor_high")
    if stage != "EARLY_REPAIR" and (not isinstance(metrics.get("trigger_distance_atr"), (int, float))
                                     or not math.isfinite(metrics["trigger_distance_atr"])):
        missing.append("trigger_distance_atr")
    if (result.get("evidence_status") != "ready" or not result.get("current_bar_evaluated")
            or result.get("provisional")):
        missing.append("current_evidence")
    if missing:
        score.update(status="insufficient", missing=missing)
        return score
    close, strength = metrics["close"], metrics["close_strength"]
    volume = metrics["volume_prior5_ratio"]
    tolerance = metrics["ma_flat_tolerance"]
    anchor = structure["anchor_high"]
    age = metrics["structure_age"]
    distance = metrics.get("trigger_distance_atr")
    components = {
        "stage": SCORING_CONFIG["stage"][stage],
        "close_strength": 15 if strength >= .8 else 12 if strength >= .7 else 9 if strength >= .6 else 0,
        "volume": 0 if volume < .8 else 6 if volume < 1 else 10 if volume < 1.2 else 15 if volume <= 2.5 else 10,
        "moving_averages": 4 * (close > metrics["ma10"]) + 4 * (close > metrics["ma20"])
            + 3 * (metrics["ma10_slope_3"] >= -tolerance) + 3 * (metrics["ma20_slope_3"] >= -tolerance),
        "structure_position": 12 if close > anchor else 8 if close >= .95 * anchor else 4,
        "structure_age": 8 if 6 <= age <= 30 else 5 if 31 <= age <= 60 else 3,
        "trigger_distance": 3 if stage == "EARLY_REPAIR" else 6 if 0 <= distance <= 1 else 3 if 1 < distance <= 2 else 0,
    }
    base = sum(components.values())
    penalty = min(SCORING_CONFIG["risk_cap"], sum(SCORING_CONFIG["risk"].get(flag, 0)
                                                 for flag in set(result.get("risk_flags") or [])))
    bonus = max(0, min(SCORING_CONFIG["probe_cap"], probe_bonus))
    score.update(status="ready", base=base, components=components, probe_bonus=bonus,
                 risk_penalty=penalty, total=max(0, min(100, base + bonus - penalty)))
    return score


def analyze_daily_weak_to_strong_v3(kline_payload, *, as_of_date, code="", name="",
                                   close_evidence=None, calendar_evidence=None,
                                   config=None, v1_result=None):
    """Classify with v2 and attach a point-in-time v3 scoring overlay."""
    from .probe_v1 import analyze_daily_probes, select_probe

    v2 = analyze_daily_weak_to_strong_v2(
        kline_payload, as_of_date=as_of_date, code=code, name=name,
        close_evidence=close_evidence, calendar_evidence=calendar_evidence,
        config=config, v1_result=v1_result)
    result = copy.deepcopy(v2)
    result["v2_parameter_hash"] = v2["parameter_hash"]
    result["schema_version"] = SCHEMA_VERSION
    result["detector_version"] = DETECTOR_VERSION
    result["parameter_hash"] = _canonical_digest({"v2": v2["parameter_hash"],
                                                   "detector_version": DETECTOR_VERSION,
                                                   "scoring": SCORING_CONFIG,
                                                   "probe_version": "probe-v1"})
    result["stage_label"] = LABELS.get(v2["stage"])
    result["probe"] = {"status": "insufficient", "candidates": [], "selected": None}
    if v2["evidence_status"] == "ready" and v2["current_bar_evaluated"]:
        rows, reasons, duplicates = _normalise_rows(kline_payload, v2["evaluation_date"])
        if not reasons and not duplicates:
            probe = analyze_daily_probes(kline_payload, as_of_date=v2["evaluation_date"],
                                         min_candidate_date=rows[max(0, len(rows) - 21)]["date"])
            selected = select_probe(probe["candidates"], rows, (v2.get("structure") or {}).get("structure_start"))
            result["probe"] = {"status": probe["evidence_status"], "candidates": probe["candidates"], "selected": selected,
                               "parameter_hash": probe["parameter_hash"], "reason_codes": probe["reason_codes"]}
    bonus = (result["probe"]["selected"] or {}).get("bonus", 0)
    result["score"] = score_v2_result(v2, bonus)
    if result["probe"]["status"] != "ready" and result["score"]["status"] == "ready":
        result["score"].update(status="insufficient", total=None, missing=["probe_data"])
    return result


def rank_daily_candidates(results):
    """Return scored candidates in deterministic order with global and stage ranks."""
    eligible = [copy.deepcopy(result) for result in results
                if result.get("score", {}).get("status") == "ready"]
    eligible.sort(key=lambda item: (-item["score"]["total"], str(item.get("code") or "")))
    stage_counts = {}
    for rank, item in enumerate(eligible, 1):
        stage = item["stage"]
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        item["rank"] = rank
        item["stage_rank"] = stage_counts[stage]
    return eligible
