"""Immutable registry for recommendation-evolution experiments.

The registry records attempts as well as successes.  It is deliberately not a
feature flag for the formal candidate policy; publishing remains a later,
explicit operation.
"""
import copy
import json
import os
import tempfile
from pathlib import Path

from .cache_utils import CACHE_DIR
from .recommendation_snapshot import canonical_json, content_sha256


SCHEMA_VERSION = "evolution-registry/v2"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "registry"
DEFAULT_RELEASE_ROOT = Path(CACHE_DIR) / "evolution" / "releases"
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


def transition(record, state, evidence):
    """Return a new immutable record after a guarded state transition."""
    content = copy.deepcopy((record or {}).get("content") or {})
    current = content.get("state")
    if state not in STATES or state not in _ALLOWED.get(current, set()):
        raise ValueError("invalid_registry_transition")
    if not evidence:
        raise ValueError("transition_evidence_required")
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
    validate_experiment_definition((record.get("content") or {}).get("definition"))
    return record


def _event_root(experiment_id, root):
    return Path(root) / "events" / experiment_id


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
        record = transition(record, event.get("state"), event.get("evidence"))
    return record


def transition_experiment(experiment_id, state, evidence, root=DEFAULT_ROOT):
    """Append a guarded state transition idempotently, with evidence."""
    record = resolve_experiment(experiment_id, root)
    next_record = transition(record, state, evidence)
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
    if current["content"].get("state") != "eligible":
        raise ValueError("experiment_not_eligible_for_publish")
    if not evidence: raise ValueError("publish_evidence_required")
    definition = validate_experiment_definition(current["content"].get("definition"))
    old = load_active_policy(release_root)
    transition_experiment(experiment_id, "active", {"publish": copy.deepcopy(evidence)}, root)
    payload = {"schema_version": "recommendation-policy-pointer/v1", "experiment_id": experiment_id,
               "definition": definition, "previous_experiment_id": old.get("experiment_id"),
               "previous_definition": old.get("priority_bonuses"), "evidence": copy.deepcopy(evidence)}
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
