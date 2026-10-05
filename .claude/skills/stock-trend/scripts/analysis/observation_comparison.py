"""Pure comparison of two frozen YAML observation-list artifacts."""

import hashlib
import json
import math
from datetime import date

SCHEMA = "observation-comparison/v1"
V2_SCHEMA = "yaml-observation-analysis/v2"
V1_SCHEMA = "yaml-observation-analysis/v1"


def canonical_digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _market_for_code(code):
    code = str(code or "")
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    if code.startswith(("4", "8")):
        return "BJ"
    return ""


def _identity(row):
    code = str(row.get("code") or "")
    return (str(row.get("market") or _market_for_code(code)), code)


def _identity_dict(identity):
    return {"market": identity[0], "code": identity[1]}


def _artifact_rows(artifact):
    rows = artifact.get("items") if isinstance(artifact, dict) else []
    return rows if isinstance(rows, list) else []


def _index_rows(artifact):
    indexed = {}
    order = []
    for row in _artifact_rows(artifact):
        if not isinstance(row, dict):
            continue
        identity = _identity(row)
        if not all(identity) or identity in indexed:
            continue
        indexed[identity] = row
        order.append(identity)
    return indexed, order


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _complete_data(row):
    quality = row.get("data_quality") or {}
    return quality.get("eligible") is True and _number(
        row.get("raw_composite_score")) and _number(
            row.get("quality_adjusted_score"))


def _structure_state(row):
    wyckoff = row.get("wyckoff") or {}
    health = wyckoff.get("event_health") or {}
    short = wyckoff.get("short_term") or {}
    signal = wyckoff.get("signal") or {}
    value = str(health.get("state") or short.get("current_state")
                or signal.get("current_state") or "").strip().lower()
    if not value:
        return "unknown"
    if value in {"invalid", "invalidated", "failed", "expired", "broken",
                 "failed_breakout", "structure_invalidated"}:
        return "invalid"
    if value in {"valid", "confirmed_holding"}:
        return "valid"
    return "unknown"


def _confirmed_event(row, cutoff_date):
    event = (row.get("wyckoff") or {}).get("confirmed_event") or {}
    if not isinstance(event, dict) or event.get("status") != "confirmed":
        return None
    event_type = event.get("type") or event.get("event")
    confirmation = str(event.get("confirmation_date") or event.get("detected_date") or "")
    if len(confirmation) == 8 and confirmation.isdigit():
        confirmation = f"{confirmation[:4]}-{confirmation[4:6]}-{confirmation[6:]}"
    try:
        if date.fromisoformat(confirmation) > date.fromisoformat(cutoff_date):
            return None
    except (TypeError, ValueError):
        return None
    if not event_type:
        return None
    # Age, confidence and rolling row indices evolve without a new event.
    return {"type": event_type, "status": "confirmed",
            "event_date": event.get("event_date"), "range_id": event.get("range_id"),
            "confirmation_date": confirmation}


def _base_result(current, previous, expected_previous_date):
    return {
        "schema": SCHEMA, "status": "degraded", "reason_code": "",
        "reason": "", "current_date": current.get("data_date") if isinstance(
            current, dict) else None,
        "previous_date": previous.get("data_date") if isinstance(
            previous, dict) else None,
        "expected_previous_date": expected_previous_date,
        "comparison_scope": "unavailable", "collection_changes": {
            "added": [], "removed": [], "wording": "artifact_record_set",
        }, "events": [], "rows": [],
    }


def _reject(result, code, reason):
    result["reason_code"] = code
    result["reason"] = reason
    return result


def _score_guard(current_artifact, previous_artifact, current, previous):
    if not current_artifact.get("scoring_model_version") or current_artifact.get("scoring_model_version") != previous_artifact.get(
            "scoring_model_version"):
        return "model_version_mismatch"
    if current.get("joined_date") != previous.get("joined_date"):
        return "code_reentered"
    if current.get("row_config_sha256") != previous.get("row_config_sha256"):
        return "row_config_changed"
    if current.get("quality_method") != previous.get("quality_method"):
        return "quality_method_mismatch"
    if current.get("quality_digest") != previous.get("quality_digest"):
        return "quality_changed"
    if not _complete_data(current) or not _complete_data(previous):
        return "score_evidence_incomplete"
    return ""


def _event(identity, kind, priority, **extra):
    return {
        "type": kind, "priority": priority,
        **_identity_dict(identity), **extra,
    }


def compare_observation_artifacts(current, previous, *,
                                  expected_previous_date=None,
                                  as_of_date=None):
    """Compare adjacent completed artifacts without reading files or YAML.

    The caller owns authoritative calendar selection.  A missing expected date
    degrades deterministically rather than searching older artifacts.
    """
    current = current if isinstance(current, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    result = _base_result(current, previous, expected_previous_date)
    if not expected_previous_date:
        return _reject(result, "previous_trading_date_unavailable",
                       "缺少权威的相邻上一交易日")
    if previous.get("status") == "unavailable" or not previous:
        result["previous_reason_code"] = previous.get("reason_code") or "artifact_missing"
        return _reject(result, "adjacent_artifact_missing", "相邻上一交易日没有合格观察 artifact")
    if current.get("status") == "unavailable":
        return _reject(result, current.get("reason_code") or "current_artifact_unavailable",
                       current.get("reason") or "当前观察 artifact 不合格")
    try:
        current_date = date.fromisoformat(str(current.get("data_date") or ""))
        previous_date = date.fromisoformat(str(previous.get("data_date") or ""))
        expected = date.fromisoformat(str(expected_previous_date))
        cutoff = date.fromisoformat(str(as_of_date)) if as_of_date else None
    except ValueError:
        return _reject(result, "invalid_artifact_date", "观察 artifact 日期无效")
    if cutoff and (current_date > cutoff or previous_date > cutoff):
        return _reject(result, "future_artifact", "观察 artifact 晚于报告截止日")
    if previous_date != expected or previous_date >= current_date:
        return _reject(result, "adjacent_artifact_missing",
                       "相邻上一交易日没有合格观察 artifact")
    if current.get("provisional") or previous.get("provisional"):
        return _reject(result, "incomplete_trading_day",
                       "盘中观察 artifact 不作跨日变化比较")

    if current.get("schema") not in {V1_SCHEMA, V2_SCHEMA} or previous.get("schema") not in {V1_SCHEMA, V2_SCHEMA}:
        return _reject(result, "unsupported_schema", "观察 artifact schema 不支持")
    if any(artifact.get("schema") == V2_SCHEMA and artifact.get("evidence_cutoff_date") != artifact.get("data_date")
           for artifact in (current, previous)):
        return _reject(result, "evidence_cutoff_mismatch", "观察证据截止日不匹配")
    current_rows, current_order = _index_rows(current)
    previous_rows, previous_order = _index_rows(previous)
    current_ids, previous_ids = set(current_rows), set(previous_rows)
    added = [identity for identity in current_order if identity not in previous_ids]
    removed = [identity for identity in previous_order if identity not in current_ids]
    both_v2 = current.get("schema") == previous.get("schema") == V2_SCHEMA
    result["collection_changes"] = {
        "added": [_identity_dict(identity) for identity in added],
        "removed": [_identity_dict(identity) for identity in removed],
        "wording": "yaml_config" if both_v2 else "artifact_record_set",
    }
    result["comparison_scope"] = "full" if both_v2 else "collection_only"
    result["status"] = "ready" if both_v2 else "partial"
    if not both_v2:
        result["reason_code"] = "legacy_collection_only"
        result["reason"] = "旧版 artifact 仅支持记录集合新增/移除"

    for identity in current_order:
        if identity not in previous_rows:
            continue
        current_row = current_rows[identity]
        previous_row = previous_rows[identity]
        row_result = {
            **_identity_dict(identity), "score_status": "unavailable",
            "structure_status": "unavailable", "reason_code": "",
            "previous_raw_score": previous_row.get("raw_composite_score"),
            "current_raw_score": current_row.get("raw_composite_score"),
        }
        if not both_v2:
            row_result["reason_code"] = "legacy_collection_only"
            result["rows"].append(row_result)
            continue

        was_complete = _complete_data(previous_row)
        is_complete = _complete_data(current_row)
        guard = _score_guard(current, previous, current_row, previous_row)
        if (not was_complete and is_complete and guard in {
                "", "quality_changed", "score_evidence_incomplete"}):
            result["events"].append(_event(identity, "data_recovered", 30))
        if guard:
            row_result["reason_code"] = guard
            if guard in {"model_version_mismatch", "code_reentered", "row_config_changed"}:
                result["rows"].append(row_result)
                continue
        else:
            row_result["score_status"] = "comparable"
            row_result["raw_score_delta"] = round(
                current_row["raw_composite_score"] - previous_row["raw_composite_score"], 4)
            row_result["quality_adjusted_score_delta"] = round(
                current_row["quality_adjusted_score"] - previous_row["quality_adjusted_score"], 4)
        # Structure evidence is independent of unrelated dimension quality.
        current_rule = (current_row.get("wyckoff") or {}).get("event_health", {}).get("rule_version")
        previous_rule = (previous_row.get("wyckoff") or {}).get("event_health", {}).get("rule_version")
        if current_rule != previous_rule:
            row_result["structure_reason_code"] = "structure_method_mismatch"
            result["rows"].append(row_result)
            continue
        row_result["structure_status"] = "comparable"

        previous_state = _structure_state(previous_row)
        current_state = _structure_state(current_row)
        if previous_state == "valid" and current_state == "invalid":
            result["events"].append(_event(
                identity, "structure_invalidated", 10))
        elif previous_state == "invalid" and current_state == "valid":
            result["events"].append(_event(
                identity, "structure_restored", 20))

        confirmed = _confirmed_event(current_row, current_date.isoformat())
        prior_confirmed = _confirmed_event(previous_row, previous_date.isoformat())
        if confirmed and canonical_digest(confirmed) != canonical_digest(
                prior_confirmed):
            result["events"].append(_event(
                identity, "confirmed_evidence", 40,
                event_type=confirmed.get("type") or confirmed.get("event"),
                confirmation_date=confirmed.get("confirmation_date")
                or confirmed.get("detected_date") or ""))
        result["rows"].append(row_result)

    result["events"].sort(key=lambda item: (
        item["priority"], item["market"], item["code"]))
    return result
