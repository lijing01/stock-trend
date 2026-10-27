"""Content-addressed, independent weak-to-strong evidence."""

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path


def save_snapshot(payload, inputs, calendar, market, root, *, provisional=False):
    """Save the frozen input and result atomically without replacing old evidence."""
    try:
        detector_path = Path(__file__).resolve().parent.parent / "analysis" / "daily_weak_to_strong.py"
        content = {"schema_version": "weak-to-strong-snapshot/v1",
                   "result": copy.deepcopy(payload), "inputs": copy.deepcopy(inputs),
                   "calendar": copy.deepcopy(calendar), "market_context": copy.deepcopy(market),
                   "detector_version": "1.0.0",
                   "detector_sha256": hashlib.sha256(detector_path.read_bytes()).hexdigest()}
        content["result"].pop("tracking", None)
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        date = str(payload.get("as_of_date") or "")
        if len(date) != 10 or date[4] != "-" or date[7] != "-" or not date.replace("-", "").isdigit():
            raise ValueError("invalid as_of_date")
        directory = Path(root) / ("provisional" if provisional else "daily") / date
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + ".json")
        if path.exists():
            if path.read_bytes() != encoded:
                raise ValueError("content_hash_conflict")
            return {"status": "existing", "path": str(path), "content_sha256": digest}
        fd, temporary = tempfile.mkstemp(prefix=".weak-to-strong-", dir=str(directory))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise ValueError("content_hash_conflict")
        finally:
            os.unlink(temporary)
        return {"status": "saved", "path": str(path), "content_sha256": digest}
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        return {"status": "write_failed", "reason": type(exc).__name__}
