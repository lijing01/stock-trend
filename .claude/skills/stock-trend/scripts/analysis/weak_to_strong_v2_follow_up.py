"""Pure lineage-based follow-up for daily weak-to-strong v2 results."""

import copy


SCHEMA_VERSION = "daily-weak-to-strong-follow-up/v2"
RESULT_SCHEMA = "daily-weak-to-strong/v2"

_ACTIVE_GROUPS = (
    "preparing",
    "early_repair",
    "local_confirmation",
    "range_breakout",
    "provisional",
)
_STAGE_RANK = {
    "NONE": 0,
    "PREPARING": 1,
    "EARLY_REPAIR": 2,
    "LOCAL_CONFIRMATION": 3,
    "RANGE_BREAKOUT": 4,
}
_IDENTITY_FIELDS = (
    "code",
    "origin_pattern_type",
    "cycle_id",
    "detector_version",
    "parameter_hash",
)


def _result(payload):
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("result")
    return nested if isinstance(nested, dict) else payload


def _rows(payload, *, include_diagnostics=False):
    result = _result(payload)
    groups = _ACTIVE_GROUPS + (("diagnostics",) if include_diagnostics else ())
    values = []
    for group in groups:
        for item in result.get(group) or []:
            if not isinstance(item, dict):
                continue
            row = copy.deepcopy(item)
            row.setdefault("detector_version", result.get("detector_version"))
            row.setdefault("parameter_hash", result.get("parameter_hash"))
            values.append(row)
            for secondary in item.get("secondary_matches") or []:
                if isinstance(secondary, dict):
                    extra = copy.deepcopy(secondary)
                    extra.setdefault("code", item.get("code"))
                    extra.setdefault("detector_version", result.get("detector_version"))
                    extra.setdefault("parameter_hash", result.get("parameter_hash"))
                    values.append(extra)
    follow_up = result.get("follow_up") or {}
    for item in follow_up.get("rows") or []:
        if not isinstance(item, dict) or item.get("active") is not True:
            continue
        row = copy.deepcopy(item)
        row["stage"] = row.get("current_stage") or row.get("origin_stage")
        row.setdefault("signal_date", row.get("origin_signal_date"))
        row.setdefault("detector_version", result.get("detector_version"))
        row.setdefault("parameter_hash", result.get("parameter_hash"))
        values.append(row)
    return values


def _identity(row):
    values = tuple(str(row.get(field) or "") for field in _IDENTITY_FIELDS)
    return values if all(values) else None


def _public_identity(key):
    return dict(zip(_IDENTITY_FIELDS, key))


def _compatible(previous, current):
    previous = _result(previous)
    current = _result(current)
    for payload in (previous, current):
        schema = str(payload.get("schema_version") or "")
        if schema and schema != RESULT_SCHEMA:
            return False, "schema_version_mismatch"
    previous_version = str(previous.get("detector_version") or "")
    current_version = str(current.get("detector_version") or "")
    if previous and not previous_version:
        return False, "previous_detector_version_missing"
    if current and not current_version:
        return False, "current_detector_version_missing"
    if previous_version and current_version and previous_version != current_version:
        return False, "detector_version_changed"
    previous_parameters = str(previous.get("parameter_hash") or "")
    current_parameters = str(current.get("parameter_hash") or "")
    if previous and not previous_parameters:
        return False, "previous_parameter_hash_missing"
    if current and not current_parameters:
        return False, "current_parameter_hash_missing"
    if (previous_parameters and current_parameters
            and previous_parameters != current_parameters):
        return False, "parameter_hash_changed"
    return True, ""


def _pending(prior, key, current_date, reason):
    return {
        **_public_identity(key),
        "name": prior.get("name", ""),
        "origin_stage": prior.get("stage"),
        "current_stage": None,
        "origin_signal_date": prior.get("signal_date") or prior.get("stage_first_at"),
        "as_of_date": current_date,
        "follow_up_status": "PENDING_REVIEW",
        "active": True,
        "reason_codes": [reason],
    }


def build_v2_follow_up(previous_payload, current_payload):
    """Compare two frozen v2 outputs by immutable cycle lineage.

    Absence alone never invalidates a cycle.  Only an evidence-ready diagnostic
    carrying an explicit terminal lifecycle status may end it.
    """
    previous = _result(previous_payload)
    current = _result(current_payload)
    compatible, reason = _compatible(previous, current)
    current_date = str(current.get("as_of_date") or "")
    output = {
        "schema_version": SCHEMA_VERSION,
        "status": "loaded" if compatible else "incompatible",
        "reason": reason,
        "baseline": {
            "as_of_date": str(previous.get("as_of_date") or ""),
            "detector_version": previous.get("detector_version"),
            "parameter_hash": previous.get("parameter_hash"),
        },
        "as_of_date": current_date,
        "rows": [],
    }
    if not compatible:
        return output

    prior_by_key = {}
    prior_rows = _rows(previous)
    if any(row.get("stage") in _STAGE_RANK and _identity(row) is None
           for row in prior_rows):
        output.update(status="incompatible", reason="baseline_identity_incomplete")
        return output
    for row in prior_rows:
        key = _identity(row)
        if key and row.get("stage") in _STAGE_RANK:
            prior_by_key.setdefault(key, row)
    if not prior_by_key:
        output.update(status="missing", reason="baseline_missing")
        return output

    current_by_key = {}
    current_by_code = {}
    terminal_by_key = {}
    for row in _rows(current, include_diagnostics=True):
        key = _identity(row)
        if not key:
            continue
        current_by_code.setdefault(key[0], []).append(row)
        lifecycle = str(row.get("lifecycle_status") or "")
        if lifecycle in {"invalidated", "expired", "completed"}:
            terminal_by_key[key] = row
        elif row.get("stage") in _STAGE_RANK:
            current_by_key.setdefault(key, row)

    for key, prior in sorted(prior_by_key.items()):
        terminal = terminal_by_key.get(key)
        current_row = current_by_key.get(key)
        evidence = (current_row or terminal or {}).get("evidence_status")
        confirmation = (current_row or terminal or {}).get("confirmation")
        if evidence != "ready" or confirmation in {"provisional", "evidence_incomplete"}:
            reason_code = ("current_cycle_missing" if current_row is None and terminal is None
                           else "current_evidence_incomplete")
            if current_row is None and terminal is None and current_by_code.get(key[0]):
                reason_code = "cycle_lineage_changed"
            output["rows"].append(_pending(prior, key, current_date, reason_code))
            continue

        base = {
            **_public_identity(key),
            "name": current_row.get("name", prior.get("name", ""))
                    if current_row else prior.get("name", ""),
            "origin_stage": prior.get("stage"),
            "current_stage": current_row.get("stage") if current_row else None,
            "origin_signal_date": prior.get("signal_date") or prior.get("stage_first_at"),
            "as_of_date": current_date,
            "reason_codes": [],
        }
        if terminal is not None:
            lifecycle = terminal["lifecycle_status"]
            base.update(
                follow_up_status=lifecycle.upper(),
                active=False,
                reason_codes=list(terminal.get("reason_codes") or []),
            )
        else:
            old_rank = _STAGE_RANK[prior["stage"]]
            new_rank = _STAGE_RANK[current_row["stage"]]
            status = ("STAGE_UPGRADED" if new_rank > old_rank else
                      "DOWNGRADED" if new_rank < old_rank else "CONTINUED")
            base.update(
                follow_up_status=status,
                active=True,
                signal_date=current_row.get("signal_date"),
                stage_first_at=current_row.get("stage_first_at"),
            )
        output["rows"].append(base)
    return output


__all__ = ["SCHEMA_VERSION", "build_v2_follow_up"]
