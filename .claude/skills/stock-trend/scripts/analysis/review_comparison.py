"""Pure adjacent-session review comparison and immutable snapshot storage.

The comparison contract never searches backwards past a missing authoritative
session.  Score comparisons are deliberately stricter than raw-turnover
comparisons so legacy history can remain useful without being silently
promoted to the current model methodology.
"""

import copy
import fcntl
import json
import math
import os
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from core.recommendation_snapshot import canonical_json, content_sha256


SCHEMA_VERSION = "review-comparison/v1"
SNAPSHOT_SCHEMA_VERSION = "review-snapshot/v1"
POINTER_SCHEMA_VERSION = "review-snapshot-pointer/v1"
COMPONENT_ORDER = (
    "index_trend", "volume", "breadth", "zt_emotion", "capital",
)
COMPONENT_NAMES = {
    "index_trend": "大盘趋势",
    "volume": "成交额",
    "breadth": "赚钱效应",
    "zt_emotion": "涨停情绪",
    "capital": "资金",
}
DEFAULT_WEIGHTS = {
    "index_trend": 0.25,
    "volume": 0.20,
    "breadth": 0.25,
    "zt_emotion": 0.20,
    "capital": 0.10,
}
SHANGHAI = ZoneInfo("Asia/Shanghai")
RECONCILIATION_TOLERANCE = 1e-9


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_date(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _aware_datetime(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def _explanation(ctx):
    value = ctx.get("market_explanation") or {}
    return value if isinstance(value, dict) else {}


def _qualification(ctx):
    value = ctx.get("conclusion_qualification")
    if not isinstance(value, dict):
        value = _explanation(ctx).get("conclusion_qualification")
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _component_rows(ctx):
    rows = _explanation(ctx).get("components") or []
    return {
        row.get("id"): row for row in rows
        if isinstance(row, dict) and row.get("id") in COMPONENT_ORDER
    }


def _detail_coverage(ctx, component_id):
    detail = (ctx.get("detail_inputs") or {}).get(component_id) or {}
    if component_id == "volume":
        return {
            "coverage": detail.get("coverage"),
            "history_sample_count": detail.get("history_sample_count"),
            "baseline_method": "mean_positive_prior_amounts_up_to_20",
        }
    if component_id == "breadth":
        return {
            "coverage": detail.get("coverage"),
            "industry_count": detail.get("industry_count"),
        }
    if component_id == "zt_emotion":
        count = detail.get("history_sample_count")
        return {
            "history_sample_count": count,
            "baseline_method": (
                "mean_positive_prior_counts_up_to_20"
                if isinstance(count, int) and count >= 5
                else "absolute_count_50"
            ),
        }
    if component_id == "index_trend":
        return {"index_codes": sorted(
            str(item.get("code")) for item in detail.get("indices") or []
            if isinstance(item, dict) and item.get("code"))}
    return {}


def _coverage_projection(ctx, component_id, row):
    evidence = row.get("evidence") or {}
    return {
        "completeness": evidence.get("completeness"),
        "usage": evidence.get("usage"),
        "metric": evidence.get("metric"),
        "provider": evidence.get("provider"),
        "source_kind": evidence.get("source_kind"),
        "expected_count": evidence.get("expected_count"),
        "available_count": evidence.get("available_count"),
        "index_codes": list(evidence.get("index_codes") or []),
        "detail_inputs": _detail_coverage(ctx, component_id),
    }


def _amount_projection(ctx, basis_date, mode):
    evidence = ctx.get("amount_evidence") or {}
    value = _finite(ctx.get("amount_yi"))
    reasons = []
    if value is None or value <= 0:
        reasons.append("amount_missing")
    if evidence.get("completeness") != "complete":
        reasons.append("amount_evidence_incomplete")
    if evidence.get("usage") != "scorable":
        reasons.append("amount_evidence_not_scorable")
    if evidence.get("date_origin") != "provider" or evidence.get("data_date") != basis_date:
        reasons.append("amount_date_unqualified")
    markets = sorted((evidence.get("per_index") or {}).keys())
    if markets != ["000001.SH", "399106.SZ"]:
        reasons.append("amount_range_unqualified")
    if mode != "close":
        reasons.append("current_not_completed_close")
    return {
        "value": value,
        "data_date": evidence.get("data_date"),
        "coverage": markets,
        "qualified": not reasons,
        "reasons": reasons,
    }


def _component_coverage_complete(component_id, coverage):
    if (coverage.get("completeness") != "complete"
            or coverage.get("usage") != "scorable"
            or not coverage.get("metric")
            or not coverage.get("provider")
            or not coverage.get("source_kind")):
        return False
    detail = coverage.get("detail_inputs") or {}
    if component_id == "index_trend":
        return bool(detail.get("index_codes"))
    if component_id == "volume":
        return bool(detail.get("coverage")) and detail.get("history_sample_count") is not None \
            and bool(detail.get("baseline_method"))
    if component_id == "breadth":
        return bool(detail.get("coverage")) and detail.get("industry_count") is not None
    if component_id == "zt_emotion":
        return detail.get("history_sample_count") is not None \
            and bool(detail.get("baseline_method"))
    return True


def build_review_snapshot(ctx, *, frozen_at, source_digest=None):
    """Detach the minimum comparison summary from one frozen review context."""
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")
    basis_date = _valid_date(ctx.get("data_date"))
    frozen = _aware_datetime(frozen_at)
    if not basis_date:
        raise ValueError("invalid basis date")
    if frozen is None:
        raise ValueError("frozen_at must include timezone")
    explanation = _explanation(ctx)
    mode = (
        "intraday" if ctx.get("intraday") is True
        else explanation.get("mode") or "close"
    )
    rows = _component_rows(ctx)
    denominator = _finite(explanation.get("normalization_denominator"))
    if denominator is None:
        denominator = _finite((ctx.get("regime") or {}).get("normalization_denominator"))
    components = {}
    for component_id in COMPONENT_ORDER:
        row = rows.get(component_id) or {}
        fallback = (ctx.get("components") or {}).get(component_id) or {}
        # The source component retains formula precision; explanation rows are
        # display-rounded and must not drive contribution reconciliation.
        score = _finite(fallback.get("score"))
        if score is None:
            score = _finite(row.get("score"))
        weight = _finite(row.get("weight"))
        if weight is None:
            weight = DEFAULT_WEIGHTS[component_id]
        contribution = (
            score * weight / denominator
            if score is not None and weight is not None and denominator
            else None
        )
        components[component_id] = {
            "score": score,
            "weight": weight,
            "contribution": contribution,
            "coverage": _coverage_projection(ctx, component_id, row),
        }
    qualification = _qualification(ctx)
    score = _finite(explanation.get("score"))
    if score is None:
        score = _finite((ctx.get("regime") or {}).get("score"))
    model_score = (
        sum(item["score"] * item["weight"] for item in components.values()) / denominator
        if denominator and all(item["score"] is not None and item["weight"] is not None
                               for item in components.values())
        else None
    )
    completed_at = datetime.combine(
        date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    close_completed = frozen.astimezone(SHANGHAI) >= completed_at
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "basis_date": basis_date,
        "frozen_at": frozen.isoformat(),
        "source_digest": source_digest or ctx.get("content_sha256") or content_sha256(ctx),
        "model_version": ctx.get("model_version"),
        "methodology_version": ctx.get("methodology_version"),
        "mode": mode,
        "score": score,
        "model_score": model_score,
        "close_completed": close_completed,
        "normalization_denominator": denominator,
        "components": components,
        "indicator_coverage": {
            key: copy.deepcopy(value["coverage"])
            for key, value in components.items()
        },
        "conclusion_qualification": qualification,
        "reconciliation": explanation.get("reconciliation"),
        "amount": _amount_projection(ctx, basis_date, mode),
    }
    snapshot["eligible_close"] = bool(
        mode == "close"
        and close_completed
        and snapshot["model_version"]
        and snapshot["methodology_version"]
        and qualification.get("eligible") is True
        and qualification.get("expected_date") in (None, basis_date)
        and snapshot["reconciliation"] == "matched"
        and score is not None
        and denominator is not None and denominator > 0
        and all(item["score"] is not None and item["weight"] is not None
                for item in components.values())
        and all(_component_coverage_complete(key, item["coverage"])
                for key, item in components.items())
    )
    snapshot["content_sha256"] = content_sha256(snapshot)
    return snapshot


def _snapshot_valid(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return False
    expected = snapshot.get("content_sha256")
    content = copy.deepcopy(snapshot)
    content.pop("content_sha256", None)
    return bool(expected and expected == content_sha256(content))


def _write_immutable(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    os.close(descriptor)
    try:
        with open(temporary_name, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, path)
            return "created"
        except FileExistsError:
            return "unchanged"
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _atomic_replace(path, payload):
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(descriptor)
    try:
        with open(temporary_name, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def persist_review_snapshot(snapshot, cache_dir):
    """Store immutable history and atomically update only an eligible pointer."""
    if not _snapshot_valid(snapshot):
        raise ValueError("invalid review snapshot digest")
    basis_date = _valid_date(snapshot.get("basis_date"))
    if not basis_date:
        raise ValueError("invalid basis date")
    directory = Path(cache_dir) / "review_snapshots" / basis_date
    directory.mkdir(parents=True, exist_ok=True)
    digest = snapshot["content_sha256"]
    target = directory / f"{digest}.json"
    lock_path = directory / ".lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        status = _write_immutable(target, snapshot)
        if not snapshot.get("eligible_close"):
            return {"status": "stored_ineligible", "path": str(target),
                    "content_sha256": digest, "pointer_updated": False}
        pointer = {
            "schema_version": POINTER_SCHEMA_VERSION,
            "basis_date": basis_date,
            "content_sha256": digest,
            "artifact": target.name,
            "frozen_at": snapshot["frozen_at"],
            "source_digest": snapshot["source_digest"],
        }
        pointer_path = directory / "eligible.json"
        try:
            existing = json.loads(pointer_path.read_text(encoding="utf-8"))
            existing_frozen = _aware_datetime(existing.get("frozen_at"))
        except (OSError, ValueError, TypeError):
            existing_frozen = None
        incoming_frozen = _aware_datetime(snapshot["frozen_at"])
        if existing_frozen is not None and existing_frozen > incoming_frozen:
            return {"status": status, "path": str(target),
                    "content_sha256": digest, "pointer_updated": False,
                    "reason": "newer_eligible_pointer_exists"}
        _atomic_replace(pointer_path, pointer)
        return {"status": status, "path": str(target),
                "content_sha256": digest, "pointer_updated": True}


def load_eligible_snapshot(basis_date, cache_dir, *, as_of):
    """Load a digest-verified eligible close that existed by the replay cutoff."""
    basis_date = _valid_date(basis_date)
    cutoff = _aware_datetime(as_of)
    if not basis_date or cutoff is None:
        return None
    directory = Path(cache_dir) / "review_snapshots" / basis_date
    candidates = []
    try:
        pointer = json.loads((directory / "eligible.json").read_text(encoding="utf-8"))
        frozen = _aware_datetime(pointer.get("frozen_at"))
        digest = pointer.get("content_sha256")
        artifact_name = pointer.get("artifact")
        if (pointer.get("schema_version") != POINTER_SCHEMA_VERSION
                or pointer.get("basis_date") != basis_date
                or artifact_name != f"{digest}.json"):
            raise ValueError("invalid pointer")
        if frozen is not None and frozen <= cutoff:
            candidates.append(directory / artifact_name)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    # The pointer is the newest eligible artifact, so historical replay may
    # need an older immutable artifact that predates its cutoff.
    candidates.extend(sorted(
        path for path in directory.glob("*.json")
        if path.name != "eligible.json"
    ))
    eligible = []
    for path in dict.fromkeys(candidates):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            frozen = _aware_datetime(snapshot.get("frozen_at"))
            digest = snapshot.get("content_sha256")
            if (not _snapshot_valid(snapshot) or path.name != f"{digest}.json"
                    or snapshot.get("basis_date") != basis_date
                    or snapshot.get("eligible_close") is not True
                    or frozen is None or frozen > cutoff):
                continue
            eligible.append((frozen, digest, snapshot))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return max(eligible, key=lambda item: (item[0], item[1]))[2] if eligible else None


def _legacy_provider_frozen_at(entry, cutoff):
    timestamps = []
    for key in ("amount_evidence", "zt_evidence", "activity_evidence"):
        evidence = entry.get(key) or {}
        if isinstance(evidence, dict) and (evidence.get("provider") or evidence.get("source_kind")):
            value = _aware_datetime(evidence.get("fetched_at"))
            if value is not None:
                timestamps.append(value)
    for evidence in (entry.get("component_evidence") or {}).values():
        if isinstance(evidence, dict) and (evidence.get("provider") or evidence.get("source_kind")):
            value = _aware_datetime(evidence.get("fetched_at"))
            if value is not None:
                timestamps.append(value)
    timestamps = [value for value in timestamps if value <= cutoff]
    return max(timestamps) if timestamps else None


def _legacy_amount_freeze_status(entry, basis_date, cutoff, direct_frozen):
    evidence = entry.get("amount_evidence") or \
        (entry.get("component_evidence") or {}).get("volume") or {}
    completed_at = datetime.combine(
        date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    raw_timestamp = evidence.get("fetched_at") if isinstance(evidence, dict) else None
    if raw_timestamp:
        amount_frozen = _aware_datetime(raw_timestamp)
        if amount_frozen is None:
            return False, "amount_freeze_unknown"
        if amount_frozen > cutoff:
            return False, "amount_frozen_after_cutoff"
        if amount_frozen.astimezone(SHANGHAI) < completed_at:
            return False, "legacy_close_not_completed"
        return True, None
    if direct_frozen is None:
        return False, "freeze_unknown"
    if direct_frozen.astimezone(SHANGHAI) < completed_at:
        return False, "legacy_close_not_completed"
    return True, None


def _legacy_snapshot(entry, basis_date, cutoff):
    """Project old history without inventing model compatibility."""
    if not isinstance(entry, dict):
        return None
    if entry.get("schema_version") == SNAPSHOT_SCHEMA_VERSION:
        return copy.deepcopy(entry) if _snapshot_valid(entry) else None
    direct_frozen = _aware_datetime(entry.get("frozen_at") or entry.get("generated_at"))
    if direct_frozen is not None and direct_frozen > cutoff:
        return None
    verified_frozen = direct_frozen or _legacy_provider_frozen_at(entry, cutoff)
    amount_close_completed, amount_freeze_reason = _legacy_amount_freeze_status(
        entry, basis_date, cutoff, direct_frozen)
    raw_components = entry.get("components") or {}
    components = {}
    for key in COMPONENT_ORDER:
        raw = raw_components.get(key)
        score = _finite(raw.get("score")) if isinstance(raw, dict) else _finite(raw)
        components[key] = {
            "score": score, "weight": None, "contribution": None,
            "coverage": {},
        }
    legacy_mode = "intraday" if entry.get("intraday") else "close"
    amount_ctx = {
        "amount_yi": entry.get("amount_yi"),
        "amount_evidence": entry.get("amount_evidence") or
            (entry.get("component_evidence") or {}).get("volume") or {},
    }
    result = {
        "schema_version": "legacy-market-history",
        "basis_date": basis_date,
        "frozen_at": verified_frozen.isoformat() if verified_frozen else None,
        "freeze_verified": verified_frozen is not None,
        "close_completed": amount_close_completed,
        "model_version": entry.get("model_version"),
        "methodology_version": entry.get("methodology_version"),
        "mode": legacy_mode,
        "score": _finite(entry.get("regime_score")),
        "normalization_denominator": _finite(entry.get("normalization_denominator")),
        "components": components,
        "indicator_coverage": entry.get("indicator_coverage") or {},
        "conclusion_qualification": copy.deepcopy(entry.get("conclusion_qualification") or {}),
        "reconciliation": entry.get("reconciliation"),
        "amount": _amount_projection(amount_ctx, basis_date, legacy_mode),
        "eligible_close": False,
    }
    if not amount_close_completed:
        result["amount"]["qualified"] = False
        result["amount"]["reasons"] = _dedupe(
            list(result["amount"].get("reasons") or []) + [amount_freeze_reason])
    return result


def _calendar_previous(calendar, basis_date):
    if not isinstance(calendar, dict) or calendar.get("schema") != "review-time/v1":
        return None, ["calendar_invalid"]
    days = calendar.get("trading_dates")
    coverage = calendar.get("coverage")
    if (not isinstance(days, list) or not days or days != sorted(set(days))
            or any(_valid_date(day) is None for day in days)
            or coverage != {"start": days[0], "end": days[-1]}
            or not calendar.get("source")):
        return None, ["calendar_invalid"]
    if basis_date not in days:
        return None, ["calendar_current_session_missing"]
    position = days.index(basis_date)
    if position == 0:
        return None, ["previous_session_outside_calendar_coverage"]
    return days[position - 1], []


def _field(status, current=None, previous=None, change=None, reasons=None,
           percent_change=None):
    return {
        "status": status,
        "current": current,
        "previous": previous,
        "change": change,
        "percent_change": percent_change,
        "reasons": _dedupe(reasons or []),
    }


def _score_compatibility(current, previous):
    reasons = []
    for key, missing_reason, mismatch_reason in (
        ("model_version", "model_version_missing", "model_version_mismatch"),
        ("methodology_version", "methodology_version_missing", "methodology_version_mismatch"),
    ):
        if not current.get(key) or not previous.get(key):
            reasons.append(missing_reason)
        elif current[key] != previous[key]:
            reasons.append(mismatch_reason)
    if current.get("mode") != "close" or previous.get("mode") != "close":
        reasons.append("close_qualification_mismatch")
    if current.get("eligible_close") is not True:
        reasons.append("current_close_not_eligible")
    if previous.get("eligible_close") is not True:
        reasons.append("previous_close_not_eligible")
    if current.get("reconciliation") != "matched" or previous.get("reconciliation") != "matched":
        reasons.append("snapshot_reconciliation_unqualified")
    if (current.get("conclusion_qualification") or {}).get("eligible") is not True:
        reasons.append("current_close_unqualified")
    if (previous.get("conclusion_qualification") or {}).get("eligible") is not True:
        reasons.append("previous_close_unqualified")
    current_denominator = _finite(current.get("normalization_denominator"))
    previous_denominator = _finite(previous.get("normalization_denominator"))
    if current_denominator is None or previous_denominator is None:
        reasons.append("normalization_denominator_missing")
    elif abs(current_denominator - previous_denominator) > 1e-9:
        reasons.append("normalization_denominator_mismatch")
    if current.get("indicator_coverage") != previous.get("indicator_coverage"):
        reasons.append("indicator_coverage_mismatch")
    current_components = current.get("components") or {}
    previous_components = previous.get("components") or {}
    if set(current_components) != set(COMPONENT_ORDER) or set(previous_components) != set(COMPONENT_ORDER):
        reasons.append("component_coverage_mismatch")
    else:
        for key in COMPONENT_ORDER:
            current_weight = _finite((current_components[key] or {}).get("weight"))
            previous_weight = _finite((previous_components[key] or {}).get("weight"))
            if current_weight is None or previous_weight is None:
                reasons.append("component_weight_missing")
                break
            if abs(current_weight - previous_weight) > 1e-9:
                reasons.append("component_weight_mismatch")
                break
            if (_finite((current_components[key] or {}).get("score")) is None
                    or _finite((previous_components[key] or {}).get("score")) is None):
                reasons.append("component_score_missing")
                break
    if _finite(current.get("score")) is None or _finite(previous.get("score")) is None:
        reasons.append("score_missing")
    if (_finite(current.get("model_score")) is None
            or _finite(previous.get("model_score")) is None):
        reasons.append("model_score_missing")
    return _dedupe(reasons)


def _empty_result(basis_date, mode, prior_date=None, reasons=None):
    unavailable = _field("unavailable", reasons=reasons)
    return {
        "schema_version": SCHEMA_VERSION,
        "basis_date": basis_date,
        "mode": mode,
        "prior_session_date": prior_date,
        "actual_baseline_date": None,
        "session_gap": None,
        "status": "unavailable",
        "reasons": _dedupe(reasons or []),
        "score": copy.deepcopy(unavailable),
        "components": {key: copy.deepcopy(unavailable) for key in COMPONENT_ORDER},
        "amount": copy.deepcopy(unavailable),
        "main_reasons": [],
        "contribution_reconciliation": {"status": "unavailable"},
        "previous_close_reference": None,
    }


def _attach_persistence(result, persistence):
    if persistence is not None:
        result["snapshot_persistence"] = persistence
    return result


def prepare_comparison(ctx, calendar, *, as_of, cache_dir, persist=False,
                       source_history=None, frozen_at=None):
    """Compare one review only with its authoritative adjacent session."""
    cutoff = _aware_datetime(as_of)
    if cutoff is None:
        raise ValueError("as_of must include timezone")
    frozen = _aware_datetime(frozen_at) if frozen_at is not None else cutoff
    if frozen is None:
        raise ValueError("frozen_at must include timezone")
    current = build_review_snapshot(ctx, frozen_at=frozen.isoformat())
    if date.fromisoformat(current["basis_date"]) > cutoff.astimezone(SHANGHAI).date():
        current["eligible_close"] = False
        current.pop("content_sha256", None)
        current["content_sha256"] = content_sha256(current)
    persistence = None
    if persist:
        try:
            persistence = persist_review_snapshot(current, cache_dir)
        except Exception as exc:
            persistence = {"status": "error", "reason": type(exc).__name__}
    basis_date = current["basis_date"]
    prior_date, calendar_reasons = _calendar_previous(calendar, basis_date)
    if calendar_reasons:
        return _attach_persistence(
            _empty_result(basis_date, current["mode"], reasons=calendar_reasons),
            persistence)
    previous = load_eligible_snapshot(prior_date, cache_dir, as_of=cutoff)
    if previous is None and isinstance(source_history, dict):
        previous = _legacy_snapshot(source_history.get(prior_date), prior_date, cutoff)
    if previous is None:
        return _attach_persistence(_empty_result(
            basis_date, current["mode"], prior_date,
            ["adjacent_snapshot_missing"]), persistence)

    result = _empty_result(basis_date, current["mode"], prior_date)
    result["actual_baseline_date"] = prior_date
    result["session_gap"] = 1
    previous_components = previous.get("components") or {}
    result["previous_close_reference"] = {
        "date": prior_date,
        "score": previous.get("score"),
        "components": {
            key: (previous_components.get(key) or {}).get("score")
            for key in COMPONENT_ORDER
        },
    }

    if current["mode"] != "close":
        reason = "intraday_vs_close_not_comparable"
        result["status"] = "reference_only"
        result["reasons"] = [reason]
        result["score"] = _field(
            "reference_only", current["score"], previous.get("score"),
            reasons=[reason])
        result["components"] = {
            key: _field(
                "reference_only", (current["components"].get(key) or {}).get("score"),
                (previous_components.get(key) or {}).get("score"), reasons=[reason])
            for key in COMPONENT_ORDER
        }
        result["amount"] = _field(
            "reference_only", current["amount"].get("value"),
            (previous.get("amount") or {}).get("value"),
            reasons=["current_not_completed_close"])
        return _attach_persistence(result, persistence)

    compatibility = _score_compatibility(current, previous)
    if compatibility:
        result["score"] = _field(
            "unavailable", current.get("score"), previous.get("score"),
            reasons=compatibility)
        result["components"] = {
            key: _field(
                "unavailable", (current["components"].get(key) or {}).get("score"),
                (previous_components.get(key) or {}).get("score"), reasons=compatibility)
            for key in COMPONENT_ORDER
        }
    else:
        score_change = round(current["score"] - previous["score"], 3)
        model_score_change = current["model_score"] - previous["model_score"]
        result["score"] = _field(
            "comparable", current["score"], previous["score"], score_change)
        rows = []
        for key in COMPONENT_ORDER:
            current_component = current["components"][key]
            previous_component = previous_components[key]
            score_delta = round(
                current_component["score"] - previous_component["score"], 3)
            contribution_delta = (
                current_component["contribution"]
                - previous_component["contribution"]
            )
            result["components"][key] = _field(
                "comparable", current_component["score"],
                previous_component["score"], score_delta)
            rows.append({
                "component_id": key,
                "name": COMPONENT_NAMES[key],
                "current_contribution": current_component["contribution"],
                "previous_contribution": previous_component["contribution"],
                "change": contribution_delta,
            })
        rows.sort(key=lambda row: (-abs(row["change"]), COMPONENT_ORDER.index(row["component_id"])))
        contribution_change = sum(row["change"] for row in rows)
        model_residual = model_score_change - contribution_change
        rounding_residual = score_change - model_score_change
        reconciliation = (
            "matched" if abs(model_residual) <= RECONCILIATION_TOLERANCE
            else "mismatch"
        )
        result["main_reasons"] = rows
        result["contribution_reconciliation"] = {
            "status": reconciliation,
            "displayed_score_change": score_change,
            "model_score_change": model_score_change,
            "contribution_change_sum": contribution_change,
            "model_residual": model_residual,
            "rounding_residual": rounding_residual,
            "tolerance": RECONCILIATION_TOLERANCE,
        }
        if reconciliation == "mismatch":
            result["score"]["status"] = "unavailable"
            result["score"]["reasons"] = ["weighted_contribution_mismatch"]
            for field in result["components"].values():
                field["status"] = "unavailable"
                field["reasons"] = ["weighted_contribution_mismatch"]
            result["main_reasons"] = []

    current_amount = current.get("amount") or {}
    previous_amount = previous.get("amount") or {}
    amount_reasons = list(current_amount.get("reasons") or []) + list(
        previous_amount.get("reasons") or [])
    current_value = _finite(current_amount.get("value"))
    previous_value = _finite(previous_amount.get("value"))
    if current_amount.get("coverage") != previous_amount.get("coverage"):
        amount_reasons.append("amount_coverage_mismatch")
    if (current_amount.get("qualified") and previous_amount.get("qualified")
            and current_amount.get("coverage") == previous_amount.get("coverage")
            and current_value is not None and previous_value is not None
            and previous_value > 0):
        change = current_value - previous_value
        result["amount"] = _field(
            "comparable", current_value, previous_value, change,
            percent_change=round(change / previous_value * 100, 2))
    else:
        result["amount"] = _field(
            "unavailable", current_value, previous_value,
            reasons=amount_reasons or ["amount_evidence_unqualified"])

    score_ok = result["score"]["status"] == "comparable"
    amount_ok = result["amount"]["status"] == "comparable"
    result["status"] = "comparable" if score_ok else (
        "reference_only" if amount_ok else "unavailable")
    result["reasons"] = _dedupe(
        result["score"]["reasons"] + result["amount"]["reasons"])
    return _attach_persistence(result, persistence)
