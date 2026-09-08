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
from core.evolution_contract import build_evaluation_contract
from core.evolution_registry import validate_experiment_definition
from core.evolution_storage import input_manifest, storage_root
from core.research_events import assign_research_events, summarize_daily_alpha
from scans.daily_candidates import classify_candidates, select_candidate_pool

SCHEMA_VERSION = "recommendation-experiment/v2"
DEFAULT_ROOT = storage_root("experiments")
SHADOW_ROOT = storage_root("shadow")
# This is the stable v2 candidate-signal attribution contract used by the
# default evaluator (20/60 trading-day windows, zero-cost qfq HS300 alpha on
# the frozen investable research population).  Keep this derived from the
# same contract builder as ``recommendation_attribution``; the experiment
# schema name is not itself an evaluation contract.
DEFAULT_CONTRACT_ID = build_evaluation_contract(
    (5, 10, 20, 60), {
        "buy_commission_bps": 0,
        "sell_commission_bps": 0,
        "buy_slippage_bps": 0,
        "sell_slippage_bps": 0,
        "sell_tax_bps": 0,
    }, population_kind="frozen_investable_research_population",
).get("contract_id")
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
            "contract_id": DEFAULT_CONTRACT_ID,
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


def _contract_id_from_item(item):
    if not isinstance(item, dict):
        return None
    direct = item.get("contract_id")
    if direct:
        return str(direct)
    contract = item.get("evaluation_contract")
    if isinstance(contract, dict) and contract.get("contract_id"):
        return str(contract["contract_id"])
    return None


def _validate_candidate_contract_bindings(items, definition):
    """Require every evaluated outcome row to name the frozen contract."""
    observed = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        contract_id = _contract_id_from_item(item)
        if contract_id is None:
            raise ValueError("evaluation_contract_missing")
        observed.add(contract_id)
    if len(observed) > 1:
        raise ValueError("evaluation_contract_mixed")
    if observed and definition.get("contract_id") not in observed:
        raise ValueError("evaluation_contract_mismatch")
    return observed


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
        identity = (_market_from_item(record), str(candidate.get("code")))
        if identity in seen:
            reasons.append("replay_input_duplicate_code")
            continue
        seen.add(identity)
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
    valid_blocks_by_partition = defaultdict(list)
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
            label = next(iter(labels))
            valid_blocks.append(block)
            valid_blocks_by_partition[label].append(block)
    # A stratified bootstrap keeps every registered validation partition in
    # the sampling distribution instead of giving longer partitions more
    # weight merely because they expose more possible block starts.
    partition_indices = defaultdict(list)
    for index, day in enumerate(ordered_dates):
        partition_indices[partition_by_day.get(day, "default")].append(index)
    if len(partition_indices) > 1:
        missing_partition_pool = any(
            not valid_blocks_by_partition.get(label)
            for label in partition_indices
        )
    else:
        missing_partition_pool = False
    contract = {"method": "moving_trading_session_block_bootstrap",
                "status": ("ready" if len(valid_blocks) >= 2 and not missing_partition_pool
                            else "insufficient_data"),
                "seed": seed, "draws": draws, "block_length": block_length,
                "confidence": 0.95, "cross_partition_blocks": False,
                "valid_block_count": len(valid_blocks),
                "valid_blocks": valid_blocks,
                "valid_blocks_by_partition": {
                    label: blocks for label, blocks in sorted(valid_blocks_by_partition.items())
                },
                "sample_indices": [],
                "lower_95": None, "upper_95": None}
    if len(valid_blocks) < 2 or missing_partition_pool:
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
    partition_labels = sorted(partition_indices)
    for _ in range(draw_count):
        indices = []
        # Draw each partition independently and trim to its observed size so
        # the resulting mean retains the original date-weighted composition.
        for label in partition_labels:
            target_indices = partition_indices[label]
            pool = valid_blocks_by_partition.get(label) or []
            blocks_needed = (len(target_indices) + block_length - 1) // block_length
            sampled = []
            for _block in range(blocks_needed):
                sampled.extend(rng.choice(pool))
            indices.extend(sampled[:len(target_indices)])
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


def _partition_input_manifest(research_snapshots, candidate_signal_items,
                              definition, role, partition_dates, sessions):
    """Hash only the frozen inputs belonging to one registered partition.

    A validation result and its final-holdout result may be produced from the
    same source batch, but their evidence manifests must identify disjoint
    partition rows.  Hashing the complete caller input for both roles would
    make accidental holdout reuse indistinguishable from a valid run.
    """
    days = set(partition_dates or [])
    selected_snapshots = []
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        if _normalise_day(content.get("recommendation_date")) in days:
            selected_snapshots.append(snapshot)
    selected_items = []
    for item in candidate_signal_items or []:
        if not isinstance(item, dict):
            continue
        if _normalise_day(item.get("recommendation_date")) in days:
            # Keep the complete immutable outcome alongside its frozen signal
            # identity.  Release verification uses this embedded copy to
            # prove that every event alpha/MAE came from the evaluated
            # candidate row rather than from a self-authored summary.
            selected_items.append(copy.deepcopy(item))
    selected_items.sort(key=lambda item: (
        str(item.get("recommendation_date") or ""),
        _market_from_item(item), str(item.get("code") or ""),
        str(item.get("record_id") or ""),
    ))
    return input_manifest(
        partition_role=role,
        partition_dates=sorted(days),
        partition_dates_sha256=content_sha256(sorted(days)),
        research_run_ids=sorted(
            str((item or {}).get("run_id")
                or content_sha256(item.get("content", item))[:16])
            for item in selected_snapshots),
        research_content_sha256=sorted(
            str((item or {}).get("content_sha256")
                or content_sha256(item.get("content", item)))
            for item in selected_snapshots),
        candidate_signal_items=selected_items,
        definition=definition,
        primary_window=PRIMARY_WINDOW,
        confirmation_window=CONFIRMATION_WINDOW,
        trading_sessions=sessions,
    )


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
    values, maes, missing, events, complete_event_ids, alpha_rows, mae_rows = [], [], [], [], set(), [], []
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
        # Freeze the event anchor before looking at whether this particular
        # outcome is complete.  Otherwise a missing earliest outcome would
        # let a later overlapping signal become a false independent event.
        event = {
            "record_id": outcome.get("_record_id") or f"{day}:{market}:{code}",
            "market": market,
            "code": code,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "recommendation_date": day,
            "market_sessions": outcome.get("_market_sessions"),
            "evaluation_complete": False,
        }
        events.append(event)
        alpha = _number(label.get("hs300_alpha"))
        mae = _number(label.get("mae"))
        if label.get("status") != "complete" or alpha is None:
            missing.append({"code": code, "reason": label.get("status") or "missing_alpha", "side": side})
            continue
        complete_event_ids.add(event["record_id"])
        event["evaluation_complete"] = True
        event["hs300_alpha"] = alpha
        event["mae"] = mae
        values.append(alpha)
        alpha_rows.append({"recommendation_date": day, "code": code,
                           "hs300_alpha": alpha, "evaluation_status": "complete"})
        if mae is not None:
            maes.append(mae)
            mae_rows.append({"record_id": event["record_id"],
                             "recommendation_date": day, "market": market,
                             "code": code, "mae": mae, "side": side,
                             "evaluation_status": "complete"})
    return {
        "mean": sum(values) / len(values) if values else None,
        "maes": maes, "missing": missing, "events": events,
        "complete_event_ids": complete_event_ids,
        "alpha_rows": alpha_rows, "mae_rows": mae_rows,
        "complete": not missing and bool(rows),
    }


def run_walk_forward(research_snapshots, candidate_signal_items, definition=None, top=None,
                     experiment_id=None):
    # Direct exploratory calls may omit a frozen schedule and will remain
    # ``continue_accumulating``.  Registered/release-bound calls pass the
    # schedule-bearing definition and experiment identity explicitly.
    definition = validate_experiment_definition(
        definition or default_experiment(), require_schedule=False)
    _validate_candidate_contract_bindings(candidate_signal_items, definition)
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
    contract_id = definition["contract_id"]
    content = {"schema_version": SCHEMA_VERSION, "definition": definition,
               "experiment_id": str(experiment_id or ""),
               "contract_id": contract_id,
               "primary_window": PRIMARY_WINDOW, "confirmation_window": CONFIRMATION_WINDOW,
               "purge_sessions": PURGE_SESSIONS, "calendar_verified": calendar_verified,
               "trading_sessions": list(sessions),
               "partition_role": "validation", "input_dates": dates,
               "skipped_reasons": sorted(set(skipped)),
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
    # Retain every registered validation date in the denominator.  A missing
    # snapshot is an explicit rejected coverage row, not a silent deletion
    # that could inflate completeness ratios.
    blocks = [list(partition_result["validation"][name])
              for name in VALIDATION_BLOCK_NAMES]

    paired, coverage, baseline_maes, treatment_maes = [], [], [], []
    baseline_mae_rows, treatment_mae_rows = [], []
    baseline_events, treatment_events = [], []
    baseline_complete_event_ids, treatment_complete_event_ids = set(), set()
    confirmation_pairs, confirmation_baseline_events, confirmation_treatment_events = [], [], []
    confirmation_baseline_complete_event_ids = set()
    confirmation_treatment_complete_event_ids = set()
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
            if day not in by_date or day in invalid_dates:
                reason = ("replay_input_invalid_date_records" if day in invalid_dates
                          else "replay_input_missing_research_date")
                replay_rejections.append({"date": day, "baseline": [reason],
                                          "treatment": [reason]})
                coverage.append({"date": day, "block": block_no, "expected_date": True,
                                 "baseline_count": 0, "treatment_count": 0,
                                 "baseline_complete": False, "treatment_complete": False,
                                 "baseline_missing": [{"reason": reason}],
                                 "treatment_missing": [{"reason": reason}],
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
                coverage.append({"date": day, "block": block_no, "expected_date": True,
                                 "baseline_count": 0, "treatment_count": 0,
                                 "baseline_complete": False, "treatment_complete": False,
                                 "baseline_missing": baseline_replay["reasons"],
                                 "treatment_missing": treatment_replay["reasons"],
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
            baseline_mae_rows.extend(old["mae_rows"]); treatment_mae_rows.extend(new["mae_rows"])
            baseline_events.extend(old["events"]); treatment_events.extend(new["events"])
            baseline_complete_event_ids.update(old["complete_event_ids"])
            treatment_complete_event_ids.update(new["complete_event_ids"])
            baseline_daily_rows.extend(old["alpha_rows"]); treatment_daily_rows.extend(new["alpha_rows"])
            old60 = _metrics_for_rows(base, day, outcomes, label_limit, CONFIRMATION_WINDOW,
                                      "baseline_60d", skipped, sessions)
            new60 = _metrics_for_rows(trial, day, outcomes, label_limit, CONFIRMATION_WINDOW,
                                      "treatment_60d", skipped, sessions)
            confirmation_baseline_events.extend(old60["events"])
            confirmation_treatment_events.extend(new60["events"])
            confirmation_baseline_complete_event_ids.update(old60["complete_event_ids"])
            confirmation_treatment_complete_event_ids.update(new60["complete_event_ids"])
            if old["complete"] and new["complete"] and old["mean"] is not None and new["mean"] is not None:
                paired.append({"date": day, "block": block_no,
                               "baseline_mean": old["mean"],
                               "treatment_mean": new["mean"],
                               "delta": new["mean"] - old["mean"]})
            confirmation_entry = {"date": day, "baseline_complete": old60["complete"],
                                  "treatment_complete": new60["complete"],
                                  "baseline_missing": old60["missing"],
                                  "treatment_missing": new60["missing"]}
            if old60["complete"] and new60["complete"] and old60["mean"] is not None and new60["mean"] is not None:
                confirmation_entry.update({
                    "baseline_mean": old60["mean"],
                    "treatment_mean": new60["mean"],
                    "delta": new60["mean"] - old60["mean"],
                })
                confirmation_pairs.append(confirmation_entry)
            coverage.append({"date": day, "block": block_no, "expected_date": True,
                             "baseline_count": len(base), "treatment_count": len(trial),
                             "baseline_complete": old["complete"],
                             "treatment_complete": new["complete"],
                             "baseline_missing": old["missing"],
                             "treatment_missing": new["missing"],
                             "status": "paired" if old["complete"] and new["complete"] else "incomplete"})
    paired_dates = [row["date"] for row in paired]
    deltas = [row["delta"] for row in paired]
    validation_partition_dates = {
        name: list(partition_result["validation"][name])
        for name in VALIDATION_BLOCK_NAMES}
    interval = _bootstrap(
        deltas,
        seed=(definition.get("bootstrap") or {}).get("seed", 20260907),
        draws=(definition.get("bootstrap") or {}).get("draws", 2000),
        block_length=(definition.get("bootstrap") or {}).get("block_length", PRIMARY_WINDOW),
        dates=paired_dates,
        partitions=partition_result["validation"],
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
    # Only an event's frozen anchor can become mature.  A later overlapping
    # row with a complete outcome must not promote an earlier missing anchor.
    baseline_mature_events = [event for event in baseline_assigned["events"]
                              if event.get("record_id") in baseline_complete_event_ids]
    treatment_mature_events = [event for event in treatment_assigned["events"]
                               if event.get("record_id") in treatment_complete_event_ids]
    baseline_60_mature_events = [event for event in baseline_60_assigned["events"]
                                 if event.get("record_id") in confirmation_baseline_complete_event_ids]
    treatment_60_mature_events = [event for event in treatment_60_assigned["events"]
                                  if event.get("record_id") in confirmation_treatment_complete_event_ids]
    baseline_daily = summarize_daily_alpha(baseline_daily_rows)
    treatment_daily = summarize_daily_alpha(treatment_daily_rows)
    confirmation_deltas = [row["delta"] for row in confirmation_pairs]
    confirmation_mean = (sum(confirmation_deltas) / len(confirmation_deltas)
                         if confirmation_deltas else None)
    baseline_coverage = sum(bool(row.get("baseline_complete")) for row in coverage)
    treatment_coverage = sum(bool(row.get("treatment_complete")) for row in coverage)
    block_pair_counts = {
        str(block_no): sum(
            row["block"] == block_no and row["status"] == "paired"
            for row in coverage
        )
        for block_no in range(1, len(blocks) + 1)
    }
    baseline_expected_days = sum(bool(row.get("expected_date", True)) for row in coverage)
    treatment_expected_days = sum(bool(row.get("expected_date", True)) for row in coverage)
    baseline_complete_ratio = (baseline_coverage / baseline_expected_days
                               if baseline_expected_days else 0.0)
    treatment_complete_ratio = (treatment_coverage / treatment_expected_days
                                if treatment_expected_days else 0.0)
    baseline_tail = _percentile(baseline_maes, .05)
    treatment_tail = _percentile(treatment_maes, .05)
    maturity = {
        "baseline": {"valid_alpha_events": len(baseline_mature_events),
                      "alpha_mature_dates": baseline_daily["mature_dates"],
                      "alpha_mean": baseline_daily["mean_alpha"],
                      "alpha_missing_records": baseline_daily["missing_records"]},
        "treatment": {"valid_alpha_events": len(treatment_mature_events),
                       "alpha_mature_dates": treatment_daily["mature_dates"],
                       "alpha_mean": treatment_daily["mean_alpha"],
                       "alpha_missing_records": treatment_daily["missing_records"]},
        "valid_alpha_events": min(len(baseline_mature_events), len(treatment_mature_events)),
        "alpha_mature_dates": len(paired_dates),
        "confirmation_60d": {
            "baseline_valid_alpha_events": len(baseline_60_mature_events),
            "treatment_valid_alpha_events": len(treatment_60_mature_events),
            "complete_paired_dates": len(confirmation_pairs),
            "mean_delta": confirmation_mean,
        },
    }
    gates = {
        "calendar_verified": calendar_verified,
        "partition_contract": True,
        "minimum_events": maturity["valid_alpha_events"] >= 100,
        "minimum_events_baseline": len(baseline_mature_events) >= 100,
        "minimum_events_treatment": len(treatment_mature_events) >= 100,
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
            len(baseline_60_mature_events) >= 100
            and len(treatment_60_mature_events) >= 100
            and len(confirmation_pairs) >= 20
            and confirmation_mean is not None and confirmation_mean >= 0
        ),
    }
    primary_gate_names = (
        "calendar_verified", "partition_contract", "minimum_events",
        "minimum_events_baseline", "minimum_events_treatment", "minimum_dates",
        "minimum_dates_per_block", "three_oos_blocks", "coverage",
        "pair_completeness_95pct", "ci_lower_positive", "mae_tail",
    )
    primary_ready = all(gates[name] for name in primary_gate_names)
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
        "event_rows": {
            "baseline": baseline_events,
            "treatment": treatment_events,
            "baseline_60d": confirmation_baseline_events,
            "treatment_60d": confirmation_treatment_events,
        },
        "event_invalid": {
            "baseline": baseline_assigned["invalid"],
            "treatment": treatment_assigned["invalid"],
            "baseline_60d": baseline_60_assigned["invalid"],
            "treatment_60d": treatment_60_assigned["invalid"],
        },
        "coverage_rows": coverage, "replay_rejections": replay_rejections,
        "mae_rows": {"baseline": baseline_mae_rows, "treatment": treatment_mae_rows},
        "interval": interval, "maturity": maturity,
        "mae_tail_5pct": {"baseline": baseline_tail, "treatment": treatment_tail},
        "holdout": {"status": "unconsumed", "dates": partition_result["final_holdout"],
                    "freeze_at": partition_result["freeze_at"]},
        "promotion": {"eligible": all(gates.values()), "gates": gates,
                       "reason": "frozen_p3_promotion_gates",
                       "shadow_only": primary_ready and not gates["confirmation_60d"]},
    })
    content["input_manifest"] = _partition_input_manifest(
        research_snapshots, candidate_signal_items, definition, "validation",
        [day for name in VALIDATION_BLOCK_NAMES
         for day in partition_result["validation"][name]], sessions)
    return _package(content)


def run_final_holdout(research_snapshots, candidate_signal_items, definition=None,
                      holdout_consumption=None, top=None, experiment_id=None):
    """Evaluate the frozen final holdout exactly once after reservation.

    Validation results intentionally leave ``final_holdout`` unconsumed.  This
    entry point is the separate post-freeze operation: callers must first
    reserve the batch/input through ``register_holdout_consumption`` and pass
    that receipt here.  The returned v2 result carries only holdout rows and
    the final-confirmation gates; it is not fed back into proposal generation.
    """
    definition = validate_experiment_definition(
        definition or default_experiment(), require_schedule=False)
    if not isinstance(holdout_consumption, dict):
        raise ValueError("holdout_consumption_required")
    consumption_status = holdout_consumption.get("status")
    semantic_status = holdout_consumption.get("consumption_status")
    if consumption_status not in ("created", "unchanged", "reserved", "completed") \
            and semantic_status not in ("reserved", "completed"):
        raise ValueError("holdout_consumption_invalid")
    consumption_id = str(holdout_consumption.get("consumption_id") or "")
    result_id = str(holdout_consumption.get("result_id") or "")
    receipt_experiment_id = str(holdout_consumption.get("experiment_id") or "")
    research_batch_id = str(holdout_consumption.get("research_batch_id") or "")
    input_sha256 = str(holdout_consumption.get("input_manifest_sha256") or "")
    if not consumption_id or not research_batch_id or not input_sha256:
        raise ValueError("holdout_consumption_identity_missing")
    if experiment_id and receipt_experiment_id and str(experiment_id) != receipt_experiment_id:
        raise ValueError("holdout_consumption_experiment_mismatch")
    bound_experiment_id = str(experiment_id or receipt_experiment_id or "")
    if not result_id or Path(result_id).name != result_id or result_id in {".", ".."} \
            or "/" in result_id or "\\" in result_id:
        raise ValueError("holdout_consumption_result_id_invalid")
    _validate_candidate_contract_bindings(candidate_signal_items, definition)
    by_date, metadata, skipped = defaultdict(list), {}, []
    invalid_dates = set()
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        if content.get("snapshot_type") != "formal" \
                or (content.get("official_snapshot") or {}).get("link_status") != "linked":
            continue
        day = _normalise_day(content.get("recommendation_date"))
        if day is None:
            continue
        parameters = content.get("parameter_summary") or {}
        limit = parameters.get("top") if top is None else top
        min_score = parameters.get("min_score", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 \
                or _number(min_score) is None:
            invalid_dates.add(day)
            continue
        metadata[day] = {"top": limit, "min_score": float(min_score),
                         "policy": copy.deepcopy(content.get("policy") or {
                             "mode": "actionable", "max_recommendations": limit})}
        for record in content.get("records") or []:
            if not isinstance(record, dict) or _research_record_date(content, record) != day:
                invalid_dates.add(day)
                continue
            by_date[day].append(record)
    outcomes = _outcomes(candidate_signal_items)
    dates = sorted(day for day in by_date if day)
    sessions = _calendar_from_inputs(research_snapshots, candidate_signal_items, definition)
    calendar_verified = bool(sessions)
    if not sessions:
        sessions = dates
        skipped.append("trading_sessions_required")
    partition_result = _resolve_time_partitions(definition, dates, sessions)
    contract_id = definition["contract_id"]
    base_content = {
        "schema_version": SCHEMA_VERSION,
        "definition": definition,
        "experiment_id": bound_experiment_id,
        "contract_id": contract_id,
        "primary_window": PRIMARY_WINDOW,
        "confirmation_window": CONFIRMATION_WINDOW,
        "purge_sessions": PURGE_SESSIONS,
        "calendar_verified": calendar_verified,
        "trading_sessions": list(sessions),
        "input_dates": dates,
        "partition_role": "final_holdout",
        "input_manifest": input_manifest(
            partition_role="final_holdout",
            partition_dates=[],
            partition_dates_sha256=content_sha256([]),
            candidate_signal_items=[], definition=definition,
            primary_window=PRIMARY_WINDOW, confirmation_window=CONFIRMATION_WINDOW,
            trading_sessions=sessions,
        ),
    }
    if partition_result.get("status") != "valid":
        base_content.update({
            "status": "continue_accumulating",
            "reason": partition_result.get("reason", "partition_definition_invalid"),
            "partition_status": partition_result,
            "holdout": {"status": "not_consumed"},
        })
        return _package(base_content)
    # Include every registered holdout date so missing post-freeze inputs
    # remain visible in the final confirmation denominator.
    holdout_dates = list(partition_result["final_holdout"])

    paired, coverage, baseline_maes, treatment_maes = [], [], [], []
    baseline_mae_rows, treatment_mae_rows = [], []
    baseline_events, treatment_events = [], []
    baseline_complete_ids, treatment_complete_ids = set(), set()
    replay_rejections = []
    # Final-holdout recommendation dates may mature after the last registered
    # holdout date; unlike validation blocks there is no later partition to
    # protect.  Endpoint/calendar validation still happens in
    # ``_metrics_for_rows``.
    holdout_end = None
    for day in holdout_dates:
        if day not in by_date or day in invalid_dates:
            reason = ("replay_input_invalid_date_records" if day in invalid_dates
                      else "replay_input_missing_research_date")
            replay_rejections.append({"date": day, "baseline": [reason], "treatment": [reason]})
            coverage.append({"date": day, "block": "holdout", "expected_date": True,
                             "baseline_count": 0, "treatment_count": 0,
                             "baseline_complete": False, "treatment_complete": False,
                             "baseline_missing": [{"reason": reason}],
                             "treatment_missing": [{"reason": reason}],
                             "status": "replay_input_rejected"})
            continue
        meta = metadata[day]
        baseline = _replay_selection(by_date[day], definition["baseline"], meta["top"],
                                     meta["min_score"], meta["policy"])
        treatment = _replay_selection(by_date[day], definition["treatment"], meta["top"],
                                      meta["min_score"], meta["policy"])
        if baseline["status"] != "ok" or treatment["status"] != "ok":
            replay_rejections.append({"date": day, "baseline": baseline["reasons"],
                                       "treatment": treatment["reasons"]})
            coverage.append({"date": day, "block": "holdout", "expected_date": True,
                             "baseline_count": 0, "treatment_count": 0,
                             "baseline_complete": False, "treatment_complete": False,
                             "baseline_missing": baseline["reasons"],
                             "treatment_missing": treatment["reasons"],
                             "status": "replay_input_rejected"})
            continue
        old = _metrics_for_rows(baseline["selected"], day, outcomes, holdout_end,
                                PRIMARY_WINDOW, "baseline", skipped, sessions)
        new = _metrics_for_rows(treatment["selected"], day, outcomes, holdout_end,
                                PRIMARY_WINDOW, "treatment", skipped, sessions)
        baseline_maes.extend(old["maes"]); treatment_maes.extend(new["maes"])
        baseline_mae_rows.extend(old["mae_rows"]); treatment_mae_rows.extend(new["mae_rows"])
        baseline_events.extend(old["events"]); treatment_events.extend(new["events"])
        baseline_complete_ids.update(old["complete_event_ids"])
        treatment_complete_ids.update(new["complete_event_ids"])
        if old["complete"] and new["complete"] and old["mean"] is not None and new["mean"] is not None:
            paired.append({"date": day, "block": "holdout",
                           "baseline_mean": old["mean"], "treatment_mean": new["mean"],
                           "delta": new["mean"] - old["mean"]})
        coverage.append({"date": day, "block": "holdout", "expected_date": True,
                         "baseline_count": len(baseline["selected"]),
                         "treatment_count": len(treatment["selected"]),
                         "baseline_complete": old["complete"],
                         "treatment_complete": new["complete"],
                         "baseline_missing": old["missing"], "treatment_missing": new["missing"],
                         "status": "paired" if old["complete"] and new["complete"] else "incomplete"})
    baseline_assigned = assign_research_events(baseline_events, PRIMARY_WINDOW,
                                               market_sessions=sessions)
    treatment_assigned = assign_research_events(treatment_events, PRIMARY_WINDOW,
                                                market_sessions=sessions)
    baseline_mature = [row for row in baseline_assigned["events"]
                       if row.get("record_id") in baseline_complete_ids]
    treatment_mature = [row for row in treatment_assigned["events"]
                        if row.get("record_id") in treatment_complete_ids]
    baseline_complete = sum(bool(row.get("baseline_complete")) for row in coverage)
    treatment_complete = sum(bool(row.get("treatment_complete")) for row in coverage)
    expected = len(coverage)
    baseline_ratio = baseline_complete / expected if expected else 0.0
    treatment_ratio = treatment_complete / expected if expected else 0.0
    baseline_tail = _percentile(baseline_maes, .05)
    treatment_tail = _percentile(treatment_maes, .05)
    deltas = [row["delta"] for row in paired]
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    holdout_gates = {
        "calendar_verified": calendar_verified,
        "partition_contract": True,
        "minimum_complete_paired_dates": len(paired) >= 20,
        "primary_delta_positive": mean_delta is not None and mean_delta > 0,
        "coverage": baseline_complete >= 1 and treatment_complete >= .9 * baseline_complete,
        "pair_completeness_95pct": baseline_ratio >= .95 and treatment_ratio >= .95,
        "mae_tail": baseline_tail is not None and treatment_tail is not None
        and treatment_tail >= baseline_tail - .01,
    }
    content = {
        **base_content,
        "status": "holdout_confirmed" if all(holdout_gates.values()) else "continue_accumulating",
        "partitions": partition_result,
        "paired_dates": paired,
        "block_pair_counts": {"holdout": sum(row.get("status") == "paired" for row in coverage)},
        "coverage_rows": coverage,
        "coverage": {"baseline_dates": baseline_complete,
                      "treatment_dates": treatment_complete,
                      "ratio": treatment_complete / baseline_complete if baseline_complete else None,
                      "baseline_complete_ratio": baseline_ratio,
                      "treatment_complete_ratio": treatment_ratio,
                      "complete_pair_ratio": len(paired) / expected if expected else 0.0},
        "event_rows": {"baseline": baseline_events, "treatment": treatment_events},
        "event_invalid": {"baseline": baseline_assigned["invalid"],
                           "treatment": treatment_assigned["invalid"]},
        "replay_rejections": replay_rejections,
        "maturity": {
            "baseline": {"valid_alpha_events": len(baseline_mature)},
            "treatment": {"valid_alpha_events": len(treatment_mature)},
            "valid_alpha_events": min(len(baseline_mature), len(treatment_mature)),
            "alpha_mature_dates": len(paired),
        },
        "mae_tail_5pct": {"baseline": baseline_tail, "treatment": treatment_tail},
        "interval": {},
        "holdout": {"status": "consumed", "dates": partition_result["final_holdout"],
                     "freeze_at": partition_result["freeze_at"],
                     "consumption_id": consumption_id,
                     "research_batch_id": research_batch_id},
        "promotion": {"eligible": all(holdout_gates.values()), "gates": holdout_gates,
                       "reason": "frozen_final_holdout_confirmation"},
        "skipped_reasons": sorted(set(skipped)),
        "mae_rows": {"baseline": baseline_mae_rows, "treatment": treatment_mae_rows},
    }
    holdout_partition_dates = list(partition_result["final_holdout"])
    holdout_manifest = _partition_input_manifest(
        research_snapshots, candidate_signal_items, definition, "final_holdout",
        holdout_partition_dates, sessions)
    # The reservation must be over exactly the frozen partition inputs that
    # this evaluator is about to consume.  Never let an arbitrary receipt
    # value replace the canonical derived hash.
    derived_input_sha256 = holdout_manifest["input_sha256"]
    if input_sha256 != derived_input_sha256:
        raise ValueError("holdout_input_manifest_mismatch")
    holdout_manifest["derived_input_sha256"] = derived_input_sha256
    content["input_manifest"] = holdout_manifest
    content["input_manifest"] = dict(
        content["input_manifest"],
        holdout_consumption_id=consumption_id,
        holdout_research_batch_id=research_batch_id,
        holdout_input_manifest_sha256=input_sha256,
    )
    # The receipt's result id is the only permitted holdout filename identity;
    # validate it above before using it in the returned envelope.
    return _package(content, result_id=result_id)


def build_forward_shadow_result(strategy_runs, evaluation=None, definition=None,
                                experiment_id=None):
    """Aggregate measured outcomes onto immutable forward shadow snapshots.

    ``daily_candidates`` intentionally persists ranking-only snapshots.  This
    API is the separate evaluator boundary: it accepts only those snapshots
    plus a measured evaluation payload, verifies every source binding, and
    emits a release-verifiable ``recommendation-shadow/v1`` envelope.  A raw
    ranking snapshot can therefore never be mistaken for a mature shadow
    result.
    """
    if isinstance(strategy_runs, dict):
        strategy_runs = [strategy_runs]
    runs = list(strategy_runs or [])
    if not runs:
        raise ValueError("shadow_sources_required")
    first_payload = (runs[0].get("content") if isinstance(runs[0], dict)
                     and isinstance(runs[0].get("content"), dict) else runs[0])
    selected_definition = validate_experiment_definition(
        definition or (first_payload.get("definition") if isinstance(first_payload, dict) else None)
        or default_experiment(), require_schedule=False)
    selected_experiment = str(experiment_id or "")
    nested_evaluations = []
    sources = []
    source_dates = set()
    source_selection_by_date = {}

    def normalize_selection(value):
        if not isinstance(value, dict):
            raise ValueError("shadow_source_selection_missing")
        top = value.get("top")
        min_score = _number(value.get("min_score"))
        policy = value.get("policy")
        if isinstance(top, bool) or not isinstance(top, int) or top < 1 \
                or min_score is None or not isinstance(policy, dict):
            raise ValueError("shadow_source_selection_invalid")
        result = {"top": top, "min_score": float(min_score),
                  "policy": copy.deepcopy(policy)}
        for side in ("baseline", "treatment"):
            arm = value.get(side)
            if not isinstance(arm, dict) or not isinstance(arm.get("selected"), list) \
                    or not isinstance(arm.get("buckets"), dict):
                raise ValueError("shadow_source_selection_invalid")
            selected = []
            selected_keys = set()
            for item in arm["selected"]:
                if not isinstance(item, dict) or not item.get("market") or not item.get("code"):
                    raise ValueError("shadow_source_selection_identity_invalid")
                identity = {"market": str(item["market"]).upper(),
                            "code": str(item["code"])}
                key = (identity["market"], identity["code"])
                if key in selected_keys:
                    raise ValueError("shadow_source_selection_duplicate")
                selected_keys.add(key)
                selected.append(identity)
            selected.sort(key=lambda item: (item["market"], item["code"]))
            buckets = {}
            bucket_keys = set()
            for bucket, rows in sorted(arm["buckets"].items()):
                if not isinstance(rows, list):
                    raise ValueError("shadow_source_selection_bucket_invalid")
                buckets[str(bucket)] = []
                for item in rows:
                    if not isinstance(item, dict) or not item.get("market") or not item.get("code"):
                        raise ValueError("shadow_source_selection_identity_invalid")
                    identity = {"market": str(item["market"]).upper(),
                                "code": str(item["code"])}
                    key = (identity["market"], identity["code"])
                    if key in bucket_keys:
                        raise ValueError("shadow_source_selection_duplicate")
                    bucket_keys.add(key)
                    buckets[str(bucket)].append(identity)
                buckets[str(bucket)].sort(key=lambda item: (item["market"], item["code"]))
            if bucket_keys != selected_keys:
                raise ValueError("shadow_source_selection_bucket_mismatch")
            result[side] = {"selected": selected, "buckets": buckets}
        return result

    for original in runs:
        if not isinstance(original, dict):
            raise ValueError("shadow_source_invalid")
        wrapped = isinstance(original.get("content"), dict)
        payload = original.get("content") if wrapped else original
        source = copy.deepcopy(payload)
        expected_digest = (original.get("content_sha256") if wrapped else
                           source.pop("content_sha256", None))
        expected_input = (original.get("input_digest") if wrapped else
                          source.pop("input_digest", None))
        if wrapped and expected_digest is None:
            expected_digest = source.pop("content_sha256", None)
        if wrapped and expected_input is None:
            expected_input = source.pop("input_digest", None)
        if not isinstance(expected_digest, str) or expected_input != expected_digest \
                or expected_digest != content_sha256(source):
            raise ValueError("shadow_source_digest_mismatch")
        if source.get("schema_version") != "recommendation-strategy-shadow/v1" \
                or source.get("snapshot_type") != "formal" \
                or source.get("shadow_type") != "forward" \
                or source.get("formal_policy_affected") is not False \
                or source.get("retrospective") is True \
                or source.get("independent_snapshot") is not True:
            raise ValueError("shadow_source_not_forward")
        source_definition = validate_experiment_definition(
            source.get("definition"), require_schedule=False)
        if source_definition != selected_definition:
            raise ValueError("shadow_source_definition_mismatch")
        source_experiment = str(source.get("experiment_id")
                                or original.get("experiment_id") or "")
        if not source_experiment:
            raise ValueError("shadow_source_experiment_missing")
        if selected_experiment and source_experiment != selected_experiment:
            raise ValueError("shadow_source_experiment_mismatch")
        selected_experiment = selected_experiment or source_experiment
        if source.get("contract_id") != selected_definition["contract_id"]:
            raise ValueError("shadow_source_contract_mismatch")
        if not isinstance(source.get("basis_date"), str) or not source["basis_date"]:
            raise ValueError("shadow_source_date_missing")
        try:
            if date.fromisoformat(source["basis_date"]).isoformat() != source["basis_date"]:
                raise ValueError
        except ValueError as exc:
            raise ValueError("shadow_source_date_invalid") from exc
        if source["basis_date"] in source_dates:
            raise ValueError("shadow_source_date_duplicate")
        source_dates.add(source["basis_date"])
        manifest = source.get("input_manifest")
        manifest_inputs = manifest.get("inputs") if isinstance(manifest, dict) else None
        if not isinstance(manifest_inputs, dict) \
                or not isinstance(manifest.get("input_sha256"), str) \
                or manifest.get("input_sha256") != content_sha256(manifest_inputs):
            raise ValueError("shadow_source_manifest_missing")
        source_snapshot = copy.deepcopy(source)
        selection = normalize_selection(source.get("selection"))
        source_selection_by_date[source["basis_date"]] = selection
        sources.append({"basis_date": source["basis_date"],
                        "snapshot_type": source["snapshot_type"],
                        "content_sha256": expected_digest,
                        "input_sha256": manifest["input_sha256"],
                        "selection": selection,
                        "snapshot": source_snapshot})
        if isinstance(source.get("evaluation"), dict):
            nested_evaluations.append(source["evaluation"])
    if evaluation is None:
        if len(nested_evaluations) != 1:
            raise ValueError("shadow_evaluation_required")
        evaluation = nested_evaluations[0]
    if not isinstance(evaluation, dict):
        raise ValueError("shadow_evaluation_invalid")
    paired = evaluation.get("paired_dates")
    coverage_rows = evaluation.get("coverage_rows")
    event_rows = evaluation.get("event_rows")
    trading_sessions = evaluation.get("trading_sessions")
    if not isinstance(paired, list) or not isinstance(coverage_rows, list) \
            or not isinstance(event_rows, dict) or not isinstance(trading_sessions, list):
        raise ValueError("shadow_evaluation_raw_rows_missing")
    paired = copy.deepcopy(paired)
    coverage_rows = copy.deepcopy(coverage_rows)
    if len({str(row.get("date")) for row in paired if isinstance(row, dict)}) != len(paired):
        raise ValueError("shadow_evaluation_duplicate_dates")
    # One forward source snapshot represents one frozen recommendation date.
    # Do not allow a single source to smuggle a retrospective multi-date
    # aggregate into the shadow evidence envelope.
    if len(source_dates) != len(paired):
        raise ValueError("shadow_source_date_binding_mismatch")
    paired_source_dates = set()
    deltas = []
    for row in paired:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValueError("shadow_evaluation_pair_invalid")
        try:
            if date.fromisoformat(row["date"]).isoformat() != row["date"]:
                raise ValueError
        except ValueError as exc:
            raise ValueError("shadow_evaluation_pair_invalid") from exc
        if row["date"] not in source_dates or row["date"] in paired_source_dates:
            raise ValueError("shadow_source_date_binding_mismatch")
        paired_source_dates.add(row["date"])
        if not (_number(row.get("baseline_mean")) is not None
                                              and _number(row.get("treatment_mean")) is not None
                                              and _number(row.get("delta")) is not None):
            raise ValueError("shadow_evaluation_pair_invalid")
        baseline, treatment, delta = (float(row["baseline_mean"]),
                                      float(row["treatment_mean"]),
                                      float(row["delta"]))
        if abs(treatment - baseline - delta) > 1e-9:
            raise ValueError("shadow_evaluation_pair_invalid")
        deltas.append(delta)
    source_maturity = evaluation.get("maturity") if isinstance(evaluation.get("maturity"), dict) else {}
    event_counts = {}
    for side in ("baseline", "treatment"):
        rows = event_rows.get(side)
        if not isinstance(rows, list):
            raise ValueError("shadow_evaluation_event_rows_missing")
        complete_ids = {str(row.get("record_id")) for row in rows
                        if isinstance(row, dict) and row.get("evaluation_complete") is True}
        assigned = assign_research_events(rows, PRIMARY_WINDOW,
                                          market_sessions=trading_sessions)
        if any(not isinstance(row, dict)
               or str(row.get("recommendation_date") or "") not in source_dates
               for row in rows):
            raise ValueError("shadow_source_date_binding_mismatch")
        if any(row.get("evaluation_complete") is True
               and _number(row.get("hs300_alpha")) is None for row in rows
               if isinstance(row, dict)):
            raise ValueError("shadow_evaluation_event_alpha_missing")
        event_counts[side] = sum(str(row.get("record_id")) in complete_ids
                                 for row in assigned["events"])
    coverage_dates = {
        str(row.get("date")) for row in coverage_rows if isinstance(row, dict)
    }
    if coverage_dates != source_dates:
        raise ValueError("shadow_source_date_binding_mismatch")
    for row in coverage_rows:
        if not isinstance(row, dict):
            raise ValueError("shadow_coverage_row_invalid")
        day = str(row.get("date") or "")
        selection = source_selection_by_date.get(day) or {}
        for side in ("baseline", "treatment"):
            count = row.get(f"{side}_count")
            selected = ((selection.get(side) or {}).get("selected") or [])
            if isinstance(count, bool) or not isinstance(count, int) or count != len(selected):
                raise ValueError("shadow_selection_coverage_mismatch")
    for side in ("baseline", "treatment"):
        rows = event_rows.get(side) or []
        selected_by_date = {
            day: {(item["market"], item["code"])
                  for item in (source_selection_by_date[day][side]["selected"])}
            for day in source_dates
        }
        event_keys_by_date = {day: set() for day in source_dates}
        event_rows_by_date = {day: {} for day in source_dates}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("shadow_evaluation_event_rows_missing")
            key = (str(row.get("market") or "").upper(), str(row.get("code") or ""))
            day = str(row.get("recommendation_date") or "")
            if key not in selected_by_date.get(day, set()):
                raise ValueError("shadow_selection_event_mismatch")
            if key in event_keys_by_date.setdefault(day, set()):
                raise ValueError("shadow_selection_event_duplicate")
            event_keys_by_date[day].add(key)
            event_rows_by_date.setdefault(day, {})[key] = row
        if any(event_keys_by_date.get(day, set()) != selected
               for day, selected in selected_by_date.items()):
            raise ValueError("shadow_selection_event_set_mismatch")
        for day, selected in selected_by_date.items():
            if any(event_rows_by_date[day][key].get("evaluation_complete") is not True
                   or _number(event_rows_by_date[day][key].get("hs300_alpha")) is None
                   for key in selected):
                raise ValueError("shadow_selection_event_incomplete")
    valid_events = min(event_counts["baseline"], event_counts["treatment"])
    supplied_events = evaluation.get("valid_alpha_events", source_maturity.get("valid_alpha_events"))
    if supplied_events is not None:
        supplied_events_number = _number(supplied_events)
        if supplied_events_number is None or int(supplied_events_number) != valid_events:
            raise ValueError("shadow_evaluation_event_count_mismatch")
    coverage = copy.deepcopy(evaluation.get("coverage") or {})
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    content = {
        "schema_version": "recommendation-shadow/v1",
        "shadow_type": "forward",
        "retrospective": False,
        "independent_snapshot": True,
        "formal_policy_affected": False,
        "evaluation_status": "forward_aggregated",
        "experiment_id": selected_experiment,
        "definition": selected_definition,
        "contract_id": selected_definition["contract_id"],
        "source_runs": sorted(sources, key=lambda item: item["basis_date"]),
        "trading_sessions": copy.deepcopy(trading_sessions),
        "paired_dates": paired,
        "coverage_rows": coverage_rows,
        "event_rows": copy.deepcopy(event_rows),
        "coverage": coverage,
        "complete_paired_dates": len(paired),
        "valid_alpha_events": int(valid_events),
        "mean_delta": mean_delta,
        "input_manifest": input_manifest(
            partition_role="forward_shadow",
            source_runs=sorted(sources, key=lambda item: item["basis_date"]),
            evaluation_input_sha256=(evaluation.get("input_manifest") or {}).get("input_sha256"),
            definition=selected_definition,
            contract_id=selected_definition["contract_id"],
        ),
    }
    result = _package(content)
    # Reuse the registry verifier at call time (the import is intentionally
    # lazy because the registry imports this module's frozen definition).
    from core.evolution_registry import _verify_shadow_result
    _verify_shadow_result(result)
    return result


# Descriptive alias for orchestration code that calls this a shadow reducer.
aggregate_forward_shadow = build_forward_shadow_result


# Descriptive aliases used by orchestration callers.
evaluate_final_holdout = run_final_holdout
run_holdout = run_final_holdout


def _package(content, result_id=None):
    result_id = str(result_id or content_sha256(content)[:16])
    if not result_id or Path(result_id).name != result_id or result_id in {".", ".."} \
            or "/" in result_id or "\\" in result_id:
        raise ValueError("result_id_invalid")
    return {"experiment_id": result_id, "content_sha256": content_sha256(content), "content": content}


def save_experiment(result, root=DEFAULT_ROOT):
    if not isinstance(result, dict):
        raise ValueError("result_envelope_invalid")
    result_id = str(result.get("experiment_id") or result.get("result_id") or "")
    if not result_id or Path(result_id).name != result_id \
            or result_id in {".", ".."} or "/" in result_id or "\\" in result_id:
        raise ValueError("result_id_invalid")
    content = result.get("content")
    if not isinstance(content, dict) \
            or result.get("content_sha256") != content_sha256(content):
        raise ValueError("result_digest_mismatch")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (result_id + ".json")
    payload = canonical_json(result) + b"\n"
    if path.exists():
        try:
            existing = path.read_bytes()
            if existing == payload:
                return {"status": "unchanged", "path": str(path), "experiment_id": result_id}
        except OSError:
            pass
        raise ValueError("result_persistence_conflict")
    import os
    import tempfile
    fd, temporary_name = tempfile.mkstemp(dir=root, prefix=f".{result_id}.tmp-")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(payload)
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise ValueError("result_persistence_conflict")
            return {"status": "unchanged", "path": str(path), "experiment_id": result_id}
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "created", "path": str(path), "experiment_id": result_id}


def save_shadow_evaluation(result, root=SHADOW_ROOT):
    """Persist an already verified forward-shadow aggregate immutably."""
    from core.evolution_registry import _verify_shadow_result
    _verify_shadow_result(result)
    return save_experiment(result, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run frozen recommendation walk-forward experiment")
    parser.add_argument("--research-root", default=str(storage_root("research"))); parser.add_argument("--attribution-root", default=str(storage_root("evaluations"))); parser.add_argument("--contract-id"); parser.add_argument("--experiment-id"); parser.add_argument("--save", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    definition = None
    if args.experiment_id:
        from core.evolution_registry import resolve_experiment
        definition = (resolve_experiment(args.experiment_id).get("content") or {}).get("definition")
    result = run_walk_forward(
        load_primary_research_snapshots(args.research_root),
        load_candidate_signal_items(args.attribution_root, args.contract_id),
        definition=definition, experiment_id=args.experiment_id)
    if args.save: result["persistence"] = save_experiment(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
