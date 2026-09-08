"""Bounded, evidence-checked proposal handling for recommendation research."""
import copy
import argparse
import json
import math
import os
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.recommendation_snapshot import canonical_json, content_sha256
from core.evolution_storage import input_manifest, storage_root


SCHEMA_VERSION = "recommendation-evolution-proposal/v1"
DEFAULT_ROOT = storage_root("proposals")
MAX_WEEKLY_PROPOSALS = 3
ALLOWED_VARIABLE = "buy_point_priority_bonus"
ALLOWED_LEVELS = {"strict_level_1", "strict_level_2", "strict_level_3"}
BUDGET_SCHEMA_VERSION = "recommendation-evolution-proposal-budget/v1"
DIAGNOSTICS_SCHEMA_VERSION = "recommendation-diagnostics/v1"
_NO_RESPONSE = object()


def _count(value):
    """Parse a non-negative finite count without accepting booleans."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        return None
    return int(value)


def _research_gate(overall):
    """Return the conservative overall P0 gate counts.

    ``mature_dates`` is retained as an audit count, but cannot substitute for
    dates having a valid deduplicated primary alpha once v2 diagnostics expose
    ``alpha_mature_dates``/``valid_alpha_dates``.
    """
    mature_events = _count(overall.get("valid_alpha_events"))
    if mature_events is None:
        return 0, 0, False
    alpha_dates = _count(overall.get("alpha_mature_dates"))
    if alpha_dates is None:
        alpha_dates = _count(overall.get("valid_alpha_dates"))
    if alpha_dates is None:
        return mature_events, 0, False
    return mature_events, alpha_dates, mature_events >= 100 and alpha_dates >= 20


def _week_key(value=None):
    value = value or date.today()
    if isinstance(value, (tuple, list)):
        if len(value) < 2:
            raise ValueError("invalid_iso_week")
        return tuple(value)
    return value.isocalendar()


def proposal_schema():
    """A JSON-schema-shaped contract usable by an external model adapter."""
    return {"type": "object", "required": ["proposals"], "properties": {"proposals": {
        "type": "array", "maxItems": MAX_WEEKLY_PROPOSALS, "items": {"type": "object",
        "required": ["hypothesis", "evidence_ids", "counterexample", "change", "expected_direction", "failure_condition", "validation_plan"]}}}}


def research_material(diagnostics):
    content = diagnostics.get("content", diagnostics)
    return {"diagnostic_id": diagnostics.get("diagnostic_id"),
            "content_sha256": diagnostics.get("content_sha256"),
            "schema": proposal_schema(), "overall": content.get("overall"),
            "dimensions": content.get("dimensions"),
            "instruction": "只基于 evidence_ids 提案；样本不足时返回 proposals=[]，不得声称因果或直接改参数。"}


_ATTEMPT_IDENTITY_FIELDS = ("diagnostic_id", "diagnostic_sha256", "material_sha256")


def _normalize_attempt_identity(input_identity=None, diagnostic_id=None,
                                diagnostic_sha256=None, material_sha256=None):
    """Normalize the immutable input identity bound to a weekly model attempt."""
    supplied = dict(input_identity or {})
    explicit = {
        "diagnostic_id": diagnostic_id,
        "diagnostic_sha256": diagnostic_sha256,
        "material_sha256": material_sha256,
    }
    for key, value in explicit.items():
        if value is None:
            continue
        if key in supplied and supplied[key] != value:
            raise ValueError("weekly_attempt_identity_conflict")
        supplied[key] = value
    if not supplied:
        return None
    if any(key not in supplied or supplied[key] in (None, "")
           for key in _ATTEMPT_IDENTITY_FIELDS):
        raise ValueError("weekly_attempt_identity_required")
    return {key: str(supplied[key]) for key in _ATTEMPT_IDENTITY_FIELDS}


def _diagnostic_attempt_identity(diagnostics, material=None):
    """Derive one stable identity for the exact diagnostic/model material."""
    material = research_material(diagnostics) if material is None else material
    return _normalize_attempt_identity(
        diagnostic_id=diagnostics.get("diagnostic_id"),
        diagnostic_sha256=diagnostics.get("content_sha256"),
        material_sha256=content_sha256(material),
    )


def _stored_attempt_identity(attempt):
    nested = attempt.get("input_identity")
    if isinstance(nested, dict):
        try:
            return _normalize_attempt_identity(nested)
        except ValueError:
            return None
    legacy = {key: attempt.get(key) for key in _ATTEMPT_IDENTITY_FIELDS
              if key in attempt}
    if not legacy:
        return None
    try:
        return _normalize_attempt_identity(legacy)
    except ValueError:
        return None


def _attempt_identity_matches(attempt, input_identity):
    """Return False for a bound-attempt mismatch; None means legacy/unbound."""
    if input_identity is None:
        return True
    stored = _stored_attempt_identity(attempt)
    return stored is not None and stored == input_identity


def _validate_diagnostics_envelope(diagnostics):
    """Return whether diagnostics are a verified v1 envelope.

    A bare or legacy shape remains readable for audit, but is never eligible
    to produce a proposal.  A present, stale digest is an integrity error and
    is rejected loudly rather than silently downgraded.
    """
    if not isinstance(diagnostics, dict):
        raise ValueError("invalid_diagnostics")
    content = diagnostics.get("content")
    if content is None:
        return False
    if not isinstance(content, dict):
        raise ValueError("invalid_diagnostics")
    if (diagnostics.get("schema_version") != DIAGNOSTICS_SCHEMA_VERSION
            or content.get("schema_version") != DIAGNOSTICS_SCHEMA_VERSION
            or diagnostics.get("content_sha256") is None
            or diagnostics.get("diagnostic_id") is None):
        return False
    digest = content_sha256(content)
    supplied = diagnostics.get("content_sha256")
    if supplied != digest:
        raise ValueError("invalid_diagnostics_digest")
    diagnostic_id = diagnostics.get("diagnostic_id")
    if diagnostic_id != digest[:16]:
        raise ValueError("invalid_diagnostics_id")
    return True


def validate_proposals(diagnostics, response):
    content = diagnostics.get("content", diagnostics)
    evidence = content.get("evidence_index") or {}
    proposals = (response or {}).get("proposals")
    if not isinstance(proposals, list):
        return [], ["proposals_not_list"]
    errors, valid = [], []
    if len(proposals) > MAX_WEEKLY_PROPOSALS:
        return [], ["too_many_proposals"]
    for number, proposal in enumerate(proposals):
        prefix = f"proposal_{number}"
        if not isinstance(proposal, dict):
            errors.append(prefix + ":not_object"); continue
        required = ("hypothesis", "evidence_ids", "counterexample", "change", "expected_direction", "failure_condition", "validation_plan")
        if any(not proposal.get(field) for field in required):
            errors.append(prefix + ":missing_required_field"); continue
        ids = proposal["evidence_ids"]
        if not isinstance(ids, list) or not ids or any(item not in evidence for item in ids):
            errors.append(prefix + ":invalid_evidence_reference"); continue
        if any(not evidence[item].get("proposal_eligible") for item in ids):
            errors.append(prefix + ":insufficient_group_sample"); continue
        change = proposal["change"]
        if (not isinstance(change, dict)
                or set(change) != {"variable", "values"}
                or change.get("variable") != ALLOWED_VARIABLE):
            errors.append(prefix + ":parameter_out_of_scope"); continue
        values = change.get("values")
        if (not isinstance(values, dict) or not values
                or set(values) - ALLOWED_LEVELS or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or value > 3
                for value in values.values())):
            errors.append(prefix + ":parameter_out_of_bounds"); continue
        valid.append(copy.deepcopy(proposal))
    return valid, errors


def build_proposal_run(diagnostics, response=None, week=None, adapter_status="model_unavailable",
                       usage=None, attempt_id=None):
    """Create an idempotent weekly proposal record without invoking a model.

    An orchestration layer may supply one adapter response and one format repair;
    both calls and their token usage belong in ``usage`` for auditability.
    """
    verified = _validate_diagnostics_envelope(diagnostics)
    content = diagnostics.get("content", diagnostics)
    overall = content.get("overall") or {}
    # The 30-event group floor prevents noisy slice-level stories.  The P0
    # research gate remains stricter: no weekly proposal before 20 mature
    # dates and 100 mature primary-window events overall.
    mature_events, mature_dates, ready = _research_gate(overall)
    ready = verified and ready
    if not ready:
        # Do not even validate/accept a model response while the overall
        # research gate is closed.  A syntactically valid response cannot turn
        # one mature group into a sufficiently broad evidence base.
        valid, errors = [], ["overall_research_not_ready"]
        status = "continue_accumulating"
    else:
        valid, errors = validate_proposals(diagnostics, response) if response else ([], [])
        status = "proposed" if valid else "no_valid_proposal"
    run_content = {"schema_version": SCHEMA_VERSION, "week": list(_week_key(week)),
                   "diagnostic_id": diagnostics.get("diagnostic_id"),
                   "diagnostic_sha256": diagnostics.get("content_sha256"), "status": status,
                   "adapter_status": adapter_status, "usage": copy.deepcopy(usage or {"generation_calls": 0, "repair_calls": 0}),
                   "generation_attempt_id": attempt_id,
                   "proposals": valid, "rejected": errors,
                   "policy": "每周最多3条；只生成假设，不修改参数、不自动发布。"}
    run_content["input_manifest"] = input_manifest(
        diagnostics_id=diagnostics.get("diagnostic_id"),
        diagnostics_sha256=diagnostics.get("content_sha256"),
        diagnostics_schema_version=content.get("schema_version"),
        week=list(_week_key(week)),
    )
    return {"schema_version": SCHEMA_VERSION, "proposal_id": content_sha256(run_content)[:16],
            "content_sha256": content_sha256(run_content), "content": run_content}


def _budget_week(week=None):
    value = _week_key(week)
    return f"{value[0]}-W{value[1]:02d}"


def _budget_path(root, week=None):
    return Path(root) / _budget_week(week) / "budget.json"


@contextmanager
def _budget_lock(path):
    """Serialize weekly attempt/proposal reservations across workers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + ".lock", "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _read_budget(path, week=None):
    if not Path(path).exists():
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "week": list(_week_key(week)),
            "generation_attempts": [],
            "repair_attempts": [],
            "proposal_ids": [],
            "proposal_count": 0,
        }
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_weekly_proposal_budget") from exc
    if not isinstance(value, dict) or value.get("schema_version") != BUDGET_SCHEMA_VERSION:
        raise ValueError("invalid_weekly_proposal_budget")
    value.setdefault("generation_attempts", [])
    value.setdefault("repair_attempts", [])
    value.setdefault("proposal_ids", [])
    value["proposal_count"] = int(value.get("proposal_count", len(value["proposal_ids"])))
    return value


def _write_budget(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _attempt_response_fields(attempt, prefix="response"):
    digest_key = prefix + "_sha256"
    if digest_key not in attempt:
        return None
    return {
        "response": copy.deepcopy(attempt.get(prefix)),
        "response_sha256": attempt[digest_key],
        "response_source": prefix,
    }


def _repair_replay_response(budget, generation_attempt, input_identity):
    """Find a completed, identity-matched repair response for a generation."""
    linked_id = generation_attempt.get("repair_attempt_id")
    for repair in budget.get("repair_attempts", []):
        if repair.get("status") != "completed" or "response_sha256" not in repair:
            continue
        if linked_id and repair.get("attempt_id") != linked_id:
            continue
        if input_identity is not None and not _attempt_identity_matches(repair, input_identity):
            continue
        return _attempt_response_fields(repair)
    return None


def claim_weekly_attempt(root=DEFAULT_ROOT, week=None, kind="generation", attempt_id=None,
                         input_identity=None, diagnostic_id=None,
                         diagnostic_sha256=None, material_sha256=None):
    """Atomically reserve one weekly model-generation or repair attempt.

    Generation and format-repair reservations are deliberately independent,
    but each has a hard weekly limit of one.  Retrying with the returned
    ``attempt_id`` is idempotent and cannot spend another call.
    """
    if kind not in ("generation", "repair"):
        raise ValueError("unknown_weekly_attempt_kind")
    input_identity = _normalize_attempt_identity(
        input_identity, diagnostic_id, diagnostic_sha256, material_sha256)
    path = _budget_path(root, week)
    field = "generation_attempts" if kind == "generation" else "repair_attempts"
    with _budget_lock(path):
        budget = _read_budget(path, week)
        attempts = budget[field]
        if attempt_id:
            for existing in attempts:
                if existing.get("attempt_id") == attempt_id:
                    if not _attempt_identity_matches(existing, input_identity):
                        return {"status": "identity_mismatch", "attempt_id": attempt_id,
                                "week": _budget_week(week), "kind": kind,
                                "attempt_status": existing.get("status")}
                    result = {"status": "existing", "attempt_id": attempt_id,
                              "week": _budget_week(week), "kind": kind,
                              "attempt_status": existing.get("status")}
                    replay = _attempt_response_fields(existing, "final_response")
                    if replay is None:
                        replay = _attempt_response_fields(existing)
                    if kind == "generation" and "final_response_sha256" not in existing:
                        replay = _repair_replay_response(budget, existing, input_identity) or replay
                    if replay is not None:
                        result.update(replay)
                    if input_identity is not None:
                        result["input_identity"] = copy.deepcopy(input_identity)
                    return result
        if attempts:
            return {"status": "budget_exhausted", "attempt_id": None,
                    "week": _budget_week(week), "kind": kind}
        identifier = attempt_id or f"{_budget_week(week)}:{kind}:1"
        record = {"attempt_id": identifier, "status": "claimed"}
        if input_identity is not None:
            record["input_identity"] = copy.deepcopy(input_identity)
            # Keep the digest fields queryable for operators and older tools;
            # the nested object is the canonical comparison source.
            record.update(input_identity)
        attempts.append(record)
        _write_budget(path, budget)
        result = {"status": "claimed", "attempt_id": identifier,
                  "week": _budget_week(week), "kind": kind}
        if input_identity is not None:
            result["input_identity"] = copy.deepcopy(input_identity)
        return result


def _finish_weekly_attempt(root, week, kind, attempt_id, status, reason=None,
                           response=_NO_RESPONSE, input_identity=None,
                           diagnostic_id=None, diagnostic_sha256=None,
                           material_sha256=None, final_response=_NO_RESPONSE):
    if not attempt_id:
        return
    input_identity = _normalize_attempt_identity(
        input_identity, diagnostic_id, diagnostic_sha256, material_sha256)
    path = _budget_path(root, week)
    field = "generation_attempts" if kind == "generation" else "repair_attempts"
    with _budget_lock(path):
        budget = _read_budget(path, week)
        for attempt in budget[field]:
            if attempt.get("attempt_id") == attempt_id:
                if not _attempt_identity_matches(attempt, input_identity):
                    raise ValueError("weekly_attempt_identity_mismatch")
                if response is not _NO_RESPONSE:
                    digest = content_sha256(response)
                    if ("response_sha256" in attempt
                            and attempt["response_sha256"] != digest):
                        raise ValueError("weekly_attempt_response_conflict")
                    attempt["response"] = copy.deepcopy(response)
                    attempt["response_sha256"] = digest
                if final_response is not _NO_RESPONSE:
                    digest = content_sha256(final_response)
                    if ("final_response_sha256" in attempt
                            and attempt["final_response_sha256"] != digest):
                        raise ValueError("weekly_attempt_final_response_conflict")
                    attempt["final_response"] = copy.deepcopy(final_response)
                    attempt["final_response_sha256"] = digest
                attempt["status"] = status
                if reason:
                    attempt["reason"] = reason
                break
        _write_budget(path, budget)


def _link_weekly_repair(root, week, generation_attempt_id, repair_attempt_id,
                        input_identity=None):
    """Persist generation -> repair linkage before a repair call is made."""
    if not generation_attempt_id or not repair_attempt_id:
        return
    path = _budget_path(root, week)
    with _budget_lock(path):
        budget = _read_budget(path, week)
        for attempt in budget["generation_attempts"]:
            if attempt.get("attempt_id") != generation_attempt_id:
                continue
            if not _attempt_identity_matches(attempt, input_identity):
                raise ValueError("weekly_attempt_identity_mismatch")
            existing = attempt.get("repair_attempt_id")
            if existing and existing != repair_attempt_id:
                raise ValueError("weekly_attempt_repair_conflict")
            attempt["repair_attempt_id"] = repair_attempt_id
            _write_budget(path, budget)
            return
    raise ValueError("weekly_attempt_not_found")


def record_weekly_attempt_response(root=DEFAULT_ROOT, week=None, kind="generation",
                                   attempt_id=None, response=None, input_identity=None,
                                   diagnostic_id=None, diagnostic_sha256=None,
                                   material_sha256=None):
    """Persist one model response under its claimed attempt idempotently."""
    if kind not in ("generation", "repair") or not attempt_id:
        raise ValueError("invalid_weekly_attempt")
    input_identity = _normalize_attempt_identity(
        input_identity, diagnostic_id, diagnostic_sha256, material_sha256)
    path = _budget_path(root, week)
    field = "generation_attempts" if kind == "generation" else "repair_attempts"
    digest = content_sha256(response)
    with _budget_lock(path):
        budget = _read_budget(path, week)
        for attempt in budget[field]:
            if attempt.get("attempt_id") != attempt_id:
                continue
            if not _attempt_identity_matches(attempt, input_identity):
                raise ValueError("weekly_attempt_identity_mismatch")
            if "response_sha256" in attempt:
                if attempt["response_sha256"] != digest:
                    raise ValueError("weekly_attempt_response_conflict")
                return {"status": "unchanged", "attempt_id": attempt_id,
                        "response_sha256": digest}
            attempt["response"] = copy.deepcopy(response)
            attempt["response_sha256"] = digest
            if attempt.get("status") == "claimed":
                attempt["status"] = "response_recorded"
            _write_budget(path, budget)
            return {"status": "recorded", "attempt_id": attempt_id,
                    "response_sha256": digest}
    raise ValueError("weekly_attempt_not_found")


def resume_weekly_attempt(root=DEFAULT_ROOT, week=None, kind="generation", attempt_id=None,
                          input_identity=None, diagnostic_id=None,
                          diagnostic_sha256=None, material_sha256=None):
    """Read a claimed attempt for crash-safe response finalization."""
    if kind not in ("generation", "repair") or not attempt_id:
        raise ValueError("invalid_weekly_attempt")
    input_identity = _normalize_attempt_identity(
        input_identity, diagnostic_id, diagnostic_sha256, material_sha256)
    path = _budget_path(root, week)
    field = "generation_attempts" if kind == "generation" else "repair_attempts"
    with _budget_lock(path):
        budget = _read_budget(path, week)
        for attempt in budget[field]:
            if attempt.get("attempt_id") == attempt_id:
                if not _attempt_identity_matches(attempt, input_identity):
                    raise ValueError("weekly_attempt_identity_mismatch")
                result = {"status": attempt.get("status"),
                          "attempt_id": attempt_id,
                          "week": _budget_week(week), "kind": kind}
                replay = _attempt_response_fields(attempt, "final_response")
                if replay is None:
                    replay = _attempt_response_fields(attempt)
                if kind == "generation" and "final_response_sha256" not in attempt:
                    replay = _repair_replay_response(budget, attempt, input_identity) or replay
                if replay is not None:
                    result.update(replay)
                if input_identity is not None:
                    result["input_identity"] = copy.deepcopy(input_identity)
                return result
    raise ValueError("weekly_attempt_not_found")


def _reserve_proposal_ids(root, run):
    content = run.get("content") or {}
    proposals = content.get("proposals") or []
    if not proposals:
        return {"status": "not_counted", "proposal_count": 0}
    week = content.get("week") or list(_week_key())
    path = _budget_path(root, week)
    with _budget_lock(path):
        budget = _read_budget(path, week)
        proposal_id = str(run.get("proposal_id") or "")
        if proposal_id in budget["proposal_ids"]:
            return {"status": "unchanged", "proposal_count": budget["proposal_count"]}
        count = len(proposals)
        if budget["proposal_count"] + count > MAX_WEEKLY_PROPOSALS:
            return {"status": "budget_exhausted",
                    "proposal_count": budget["proposal_count"],
                    "remaining": max(0, MAX_WEEKLY_PROPOSALS - budget["proposal_count"])}
        budget["proposal_ids"].append(proposal_id)
        budget["proposal_count"] += count
        _write_budget(path, budget)
        return {"status": "reserved", "proposal_count": budget["proposal_count"]}


def _validate_proposal_envelope(run):
    if not isinstance(run, dict) or run.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid_proposal_run")
    content = run.get("content")
    if not isinstance(content, dict):
        raise ValueError("invalid_proposal_run")
    digest = content_sha256(content)
    if run.get("content_sha256") != digest or run.get("proposal_id") != digest[:16]:
        raise ValueError("invalid_proposal_run_digest")
    return run


def generate_weekly_proposals(diagnostics, adapter=None, week=None, root=DEFAULT_ROOT,
                              attempt_id=None):
    """Use an optional adapter once, with at most one format-only repair.

    ``adapter`` can be a callable or an object exposing ``generate`` and,
    optionally, ``repair``.  Provider failures deliberately degrade to a
    persisted no-proposal result so diagnostics remain useful offline.
    """
    verified = _validate_diagnostics_envelope(diagnostics)
    if not verified:
        return build_proposal_run(
            diagnostics, week=week, adapter_status="model_not_attempted",
            usage={"generation_calls": 0, "repair_calls": 0,
                   "generation_status": "not_attempted",
                   "repair_status": "not_attempted"},
        )
    overall = (diagnostics.get("content", diagnostics) or {}).get("overall") or {}
    mature_events, mature_dates, ready = _research_gate(overall)
    if not ready:
        return build_proposal_run(
            diagnostics, week=week,
            adapter_status="model_not_attempted",
            usage={"generation_calls": 0, "repair_calls": 0,
                   "generation_status": "not_attempted",
                   "repair_status": "not_attempted"},
        )
    if adapter is None:
        return build_proposal_run(diagnostics, week=week)
    material = research_material(diagnostics)
    input_identity = _diagnostic_attempt_identity(diagnostics, material)
    claim = claim_weekly_attempt(root, week, "generation", attempt_id=attempt_id,
                                 input_identity=input_identity)
    usage = {"generation_calls": 0, "repair_calls": 0,
             "generation_status": claim["status"],
             "repair_status": "not_attempted",
             "generation_attempt_id": claim.get("attempt_id")}
    if claim["status"] == "budget_exhausted":
        return build_proposal_run(
            diagnostics, week=week, adapter_status="weekly_budget_exhausted",
            usage=usage, attempt_id=claim.get("attempt_id"),
        )
    if claim["status"] == "identity_mismatch":
        usage["generation_status"] = "identity_mismatch"
        return build_proposal_run(
            diagnostics, week=week, adapter_status="weekly_attempt_identity_mismatch",
            usage=usage, attempt_id=claim.get("attempt_id"),
        )
    if claim["status"] == "existing":
        # A response recorded before a worker crash can be finalized without
        # spending another model call.  A claimed-but-empty attempt remains a
        # hard stop until an operator explicitly supplies a response by id.
        if "response_sha256" in claim:
            usage["generation_status"] = "replayed"
            usage["response_sha256"] = claim["response_sha256"]
            return build_proposal_run(
                diagnostics, claim.get("response"), week=week,
                adapter_status="replayed", usage=usage,
                attempt_id=claim.get("attempt_id"),
            )
        usage["generation_status"] = "claimed_without_response"
        return build_proposal_run(
            diagnostics, week=week, adapter_status="weekly_attempt_incomplete",
            usage=usage, attempt_id=claim.get("attempt_id"),
        )
    repair_claim = None
    response = None
    repair_applied = False
    try:
        generate = getattr(adapter, "generate", adapter)
        usage["generation_calls"] = 1
        response = generate(material)
        usage["generation_status"] = "completed"
        _finish_weekly_attempt(root, week, "generation", claim.get("attempt_id"), "completed",
                               response=response, input_identity=input_identity)
        valid, errors = validate_proposals(diagnostics, response)
        if not valid and errors and callable(getattr(adapter, "repair", None)):
            repair_claim = claim_weekly_attempt(root, week, "repair",
                                                input_identity=input_identity)
            usage["repair_status"] = repair_claim["status"]
            if repair_claim["status"] == "claimed":
                _link_weekly_repair(root, week, claim.get("attempt_id"),
                                    repair_claim.get("attempt_id"), input_identity)
                usage["repair_calls"] = 1
                response = adapter.repair(material, response, errors)
                usage["repair_status"] = "completed"
                _finish_weekly_attempt(root, week, "repair", repair_claim.get("attempt_id"), "completed",
                                       response=response, input_identity=input_identity)
                _finish_weekly_attempt(root, week, "generation", claim.get("attempt_id"), "completed",
                                       final_response=response, input_identity=input_identity)
                repair_applied = True
        if not repair_applied:
            _finish_weekly_attempt(root, week, "generation", claim.get("attempt_id"), "completed",
                                   final_response=response, input_identity=input_identity)
        return build_proposal_run(diagnostics, response, week=week,
                                  adapter_status="completed", usage=usage,
                                  attempt_id=claim.get("attempt_id"))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        # If generation completed but a repair/finalization step failed, keep
        # the original response as the deterministic replay fallback.
        if usage.get("generation_status") == "completed" and response is not None:
            try:
                _finish_weekly_attempt(root, week, "generation", claim.get("attempt_id"), "completed",
                                       final_response=response, input_identity=input_identity)
            except ValueError:
                pass
        if usage.get("generation_status") != "completed":
            usage["generation_status"] = "failed"
            _finish_weekly_attempt(root, week, "generation", claim.get("attempt_id"),
                                   "failed", type(exc).__name__,
                                   input_identity=input_identity)
        if usage.get("repair_calls") and usage.get("repair_status") != "completed":
            usage["repair_status"] = "failed"
            _finish_weekly_attempt(root, week, "repair", (repair_claim or {}).get("attempt_id"),
                                   "failed", type(exc).__name__,
                                   input_identity=input_identity)
        return build_proposal_run(diagnostics, week=week,
                                  adapter_status="model_failed:" + type(exc).__name__, usage=usage,
                                  attempt_id=claim.get("attempt_id"))


def save_proposal_run(run, root=DEFAULT_ROOT):
    _validate_proposal_envelope(run)
    reservation = _reserve_proposal_ids(root, run)
    if reservation["status"] == "budget_exhausted":
        return {"status": "budget_exhausted", "path": None,
                "proposal_id": run.get("proposal_id"),
                "remaining": reservation.get("remaining", 0)}
    week = run["content"]["week"]
    path = Path(root) / f"{week[0]}-W{week[1]:02d}" / (run["proposal_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(run) + b"\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    try:
        os.link(temporary, path)
        status = "created"
    except FileExistsError:
        status = "unchanged"
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": status, "path": str(path), "proposal_id": run["proposal_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate bounded evolution proposals")
    parser.add_argument("--diagnostics", required=True)
    parser.add_argument("--response", help="Optional model JSON response; omitted means offline accumulation")
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    diagnostics = json.loads(Path(args.diagnostics).read_text(encoding="utf-8"))
    response = json.loads(Path(args.response).read_text(encoding="utf-8")) if args.response else None
    run = build_proposal_run(diagnostics, response)
    if args.save:
        run["tracking"] = save_proposal_run(run, args.output_root)
    print(json.dumps(run, ensure_ascii=False, sort_keys=True) if args.json else run["content"]["status"])
    return 0


if __name__ == "__main__":
    main()
