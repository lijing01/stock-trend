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
from pathlib import Path

from .cache_utils import CACHE_DIR
from .recommendation_snapshot import canonical_json, content_sha256


SCHEMA_VERSION = "evolution-registry/v2"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "registry"
DEFAULT_RELEASE_ROOT = Path(CACHE_DIR) / "evolution" / "releases"
DEFAULT_EXPERIMENTS_ROOT = Path(CACHE_DIR) / "evolution" / "experiments"
DEFAULT_SHADOW_ROOT = Path(CACHE_DIR) / "evolution" / "shadow"
HOLDOUT_SCHEMA_VERSION = "evolution-holdout-consumption/v1"
RELEASE_EVIDENCE_SCHEMA_VERSION = "evolution-release-evidence/v2"
STATES = ("draft", "validated", "shadow", "eligible", "active", "retired", "reverted")
_ALLOWED = {
    "draft": {"validated", "retired"}, "validated": {"shadow", "retired"},
    "shadow": {"eligible", "retired", "reverted"}, "eligible": {"active", "retired", "reverted"},
    "active": {"retired", "reverted"}, "retired": set(), "reverted": set(),
}


def validate_experiment_definition(definition):
    """Accept P3's single, frozen within-bucket ranking experiment only."""
    value = copy.deepcopy(definition or {})
    if value.get("kind") != "buy_point_priority_bonus":
        raise ValueError("experiment_kind_out_of_scope")
    if value.get("baseline") != {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}:
        raise ValueError("baseline_not_frozen")
    if value.get("treatment") != {"strict_level_1": 0, "strict_level_2": 0, "strict_level_3": 0}:
        raise ValueError("treatment_not_frozen")
    if value.get("changes") != ["within_bucket_ranking"]:
        raise ValueError("experiment_changes_out_of_scope")
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
        payload = evidence.get("validation_result") if isinstance(evidence, dict) else None
        payload = payload or _load_result(
            (evidence or {}).get("validation_result_id") if isinstance(evidence, dict)
            else None, experiments_root)
        return _verify_validation_result(
            payload,
            expected_result_id=(evidence or {}).get("validation_result_id")
            if isinstance(evidence, dict) else None,
            require_eligible=False)
    if state == "shadow":
        payload = evidence.get("shadow_result") if isinstance(evidence, dict) else None
        payload = payload or _load_result(
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
    if not isinstance(content.get("input_manifest"), dict) \
            or not isinstance(content["input_manifest"].get("input_sha256"), str) \
            or not content["input_manifest"].get("input_sha256"):
        raise ValueError("validation_result_manifest_missing")
    gates = _promotion_gates_from_content(content)
    if require_eligible:
        # The summary's ``promotion.eligible`` flag is advisory only.  A
        # caller cannot make a release eligible by asserting a boolean; the
        # gate values above are recomputed from the measured content.
        if not all(gates.values()):
            raise ValueError("validation_gates_not_satisfied")
    return {"result_id": result_id, "content": content, "gates": gates}


def _verify_shadow_result(result, expected_result_id=None):
    result_id, content = _result_envelope(result, expected_result_id=expected_result_id)
    schema = str(content.get("schema_version") or result.get("schema_version") or "")
    if schema != "recommendation-shadow/v1" \
            or content.get("shadow_type") != "forward":
        raise ValueError("shadow_result_schema_invalid")
    if content.get("formal_policy_affected") is not False:
        raise ValueError("shadow_formal_policy_affected")
    if content.get("retrospective") is True or content.get("independent_snapshot") is not True:
        raise ValueError("shadow_result_not_forward_independent")
    complete_dates = content.get("complete_paired_dates")
    if complete_dates is None:
        complete_dates = (content.get("maturity") or {}).get("complete_paired_dates")
    mature_events = content.get("valid_alpha_events")
    if mature_events is None:
        mature_events = (content.get("maturity") or {}).get("valid_alpha_events")
    if not _count_at_least(complete_dates, 20) \
            or not _count_at_least(mature_events, 100):
        raise ValueError("shadow_maturity_insufficient")
    if not isinstance(content.get("input_manifest"), dict) \
            or not isinstance(content["input_manifest"].get("input_sha256"), str) \
            or not content["input_manifest"].get("input_sha256"):
        raise ValueError("shadow_manifest_missing")
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
        if isinstance(payload, dict) \
                and str(payload.get("experiment_id") or payload.get("result_id") or "") == str(result_id):
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
    validation_payload = evidence.get("validation_result") or _load_result(
        evidence["validation_result_id"], experiments_root)
    holdout_payload = evidence.get("holdout_result") or _load_result(
        evidence["holdout_result_id"], experiments_root)
    shadow_payload = evidence.get("shadow_result") or _load_result(
        evidence["shadow_result_id"], shadow_root)
    validation = _verify_validation_result(
        validation_payload, expected_result_id=evidence["validation_result_id"],
        require_eligible=True)
    holdout = _verify_validation_result(
        holdout_payload, expected_result_id=evidence["holdout_result_id"],
        require_eligible=True)
    shadow = _verify_shadow_result(
        shadow_payload, expected_result_id=evidence["shadow_result_id"])
    result_ids = {validation["result_id"], holdout["result_id"], shadow["result_id"]}
    if len(result_ids) != 3:
        raise ValueError("release_evidence_result_identity_reused")
    for result in (validation, holdout):
        if (result["content"].get("definition") or {}) != definition:
            raise ValueError("release_evidence_definition_mismatch")
    validation_input = (validation["content"].get("input_manifest") or {}).get("input_sha256")
    holdout_input = (holdout["content"].get("input_manifest") or {}).get("input_sha256")
    shadow_input = (shadow["content"].get("input_manifest") or {}).get("input_sha256")
    if shadow_input in {validation_input, holdout_input}:
        raise ValueError("shadow_input_not_independent")
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
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(payload) + b"\n")
    try:
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
    for path in sorted(directory.glob("*.json")):
        try:
            payload = _read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        previous = payload.get("content") if isinstance(payload.get("content"), dict) else payload
        if not isinstance(previous, dict):
            continue
        same_input = (previous.get("research_batch_id") == fields["research_batch_id"]
                      or previous.get("input_manifest_sha256") == fields["input_manifest_sha256"])
        if not same_input:
            continue
        same_identity = (
            previous.get("research_batch_id") == fields["research_batch_id"]
            and previous.get("input_manifest_sha256") == fields["input_manifest_sha256"]
            and previous.get("result_id") == fields["result_id"]
            and previous.get("parameters") == fields["parameters"]
        )
        if same_identity:
            return {**previous, "status": "unchanged",
                    "consumption_status": previous.get("status", "reserved"),
                    "path": str(path),
                    "consumption_id": payload.get("consumption_id") or previous.get("consumption_id")}
        _append_holdout_attempt(
            directory, fields, "rejected",
            "holdout_already_consumed_with_different_parameters")
        raise ValueError("holdout_already_consumed_with_different_parameters")
    content = {"schema_version": HOLDOUT_SCHEMA_VERSION, **fields,
               "status": "reserved"}
    consumption_id = content_sha256(content)[:16]
    payload = {"consumption_id": consumption_id,
               "content_sha256": content_sha256(content), "content": content}
    path = directory / f"{consumption_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(payload) + b"\n")
    try:
        os.link(temporary, path)
        status = "created"
    except FileExistsError:
        status = "unchanged"
    finally:
        temporary.unlink(missing_ok=True)
    return {**content, "status": status, "consumption_status": content["status"],
            "path": str(path),
            "consumption_id": consumption_id}


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
