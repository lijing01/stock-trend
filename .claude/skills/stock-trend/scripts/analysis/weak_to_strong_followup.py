"""Pure follow-up evaluation for daily weak-to-strong signals."""

from __future__ import annotations

import copy
import math


RULE_VERSION = "weak-to-strong-follow-up/v1"
ACTIVE_STATUSES = {"ENHANCED", "CONTINUED", "PENDING_REVIEW"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _basis(item):
    evidence = item.get("kline_evidence") if isinstance(item, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    return str(evidence.get("basis") or item.get("basis") or "").lower()


def _tracking_count(item):
    """Return an auditable persisted count, preserving legacy unknowns."""
    value = item.get("tracking_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def build_follow_up_baseline(item, source_date, *, latest_close=None):
    """Freeze fields needed to evaluate a signal on the next session."""
    if not isinstance(item, dict):
        return None
    if item.get("follow_up_status") == "INVALIDATED" or item.get("active") is False:
        return None
    origin_state = str(item.get("origin_state") or item.get("state") or "")
    if origin_state not in {"BREAKOUT_CONFIRMED", "SUPPLY_DRY"}:
        return None
    previous_close = _number(latest_close)
    if previous_close is None:
        previous_close = _number(item.get("latest_valid_close"))
    if previous_close is None and origin_state == "BREAKOUT_CONFIRMED":
        previous_close = _number(item.get("Ct"))
    is_follow_up = bool(item.get("follow_up_status"))
    tracking_count = _tracking_count(item) if is_follow_up else 0
    tracking_last_date = ""
    if is_follow_up and tracking_count is not None:
        tracking_last_date = str(item.get("tracking_last_date") or
                                 item.get("data_date") or item.get("as_of_date") or "")
    return {
        "rule_version": RULE_VERSION,
        "code": str(item.get("code") or ""),
        "ts_code": str(item.get("ts_code") or ""),
        "name": str(item.get("name") or ""),
        "origin_state": origin_state,
        "initial_origin_state": str(item.get("initial_origin_state") or origin_state),
        "origin_signal_date": str(item.get("origin_signal_date") or
                                  item.get("signal_date") or source_date or ""),
        "origin_cycle_id": str(item.get("origin_cycle_id") or item.get("cycle_id") or ""),
        "source_date": str(source_date or ""),
        "previous_status": str(item.get("follow_up_status") or origin_state),
        "previous_close": previous_close,
        "H": _number(item.get("H")),
        "L": _number(item.get("L")),
        "basis": _basis(item),
        "sector_memberships": copy.deepcopy(item.get("sector_memberships") or []),
        "tracking_count": tracking_count,
        "tracking_last_date": tracking_last_date,
    }


def _result(baseline, status, reasons, *, latest_close, data_date,
            current_result, threshold=None):
    tracking_count = _tracking_count(baseline)
    tracking_last_date = str(baseline.get("tracking_last_date") or "")
    current_tracking_date = str(data_date or "")
    if tracking_count is not None and current_tracking_date:
        if current_tracking_date != tracking_last_date:
            tracking_count += 1
        tracking_last_date = current_tracking_date
    return {
        **copy.deepcopy(baseline),
        "rule_version": RULE_VERSION,
        "as_of_date": str(data_date or ""),
        "follow_up_status": status,
        "active": status in ACTIVE_STATUSES,
        "reason_codes": list(dict.fromkeys(reasons)),
        "latest_valid_close": (latest_close if status != "PENDING_REVIEW"
                               else baseline.get("previous_close")),
        "current_close": latest_close,
        "threshold_H": threshold,
        "current_state": str(current_result.get("state") or ""),
        "current_cycle_id": str(current_result.get("cycle_id") or ""),
        "evidence_status": str(current_result.get("evidence_status") or ""),
        "tracking_count": tracking_count,
        "tracking_last_date": tracking_last_date,
    }


def evaluate_follow_up(baseline, current_result, *, latest_close,
                       data_date, close_confirmed=None):
    """Return ENHANCED, CONTINUED, INVALIDATED or PENDING_REVIEW."""
    if not isinstance(baseline, dict) or not isinstance(current_result, dict):
        raise TypeError("baseline and current_result must be mappings")
    close = _number(latest_close)
    evidence = current_result.get("kline_evidence") or {}
    evidence = evidence if isinstance(evidence, dict) else {}
    confirmation = evidence.get("close_confirmation") or {}
    if close_confirmed is None:
        close_confirmed = bool(isinstance(confirmation, dict) and
                               confirmation.get("confirmed") is True and
                               confirmation.get("source") != "not_required")
    current_date = str(current_result.get("as_of_date") or "")
    reasons = []
    if current_result.get("evidence_status") != "ready":
        reasons.append("current_evidence_incomplete")
    if not close_confirmed:
        reasons.append("close_unconfirmed")
    if close is None:
        reasons.append("current_close_missing")
    if not data_date or current_date != str(data_date):
        reasons.append("data_date_mismatch")
    old_basis = str(baseline.get("basis") or "").lower()
    new_basis = _basis(current_result)
    if not old_basis or not new_basis:
        reasons.append("adjustment_basis_missing")
    elif old_basis != new_basis:
        reasons.append("adjustment_basis_changed")
    origin_state = str(baseline.get("origin_state") or "")
    cycle_id = str(baseline.get("origin_cycle_id") or "")
    if origin_state not in {"BREAKOUT_CONFIRMED", "SUPPLY_DRY"}:
        reasons.append("origin_state_unsupported")
    if not cycle_id:
        reasons.append("origin_cycle_missing")
    threshold = _number(baseline.get("H"))
    if reasons:
        return _result(baseline, "PENDING_REVIEW", reasons,
                       latest_close=close, data_date=data_date,
                       current_result=current_result, threshold=threshold)

    current_cycle = str(current_result.get("cycle_id") or "")
    if origin_state == "SUPPLY_DRY":
        if current_cycle != cycle_id:
            return _result(baseline, "PENDING_REVIEW", ["cycle_not_reliably_linked"],
                           latest_close=close, data_date=data_date,
                           current_result=current_result, threshold=threshold)
        state = current_result.get("state")
        outcomes = {
            "BREAKOUT_CONFIRMED": ("ENHANCED", "supply_dry_breakout_confirmed"),
            "SUPPLY_DRY": ("CONTINUED", "supply_dry_continues"),
            "INVALID": ("INVALIDATED", "same_cycle_invalidated"),
        }
        status, reason = outcomes.get(state, ("PENDING_REVIEW", "same_cycle_state_unresolved"))
        output = _result(baseline, status, [reason], latest_close=close,
                         data_date=data_date, current_result=current_result,
                         threshold=threshold)
        if state == "BREAKOUT_CONFIRMED":
            output["origin_state"] = "BREAKOUT_CONFIRMED"
            output["origin_signal_date"] = str(
                current_result.get("signal_date") or data_date or "")
        return output

    previous_close = _number(baseline.get("previous_close"))
    if threshold is None or previous_close is None:
        missing = "threshold_missing" if threshold is None else "previous_close_missing"
        return _result(baseline, "PENDING_REVIEW", [missing], latest_close=close,
                       data_date=data_date, current_result=current_result,
                       threshold=threshold)
    if current_cycle and current_cycle != cycle_id:
        return _result(baseline, "PENDING_REVIEW", ["new_cycle_replaced_original"],
                       latest_close=close, data_date=data_date,
                       current_result=current_result, threshold=threshold)
    if close < threshold:
        status, reason = "INVALIDATED", "close_below_breakout_threshold"
    elif close > previous_close:
        status, reason = "ENHANCED", "close_above_previous_valid_close"
    else:
        status, reason = "CONTINUED", "close_holds_breakout_threshold"
    return _result(baseline, status, [reason], latest_close=close,
                   data_date=data_date, current_result=current_result,
                   threshold=threshold)


__all__ = ["RULE_VERSION", "build_follow_up_baseline", "evaluate_follow_up"]
