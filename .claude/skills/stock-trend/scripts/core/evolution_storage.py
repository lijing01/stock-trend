"""Storage contract and read-only compatibility adapters for evolution data.

The formal recommendation history deliberately lives outside this module.  It
is immutable production evidence; evolution data is derived research material
and is kept under one cache root instead.
"""
import copy
import json
from pathlib import Path

from .cache_utils import CACHE_DIR
from .recommendation_snapshot import content_sha256


SCHEMA_VERSION = "recommendation-evolution-storage/v1"
EVOLUTION_ROOT = Path(CACHE_DIR) / "evolution"
SUBDIRECTORIES = ("research", "evaluations", "diagnostics", "proposals", "experiments", "shadow", "registry")
LEGACY_RESEARCH_ROOT = Path(CACHE_DIR) / "candidate_research_history"


def storage_root(name, root=None):
    """Return a named evolution directory and reject accidental new stores."""
    if name not in SUBDIRECTORIES:
        raise ValueError("unknown_evolution_storage_area")
    return Path(root) if root is not None else EVOLUTION_ROOT / name


def input_manifest(**inputs):
    """Describe frozen or externally immutable inputs without storing secrets.

    Values are embedded snapshots or immutable identifiers, never cache paths
    that might later be overwritten.  The manifest is content-addressed so a
    derived result remains reproducible even after normal cache eviction.
    """
    frozen = copy.deepcopy(inputs)
    return {
        "schema_version": SCHEMA_VERSION,
        "archive_mode": "embedded_or_immutable_reference",
        "inputs": frozen,
        "input_sha256": content_sha256(frozen),
        "credential_policy": "no_model_credentials_stored",
    }


def _legacy_content(value):
    """Turn the early unwrapped P1 shape into a read-only v1-like envelope."""
    if not isinstance(value, dict):
        raise ValueError("research_snapshot_not_object")
    content = copy.deepcopy(value.get("content") if isinstance(value.get("content"), dict) else value)
    content.setdefault("schema_version", "candidate-research-snapshot/legacy")
    content.setdefault("snapshot_type", "formal")
    content.setdefault("records", [])
    content.setdefault("official_snapshot", {"link_status": "unlinked"})
    content.setdefault("parameter_summary", {})
    content.setdefault("policy", {})
    content.setdefault("market_regime", {})
    content.setdefault("sectors", [])
    return content


def load_research_snapshot(path_or_payload):
    """Read v1 research data or a pre-v1 record without rewriting either.

    Compatibility is intentionally additive and read-only: old content and its
    original digest are never altered, so a loader cannot silently "migrate"
    historical research into a different experiment.
    """
    if isinstance(path_or_payload, (str, Path)):
        value = json.loads(Path(path_or_payload).read_text(encoding="utf-8"))
    else:
        value = copy.deepcopy(path_or_payload)
    if not isinstance(value, dict):
        raise ValueError("research_snapshot_not_object")
    content = value.get("content") if isinstance(value.get("content"), dict) else None
    version = value.get("schema_version") or (content or {}).get("schema_version")
    if version == "candidate-research-snapshot/v1":
        return value
    legacy = _legacy_content(value)
    return {
        "schema_version": "candidate-research-snapshot/v1",
        "run_id": value.get("run_id") or content_sha256(legacy)[:16],
        "content_sha256": value.get("content_sha256") or content_sha256(legacy),
        "content": legacy,
        "compatibility": {
            "status": "legacy_read_only",
            "source_schema_version": version or "unversioned",
            "limitations": "only fields present in the old record are available for research",
        },
    }
