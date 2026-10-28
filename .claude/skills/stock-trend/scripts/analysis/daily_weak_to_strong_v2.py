#!/usr/bin/env python3
"""Causal grouped daily weak-to-strong detector (research contract v2).

The module is deliberately pure: callers supply frozen OHLCV, close and
calendar evidence.  It keeps the strict v1 pullback detector as an independent
candidate while adding a left-only parent structure for base and deep-repair
setups.  No network or filesystem access occurs here.
"""

from __future__ import annotations

import copy
import math

from analysis.daily_weak_to_strong import (
    _calendar_status,
    _canonical_digest,
    _normalise_date,
    _normalise_rows,
)


SCHEMA_VERSION = "daily-weak-to-strong/v2"
DETECTOR_VERSION = "2.0.0-research.1"
DEFAULT_CONFIG = {
    "min_history_bars": 120,
    "ma_short_window": 10,
    "ma_long_window": 20,
    "atr_window": 14,
    "ma_slope_days": 3,
    "ma_flat_tolerance": 0.002,
    "local_short_window": 5,
    "local_long_window": 10,
    "range_window": 20,
    "parent_window": 40,
    "deep_drawdown": 0.12,
    "strong_day_return": 0.03,
    "min_volume_ratio": 0.80,
    "min_close_strength": 0.60,
    "parent_trigger_atr": 1.50,
    "failure_atr": 0.50,
    "max_structure_age": 60,
    "base_min_age": 6,
    "extended_ratio": 0.05,
    "extended_atr": 2.0,
}

STAGE_RANK = {
    "NONE": 0,
    "PREPARING": 1,
    "EARLY_REPAIR": 2,
    "LOCAL_CONFIRMATION": 3,
    "RANGE_BREAKOUT": 4,
}
PATTERN_RANK = {"base_consolidation": 1, "short_pullback": 2, "deep_repair": 3}


def _config(value):
    config = dict(DEFAULT_CONFIG)
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(config):
            return dict(DEFAULT_CONFIG), [
                "config_unknown_key" if isinstance(value, dict) else "config_invalid"]
        config.update(value)
    integer_keys = {
        "min_history_bars", "ma_short_window", "ma_long_window", "atr_window",
        "ma_slope_days", "local_short_window", "local_long_window",
        "range_window", "parent_window", "max_structure_age", "base_min_age",
    }
    for key, number in config.items():
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number)):
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        if key in integer_keys and (int(number) != number or number <= 0):
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        if key not in integer_keys and number <= 0 and key != "ma_flat_tolerance":
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        if key == "ma_flat_tolerance" and number < 0:
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        if key in integer_keys:
            config[key] = int(number)
    if not (
        config["ma_short_window"] < config["ma_long_window"]
        <= config["min_history_bars"]
        and config["local_short_window"] < config["local_long_window"]
        <= config["range_window"] <= config["parent_window"]
        and config["atr_window"] < config["min_history_bars"]
        and config["base_min_age"] < config["max_structure_age"]
        and 0 < config["min_close_strength"] <= 1
        and 0 < config["min_volume_ratio"]
        and 0 < config["deep_drawdown"] < 1
    ):
        return dict(DEFAULT_CONFIG), ["config_invalid"]
    return config, []


def _mean(values):
    return sum(values) / len(values) if values else None


def _ma(rows, index, window):
    if index + 1 < window:
        return None
    return _mean([row["close"] for row in rows[index + 1 - window:index + 1]])


def _true_range(rows, index):
    current = rows[index]
    previous = rows[index - 1]["close"] if index else current["close"]
    return max(current["high"] - current["low"],
               abs(current["high"] - previous), abs(current["low"] - previous))


def _atr(rows, index, window):
    if index < window:
        return None
    return _mean([_true_range(rows, position)
                  for position in range(index + 1 - window, index + 1)])


def _prior_high(rows, index, window):
    start = max(0, index - window)
    prior = rows[start:index]
    return max((row["high"] for row in prior), default=None)


def _prior_volume(rows, index, window=5):
    return _mean([row["volume"] for row in rows[max(0, index - window):index]])


def _close_strength(row):
    amplitude = row["high"] - row["low"]
    return (row["close"] - row["low"]) / amplitude if amplitude > 0 else None


def _direction(value):
    if value is None:
        return "unknown"
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _cycle_id(code, origin, structure_start, anchor_date, parameter_hash):
    return _canonical_digest({
        "code": str(code), "origin_pattern_type": origin,
        "structure_start": structure_start, "anchor_high_date": anchor_date,
        "detector_version": DETECTOR_VERSION, "parameter_hash": parameter_hash,
    })


def _event_id(cycle_id, stage, event_date, sequence=0):
    return _canonical_digest({"cycle_id": cycle_id, "stage": stage,
                              "date": event_date, "sequence": sequence})


def _empty(target_date, code, name, config, reasons=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "parameter_hash": _canonical_digest(config),
        "code": str(code or ""), "name": str(name or ""),
        "as_of_date": target_date,
        "origin_pattern_type": None, "pattern_type": None,
        "stage": "NONE", "confirmation": "none",
        "evidence_status": "data_insufficient" if reasons else "ready",
        "cycle_id": "", "known_at": "", "stage_first_at": "",
        "signal_date": "", "reason_codes": list(dict.fromkeys(reasons or [])),
        "metrics": {}, "checks": {}, "risk_flags": [], "structure": {},
        "context": {}, "secondary_matches": [], "events": [],
        "lifecycle_status": None,
        "kline_evidence": {},
    }


def _new_structure(rows, index, code, parameter_hash, config):
    prior_atr = _atr(rows, index - 1, config["atr_window"])
    double_decline = (index >= 2 and rows[index]["close"] < rows[index - 1]["close"]
                      < rows[index - 2]["close"])
    parent_high = _prior_high(rows, index, config["parent_window"])
    atr_decline = bool(prior_atr and parent_high and
                       parent_high - rows[index]["close"] >=
                       config["parent_trigger_atr"] * prior_atr)
    if not (double_decline or atr_decline) or parent_high is None:
        return None
    start = max(0, index - config["parent_window"])
    anchor_index = max(
        (position for position in range(start, index)
         if rows[position]["high"] == parent_high), default=index - 1)
    structure_start_index = min(anchor_index + 1, index)
    origin = "base_consolidation"
    cycle = _cycle_id(code, origin, rows[structure_start_index]["date"],
                      rows[anchor_index]["date"], parameter_hash)
    return {
        "cycle_id": cycle, "origin_pattern_type": origin,
        "pattern_type": origin, "known_at": rows[index]["date"],
        "created_index": index, "anchor_index": anchor_index,
        "anchor_high": parent_high, "anchor_high_date": rows[anchor_index]["date"],
        "structure_start_index": structure_start_index,
        "building_low": min(row["low"] for row in rows[structure_start_index:index + 1]),
        "signal_base_low": None, "frozen_atr14": None,
        "stage": "NONE", "stage_first_at": "", "trigger_level": None,
        "trigger_scope": "", "below_trigger_days": 0, "reclaim_sequence": 0,
        "lost_stage": None,
        "stage_first_dates": {},
        "parent_recovered_at": "", "parent_recovered_index": None,
        "events": [],
    }


def _instant_metrics(rows, index, active, config):
    current = rows[index]
    previous = rows[index - 1] if index else current
    ma10 = _ma(rows, index, config["ma_short_window"])
    ma20 = _ma(rows, index, config["ma_long_window"])
    past = index - config["ma_slope_days"]
    old_ma10 = _ma(rows, past, config["ma_short_window"]) if past >= 0 else None
    old_ma20 = _ma(rows, past, config["ma_long_window"]) if past >= 0 else None
    slope10 = ma10 / old_ma10 - 1 if ma10 and old_ma10 else None
    slope20 = ma20 / old_ma20 - 1 if ma20 and old_ma20 else None
    atr14 = _atr(rows, index, config["atr_window"])
    prior_volume = _prior_volume(rows, index)
    volume_ratio = current["volume"] / prior_volume if prior_volume else None
    strength = _close_strength(current)
    h5 = _prior_high(rows, index, config["local_short_window"])
    h10 = _prior_high(rows, index, config["local_long_window"])
    h20 = _prior_high(rows, index, config["range_window"])
    daily_return = current["close"] / previous["close"] - 1 if index else None
    return {
        "metric_date": current["date"], "cycle_id": active["cycle_id"],
        "close": current["close"], "daily_return": daily_return,
        "ma10": ma10, "ma20": ma20, "ma10_slope_3": slope10,
        "ma20_slope_3": slope20, "ma_flat_tolerance": config["ma_flat_tolerance"],
        "ma10_direction": _direction(
            None if slope10 is None else 0 if abs(slope10) <= config["ma_flat_tolerance"] else slope10),
        "ma20_direction": _direction(
            None if slope20 is None else 0 if abs(slope20) <= config["ma_flat_tolerance"] else slope20),
        "above_ma10": bool(ma10 and current["close"] > ma10),
        "above_ma20": bool(ma20 and current["close"] > ma20),
        "atr14": atr14, "close_strength": strength,
        "volume_prior5_ratio": volume_ratio, "h5": h5, "h10": h10, "h20": h20,
        "parent_high": active["anchor_high"],
        "drawdown_from_parent": 1 - active["building_low"] / active["anchor_high"],
    }


def _check(value, threshold, operator, passed):
    return {"value": value, "threshold": threshold,
            "operator": operator, "passed": bool(passed)}


def _candidate_from_active(rows, index, active, config, *, event_allowed=True):
    current = rows[index]
    previous = rows[index - 1] if index else current
    active["building_low"] = min(active["building_low"], current["low"])
    metrics = _instant_metrics(rows, index, active, config)
    drawdown = metrics["drawdown_from_parent"]
    if drawdown > config["deep_drawdown"]:
        active["pattern_type"] = "deep_repair"
    age = index - active["created_index"] + 1

    if (active["signal_base_low"] is not None and active["frozen_atr14"]
            and current["close"] < active["signal_base_low"] -
            config["failure_atr"] * active["frozen_atr14"]):
        return None, "invalidated"
    if age > config["max_structure_age"] and not active["parent_recovered_at"]:
        return None, "expired"

    if not active["parent_recovered_at"] and current["close"] > active["anchor_high"]:
        active["parent_recovered_at"] = current["date"]
        active["parent_recovered_index"] = index
    if (active["parent_recovered_index"] is not None
            and index - active["parent_recovered_index"] >= 3):
        prior_atr = _atr(rows, index - 1, config["atr_window"])
        event_peak = max(row["high"] for row in
                         rows[active["parent_recovered_index"]:index])
        if prior_atr and event_peak - current["close"] >= config["parent_trigger_atr"] * prior_atr:
            return None, "completed"

    eligible = (active["pattern_type"] == "deep_repair" or age >= config["base_min_age"])
    stage = "PREPARING" if eligible else "NONE"
    checks = {}
    selected_level = None
    selected_scope = ""

    ma_reclaims = []
    for label, window in (("ma10", config["ma_short_window"]),
                          ("ma20", config["ma_long_window"])):
        now_ma = metrics[label]
        old_ma = _ma(rows, index - 1, window) if index else None
        if now_ma and old_ma and previous["close"] <= old_ma and current["close"] > now_ma:
            was_below = any(
                rows[position]["close"] <= _ma(rows, position, window)
                for position in range(max(window - 1, index - 3), index)
            )
            if was_below:
                ma_reclaims.append(label)
    early_checks = {
        "ma_reclaim": _check(ma_reclaims, "at_least_one", "contains", bool(ma_reclaims)),
        "daily_return": _check(metrics["daily_return"], config["strong_day_return"], ">=",
                               metrics["daily_return"] is not None and
                               metrics["daily_return"] >= config["strong_day_return"]),
        "close_strength": _check(metrics["close_strength"], config["min_close_strength"], ">=",
                                 metrics["close_strength"] is not None and
                                 metrics["close_strength"] >= config["min_close_strength"]),
        "volume_available": _check(metrics["volume_prior5_ratio"], None, "available",
                                   metrics["volume_prior5_ratio"] is not None),
    }
    if eligible:
        checks.update(early_checks)
    if eligible and all(item["passed"] for item in early_checks.values()):
        stage = "EARLY_REPAIR"

    price_events = []
    for scope, level in (("local5", metrics["h5"]), ("local10", metrics["h10"]),
                         ("rolling20", metrics["h20"]),
                         ("parent_structure", active["anchor_high"])):
        crossed = bool(level is not None and current["close"] > level
                       and previous["close"] <= level)
        if crossed:
            price_events.append((scope, level))
    volume_ok = (metrics["volume_prior5_ratio"] is not None and
                 metrics["volume_prior5_ratio"] >= config["min_volume_ratio"])
    strength_ok = (metrics["close_strength"] is not None and
                   metrics["close_strength"] >= config["min_close_strength"])
    confirmed_events = [item for item in price_events if volume_ok and strength_ok]
    range_events = [item for item in confirmed_events
                    if item[0] in {"rolling20", "parent_structure"}]
    local_events = [item for item in confirmed_events
                    if item[0] in {"local5", "local10"}]
    if eligible and range_events:
        stage = "RANGE_BREAKOUT"
        selected_scope, selected_level = range_events[-1]
    elif eligible and local_events:
        stage = "LOCAL_CONFIRMATION"
        selected_scope, selected_level = local_events[-1]
    if price_events:
        checks.update({
            "price_crossed": _check([item[0] for item in price_events], None,
                                    "contains", True),
            "volume_prior5_ratio": _check(metrics["volume_prior5_ratio"],
                                      config["min_volume_ratio"], ">=", volume_ok),
            "close_strength": _check(metrics["close_strength"],
                                     config["min_close_strength"], ">=", strength_ok),
        })

    previous_stage = active["stage"]
    prior_below_days = active["below_trigger_days"]
    trigger = active["trigger_level"]
    reclaim_ready = False
    if trigger is not None and current["close"] <= trigger:
        if STAGE_RANK[previous_stage] >= STAGE_RANK["LOCAL_CONFIRMATION"]:
            active["lost_stage"] = previous_stage
        active["below_trigger_days"] += 1
        if active["lost_stage"]:
            stage = max((stage, "PREPARING"), key=STAGE_RANK.get)
    elif trigger is not None:
        reclaim_ready = bool(active["lost_stage"] and prior_below_days >= 2
                             and previous["close"] <= trigger)
        if reclaim_ready:
            stage = max((stage, active["lost_stage"]), key=STAGE_RANK.get)
            selected_level = trigger
            selected_scope = active["trigger_scope"]
        elif active["lost_stage"]:
            # A one-day dip cannot manufacture a new stage event from a
            # rolling high.  The frozen trigger requires two complete closes
            # below it before a reclaim is eligible.
            stage = "PREPARING"
            selected_level = None
            selected_scope = ""
        active["below_trigger_days"] = 0
    is_new_event = (event_allowed and STAGE_RANK[stage] > STAGE_RANK[previous_stage]
                    and stage != "PREPARING")
    is_reclaim = bool(event_allowed and reclaim_ready)
    if is_new_event or is_reclaim:
        if active["signal_base_low"] is None:
            active["signal_base_low"] = active["building_low"]
            active["frozen_atr14"] = metrics["atr14"]
        if is_reclaim:
            active["reclaim_sequence"] += 1
        active["stage_first_dates"].setdefault(stage, current["date"])
        active["stage_first_at"] = active["stage_first_dates"][stage]
        active["events"].append({
            "event_id": _event_id(active["cycle_id"], stage, current["date"],
                                  active["reclaim_sequence"]),
            "cycle_id": active["cycle_id"], "date": current["date"],
            "stage": stage, "event_type": "reclaim" if is_reclaim else "stage_first",
            "trigger_level": selected_level, "trigger_scope": selected_scope,
        })
        if is_reclaim:
            active["lost_stage"] = None
    if STAGE_RANK[stage] > STAGE_RANK[active["stage"]] or active["stage"] == "NONE":
        active["stage"] = stage
    elif STAGE_RANK[active["stage"]] >= STAGE_RANK["LOCAL_CONFIRMATION"]:
        active["stage"] = stage
    if (selected_level is not None and
            (is_new_event or is_reclaim or active["trigger_level"] is None)):
        active["trigger_level"] = selected_level
        active["trigger_scope"] = selected_scope

    shown_stage = active["stage"]
    reference = active["trigger_level"] or active["anchor_high"]
    atr_distance = (max(0.0, (current["close"] - reference) / metrics["atr14"])
                    if reference and metrics["atr14"] else None)
    extended = bool(reference and (
        current["close"] / reference - 1 > config["extended_ratio"]
        or (metrics["atr14"] and current["close"] - reference >
            config["extended_atr"] * metrics["atr14"])))
    risks = []
    if extended:
        risks.append("extended")
    if metrics["volume_prior5_ratio"] is not None and metrics["volume_prior5_ratio"] < config["min_volume_ratio"]:
        risks.append("weak_volume")
    if active["below_trigger_days"] or active["lost_stage"]:
        risks.append("lost_level")
    if (current["close"] <= active["anchor_high"]
            and active["trigger_scope"] != "parent_structure"):
        risks.append("parent_overhead")
    if current["high"] == current["low"]:
        risks.append("flat_bar")
    metrics.update({"stage_reference": reference, "trigger_distance_atr": atr_distance,
                    "structure_age": age})
    checks["holds_trigger_level"] = _check(
        current["close"], active["trigger_level"], ">",
        active["trigger_level"] is None or current["close"] > active["trigger_level"])
    return {
        "origin_pattern_type": active["origin_pattern_type"],
        "pattern_type": active["pattern_type"], "stage": shown_stage,
        "cycle_id": active["cycle_id"], "known_at": active["known_at"],
        "stage_first_at": active["stage_first_dates"].get(shown_stage, ""),
        "signal_date": current["date"] if (is_new_event or is_reclaim) else "",
        "metrics": metrics, "checks": checks, "risk_flags": risks,
        "structure": {
            "anchor_high": active["anchor_high"],
            "anchor_high_date": active["anchor_high_date"],
            "anchor_high_known_at": active["known_at"],
            "structure_start": rows[active["structure_start_index"]]["date"],
            "building_low": active["building_low"],
            "signal_base_low": active["signal_base_low"],
            "frozen_atr14": active["frozen_atr14"],
            "failure_line": (active["signal_base_low"] - config["failure_atr"] * active["frozen_atr14"]
                             if active["signal_base_low"] is not None and active["frozen_atr14"] else None),
            "parent_recovered_at": active["parent_recovered_at"],
            "trigger_level": active["trigger_level"],
            "breakout_scope": active["trigger_scope"] or None,
            "lifecycle_status": "active",
        },
        "events": copy.deepcopy(active["events"]),
        "is_new_event": bool(is_new_event or is_reclaim),
        "lifecycle_status": "active",
    }, None


def _terminal_candidate(rows, index, active, config, lifecycle_status, events):
    """Keep the ended cycle auditable on its terminal date."""
    metrics = _instant_metrics(rows, index, active, config)
    failure_line = (
        active["signal_base_low"] - config["failure_atr"] * active["frozen_atr14"]
        if active["signal_base_low"] is not None and active["frozen_atr14"] else None)
    return {
        "origin_pattern_type": active["origin_pattern_type"],
        "pattern_type": active["pattern_type"], "stage": active["stage"],
        "cycle_id": active["cycle_id"], "known_at": active["known_at"],
        "stage_first_at": active["stage_first_dates"].get(
            active["stage"], active["stage_first_at"]),
        "signal_date": "", "metrics": metrics, "checks": {},
        "risk_flags": ["lost_level"] if lifecycle_status == "invalidated" else [],
        "structure": {
            "anchor_high": active["anchor_high"],
            "anchor_high_date": active["anchor_high_date"],
            "anchor_high_known_at": active["known_at"],
            "structure_start": rows[active["structure_start_index"]]["date"],
            "building_low": active["building_low"],
            "signal_base_low": active["signal_base_low"],
            "frozen_atr14": active["frozen_atr14"],
            "failure_line": failure_line,
            "parent_recovered_at": active["parent_recovered_at"],
            "trigger_level": active["trigger_level"],
            "breakout_scope": active["trigger_scope"] or None,
            "lifecycle_status": lifecycle_status,
        },
        "events": copy.deepcopy(events), "is_new_event": False,
        "lifecycle_status": lifecycle_status,
    }


def _parent_replay(rows, code, parameter_hash, config):
    active = None
    current_candidate = None
    lifecycle_events = []
    start = max(config["parent_window"], config["ma_long_window"] + config["ma_slope_days"],
                config["atr_window"] + 1)
    for index in range(start, len(rows)):
        if active is None:
            active = _new_structure(rows, index, code, parameter_hash, config)
        if active is None:
            current_candidate = None
            continue
        candidate, ended = _candidate_from_active(rows, index, active, config)
        if ended:
            lifecycle_events.extend(active["events"])
            lifecycle_events.append({"cycle_id": active["cycle_id"],
                                     "event_id": _event_id(active["cycle_id"],
                                                           "LIFECYCLE", rows[index]["date"]),
                                     "date": rows[index]["date"],
                                     "stage": active["stage"],
                                     "lifecycle_status": ended})
            current_candidate = _terminal_candidate(
                rows, index, active, config, ended, lifecycle_events)
            # A completed parent recovery establishes the next adjustment on
            # T with a T-1 event high, while the terminal row remains the
            # public result needed to close the old lineage.  Invalidated and
            # expired cycles may only restart on a later session.
            active = (_new_structure(rows, index, code, parameter_hash, config)
                      if ended == "completed" else None)
            continue
        current_candidate = candidate
    if current_candidate:
        current_candidate["events"] = lifecycle_events + current_candidate["events"]
    return current_candidate, lifecycle_events


def _v1_candidate(v1_result, rows, code, parameter_hash, config):
    if not isinstance(v1_result, dict) or not rows:
        return None
    state = str(v1_result.get("state") or "")
    mapping = {
        "SUPPLY_DRY": ("PREPARING", "none"),
        "PULLBACK": ("PREPARING", "none"),
        "BREAKOUT_CONFIRMED": ("LOCAL_CONFIRMATION", "close_confirmed"),
        "PROVISIONAL_BREAKOUT": ("LOCAL_CONFIRMATION", "provisional"),
    }
    if state not in mapping:
        return None
    stage, confirmation = mapping[state]
    current = rows[-1]
    signal_date = str(v1_result.get("signal_date") or "")
    if stage == "LOCAL_CONFIRMATION" and signal_date != current["date"]:
        return None
    h20 = _prior_high(rows, len(rows) - 1, config["range_window"])
    if (stage == "LOCAL_CONFIRMATION" and h20 and current["close"] > h20
            and len(rows) > 1 and rows[-2]["close"] <= h20):
        stage = "RANGE_BREAKOUT"
    v1_cycle_id = str(v1_result.get("cycle_id") or "")
    cycle_id = _canonical_digest({
        "code": str(code), "origin_pattern_type": "short_pullback",
        "v1_cycle_id": v1_cycle_id, "detector_version": DETECTOR_VERSION,
        "parameter_hash": parameter_hash,
    })
    metrics = {
        "metric_date": current["date"], "cycle_id": cycle_id,
        "close": current["close"], "h20": h20,
        "va_v0_ratio": v1_result.get("va_v0_ratio"),
    }
    if stage in {"LOCAL_CONFIRMATION", "RANGE_BREAKOUT"}:
        metrics.update(
            breakout_volume_ratio=v1_result.get("breakout_volume_ratio"),
            close_strength=v1_result.get("close_strength"),
            breakout_ratio=v1_result.get("breakout_ratio"),
        )
    events = []
    for sequence, raw_event in enumerate(v1_result.get("events") or []):
        if not isinstance(raw_event, dict):
            continue
        event = copy.deepcopy(raw_event)
        event_date = str(event.get("date") or signal_date or current["date"])
        event.update(event_id=_event_id(cycle_id, str(event.get("state") or stage),
                                        event_date, sequence),
                     date=event_date, cycle_id=cycle_id)
        events.append(event)
    return {
        "origin_pattern_type": "short_pullback", "pattern_type": "short_pullback",
        "stage": stage, "confirmation": confirmation, "cycle_id": cycle_id,
        "known_at": str(v1_result.get("d0_date") or ""),
        "stage_first_at": signal_date if stage != "PREPARING" else "",
        "signal_date": signal_date, "metrics": metrics,
        "checks": copy.deepcopy(v1_result.get("checks") or {}),
        "risk_flags": (["extended"] if v1_result.get("breakout_ratio") and
                       v1_result["breakout_ratio"] > 1 + config["extended_ratio"] else []),
        "structure": {"trigger_level": v1_result.get("H"),
                      "lifecycle_status": "active", "breakout_scope":
                      "rolling20" if stage == "RANGE_BREAKOUT" else "v1_adjustment"},
        "events": events,
        "is_new_event": bool(signal_date == current["date"]),
        "lifecycle_status": "active",
        "source_evidence_status": str(v1_result.get("evidence_status") or ""),
    }


def _main_key(candidate):
    known_at = str(candidate.get("known_at") or "").replace("-", "")
    known_rank = -int(known_at) if known_at.isdigit() else 0
    return (
        1 if candidate.get("evidence_status") == "ready" else 0,
        STAGE_RANK.get(candidate.get("stage"), 0),
        1 if candidate.get("is_new_event") else 0,
        1 if candidate.get("structure", {}).get("anchor_high") is not None else 0,
        PATTERN_RANK.get(candidate.get("pattern_type"), 0),
        known_rank, str(candidate.get("cycle_id") or ""),
    )


def analyze_daily_weak_to_strong_v2(
        kline_payload, *, as_of_date, code="", name="", close_evidence=None,
        calendar_evidence=None, config=None, v1_result=None):
    """Classify one stock with the frozen v2 research contract."""
    resolved, config_reasons = _config(config)
    target = _normalise_date(as_of_date)
    result = _empty(target, code, name, resolved, config_reasons)
    if not target:
        result["reason_codes"] = ["as_of_date_invalid"]
        return result
    if config_reasons:
        return result
    rows, row_reasons, duplicates = _normalise_rows(kline_payload, target)
    meta = kline_payload.get("meta", {}) if isinstance(kline_payload, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    basis = str(meta.get("adj") or meta.get("adjustment") or "").lower()
    result["kline_evidence"] = {
        "provider": str(meta.get("data_source") or meta.get("provider") or ""),
        "basis": basis, "volume_unit": str(meta.get("volume_unit") or "unknown"),
        "data_date": rows[-1]["date"] if rows else "",
        "fetched_at": str(meta.get("fetch_time") or meta.get("fetched_at") or ""),
        "close_confirmation": copy.deepcopy(close_evidence or {}),
        "duplicate_dates": duplicates, "input_sha256": _canonical_digest(rows),
    }
    reasons = list(row_reasons)
    if basis not in {"qfq", "hfq", "none", "unadjusted"}:
        reasons.append("basis_unknown")
    if meta.get("volume_basis_conflict") is True or meta.get("mixed_sources") is True:
        reasons.append("volume_basis_conflict")
    if len(rows) < resolved["min_history_bars"]:
        reasons.append("history_insufficient")
    if not rows or rows[-1]["date"] != target:
        reasons.append("as_of_not_covered")
    if reasons:
        result.update(evidence_status="data_insufficient",
                      reason_codes=list(dict.fromkeys(reasons)))
        result["kline_evidence"]["calendar_coverage"] = {}
        return result

    required_start = rows[-resolved["min_history_bars"]]["date"]
    _, calendar_reasons, detail = _calendar_status(
        calendar_evidence, rows, required_start, target)
    result["kline_evidence"]["calendar_coverage"] = detail
    close_confirmed = bool(isinstance(close_evidence, dict)
                           and close_evidence.get("confirmed") is True
                           and close_evidence.get("source") != "not_required")
    provisional = bool(isinstance(close_evidence, dict)
                       and close_evidence.get("provisional") is True)
    technical_rows = rows if close_confirmed or provisional else rows[:-1]
    if len(technical_rows) < resolved["min_history_bars"]:
        result.update(evidence_status="data_insufficient",
                      reason_codes=["close_unconfirmed"])
        return result
    parent, lifecycle_events = _parent_replay(
        technical_rows, code, result["parameter_hash"], resolved)
    strict = _v1_candidate(
        v1_result, technical_rows, code, result["parameter_hash"], resolved)
    candidates = [item for item in (parent, strict) if item]

    evidence_reasons = list(calendar_reasons)
    if not close_confirmed:
        evidence_reasons.append("close_unconfirmed")
    evidence_status = "ready" if not evidence_reasons else "data_insufficient"
    result["evidence_status"] = evidence_status
    result["reason_codes"] = list(dict.fromkeys(evidence_reasons))
    if not candidates:
        result["events"] = lifecycle_events
        return result
    for candidate in candidates:
        candidate_evidence = evidence_status
        if (candidate.get("origin_pattern_type") == "short_pullback"
                and candidate.pop("source_evidence_status", "") != "ready"):
            candidate_evidence = "data_insufficient"
        candidate.update(
            code=str(code or ""), name=str(name or ""), as_of_date=target,
            detector_version=DETECTOR_VERSION,
            parameter_hash=result["parameter_hash"],
            evidence_status=candidate_evidence,
        )
        candidate.setdefault("context", {})
        candidate.setdefault("secondary_matches", [])
        candidate.setdefault("reason_codes", [] if candidate_evidence == "ready"
                             else list(result["reason_codes"]) or
                             ["source_evidence_incomplete"])
        if candidate.get("confirmation") not in {
                "close_confirmed", "provisional", "evidence_incomplete"}:
            candidate["confirmation"] = (
                "close_confirmed" if (close_confirmed and
                                      STAGE_RANK[candidate["stage"]] >= 2)
                else "provisional" if (provisional and
                                        STAGE_RANK[candidate["stage"]] >= 2)
                else "none" if close_confirmed else "evidence_incomplete")
    candidates.sort(key=_main_key, reverse=True)
    main = copy.deepcopy(candidates[0])
    result.update({key: main.get(key, result.get(key)) for key in (
        "origin_pattern_type", "pattern_type", "stage", "cycle_id", "known_at",
        "stage_first_at", "signal_date", "metrics", "checks", "risk_flags",
        "structure", "events", "lifecycle_status", "confirmation",
        "evidence_status")})
    secondary = []
    for candidate in candidates[1:]:
        item = copy.deepcopy(candidate)
        item.pop("is_new_event", None)
        secondary.append(item)
    result["secondary_matches"] = secondary
    result["context"] = {
        "v1_state": str(v1_result.get("state") or "") if isinstance(v1_result, dict) else "",
        "v1_qualified": bool(isinstance(v1_result, dict) and
                             v1_result.get("state") == "BREAKOUT_CONFIRMED" and
                             v1_result.get("evidence_status") == "ready"),
    }
    return result


__all__ = ["DEFAULT_CONFIG", "DETECTOR_VERSION", "SCHEMA_VERSION",
           "analyze_daily_weak_to_strong_v2"]
