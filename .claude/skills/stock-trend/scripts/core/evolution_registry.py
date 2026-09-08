"""Immutable registry for recommendation-evolution experiments.

The registry records attempts as well as successes.  It is deliberately not a
feature flag for the formal candidate policy; publishing remains a later,
explicit operation.
"""
import copy
import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path

from .cache_utils import CACHE_DIR
from .evolution_contract import build_evaluation_contract
from .recommendation_snapshot import canonical_json, content_sha256
from .research_events import assign_research_events


SCHEMA_VERSION = "evolution-registry/v2"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "registry"
DEFAULT_RELEASE_ROOT = Path(CACHE_DIR) / "evolution" / "releases"
DEFAULT_EXPERIMENTS_ROOT = Path(CACHE_DIR) / "evolution" / "experiments"
DEFAULT_SHADOW_ROOT = Path(CACHE_DIR) / "evolution" / "shadow"
FROZEN_CONTRACT_ID = build_evaluation_contract(
    (5, 10, 20, 60), {
        "buy_commission_bps": 0,
        "sell_commission_bps": 0,
        "buy_slippage_bps": 0,
        "sell_slippage_bps": 0,
        "sell_tax_bps": 0,
    }, population_kind="frozen_investable_research_population",
).get("contract_id")
HOLDOUT_SCHEMA_VERSION = "evolution-holdout-consumption/v1"
RELEASE_EVIDENCE_SCHEMA_VERSION = "evolution-release-evidence/v2"
STATES = ("draft", "validated", "shadow", "eligible", "active", "retired", "reverted")
_ALLOWED = {
    "draft": {"validated", "retired"}, "validated": {"shadow", "retired"},
    "shadow": {"eligible", "retired", "reverted"}, "eligible": {"active", "retired", "reverted"},
    "active": {"retired", "reverted"}, "retired": set(), "reverted": set(),
}


def _frozen_partition_contract(value):
    """Resolve and validate the schedule that is frozen at registration.

    ``definition.partitions`` may use inclusive ``start``/``end`` intervals
    or explicit session lists, but the persisted registration must always
    carry one complete, sorted trading-session calendar.  Keeping this check
    in the registry prevents a result from supplying a different calendar or
    partition layout at release time.
    """
    sessions = value.get("trading_sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("experiment_trading_sessions_required")

    def iso_day(raw, field):
        if not isinstance(raw, str):
            raise ValueError(f"experiment_{field}_invalid")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"experiment_{field}_invalid") from exc
        if parsed.isoformat() != raw:
            raise ValueError(f"experiment_{field}_invalid")
        return raw

    normalized_sessions = [iso_day(raw, "trading_session") for raw in sessions]
    if normalized_sessions != sorted(set(normalized_sessions)):
        raise ValueError("experiment_trading_sessions_not_sorted")
    positions = {day: index for index, day in enumerate(normalized_sessions)}

    raw = value.get("partitions")
    if not isinstance(raw, dict):
        raise ValueError("experiment_partitions_required")
    validation = raw.get("validation")
    if validation is not None:
        if not isinstance(validation, (list, tuple)) or len(validation) != 3:
            raise ValueError("experiment_validation_partitions_required")
        raw = dict(raw)
        for name, interval in zip(("validation_1", "validation_2", "validation_3"), validation):
            raw.setdefault(name, interval)
    required = ("discovery", "validation_1", "validation_2", "validation_3", "final_holdout")
    if any(name not in raw for name in required):
        raise ValueError("experiment_partitions_incomplete")

    resolved = {}
    for name in required:
        interval = raw[name]
        if isinstance(interval, dict):
            start, end = interval.get("start"), interval.get("end")
            if start is None or end is None:
                raise ValueError(f"experiment_partition_{name}_invalid")
            start, end = iso_day(start, f"partition_{name}_start"), iso_day(end, f"partition_{name}_end")
            if start > end:
                raise ValueError(f"experiment_partition_{name}_invalid")
            values = normalized_sessions[positions[start]:positions[end] + 1] \
                if start in positions and end in positions else []
        elif isinstance(interval, list):
            values = [iso_day(day, f"partition_{name}_date") for day in interval]
            if values != sorted(set(values)) or any(day not in positions for day in values):
                raise ValueError(f"experiment_partition_{name}_invalid")
        else:
            raise ValueError(f"experiment_partition_{name}_invalid")
        if not values:
            raise ValueError(f"experiment_partition_{name}_empty")
        resolved[name] = values

    purge = value.get("purge_sessions", 60)
    if isinstance(purge, bool) or not isinstance(purge, int) or purge < 60:
        raise ValueError("experiment_purge_window_invalid")
    ordered = required
    for previous, current in zip(ordered, ordered[1:]):
        if positions[resolved[current][0]] - positions[resolved[previous][-1]] - 1 < purge:
            raise ValueError("experiment_partition_purge_gap_invalid")
    freeze_at = value.get("freeze_at") or value.get("frozen_at")
    freeze_at = iso_day(freeze_at, "freeze_at")
    if freeze_at not in positions or freeze_at >= resolved["final_holdout"][0]:
        raise ValueError("experiment_freeze_at_invalid")
    used = [day for name in ordered for day in resolved[name]]
    if len(used) != len(set(used)):
        raise ValueError("experiment_partition_overlap")
    return {
        "status": "valid", "calendar_verified": True,
        "purge_sessions": purge, "freeze_at": freeze_at,
        "discovery": resolved["discovery"],
        "validation": {name: resolved[name] for name in ("validation_1", "validation_2", "validation_3")},
        "final_holdout": resolved["final_holdout"],
        "all_dates": sorted(set(used)),
    }


def validate_experiment_definition(definition, require_schedule=True):
    """Accept P3's single, frozen within-bucket ranking experiment only.

    Exploratory evaluator calls can use the loose rule-only form and are
    never release eligible.  Registry registration and every release verifier
    require the complete frozen schedule.
    """
    value = copy.deepcopy(definition or {})
    if value.get("kind") != "buy_point_priority_bonus":
        raise ValueError("experiment_kind_out_of_scope")
    if value.get("baseline") != {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}:
        raise ValueError("baseline_not_frozen")
    if value.get("treatment") != {"strict_level_1": 0, "strict_level_2": 0, "strict_level_3": 0}:
        raise ValueError("treatment_not_frozen")
    if value.get("changes") != ["within_bucket_ranking"]:
        raise ValueError("experiment_changes_out_of_scope")
    contract_id = value.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id \
            or Path(contract_id).name != contract_id \
            or contract_id in {".", ".."}:
        raise ValueError("experiment_contract_id_required")
    if contract_id != FROZEN_CONTRACT_ID:
        raise ValueError("experiment_contract_id_not_frozen")
    if "primary_window" in value and value["primary_window"] != 20:
        raise ValueError("primary_window_not_frozen")
    if "confirmation_window" in value and value["confirmation_window"] != 60:
        raise ValueError("confirmation_window_not_frozen")
    if "purge_sessions" in value:
        purge = value["purge_sessions"]
        if isinstance(purge, bool) or not isinstance(purge, int) or purge < 60:
            raise ValueError("purge_window_not_frozen")
    bootstrap = value.get("bootstrap")
    if bootstrap is not None:
        if not isinstance(bootstrap, dict):
            raise ValueError("bootstrap_contract_invalid")
        if bootstrap.get("method") != "moving_trading_session_block_bootstrap":
            raise ValueError("bootstrap_method_not_frozen")
        if bootstrap.get("block_length") != 20 or bootstrap.get("seed") != 20260907 \
                or bootstrap.get("draws") != 2000 or bootstrap.get("confidence") != .95 \
                or bootstrap.get("cross_partition_blocks") is not False:
            raise ValueError("bootstrap_contract_not_frozen")
    if require_schedule:
        _frozen_partition_contract(value)
    return value


def register_experiment(definition, root=DEFAULT_ROOT):
    definition = validate_experiment_definition(definition)
    content = {"schema_version": SCHEMA_VERSION, "definition": definition,
               "state": "draft", "history": [{"state": "draft", "evidence": "registered"}]}
    experiment_id = content_sha256(content)[:16]
    payload = {"experiment_id": experiment_id, "content_sha256": content_sha256(content), "content": content}
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    path = root / f"{experiment_id}.json"
    if path.exists():
        return {"status": "unchanged", "path": str(path), "experiment_id": experiment_id}
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".tmp-"); os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n"); handle.flush(); os.fsync(handle.fileno())
        try: os.link(temporary, path)
        except FileExistsError: pass
    finally:
        try: os.unlink(temporary)
        except OSError: pass
    return {"status": "created", "path": str(path), "experiment_id": experiment_id}


def _validate_transition_evidence(record, state, evidence, root=DEFAULT_ROOT,
                                  experiments_root=DEFAULT_EXPERIMENTS_ROOT,
                                  shadow_root=DEFAULT_SHADOW_ROOT):
    content = (record or {}).get("content") or {}
    # Pre-v2 ad-hoc records remain readable for compatibility.  They are not
    # eligible for publication because publish_experiment resolves a v2
    # registration and requires the structured evidence below.
    if not content.get("definition"):
        return {"status": "legacy_unverified"}
    if content.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("legacy_experiment_unverified")
    if state == "validated":
        if isinstance(evidence, dict) and evidence.get("validation_result") is not None:
            raise ValueError("release_evidence_embedded_result_forbidden")
        payload = _load_result(
            (evidence or {}).get("validation_result_id") if isinstance(evidence, dict)
            else None, experiments_root)
        return _verify_validation_result(
            payload,
            expected_result_id=(evidence or {}).get("validation_result_id")
            if isinstance(evidence, dict) else None,
            require_eligible=False)
    if state == "shadow":
        if isinstance(evidence, dict) and evidence.get("shadow_result") is not None:
            raise ValueError("release_evidence_embedded_result_forbidden")
        payload = _load_result(
            (evidence or {}).get("shadow_result_id") if isinstance(evidence, dict)
            else None, shadow_root)
        return _verify_shadow_result(
            payload,
            expected_result_id=(evidence or {}).get("shadow_result_id")
            if isinstance(evidence, dict) else None)
    if state == "eligible":
        return verify_release_evidence(
            evidence, record.get("experiment_id"), root=root,
            experiments_root=experiments_root, shadow_root=shadow_root)
    if state == "active":
        payload = evidence.get("publish") if isinstance(evidence, dict) else evidence
        return verify_release_evidence(
            payload, record.get("experiment_id"), root=root,
            experiments_root=experiments_root, shadow_root=shadow_root)
    if not evidence:
        raise ValueError("transition_evidence_required")
    return {"status": "accepted"}


def transition(record, state, evidence, verify=True, root=DEFAULT_ROOT,
               experiments_root=DEFAULT_EXPERIMENTS_ROOT,
               shadow_root=DEFAULT_SHADOW_ROOT):
    """Return a new immutable record after a guarded state transition."""
    content = copy.deepcopy((record or {}).get("content") or {})
    current = content.get("state")
    if state not in STATES or state not in _ALLOWED.get(current, set()):
        raise ValueError("invalid_registry_transition")
    if not evidence:
        raise ValueError("transition_evidence_required")
    if verify:
        _validate_transition_evidence(
            record, state, evidence, root=root, experiments_root=experiments_root,
            shadow_root=shadow_root)
    content["state"] = state
    content.setdefault("history", []).append({"state": state, "evidence": copy.deepcopy(evidence)})
    return {"experiment_id": record.get("experiment_id"), "content_sha256": content_sha256(content), "content": content}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_experiment(experiment_id, root=DEFAULT_ROOT):
    """Load the immutable registration envelope, rejecting path traversal."""
    if not isinstance(experiment_id, str) or not experiment_id.isalnum():
        raise ValueError("invalid_experiment_id")
    path = Path(root) / f"{experiment_id}.json"
    if not path.exists():
        raise ValueError("experiment_not_registered")
    record = _read(path)
    if record.get("experiment_id") != experiment_id:
        raise ValueError("registry_identity_mismatch")
    content = record.get("content")
    if not isinstance(content, dict) or record.get("content_sha256") != content_sha256(content):
        raise ValueError("registry_digest_mismatch")
    validate_experiment_definition(content.get("definition"))
    return record


def _is_finite_number(value):
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _is_finite_metric(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _count_at_least(value, minimum):
    return _is_finite_metric(value) and float(value) >= minimum


def _is_safe_result_id(value):
    text = str(value or "")
    return bool(text) and Path(text).name == text and text not in {".", ".."}


def _result_envelope(result, expected_result_id=None):
    """Validate an immutable experiment/shadow result envelope."""
    if not isinstance(result, dict):
        raise ValueError("release_result_missing")
    content = result.get("content")
    if not isinstance(content, dict):
        raise ValueError("release_result_content_missing")
    result_id = str(result.get("experiment_id") or result.get("result_id") or "")
    digest = result.get("content_sha256")
    if not _is_safe_result_id(result_id) or not isinstance(digest, str) \
            or digest != content_sha256(content):
        raise ValueError("release_result_digest_mismatch")
    if expected_result_id and (
            not _is_safe_result_id(expected_result_id)
            or result_id != str(expected_result_id)):
        raise ValueError("release_result_identity_mismatch")
    return result_id, content


def _close_enough(left, right, tolerance=1e-9):
    return _is_finite_metric(left) and _is_finite_metric(right) \
        and abs(float(left) - float(right)) <= tolerance


def _validate_partition_manifest(content, role, expected_role_days):
    """Validate the canonical production input manifest and index outcomes."""
    manifest = content.get("input_manifest")
    inputs = manifest.get("inputs") if isinstance(manifest, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError("evaluation_manifest_inputs_missing")
    required = {
        "partition_role", "partition_dates", "partition_dates_sha256",
        "research_run_ids", "research_content_sha256", "candidate_signal_items",
        "definition", "primary_window", "confirmation_window", "trading_sessions",
    }
    if not required.issubset(inputs):
        raise ValueError("evaluation_manifest_inputs_incomplete")
    expected_dates = sorted(expected_role_days)
    if inputs.get("partition_role") != role \
            or inputs.get("partition_dates") != expected_dates \
            or inputs.get("partition_dates_sha256") != content_sha256(expected_dates):
        raise ValueError("evaluation_manifest_partition_mismatch")
    if inputs.get("definition") != content.get("definition") \
            or inputs.get("primary_window") != content.get("primary_window", 20) \
            or inputs.get("confirmation_window") != content.get("confirmation_window", 60) \
            or inputs.get("trading_sessions") != content.get("trading_sessions"):
        raise ValueError("evaluation_manifest_definition_mismatch")
    for key in ("research_run_ids", "research_content_sha256"):
        values = inputs.get(key)
        if not isinstance(values, list) or values != sorted(values) \
                or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("evaluation_manifest_research_inputs_invalid")
    if not isinstance(manifest.get("input_sha256"), str) \
            or manifest.get("input_sha256") != content_sha256(inputs):
        raise ValueError("evaluation_manifest_digest_mismatch")
    if manifest.get("derived_input_sha256") is not None \
            and manifest.get("derived_input_sha256") != manifest.get("input_sha256"):
        raise ValueError("evaluation_manifest_derived_digest_mismatch")
    if role == "final_holdout" and manifest.get("input_sha256") != manifest.get("derived_input_sha256"):
        raise ValueError("evaluation_manifest_derived_digest_required")

    by_record = {}
    by_identity = {}
    items = inputs.get("candidate_signal_items")
    if not isinstance(items, list):
        raise ValueError("evaluation_manifest_candidates_missing")
    previous_sort_key = None
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("evaluation_manifest_candidate_invalid")
        day = str(item.get("recommendation_date") or "")
        market = str(item.get("market") or item.get("exchange") or "").upper()
        code = str(item.get("code") or "")
        record_id = str(item.get("record_id") or "")
        if day not in expected_role_days or not market or not code or not record_id:
            raise ValueError("evaluation_manifest_candidate_identity_invalid")
        contract_id = item.get("contract_id")
        contract = item.get("evaluation_contract")
        if contract_id != content.get("contract_id") \
                or not isinstance(contract, dict) \
                or contract.get("contract_id") != content.get("contract_id"):
            raise ValueError("evaluation_manifest_candidate_contract_mismatch")
        sort_key = (day, market, code, record_id)
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise ValueError("evaluation_manifest_candidates_not_canonical")
        previous_sort_key = sort_key
        record_key = (record_id, day, market, code)
        identity_key = (day, market, code)
        if record_key in by_record or identity_key in by_identity:
            raise ValueError("evaluation_manifest_candidate_duplicate")
        by_record[record_key] = item
        by_identity[identity_key] = item
    return {"by_record": by_record, "by_identity": by_identity}


def _bind_event_to_manifest(event, window, manifest_index, expected_role_days):
    """Bind one measured event back to its frozen candidate outcome."""
    if not isinstance(event, dict):
        raise ValueError("evaluation_event_row_invalid")
    day = str(event.get("recommendation_date") or "")
    market = str(event.get("market") or "").upper()
    code = str(event.get("code") or "")
    record_id = str(event.get("record_id") or "")
    if day not in expected_role_days or not market or not code or not record_id:
        raise ValueError("evaluation_event_identity_invalid")
    candidate = manifest_index["by_record"].get((record_id, day, market, code))
    if candidate is None:
        candidate = manifest_index["by_identity"].get((day, market, code))
        if candidate is not None and str(candidate.get("record_id") or "") != record_id:
            candidate = None
    if candidate is None:
        raise ValueError("evaluation_event_input_mismatch")
    windows = candidate.get("windows") if isinstance(candidate.get("windows"), dict) else {}
    label = windows.get(str(window))
    if not isinstance(label, dict):
        raise ValueError("evaluation_event_outcome_missing")
    expected_entry = str(label.get("entry_date") or "")
    expected_exit = str(label.get("exit_date") or label.get("mark_date") or "")
    if expected_entry and str(event.get("entry_date") or "") != expected_entry:
        raise ValueError("evaluation_event_outcome_mismatch")
    if expected_exit and str(event.get("exit_date") or "") != expected_exit:
        raise ValueError("evaluation_event_outcome_mismatch")
    complete = event.get("evaluation_complete") is True
    if complete:
        if label.get("status") != "complete" \
                or not _is_finite_metric(label.get("hs300_alpha")) \
                or not _close_enough(event.get("hs300_alpha"), label.get("hs300_alpha")):
            raise ValueError("evaluation_event_alpha_input_mismatch")
        expected_mae = label.get("mae")
        actual_mae = event.get("mae")
        if expected_mae is None:
            if actual_mae is not None:
                raise ValueError("evaluation_event_mae_input_mismatch")
        elif not _is_finite_metric(expected_mae) or not _close_enough(actual_mae, expected_mae):
            raise ValueError("evaluation_event_mae_input_mismatch")
    return candidate


def _raw_evaluation_metrics(content, window=20, require_blocks=True):
    """Recompute release metrics from immutable paired/coverage/event rows.

    A result envelope is content addressed, but a caller could still create a
    self-consistent summary and digest.  Release verification therefore needs
    the primitive rows emitted by the evaluator and checks every derived
    count/mean used by the promotion gates.
    """
    if not isinstance(content, dict):
        raise ValueError("evaluation_content_missing")
    role = "validation" if require_blocks else "final_holdout"

    def evidence_day(value, field):
        text = str(value or "")
        try:
            parsed = date.fromisoformat(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"evaluation_{field}_invalid") from exc
        if parsed.isoformat() != text:
            raise ValueError(f"evaluation_{field}_invalid")
        return text

    sessions = content.get("trading_sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("evaluation_calendar_missing")
    try:
        normalized_sessions = [evidence_day(value, "trading_session") for value in sessions]
    except ValueError:
        raise
    if normalized_sessions != sorted(set(normalized_sessions)):
        raise ValueError("evaluation_calendar_not_sorted")
    session_set = set(normalized_sessions)
    partitions = content.get("partitions")
    if not isinstance(partitions, dict) or partitions.get("status") != "valid":
        raise ValueError("evaluation_partition_contract_missing")
    definition = content.get("definition")
    try:
        definition = validate_experiment_definition(definition, require_schedule=True)
        frozen_partitions = _frozen_partition_contract(definition)
    except ValueError as exc:
        raise ValueError("evaluation_definition_schedule_invalid") from exc
    if normalized_sessions != definition.get("trading_sessions") \
            or partitions != frozen_partitions:
        raise ValueError("evaluation_definition_partition_mismatch")
    validation = partitions.get("validation")
    if not isinstance(validation, dict) \
            or set(validation) != {"validation_1", "validation_2", "validation_3"}:
        raise ValueError("evaluation_validation_partitions_missing")
    partition_days = {}
    for name, raw_days in [("discovery", partitions.get("discovery")),
                           *[(name, validation.get(name)) for name in validation],
                           ("final_holdout", partitions.get("final_holdout"))]:
        if not isinstance(raw_days, list) or not raw_days:
            raise ValueError("evaluation_partition_dates_missing")
        days = [evidence_day(value, "partition_date") for value in raw_days]
        if days != sorted(set(days)) or any(day not in session_set for day in days):
            raise ValueError("evaluation_partition_date_invalid")
        partition_days[name] = days
    purge = partitions.get("purge_sessions", content.get("purge_sessions"))
    if isinstance(purge, bool) or not isinstance(purge, int) or purge < 60:
        raise ValueError("evaluation_partition_purge_invalid")
    session_positions = {day: index for index, day in enumerate(normalized_sessions)}
    ordered_partition_names = ("discovery", "validation_1", "validation_2",
                               "validation_3", "final_holdout")
    for previous, current in zip(ordered_partition_names, ordered_partition_names[1:]):
        gap = (session_positions[partition_days[current][0]]
               - session_positions[partition_days[previous][-1]] - 1)
        if gap < purge:
            raise ValueError("evaluation_partition_purge_invalid")
    freeze_at = partitions.get("freeze_at")
    if not isinstance(freeze_at, str) or freeze_at not in session_set \
            or freeze_at >= partition_days["final_holdout"][0]:
        raise ValueError("evaluation_partition_freeze_invalid")
    used = [day for days in partition_days.values() for day in days]
    if len(used) != len(set(used)):
        raise ValueError("evaluation_partition_overlap")
    expected_role_days = (set(partition_days["final_holdout"])
                          if role == "final_holdout" else
                          set().union(*(set(partition_days[name]) for name in validation)))
    if content.get("partition_role") != role:
        raise ValueError("evaluation_partition_role_mismatch")
    manifest_index = _validate_partition_manifest(content, role, expected_role_days)
    if content.get("partitions", {}).get("all_dates") is not None:
        all_dates = content["partitions"].get("all_dates")
        if all_dates != sorted(set(used)):
            raise ValueError("evaluation_partition_all_dates_mismatch")

    paired = content.get("paired_dates")
    if paired is None:
        paired = content.get("holdout_paired_dates")
    coverage_rows = content.get("coverage_rows")
    if coverage_rows is None:
        coverage_rows = content.get("holdout_coverage_rows")
    event_rows = content.get("event_rows")
    if event_rows is None:
        event_rows = content.get("holdout_event_rows")
    if not isinstance(paired, list) or not isinstance(coverage_rows, list) \
            or not isinstance(event_rows, dict):
        raise ValueError("evaluation_raw_rows_missing")
    seen_dates = set()
    paired_by_date = {}
    for row in paired:
        if not isinstance(row, dict) or not row.get("date"):
            raise ValueError("evaluation_paired_row_invalid")
        day = evidence_day(row["date"], "paired_date")
        if day in seen_dates:
            raise ValueError("evaluation_paired_date_duplicate")
        seen_dates.add(day)
        if day not in expected_role_days:
            raise ValueError("evaluation_paired_date_outside_partition")
        expected_block = ("holdout" if role == "final_holdout" else
                          next((str(index) for index, name in enumerate(
                              ("validation_1", "validation_2", "validation_3"), 1)
                                if day in partition_days[name]), None))
        if str(row.get("block") or "") != expected_block:
            raise ValueError("evaluation_paired_block_mismatch")
        baseline = row.get("baseline_mean")
        treatment = row.get("treatment_mean")
        delta = row.get("delta")
        if not (_is_finite_metric(baseline) and _is_finite_metric(treatment)
                and _is_finite_metric(delta)
                and _close_enough(float(treatment) - float(baseline), delta)):
            raise ValueError("evaluation_paired_row_inconsistent")
        paired_by_date[day] = row
    expected_rows = []
    coverage_by_date = {}
    for row in coverage_rows:
        if not isinstance(row, dict) or not row.get("date"):
            raise ValueError("evaluation_coverage_row_invalid")
        day = evidence_day(row["date"], "coverage_date")
        if day in coverage_by_date:
            raise ValueError("evaluation_coverage_date_duplicate")
        if day not in expected_role_days:
            raise ValueError("evaluation_coverage_date_outside_partition")
        expected_block = ("holdout" if role == "final_holdout" else
                          next((str(index) for index, name in enumerate(
                              ("validation_1", "validation_2", "validation_3"), 1)
                                if day in partition_days[name]), None))
        if str(row.get("block") or "") != expected_block:
            raise ValueError("evaluation_coverage_block_mismatch")
        if not isinstance(row.get("expected_date"), bool):
            raise ValueError("evaluation_coverage_expected_status_missing")
        if row.get("expected_date") is not True:
            raise ValueError("evaluation_coverage_expected_date_false")
        if row.get("status") == "paired" \
                and not (row.get("baseline_complete") and row.get("treatment_complete")):
            raise ValueError("evaluation_coverage_status_inconsistent")
        coverage_by_date[day] = row
        if row["expected_date"]:
            expected_rows.append(row)
        if not isinstance(row.get("baseline_complete"), bool) \
                or not isinstance(row.get("treatment_complete"), bool):
            raise ValueError("evaluation_coverage_status_missing")
        for count_key in ("baseline_count", "treatment_count"):
            count = row.get(count_key)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("evaluation_coverage_count_invalid")
    if set(paired_by_date) - set(coverage_by_date):
        raise ValueError("evaluation_paired_coverage_mismatch")
    if set(coverage_by_date) != expected_role_days:
        raise ValueError("evaluation_coverage_date_set_mismatch")
    paired_coverage = {
        day for day, row in coverage_by_date.items()
        if row.get("status") == "paired"
    }
    if paired_coverage != set(paired_by_date):
        raise ValueError("evaluation_paired_status_mismatch")
    expected_count = len(expected_rows)
    baseline_complete = sum(row["baseline_complete"] for row in expected_rows)
    treatment_complete = sum(row["treatment_complete"] for row in expected_rows)
    coverage = content.get("coverage") if isinstance(content.get("coverage"), dict) else {}
    if coverage.get("baseline_dates") != baseline_complete \
            or coverage.get("treatment_dates") != treatment_complete:
        raise ValueError("evaluation_coverage_count_mismatch")
    for key, value in (
            ("baseline_complete_ratio", baseline_complete / expected_count if expected_count else 0.0),
            ("treatment_complete_ratio", treatment_complete / expected_count if expected_count else 0.0)):
        if not _close_enough(coverage.get(key), value):
            raise ValueError("evaluation_coverage_ratio_mismatch")
    block_counts = content.get("block_pair_counts")
    if block_counts is None:
        block_counts = content.get("holdout_block_pair_counts")
    if not isinstance(block_counts, dict):
        raise ValueError("evaluation_block_counts_missing")
    calculated_blocks = {}
    for row in coverage_rows:
        block = str(row.get("block") or "")
        if row.get("status") == "paired":
            calculated_blocks[block] = calculated_blocks.get(block, 0) + 1
    if {str(key): value for key, value in block_counts.items()} != calculated_blocks:
        raise ValueError("evaluation_block_counts_mismatch")
    if require_blocks and set(calculated_blocks) != {"1", "2", "3"}:
        raise ValueError("evaluation_validation_blocks_missing")

    # Event rows include incomplete outcomes so the earliest frozen overlap is
    # still the anchor.  Re-run the shared event allocator and compare the
    # reported mature-event counts to the resulting anchors with complete
    # outcomes.
    event_metrics = {}
    daily_alpha = {}
    maturity = content.get("maturity") if isinstance(content.get("maturity"), dict) else {}
    for side in ("baseline", "treatment"):
        rows = event_rows.get(side)
        if not isinstance(rows, list):
            raise ValueError("evaluation_event_rows_missing")
        record_ids = [str(row.get("record_id")) for row in rows
                      if isinstance(row, dict) and row.get("record_id")]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("evaluation_event_record_id_duplicate")
        identities = set()
        if any(not isinstance(row, dict) or evidence_day(row.get("recommendation_date"),
                                                         "event_recommendation_date")
               not in expected_role_days for row in rows):
            raise ValueError("evaluation_event_date_outside_partition")
        assigned = assign_research_events(rows, window, market_sessions=normalized_sessions)
        complete_ids = {str(row.get("record_id")) for row in rows
                        if isinstance(row, dict) and row.get("evaluation_complete") is True}
        if any(not isinstance(row, dict) or not row.get("record_id") for row in rows):
            raise ValueError("evaluation_event_record_id_missing")
        for row in rows:
            if not isinstance(row, dict):
                continue
            _bind_event_to_manifest(row, window, manifest_index, expected_role_days)
            identity = (evidence_day(row.get("recommendation_date"),
                                     "event_recommendation_date"),
                        str(row.get("market") or "").upper(), str(row.get("code") or ""))
            if not identity[1] or not identity[2] or identity in identities:
                raise ValueError("evaluation_event_identity_duplicate")
            identities.add(identity)
            entry = evidence_day(row.get("entry_date"), "event_entry_date")
            exit_date = evidence_day(row.get("exit_date"), "event_exit_date")
            positions = {day: index for index, day in enumerate(normalized_sessions)}
            if entry not in positions or exit_date not in positions \
                    or positions[exit_date] - positions[entry] != window - 1:
                raise ValueError("evaluation_event_endpoint_invalid")
            if row.get("evaluation_complete") is True \
                    and not _is_finite_metric(row.get("hs300_alpha")):
                raise ValueError("evaluation_event_alpha_missing")
            if row.get("evaluation_complete") is True:
                day = evidence_day(row.get("recommendation_date"), "event_recommendation_date")
                daily_alpha.setdefault(side, {}).setdefault(day, []).append(
                    float(row["hs300_alpha"]))
        mature = [row for row in assigned["events"]
                  if str(row.get("record_id")) in complete_ids]
        reported = ((maturity.get(side) or {}).get("valid_alpha_events"))
        if reported != len(mature):
            raise ValueError("evaluation_event_count_mismatch")
        event_metrics[side] = {"assigned": assigned, "mature": mature}

    # Paired daily means are derived from the complete primitive event rows.
    # A self-consistent paired summary cannot invent an alpha that was never
    # present in either selected arm.
    for pair in paired:
        day = evidence_day(pair.get("date"), "paired_date")
        coverage_row = coverage_by_date[day]
        for side, mean_key, count_key in (
                ("baseline", "baseline_mean", "baseline_count"),
                ("treatment", "treatment_mean", "treatment_count")):
            rows = event_rows[side]
            date_rows = [row for row in rows
                         if isinstance(row, dict)
                         and str(row.get("recommendation_date")) == day]
            complete_rows = [row for row in date_rows
                             if row.get("evaluation_complete") is True]
            if len(date_rows) != coverage_row.get(count_key) \
                    or len(complete_rows) != len(date_rows):
                raise ValueError("evaluation_paired_event_binding_mismatch")
            values = [float(row["hs300_alpha"]) for row in complete_rows]
            if not values or not _close_enough(sum(values) / len(values), pair.get(mean_key)):
                raise ValueError("evaluation_paired_mean_mismatch")
        expected_delta = float(pair["treatment_mean"]) - float(pair["baseline_mean"])
        if not _close_enough(expected_delta, pair.get("delta")):
            raise ValueError("evaluation_paired_delta_mismatch")
    if maturity.get("valid_alpha_events") != min(
            len(event_metrics["baseline"]["mature"]),
            len(event_metrics["treatment"]["mature"])):
        raise ValueError("evaluation_event_count_mismatch")
    if maturity.get("alpha_mature_dates") != len(paired):
        raise ValueError("evaluation_mature_date_count_mismatch")

    # MAE and 60-day confirmation must be reproducible from primitive rows,
    # rather than copied from a caller-provided summary.
    mae_rows = content.get("mae_rows")
    mae_tails = content.get("mae_tail_5pct")
    if not isinstance(mae_rows, dict) or not isinstance(mae_tails, dict):
        raise ValueError("evaluation_mae_rows_missing")

    def percentile(values, q):
        ordered = sorted(values)
        return ordered[max(0, min(len(ordered) - 1, int(q * (len(ordered) - 1))))] \
            if ordered else None

    for side in ("baseline", "treatment"):
        rows = mae_rows.get(side)
        if not isinstance(rows, list):
            raise ValueError("evaluation_mae_rows_missing")
        values = []
        expected_mae = {}
        for event in event_rows[side]:
            if not isinstance(event, dict) or event.get("evaluation_complete") is not True:
                continue
            mae = event.get("mae")
            if mae is None:
                continue
            if not _is_finite_metric(mae):
                raise ValueError("evaluation_mae_row_invalid")
            key = (str(event.get("record_id")),
                   str(event.get("market") or ""),
                   str(event.get("code") or ""),
                   evidence_day(event.get("recommendation_date"), "mae_date"))
            expected_mae[key] = float(mae)
        supplied_mae = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("evaluation_status") != "complete" \
                    or row.get("side") != side:
                raise ValueError("evaluation_mae_row_invalid")
            day = evidence_day(row.get("recommendation_date"), "mae_date")
            if day not in expected_role_days or not _is_finite_metric(row.get("mae")):
                raise ValueError("evaluation_mae_row_invalid")
            record_id = row.get("record_id")
            key = (str(record_id or ""), str(row.get("market") or ""),
                   str(row.get("code") or ""), day)
            if not record_id or key in supplied_mae or key not in expected_mae \
                    or not _close_enough(row["mae"], expected_mae[key]):
                raise ValueError("evaluation_mae_event_binding_mismatch")
            supplied_mae[key] = float(row["mae"])
            values.append(float(row["mae"]))
        if set(supplied_mae) != set(expected_mae):
            raise ValueError("evaluation_mae_event_binding_mismatch")
        if not _close_enough(mae_tails.get(side), percentile(values, .05)):
            raise ValueError("evaluation_mae_tail_mismatch")

    confirmation = maturity.get("confirmation_60d")
    confirmation_pairs = content.get("confirmation_60d_pairs")
    if require_blocks:
        if not isinstance(confirmation, dict) or not isinstance(confirmation_pairs, list):
            raise ValueError("evaluation_confirmation_rows_missing")
        confirmation_by_date = {}
        for row in confirmation_pairs:
            if not isinstance(row, dict) or not row.get("date"):
                raise ValueError("evaluation_confirmation_pair_invalid")
            day = evidence_day(row["date"], "confirmation_date")
            if day in confirmation_by_date or day not in expected_role_days:
                raise ValueError("evaluation_confirmation_pair_invalid")
            if str(row.get("block") or "") != str(coverage_by_date[day].get("block") or ""):
                raise ValueError("evaluation_confirmation_block_mismatch")
            if not (_is_finite_metric(row.get("baseline_mean"))
                    and _is_finite_metric(row.get("treatment_mean"))
                    and _is_finite_metric(row.get("delta"))
                    and _close_enough(float(row["treatment_mean"])
                                      - float(row["baseline_mean"]), row["delta"])):
                raise ValueError("evaluation_confirmation_pair_invalid")
            confirmation_by_date[day] = row
        confirmation_deltas = []
        confirmation_daily = {}
        for side, field in (("baseline_60d", "baseline_mean"),
                            ("treatment_60d", "treatment_mean")):
            rows = event_rows.get(side)
            if not isinstance(rows, list):
                raise ValueError("evaluation_confirmation_rows_missing")
            record_ids = [str(row.get("record_id")) for row in rows
                          if isinstance(row, dict) and row.get("record_id")]
            if len(record_ids) != len(set(record_ids)):
                raise ValueError("evaluation_confirmation_event_record_id_duplicate")
            identities = set()
            for row in rows:
                if not isinstance(row, dict) or not row.get("record_id"):
                    raise ValueError("evaluation_confirmation_event_record_id_missing")
                _bind_event_to_manifest(row, 60, manifest_index,
                                        expected_role_days)
                day = evidence_day(row.get("recommendation_date"),
                                   "confirmation_event_date")
                if day not in expected_role_days:
                    raise ValueError("evaluation_confirmation_event_date_invalid")
                identity = (day, str(row.get("market") or "").upper(), str(row.get("code") or ""))
                if not identity[1] or not identity[2] or identity in identities:
                    raise ValueError("evaluation_confirmation_event_identity_duplicate")
                identities.add(identity)
                entry = evidence_day(row.get("entry_date"), "confirmation_event_entry_date")
                exit_date = evidence_day(row.get("exit_date"), "confirmation_event_exit_date")
                positions = {value: index for index, value in enumerate(normalized_sessions)}
                if entry not in positions or exit_date not in positions \
                        or positions[exit_date] - positions[entry] != 59:
                    raise ValueError("evaluation_confirmation_event_endpoint_invalid")
                if row.get("evaluation_complete") is True:
                    if not _is_finite_metric(row.get("hs300_alpha")):
                        raise ValueError("evaluation_confirmation_event_alpha_missing")
                    confirmation_daily.setdefault(side, {}).setdefault(day, []).append(
                        float(row["hs300_alpha"]))
        for row in confirmation_pairs:
            day = evidence_day(row.get("date"), "confirmation_date")
            baseline_values = confirmation_daily.get("baseline_60d", {}).get(day, [])
            treatment_values = confirmation_daily.get("treatment_60d", {}).get(day, [])
            coverage_row = coverage_by_date[day]
            expected_baseline_count = coverage_row.get("baseline_count")
            expected_treatment_count = coverage_row.get("treatment_count")
            baseline_rows = [event for event in event_rows["baseline_60d"]
                             if str(event.get("recommendation_date")) == day]
            treatment_rows = [event for event in event_rows["treatment_60d"]
                              if str(event.get("recommendation_date")) == day]
            if len(baseline_rows) != expected_baseline_count \
                    or len(treatment_rows) != expected_treatment_count \
                    or len(baseline_values) != len(baseline_rows) \
                    or len(treatment_values) != len(treatment_rows):
                raise ValueError("evaluation_confirmation_event_binding_mismatch")
            if not baseline_values or not treatment_values \
                    or not _close_enough(sum(baseline_values) / len(baseline_values),
                                         row.get("baseline_mean")) \
                    or not _close_enough(sum(treatment_values) / len(treatment_values),
                                         row.get("treatment_mean")):
                raise ValueError("evaluation_confirmation_mean_mismatch")
            delta = float(row["treatment_mean"]) - float(row["baseline_mean"])
            if not _close_enough(delta, row.get("delta")):
                raise ValueError("evaluation_confirmation_delta_mismatch")
            confirmation_deltas.append(delta)
        confirmation_mean = (sum(confirmation_deltas) / len(confirmation_deltas)
                             if confirmation_deltas else None)
        if confirmation.get("complete_paired_dates") != len(confirmation_pairs) \
                or not _close_enough(confirmation.get("mean_delta"), confirmation_mean):
            raise ValueError("evaluation_confirmation_pair_summary_mismatch")
        for side, field in (("baseline_60d", "baseline_valid_alpha_events"),
                            ("treatment_60d", "treatment_valid_alpha_events")):
            rows = event_rows.get(side)
            assigned = assign_research_events(rows, 60, market_sessions=normalized_sessions)
            complete_ids = {str(row.get("record_id")) for row in rows
                            if row.get("evaluation_complete") is True}
            mature_count = sum(str(row.get("record_id")) in complete_ids
                               for row in assigned["events"])
            if confirmation.get(field) != mature_count:
                raise ValueError("evaluation_confirmation_count_mismatch")

    if require_blocks:
        interval = content.get("interval")
        definition = content.get("definition") if isinstance(content.get("definition"), dict) else {}
        bootstrap = definition.get("bootstrap") if isinstance(definition.get("bootstrap"), dict) else {}
        if not isinstance(interval, dict) or not bootstrap:
            raise ValueError("evaluation_bootstrap_missing")
        # Import lazily to avoid the registry -> experiment -> registry import
        # cycle at module initialization.
        from backtesting.recommendation_experiments import _bootstrap
        expected_interval = _bootstrap(
            [row["delta"] for row in paired], seed=bootstrap.get("seed"),
            draws=bootstrap.get("draws"), block_length=bootstrap.get("block_length"),
            dates=[row["date"] for row in paired],
            partitions=validation, trading_sessions=normalized_sessions)
        for key in ("method", "status", "seed", "draws", "block_length",
                    "valid_block_count", "cross_partition_blocks"):
            if interval.get(key) != expected_interval.get(key):
                raise ValueError("evaluation_bootstrap_mismatch")
        for key in ("lower_95", "upper_95"):
            if not _close_enough(interval.get(key), expected_interval.get(key)):
                raise ValueError("evaluation_bootstrap_mismatch")
    return {
        "paired": paired,
        "coverage_rows": coverage_rows,
        "expected_count": expected_count,
        "baseline_complete": baseline_complete,
        "treatment_complete": treatment_complete,
        "block_counts": calculated_blocks,
        "event_metrics": event_metrics,
        "confirmation_pairs": confirmation_pairs or [],
    }


def _promotion_gates_from_content(content):
    """Recalculate frozen promotion gates from result metrics, never booleans."""
    maturity = content.get("maturity")
    maturity = maturity if isinstance(maturity, dict) else {}
    baseline = maturity.get("baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    treatment = maturity.get("treatment")
    treatment = treatment if isinstance(treatment, dict) else {}
    confirmation = maturity.get("confirmation_60d")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    coverage = content.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    interval = content.get("interval")
    interval = interval if isinstance(interval, dict) else {}
    mae = content.get("mae_tail_5pct")
    mae = mae if isinstance(mae, dict) else {}
    paired = content.get("paired_dates") or []
    if not isinstance(paired, list):
        paired = []
    paired_values_valid = all(
        isinstance(row, dict) and _is_finite_metric(row.get("delta"))
        for row in paired
    )
    block_counts = content.get("block_pair_counts")
    block_counts = block_counts if isinstance(block_counts, dict) else {}
    partitions = content.get("partitions")
    partitions = partitions if isinstance(partitions, dict) else {}
    baseline_events = baseline.get("valid_alpha_events")
    treatment_events = treatment.get("valid_alpha_events")
    baseline_dates = baseline.get("alpha_mature_dates")
    treatment_dates = treatment.get("alpha_mature_dates")
    confirmation_mean = confirmation.get("mean_delta")
    baseline_60_events = confirmation.get("baseline_valid_alpha_events")
    treatment_60_events = confirmation.get("treatment_valid_alpha_events")
    complete_60_dates = confirmation.get("complete_paired_dates")
    baseline_cov = coverage.get("baseline_dates")
    treatment_cov = coverage.get("treatment_dates")
    baseline_mae = mae.get("baseline")
    treatment_mae = mae.get("treatment")
    return {
        "calendar_verified": content.get("calendar_verified") is True,
        "partition_contract": partitions.get("status") == "valid",
        "minimum_events": _count_at_least(baseline_events, 100)
        and _count_at_least(treatment_events, 100),
        "minimum_events_baseline": _count_at_least(baseline_events, 100),
        "minimum_events_treatment": _count_at_least(treatment_events, 100),
        "minimum_dates": _count_at_least(baseline_dates, 20)
        and _count_at_least(treatment_dates, 20)
        and len(paired) >= 20 and paired_values_valid,
        "minimum_dates_per_block": bool(block_counts)
        and all(_count_at_least(value, 20)
                for value in block_counts.values()),
        "three_oos_blocks": set(block_counts) == {"1", "2", "3"},
        "coverage": _count_at_least(baseline_cov, 1)
        and _count_at_least(treatment_cov, .9 * float(baseline_cov))
        if _is_finite_metric(baseline_cov) else False,
        "pair_completeness_95pct": (
            _count_at_least(coverage.get("baseline_complete_ratio"), .95)
            and _count_at_least(coverage.get("treatment_complete_ratio"), .95)
        ),
        "ci_lower_positive": _is_finite_metric(interval.get("lower_95"))
        and float(interval.get("lower_95")) > 0
        and _count_at_least(interval.get("valid_block_count", 0), 2),
        "mae_tail": _is_finite_metric(baseline_mae)
        and _is_finite_metric(treatment_mae)
        and float(treatment_mae) >= float(baseline_mae) - .01,
        "confirmation_60d": (
            _count_at_least(baseline_60_events, 100)
            and _count_at_least(treatment_60_events, 100)
            and _count_at_least(complete_60_dates, 20)
            and _is_finite_metric(confirmation_mean)
            and float(confirmation_mean) >= 0
        ),
    }


def _verify_validation_result(result, expected_result_id=None,
                              require_eligible=False):
    result_id, content = _result_envelope(result, expected_result_id=expected_result_id)
    if content.get("schema_version") != "recommendation-experiment/v2":
        raise ValueError("validation_result_schema_invalid")
    validate_experiment_definition(content.get("definition"))
    if not isinstance(content.get("experiment_id"), str) or not content.get("experiment_id"):
        raise ValueError("validation_result_experiment_missing")
    if not isinstance(content.get("input_manifest"), dict) \
            or not isinstance(content["input_manifest"].get("input_sha256"), str) \
            or not content["input_manifest"].get("input_sha256"):
        raise ValueError("validation_result_manifest_missing")
    if require_eligible:
        _raw_evaluation_metrics(content, window=20, require_blocks=True)
        marker = content.get("holdout")
        partitions = content.get("partitions") or {}
        if not isinstance(marker, dict) or marker.get("status") != "unconsumed" \
                or marker.get("dates") != partitions.get("final_holdout") \
                or marker.get("freeze_at") != partitions.get("freeze_at"):
            raise ValueError("validation_holdout_state_invalid")
    gates = _promotion_gates_from_content(content)
    if require_eligible:
        # The summary's ``promotion.eligible`` flag is advisory only.  A
        # caller cannot make a release eligible by asserting a boolean; the
        # gate values above are recomputed from the measured content.
        if not all(gates.values()):
            raise ValueError("validation_gates_not_satisfied")
    return {"result_id": result_id, "content": content, "gates": gates}


def _holdout_gates_from_content(content, raw):
    """Apply only the frozen final-holdout confirmation gates.

    The holdout is a single post-freeze confirmation interval.  It is not a
    fourth validation block, so it must not be forced to carry validation
    bootstrap/three-block/60-day evidence.
    """
    partitions = content.get("partitions") if isinstance(content.get("partitions"), dict) else {}
    coverage = content.get("coverage") if isinstance(content.get("coverage"), dict) else {}
    mae = content.get("mae_tail_5pct") if isinstance(content.get("mae_tail_5pct"), dict) else {}
    paired = raw["paired"]
    deltas = [float(row["delta"]) for row in paired]
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    baseline_dates = coverage.get("baseline_dates")
    treatment_dates = coverage.get("treatment_dates")
    return {
        "calendar_verified": content.get("calendar_verified") is True,
        "partition_contract": partitions.get("status") == "valid",
        "minimum_complete_paired_dates": len(paired) >= 20,
        "primary_delta_positive": _is_finite_metric(mean_delta) and mean_delta > 0,
        "coverage": _count_at_least(baseline_dates, 1)
        and _count_at_least(treatment_dates, .9 * float(baseline_dates))
        if _is_finite_metric(baseline_dates) else False,
        "pair_completeness_95pct": (
            _count_at_least(coverage.get("baseline_complete_ratio"), .95)
            and _count_at_least(coverage.get("treatment_complete_ratio"), .95)
        ),
        "mae_tail": _is_finite_metric(mae.get("baseline"))
        and _is_finite_metric(mae.get("treatment"))
        and float(mae["treatment"]) >= float(mae["baseline"]) - .01,
    }


def _normalize_shadow_selection(value):
    """Canonicalize one forward source's frozen replay selection."""
    if not isinstance(value, dict):
        raise ValueError("shadow_source_selection_missing")
    top = value.get("top")
    try:
        min_score = float(value.get("min_score"))
    except (TypeError, ValueError):
        min_score = None
    if isinstance(top, bool) or not isinstance(top, int) or top < 1 \
            or min_score is None or not math.isfinite(min_score) \
            or not isinstance(value.get("policy"), dict):
        raise ValueError("shadow_source_selection_invalid")
    normalized = {"top": top, "min_score": min_score,
                  "policy": copy.deepcopy(value["policy"])}
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
        normalized[side] = {"selected": selected, "buckets": buckets}
    return normalized


def _verify_holdout_result(result, expected_result_id=None):
    result_id, content = _result_envelope(result, expected_result_id=expected_result_id)
    if content.get("schema_version") != "recommendation-experiment/v2":
        raise ValueError("holdout_result_schema_invalid")
    validate_experiment_definition(content.get("definition"))
    if not isinstance(content.get("experiment_id"), str) or not content.get("experiment_id"):
        raise ValueError("holdout_result_experiment_missing")
    if not isinstance(content.get("input_manifest"), dict) \
            or not isinstance(content["input_manifest"].get("input_sha256"), str) \
            or not content["input_manifest"].get("input_sha256"):
        raise ValueError("holdout_result_manifest_missing")
    holdout_marker = content.get("holdout")
    if not isinstance(holdout_marker, dict) \
            or holdout_marker.get("status") not in ("consumed", "evaluated", "complete") \
            or not isinstance(holdout_marker.get("consumption_id"), str) \
            or not holdout_marker.get("consumption_id") \
            or not isinstance(holdout_marker.get("research_batch_id"), str) \
            or not holdout_marker.get("research_batch_id"):
        raise ValueError("holdout_result_not_consumed")
    partitions = content.get("partitions") or {}
    if holdout_marker.get("dates") != partitions.get("final_holdout") \
            or holdout_marker.get("freeze_at") != partitions.get("freeze_at"):
        raise ValueError("holdout_partition_binding_invalid")
    raw = _raw_evaluation_metrics(content, window=20, require_blocks=False)
    gates = _holdout_gates_from_content(content, raw)
    if not all(gates.values()):
        raise ValueError("holdout_gates_not_satisfied")
    return {"result_id": result_id, "content": content, "gates": gates}


def _verify_shadow_result(result, expected_result_id=None):
    flat = isinstance(result, dict) and isinstance(result.get("content"), dict) is False \
        and result.get("schema_version") in {
            "recommendation-shadow/v1", "recommendation-strategy-shadow/v1",
            "market-shadow-candidate-run/v1",
        }
    if flat:
        raw = copy.deepcopy(result)
        digest = raw.get("content_sha256")
        input_digest = raw.get("input_digest")
        raw.pop("content_sha256", None)
        raw.pop("input_digest", None)
        if not isinstance(digest, str) or input_digest != digest \
                or digest != content_sha256(raw):
            raise ValueError("shadow_result_digest_mismatch")
        result_id = str(result.get("result_id") or digest)
        if expected_result_id and not digest.startswith(str(expected_result_id)) \
                and result_id != str(expected_result_id):
            raise ValueError("release_result_identity_mismatch")
        content = raw
        source_schema = content.get("schema_version")
        if source_schema == "recommendation-strategy-shadow/v1":
            evaluation = content.get("evaluation")
            if not isinstance(evaluation, dict):
                # The daily producer's ranking-only snapshot is deliberately
                # not release evidence.  A separate forward evaluator must
                # append measured paired outcomes under this section.
                raise ValueError("shadow_evaluation_missing")
            # Merge only that versioned evaluation section before applying the
            # release gates, while retaining the source digest.
            content.update(copy.deepcopy(evaluation))
            content["source_schema_version"] = source_schema
            content["schema_version"] = "recommendation-shadow/v1"
    else:
        result_id, content = _result_envelope(result, expected_result_id=expected_result_id)
        if content.get("schema_version") == "recommendation-strategy-shadow/v1":
            evaluation = content.get("evaluation")
            if isinstance(evaluation, dict):
                content = copy.deepcopy(content)
                content.update(copy.deepcopy(evaluation))
            content["source_schema_version"] = "recommendation-strategy-shadow/v1"
            content["schema_version"] = "recommendation-shadow/v1"
    schema = str(content.get("schema_version") or "")
    if schema != "recommendation-shadow/v1" \
            or content.get("shadow_type", "forward") != "forward":
        raise ValueError("shadow_result_schema_invalid")
    if content.get("formal_policy_affected") is not False:
        raise ValueError("shadow_formal_policy_affected")
    if content.get("retrospective") is True or content.get("independent_snapshot") is not True:
        raise ValueError("shadow_result_not_forward_independent")
    if content.get("evaluation_status") != "forward_aggregated":
        raise ValueError("shadow_evaluation_not_aggregated")
    if not isinstance(content.get("definition"), dict) \
            or not isinstance(content.get("experiment_id"), str) \
            or not content.get("experiment_id") \
            or not isinstance(content.get("contract_id"), str) \
            or not content.get("contract_id"):
        raise ValueError("shadow_binding_missing")
    shadow_definition = validate_experiment_definition(content["definition"])
    if content["contract_id"] != shadow_definition["contract_id"]:
        raise ValueError("shadow_contract_mismatch")
    complete_dates = content.get("complete_paired_dates")
    if complete_dates is None:
        complete_dates = (content.get("maturity") or {}).get("complete_paired_dates")
    mature_events = content.get("valid_alpha_events")
    if mature_events is None:
        mature_events = (content.get("maturity") or {}).get("valid_alpha_events")
    if not _count_at_least(complete_dates, 20) \
            or not _count_at_least(mature_events, 100):
        raise ValueError("shadow_maturity_insufficient")
    paired = content.get("paired_dates") or []
    if not isinstance(paired, list) or not paired:
        raise ValueError("shadow_raw_rows_missing")
    paired_dates = set()
    deltas = []
    for row in paired:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValueError("shadow_paired_row_inconsistent")
        try:
            if date.fromisoformat(row["date"]).isoformat() != row["date"]:
                raise ValueError
        except ValueError as exc:
            raise ValueError("shadow_paired_row_inconsistent") from exc
        if row["date"] in paired_dates:
            raise ValueError("shadow_paired_date_duplicate")
        paired_dates.add(row["date"])
        if not isinstance(row, dict) \
                or not (_is_finite_metric(row.get("baseline_mean"))
                        and _is_finite_metric(row.get("treatment_mean"))
                        and _is_finite_metric(row.get("delta"))) \
                or not _close_enough(float(row["treatment_mean"])
                                     - float(row["baseline_mean"]), row["delta"]):
            raise ValueError("shadow_paired_row_inconsistent")
        deltas.append(float(row["delta"]))
    if _is_finite_metric(complete_dates) and int(float(complete_dates)) != len(paired):
        raise ValueError("shadow_mature_date_count_mismatch")
    coverage_rows = content.get("coverage_rows")
    if not isinstance(coverage_rows, list):
        raise ValueError("shadow_coverage_rows_missing")
    coverage_by_date = {}
    for row in coverage_rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValueError("shadow_coverage_row_invalid")
        day = row["date"]
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError as exc:
            raise ValueError("shadow_coverage_row_invalid") from exc
        if day in coverage_by_date:
            raise ValueError("shadow_coverage_date_duplicate")
        if not isinstance(row.get("baseline_complete"), bool) \
                or not isinstance(row.get("treatment_complete"), bool):
            raise ValueError("shadow_coverage_status_missing")
        if row.get("status") == "paired" \
                and not (row["baseline_complete"] and row["treatment_complete"]):
            raise ValueError("shadow_coverage_status_inconsistent")
        coverage_by_date[day] = row
    if set(coverage_by_date) != paired_dates \
            or {day for day, row in coverage_by_date.items()
                if row.get("status") == "paired"} != paired_dates:
        raise ValueError("shadow_coverage_pair_mismatch")
    mean_delta = sum(deltas) / len(deltas)
    if mean_delta < 0 or not _is_finite_metric(content.get("mean_delta")) \
            or not _close_enough(content["mean_delta"], mean_delta):
        raise ValueError("shadow_mean_delta_negative")
    coverage = content.get("coverage") if isinstance(content.get("coverage"), dict) else {}
    baseline_dates = coverage.get("baseline_dates")
    treatment_dates = coverage.get("treatment_dates")
    if not (_count_at_least(baseline_dates, 1)
            and _count_at_least(treatment_dates, .9 * float(baseline_dates))):
        raise ValueError("shadow_coverage_insufficient")
    if not (_count_at_least(coverage.get("baseline_complete_ratio"), .95)
            and _count_at_least(coverage.get("treatment_complete_ratio"), .95)):
        raise ValueError("shadow_pair_completeness_insufficient")
    expected_count = len(coverage_by_date)
    baseline_complete = sum(row["baseline_complete"] for row in coverage_by_date.values())
    treatment_complete = sum(row["treatment_complete"] for row in coverage_by_date.values())
    if coverage.get("baseline_dates") != baseline_complete \
            or coverage.get("treatment_dates") != treatment_complete \
            or not _close_enough(coverage.get("baseline_complete_ratio"),
                                 baseline_complete / expected_count if expected_count else 0.0) \
            or not _close_enough(coverage.get("treatment_complete_ratio"),
                                 treatment_complete / expected_count if expected_count else 0.0):
        raise ValueError("shadow_coverage_summary_mismatch")
    if not isinstance(content.get("input_manifest"), dict) \
            or not isinstance(content["input_manifest"].get("input_sha256"), str) \
            or not content["input_manifest"].get("input_sha256"):
        raise ValueError("shadow_manifest_missing")
    shadow_inputs = content["input_manifest"].get("inputs")
    if not isinstance(shadow_inputs, dict) \
            or shadow_inputs.get("partition_role") != "forward_shadow":
        raise ValueError("shadow_manifest_role_missing")
    if content["input_manifest"].get("input_sha256") != content_sha256(shadow_inputs):
        raise ValueError("shadow_manifest_digest_mismatch")
    source_runs = content.get("source_runs")
    if not isinstance(source_runs, list) or not source_runs:
        raise ValueError("shadow_source_runs_missing")
    source_dates = set()
    normalized_source_runs = []
    source_selection_by_date = {}
    for source in source_runs:
        if not isinstance(source, dict) \
                or not isinstance(source.get("basis_date"), str) \
                or source.get("snapshot_type") != "formal" \
                or not isinstance(source.get("content_sha256"), str) \
                or not source.get("content_sha256") \
                or not isinstance(source.get("input_sha256"), str) \
                or not source.get("input_sha256"):
            raise ValueError("shadow_source_run_invalid")
        source_date = source["basis_date"]
        try:
            if date.fromisoformat(source_date).isoformat() != source_date:
                raise ValueError
        except ValueError as exc:
            raise ValueError("shadow_source_run_invalid") from exc
        if source_date in source_dates:
            raise ValueError("shadow_source_date_duplicate")
        source_dates.add(source_date)
        selection = _normalize_shadow_selection(source.get("selection"))
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, dict) \
                or snapshot.get("schema_version") != "recommendation-strategy-shadow/v1" \
                or snapshot.get("basis_date") != source_date \
                or snapshot.get("snapshot_type") != "formal" \
                or snapshot.get("selection") is None \
                or _normalize_shadow_selection(snapshot.get("selection")) != selection \
                or content_sha256(snapshot) != source.get("content_sha256"):
            raise ValueError("shadow_source_snapshot_mismatch")
        snapshot_manifest = snapshot.get("input_manifest")
        if not isinstance(snapshot_manifest, dict) \
                or not isinstance(snapshot_manifest.get("inputs"), dict) \
                or snapshot_manifest.get("input_sha256") != content_sha256(snapshot_manifest["inputs"]) \
                or snapshot_manifest.get("input_sha256") != source.get("input_sha256"):
            raise ValueError("shadow_source_snapshot_manifest_mismatch")
        source_selection_by_date[source_date] = selection
        normalized_source_runs.append({
            "basis_date": source_date,
            "snapshot_type": "formal",
            "content_sha256": source["content_sha256"],
            "input_sha256": source["input_sha256"],
            "selection": selection,
            "snapshot": snapshot,
        })
    expected_source_runs = sorted(normalized_source_runs, key=lambda item: item["basis_date"])
    if source_runs != expected_source_runs:
        raise ValueError("shadow_source_runs_not_canonical")
    if shadow_inputs.get("source_runs") != expected_source_runs:
        raise ValueError("shadow_source_manifest_mismatch")
    if paired_dates != source_dates:
        raise ValueError("shadow_source_date_binding_mismatch")
    if set(coverage_by_date) != source_dates:
        raise ValueError("shadow_source_date_binding_mismatch")
    for day, row in coverage_by_date.items():
        selection = source_selection_by_date.get(day) or {}
        for side in ("baseline", "treatment"):
            count = row.get(f"{side}_count")
            selected = ((selection.get(side) or {}).get("selected") or [])
            if isinstance(count, bool) or not isinstance(count, int) \
                    or count != len(selected):
                raise ValueError("shadow_selection_coverage_mismatch")
    sessions = content.get("trading_sessions")
    shadow_events = content.get("event_rows")
    if not isinstance(sessions, list) or not sessions or not isinstance(shadow_events, dict):
        raise ValueError("shadow_event_rows_missing")
    if any(not isinstance(value, str) for value in sessions):
        raise ValueError("shadow_calendar_invalid")
    try:
        if [date.fromisoformat(value).isoformat() for value in sessions] != sorted(set(sessions)):
            raise ValueError
    except ValueError as exc:
        raise ValueError("shadow_calendar_invalid") from exc
    event_counts = {}
    shadow_daily_alpha = {}
    session_positions = {str(value): index for index, value in enumerate(sessions)}
    for side in ("baseline", "treatment"):
        rows = shadow_events.get(side)
        if not isinstance(rows, list):
            raise ValueError("shadow_event_rows_missing")
        selected_by_date = {
            day: {(item["market"], item["code"])
                  for item in source_selection_by_date[day][side]["selected"]}
            for day in source_dates
        }
        event_keys_by_date = {day: set() for day in source_dates}
        event_rows_by_date = {day: {} for day in source_dates}
        record_ids = set()
        identities = set()
        for row in rows:
            if not isinstance(row, dict) or not row.get("record_id") \
                    or row.get("record_id") in record_ids:
                raise ValueError("shadow_event_row_invalid")
            record_ids.add(row["record_id"])
            if row.get("evaluation_complete") is True \
                    and not _is_finite_metric(row.get("hs300_alpha")):
                raise ValueError("shadow_event_alpha_missing")
            recommendation_date = str(row.get("recommendation_date") or "")
            try:
                if date.fromisoformat(recommendation_date).isoformat() != recommendation_date:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("shadow_event_date_invalid") from exc
            if recommendation_date not in source_dates:
                raise ValueError("shadow_source_date_binding_mismatch")
            identity = (recommendation_date, str(row.get("market") or "").upper(),
                        str(row.get("code") or ""))
            if not identity[1] or not identity[2] or identity in identities:
                raise ValueError("shadow_event_identity_invalid")
            identities.add(identity)
            key = (identity[1], identity[2])
            if key not in selected_by_date[recommendation_date]:
                raise ValueError("shadow_selection_event_mismatch")
            if key in event_keys_by_date[recommendation_date]:
                raise ValueError("shadow_selection_event_duplicate")
            event_keys_by_date[recommendation_date].add(key)
            event_rows_by_date[recommendation_date][key] = row
            entry = str(row.get("entry_date") or "")
            exit_date = str(row.get("exit_date") or "")
            if entry not in session_positions or exit_date not in session_positions \
                    or session_positions[exit_date] - session_positions[entry] != 19:
                raise ValueError("shadow_event_endpoint_invalid")
            if row.get("evaluation_complete") is True:
                shadow_daily_alpha.setdefault(side, {}).setdefault(
                    recommendation_date, []).append(float(row["hs300_alpha"]))
        if any(event_keys_by_date.get(day, set()) != selected
               for day, selected in selected_by_date.items()):
            raise ValueError("shadow_selection_event_set_mismatch")
        for day, selected in selected_by_date.items():
            if any(event_rows_by_date[day][key].get("evaluation_complete") is not True
                   or not _is_finite_metric(event_rows_by_date[day][key].get("hs300_alpha"))
                   for key in selected):
                raise ValueError("shadow_selection_event_incomplete")
        assigned = assign_research_events(rows, 20, market_sessions=sessions)
        complete_ids = {str(row["record_id"]) for row in rows
                        if row.get("evaluation_complete") is True}
        event_counts[side] = sum(str(row.get("record_id")) in complete_ids
                                 for row in assigned["events"])
    for row in paired:
        day = row["date"]
        baseline_values = shadow_daily_alpha.get("baseline", {}).get(day, [])
        treatment_values = shadow_daily_alpha.get("treatment", {}).get(day, [])
        if not baseline_values or not treatment_values \
                or not _close_enough(sum(baseline_values) / len(baseline_values),
                                     row.get("baseline_mean")) \
                or not _close_enough(sum(treatment_values) / len(treatment_values),
                                     row.get("treatment_mean")):
            raise ValueError("shadow_paired_mean_mismatch")
    if int(float(mature_events)) != min(event_counts["baseline"], event_counts["treatment"]):
        raise ValueError("shadow_event_count_mismatch")
    return {"result_id": result_id, "content": content}


def _load_result(result_id, root):
    if isinstance(result_id, dict):
        return result_id
    if not _is_safe_result_id(result_id):
        raise ValueError("release_result_id_missing")
    root = Path(root)
    direct = root / f"{result_id}.json"
    paths = [direct] if direct.exists() else sorted(root.rglob("*.json"))
    for path in paths:
        try:
            payload = _read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        identity = str(payload.get("experiment_id") or payload.get("result_id") or "")
        if identity == str(result_id):
            return payload
        # ``market_shadow_snapshot`` names files by the full content digest;
        # release evidence may carry its stable short prefix instead.
        digest = str(payload.get("content_sha256") or "")
        if digest and digest.startswith(str(result_id)):
            return payload
    raise ValueError("release_result_not_found")


def verify_release_evidence(evidence, experiment_id, root=DEFAULT_ROOT,
                            experiments_root=DEFAULT_EXPERIMENTS_ROOT,
                            shadow_root=DEFAULT_SHADOW_ROOT):
    """Verify complete v2 evidence before ``eligible`` or ``active``."""
    if not isinstance(evidence, dict) \
            or evidence.get("schema_version") != RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("release_evidence_schema_required")
    if str(evidence.get("experiment_id") or "") != str(experiment_id):
        raise ValueError("release_evidence_experiment_mismatch")
    for key in ("validation_result_id", "holdout_result_id", "shadow_result_id", "contract_id"):
        if not isinstance(evidence.get(key), str) or not evidence[key]:
            raise ValueError(f"release_evidence_{key}_missing")
    current = load_experiment(experiment_id, root)
    definition = (current.get("content") or {}).get("definition") or {}
    # Release evidence is an attestation over immutable stored results.  Do
    # not trust caller-supplied inline summaries, even when they carry a
    # self-computed digest; IDs must resolve through the controlled roots.
    if any(evidence.get(key) is not None
           for key in ("validation_result", "holdout_result", "shadow_result")):
        raise ValueError("release_evidence_embedded_result_forbidden")
    validation_payload = _load_result(evidence["validation_result_id"], experiments_root)
    holdout_payload = _load_result(evidence["holdout_result_id"], experiments_root)
    shadow_payload = _load_result(evidence["shadow_result_id"], shadow_root)
    validation = _verify_validation_result(
        validation_payload, expected_result_id=evidence["validation_result_id"],
        require_eligible=True)
    holdout = _verify_holdout_result(
        holdout_payload, expected_result_id=evidence["holdout_result_id"])
    shadow = _verify_shadow_result(
        shadow_payload, expected_result_id=evidence["shadow_result_id"])
    result_ids = {validation["result_id"], holdout["result_id"], shadow["result_id"]}
    if len(result_ids) != 3:
        raise ValueError("release_evidence_result_identity_reused")
    definition_contract = definition.get("contract_id")
    if not isinstance(definition_contract, str) or not definition_contract:
        raise ValueError("release_evidence_contract_mismatch")
    for result in (validation, holdout):
        if (result["content"].get("definition") or {}) != definition:
            raise ValueError("release_evidence_definition_mismatch")
        bound_experiment = result["content"].get("experiment_id")
        if bound_experiment != str(experiment_id):
            raise ValueError("release_evidence_experiment_mismatch")
        if result["content"].get("contract_id") != definition_contract:
            raise ValueError("release_evidence_contract_mismatch")
    shadow_definition = shadow["content"].get("definition")
    if not isinstance(shadow_definition, dict) or shadow_definition != definition:
        raise ValueError("release_evidence_definition_mismatch")
    shadow_experiment = shadow["content"].get("experiment_id")
    if shadow_experiment != str(experiment_id):
        raise ValueError("release_evidence_experiment_mismatch")
    validation_input = (validation["content"].get("input_manifest") or {}).get("input_sha256")
    holdout_input = (holdout["content"].get("input_manifest") or {}).get("input_sha256")
    shadow_input = (shadow["content"].get("input_manifest") or {}).get("input_sha256")
    if not validation_input or not holdout_input or validation_input == holdout_input:
        raise ValueError("validation_holdout_input_reused")
    if shadow_input in {validation_input, holdout_input}:
        raise ValueError("shadow_input_not_independent")
    validation_manifest = validation["content"].get("input_manifest") or {}
    holdout_manifest = holdout["content"].get("input_manifest") or {}
    validation_inputs = validation_manifest.get("inputs") if isinstance(validation_manifest, dict) else {}
    holdout_inputs = holdout_manifest.get("inputs") if isinstance(holdout_manifest, dict) else {}
    if validation_inputs.get("partition_role") != "validation" \
            or holdout_inputs.get("partition_role") != "final_holdout":
        raise ValueError("release_evidence_partition_role_mismatch")
    for key, expected in (("validation_input_sha256", validation_input),
                          ("holdout_input_sha256", holdout_input),
                          ("shadow_input_sha256", shadow_input)):
        supplied = evidence.get(key)
        if supplied is not None and supplied != expected:
            raise ValueError("release_evidence_input_mismatch")
    contract_ids = {
        evidence["contract_id"],
        (validation["content"].get("contract_id") or ""),
        (holdout["content"].get("contract_id") or ""),
    }
    shadow_contract = shadow["content"].get("contract_id")
    if shadow_contract is None:
        raise ValueError("release_evidence_contract_mismatch")
    contract_ids.add(shadow_contract or "")
    if len(contract_ids) != 1:
        raise ValueError("release_evidence_contract_mismatch")
    consumption = evidence.get("holdout_consumption")
    consumption_id = evidence.get("holdout_consumption_id")
    if consumption_id is None and isinstance(consumption, dict):
        consumption_id = consumption.get("consumption_id")
    if not consumption_id:
        raise ValueError("holdout_consumption_missing")
    stored_consumption = None
    if consumption_id:
        stored_consumption = load_holdout_consumption(consumption_id, root=root)
        if isinstance(consumption, dict):
            for key in ("experiment_id", "research_batch_id", "input_manifest_sha256",
                        "result_id", "parameters"):
                if key in consumption and consumption[key] != stored_consumption.get(key):
                    raise ValueError("holdout_consumption_mismatch")
        consumption = stored_consumption
    operation_status = (evidence.get("holdout_consumption") or {}).get("status") \
        if isinstance(evidence.get("holdout_consumption"), dict) else None
    if not isinstance(consumption, dict) or (
            consumption.get("status") not in ("reserved", "completed")
            and operation_status not in ("created", "unchanged")):
        raise ValueError("holdout_consumption_missing")
    if consumption.get("experiment_id") != str(experiment_id):
        raise ValueError("holdout_consumption_experiment_mismatch")
    if consumption.get("result_id") not in (None, evidence["holdout_result_id"]):
        raise ValueError("holdout_consumption_result_mismatch")
    holdout_input = (holdout["content"].get("input_manifest") or {}).get("input_sha256")
    if consumption.get("input_manifest_sha256") not in (None, holdout_input):
        raise ValueError("holdout_consumption_input_mismatch")
    holdout_marker = holdout["content"].get("holdout")
    if not isinstance(holdout_marker, dict) \
            or holdout_marker.get("status") not in ("consumed", "evaluated", "complete"):
        raise ValueError("holdout_result_not_consumed")
    marker_id = holdout_marker.get("consumption_id")
    if marker_id != consumption_id:
        raise ValueError("holdout_consumption_id_mismatch")
    marker_batch = holdout_marker.get("research_batch_id")
    if marker_batch != consumption.get("research_batch_id"):
        raise ValueError("holdout_consumption_batch_mismatch")
    manifest_consumption_id = (holdout["content"].get("input_manifest") or {}).get(
        "holdout_consumption_id")
    if manifest_consumption_id != consumption_id:
        raise ValueError("holdout_consumption_id_mismatch")
    manifest_batch = holdout_manifest.get("holdout_research_batch_id")
    if manifest_batch != consumption.get("research_batch_id"):
        raise ValueError("holdout_consumption_batch_mismatch")
    manifest_input = holdout_manifest.get("holdout_input_manifest_sha256")
    if manifest_input != holdout_input or manifest_input != consumption.get("input_manifest_sha256"):
        raise ValueError("holdout_consumption_input_mismatch")
    return {"status": "verified", "experiment_id": str(experiment_id),
            "validation_result_id": validation["result_id"],
            "holdout_result_id": holdout["result_id"],
            "shadow_result_id": shadow["result_id"],
            "contract_id": evidence["contract_id"],
            "gates": validation["gates"]}


def _event_root(experiment_id, root):
    return Path(root) / "events" / experiment_id


def _holdout_root(root):
    return Path(root) / "holdout_consumption"


def _holdout_claim_root(root):
    return _holdout_root(root) / "claims"


def _append_holdout_attempt(directory, fields, status, reason):
    """Append a content-addressed attempt audit without changing the reserve."""
    attempts = Path(directory) / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    content = {"schema_version": HOLDOUT_SCHEMA_VERSION,
               "attempt_type": "holdout_consumption_attempt",
               "status": status, "reason": reason, **copy.deepcopy(fields)}
    attempt_id = content_sha256(content)[:16]
    payload = {"attempt_id": attempt_id,
               "content_sha256": content_sha256(content), "content": content}
    path = attempts / f"{attempt_id}.json"
    if path.exists():
        return path
    fd, temporary_name = tempfile.mkstemp(dir=attempts, prefix=f".{attempt_id}.tmp-")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(payload) + b"\n")
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return path


def register_holdout_consumption(experiment_id, research_batch_id,
                                 input_manifest_sha256, result_id,
                                 parameters, root=DEFAULT_ROOT):
    """Atomically reserve a frozen holdout input exactly once.

    Retries with the same batch/input/parameters are idempotent.  A changed
    result or parameter set for an already-consumed frozen input is rejected so
    a holdout cannot be used as an iterative tuning set.
    """
    fields = {
        "experiment_id": str(experiment_id or ""),
        "research_batch_id": str(research_batch_id or ""),
        "input_manifest_sha256": str(input_manifest_sha256 or ""),
        "result_id": str(result_id or ""),
        "parameters": copy.deepcopy(parameters),
    }
    if not all(fields[key] for key in ("experiment_id", "research_batch_id",
                                       "input_manifest_sha256", "result_id")) \
            or not _is_safe_result_id(fields["experiment_id"]) \
            or not _is_safe_result_id(fields["result_id"]):
        raise ValueError("holdout_consumption_identity_missing")
    if not isinstance(fields["parameters"], dict):
        raise ValueError("holdout_consumption_parameters_missing")
    directory = _holdout_root(root) / fields["experiment_id"]
    directory.mkdir(parents=True, exist_ok=True)
    # The once-only key is global to the frozen batch/input, not scoped to an
    # experiment.  A content-addressed hard link makes the claim operation
    # atomic for concurrent evaluators.
    claim_content = {
        "schema_version": HOLDOUT_SCHEMA_VERSION,
        "claim_key": content_sha256({
            "input_manifest_sha256": fields["input_manifest_sha256"],
        })[:32],
        **fields,
        "status": "reserved",
    }
    claim_id = claim_content["claim_key"]
    claims = _holdout_claim_root(root)
    claims.mkdir(parents=True, exist_ok=True)
    claim_path = claims / f"{claim_id}.json"
    claim_payload = {"claim_id": claim_id,
                     "content_sha256": content_sha256(claim_content),
                     "content": claim_content}
    claim_created = False
    if not claim_path.exists():
        fd, temporary_claim_name = tempfile.mkstemp(
            dir=claims, prefix=f".{claim_id}.tmp-")
        os.close(fd)
        temporary_claim = Path(temporary_claim_name)
        try:
            temporary_claim.write_bytes(canonical_json(claim_payload) + b"\n")
            os.link(temporary_claim, claim_path)
            claim_created = True
        except FileExistsError:
            pass
        finally:
            temporary_claim.unlink(missing_ok=True)
    try:
        existing_claim = _read(claim_path)
        previous_claim = existing_claim.get("content") if isinstance(existing_claim, dict) else None
        if not isinstance(previous_claim, dict) \
                or existing_claim.get("claim_id") != claim_id \
                or existing_claim.get("content_sha256") != content_sha256(previous_claim):
            raise ValueError("holdout_claim_digest_mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        # If the process that won the link crashed before the claim became
        # readable, leave the failed attempt auditable and fail closed.
        _append_holdout_attempt(directory, fields, "rejected", "holdout_claim_unreadable")
        raise ValueError("holdout_claim_unreadable")
    immutable_claim_keys = ("claim_key", "experiment_id", "research_batch_id",
                            "input_manifest_sha256", "result_id", "parameters")
    if any(previous_claim.get(key) != claim_content.get(key)
           for key in immutable_claim_keys):
        _append_holdout_attempt(
            directory, fields, "rejected",
            "holdout_already_consumed_with_different_parameters")
        raise ValueError("holdout_already_consumed_with_different_parameters")

    # Keep a per-experiment envelope for evidence lookup.  Reconstructing it
    # after a crash is safe because the global claim already owns the input.
    content = claim_content
    consumption_id = content_sha256(content)[:16]
    payload = {"consumption_id": consumption_id,
               "content_sha256": content_sha256(content), "content": content}
    path = directory / f"{consumption_id}.json"
    if path.exists():
        try:
            existing = _read(path)
            existing_content = existing.get("content") or {}
            if existing.get("content_sha256") == content_sha256(existing_content) \
                    and existing_content == content:
                return {**content, "status": "unchanged",
                        "consumption_status": content["status"],
                        "path": str(path), "consumption_id": consumption_id}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    fd, temporary_name = tempfile.mkstemp(dir=directory, prefix=f".{consumption_id}.tmp-")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(payload) + b"\n")
        os.link(temporary, path)
        status = "created" if claim_created else "unchanged"
    except FileExistsError:
        status = "unchanged"
    finally:
        temporary.unlink(missing_ok=True)
    return {**content, "status": status, "consumption_status": content["status"],
            "path": str(path), "consumption_id": consumption_id}


def load_holdout_consumption(consumption_id, root=DEFAULT_ROOT):
    if not isinstance(consumption_id, str) or not consumption_id.isalnum():
        raise ValueError("invalid_holdout_consumption_id")
    path = _holdout_root(root).rglob(f"{consumption_id}.json")
    for candidate in path:
        payload = _read(candidate)
        if payload.get("consumption_id") != consumption_id:
            raise ValueError("holdout_consumption_identity_mismatch")
        content = payload.get("content") or {}
        if payload.get("content_sha256") != content_sha256(content):
            raise ValueError("holdout_consumption_digest_mismatch")
        batch = content.get("research_batch_id")
        input_sha = content.get("input_manifest_sha256")
        if not batch or not input_sha:
            raise ValueError("holdout_claim_missing")
        if batch and input_sha:
            claim_id = content_sha256({
                "input_manifest_sha256": input_sha,
            })[:32]
            claim_path = _holdout_claim_root(root) / f"{claim_id}.json"
            if not claim_path.exists():
                raise ValueError("holdout_claim_missing")
            claim_payload = _read(claim_path)
            claim_content = claim_payload.get("content") or {}
            if claim_payload.get("claim_id") != claim_id \
                    or claim_payload.get("content_sha256") != content_sha256(claim_content):
                raise ValueError("holdout_claim_digest_mismatch")
            for key in ("experiment_id", "research_batch_id", "input_manifest_sha256",
                        "result_id", "parameters"):
                if claim_content.get(key) != content.get(key):
                    raise ValueError("holdout_claim_mismatch")
        return {"consumption_id": consumption_id, **content}
    raise ValueError("holdout_consumption_not_found")


# Names used by callers that describe the same append-only operation.
consume_holdout = register_holdout_consumption
record_holdout_consumption = register_holdout_consumption


def resolve_experiment(experiment_id, root=DEFAULT_ROOT):
    """Replay append-only transition evidence; the registration file never changes."""
    record = load_experiment(experiment_id, root)
    events = []
    for path in _event_root(experiment_id, root).glob("*.json"):
        try: events.append(_read(path))
        except (OSError, json.JSONDecodeError): continue
    # Event filenames are content hashes, not timestamps. Follow the evidence
    # chain by parent digest so filesystem iteration/order cannot alter state.
    while True:
        matching = [event for event in events
                    if event.get("experiment_id") == experiment_id
                    and event.get("previous_content_sha256") == record.get("content_sha256")]
        if not matching: break
        if len(matching) != 1: raise ValueError("registry_transition_fork")
        event = matching[0]
        # Events were validated before append; replay only reconstructs the
        # immutable state and must not depend on whichever result roots happen
        # to be available later.
        record = transition(record, event.get("state"), event.get("evidence"), verify=False)
    return record


def transition_experiment(experiment_id, state, evidence, root=DEFAULT_ROOT):
    """Append a guarded state transition idempotently, with evidence."""
    record = resolve_experiment(experiment_id, root)
    _validate_transition_evidence(
        record, state, evidence, root=root,
        experiments_root=Path(root).parent / "experiments",
        shadow_root=Path(root).parent / "shadow",
    )
    next_record = transition(record, state, evidence, verify=False)
    event = {"schema_version": SCHEMA_VERSION, "experiment_id": experiment_id,
             "previous_content_sha256": record["content_sha256"], "state": state,
             "evidence": copy.deepcopy(evidence),
             "content_sha256": next_record["content_sha256"]}
    directory = _event_root(experiment_id, root); directory.mkdir(parents=True, exist_ok=True)
    path = directory / (content_sha256(event) + ".json")
    if path.exists():
        return {"status": "unchanged", "path": str(path), "record": next_record}
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(event) + b"\n")
    try:
        os.link(temporary, path); status = "created"
    except FileExistsError:
        status = "unchanged"
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": status, "path": str(path), "record": next_record}


def _pointer_path(release_root):
    return Path(release_root) / "active_policy.json"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-"); os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except OSError: pass


def load_active_policy(release_root=DEFAULT_RELEASE_ROOT):
    """Return the only policy candidate selection may consume; fail closed to baseline."""
    path = _pointer_path(release_root)
    baseline = {"status": "baseline", "experiment_id": "baseline",
                "priority_bonuses": {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}}
    if not path.exists(): return baseline
    try:
        payload = _read(path)
        definition = validate_experiment_definition(payload.get("definition"))
        if not payload.get("experiment_id"):
            raise ValueError("release_pointer_missing_experiment")
        return {"status": "active", "experiment_id": payload["experiment_id"],
                "priority_bonuses": definition["treatment"], "release_id": payload.get("release_id"),
                "previous_experiment_id": payload.get("previous_experiment_id")}
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(baseline, status="invalid_pointer_fallback")


def publish_experiment(experiment_id, evidence, root=DEFAULT_ROOT, release_root=DEFAULT_RELEASE_ROOT):
    """Explicitly activate an eligible registered experiment and atomically move the pointer."""
    current = resolve_experiment(experiment_id, root)
    if current["content"].get("schema_version") != SCHEMA_VERSION:
        raise ValueError("legacy_experiment_unverified")
    if current["content"].get("state") != "eligible":
        raise ValueError("experiment_not_eligible_for_publish")
    if not evidence: raise ValueError("publish_evidence_required")
    definition = validate_experiment_definition(current["content"].get("definition"))
    old = load_active_policy(release_root)
    verified = verify_release_evidence(
        evidence, experiment_id, root=root,
        experiments_root=Path(root).parent / "experiments",
        shadow_root=Path(root).parent / "shadow",
    )
    transition_experiment(experiment_id, "active", {"publish": copy.deepcopy(evidence)}, root)
    payload = {"schema_version": "recommendation-policy-pointer/v1", "experiment_id": experiment_id,
               "definition": definition, "previous_experiment_id": old.get("experiment_id"),
               "previous_definition": old.get("priority_bonuses"),
               "evidence": copy.deepcopy(evidence), "verification": verified}
    payload["release_id"] = content_sha256(payload)[:16]
    _atomic_write(_pointer_path(release_root), payload)
    history = Path(release_root) / "history" / (payload["release_id"] + ".json")
    if not history.exists(): _atomic_write(history, payload)
    return {"status": "published", "path": str(_pointer_path(release_root)), **payload}


def rollback_active_policy(evidence, release_root=DEFAULT_RELEASE_ROOT):
    """Restore the preceding pointer without modifying old recommendation snapshots."""
    current_path = _pointer_path(release_root)
    if not current_path.exists(): raise ValueError("no_active_policy_to_rollback")
    current = _read(current_path)
    previous = current.get("previous_experiment_id")
    definition = current.get("previous_definition")
    if previous in (None, "baseline"):
        current_path.unlink()
        return {"status": "rolled_back_to_baseline", "experiment_id": "baseline"}
    # A prior release is retained verbatim in history, so its frozen definition is recoverable.
    matches = list((Path(release_root) / "history").glob("*.json"))
    old = next((_read(path) for path in matches if _read(path).get("experiment_id") == previous), None)
    if not old: raise ValueError("previous_release_not_found")
    payload = copy.deepcopy(old); payload["previous_experiment_id"] = current.get("experiment_id")
    payload["evidence"] = {"rollback": copy.deepcopy(evidence)}; payload["release_id"] = content_sha256(payload)[:16]
    _atomic_write(current_path, payload)
    return {"status": "rolled_back", "experiment_id": previous, "path": str(current_path)}


def recover_policy_incident(incident, release_root=DEFAULT_RELEASE_ROOT):
    """Fail closed once for a structured incident without reintroducing its policy.

    This is intentionally separate from operator-requested rollback: recovery
    does not leave a pointer back to the failing version and records a stable
    incident receipt so retries cannot keep moving the pointer.
    """
    if not isinstance(incident, dict) or incident.get("failure_class") not in {
            "contract", "interface", "schema", "digest", "active_pointer"}:
        raise ValueError("recovery_incident_class_invalid")
    active = load_active_policy(release_root)
    if active.get("experiment_id") == "baseline":
        return {"status": "already_baseline", "experiment_id": "baseline"}
    pointer_path = _pointer_path(release_root)
    pointer = _read(pointer_path)
    broken = pointer.get("experiment_id")
    incident_id = content_sha256({"broken_experiment_id": broken, **incident})[:32]
    incident_path = Path(release_root) / "incidents" / f"{incident_id}.json"
    if incident_path.exists():
        return {"status": "unchanged", "incident_id": incident_id,
                "experiment_id": load_active_policy(release_root).get("experiment_id")}
    baseline = {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}
    previous = pointer.get("previous_experiment_id")
    if previous in (None, "baseline", broken):
        pointer_path.unlink()
        restored = "baseline"
    else:
        history = Path(release_root) / "history"
        candidates = [] if not history.exists() else [_read(path) for path in history.glob("*.json")]
        prior = next((item for item in candidates if item.get("experiment_id") == previous
                      and isinstance(item.get("verification"), dict)), None)
        if prior is None:
            pointer_path.unlink(); restored = "baseline"
        else:
            validate_experiment_definition(prior.get("definition"))
            replacement = copy.deepcopy(prior)
            replacement.update({"previous_experiment_id": "baseline",
                                "previous_definition": baseline,
                                "recovery": {"incident_id": incident_id,
                                             "replaced_experiment_id": broken}})
            replacement["release_id"] = content_sha256(replacement)[:16]
            _atomic_write(pointer_path, replacement); restored = previous
    receipt = {"schema_version": "evolution-recovery-incident/v1", "incident_id": incident_id,
               "broken_experiment_id": broken, "restored_experiment_id": restored,
               "incident": copy.deepcopy(incident)}
    _atomic_write(incident_path, receipt)
    return {"status": "recovered", "incident_id": incident_id,
            "experiment_id": restored}
