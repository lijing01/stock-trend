#!/usr/bin/env python3
"""Offline, immutable shadow research for confirmed LPS entry distance.

This module never publishes a recommendation or changes the production rank.
It replays a frozen formal snapshot, changes only already occupied eligible LPS
slots, and evaluates the resulting Top-K lists against immutable outcomes.
"""

import argparse
import copy
import json
import math
import os
import random
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from analysis.factor_ablation import _scope_records
from analysis.recommendation_diagnostics import (
    load_candidate_signal_items, load_primary_research_snapshots,
)
from backtesting.recommendation_experiments import (
    DEFAULT_CONTRACT_ID, _frozen_replay_candidate, _replay_selection,
)
from core.evolution_storage import EVOLUTION_ROOT, storage_root
from core.recommendation_snapshot import (
    DEFAULT_ROOT as OFFICIAL_ROOT, canonical_json, content_sha256,
    load_official_snapshot,
)
from core.research_events import assign_research_events


SCHEMA_VERSION = "lps-distance-research/v1"
TOP_K = (1, 3, 5)
WINDOWS = (5, 10, 20, 60)
PRIMARY_WINDOW = 20
CONFIRMATION_WINDOW = 60
POPULATION_KIND = "frozen_investable_research_population"
FORMAL_BUCKETS = ("actionable", "waiting_trigger")
ALL_BUCKETS = (
    "actionable", "waiting_trigger", "next_day_confirmation", "observation",
    "data_rejected", "unenriched_observation",
)
FINAL_CLOSE_TIME = time(15, 10)
DEFINITION_FROZEN_ON = "2026-10-01"


def definition(contract_id=DEFAULT_CONTRACT_ID):
    frozen = {
        "schema_version": SCHEMA_VERSION,
        "definition_frozen_on": DEFINITION_FROZEN_ON,
        "implementation_revision": 2,
        "evaluation_contract_id": contract_id,
        "population_kind": POPULATION_KIND,
        "distance": "(close-trigger_close)/atr",
        "percentage": "close/trigger_close-1",
        "bins": ["negative", "0_to_0_5", "0_5_to_1", "over_1", "insufficient"],
        "top_k": list(TOP_K), "windows": list(WINDOWS),
        "primary_top_k": 3, "primary_window": PRIMARY_WINDOW,
        "confirmation_window": CONFIRMATION_WINDOW,
        "quality_bands": ["low_<60", "medium_60_79", "high_80_plus"],
        "lps_rule": {"sub_phase": "lps", "signal_status": "confirmed",
                     "maximum_signal_age_bars": 3, "current_state": "confirmed_holding",
                     "requires_positive_trigger_and_atr": True,
                     "requires_close_above_structural_floor": True},
        "final_close_rule": {"same_source_date": True, "cutoff_local_time": "15:10:00",
                             "timezone": "Asia/Shanghai", "cache_only_rejected": True,
                             "source_must_be_available_by_frozen_decision": True},
        "stored_distance_tolerance": .00011,
        "negative_distance_rank_treatment": "excluded_fixed_slot",
        "calendar_rule": "frozen_sorted_unique_sessions_exit_index_minus_entry_index_equals_window_minus_one",
        "event_rule": "common_full_formal_universe_earliest_overlapping_anchor_per_window",
        "bootstrap": {"method": "moving_trading_session_block_bootstrap", "block_length": 5,
                      "draws": 2000, "seed": 20261001, "confidence": .95},
        "treatment": "same_bucket_and_quality_band_nonnegative_lps_slot_permutation",
        "minimum_mature_dates": 20, "minimum_unique_alpha_events": 100,
        "minimum_paired_coverage": .90,
        "decision": "signal_research_only_no_strategy_upgrade",
    }
    frozen["definition_sha256"] = content_sha256(frozen)
    return frozen


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _day(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    text = str(value or "").strip()
    if len(text) == 15 and text[8] == "-" and text.replace("-", "").isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}T{text[9:11]}:{text[11:13]}:{text[13:15]}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _codes(rows):
    return [str(row.get("code")) for row in rows or [] if isinstance(row, dict)]


def _bucket_codes(buckets):
    return {name: _codes((buckets or {}).get(name)) for name in ALL_BUCKETS}


def _quality_band(record):
    candidate = record.get("candidate") or {}
    score = _number(candidate.get("quality_adjusted_score"))
    if score is None:
        return "unknown"
    return "low_<60" if score < 60 else "medium_60_79" if score < 80 else "high_80_plus"


def _market_band(regime):
    score = _number((regime or {}).get("score", (regime or {}).get("market_score")))
    if score is None: return "unknown"
    return "weak_<60" if score < 60 else "neutral_60_79" if score < 80 else "strong_80_plus"


def distance_bin(value):
    value = _number(value)
    if value is None:
        return "insufficient"
    if value < 0:
        return "negative"
    if value <= .5:
        return "0_to_0_5"
    if value <= 1:
        return "0_5_to_1"
    return "over_1"


def _final_close_evidence(record, recommendation_day):
    """Accept explicit final-close proof or a non-cache same-day fetch >=15:10."""
    candidate = record.get("candidate") or {}
    source = (candidate.get("source_evidence") or {}).get("kline") or {}
    dates = ((candidate.get("data_quality") or {}).get("dimensions") or {}).get("kline") or {}
    source_day = _day(source.get("data_date") or source.get("source_date")
                      or dates.get("data_date") or dates.get("source_date"))
    explicit = source.get("final_close_confirmed") is True or str(
        source.get("close_status") or source.get("status") or "").lower() in {
            "final_close", "official_close", "close_confirmed",
        }
    fetched_value = (source.get("fetched_at") or source.get("fetch_time")
                     or dates.get("fetched_at") or dates.get("fetch_time"))
    fetched = _timestamp(fetched_value)
    if fetched and fetched.tzinfo is not None:
        fetched = fetched.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    fetched_after_close = bool(
        fetched and fetched.date().isoformat() == recommendation_day
        and fetched.timetz().replace(tzinfo=None) >= FINAL_CLOSE_TIME
    )
    cache_used = source.get("cache_used") is True or str(source.get("status") or "").lower() in {
        "cache_hit", "cached", "cached_valid", "cache_valid",
    }
    frozen_decision = _timestamp(record.get("decision_at"))
    if frozen_decision and frozen_decision.tzinfo is not None:
        frozen_decision = frozen_decision.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    available_at_decision = bool(fetched and frozen_decision and fetched <= frozen_decision)
    valid = (source_day == recommendation_day and available_at_decision
             and (explicit or (fetched_after_close and not cache_used)))
    return {
        "status": "confirmed" if valid else "insufficient",
        "source_date": source_day, "fetched_at": fetched_value,
        "explicit_final_close": explicit, "cache_used": cache_used,
        "reason": None if valid else "final_close_evidence_missing",
    }


def _nested(payload, *paths):
    for path in paths:
        value = payload
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def classify_lps_record(record, recommendation_day):
    """Return auditable distance evidence; unknown values never become zero."""
    candidate = record.get("candidate") or {}
    wyckoff = candidate.get("wyckoff") or {}
    timing = wyckoff.get("entry_timing") if isinstance(wyckoff.get("entry_timing"), dict) else {}
    short = wyckoff.get("short_term") if isinstance(wyckoff.get("short_term"), dict) else {}
    health = _nested(wyckoff, ("event_health",), ("health",)) or {}
    sub_phase = str(timing.get("sub_phase") or short.get("sub_phase")
                    or wyckoff.get("sub_phase") or "").lower()
    status = str(short.get("signal_status") or timing.get("signal_status")
                 or wyckoff.get("signal_status") or "").lower()
    age = _number(timing.get("signal_age_bars"))
    if age is None:
        age = _number(short.get("signal_age_bars"))
    state = str(timing.get("current_state") or health.get("state")
                or health.get("current_state") or "").lower()
    close = _number(timing.get("current_close"))
    trigger = _number(timing.get("trigger_close"))
    atr = _number(timing.get("current_atr"))
    floor = _number(health.get("structural_floor"))
    if floor is None:
        floor = _number(_nested(wyckoff, ("evidence", "event_health", "structural_floor")))
    close_evidence = _final_close_evidence(record, recommendation_day)
    reasons = []
    if sub_phase != "lps": reasons.append("not_lps")
    if status != "confirmed": reasons.append("signal_not_confirmed")
    if age is None or age < 0 or age > 3 or int(age) != age: reasons.append("signal_age_invalid")
    if state != "confirmed_holding": reasons.append("state_not_confirmed_holding")
    if close is None or trigger is None or atr is None or trigger <= 0 or atr <= 0:
        reasons.append("price_or_atr_invalid")
    if floor is None: reasons.append("structural_floor_missing")
    elif close is not None and close <= floor: reasons.append("below_structural_floor")
    if close_evidence["status"] != "confirmed": reasons.append("final_close_evidence_missing")
    distance = percentage = None
    if not reasons:
        distance = (close - trigger) / atr
        percentage = close / trigger - 1
        stored_d = _number(timing.get("trigger_extension_atr"))
        stored_p = _number(timing.get("trigger_extension_pct"))
        if stored_d is not None and abs(stored_d - distance) > .00011:
            reasons.append("stored_distance_mismatch")
        if stored_p is not None and abs(stored_p - percentage) > .00011:
            reasons.append("stored_percentage_mismatch")
    eligible = not reasons
    return {
        "record_id": record.get("record_id"), "code": str(record.get("code") or ""),
        "formal_bucket": record.get("final_status"), "quality_band": _quality_band(record),
        "is_lps": sub_phase == "lps", "eligible": eligible,
        "distance_atr": round(distance, 8) if eligible else None,
        "distance_pct": round(percentage, 8) if eligible else None,
        "distance_bin": distance_bin(distance) if eligible else "insufficient",
        "signal_age_bars": int(age) if age is not None and int(age) == age else age,
        "current_state": state or "unknown", "current_close": close,
        "trigger_close": trigger, "current_atr": atr, "structural_floor": floor,
        "execution_priority_score": _number((record.get("scores") or {}).get("execution_priority_score")),
        "close_evidence": close_evidence, "reasons": sorted(set(reasons)),
        "basis_date": record.get("basis_date"), "decision_at": record.get("decision_at"),
        "known_at": record.get("known_at"), "captured_at": record.get("captured_at"),
        "market": record.get("market") or candidate.get("market") or candidate.get("exchange") or "default",
        "sector_actionable": candidate.get("sector_actionable"),
    }


def _top_k(bucket_codes):
    ordered = [code for bucket in FORMAL_BUCKETS for code in bucket_codes.get(bucket, [])]
    return {str(k): ordered[:k] for k in TOP_K}


def _treatment_buckets(baseline, evidence):
    treated = copy.deepcopy(baseline)
    for bucket in ALL_BUCKETS:
        codes = list(treated.get(bucket, []))
        groups = defaultdict(list)
        for index, code in enumerate(codes):
            item = evidence.get(code) or {}
            if item.get("eligible") and _number(item.get("distance_atr")) is not None \
                    and item["distance_atr"] >= 0:
                groups[item.get("quality_band")].append(index)
        for indexes in groups.values():
            values = [codes[index] for index in indexes]
            values.sort(key=lambda code: (
                evidence[code]["distance_atr"],
                -(_number(evidence[code].get("execution_priority_score")) or 0), code,
            ))
            for index, code in zip(indexes, values):
                codes[index] = code
        treated[bucket] = codes
    return treated


def run_daily_distance(research_snapshot, official_snapshot, contract_id=DEFAULT_CONTRACT_ID):
    content = (research_snapshot or {}).get("content") or {}
    formal = (official_snapshot or {}).get("content") or {}
    day = content.get("recommendation_date")
    result = {
        "schema_version": SCHEMA_VERSION, "recommendation_date": day,
        "definition": definition(contract_id), "status": "skipped", "reason": None,
        "research_snapshot_sha256": (research_snapshot or {}).get("content_sha256"),
        "official_snapshot_sha256": (official_snapshot or {}).get("content_sha256"),
    }
    if content.get("snapshot_type") != "formal" or formal.get("snapshot_type") != "formal":
        result["reason"] = "not_formal"; return result
    if not day or day != formal.get("recommendation_date"):
        result["reason"] = "date_mismatch"; return result
    if (content.get("official_snapshot") or {}).get("link_status") != "linked":
        result["reason"] = "official_unlinked"; return result
    if not isinstance(content.get("selection_scope"), dict):
        result.update(status="scope_unverified", reason="scope_unverified"); return result
    if (result["research_snapshot_sha256"] != content_sha256(content)
            or result["official_snapshot_sha256"] != content_sha256(formal)
            or (content.get("official_snapshot") or {}).get("content_sha256")
            != result["official_snapshot_sha256"]):
        result["reason"] = "snapshot_hash_mismatch"; return result
    manifest = content.get("input_manifest") or {}
    inputs = manifest.get("inputs") or {}
    if (manifest.get("input_sha256") != content_sha256(inputs)
            or inputs.get("candidate_records") != content.get("records")
            or inputs.get("selection_scope") != content.get("selection_scope")):
        result["reason"] = "input_manifest_mismatch"; return result
    records, scope_error = _scope_records(content)
    if scope_error:
        result.update(status=scope_error, reason=scope_error); return result
    params, scope = content.get("parameter_summary") or {}, content["selection_scope"]
    top, min_score = params.get("top"), _number(params.get("min_score"))
    if (isinstance(top, bool) or not isinstance(top, int) or top < 1 or min_score is None
            or scope.get("top") != top or _number(scope.get("min_score")) != min_score
            or content.get("policy") != formal.get("policy")
            or (content.get("model_version") != formal.get("model_version") and not (
                content.get("model_version") == "daily-candidates/v5-news-shadow"
                and formal.get("model_version") == "daily-candidates/v4"))):
        result.update(status="input_incomplete", reason="model_or_parameter_mismatch"); return result
    active = [row for row in records if row.get("final_status") != "phase2_filtered"]
    bonuses = params.get("buy_point_priority_bonus")
    baseline = _replay_selection(active, bonuses, top, min_score, content.get("policy"))
    if baseline["status"] != "ok":
        result.update(status="input_incomplete", reason=",".join(baseline["reasons"])); return result
    baseline_buckets = _bucket_codes(baseline["buckets"])
    if (_codes(baseline["selected"]) != _codes(formal.get("candidates"))
            or baseline_buckets != _bucket_codes(formal.get("buckets"))):
        result.update(status="baseline_mismatch", reason="selection_or_bucket_mismatch"); return result
    evidence, record_ids, invalid = {}, {}, []
    for record in active:
        frozen, error = _frozen_replay_candidate(record, min_score)
        if error:
            invalid.append({"record_id": record.get("record_id"), "reason": error}); continue
        code = str(record.get("code")); evidence[code] = classify_lps_record(record, day)
        evidence[code]["quality_band"] = (
            "low_<60" if frozen["quality_adjusted_score"] < 60 else
            "medium_60_79" if frozen["quality_adjusted_score"] < 80 else "high_80_plus")
        evidence[code]["execution_priority_score"] = _number(
            frozen.get("execution_priority_score"))
        evidence[code]["market_regime_band"] = _market_band(content.get("market_regime"))
        record_ids[code] = str(record.get("record_id"))
    if invalid:
        result.update(status="input_incomplete", reason="replay_evidence_incomplete",
                      invalid_records=invalid); return result
    treatment_buckets = _treatment_buckets(baseline_buckets, evidence)
    result.update({
        "status": "completed", "selection_scope": copy.deepcopy(scope),
        "evidence_phase": "prospective" if day >= DEFINITION_FROZEN_ON else "historical_backfill",
        "selection_scope_sha256": scope.get("codes_sha256"),
        "record_ids": record_ids, "records": [evidence[code] for code in sorted(evidence)],
        "baseline": {"buckets": baseline_buckets, "top_k": _top_k(baseline_buckets)},
        "treatment": {"buckets": treatment_buckets, "top_k": _top_k(treatment_buckets)},
    })
    result["input_sha256"] = content_sha256({
        "research": result["research_snapshot_sha256"],
        "official": result["official_snapshot_sha256"],
        "definition": result["definition"],
    })
    result["experiment_id"] = result["input_sha256"][:16]
    return result


def _outcome_index(items, cutoff, contract_id):
    indexed, conflicts = {}, set()
    for item in items or []:
        if not isinstance(item, dict) or item.get("point_in_time_status") == "legacy_unverified":
            continue
        as_of = _day(item.get("evaluation_as_of"))
        if as_of is None or as_of > cutoff:
            continue
        contract = item.get("evaluation_contract") or {}
        actual_contract = item.get("contract_id") or contract.get("contract_id")
        population = item.get("population_kind") or contract.get("population_kind")
        if actual_contract != contract_id or population != POPULATION_KIND:
            continue
        key = (item.get("record_id"), item.get("research_snapshot_sha256"), actual_contract,
               population)
        if not all(key):
            continue
        if item.get("conflicts") or item.get("point_in_time_status") == "evaluation_conflict":
            conflicts.add(key)
            continue
        if key in indexed:
            if canonical_json(indexed[key]) != canonical_json(item): conflicts.add(key)
        else:
            indexed[key] = item
    for key in conflicts: indexed.pop(key, None)
    return indexed, conflicts


def _metrics(values):
    def median(items):
        items = sorted(items); n = len(items)
        return None if not n else items[n // 2] if n % 2 else (items[n // 2 - 1] + items[n // 2]) / 2
    returns = [row["signal_return"] for row in values if row.get("signal_return") is not None]
    alpha = [row["hs300_alpha"] for row in values if row.get("hs300_alpha") is not None]
    mae = [row["mae"] for row in values if row.get("mae") is not None]
    return {"event_count": len(values), "return_count": len(returns), "alpha_count": len(alpha),
            "mean_return": sum(returns) / len(returns) if returns else None,
            "median_return": median(returns),
            "positive_fraction": sum(v > 0 for v in returns) / len(returns) if returns else None,
            "mean_hs300_alpha": sum(alpha) / len(alpha) if alpha else None,
            "mean_mae": sum(mae) / len(mae) if mae else None}


def _calendar_valid(row, window, cutoff=None):
    sessions = row.get("market_sessions")
    if not isinstance(sessions, list) or not sessions:
        return False
    normalized = [_day(value) for value in sessions]
    if any(value is None for value in normalized) or normalized != sorted(set(normalized)):
        return False
    entry, exit_day = _day(row.get("entry_date")), _day(row.get("exit_date"))
    recommendation_day = _day(row.get("recommendation_date"))
    if recommendation_day not in normalized or entry not in normalized or exit_day not in normalized:
        return False
    if normalized.index(entry) != normalized.index(recommendation_day) + 1:
        return False
    if cutoff is not None and (exit_day is None or exit_day > cutoff):
        return False
    return normalized.index(exit_day) - normalized.index(entry) == int(window) - 1


def _merged_frozen_calendar(rows):
    calendars = []
    for row in rows:
        values = [_day(value) for value in row.get("market_sessions") or []]
        if not values or any(value is None for value in values) or values != sorted(set(values)):
            return None
        calendars.append(values)
    merged = sorted({value for values in calendars for value in values})
    positions = {value: index for index, value in enumerate(merged)}
    for values in calendars:
        indexes = [positions[value] for value in values]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            return None
    return merged


def _block_bootstrap(pairs, calendar, block_length=5, draws=2000, seed=20261001):
    pairs = sorted(pairs, key=lambda row: row["date"])
    values = [float(row["alpha_delta"]) for row in pairs
              if _number(row.get("alpha_delta")) is not None]
    days = [row["date"] for row in pairs]
    if len(values) != len(pairs) or len(values) < block_length or not calendar:
        return None
    positions = {value: index for index, value in enumerate(calendar)}
    if any(day not in positions for day in days):
        return None
    indexes = [positions[day] for day in days]
    rng, n, means = random.Random(seed), len(values), []
    runs = []
    start = 0
    for index in range(1, n):
        if indexes[index] != indexes[index - 1] + 1:
            runs.append(values[start:index])
            start = index
    runs.append(values[start:])
    # Every paired observation must contribute. Short runs cannot disappear
    # while the remaining runs supply a deceptively narrow interval.
    if any(len(run) < block_length for run in runs):
        return None
    run_blocks = [[run[index:index + block_length]
                   for index in range(len(run) - block_length + 1)] for run in runs]
    for _ in range(draws):
        sample = []
        for run, blocks in zip(runs, run_blocks):
            run_sample = []
            while len(run_sample) < len(run):
                run_sample.extend(blocks[rng.randrange(len(blocks))])
            sample.extend(run_sample[:len(run)])
        means.append(sum(sample) / n)
    means.sort()
    return {"method": "moving_trading_session_block_bootstrap",
            "preregistered_method_satisfied": True, "block_length": block_length,
            "draws": draws, "seed": seed, "confidence": .95,
            "lower": means[int(draws * .025)], "upper": means[min(draws - 1, int(draws * .975))]}


def _cohort_id(contract_id, window, k, side, rows):
    identities = sorted((row.get("record_id"), row.get("recommendation_date"),
                         row.get("event_id")) for row in rows)
    return content_sha256({"schema_version": SCHEMA_VERSION, "contract_id": contract_id,
                           "window": window, "top_k": k, "side": side,
                           "identities": identities})[:16]


def _outcome_problem(item, result):
    if item is None: return "outcome_missing"
    status = str(result.get("status") or "").lower()
    if status in {"pending", "not_mature", "insufficient_sessions"}: return "outcome_pending"
    if status in {"data_error", "fetch_error", "provider_error"}: return "outcome_data_error"
    if status != "complete": return "outcome_invalid"
    return None


def _descriptive_groups(daily, index, conflict_keys, contract_id, cutoff):
    """Describe frozen LPS events independently of the Top-K comparison."""
    output = {}
    for window in WINDOWS:
        rows, missing, blocked = [], defaultdict(int), set()
        for artifact in daily:
            day, snapshot_hash = artifact["recommendation_date"], artifact["research_snapshot_sha256"]
            for evidence in artifact.get("records") or []:
                if not evidence.get("is_lps"):
                    continue
                identity = (str(evidence.get("market") or "default").upper(), evidence.get("code"))
                if identity in blocked:
                    missing["earlier_unresolved_anchor"] += 1; continue
                key = (evidence.get("record_id"), snapshot_hash, contract_id, POPULATION_KIND)
                item = index.get(key)
                result = ((item or {}).get("windows") or {}).get(str(window)) or {}
                if key in conflict_keys:
                    missing["outcome_conflict"] += 1; blocked.add(identity); continue
                problem = _outcome_problem(item, result)
                if problem:
                    missing[problem] += 1; blocked.add(identity); continue
                if item.get("recommendation_date") != day:
                    missing["outcome_invalid_identity"] += 1; blocked.add(identity); continue
                alpha, ret, mae = (_number(result.get(name)) for name in
                                   ("hs300_alpha", "signal_return", "mae"))
                if alpha is None:
                    missing["alpha_missing"] += 1; blocked.add(identity); continue
                row = {"recommendation_date": day,
                       "record_id": f"{evidence.get('record_id')}:{snapshot_hash}",
                       "source_record_id": evidence.get("record_id"),
                       "research_snapshot_sha256": snapshot_hash,
                       "market": item.get("market") or item.get("exchange") or identity[0],
                       "code": evidence.get("code"), "entry_date": result.get("entry_date"),
                       "exit_date": result.get("exit_date"), "market_sessions": item.get("market_sessions"),
                       "signal_return": ret, "hs300_alpha": alpha, "mae": mae,
                       "distance_bin": evidence.get("distance_bin"),
                       "signal_age": str(evidence.get("signal_age_bars")),
                       "sector_status": "actionable" if evidence.get("sector_actionable") is True else "not_actionable",
                       "formal_layer": ("formal" if evidence.get("formal_bucket") in FORMAL_BUCKETS
                                        else "observation"),
                       "market_band": evidence.get("market_regime_band") or "unknown"}
                if not _calendar_valid(row, window, cutoff):
                    missing["calendar_or_endpoint_invalid"] += 1; blocked.add(identity); continue
                rows.append(row)
        assigned = assign_research_events(rows, window)
        events = assigned["events"]
        dimensions = {}
        for field in ("distance_bin", "market_band", "signal_age", "sector_status", "formal_layer"):
            grouped = defaultdict(list)
            for row in events: grouped[str(row.get(field) or "unknown")].append(row)
            dimensions[field] = {name: _metrics(values) for name, values in sorted(grouped.items())}
        output[str(window)] = {
            "cohort_id": _cohort_id(contract_id, window, "descriptive", "lps", events),
            "event_count": len(events), "missing": dict(sorted(missing.items())),
            "metrics": _metrics(events), "groups": dimensions,
            "event_ids": [row["event_id"] for row in events],
            "formal_eligible_nonnegative_event_ids": [
                row["event_id"] for row in events
                if row.get("formal_layer") == "formal"
                and row.get("distance_bin") not in {"negative", "insufficient"}],
        }
    return output


def _common_formal_cohort(daily, index, conflict_keys, contract_id, window, cutoff):
    """Freeze event anchors once from the shared full formal universe."""
    rows, missing, blocked = [], defaultdict(int), set()
    for artifact in daily:
        day, snapshot_hash = artifact["recommendation_date"], artifact["research_snapshot_sha256"]
        evidence = {row.get("code"): row for row in artifact.get("records") or []}
        formal_codes = [code for bucket in FORMAL_BUCKETS
                        for code in artifact["baseline"]["buckets"].get(bucket, [])]
        for code in formal_codes:
            market = str((evidence.get(code) or {}).get("market") or "default").upper()
            identity = (market, code)
            if identity in blocked:
                missing["earlier_unresolved_anchor"] += 1; continue
            source_record_id = (artifact.get("record_ids") or {}).get(code)
            key = (source_record_id, snapshot_hash, contract_id, POPULATION_KIND)
            item = index.get(key)
            result = ((item or {}).get("windows") or {}).get(str(window)) or {}
            if key in conflict_keys:
                missing["outcome_conflict"] += 1; blocked.add(identity); continue
            problem = _outcome_problem(item, result)
            if problem:
                missing[problem] += 1; blocked.add(identity); continue
            if item.get("recommendation_date") != day:
                missing["outcome_invalid_identity"] += 1; blocked.add(identity); continue
            alpha, ret, mae = (_number(result.get(name)) for name in
                               ("hs300_alpha", "signal_return", "mae"))
            if alpha is None:
                missing["alpha_missing"] += 1; blocked.add(identity); continue
            compound_id = f"{source_record_id}:{snapshot_hash}"
            row = {"recommendation_date": day, "record_id": compound_id,
                   "source_record_id": source_record_id, "research_snapshot_sha256": snapshot_hash,
                   "market": item.get("market") or item.get("exchange") or market, "code": code,
                   "entry_date": result.get("entry_date"), "exit_date": result.get("exit_date"),
                   "market_sessions": item.get("market_sessions"), "signal_return": ret,
                   "hs300_alpha": alpha, "mae": mae}
            if not _calendar_valid(row, window, cutoff):
                missing["calendar_or_endpoint_invalid"] += 1; blocked.add(identity); continue
            rows.append(row)
    assigned = assign_research_events(rows, window)
    return {"events": {row["event_id"]: row for row in assigned["events"]},
            "record_to_event": assigned["record_to_event"],
            "missing": dict(sorted(missing.items())),
            "cohort_id": _cohort_id(contract_id, window, "common", "formal", assigned["events"])}


def evaluate_distance_days(daily_artifacts, outcome_items, cutoff,
                           contract_id=DEFAULT_CONTRACT_ID):
    cutoff = _day(cutoff)
    if cutoff is None: raise ValueError("invalid_evaluation_as_of")
    index, conflict_keys = _outcome_index(outcome_items, cutoff, contract_id)
    days = {}
    for artifact in daily_artifacts or []:
        if artifact.get("status") == "completed" and artifact.get("recommendation_date", "") <= cutoff \
                and (artifact.get("definition") or {}).get("evaluation_contract_id") == contract_id:
            days.setdefault(artifact["recommendation_date"], []).append(artifact)
    duplicates = sorted(day for day, rows in days.items() if len(rows) != 1)
    daily = [rows[0] for day, rows in sorted(days.items()) if len(rows) == 1]
    result = {"schema_version": SCHEMA_VERSION, "evaluation_as_of": cutoff,
              "evaluation_contract_id": contract_id, "population_kind": POPULATION_KIND,
              "daily_count": len(daily), "duplicate_daily_dates": duplicates,
              "duplicate_outcome_identities": len(conflict_keys), "windows": {},
              "status": "continue_accumulating", "upgrade_allowed": False,
              "decision_scope": "signal_research_only"}
    for window in WINDOWS:
        by_k = {}
        common = _common_formal_cohort(
            daily, index, conflict_keys, contract_id, window, cutoff)
        frozen_calendar = _merged_frozen_calendar(list(index.values()))
        for k in TOP_K:
            pairs, missing, all_rows = [], defaultdict(int), []
            affected_event_ids = set()
            affected_by_day = defaultdict(set)
            mature_opportunity_dates = set()
            maturity_unknown_dates = set()
            empty_dates = []
            missing.update(common["missing"])
            projected, seen = {"baseline": defaultdict(list), "treatment": defaultdict(list)}, {
                "baseline": set(), "treatment": set()}
            for artifact in daily:
                day, snapshot_hash = artifact["recommendation_date"], artifact["research_snapshot_sha256"]
                mapping = artifact.get("record_ids") or {}
                evidence = {row.get("code"): row for row in artifact.get("records") or []}
                baseline_codes = artifact["baseline"]["top_k"][str(k)]
                treatment_codes = artifact["treatment"]["top_k"][str(k)]
                if not baseline_codes and not treatment_codes:
                    empty_dates.append(day)
                    continue
                if frozen_calendar and day in frozen_calendar:
                    exit_index = frozen_calendar.index(day) + window
                    if exit_index < len(frozen_calendar) and frozen_calendar[exit_index] <= cutoff:
                        mature_opportunity_dates.add(day)
                else:
                    maturity_unknown_dates.add(day)
                changed_codes = set(artifact["baseline"]["top_k"][str(k)]) ^ set(
                    artifact["treatment"]["top_k"][str(k)])
                for code in changed_codes:
                    detail = evidence.get(code) or {}
                    if detail.get("eligible") and _number(detail.get("distance_atr")) is not None \
                            and detail["distance_atr"] >= 0:
                        event_id = common["record_to_event"].get(f"{mapping.get(code)}:{snapshot_hash}")
                        event = common["events"].get(event_id)
                        if event_id and event and event.get("record_id") == f"{mapping.get(code)}:{snapshot_hash}":
                            affected_by_day[day].add(event_id)
                for side in ("baseline", "treatment"):
                    pending_rows, pending_event_ids, unresolved = [], [], False
                    for code in artifact[side]["top_k"][str(k)]:
                        compound_id = f"{mapping.get(code)}:{snapshot_hash}"
                        event_id = common["record_to_event"].get(compound_id)
                        if event_id is None:
                            missing["not_in_common_mature_cohort"] += 1; unresolved = True; break
                        event = common["events"][event_id]
                        if event.get("record_id") != compound_id:
                            # A later overlapping signal is suppressed.  It may
                            # not borrow the earlier anchor's realized return.
                            continue
                        if event_id in seen[side]:
                            continue
                        pending_event_ids.append(event_id)
                        pending_rows.append(event)
                    if not unresolved:
                        seen[side].update(pending_event_ids)
                        projected[side][day].extend(pending_rows)
            by_day = projected
            for day in sorted(set(by_day["baseline"]) & set(by_day["treatment"])):
                base_m = _metrics(by_day["baseline"][day])
                trial_m = _metrics(by_day["treatment"][day])
                if base_m["mean_hs300_alpha"] is None or trial_m["mean_hs300_alpha"] is None:
                    missing["alpha_missing"] += 1; continue
                all_rows.extend(dict(row, side="baseline") for row in by_day["baseline"][day])
                all_rows.extend(dict(row, side="treatment") for row in by_day["treatment"][day])
                affected_event_ids.update(affected_by_day.get(day, set()))
                pairs.append({"date": day, "baseline_alpha": base_m["mean_hs300_alpha"],
                              "treatment_alpha": trial_m["mean_hs300_alpha"],
                              "alpha_delta": trial_m["mean_hs300_alpha"] - base_m["mean_hs300_alpha"],
                              "baseline_return": base_m["mean_return"], "treatment_return": trial_m["mean_return"]})
            baseline_rows = [row for row in all_rows if row["side"] == "baseline"]
            treatment_rows = [row for row in all_rows if row["side"] == "treatment"]
            opportunity_dates = len(daily) - len(empty_dates)
            by_k[str(k)] = {"paired_dates": len(pairs),
                            "frozen_date_count": len(daily),
                            "frozen_opportunity_dates": opportunity_dates,
                            "empty_recommendation_dates": empty_dates,
                            "mature_opportunity_dates": len(mature_opportunity_dates),
                            "maturity_unknown_dates": sorted(maturity_unknown_dates),
                            "coverage_rate": len(pairs) / opportunity_dates if opportunity_dates else None,
                            "all_frozen_date_coverage": len(pairs) / len(daily) if daily else None,
                            "mature_opportunity_coverage": (
                                len(pairs) / len(mature_opportunity_dates)
                                if mature_opportunity_dates else None),
                            "missing": dict(sorted(missing.items())), "date_pairs": pairs,
                            "mean_alpha_delta": sum(p["alpha_delta"] for p in pairs) / len(pairs) if pairs else None,
                            "alpha_delta_bootstrap_95": _block_bootstrap(pairs, frozen_calendar),
                            "affected_eligible_lps_event_ids": sorted(affected_event_ids),
                            "cohort_ids": {
                                "common": common["cohort_id"],
                                "baseline": _cohort_id(contract_id, window, k, "baseline", baseline_rows),
                                "treatment": _cohort_id(contract_id, window, k, "treatment", treatment_rows),
                            },
                            "metrics": {"baseline": _metrics(baseline_rows),
                                        "treatment": _metrics(treatment_rows)}}
        result["windows"][str(window)] = by_k
    result["descriptive_lps"] = _descriptive_groups(
        daily, index, conflict_keys, contract_id, cutoff)
    primary = result["windows"][str(PRIMARY_WINDOW)]["3"]
    mature_dates = primary["paired_dates"]
    formal_eligible_ids = set(result["descriptive_lps"][str(PRIMARY_WINDOW)]
                              ["formal_eligible_nonnegative_event_ids"])
    unique_events = len(formal_eligible_ids & set(primary["affected_eligible_lps_event_ids"]))
    coverage = primary["mature_opportunity_coverage"] or 0
    ci = primary.get("alpha_delta_bootstrap_95")
    ci_contract_ok = bool(ci and ci.get("preregistered_method_satisfied") is True)
    result["research_gate"] = {"mature_dates": mature_dates, "minimum_mature_dates": 20,
                               "unique_valid_alpha_events": unique_events, "minimum_unique_events": 100,
                               "paired_coverage": primary["mature_opportunity_coverage"],
                               "all_frozen_opportunity_coverage": primary["coverage_rate"],
                               "maturity_unknown_dates": primary["maturity_unknown_dates"],
                               "minimum_coverage": .90,
                               "paired_alpha_ci_available": ci is not None,
                               "preregistered_bootstrap_contract_satisfied": ci_contract_ok,
                               "signal_research_ready": mature_dates >= 20 and unique_events >= 100 and coverage >= .90 and ci_contract_ok and not primary["maturity_unknown_dates"],
                               "net_return_upgrade_blocked_until_trade_assessment": True}
    return result


def _atomic_new(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload: raise ValueError("lps_distance_artifact_conflict")
        return path
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-"); os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        try: os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload: raise ValueError("lps_distance_artifact_conflict")
    finally:
        try: os.unlink(temporary)
        except OSError: pass
    return path


def save_artifact(artifact, root=None):
    root = Path(root) if root is not None else EVOLUTION_ROOT / "shadow" / "lps_distance"
    digest = artifact.get("input_sha256") or content_sha256(artifact)
    day = artifact.get("evaluation_as_of") or artifact.get("recommendation_date") or "unknown"
    path = root / str(day) / f"{digest}.json"
    _atomic_new(path, canonical_json(artifact) + b"\n")
    return str(path)


def run(as_of, contract_id=DEFAULT_CONTRACT_ID, state_root=EVOLUTION_ROOT,
        official_root=OFFICIAL_ROOT, outcome_root=None, save=False):
    cutoff = _day(as_of)
    if cutoff is None: return {"status": "failed", "reason": "invalid_evaluation_as_of"}
    research = load_primary_research_snapshots(Path(state_root) / "research", as_of=cutoff)
    daily = []
    for snapshot in research:
        day = (snapshot.get("content") or {}).get("recommendation_date")
        path = Path(official_root) / f"{day}.json"
        if not path.is_file():
            daily.append({"status": "skipped", "reason": "official_snapshot_missing",
                          "recommendation_date": day}); continue
        daily.append(run_daily_distance(snapshot, load_official_snapshot(path), contract_id))
    outcome_root = Path(outcome_root) if outcome_root is not None else storage_root("evaluations")
    outcomes = load_candidate_signal_items(outcome_root, contract_id=contract_id, as_of=cutoff)
    evaluation = evaluate_distance_days(daily, outcomes, cutoff, contract_id)
    artifact = {"schema_version": SCHEMA_VERSION, "evaluation_as_of": cutoff,
                "definition": definition(contract_id), "daily": daily, "evaluation": evaluation,
                "historical_or_prospective": "historical_backfill_and_forward_accumulation",
                "prospective_evidence_starts_on": DEFINITION_FROZEN_ON,
                "formal_policy_changed": False}
    artifact["input_sha256"] = content_sha256({"definition": artifact["definition"],
                                                "daily": daily, "outcomes": outcomes,
                                                "evaluation_as_of": cutoff})
    if save: artifact["path"] = save_artifact(
        artifact, Path(state_root) / "shadow" / "lps_distance")
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--contract-id", default=DEFAULT_CONTRACT_ID)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    result = run(args.as_of, args.contract_id, save=args.save)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        gate = (result.get("evaluation") or {}).get("research_gate") or {}
        print(f"LPS distance shadow: {result.get('evaluation_as_of')} status={result.get('evaluation', {}).get('status')}")
        print(f"Top3/20d paired dates={gate.get('mature_dates', 0)}, events={gate.get('unique_valid_alpha_events', 0)}, coverage={gate.get('paired_coverage')}")
        print("Decision: continue_accumulating; signal research only; formal policy unchanged.")
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
