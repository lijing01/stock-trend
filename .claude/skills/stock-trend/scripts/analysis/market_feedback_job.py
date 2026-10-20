"""Immutable orchestration for strong-stock cohort and next-session feedback."""

import copy
import fcntl
import json
import os
import queue
import tempfile
import threading
from datetime import date, datetime, time
from pathlib import Path

from analysis.market_feedback import (
    SHANGHAI,
    aware_datetime,
    build_feedback,
    canonical_digest,
    compare_close_feedback,
    digest_valid,
    finite,
    valid_date,
)


COHORT_SCHEMA = "strong-stock-cohort/v1"
ARTIFACT_SCHEMA = "market-feedback-artifact/v1"
FETCH_TIMEOUT_SECONDS = 30.0


def _now():
    return datetime.now(SHANGHAI)


def _calendar_previous(calendar, basis_date):
    if not isinstance(calendar, dict) or calendar.get("schema") != "review-time/v1":
        return None, "calendar_invalid"
    days = calendar.get("trading_dates")
    if (not isinstance(days, list) or not days or days != sorted(set(days))
            or any(valid_date(day) is None for day in days)
            or calendar.get("coverage") != {"start": days[0], "end": days[-1]}
            or not calendar.get("source")):
        return None, "calendar_invalid"
    if basis_date not in days:
        return None, "calendar_current_session_missing"
    position = days.index(basis_date)
    if position == 0:
        return None, "previous_session_outside_calendar_coverage"
    return days[position - 1], ""


def _write_first(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            return "unchanged"
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                return "created"
            except FileExistsError:
                return "unchanged"
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _load_verified(path, *, schema=None, cutoff=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if schema and value.get("schema_version") != schema:
        return None
    if not digest_valid(value):
        return None
    frozen = aware_datetime(value.get("frozen_at") or value.get("created_at"))
    if cutoff is not None and (frozen is None or frozen > cutoff):
        return None
    return value


def _load_cohort(cache_dir, day, cutoff):
    value = _load_verified(
        Path(cache_dir) / "strong_stock_cohorts" / f"{day}.json",
        schema=COHORT_SCHEMA, cutoff=cutoff)
    frozen = aware_datetime((value or {}).get("frozen_at"))
    close_at = (datetime.combine(date.fromisoformat(day), time(15, 10), SHANGHAI)
                if valid_date(day) else None)
    if (value is None or value.get("basis_date") != day or frozen is None
            or frozen.astimezone(SHANGHAI).date().isoformat() != day
            or close_at is None or frozen.astimezone(SHANGHAI) < close_at):
        return None
    return value


def _pool_members(pool, core_members, basis_date, cutoff):
    by_code = {}
    for raw in pool.get("members") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        if not code:
            continue
        row = by_code.setdefault(code, {
            "code": code, "name": str(raw.get("name") or code),
            "close": finite(raw.get("close")), "streak": finite(raw.get("streak")),
            "roles": [], "selection_reason": "",
        })
        if "limit_up" not in row["roles"]:
            row["roles"].append("limit_up")
        incoming_streak = finite(raw.get("streak"))
        if incoming_streak is not None and (row["streak"] is None or incoming_streak > row["streak"]):
            row["streak"] = incoming_streak
    streaks = [row["streak"] for row in by_code.values() if row["streak"] is not None]
    maximum = max(streaks, default=None)
    if maximum is not None and maximum >= 2:
        for row in by_code.values():
            if row["streak"] == maximum:
                row["roles"].append("height_core")
                row["selection_reason"] = f"当日最高连板高度{maximum:g}板（规则样本，非正式市场核心）"

    # Market core is accepted only as an already frozen same-day selection
    # with an explicit role and reason. Generic candidate rows never enter.
    source = core_members if isinstance(core_members, dict) else {}
    frozen = aware_datetime(source.get("frozen_at"))
    same_day = source.get("basis_date") == basis_date
    core_close = datetime.combine(date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    source_digest = source.get("content_sha256")
    digest_ok = not source_digest or digest_valid(source)
    if (same_day and frozen is not None and frozen <= cutoff and digest_ok
            and frozen.astimezone(SHANGHAI).date().isoformat() == basis_date
            and frozen.astimezone(SHANGHAI) >= core_close):
        for raw in source.get("members") or []:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or "").strip().upper()
            roles = raw.get("roles") or ([raw.get("role")] if raw.get("role") else [])
            reason = str(raw.get("selection_reason") or "").strip()
            if not code or "core" not in roles or not reason:
                continue
            row = by_code.setdefault(code, {
                "code": code, "name": str(raw.get("name") or code),
                "close": None, "streak": finite(raw.get("streak")),
                "roles": [], "selection_reason": reason,
            })
            if "core" not in row["roles"]:
                row["roles"].append("core")
            row["selection_reason"] = reason
    return sorted(by_code.values(), key=lambda row: row["code"])


def _eligible_pool(pool, basis_date, cutoff, actual_now):
    fetched = aware_datetime(pool.get("fetched_at")) if isinstance(pool, dict) else None
    close_at = datetime.combine(date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    return bool(
        isinstance(pool, dict)
        and pool.get("data_date") == basis_date
        and pool.get("date_origin") == "provider"
        and pool.get("complete") is True
        and pool.get("mode") == "close"
        and bool(pool.get("source"))
        and isinstance(pool.get("members"), list)
        and fetched is not None
        and fetched.astimezone(SHANGHAI) >= close_at
        and fetched <= cutoff
        and cutoff.astimezone(SHANGHAI) >= close_at
        and actual_now.astimezone(SHANGHAI).date().isoformat() == basis_date
        and actual_now.astimezone(SHANGHAI) >= close_at
    )


def _freeze_cohort(ctx, pool, basis_date, cutoff, cache_dir, actual_now):
    if not _eligible_pool(pool, basis_date, cutoff, actual_now):
        return None, "current_cohort_not_freezable"
    members = _pool_members(pool, ctx.get("market_core_members"), basis_date, cutoff)
    payload = {
        "schema_version": COHORT_SCHEMA,
        "basis_date": basis_date,
        "frozen_at": actual_now.isoformat(),
        "source": pool.get("source"),
        "source_fetched_at": pool.get("fetched_at"),
        "pool_evidence": _json_safe(pool),
        "core_source": _json_safe(ctx.get("market_core_members")),
        "selection_rules": {
            "limit_up": "all_provider_limit_up_members",
            "height_core": "all_members_at_daily_max_streak_when_max_gte_2",
            "core": "explicit_same_day_frozen_market_core_only",
        },
        "members": members,
    }
    payload["content_sha256"] = canonical_digest(payload)
    path = Path(cache_dir) / "strong_stock_cohorts" / f"{basis_date}.json"
    status = _write_first(path, payload)
    frozen = _load_cohort(cache_dir, basis_date, cutoff)
    if frozen is None:
        return None, "existing_cohort_invalid"
    return frozen, status


def _bounded_fetch(basis_date, prior_members, as_of):
    result_queue = queue.Queue(maxsize=1)

    def run():
        try:
            from fetchers.market_feedback import fetch_inputs
            result_queue.put((fetch_inputs(
                basis_date, prior_members, as_of=as_of,
                budget_seconds=FETCH_TIMEOUT_SECONDS - 1.0), None))
        except Exception as exc:  # provider failures are represented, not raised
            result_queue.put((None, f"fetch_error:{type(exc).__name__}"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(FETCH_TIMEOUT_SECONDS)
    if thread.is_alive():
        return {"pool": {}, "quotes": [], "errors": ["fetch_timeout"]}
    try:
        value, error = result_queue.get_nowait()
    except queue.Empty:
        return {"pool": {}, "quotes": [], "errors": ["fetch_empty"]}
    if not isinstance(value, dict):
        return {"pool": {}, "quotes": [], "errors": [error or "fetch_invalid"]}
    value.setdefault("errors", [])
    if error:
        value["errors"].append(error)
    return value


def _state_from_analysis(analysis, reasons):
    items = []
    for row in analysis.get("members") or []:
        items.append({
            "code": row.get("code"), "name": row.get("name"),
            "roles": list(row.get("roles") or []), "streak": row.get("streak"),
            "pct_chg": row.get("pct_chg"), "status": row.get("feedback"),
            "close": row.get("close"), "volume": row.get("volume"),
            "selection_reason": row.get("selection_reason"),
            "quote_source": row.get("quote_source"),
            "quote_fetched_at": row.get("quote_fetched_at"),
        })
    normalized_reasons = []
    for reason in reasons:
        if not reason:
            continue
        if isinstance(reason, dict):
            normalized = f"fetch_{str(reason.get('stage') or 'input')}_error"
        elif isinstance(reason, str):
            normalized = reason
        else:
            normalized = json.dumps(reason, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str)
        if normalized not in normalized_reasons:
            normalized_reasons.append(normalized)
    return {
        "schema_version": "market-feedback/v1",
        "status": "complete" if not normalized_reasons else "degraded",
        "rule_version": analysis.get("rule_version"),
        "cohort_date": analysis.get("cohort_date"),
        "basis_date": analysis.get("basis_date"),
        "as_of": analysis.get("as_of"),
        "mode": analysis.get("mode"),
        "reasons": normalized_reasons,
        "reason": normalized_reasons[0] if normalized_reasons else "",
        "items": items,
        "summary": copy.deepcopy(analysis.get("summary") or {}),
        "groups": copy.deepcopy(analysis.get("groups") or {}),
        "measurement": analysis.get("measurement"),
        "notes": list(analysis.get("notes") or []),
    }


def _close_path(cache_dir, day):
    return Path(cache_dir) / "market_feedback_close" / f"{day}.json"


def _persist_close(state, cache_dir, actual_now, cutoff):
    if (state.get("mode") != "close" or (state.get("summary") or {}).get("coverage") != 1
            or actual_now.astimezone(SHANGHAI).date().isoformat() != state.get("basis_date")):
        return None
    payload = copy.deepcopy(state)
    payload.update({"schema_version": "market-feedback-close/v1",
                    "frozen_at": actual_now.isoformat()})
    payload["content_sha256"] = canonical_digest(payload)
    path = _close_path(cache_dir, state["basis_date"])
    _write_first(path, payload)
    return _load_close(cache_dir, state["basis_date"], cutoff)


def _load_close(cache_dir, day, cutoff):
    value = _load_verified(
        _close_path(cache_dir, day), schema="market-feedback-close/v1",
        cutoff=cutoff)
    frozen = aware_datetime((value or {}).get("frozen_at"))
    close_at = (datetime.combine(date.fromisoformat(day), time(15, 10), SHANGHAI)
                if valid_date(day) else None)
    if (value is None or value.get("basis_date") != day
            or value.get("rule_version") != "strong-stock-next-session-feedback/v1"
            or (value.get("summary") or {}).get("coverage") != 1
            or value.get("mode") != "close" or frozen is None or close_at is None
            or frozen.astimezone(SHANGHAI).date().isoformat() != day
            or frozen.astimezone(SHANGHAI) < close_at):
        return None
    return value


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if finite(value) is not None else None
    return str(value)


def _save_artifact(state, cache_dir, actual_now, inputs):
    artifact = {
        "schema_version": ARTIFACT_SCHEMA,
        "basis_date": state.get("basis_date"),
        "created_at": actual_now.isoformat(),
        "state": state,
        "inputs": _json_safe(inputs),
    }
    artifact["content_sha256"] = canonical_digest(artifact)
    directory = Path(cache_dir) / "market_feedback_artifacts" / str(state.get("basis_date"))
    path = directory / f"{artifact['content_sha256']}.json"
    _write_first(path, artifact)
    return path, artifact["content_sha256"]


def _load_bound(ctx, cutoff, cache_dir):
    binding = ctx.get("market_feedback_binding") if isinstance(ctx, dict) else None
    if not isinstance(binding, dict):
        return None
    path = binding.get("artifact_path") or binding.get("path")
    if not path:
        return None
    basis_date = binding.get("basis_date")
    expected_root = (Path(cache_dir) / "market_feedback_artifacts" /
                     str(basis_date)).resolve()
    resolved = Path(path).resolve()
    digest = binding.get("content_sha256")
    if (not isinstance(digest, str) or resolved.parent != expected_root
            or resolved.name != f"{digest}.json"
            or basis_date != ctx.get("data_date")):
        return None
    artifact = _load_verified(resolved, schema=ARTIFACT_SCHEMA, cutoff=cutoff)
    if artifact is None or artifact.get("content_sha256") != binding.get("content_sha256"):
        return None
    state = copy.deepcopy(artifact.get("state") or {})
    state_as_of = aware_datetime(state.get("as_of"))
    if (artifact.get("basis_date") != basis_date or state.get("basis_date") != basis_date
            or state_as_of is None or state_as_of > cutoff):
        return None
    state["artifact_path"] = str(resolved)
    state["run_binding"] = copy.deepcopy(binding)
    return state


def collect_feedback(ctx, *, calendar, as_of, cache_dir, no_refresh=False):
    """Collect feedback and bind the exact immutable artifact to this run."""
    cutoff = aware_datetime(as_of)
    if cutoff is None:
        raise ValueError("as_of must include timezone")
    if no_refresh:
        bound = _load_bound(ctx, cutoff, cache_dir)
        if bound is not None:
            return bound
        return {
            "schema_version": "market-feedback/v1",
            "basis_date": ctx.get("data_date") if isinstance(ctx, dict) else None,
            "mode": "missing", "reason": "market_feedback_binding_missing",
            "reasons": ["market_feedback_binding_missing"], "items": [],
            "summary": {}, "comparison": {"status": "unavailable",
                "reason": "market_feedback_binding_missing"},
        }

    basis_date = ctx.get("data_date") if isinstance(ctx, dict) else None
    if not valid_date(basis_date):
        raise ValueError("ctx.data_date must be an ISO date")
    prior_date, calendar_reason = _calendar_previous(calendar, basis_date)
    runtime_start = _now()
    if (cutoff.astimezone(SHANGHAI).date().isoformat() != basis_date
            or runtime_start.astimezone(SHANGHAI).date().isoformat() != basis_date):
        return {
            "schema_version": "market-feedback/v1",
            "status": "unavailable", "basis_date": basis_date,
            "mode": "unavailable", "reason": "historical_live_collection_forbidden",
            "reasons": ["historical_live_collection_forbidden"], "items": [],
            "summary": {}, "comparison": {"status": "unavailable",
                "reason": "historical_live_collection_forbidden"},
        }
    feedback_day_start = datetime.combine(
        date.fromisoformat(basis_date), time(0, 0), SHANGHAI)
    prior_cohort = (_load_cohort(cache_dir, prior_date, feedback_day_start)
                    if prior_date else None)
    prior_members = (prior_cohort or {}).get("members") or []
    fetched = _bounded_fetch(basis_date, prior_members, runtime_start)
    actual_now = _now()
    # Preserve provider-qualified observations during calendar outages. Using
    # them later still requires the verified adjacent session above.
    frozen_current, freeze_status = _freeze_cohort(
        ctx, fetched.get("pool") or {}, basis_date, actual_now,
        cache_dir, actual_now)
    cohort_freeze = {
        "status": freeze_status,
        "path": str(Path(cache_dir) / "strong_stock_cohorts" /
                    f"{basis_date}.json") if frozen_current else None,
        "content_sha256": (frozen_current or {}).get("content_sha256"),
        "calendar_status": calendar_reason or "verified",
    }

    reasons = list(fetched.get("errors") or [])
    if cohort_freeze.get("status") == "current_cohort_not_freezable":
        reasons.append("current_cohort_not_freezable")
    if calendar_reason:
        reasons.append(calendar_reason)
    if prior_date and prior_cohort is None:
        reasons.append("adjacent_cohort_missing")
    analysis = build_feedback(
        prior_cohort or {"basis_date": prior_date, "members": []},
        fetched.get("quotes") or [], basis_date=basis_date, as_of=actual_now)
    state = _state_from_analysis(analysis, reasons)
    state["cohort_freeze"] = cohort_freeze
    state["report_as_of"] = cutoff.isoformat()
    state["evidence_as_of"] = actual_now.isoformat()
    if prior_cohort is None:
        state["status"] = "unavailable"
        state["summary"] = {}
        state["groups"] = {}

    current_close = _persist_close(state, cache_dir, actual_now, actual_now)
    comparison = {"status": "unavailable", "reason": "intraday_not_comparable"}
    if state["mode"] == "close":
        previous_close = (_load_close(
            cache_dir, prior_date, feedback_day_start) if prior_date else None)
        comparison = compare_close_feedback(current_close, previous_close)
        comparison["reason"] = next(iter(comparison.get("reasons") or []), "")
    state["comparison"] = comparison
    path, digest = _save_artifact(state, cache_dir, actual_now, {
        "pool": fetched.get("pool") or {},
        "quotes": fetched.get("quotes") or [],
        "calendar": calendar,
        "prior_cohort": prior_cohort,
    })
    binding = {"artifact_path": str(path), "content_sha256": digest,
               "basis_date": basis_date, "as_of": state.get("as_of"),
               "report_as_of": state.get("report_as_of")}
    state["artifact_path"] = str(path)
    state["run_binding"] = binding
    ctx["market_feedback_binding"] = copy.deepcopy(binding)
    return state
