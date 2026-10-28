"""Content-addressed, independent weak-to-strong evidence."""

import ast
import copy
import hashlib
import json
import os
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path


SNAPSHOT_SCHEMA = "weak-to-strong-snapshot/v1"
RUN_INDEX_SCHEMA = "weak-to-strong-run-index/v1"
V2_SNAPSHOT_SCHEMA = "weak-to-strong-snapshot/v2"
V2_RUN_INDEX_SCHEMA = "weak-to-strong-run-index/v2"
_SUPPORTED_VERSIONS = {"v1", "v2"}


def _valid_date(value):
    text = str(value or "")
    try:
        return date.fromisoformat(text).isoformat() if len(text) == 10 else ""
    except ValueError:
        return ""


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _atomic_new(path, encoded):
    """Create *path* atomically, returning False when it already exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".weak-to-strong-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        os.unlink(temporary)


def _generated_at(value=None):
    generated = value or datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("generated_at_timezone_missing")
    return parsed.isoformat()


def _record_run(root, snapshot_date, digest, path, *, provisional, generated_at=None,
                run_index_schema=RUN_INDEX_SCHEMA):
    generated = _generated_at(generated_at)
    sequence = time.time_ns()
    kind = "provisional" if provisional else "daily"
    directory = Path(root) / "run_index" / kind / snapshot_date
    record = {
        "schema_version": run_index_schema,
        "snapshot_date": snapshot_date,
        "snapshot_type": kind,
        "generated_at": generated,
        "sequence": sequence,
        "content_sha256": digest,
        "snapshot_name": Path(path).name,
    }
    for offset in range(100):
        record["sequence"] = sequence + offset
        encoded = _canonical_json(record)
        index_path = directory / f"{sequence + offset:020d}-{digest[:12]}.json"
        if _atomic_new(index_path, encoded):
            return index_path, record
    raise OSError("run_index_sequence_exhausted")


def _v2_detector_identity():
    """Return every module that participates in the frozen v2 output."""
    scripts_dir = Path(__file__).resolve().parent.parent
    module_names = (
        "analysis/daily_weak_to_strong.py",
        "analysis/daily_weak_to_strong_v2.py",
        "analysis/weak_to_strong_v2_follow_up.py",
        "scans/weak_to_strong_v2.py",
        "reporting/weak_to_strong.py",
    )
    hashes = {}
    for name in module_names:
        path = scripts_dir / name
        if not path.is_file():
            raise ValueError("v2_detector_module_missing")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    source = (scripts_dir / "analysis" / "daily_weak_to_strong_v2.py").read_text(
        encoding="utf-8")
    detector_version = ""
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id == "DETECTOR_VERSION" for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            detector_version = node.value.value
            break
    if not detector_version:
        raise ValueError("v2_detector_version_missing")
    return detector_version, hashes


def _version_contract(root, requested_version):
    if requested_version not in _SUPPORTED_VERSIONS:
        raise ValueError("snapshot_version_invalid")
    if requested_version == "v2":
        return (Path(root) / "v2", V2_SNAPSHOT_SCHEMA, V2_RUN_INDEX_SCHEMA)
    return Path(root), SNAPSHOT_SCHEMA, RUN_INDEX_SCHEMA


def save_snapshot(payload, inputs, calendar, market, root, *, provisional=False,
                  generated_at=None, requested_version="v1"):
    """Save frozen evidence and append an atomic successful-run index entry."""
    try:
        resolved_root, snapshot_schema, index_schema = _version_contract(
            root, requested_version)
        detector_path = Path(__file__).resolve().parent.parent / "analysis" / "daily_weak_to_strong.py"
        if requested_version == "v2":
            detector_version, detector_hashes = _v2_detector_identity()
            payload_version = str(payload.get("detector_version") or "")
            if payload_version and payload_version != detector_version:
                raise ValueError("v2_detector_version_mismatch")
            content = {
                "schema_version": snapshot_schema,
                "result": copy.deepcopy(payload),
                "inputs": copy.deepcopy(inputs),
                "calendar": copy.deepcopy(calendar),
                "market_context": copy.deepcopy(market),
                "detector_version": detector_version,
                "detector_modules_sha256": detector_hashes,
                "parameter_hash": payload.get("parameter_hash"),
            }
            performance = content["result"].get("performance")
            if isinstance(performance, dict):
                content["result"]["performance"] = {
                    key: value for key, value in performance.items()
                    if not str(key).endswith("_seconds")
                }
        else:
            content = {"schema_version": snapshot_schema,
                       "result": copy.deepcopy(payload), "inputs": copy.deepcopy(inputs),
                       "calendar": copy.deepcopy(calendar), "market_context": copy.deepcopy(market),
                       "detector_version": "1.0.0",
                       "detector_sha256": hashlib.sha256(detector_path.read_bytes()).hexdigest()}
        content["result"].pop("tracking", None)
        encoded = _canonical_json(content)
        digest = hashlib.sha256(encoded).hexdigest()
        snapshot_date = _valid_date(payload.get("as_of_date"))
        if not snapshot_date:
            raise ValueError("invalid_as_of_date")
        kind = "provisional" if provisional else "daily"
        path = resolved_root / kind / snapshot_date / (digest + ".json")
        created = _atomic_new(path, encoded)
        if not created and path.read_bytes() != encoded:
            raise ValueError("content_hash_conflict")
        index_path, record = _record_run(
            resolved_root, snapshot_date, digest, path, provisional=provisional,
            generated_at=generated_at, run_index_schema=index_schema)
        return {"status": "saved" if created else "existing", "path": str(path),
                "content_sha256": digest, "run_index_path": str(index_path),
                "generated_at": record["generated_at"]}
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        return {"status": "write_failed", "reason": type(exc).__name__}


def _load_validated(path, snapshot_date, expected_digest=None,
                    snapshot_schema=SNAPSHOT_SCHEMA):
    path = Path(path)
    encoded = path.read_bytes()
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if ((expected_digest and actual_digest != expected_digest)
            or actual_digest != path.stem):
        raise ValueError("snapshot_hash_mismatch")
    payload = json.loads(encoded.decode("utf-8"))
    if payload.get("schema_version") != snapshot_schema:
        raise ValueError("snapshot_schema_invalid")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("as_of_date") != snapshot_date:
        raise ValueError("snapshot_date_mismatch")
    return payload, actual_digest


def load_latest_snapshot(root, snapshot_date, *, provisional=False,
                         requested_version="v1"):
    """Load the latest successful run for a date, with legacy mtime fallback."""
    snapshot_date = _valid_date(snapshot_date)
    if not snapshot_date:
        return {"status": "invalid", "reason": "snapshot_date_invalid"}
    kind = "provisional" if provisional else "daily"
    try:
        root, snapshot_schema, index_schema = _version_contract(
            root, requested_version)
    except ValueError as exc:
        return {"status": "invalid", "reason": str(exc),
                "snapshot_date": snapshot_date}
    index_paths = list((root / "run_index" / kind / snapshot_date).glob("*.json"))
    selection_method = "run_index_generated_at_sequence"
    index_path = None
    generated_at = None
    try:
        if index_paths:
            indexed = []
            for candidate in index_paths:
                record = json.loads(candidate.read_text(encoding="utf-8"))
                if (record.get("schema_version") != index_schema
                        or record.get("snapshot_date") != snapshot_date
                        or record.get("snapshot_type") != kind):
                    raise ValueError("run_index_invalid")
                parsed_at = datetime.fromisoformat(
                    str(record.get("generated_at") or "").replace("Z", "+00:00"))
                if parsed_at.tzinfo is None:
                    raise ValueError("generated_at_timezone_missing")
                sequence = record.get("sequence")
                if (not isinstance(sequence, int)
                        or not candidate.name.startswith(f"{sequence:020d}-")):
                    raise ValueError("run_index_sequence_invalid")
                indexed.append((parsed_at, sequence, candidate.name,
                                candidate, record))
            _, _, _, index_path, record = max(indexed)
            name = str(record.get("snapshot_name") or "")
            if Path(name).name != name or not name.endswith(".json"):
                raise ValueError("snapshot_name_invalid")
            path = root / kind / snapshot_date / name
            generated_at = _generated_at(record.get("generated_at"))
            payload, digest = _load_validated(
                path, snapshot_date, str(record.get("content_sha256") or ""),
                snapshot_schema)
        else:
            candidates = list((root / kind / snapshot_date).glob("*.json"))
            if not candidates:
                return {"status": "missing", "reason": "snapshot_missing",
                        "snapshot_date": snapshot_date}
            path = max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name))
            payload, digest = _load_validated(
                path, snapshot_date, snapshot_schema=snapshot_schema)
            selection_method = "legacy_mtime_fallback"
        return {
            "status": "loaded", "snapshot_date": snapshot_date,
            "snapshot": payload,
            "tracking": {
                "path": str(path), "content_sha256": digest,
                "selection_method": selection_method,
                "generated_at": generated_at,
                "index_path": str(index_path) if index_path else None,
            },
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "reason": str(exc) or type(exc).__name__,
                "snapshot_date": snapshot_date,
                "tracking": {"index_path": str(index_path) if index_path else None,
                             "selection_method": selection_method}}


def load_previous_snapshot(root, as_of_date, trading_dates, *, requested_version="v1"):
    """Load the indexed snapshot for the immediately previous trading date."""
    current = _valid_date(as_of_date)
    if not current:
        return {"status": "invalid", "reason": "as_of_date_invalid"}
    calendar = {_valid_date(item) for item in (trading_dates or []) if _valid_date(item)}
    if current not in calendar:
        return {"status": "invalid", "reason": "as_of_date_not_in_trading_calendar",
                "previous_trading_date": None}
    eligible = sorted(item for item in calendar if item < current)
    if not eligible:
        return {"status": "missing", "reason": "previous_trading_date_missing",
                "previous_trading_date": None}
    previous = eligible[-1]
    loaded = load_latest_snapshot(
        root, previous, requested_version=requested_version)
    loaded["previous_trading_date"] = previous
    return loaded
