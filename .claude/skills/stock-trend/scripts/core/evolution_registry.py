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


SCHEMA_VERSION = "evolution-registry/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "registry"
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
