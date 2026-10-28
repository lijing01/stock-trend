#!/usr/bin/env python3
"""Pure daily weak-to-strong signal detector.

The detector owns no I/O.  It replays a frozen daily OHLCV payload up to an
explicit as-of date and keeps formal close/calendar evidence separate from the
technical pattern.  This makes the result reproducible and prevents an
intraday bar from being labelled as a confirmed breakout.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date


SCHEMA_VERSION = "daily-weak-to-strong/v1"
DETECTOR_VERSION = "1.0.0"
DEFAULT_CONFIG = {
    "min_history_bars": 20,
    "ma_window": 10,
    "min_up_days": 2,
    "max_days_after_d0": 5,
    "max_drawdown": 0.12,
    "ma10_floor_factor": 0.98,
    "max_adjustment_volume_ratio": 0.80,
    "max_last_volume_ratio": 0.90,
    "min_breakout_volume_ratio": 1.20,
    "min_close_strength": 0.75,
    "max_breakout_ratio": 1.05,
}


def _canonical_digest(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalise_date(value):
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
    except ValueError:
        return ""


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _empty_result(as_of_date, config, reasons=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "parameter_hash": _canonical_digest(config),
        "as_of_date": _normalise_date(as_of_date),
        "state": "WAIT",
        "confirmation": "none",
        "provisional": False,
        "current_bar_evaluated": False,
        "evaluation_date": "",
        "evidence_status": "data_insufficient" if reasons else "ready",
        "reason_codes": list(dict.fromkeys(reasons or [])),
        "cycle_id": "",
        "d0_date": "",
        "up_start_date": "",
        "up_days": 0,
        "adjustment_days": 0,
        "signal_date": "",
        "H": None,
        "L": None,
        "d0_high": None,
        "V0": None,
        "Va": None,
        "Vlast": None,
        "Ct": None,
        "Vt": None,
        "drawdown_ratio": None,
        "va_v0_ratio": None,
        "last_va_ratio": None,
        "breakout_volume_ratio": None,
        "close_strength": None,
        "breakout_ratio": None,
        "provisional_match": False,
        "checks": {},
        "kline_evidence": {},
        "events": [],
    }


def _normalise_rows(payload, as_of_date):
    raw_rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(raw_rows, list):
        return [], ["kline_rows_invalid"], []
    rows_by_date = {}
    duplicate_dates = []
    reasons = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            reasons.append("kline_row_invalid")
            continue
        date = _normalise_date(raw.get("trade_date") or raw.get("date"))
        if not date:
            reasons.append("kline_date_invalid")
            continue
        if date > as_of_date:
            continue
        values = {
            key: _finite_number(raw.get(key))
            for key in ("open", "high", "low", "close")
        }
        volume = _finite_number(raw.get("vol", raw.get("volume")))
        if any(value is None for value in values.values()) or volume is None:
            reasons.append("kline_value_invalid")
            continue
        row = {"date": date, **values, "volume": volume}
        if (min(row["open"], row["high"], row["low"], row["close"]) <= 0
                or row["volume"] <= 0
                or row["high"] < row["low"]
                or row["high"] < max(row["open"], row["close"])
                or row["low"] > min(row["open"], row["close"])):
            reasons.append("kline_ohlcv_invalid")
            continue
        old = rows_by_date.get(date)
        if old is not None:
            duplicate_dates.append(date)
            if old != row:
                reasons.append("kline_duplicate_conflict")
            continue
        rows_by_date[date] = row
    return ([rows_by_date[key] for key in sorted(rows_by_date)],
            list(dict.fromkeys(reasons)), sorted(set(duplicate_dates)))


def _config(value):
    config = dict(DEFAULT_CONFIG)
    if value is not None:
        if not isinstance(value, dict):
            return config, ["config_invalid"]
        unknown = set(value) - set(DEFAULT_CONFIG)
        if unknown:
            return config, ["config_unknown_key"]
        config.update(value)
    integer_keys = {"min_history_bars", "ma_window", "min_up_days",
                    "max_days_after_d0"}
    for key, number in config.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return config, ["config_invalid"]
        if key in integer_keys and (int(number) != number or number <= 0):
            return config, ["config_invalid"]
        if key not in integer_keys and number <= 0:
            return config, ["config_invalid"]
    if config["min_history_bars"] < config["ma_window"] + 2:
        return config, ["config_invalid"]
    return config, []


def _calendar_status(calendar_evidence, rows, required_start, as_of_date):
    if not isinstance(calendar_evidence, dict):
        return "data_insufficient", ["calendar_missing"], {}
    raw_dates = (calendar_evidence.get("trading_dates")
                 or calendar_evidence.get("dates"))
    if not isinstance(raw_dates, (list, tuple, set)):
        return "data_insufficient", ["calendar_missing"], {}
    dates = sorted({_normalise_date(value) for value in raw_dates
                    if _normalise_date(value)})
    coverage_start = _normalise_date(calendar_evidence.get("coverage_start"))
    coverage_end = _normalise_date(calendar_evidence.get("coverage_end"))
    detail = {
        "source": str(calendar_evidence.get("source") or ""),
        "identity": str(calendar_evidence.get("identity")
                        or calendar_evidence.get("sha256") or ""),
        "coverage_start": coverage_start or (dates[0] if dates else ""),
        "coverage_end": coverage_end or (dates[-1] if dates else ""),
    }
    start = required_start or (rows[-1]["date"] if rows else as_of_date)
    if (not dates or detail["coverage_start"] > start
            or detail["coverage_end"] < as_of_date
            or as_of_date not in dates):
        return "data_insufficient", ["calendar_coverage_incomplete"], detail
    expected = {date for date in dates if start <= date <= as_of_date}
    present = {row["date"] for row in rows if start <= row["date"] <= as_of_date}
    if not expected.issubset(present):
        return "data_insufficient", ["calendar_gap_or_suspension"], detail
    return "ready", [], detail


def _ma(rows, index, window):
    if index + 1 < window:
        return None
    return sum(row["close"] for row in rows[index + 1 - window:index + 1]) / window


def _check(value, threshold, passed, operator):
    return {"value": value, "threshold": threshold,
            "operator": operator, "passed": bool(passed)}


def _le(value, limit):
    return value <= limit or math.isclose(value, limit, rel_tol=1e-12, abs_tol=1e-12)


def _ge(value, limit):
    return value >= limit or math.isclose(value, limit, rel_tol=1e-12, abs_tol=1e-12)


def _cycle_metrics(rows, active, current_index, config, include_current=False):
    adjustment = list(active["adjustment"])
    if include_current:
        adjustment.append(current_index)
    d0 = rows[active["d0"]]
    up_rows = [rows[index] for index in active["up_indices"]]
    adjustment_rows = [rows[index] for index in adjustment]
    v0 = sum(row["volume"] for row in up_rows) / len(up_rows)
    h_value = max((row["high"] for row in adjustment_rows), default=None)
    l_value = min((row["low"] for row in adjustment_rows), default=None)
    va = (sum(row["volume"] for row in adjustment_rows) / len(adjustment_rows)
          if adjustment_rows else None)
    vlast = adjustment_rows[-1]["volume"] if adjustment_rows else None
    drawdown = (1 - l_value / d0["high"]
                if l_value is not None and d0["high"] else None)
    va_v0 = va / v0 if va is not None and v0 else None
    last_va = vlast / va if vlast is not None and va else None
    ma_holds = all(
        _ge(rows[index]["close"], config["ma10_floor_factor"] *
            _ma(rows, index, config["ma_window"]))
        for index in adjustment
    ) if adjustment else False
    dry = bool(
        adjustment
        and _le(drawdown, config["max_drawdown"])
        and ma_holds
        and _le(va_v0, config["max_adjustment_volume_ratio"])
        and (len(adjustment) == 1
             or _le(last_va, config["max_last_volume_ratio"]))
    )
    return {
        "H": h_value, "L": l_value, "d0_high": d0["high"],
        "V0": v0, "Va": va, "Vlast": vlast,
        "drawdown_ratio": drawdown, "va_v0_ratio": va_v0,
        "last_va_ratio": last_va, "ma10_holds": ma_holds,
        "dry": dry, "adjustment_days": len(adjustment),
    }


def _base_from_active(rows, active, index, config):
    metrics = _cycle_metrics(rows, active, index, config)
    return {
        "cycle_id": _canonical_digest({
            "schema": SCHEMA_VERSION,
            "d0": rows[active["d0"]]["date"],
            "config": config,
        }),
        "d0_date": rows[active["d0"]]["date"],
        "up_start_date": rows[active["up_indices"][0]]["date"],
        "up_days": len(active["up_indices"]),
        **metrics,
    }


def _supply_checks(metrics, config):
    last_exempt = metrics["adjustment_days"] == 1
    checks = {
        "drawdown": _check(metrics["drawdown_ratio"], config["max_drawdown"],
                           _le(metrics["drawdown_ratio"], config["max_drawdown"]), "<="),
        "ma10_support": _check(None, config["ma10_floor_factor"], metrics["ma10_holds"], ">="),
        "adjustment_volume": _check(metrics["va_v0_ratio"], config["max_adjustment_volume_ratio"],
                                    _le(metrics["va_v0_ratio"], config["max_adjustment_volume_ratio"]), "<="),
        "last_volume": _check(metrics["last_va_ratio"], config["max_last_volume_ratio"],
                              last_exempt or _le(metrics["last_va_ratio"], config["max_last_volume_ratio"]), "<="),
    }
    checks["last_volume"]["status"] = "not_applicable" if last_exempt else "evaluated"
    return checks


def _breakout(rows, active, index, config):
    current = rows[index]
    metrics = _cycle_metrics(rows, active, index, config)
    h_value = metrics["H"]
    if h_value is None or current["close"] <= h_value:
        return False, False, metrics, {}
    amplitude = current["high"] - current["low"]
    volume_ratio = current["volume"] / metrics["Va"] if metrics["Va"] else None
    close_strength = ((current["close"] - current["low"]) / amplitude
                      if amplitude > 0 else None)
    breakout_ratio = current["close"] / h_value if h_value else None
    checks = {
        **_supply_checks(metrics, config),
        "supply_dry": _check(metrics["va_v0_ratio"],
                             config["max_adjustment_volume_ratio"],
                             metrics["dry"], "structure"),
        "price_breakout": _check(current["close"], h_value, True, ">"),
        "breakout_volume": _check(
            volume_ratio, config["min_breakout_volume_ratio"],
            volume_ratio is not None and
            _ge(volume_ratio, config["min_breakout_volume_ratio"]), ">="),
        "close_strength": _check(
            close_strength, config["min_close_strength"],
            close_strength is not None and
            _ge(close_strength, config["min_close_strength"]), ">="),
        "breakout_ratio": _check(
            breakout_ratio, config["max_breakout_ratio"],
            breakout_ratio is not None and
            _le(breakout_ratio, config["max_breakout_ratio"]), "<="),
        "non_flat_bar": _check(amplitude, 0, amplitude > 0, ">"),
    }
    passed = all(check["passed"] for check in checks.values())
    return True, passed, {
        **metrics,
        "Ct": current["close"], "Vt": current["volume"],
        "breakout_volume_ratio": volume_ratio,
        "close_strength": close_strength,
        "breakout_ratio": breakout_ratio,
    }, checks


def _apply_metrics(result, values):
    for key in ("H", "L", "d0_high", "V0", "Va", "Vlast", "Ct", "Vt",
                "drawdown_ratio", "va_v0_ratio", "last_va_ratio",
                "breakout_volume_ratio", "close_strength", "breakout_ratio",
                "adjustment_days"):
        if key in values:
            result[key] = values[key]


def _replay(rows, config):
    result = _empty_result(rows[-1]["date"] if rows else "", config)
    if len(rows) < config["min_history_bars"]:
        result.update(evidence_status="data_insufficient",
                      reason_codes=["history_insufficient"])
        return result, None
    boundary_seen = False
    up_indices = []
    active = None
    events = []
    for index in range(config["ma_window"], len(rows)):
        current = rows[index]
        if active is not None:
            is_breakout, breakout_passed, metrics, checks = _breakout(
                rows, active, index, config)
            if is_breakout:
                base = _base_from_active(rows, active, index, config)
                result.update(base)
                _apply_metrics(result, metrics)
                result["checks"] = checks
                result["signal_date"] = current["date"]
                if breakout_passed:
                    result.update(state="BREAKOUT_CONFIRMED",
                                  confirmation="confirmed", reason_codes=[])
                else:
                    result.update(state="INVALID", confirmation="none",
                                  reason_codes=["breakout_unqualified"])
                events.append({
                    "date": current["date"], "state": result["state"],
                    "cycle_id": result["cycle_id"],
                    "reason_codes": copy.deepcopy(result["reason_codes"]),
                })
                active = None
                up_indices = []
                boundary_seen = True
                continue

            active["adjustment"].append(index)
            metrics = _cycle_metrics(rows, active, index, config)
            base = _base_from_active(rows, active, index, config)
            result.update(base)
            _apply_metrics(result, metrics)
            result["checks"] = _supply_checks(metrics, config)
            structural_failure = (
                not _le(metrics["drawdown_ratio"], config["max_drawdown"])
                or not metrics["ma10_holds"]
            )
            days_after_d0 = index - active["d0"]
            if structural_failure or days_after_d0 >= config["max_days_after_d0"]:
                reasons = []
                if structural_failure:
                    reasons.append("structure_broken")
                if days_after_d0 >= config["max_days_after_d0"]:
                    reasons.append("pullback_timeout")
                result.update(state="INVALID", confirmation="none",
                              reason_codes=reasons)
                events.append({
                    "date": current["date"], "state": "INVALID",
                    "cycle_id": result["cycle_id"],
                    "reason_codes": copy.deepcopy(reasons),
                })
                active = None
                up_indices = []
                boundary_seen = True
            else:
                result.update(
                    state="SUPPLY_DRY" if metrics["dry"] else "PULLBACK",
                    confirmation="none", reason_codes=[], signal_date="",
                )
            continue

        if current["close"] > rows[index - 1]["close"]:
            if boundary_seen:
                up_indices.append(index)
            result.update(state="WAIT", confirmation="none", reason_codes=[])
            continue

        boundary_seen = True
        if len(up_indices) >= config["min_up_days"]:
            active = {
                "up_indices": list(up_indices),
                "d0": up_indices[-1],
                "adjustment": [index],
            }
            metrics = _cycle_metrics(rows, active, index, config)
            base = _base_from_active(rows, active, index, config)
            result.update(base)
            _apply_metrics(result, metrics)
            result["checks"] = _supply_checks(metrics, config)
            structural_failure = (
                not _le(metrics["drawdown_ratio"], config["max_drawdown"])
                or not metrics["ma10_holds"]
            )
            if structural_failure:
                result.update(state="INVALID", confirmation="none",
                              reason_codes=["structure_broken"])
                events.append({
                    "date": current["date"], "state": "INVALID",
                    "cycle_id": result["cycle_id"],
                    "reason_codes": ["structure_broken"],
                })
                active = None
            else:
                result.update(
                    state="SUPPLY_DRY" if metrics["dry"] else "PULLBACK",
                    confirmation="none", reason_codes=[], signal_date="",
                )
        else:
            result.update(state="WAIT", confirmation="none", reason_codes=[])
        up_indices = []
    result["events"] = events
    if not boundary_seen:
        result["evidence_status"] = "data_insufficient"
        result["reason_codes"] = ["history_left_truncated"]
    return result, active


def _mark_provisional(result, target_date):
    """Mark a full intraday replay without persisting its current-day event."""
    result["provisional"] = True
    result["current_bar_evaluated"] = True
    result["evaluation_date"] = target_date
    result["confirmation"] = "provisional"
    result["events"] = [
        event for event in result.get("events", [])
        if event.get("date") != target_date
    ]
    if (result.get("state") == "BREAKOUT_CONFIRMED"
            and result.get("signal_date") == target_date):
        result["state"] = "PROVISIONAL_BREAKOUT"
        result["provisional_match"] = True
    return result


def analyze_daily_weak_to_strong(
        kline_payload, *, as_of_date, close_evidence=None,
        calendar_evidence=None, config=None):
    """Replay and classify one daily weak-to-strong setup.

    Args are frozen data/evidence objects.  The function never fetches data and
    ignores every K-line after ``as_of_date``.
    """
    resolved_config, config_reasons = _config(config)
    target_date = _normalise_date(as_of_date)
    result = _empty_result(target_date, resolved_config, config_reasons)
    if not target_date:
        result["reason_codes"] = ["as_of_date_invalid"]
        return result
    if config_reasons:
        return result
    rows, row_reasons, duplicate_dates = _normalise_rows(
        kline_payload, target_date)
    meta = (kline_payload.get("meta", {})
            if isinstance(kline_payload, dict) else {})
    meta = meta if isinstance(meta, dict) else {}
    adj = str(meta.get("adj") or meta.get("adjustment") or "").lower()
    basis_ready = adj in {"qfq", "hfq", "none", "unadjusted"}
    result["kline_evidence"] = {
        "provider": str(meta.get("data_source") or meta.get("provider") or ""),
        "basis": adj,
        "volume_unit": str(meta.get("volume_unit") or "unknown"),
        "data_date": rows[-1]["date"] if rows else "",
        "fetched_at": str(meta.get("fetch_time") or meta.get("fetched_at") or ""),
        "close_confirmation": copy.deepcopy(close_evidence or {}),
        "duplicate_dates": duplicate_dates,
        "input_sha256": _canonical_digest(rows),
    }
    preliminary_reasons = list(row_reasons)
    if not basis_ready:
        preliminary_reasons.append("basis_unknown")
    if meta.get("volume_basis_conflict") is True or meta.get("mixed_sources") is True:
        preliminary_reasons.append("volume_basis_conflict")
    if len(rows) < resolved_config["min_history_bars"]:
        preliminary_reasons.append("history_insufficient")
    if not rows or rows[-1]["date"] != target_date:
        preliminary_reasons.append("as_of_not_covered")
    if preliminary_reasons:
        result.update(evidence_status="data_insufficient",
                      reason_codes=list(dict.fromkeys(preliminary_reasons)))
        result["kline_evidence"]["calendar_coverage"] = {}
        return result

    intraday = bool(isinstance(close_evidence, dict)
                    and close_evidence.get("provisional") is True)
    close_confirmed = bool(
        isinstance(close_evidence, dict) and close_evidence.get("confirmed") is True
        and close_evidence.get("source") != "not_required"
        and not intraday)
    replay_rows = rows if close_confirmed or intraday else rows[:-1]
    replay_result, _ = _replay(replay_rows, resolved_config)
    replay_result["current_bar_evaluated"] = bool(close_confirmed or intraday)
    replay_result["evaluation_date"] = (
        replay_rows[-1]["date"] if replay_rows else "")
    if intraday and not close_confirmed:
        replay_result = _mark_provisional(replay_result, target_date)
    replay_result["as_of_date"] = target_date
    replay_result["parameter_hash"] = result["parameter_hash"]
    replay_result["kline_evidence"] = result["kline_evidence"]

    up_start = replay_result.get("up_start_date")
    up_index = next((index for index, row in enumerate(rows)
                     if row["date"] == up_start), None)
    required_index = (max(0, up_index - resolved_config["ma_window"])
                      if up_index is not None else
                      max(0, len(rows) - resolved_config["ma_window"]))
    required_start = rows[required_index]["date"]
    calendar_status, calendar_reasons, calendar_detail = _calendar_status(
        calendar_evidence, rows, required_start, target_date)
    replay_result["kline_evidence"]["calendar_coverage"] = calendar_detail
    evidence_reasons = list(calendar_reasons)
    if replay_result.get("evidence_status") == "data_insufficient":
        evidence_reasons.extend(replay_result.get("reason_codes") or [])
    if not basis_ready:
        evidence_reasons.append("basis_unknown")
    non_fatal_reasons = [] if close_confirmed else ["close_unconfirmed"]
    if evidence_reasons:
        replay_result["evidence_status"] = "data_insufficient"
        replay_result["reason_codes"] = list(dict.fromkeys(
            list(replay_result.get("reason_codes") or []) + evidence_reasons
            + non_fatal_reasons))
        if replay_result["state"] == "BREAKOUT_CONFIRMED":
            replay_result["state"] = "UNVERIFIED_BREAKOUT"
            replay_result["confirmation"] = "evidence_incomplete"
            replay_result["provisional_match"] = False
    else:
        replay_result["evidence_status"] = calendar_status
        replay_result["reason_codes"] = list(dict.fromkeys(
            list(replay_result.get("reason_codes") or []) + non_fatal_reasons))
    return replay_result


__all__ = [
    "DEFAULT_CONFIG", "DETECTOR_VERSION", "SCHEMA_VERSION",
    "analyze_daily_weak_to_strong",
]
