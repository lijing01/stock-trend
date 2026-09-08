"""Walk-forward, paired shadow evaluation for frozen candidate experiments."""
import argparse
import copy
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path: sys.path.insert(0, str(SCRIPT_ROOT))

from analysis.recommendation_diagnostics import load_candidate_signal_items, load_primary_research_snapshots
from core.recommendation_snapshot import canonical_json, content_sha256
from core.evolution_registry import validate_experiment_definition
from core.evolution_storage import input_manifest, storage_root
from core.research_events import assign_research_events, summarize_daily_alpha
from scans.daily_candidates import classify_candidates, select_candidate_pool

SCHEMA_VERSION = "recommendation-experiment/v2"
DEFAULT_ROOT = storage_root("experiments")
PRIMARY_WINDOW = 20
CONFIRMATION_WINDOW = 60
PURGE_SESSIONS = CONFIRMATION_WINDOW
VALIDATION_BLOCK_NAMES = ("validation_1", "validation_2", "validation_3")
FROZEN_BASELINE = {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}
ZERO_TREATMENT = {"strict_level_1": 0, "strict_level_2": 0, "strict_level_3": 0}


def default_experiment():
    return {"kind": "buy_point_priority_bonus", "baseline": FROZEN_BASELINE,
            "treatment": ZERO_TREATMENT, "changes": ["within_bucket_ranking"],
            "schema_version": "recommendation-experiment-definition/v2",
            "primary_window": PRIMARY_WINDOW,
            "confirmation_window": CONFIRMATION_WINDOW,
            "purge_sessions": PURGE_SESSIONS,
            "bootstrap": {"method": "moving_trading_session_block_bootstrap",
                          "block_length": PRIMARY_WINDOW, "seed": 20260907,
                          "draws": 2000, "confidence": 0.95,
                          "cross_partition_blocks": False}}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value); return value if math.isfinite(value) else None
    except (TypeError, ValueError): return None


def _normalise_day(value):
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _normalise_sessions(values):
    result = []
    for value in values or []:
        day = _normalise_day(value)
        if day is None:
            return None
        result.append(day)
    return sorted(set(result))


def _market_from_value(value, fallback="default"):
    text = str(value or "").strip()
    return text.upper() if text else fallback


def _market_from_item(item):
    if not isinstance(item, dict):
        return "default"
    market = item.get("market") or item.get("exchange")
    if market:
        return _market_from_value(market)
    ts_code = str(item.get("ts_code") or "")
    if "." in ts_code:
        return _market_from_value(ts_code.rsplit(".", 1)[-1])
    return "default"


def _outcomes(items):
    result = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        windows = item.get("windows") or {}
        value = {"windows": copy.deepcopy(windows)}
        # Keep the primary window fields at the top level for old callers and
        # fixtures while retaining the independent 60-day confirmation data.
        value.update(copy.deepcopy(windows.get(str(PRIMARY_WINDOW), {})))
        value["_market_sessions"] = item.get("market_sessions")
        value["_record_id"] = item.get("record_id")
        key = (str(item.get("recommendation_date") or ""),
               _market_from_item(item), str(item.get("code") or ""))
        if key in result and canonical_json(result[key]) != canonical_json(value):
            result[key] = {"_duplicate_outcome_conflict": True}
            continue
        result[key] = value
    return result


def _eligible(record):
    candidate = record.get("candidate") or {}
    quality = candidate.get("data_quality") or {}
    return (quality.get("eligible") is True and candidate.get("sector_actionable", True)
            and candidate.get("score_eligible", True)
            and not candidate.get("research_terminal_status"))


def _empty_buckets():
    return {name: [] for name in (
        "actionable", "waiting_trigger", "next_day_confirmation",
        "observation", "data_rejected",
    )}


def _frozen_replay_candidate(record, min_score=None):
    """Validate and detach one research row before production replay.

    Research snapshots are input archives, not a second implementation of
    candidate ranking.  The preselection envelope records the values needed by
    ``select_candidate_pool``; an unknown value makes the date ineligible for
    replay instead of silently dropping the row.
    """
    if not isinstance(record, dict):
        return None, "replay_input_missing_record"
    candidate = record.get("candidate")
    preselection = record.get("preselection")
    if not isinstance(candidate, dict) or not candidate.get("code"):
        return None, "replay_input_missing_candidate"
    if not isinstance(preselection, dict):
        return None, "replay_input_missing_preselection"
    required = (
        "composite_score", "quality_adjusted_score", "data_quality_eligible",
        "sector_actionable", "score_eligible", "qualification_status",
    )
    if any(key not in preselection for key in required):
        return None, "replay_input_missing_preselection"
    if preselection.get("qualification_status") not in ("eligible", "ineligible"):
        return None, "replay_input_qualification_unknown"
    if any(not isinstance(preselection.get(key), bool)
           for key in ("data_quality_eligible", "sector_actionable", "score_eligible")):
        return None, "replay_input_qualification_unknown"
    composite = _number(preselection.get("composite_score"))
    quality = _number(preselection.get("quality_adjusted_score"))
    if composite is None or quality is None:
        return None, "replay_input_damaged_score"
    frozen_min_score = _number(preselection.get("min_score"))
    if min_score is not None:
        if frozen_min_score is None:
            return None, "replay_input_missing_min_score"
        if abs(frozen_min_score - float(min_score)) > 1e-9:
            return None, "replay_input_parameter_mismatch"
    frozen = copy.deepcopy(candidate)
    frozen_code = str(frozen.get("code") or "")
    if frozen_code != str(record.get("code") or frozen_code):
        return None, "replay_input_identity_mismatch"
    # These are the exact hard-gate inputs used by the production selector.
    frozen["composite_score"] = composite
    frozen["quality_adjusted_score"] = quality
    frozen["sector_actionable"] = preselection["sector_actionable"]
    frozen["score_eligible"] = preselection["score_eligible"]
    quality_payload = frozen.get("data_quality")
    if not isinstance(quality_payload, dict):
        quality_payload = {}
        frozen["data_quality"] = quality_payload
    existing_quality = quality_payload.get("eligible")
    if existing_quality is not None and existing_quality is not preselection["data_quality_eligible"]:
        return None, "replay_input_qualification_conflict"
    for field in ("sector_actionable", "score_eligible"):
        existing = frozen.get(field)
        if existing is not None and existing is not preselection[field]:
            return None, "replay_input_qualification_conflict"
    expected_qualification = (
        "eligible" if all(preselection[field] for field in (
            "data_quality_eligible", "sector_actionable", "score_eligible"))
        else "ineligible"
    )
    if preselection["qualification_status"] != expected_qualification:
        return None, "replay_input_qualification_conflict"
    quality_payload["eligible"] = preselection["data_quality_eligible"]
    # Keep a stable link for diagnostics without exposing it to the selector.
    frozen["_research_record_id"] = str(record.get("record_id") or "")
    frozen["_research_market"] = _market_from_item(record)
    return frozen, None


def _replay_selection(records, bonuses, limit, min_score=50, policy=None):
    """Replay the production selector and classifier on frozen research rows."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return {"status": "rejected", "selected": [], "buckets": _empty_buckets(),
                "reasons": ["replay_input_missing_top"]}
    if _number(min_score) is None:
        return {"status": "rejected", "selected": [], "buckets": _empty_buckets(),
                "reasons": ["replay_input_missing_min_score"]}
    if not isinstance(bonuses, dict):
        return {"status": "rejected", "selected": [], "buckets": _empty_buckets(),
                "reasons": ["replay_input_missing_priority_bonuses"]}
    frozen, reasons, seen = [], [], set()
    for record in records or []:
        candidate, reason = _frozen_replay_candidate(record, min_score=min_score)
        if reason:
            reasons.append(reason)
            continue
        code = str(candidate.get("code"))
        if code in seen:
            reasons.append("replay_input_duplicate_code")
            continue
        seen.add(code)
        frozen.append(candidate)
    if reasons:
        return {"status": "rejected", "selected": [], "buckets": _empty_buckets(),
                "reasons": sorted(set(reasons))}
    frozen_policy = copy.deepcopy(policy) if isinstance(policy, dict) else {}
    selected = select_candidate_pool(
        frozen, limit, float(min_score), policy=frozen_policy,
        priority_bonuses=copy.deepcopy(bonuses),
    )
    buckets = classify_candidates(selected, frozen_policy)
    return {"status": "ok", "selected": selected, "buckets": buckets,
            "reasons": [], "input_count": len(frozen)}


def _replay_date(records, bonuses, limit, min_score=50, policy=None):
    """Compatibility wrapper returning only the production-selected rows."""
    result = _replay_selection(records, bonuses, limit, min_score, policy)
    return result["selected"] if result["status"] == "ok" else []


def _bootstrap(deltas, seed=20260907, draws=2000, block_length=PRIMARY_WINDOW,
               dates=None, partitions=None, trading_sessions=None):
    """Estimate a date-level effect with moving trading-session blocks.

    ``deltas`` is ordered by trading date.  A block is valid only when its
    indices are adjacent in the supplied date order and belong to one
    partition.  Keeping the sampled indices in the result makes the sampling
    contract auditable and allows deterministic replay from ``seed``.
    """
    values = []
    for value in deltas or []:
        number = _number(value)
        if number is None:
            return {"method": "moving_trading_session_block_bootstrap",
                    "status": "invalid_input", "seed": seed,
                    "draws": draws, "block_length": block_length,
                    "valid_block_count": 0, "valid_blocks": [],
                    "sample_indices": [], "lower_95": None,
                    "upper_95": None}
        values.append(number)
    if not values:
        return None
    if isinstance(block_length, bool) or not isinstance(block_length, int) \
            or block_length < 1:
        return {"method": "moving_trading_session_block_bootstrap",
                "status": "invalid_input", "seed": seed, "draws": draws,
                "block_length": block_length, "valid_block_count": 0,
                "valid_blocks": [], "sample_indices": [],
                "lower_95": None, "upper_95": None}
    ordered_dates = [_normalise_day(value) for value in (dates or [])]
    if dates is not None and (len(ordered_dates) != len(values)
                              or any(day is None for day in ordered_dates)):
        return {"method": "moving_trading_session_block_bootstrap",
                "status": "invalid_input", "seed": seed, "draws": draws,
                "block_length": block_length, "valid_block_count": 0,
                "valid_blocks": [], "sample_indices": [],
                "lower_95": None, "upper_95": None}
    if dates is None:
        ordered_dates = [str(index) for index in range(len(values))]
    calendar = _normalise_sessions(trading_sessions or [])
    calendar_positions = {day: index for index, day in enumerate(calendar)}

    partition_by_day = {}
    if isinstance(partitions, dict):
        for name, raw in partitions.items():
            if isinstance(raw, dict):
                raw = raw.get("dates") or raw.get("sessions") or []
            for value in raw or []:
                day = _normalise_day(value)
                if day is not None:
                    partition_by_day[day] = str(name)
    valid_blocks = []
    for start in range(0, len(values) - block_length + 1):
        block = list(range(start, start + block_length))
        labels = {partition_by_day.get(ordered_dates[index], "default")
                  for index in block}
        adjacent = True
        if calendar_positions:
            positions = [calendar_positions.get(ordered_dates[index]) for index in block]
            adjacent = (all(position is not None for position in positions)
                        and positions == list(range(positions[0], positions[0] + block_length)))
        if len(labels) == 1 and adjacent:
            valid_blocks.append(block)
    contract = {"method": "moving_trading_session_block_bootstrap",
                "status": "ready" if len(valid_blocks) >= 2 else "insufficient_data",
                "seed": seed, "draws": draws, "block_length": block_length,
                "confidence": 0.95, "cross_partition_blocks": False,
                "valid_block_count": len(valid_blocks),
                "valid_blocks": valid_blocks, "sample_indices": [],
                "lower_95": None, "upper_95": None}
    if len(valid_blocks) < 2:
        return contract
    try:
        draw_count = int(draws)
    except (TypeError, ValueError):
        draw_count = 0
    if draw_count < 1:
        contract["status"] = "invalid_input"
        return contract
    rng = random.Random(seed)
    means = []
    target_size = len(values)
    blocks_needed = (target_size + block_length - 1) // block_length
    for _ in range(draw_count):
        indices = []
        for _block in range(blocks_needed):
            indices.extend(rng.choice(valid_blocks))
        indices = indices[:target_size]
        contract["sample_indices"].append(indices)
        means.append(sum(values[index] for index in indices) / target_size)
    means.sort()
    contract["lower_95"] = means[int(.025 * (draw_count - 1))]
    contract["upper_95"] = means[int(.975 * (draw_count - 1))]
    return contract


def _percentile(values, q):
    values = sorted(values)
    if not values: return None
    return values[max(0, min(len(values) - 1, int(q * (len(values) - 1))))]


def _split_dates(dates, max_window=PURGE_SESSIONS, sessions=None):
    """Legacy fallback splitter used only for diagnostics and old fixtures."""
    ordered = sorted(set(sessions or dates)); gap = int(max_window)
    usable = len(ordered) - 2 * gap
    if usable < 3: return None
    step = usable // 3
    if step < 1: return None
    blocks = []
    start = 0
    for index in range(3):
        end = start + step if index < 2 else start + (usable - step * 2)
        blocks.append(ordered[start:end]); start = end + gap
    return blocks


def _partition_interval(raw, sessions, name):
    """Resolve one explicit partition interval to registered trading sessions."""
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            return None, f"partition_{name}_invalid_interval"
        start, end = raw
        explicit_dates = None
    elif isinstance(raw, dict):
        explicit_dates = raw.get("dates") or raw.get("sessions")
        start, end = raw.get("start"), raw.get("end")
    else:
        return None, f"partition_{name}_invalid_interval"
    if explicit_dates is not None:
        values = _normalise_sessions(explicit_dates)
        if not values:
            return None, f"partition_{name}_empty"
        if sessions:
            allowed = set(sessions)
            if any(value not in allowed for value in values):
                return None, f"partition_{name}_session_not_registered"
            positions = {day: index for index, day in enumerate(sessions)}
            indexes = [positions[value] for value in values]
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                return None, f"partition_{name}_non_contiguous"
        return values, None
    start, end = _normalise_day(start), _normalise_day(end)
    if start is None or end is None or start > end:
        return None, f"partition_{name}_invalid_interval"
    values = [day for day in sessions if start <= day <= end]
    if not values:
        return None, f"partition_{name}_empty"
    return values, None


def _resolve_time_partitions(definition, dates, sessions):
    """Validate frozen discovery/validation/holdout partitions.

    The caller must provide explicit partition boundaries.  ``dates`` are the
    available frozen recommendation dates; ``sessions`` is the trading
    calendar used to measure purge and label windows.
    """
    raw = (definition or {}).get("partitions")
    if not isinstance(raw, dict):
        return {"status": "invalid", "reason": "experiment_partitions_required"}
    required = ("discovery", *VALIDATION_BLOCK_NAMES, "final_holdout")
    # Accept a compact ``validation`` list while normalising it to named
    # blocks in the result and in the content hash.
    validation = raw.get("validation")
    if validation is not None:
        if not isinstance(validation, (list, tuple)) or len(validation) != 3:
            return {"status": "invalid", "reason": "partition_validation_blocks_required"}
        raw = dict(raw)
        for name, value in zip(VALIDATION_BLOCK_NAMES, validation):
            raw.setdefault(name, value)
    if any(name not in raw for name in required):
        return {"status": "invalid", "reason": "partition_definition_incomplete"}
    sessions = _normalise_sessions(sessions)
    if not sessions:
        return {"status": "invalid", "reason": "trading_sessions_required"}
    resolved = {}
    for name in required:
        values, reason = _partition_interval(raw[name], sessions, name)
        if reason:
            return {"status": "invalid", "reason": reason}
        resolved[name] = values
    ordered = ["discovery", *VALIDATION_BLOCK_NAMES, "final_holdout"]
    positions = {day: index for index, day in enumerate(sessions)}
    previous_end = None
    purge = int((definition or {}).get("purge_sessions") or PURGE_SESSIONS)
    if purge < CONFIRMATION_WINDOW:
        return {"status": "invalid", "reason": "purge_window_below_confirmation_window"}
    for name in ordered:
        values = resolved[name]
        first, last = positions[values[0]], positions[values[-1]]
        if previous_end is not None and first - previous_end - 1 < purge:
            return {"status": "invalid", "reason": "partition_purge_gap_insufficient"}
        previous_end = last
    freeze_at = _normalise_day((definition or {}).get("freeze_at")
                               or (definition or {}).get("frozen_at"))
    holdout_start = resolved["final_holdout"][0]
    if freeze_at is None or freeze_at >= holdout_start:
        return {"status": "invalid", "reason": "holdout_freeze_timestamp_required"}
    return {
        "status": "valid", "calendar_verified": True,
        "purge_sessions": purge, "freeze_at": freeze_at,
        "discovery": resolved["discovery"],
        "validation": {name: resolved[name] for name in VALIDATION_BLOCK_NAMES},
        "final_holdout": resolved["final_holdout"],
        "all_dates": sorted(set(day for values in resolved.values() for day in values)),
    }


def _calendar_from_inputs(research_snapshots, candidate_signal_items, definition):
    """Collect an explicit trading-session calendar from frozen inputs."""
    candidates = []
    definition = definition or {}
    candidates.extend(definition.get("trading_sessions") or [])
    calendar = definition.get("calendar") or {}
    if isinstance(calendar, dict):
        candidates.extend(calendar.get("sessions") or calendar.get("trading_sessions") or [])
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        candidates.extend(content.get("trading_sessions") or content.get("market_sessions") or [])
        for record in content.get("records") or []:
            if isinstance(record, dict):
                candidates.extend(record.get("market_sessions") or [])
    for item in candidate_signal_items or []:
        if isinstance(item, dict):
            candidates.extend(item.get("market_sessions") or [])
    sessions = _normalise_sessions(candidates)
    return sessions or []


def _outcome_window(outcome, window):
    if not isinstance(outcome, dict):
        return {}
    windows = outcome.get("windows")
    if isinstance(windows, dict):
        return copy.deepcopy(windows.get(str(window)) or {})
    return copy.deepcopy(outcome)


def _research_record_date(content, record):
    return _normalise_day(content.get("recommendation_date") or record.get("basis_date"))


def _metrics_for_rows(rows, day, outcomes, block_end, window, side, skipped,
                      trading_sessions=None):
    values, maes, missing, events, alpha_rows = [], [], [], [], []
    for row in rows:
        code = str(row.get("code") or "")
        market = _market_from_value(row.get("_research_market") or row.get("market"))
        outcome = outcomes.get((day, market, code)) or outcomes.get(
            (day, "default", code)) or {}
        if outcome.get("_duplicate_outcome_conflict"):
            missing.append({"code": code, "reason": "duplicate_outcome_conflict", "side": side})
            continue
        label = _outcome_window(outcome, window)
        entry_date = _normalise_day(label.get("entry_date"))
        exit_date = _normalise_day(label.get("exit_date") or label.get("mark_date"))
        if not entry_date:
            missing.append({"code": code, "reason": "missing_label_start", "side": side})
            continue
        if not exit_date:
            missing.append({"code": code, "reason": "missing_label_end", "side": side})
            continue
        raw_calendar = outcome.get("_market_sessions") or trading_sessions or []
        if isinstance(raw_calendar, dict):
            raw_calendar = raw_calendar.get(market) or raw_calendar.get("*") or []
        calendar = _normalise_sessions(raw_calendar)
        if raw_calendar and calendar is None:
            missing.append({"code": code, "reason": "invalid_market_calendar", "side": side})
            continue
        if calendar:
            positions = {session: index for index, session in enumerate(calendar)}
            if (entry_date not in positions or exit_date not in positions
                    or positions[exit_date] - positions[entry_date] != window - 1):
                missing.append({"code": code, "reason": "label_endpoint_mismatch", "side": side})
                continue
        if block_end and exit_date > block_end:
            skipped.append("label_crosses_oos_boundary")
            missing.append({"code": code, "reason": "label_crosses_oos_boundary", "side": side})
            continue
        alpha = _number(label.get("hs300_alpha"))
        mae = _number(label.get("mae"))
        if label.get("status") != "complete" or alpha is None:
            missing.append({"code": code, "reason": label.get("status") or "missing_alpha", "side": side})
            continue
        values.append(alpha)
        alpha_rows.append({"recommendation_date": day, "code": code,
                           "hs300_alpha": alpha, "evaluation_status": "complete"})
        if mae is not None:
            maes.append(mae)
        if entry_date and exit_date:
            events.append({
                "record_id": outcome.get("_record_id") or f"{day}:default:{code}",
                "market": outcome.get("market") or market, "code": code,
                "entry_date": entry_date, "exit_date": exit_date,
                "recommendation_date": day,
                "market_sessions": outcome.get("_market_sessions"),
            })
    return {
        "mean": sum(values) / len(values) if values else None,
        "maes": maes, "missing": missing, "events": events,
        "alpha_rows": alpha_rows, "complete": not missing and bool(rows),
    }


def run_walk_forward(research_snapshots, candidate_signal_items, definition=None, top=None):
    definition = validate_experiment_definition(definition or default_experiment())
    by_date, metadata, skipped = defaultdict(list), {}, []
    invalid_dates = set()
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        if content.get("snapshot_type") != "formal" or (content.get("official_snapshot") or {}).get("link_status") != "linked":
            continue
        day = _normalise_day(content.get("recommendation_date"))
        if day is None:
            skipped.append("replay_input_invalid_recommendation_date")
            continue
        parameters = content.get("parameter_summary") or {}
        limit = parameters.get("top") if top is None else top
        min_score = parameters.get("min_score", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            skipped.append("replay_input_missing_top")
            continue
        if _number(min_score) is None:
            skipped.append("replay_input_missing_min_score")
            continue
        policy = content.get("policy")
        if not isinstance(policy, dict):
            policy = {"mode": "actionable", "max_recommendations": limit}
        metadata[day] = {"top": limit, "min_score": float(min_score),
                         "policy": copy.deepcopy(policy)}
        for record in content.get("records") or []:
            if not isinstance(record, dict):
                skipped.append("replay_input_missing_record")
                invalid_dates.add(day)
                continue
            if _research_record_date(content, record) != day:
                skipped.append("replay_input_date_mismatch")
                invalid_dates.add(day)
                continue
            by_date[day].append(record)
    outcomes = _outcomes(candidate_signal_items)
    dates = sorted(day for day in by_date if day)
    sessions = _calendar_from_inputs(research_snapshots, candidate_signal_items, definition)
    # A real experiment must carry an explicit calendar.  Keep a date-ordered
    # fallback only for audit output; it can never satisfy a promotion gate.
    calendar_verified = bool(sessions)
    if not sessions:
        sessions = dates
        skipped.append("trading_sessions_required")
    partition_result = _resolve_time_partitions(definition, dates, sessions)
    content = {"schema_version": SCHEMA_VERSION, "definition": definition,
               "primary_window": PRIMARY_WINDOW, "confirmation_window": CONFIRMATION_WINDOW,
               "purge_sessions": PURGE_SESSIONS, "calendar_verified": calendar_verified,
               "input_dates": dates, "skipped_reasons": sorted(set(skipped)),
               "input_manifest": input_manifest(
                   research_run_ids=sorted(str((item or {}).get("run_id") or "") for item in research_snapshots or []),
                   research_content_sha256=sorted(str((item or {}).get("content_sha256") or "") for item in research_snapshots or []),
                   candidate_signal_items=candidate_signal_items or [],
                   definition=definition, primary_window=PRIMARY_WINDOW,
                   confirmation_window=CONFIRMATION_WINDOW,
                   trading_sessions=sessions,
               )}
    if partition_result.get("status") != "valid":
        reason = partition_result.get("reason", "partition_definition_invalid")
        if not dates:
            reason += ":insufficient_time_span_for_three_purged_oos_blocks"
        content.update({"status": "continue_accumulating",
                        "reason": reason, "partition_status": partition_result,
                        "promotion": {"eligible": False, "gates": {
                            "calendar_verified": calendar_verified,
                            "partition_contract": False},
                            "reason": "frozen_p3_promotion_gates"}})
        return _package(content)
    blocks = []
    for name in VALIDATION_BLOCK_NAMES:
        available = [day for day in partition_result["validation"][name] if day in by_date]
        blocks.append(available)
    if any(len(block) < 1 for block in blocks):
        content.update({"status": "continue_accumulating",
                        "reason": "validation_block_insufficient_data",
                        "partitions": partition_result, "oos_blocks": blocks,
                        "promotion": {"eligible": False, "gates": {
                            "calendar_verified": calendar_verified,
                            "partition_contract": True,
                            "three_oos_blocks": False},
                            "reason": "frozen_p3_promotion_gates"}})
        return _package(content)

    paired, coverage, baseline_maes, treatment_maes = [], [], [], []
    baseline_events, treatment_events = [], []
    confirmation_pairs, confirmation_baseline_events, confirmation_treatment_events = [], [], []
    baseline_daily_rows, treatment_daily_rows = [], []
    replay_rejections = []
    partition_order = [*VALIDATION_BLOCK_NAMES, "final_holdout"]
    session_positions = {day: index for index, day in enumerate(sessions)}
    label_limits = {}
    for index, name in enumerate(VALIDATION_BLOCK_NAMES):
        next_name = partition_order[index + 1]
        next_values = (partition_result["final_holdout"] if next_name == "final_holdout"
                       else partition_result["validation"][next_name])
        label_limits[name] = sessions[max(0, session_positions[next_values[0]] - 1)]
    for block_no, block in enumerate(blocks, 1):
        for day in block:
            if day in invalid_dates:
                reason = "replay_input_invalid_date_records"
                replay_rejections.append({"date": day, "baseline": [reason],
                                          "treatment": [reason]})
                coverage.append({"date": day, "block": block_no,
                                 "baseline_count": 0, "treatment_count": 0,
                                 "status": "replay_input_rejected"})
                continue
            meta = metadata[day]
            baseline_replay = _replay_selection(
                by_date[day], definition["baseline"], meta["top"],
                meta["min_score"], meta["policy"])
            treatment_replay = _replay_selection(
                by_date[day], definition["treatment"], meta["top"],
                meta["min_score"], meta["policy"])
            if baseline_replay["status"] != "ok" or treatment_replay["status"] != "ok":
                replay_rejections.append({"date": day,
                    "baseline": baseline_replay["reasons"],
                    "treatment": treatment_replay["reasons"]})
                coverage.append({"date": day, "block": block_no,
                                 "baseline_count": 0, "treatment_count": 0,
                                 "status": "replay_input_rejected"})
                continue
            base = baseline_replay["selected"]
            trial = treatment_replay["selected"]
            label_limit = label_limits[VALIDATION_BLOCK_NAMES[block_no - 1]]
            old = _metrics_for_rows(base, day, outcomes, label_limit, PRIMARY_WINDOW,
                                    "baseline", skipped, sessions)
            new = _metrics_for_rows(trial, day, outcomes, label_limit, PRIMARY_WINDOW,
                                    "treatment", skipped, sessions)
            baseline_maes.extend(old["maes"]); treatment_maes.extend(new["maes"])
            baseline_events.extend(old["events"]); treatment_events.extend(new["events"])
            baseline_daily_rows.extend(old["alpha_rows"]); treatment_daily_rows.extend(new["alpha_rows"])
            old60 = _metrics_for_rows(base, day, outcomes, label_limit, CONFIRMATION_WINDOW,
                                      "baseline_60d", skipped, sessions)
            new60 = _metrics_for_rows(trial, day, outcomes, label_limit, CONFIRMATION_WINDOW,
                                      "treatment_60d", skipped, sessions)
            confirmation_baseline_events.extend(old60["events"])
            confirmation_treatment_events.extend(new60["events"])
            if old["complete"] and new["complete"] and old["mean"] is not None and new["mean"] is not None:
                paired.append({"date": day, "block": block_no,
                               "delta": new["mean"] - old["mean"]})
            confirmation_entry = {"date": day, "baseline_complete": old60["complete"],
                                  "treatment_complete": new60["complete"],
                                  "baseline_missing": old60["missing"],
                                  "treatment_missing": new60["missing"]}
            if old60["complete"] and new60["complete"] and old60["mean"] is not None and new60["mean"] is not None:
                confirmation_entry["delta"] = new60["mean"] - old60["mean"]
                confirmation_pairs.append(confirmation_entry)
            coverage.append({"date": day, "block": block_no,
                             "baseline_count": len(base), "treatment_count": len(trial),
                             "baseline_complete": old["complete"],
                             "treatment_complete": new["complete"],
                             "baseline_missing": old["missing"],
                             "treatment_missing": new["missing"],
                             "status": "paired" if old["complete"] and new["complete"] else "incomplete"})
    paired_dates = [row["date"] for row in paired]
    deltas = [row["delta"] for row in paired]
    validation_partition_dates = {
        name: [day for day in partition_result["validation"][name] if day in by_date]
        for name in VALIDATION_BLOCK_NAMES}
    interval = _bootstrap(
        deltas,
        seed=(definition.get("bootstrap") or {}).get("seed", 20260907),
        draws=(definition.get("bootstrap") or {}).get("draws", 2000),
        block_length=(definition.get("bootstrap") or {}).get("block_length", PRIMARY_WINDOW),
        dates=paired_dates,
        partitions=validation_partition_dates,
        trading_sessions=sessions,
    )
    baseline_assigned = assign_research_events(
        baseline_events, PRIMARY_WINDOW, market_sessions=sessions)
    treatment_assigned = assign_research_events(
        treatment_events, PRIMARY_WINDOW, market_sessions=sessions)
    baseline_60_assigned = assign_research_events(
        confirmation_baseline_events, CONFIRMATION_WINDOW,
        market_sessions=sessions)
    treatment_60_assigned = assign_research_events(
        confirmation_treatment_events, CONFIRMATION_WINDOW,
        market_sessions=sessions)
    baseline_daily = summarize_daily_alpha(baseline_daily_rows)
    treatment_daily = summarize_daily_alpha(treatment_daily_rows)
    confirmation_deltas = [row["delta"] for row in confirmation_pairs]
    confirmation_mean = (sum(confirmation_deltas) / len(confirmation_deltas)
                         if confirmation_deltas else None)
    baseline_coverage = sum(row["baseline_complete"] for row in coverage)
    treatment_coverage = sum(row["treatment_complete"] for row in coverage)
    block_pair_counts = {
        str(block_no): sum(
            row["block"] == block_no and row["status"] == "paired"
            for row in coverage
        )
        for block_no in range(1, len(blocks) + 1)
    }
    baseline_expected_days = sum(row["baseline_count"] > 0 for row in coverage)
    treatment_expected_days = sum(row["treatment_count"] > 0 for row in coverage)
    baseline_complete_ratio = (baseline_coverage / baseline_expected_days
                               if baseline_expected_days else 0.0)
    treatment_complete_ratio = (treatment_coverage / treatment_expected_days
                                if treatment_expected_days else 0.0)
    baseline_tail = _percentile(baseline_maes, .05)
    treatment_tail = _percentile(treatment_maes, .05)
    maturity = {
        "baseline": {"valid_alpha_events": len(baseline_assigned["events"]),
                      "alpha_mature_dates": baseline_daily["mature_dates"],
                      "alpha_mean": baseline_daily["mean_alpha"],
                      "alpha_missing_records": baseline_daily["missing_records"]},
        "treatment": {"valid_alpha_events": len(treatment_assigned["events"]),
                       "alpha_mature_dates": treatment_daily["mature_dates"],
                       "alpha_mean": treatment_daily["mean_alpha"],
                       "alpha_missing_records": treatment_daily["missing_records"]},
        "valid_alpha_events": min(len(baseline_assigned["events"]), len(treatment_assigned["events"])),
        "alpha_mature_dates": len(paired_dates),
        "confirmation_60d": {
            "baseline_valid_alpha_events": len(baseline_60_assigned["events"]),
            "treatment_valid_alpha_events": len(treatment_60_assigned["events"]),
            "complete_paired_dates": len(confirmation_pairs),
            "mean_delta": confirmation_mean,
        },
    }
    gates = {
        "calendar_verified": calendar_verified,
        "partition_contract": True,
        "minimum_events": maturity["valid_alpha_events"] >= 100,
        "minimum_events_baseline": len(baseline_assigned["events"]) >= 100,
        "minimum_events_treatment": len(treatment_assigned["events"]) >= 100,
        "minimum_dates": len(paired_dates) >= 20,
        "minimum_dates_per_block": all(count >= 20 for count in block_pair_counts.values()),
        "three_oos_blocks": len(blocks) == 3,
        "coverage": treatment_coverage >= .9 * baseline_coverage if baseline_coverage else False,
        "pair_completeness_95pct": (
            baseline_complete_ratio >= .95 and treatment_complete_ratio >= .95
        ),
        "ci_lower_positive": bool(interval and interval.get("lower_95") is not None
                                   and interval.get("lower_95") > 0),
        "mae_tail": bool(baseline_tail is not None and treatment_tail is not None
                          and treatment_tail >= baseline_tail - .01),
        "confirmation_60d": (
            len(baseline_60_assigned["events"]) >= 100
            and len(treatment_60_assigned["events"]) >= 100
            and len(confirmation_pairs) >= 20
            and confirmation_mean is not None and confirmation_mean >= 0
        ),
    }
    primary_ready = (gates["calendar_verified"] and gates["minimum_events"] and gates["minimum_dates"]
                     and gates["minimum_dates_per_block"]
                     and gates["three_oos_blocks"])
    status = "validated" if primary_ready else "continue_accumulating"
    content.update({
        "status": status, "partitions": partition_result,
        "oos_blocks": blocks, "validation_partition_dates": validation_partition_dates,
        "skipped_reasons": sorted(set(skipped)),
        "paired_dates": paired, "confirmation_60d_pairs": confirmation_pairs,
        "coverage": {"baseline_dates": baseline_coverage,
                      "treatment_dates": treatment_coverage,
                      "ratio": treatment_coverage / baseline_coverage if baseline_coverage else None,
                      "complete_pair_ratio": len(paired_dates) / len(coverage) if coverage else None,
                      "baseline_complete_ratio": baseline_complete_ratio,
                      "treatment_complete_ratio": treatment_complete_ratio},
        "block_pair_counts": block_pair_counts,
        "coverage_rows": coverage, "replay_rejections": replay_rejections,
        "interval": interval, "maturity": maturity,
        "mae_tail_5pct": {"baseline": baseline_tail, "treatment": treatment_tail},
        "holdout": {"status": "unconsumed", "dates": partition_result["final_holdout"],
                    "freeze_at": partition_result["freeze_at"]},
        "promotion": {"eligible": all(gates.values()), "gates": gates,
                       "reason": "frozen_p3_promotion_gates",
                       "shadow_only": primary_ready and not gates["confirmation_60d"]},
    })
    return _package(content)


def _package(content):
    return {"experiment_id": content_sha256(content)[:16], "content_sha256": content_sha256(content), "content": content}


def save_experiment(result, root=DEFAULT_ROOT):
    root = Path(root); root.mkdir(parents=True, exist_ok=True); path = root / (result["experiment_id"] + ".json")
    if path.exists(): return {"status": "unchanged", "path": str(path), "experiment_id": result["experiment_id"]}
    temporary = path.with_suffix(".tmp"); temporary.write_bytes(canonical_json(result) + b"\n")
    try: __import__("os").link(temporary, path)
    except FileExistsError: pass
    finally: temporary.unlink(missing_ok=True)
    return {"status": "created", "path": str(path), "experiment_id": result["experiment_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run frozen recommendation walk-forward experiment")
    parser.add_argument("--research-root", default=str(storage_root("research"))); parser.add_argument("--attribution-root", default=str(storage_root("evaluations"))); parser.add_argument("--contract-id"); parser.add_argument("--save", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv); result = run_walk_forward(load_primary_research_snapshots(args.research_root), load_candidate_signal_items(args.attribution_root, args.contract_id))
    if args.save: result["persistence"] = save_experiment(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
