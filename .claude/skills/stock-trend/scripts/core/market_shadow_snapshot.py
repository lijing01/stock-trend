"""Independent, content-addressed persistence for market-style experiments."""

import copy
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .cache_utils import CACHE_DIR
from .recommendation_snapshot import (
    _normalize_for_json,
    canonical_json,
    content_sha256,
)


SCHEMA_VERSION = "market-shadow-snapshot/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "market_shadow_history"


class ShadowSnapshotResult:
    def __init__(self, status, path=None, content_sha256=None, reason=None):
        self.status = status
        self.path = str(path) if path else None
        self.content_sha256 = content_sha256
        self.reason = reason


def _valid_date(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _prepare_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("shadow payload must be an object")
    value = _normalize_for_json(copy.deepcopy(payload))
    basis_date = value.get("basis_date")
    if not _valid_date(basis_date):
        raise ValueError("invalid basis_date")
    snapshot_type = value.get("snapshot_type", "formal")
    if snapshot_type not in ("formal", "provisional"):
        raise ValueError("invalid snapshot_type")
    value.pop("content_sha256", None)
    value.pop("input_digest", None)
    value.setdefault("schema_version", "market-style-shadow/v1")
    value.setdefault("formal_policy_affected", False)
    digest = content_sha256(value)
    value["content_sha256"] = digest
    value["input_digest"] = digest
    return value, digest


def save_shadow_run(payload, root=DEFAULT_ROOT, namespace=None):
    """Atomically publish one immutable shadow run.

    The digest is the filename, so a different input can never overwrite a
    same-day run and concurrent identical writers converge on one file.
    """
    value, digest = _prepare_payload(payload)
    root = Path(root)
    snapshot_type = value["snapshot_type"]
    basis_date = value["basis_date"]
    target_dir = root
    if namespace:
        target_dir = target_dir / str(namespace)
    target_dir = target_dir / snapshot_type / basis_date
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest}.json"
    fd, temporary = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
    os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(canonical_json(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return ShadowSnapshotResult("unchanged", target, digest)
        return ShadowSnapshotResult("created", target, digest)
    except OSError as exc:
        return ShadowSnapshotResult("write_failed", None, digest, str(exc))
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def load_shadow_run(path):
    path = Path(path)
    value = _normalize_for_json(json.loads(
        path.read_text(encoding="utf-8")))
    expected = value.get("content_sha256")
    if not expected or value.get("input_digest") != expected:
        raise ValueError("shadow digest missing")
    content = copy.deepcopy(value)
    content.pop("content_sha256", None)
    content.pop("input_digest", None)
    if expected != content_sha256(content):
        raise ValueError("shadow digest mismatch")
    return value
