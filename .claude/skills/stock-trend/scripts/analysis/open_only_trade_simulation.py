"""Offline, auditable simulation for the ``open_only_v1`` trade contract.

The module intentionally accepts only caller-supplied frozen data.  It never
fetches prices, calendars, security rules, tax tables, or benchmark data.
Daily bars can establish a conservative hypothetical path; they are not proof
that a real order was submitted or filled.
"""

from __future__ import annotations

import copy
import math
from datetime import date
import hashlib
import json


SCHEMA_VERSION = "open-only-trade-simulation/v1"
CONTRACT_VERSION = "open-only-v1"
ACCOUNT_CNY = 1_000_000.0
SINGLE_NAME_LIMIT_PCT = 20.0
DEFAULT_RISK_BUDGET_PCT = 0.5
SUPPORTED_WINDOWS = (20, 60)
TAX_EFFECTIVE_FROM = "2023-08-28"
TAX_VERIFIED_THROUGH = "2026-10-01"


def build_contract(cost_scenario="reference"):
    """Return the immutable simulation and cost assumptions."""
    scenarios = {
        "reference": {"commission_bps": 3.0, "slippage_bps": 5.0},
        "stress": {"commission_bps": 3.0, "slippage_bps": 15.0},
    }
    if cost_scenario not in scenarios:
        raise ValueError("unsupported_cost_scenario")
    costs = scenarios[cost_scenario]
    return {
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "cost_scenario": cost_scenario,
        "entry_rule": "next_calendar_session_open_once",
        "sale_rule": "t_plus_one",
        "same_bar_rule": "stop_before_target",
        "expiry_rule": "window_session_close_then_next_tradable_open_if_blocked",
        "corporate_action_rule": "require_sourced_no_actions_coverage_otherwise_data_insufficient",
        "account": {
            "currency": "CNY",
            "research_account_cny": ACCOUNT_CNY,
            "risk_budget_pct": DEFAULT_RISK_BUDGET_PCT,
            "single_name_limit_pct": SINGLE_NAME_LIMIT_PCT,
            "lot_size": 100,
            "portfolio_mode": "independent_single_opportunity",
        },
        "costs": {
            "buy_commission_bps": costs["commission_bps"],
            "sell_commission_bps": costs["commission_bps"],
            "minimum_commission_cny_per_side": 5.0,
            "sell_stamp_tax_bps": 5.0,
            "buy_slippage_bps": costs["slippage_bps"],
            "sell_slippage_bps": costs["slippage_bps"],
            "commission_includes_exchange_and_transfer_fees": True,
            "slippage_embedded_in_execution_price": True,
        },
        "rule_sources": {
            "sse_rules": {
                "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml",
                "effective_from": "2026-07-06",
                "retrieved_on": "2026-10-01",
            },
            "szse_lot_size": {
                "url": "https://investor.szse.cn/knowledge/t20230308_599142.html",
                "retrieved_on": "2026-10-01",
            },
            "sell_stamp_tax": {
                "url": "https://shanghai.chinatax.gov.cn/tax/zcfw/zcfgk/yhs/202308/t468451.html",
                "effective_from": TAX_EFFECTIVE_FROM,
                "verified_through": TAX_VERIFIED_THROUGH,
                "retrieved_on": "2026-10-01",
            },
        },
    }


def _day(value):
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("invalid_date") from exc


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _field_number(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = _number(mapping.get(name))
        if value is not None:
            return value
    return None


def _error(plan, cutoff, window, contract, reason, **details):
    result = _base_result(plan, cutoff, window, contract)
    result["opportunity_status"] = "data_error"
    result["execution"] = {"status": "data_error", "reason": reason}
    result["diagnostics"] = {"reasons": [reason], **details}
    return result


def _filled_data_error(result, reason, **details):
    """Retain an already incurred fill while failing the later valuation."""
    result["opportunity_status"] = "data_error"
    result["execution"]["valuation_status"] = "data_error"
    result["execution"]["valuation_reason"] = reason
    result["returns"] = {"net_return": None, "status": "data_error", "reason": reason}
    result["diagnostics"] = {
        "reasons": [reason],
        "offline_only": True,
        "opportunity_retained_in_denominator": True,
        **details,
    }
    return result


def _base_result(plan, cutoff, window, contract):
    contract_id = hashlib.sha256(json.dumps({"contract": contract, "window": window},
                                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "simulation_contract_id": contract_id,
        "immutable_identity": plan.get("immutable_identity"),
        "code": str(plan.get("code") or ""),
        "basis_date": plan.get("basis_date") or plan.get("recommendation_date"),
        "evaluation_cutoff": cutoff,
        "window": window,
        "opportunity_status": "pending",
        "execution": {"status": "pending"},
        "cost_contract": copy.deepcopy(contract),
        "cash_flows": {},
        "returns": {},
        "risk": {},
        "benchmark": {},
        "diagnostics": {"reasons": []},
    }


def _normalise_calendar(market_data, cutoff):
    calendar = market_data.get("calendar")
    if not isinstance(calendar, dict):
        raise ValueError("calendar_missing")
    if not calendar.get("calendar_id") or not calendar.get("source"):
        raise ValueError("calendar_identity_missing")
    complete_through = _day(calendar.get("complete_through"))
    if complete_through < cutoff:
        raise ValueError("calendar_coverage_incomplete")
    raw_sessions = calendar.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError("calendar_sessions_missing")
    sessions = [_day(item) for item in raw_sessions]
    if sessions != sorted(set(sessions)):
        raise ValueError("calendar_sessions_invalid")
    return sessions, calendar


def _normalise_rows(market_data):
    rows = market_data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("raw_rows_missing")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = _day(row.get("date") or row.get("trade_date"))
        except ValueError:
            continue
        if day in result:
            raise ValueError("duplicate_raw_row")
        result[day] = row
    return result


def _validate_security(plan, market_data, basis_date):
    meta = market_data.get("metadata")
    if not isinstance(meta, dict):
        raise ValueError("security_metadata_missing")
    required = ("source", "as_of", "exchange", "board", "security_type",
                "is_st", "ipo_special_rules", "lot_size", "price_limit_pct",
                "price_scale")
    if any(name not in meta for name in required) or not meta.get("source"):
        raise ValueError("security_metadata_incomplete")
    if _day(meta["as_of"]) > basis_date:
        raise ValueError("security_metadata_lookahead")
    code = str(plan.get("code") or "")
    if str(meta.get("code") or "") != code:
        raise ValueError("security_code_mismatch")
    if str(meta.get("exchange")).upper() not in {"SSE", "SH", "SZSE", "SZ"}:
        raise ValueError("unsupported_exchange")
    if str(meta.get("board")).lower() not in {"main", "main_board", "主板"}:
        raise ValueError("unsupported_board")
    if str(meta.get("security_type")).lower() not in {
        "ordinary_a_share", "a_share", "common_stock", "普通a股"
    }:
        raise ValueError("unsupported_security_type")
    if meta.get("is_st") is not False:
        raise ValueError("st_not_supported")
    if meta.get("ipo_special_rules") is not False:
        raise ValueError("ipo_special_rules_not_supported")
    if _number(meta.get("lot_size")) != 100:
        raise ValueError("lot_size_not_supported")
    limit_pct = _number(meta.get("price_limit_pct"))
    if limit_pct not in {10, .10}:
        raise ValueError("price_limit_rule_missing")
    if meta.get("price_scale") != "raw":
        raise ValueError("market_prices_not_raw")
    if not (len(code) == 6 and code.isdigit()
            and code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
        raise ValueError("ordinary_a_share_code_invalid")
    return meta


def _market_allocation_pct(plan):
    eligibility = plan.get("market_eligibility") or {}
    if eligibility.get("eligible") is not True:
        return None
    allocation = _field_number(eligibility, "allocation_pct", "market_allocation_pct")
    if allocation is None:
        allocation = _field_number(plan, "market_allocation_pct")
    position = plan.get("position") or {}
    position_cap = _field_number(position, "max_portfolio_pct", "market_allocation_pct")
    if allocation is None or allocation <= 0:
        return None
    if position_cap is not None:
        allocation = min(allocation, position_cap)
    return allocation


def _plan_levels(plan):
    entry = plan.get("entry") or {}
    stop_loss = plan.get("stop_loss") or {}
    targets = plan.get("targets") or {}
    low = _field_number(entry, "low", "lower")
    high = _field_number(entry, "high", "upper")
    stop = _field_number(stop_loss, "price", "level")
    target = _field_number(targets, "primary", "conservative")
    if target is None:
        target = _field_number(plan.get("target") or {}, "price")
    if None in (low, high, stop, target) or not (0 < stop < low <= high < target):
        raise ValueError("trade_plan_levels_invalid")
    return {"entry_low": low, "entry_high": high, "stop": stop, "target": target}


def _validate_plan_identity(plan):
    if plan.get("schema_version") != "lps-trade-plan/v1":
        raise ValueError("trade_plan_schema_invalid")
    if plan.get("plan_status") != "ready":
        raise ValueError("trade_plan_not_ready")
    identity = plan.get("immutable_identity")
    if not isinstance(identity, str) or "@" not in identity:
        raise ValueError("trade_plan_identity_missing")
    record_id, snapshot_sha = identity.split("@", 1)
    if not record_id or not snapshot_sha:
        raise ValueError("trade_plan_identity_missing")
    validity = plan.get("validity")
    if not isinstance(validity, dict) or validity.get("trading_sessions") != 1:
        raise ValueError("trade_plan_validity_invalid")
    if plan.get("entry_method") != "next_session_open_only_v1":
        raise ValueError("trade_plan_entry_method_invalid")


def _factor_map(plan, market_data):
    scale = plan.get("price_scale") or plan.get("price_basis") or "raw"
    if scale == "raw":
        return scale, None
    if scale not in {"adjusted", "qfq"}:
        raise ValueError("plan_price_scale_unknown")
    adjustment = market_data.get("adjustment")
    if not isinstance(adjustment, dict):
        raise ValueError("adjustment_mapping_missing")
    if (adjustment.get("method") != "multiplicative"
            or adjustment.get("adjusted_equals_raw_times_factor") is not True
            or not adjustment.get("source")
            or not plan.get("price_scale_id")
            or adjustment.get("scale_id") != plan.get("price_scale_id")
            or adjustment.get("basis_date") != plan.get("basis_date")):
        raise ValueError("adjustment_mapping_invalid")
    factors = adjustment.get("factors")
    if not isinstance(factors, dict):
        raise ValueError("adjustment_factors_missing")
    return scale, factors


def _raw_level(value, day, scale, factors):
    if scale == "raw":
        return value
    factor = _number(factors.get(day)) if factors else None
    if factor is None or factor <= 0:
        raise ValueError("adjustment_factor_missing")
    return value / factor


def _bar(row):
    values = {name: _field_number(row, name) for name in ("open", "high", "low", "close")}
    volume = _field_number(row, "volume", "vol")
    if any(value is None or value <= 0 for value in values.values()) or volume is None:
        raise ValueError("ohlcv_incomplete")
    if values["low"] > min(values["open"], values["close"], values["high"]):
        raise ValueError("ohlc_invalid")
    if values["high"] < max(values["open"], values["close"], values["low"]):
        raise ValueError("ohlc_invalid")
    values["volume"] = volume
    return values


def _one_price_limit(row_values, row, previous_close, meta, direction):
    if len({row_values[name] for name in ("open", "high", "low", "close")}) != 1:
        return False
    explicit = _field_number(
        row,
        "limit_up_price" if direction == "up" else "limit_down_price",
    )
    price = row_values["open"]
    if explicit is not None:
        return abs(price - explicit) <= max(0.01, explicit * 0.001)
    if previous_close is None or previous_close <= 0:
        raise ValueError("previous_close_missing_for_limit")
    limit_pct = _number(meta.get("price_limit_pct"))
    limit = limit_pct / 100 if limit_pct > 1 else limit_pct
    change = price / previous_close - 1
    tolerance = 0.003
    return change >= limit - tolerance if direction == "up" else change <= -limit + tolerance


def _commission(notional, bps, minimum):
    return max(minimum, notional * bps / 10_000)


def _size_position(plan, contract, levels, entry_day, scale, factors):
    costs = contract["costs"]
    high_raw = _raw_level(levels["entry_high"], entry_day, scale, factors)
    stop_raw = _raw_level(levels["stop"], entry_day, scale, factors)
    priced_high = high_raw * (1 + costs["buy_slippage_bps"] / 10_000)
    risk_per_share = priced_high - stop_raw
    if risk_per_share <= 0:
        raise ValueError("risk_per_share_invalid")
    position = plan.get("position") or {}
    risk_pct = _field_number(position, "risk_budget_pct")
    if risk_pct is None:
        risk_pct = DEFAULT_RISK_BUDGET_PCT
    if not (0 < risk_pct <= 100):
        raise ValueError("risk_budget_invalid")
    allocation = _market_allocation_pct(plan)
    if allocation is None:
        raise ValueError("formal_market_allocation_missing")
    lot = int(contract["account"]["lot_size"])
    risk_qty = math.floor((ACCOUNT_CNY * risk_pct / 100) / risk_per_share / lot) * lot
    market_qty = math.floor((ACCOUNT_CNY * allocation / 100) / priced_high / lot) * lot
    name_qty = math.floor((ACCOUNT_CNY * SINGLE_NAME_LIMIT_PCT / 100) / priced_high / lot) * lot
    qty = min(risk_qty, market_qty, name_qty)
    minimum = costs["minimum_commission_cny_per_side"]
    while qty >= lot:
        notional = qty * priced_high
        if notional + _commission(notional, costs["buy_commission_bps"], minimum) <= ACCOUNT_CNY:
            break
        qty -= lot
    if qty < lot:
        raise ValueError("position_below_one_lot")
    return {
        "quantity": qty,
        "lot_size": lot,
        "risk_budget_pct": risk_pct,
        "market_allocation_pct": allocation,
        "single_name_limit_pct": SINGLE_NAME_LIMIT_PCT,
        "sizing_price_raw": priced_high,
        "risk_per_share_at_sizing": risk_per_share,
    }


def _benchmark_return(benchmark, start, end, calendar_id):
    if not isinstance(benchmark, dict):
        return None, "benchmark_missing"
    metadata = benchmark.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("source"):
        return None, "benchmark_metadata_missing"
    if metadata.get("calendar_id") != calendar_id:
        return None, "benchmark_calendar_mismatch"
    if metadata.get("code") != "000300.SH":
        return None, "benchmark_identity_unknown"
    rows = {}
    for row in benchmark.get("rows") or []:
        try:
            rows[_day(row.get("date") or row.get("trade_date"))] = row
        except (AttributeError, ValueError):
            continue
    first = _field_number(rows.get(start) or {}, "open")
    last = _field_number(rows.get(end) or {}, "close")
    if first is None or last is None or first <= 0:
        return None, "benchmark_endpoint_missing"
    return last / first - 1, None


def simulate_trade(plan, market_data, cutoff, window=20, cost_scenario="reference"):
    """Simulate one frozen opportunity under the offline ``open_only_v1`` rules."""
    if not isinstance(plan, dict) or not isinstance(market_data, dict):
        raise TypeError("plan_and_market_data_must_be_dicts")
    if window not in SUPPORTED_WINDOWS:
        raise ValueError("unsupported_window")
    contract = build_contract(cost_scenario)
    try:
        cutoff = _day(cutoff)
    except ValueError:
        return _error(plan, str(cutoff), window, contract, "invalid_cutoff")
    result = _base_result(plan, cutoff, window, contract)
    try:
        _validate_plan_identity(plan)
        basis_date = _day(plan.get("basis_date") or plan.get("recommendation_date"))
        if cutoff < basis_date:
            raise ValueError("cutoff_before_basis_date")
        if basis_date < TAX_EFFECTIVE_FROM or basis_date > TAX_VERIFIED_THROUGH:
            raise ValueError("cost_tax_table_unverified_for_basis_date")
        if basis_date < "2026-07-06":
            raise ValueError("security_rule_version_not_effective")
        sessions, calendar = _normalise_calendar(market_data, cutoff)
        if basis_date not in sessions:
            raise ValueError("basis_date_not_in_calendar")
        meta = _validate_security(plan, market_data, basis_date)
        if _day(meta.get("rules_valid_through")) < cutoff:
            raise ValueError("security_rules_coverage_insufficient")
        rows = _normalise_rows(market_data)
        levels = _plan_levels(plan)
        scale, factors = _factor_map(plan, market_data)
        entry_candidates = [day for day in sessions if day > basis_date]
        if not entry_candidates:
            raise ValueError("next_session_missing")
        entry_day = entry_candidates[0]
        if (plan.get("validity") or {}).get("entry_session") != entry_day:
            raise ValueError("trade_plan_entry_session_mismatch")
        if entry_day > cutoff:
            result["execution"] = {"status": "pending", "reason": "cutoff_before_entry", "date": entry_day}
            result["diagnostics"]["reasons"] = ["cutoff_before_entry"]
            return result
        actions = market_data.get("corporate_actions") or {}
        if (not actions.get("source") or not actions.get("no_actions") is True
                or _day(actions.get("complete_from")) > basis_date
                or _day(actions.get("complete_through")) < cutoff):
            raise ValueError("corporate_action_cashflow_mapping_unverified")
        row = rows.get(entry_day)
        if row is None:
            raise ValueError("entry_row_missing")
        entry_bar = _bar(row)
        prior_day = sessions[sessions.index(entry_day) - 1]
        prior_close = _field_number(rows.get(prior_day) or {}, "close")
        if entry_bar["volume"] <= 0:
            result["opportunity_status"] = "not_filled"
            result["execution"] = {"status": "not_filled", "reason": "entry_suspended", "date": entry_day}
            return result
        if _one_price_limit(entry_bar, row, prior_close, meta, "up"):
            result["opportunity_status"] = "not_filled"
            result["execution"] = {"status": "not_filled", "reason": "entry_one_price_limit_up", "date": entry_day}
            return result
        raw_levels = {key: _raw_level(value, entry_day, scale, factors)
                      for key, value in levels.items()}
        open_price = entry_bar["open"]
        if open_price <= raw_levels["stop"]:
            result["opportunity_status"] = "not_filled"
            result["execution"] = {"status": "cancelled", "reason": "entry_open_at_or_below_invalidation", "date": entry_day}
            return result
        if open_price < raw_levels["entry_low"] or open_price > raw_levels["entry_high"]:
            result["opportunity_status"] = "not_filled"
            result["execution"] = {"status": "not_filled", "reason": "entry_open_outside_zone", "date": entry_day}
            return result
        buy_slip = contract["costs"]["buy_slippage_bps"] / 10_000
        buy_price = open_price * (1 + buy_slip)
        if buy_price > raw_levels["entry_high"]:
            result["opportunity_status"] = "not_filled"
            result["execution"] = {"status": "not_filled", "reason": "buy_slippage_above_entry_high", "date": entry_day}
            return result
        sizing = _size_position(plan, contract, levels, entry_day, scale, factors)
    except ValueError as exc:
        return _error(plan, cutoff, window, contract, str(exc))

    costs = contract["costs"]
    qty = sizing["quantity"]
    buy_notional = qty * buy_price
    buy_commission = _commission(
        buy_notional, costs["buy_commission_bps"], costs["minimum_commission_cny_per_side"])
    invested = buy_notional + buy_commission
    result["opportunity_status"] = "filled"
    result["execution"] = {
        "status": "hypothetical_open_fill",
        "entry_date": entry_day,
        "entry_reference_price_raw": open_price,
        "entry_execution_price_raw": buy_price,
        "quantity": qty,
        "sizing": sizing,
        "fill_assumption": "next_session_open_with_registered_slippage",
    }
    result["cash_flows"] = {
        "buy_notional_cny": buy_notional,
        "buy_commission_cny": buy_commission,
        "buy_slippage_cny": (buy_price - open_price) * qty,
        "invested_cash_cny": invested,
    }

    entry_index = sessions.index(entry_day)
    expiry_index = entry_index + window - 1
    expiry_day = sessions[expiry_index] if expiry_index < len(sessions) else None
    through = [day for day in sessions[entry_index:] if day <= cutoff]
    exit_info = None
    delayed_expiry = False
    delayed_stop = False
    delayed_exit_days = []
    ambiguous_exit = False
    conservative_mae = math.inf
    conservative_mfe = -math.inf
    boundary_mae = math.inf
    latest_mark = None
    previous_close = prior_close

    for index, day in enumerate(through):
        row = rows.get(day)
        if row is None:
            return _filled_data_error(result, "holding_row_missing", missing_date=day)
        try:
            values = _bar(row)
            stop = _raw_level(levels["stop"], day, scale, factors)
            target = _raw_level(levels["target"], day, scale, factors)
        except ValueError as exc:
            return _filled_data_error(result, str(exc), missing_date=day)
        conservative_mae = min(conservative_mae, values["low"] / buy_price - 1)
        conservative_mfe = max(conservative_mfe, values["high"] / buy_price - 1)
        latest_mark = (day, values["close"])
        tradable = values["volume"] > 0
        try:
            limit_down = tradable and _one_price_limit(values, row, previous_close, meta, "down")
        except ValueError as exc:
            return _filled_data_error(result, str(exc), missing_date=day)

        # T+1: the entry bar contributes conservative excursion only.
        if index == 0:
            boundary_mae = min(boundary_mae, min(values["open"], values["close"]) / buy_price - 1)
            previous_close = values["close"]
            continue

        if delayed_expiry or delayed_stop:
            if not tradable or limit_down:
                delayed_exit_days.append(day)
                previous_close = values["close"]
                continue
            exit_info = {"date": day, "reference_price": values["open"],
                         "reason": "stop_delayed_open" if delayed_stop else "expiry_delayed_open"}
        elif not tradable or limit_down:
            if values["low"] <= stop:
                delayed_stop = True
                delayed_exit_days.append(day)
            if expiry_day is not None and day >= expiry_day:
                delayed_expiry = True
                if day not in delayed_exit_days:
                    delayed_exit_days.append(day)
            previous_close = values["close"]
            continue
        else:
            stop_hit = values["low"] <= stop
            target_hit = values["high"] >= target
            if values["open"] <= stop:
                exit_info = {"date": day, "reference_price": values["open"], "reason": "stop_gap"}
            elif values["open"] >= target:
                exit_info = {"date": day, "reference_price": values["open"], "reason": "target_gap"}
            elif stop_hit:
                ambiguous_exit = target_hit
                exit_info = {"date": day, "reference_price": stop, "reason": "stop_touch"}
            elif target_hit:
                exit_info = {"date": day, "reference_price": target, "reason": "target_touch"}
            elif expiry_day is not None and day == expiry_day:
                exit_info = {"date": day, "reference_price": values["close"], "reason": "expiry_close"}
        boundary_price = exit_info["reference_price"] if exit_info else min(values["open"], values["close"])
        boundary_mae = min(boundary_mae, boundary_price / buy_price - 1)
        previous_close = values["close"]
        if exit_info:
            break

    result["risk"] = {
        "conservative_mae": None if conservative_mae == math.inf else conservative_mae,
        "conservative_mfe": None if conservative_mfe == -math.inf else conservative_mfe,
        "determinable_mae_boundary": None if boundary_mae == math.inf else boundary_mae,
        "exit_day_whole_bar_included": True,
        "ambiguous_exit": ambiguous_exit,
        "entry_day_invalidation_touched": entry_bar["low"] <= raw_levels["stop"],
    }

    if exit_info:
        if exit_info["date"] > TAX_VERIFIED_THROUGH:
            return _filled_data_error(result, "exit_tax_table_unverified", exit_date=exit_info["date"])
        sell_reference = exit_info["reference_price"]
        sell_price = sell_reference * (1 - costs["sell_slippage_bps"] / 10_000)
        sell_notional = qty * sell_price
        sell_commission = _commission(
            sell_notional, costs["sell_commission_bps"], costs["minimum_commission_cny_per_side"])
        sell_tax = sell_notional * costs["sell_stamp_tax_bps"] / 10_000
        recovered = sell_notional - sell_commission - sell_tax
        net_return = recovered / invested - 1
        gross_return = sell_reference / open_price - 1
        execution_return = sell_price / buy_price - 1
        result["opportunity_status"] = "completed"
        result["execution"].update({
            "status": "completed", "exit_date": exit_info["date"],
            "exit_reason": exit_info["reason"],
            "exit_reference_price_raw": sell_reference,
            "exit_execution_price_raw": sell_price,
            "delayed_exit": bool(delayed_exit_days),
            "delayed_exit_days": delayed_exit_days,
        })
        result["cash_flows"].update({
            "sell_notional_cny": sell_notional,
            "sell_commission_cny": sell_commission,
            "sell_stamp_tax_cny": sell_tax,
            "sell_slippage_cny": (sell_reference - sell_price) * qty,
            "recovered_cash_cny": recovered,
            "net_profit_cny": recovered - invested,
        })
        result["returns"] = {
            "gross_return": gross_return,
            "execution_price_return": execution_return,
            "net_return": net_return,
            "cost_status": "explicit_research_assumption",
        }
        actual_benchmark, actual_reason = _benchmark_return(
            market_data.get("benchmark"), entry_day, exit_info["date"], calendar["calendar_id"])
        result["benchmark"]["actual_exit"] = {
            "measurement": "entry_session_open_to_exit_session_close_daily_proxy",
            "entry_date": entry_day, "exit_date": exit_info["date"],
            "return": actual_benchmark, "reason": actual_reason,
            "net_excess_return": net_return - actual_benchmark if actual_benchmark is not None else None,
        }
    else:
        result["opportunity_status"] = "open_delayed_exit" if (delayed_expiry or delayed_stop) else "open_position"
        result["execution"].update({
            "status": result["opportunity_status"],
            "expiry_date": expiry_day,
            "delayed_exit": delayed_expiry or delayed_stop,
            "delayed_exit_days": delayed_exit_days,
        })
        if latest_mark:
            mark_day, mark_price = latest_mark
            hypothetical_sell = mark_price * (1 - costs["sell_slippage_bps"] / 10_000)
            hypothetical_notional = qty * hypothetical_sell
            hypothetical_commission = _commission(
                hypothetical_notional, costs["sell_commission_bps"],
                costs["minimum_commission_cny_per_side"])
            hypothetical_tax = hypothetical_notional * costs["sell_stamp_tax_bps"] / 10_000
            result["returns"] = {
                "net_return": None,
                "unrealized_return_before_hypothetical_exit_cost": mark_price / buy_price - 1,
                "mark_date": mark_day,
                "mark_price_raw": mark_price,
                "hypothetical_exit_cost_cny": hypothetical_commission + hypothetical_tax
                                             + (mark_price - hypothetical_sell) * qty,
                "cost_status": "buy_cost_incurred_exit_cost_hypothetical",
            }

    if expiry_day is not None and expiry_day <= cutoff:
        fixed_benchmark, fixed_reason = _benchmark_return(
            market_data.get("benchmark"), entry_day, expiry_day, calendar["calendar_id"])
        result["benchmark"]["fixed_window"] = {
            "measurement": "entry_session_open_to_window_session_close",
            "entry_date": entry_day, "exit_date": expiry_day,
            "return": fixed_benchmark, "reason": fixed_reason,
            "post_exit_cash_return_assumption": 0.0,
        }
    else:
        result["benchmark"]["fixed_window"] = {
            "entry_date": entry_day, "exit_date": expiry_day,
            "return": None,
            "reason": "window_not_mature" if expiry_day else "calendar_window_incomplete",
            "post_exit_cash_return_assumption": 0.0,
        }
    result["diagnostics"] = {
        "reasons": [],
        "calendar_id": calendar["calendar_id"],
        "price_scale": scale,
        "market_prices": "raw",
        "offline_only": True,
        "opportunity_retained_in_denominator": True,
    }
    return result


__all__ = ["build_contract", "simulate_trade"]
