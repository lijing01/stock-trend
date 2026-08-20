"""Pure, risk-bounded trade plans for daily stock recommendations."""
import copy
import math
from datetime import date, timedelta

import pandas as pd

from analysis.technical import (build_summary, calc_adx, calc_atr,
    calc_bollinger, calc_entry_signals, calc_ma_signals, calc_risk_reward,
    calc_rsi, calc_support_resistance)

SCHEMA_VERSION = "candidate-trade-plan/v1"
MIN_PRIMARY_RR = 1.5
RISK_BUDGET_PCT = 0.5
VALID_TRADING_SESSIONS = 3
HORIZON = {"min_trading_days": 20, "max_trading_days": 120}
EVENT_STATUSES = {"not_implemented", "clear", "watch", "risk", "pending"}

def _finite_positive(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None

def _round_optional_price(value):
    price = _finite_positive(value)
    return round(price, 4) if price is not None else None

def _normalized_frame(kline):
    rows = kline.get("data", []) if isinstance(kline, dict) else (kline or [])
    df = pd.DataFrame(copy.deepcopy(rows))
    if "volume" in df and "vol" not in df:
        df["vol"] = df["volume"]
    for col in ("open", "high", "low", "close", "vol"):
        if col not in df:
            df[col] = df["close"] if "close" in df else 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)

def calculate_position_pct(entry_price, stop_price, max_portfolio_pct,
                           max_recommendations, risk_budget_pct=RISK_BUDGET_PCT):
    entry, stop = _finite_positive(entry_price), _finite_positive(stop_price)
    if not entry or not stop or stop >= entry:
        return 0.0
    stop_pct = (entry - stop) / entry * 100
    risk_based = risk_budget_pct / stop_pct * 100
    equal_cap = float(max_portfolio_pct) / max(1, int(max_recommendations))
    return round(max(0.0, min(risk_based, equal_cap, 20.0)), 1)

def _next_validity_date(basis_date, market_sessions=None):
    try:
        base = date.fromisoformat(str(basis_date)[:10])
    except (TypeError, ValueError):
        return basis_date
    normalized = []
    for raw in market_sessions or []:
        text = str(raw)
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        try:
            candidate = date.fromisoformat(text[:10])
        except ValueError:
            continue
        if candidate > base:
            normalized.append(candidate)
    if len(normalized) >= VALID_TRADING_SESSIONS:
        return sorted(set(normalized))[VALID_TRADING_SESSIONS - 1].isoformat()
    current = base
    sessions = 0
    while sessions < VALID_TRADING_SESSIONS:
        current += timedelta(days=1)
        if current.weekday() < 5:
            sessions += 1
    return current.isoformat()

def build_candidate_trade_plan(code, kline, wyckoff, policy, basis_date,
                               counterargument, market_sessions=None):
    df = _normalized_frame(kline)
    if len(df) < 2:
        return {"schema_version": SCHEMA_VERSION, "code": code, "basis_date": basis_date,
                "action": "avoid", "event_check": {"status": "not_implemented"}}
    close = _finite_positive(df["close"].iloc[-1])
    if close is None:
        return {"schema_version": SCHEMA_VERSION, "code": code, "basis_date": basis_date,
                "action": "avoid", "event_check": {"status": "not_implemented"}}
    ma = calc_ma_signals(df, [5, 10, 20, 60]); rsi = calc_rsi(df)
    bb = calc_bollinger(df); adx = calc_adx(df)
    indicators = {"ma": ma, "rsi": rsi, "bollinger": bb, "adx": adx}
    indicators["summary"] = build_summary(indicators, patterns=[], data_points=len(df))
    atr = calc_atr(df)
    atr_value = _finite_positive(atr.get("atr")) or close * 0.02
    levels = calc_support_resistance(df, ma, bb, atr_pct=atr.get("atr_pct"),
        adx_value=adx.get("adx"), atr_absolute=atr.get("atr"))
    support = [x.get("price") for x in levels.get("support", []) if _finite_positive(x.get("price")) and x["price"] < close]
    low = max(close - .75 * atr_value, max(support[-1:] or [close - .75 * atr_value]))
    high = close + .25 * atr_value
    risk = calc_risk_reward(
        df, atr, levels, direction="bullish", is_etf=False, entry_price=high)
    timing = calc_entry_signals(df, indicators, rr_ratio=risk.get("risk_reward_ratio"), is_etf=False)
    stop = _finite_positive(risk.get("stop_loss")) or close - 1.5 * atr_value
    if low <= stop: low = stop + .25 * atr_value
    target_source = str(risk.get("target_source") or "unavailable")
    targets = [
        risk.get("target_conservative"),
        risk.get("target_moderate"),
        risk.get("target_aggressive"),
    ]
    targets = [_finite_positive(value) for value in targets]
    has_valid_ladder = (
        target_source in {"resistance", "atr_projection"}
        and all(value is not None for value in targets)
        and high < targets[0] < targets[1] < targets[2]
    )
    if not has_valid_ladder:
        target_source = "unavailable"
        targets = [None, None, None]
    verdict = timing.get("verdict")
    action = "buy" if verdict == "ready" else ("wait" if verdict in ("wait", "watch") else "avoid")
    executable_target = target_source == "resistance" and has_valid_ladder
    if action == "buy" and not executable_target:
        action = "wait"
    cap = float(policy.get("max_portfolio_pct", 0) or 0)
    pos = calculate_position_pct(high, stop, cap, policy.get("max_recommendations", 1))
    supplied_rr = risk.get("risk_reward_ratio")
    recomputed_rr = (
        round((targets[1] - high) / (high - stop), 2)
        if has_valid_ladder and high > stop else None
    )
    return {"schema_version": SCHEMA_VERSION, "code": code, "basis_date": basis_date,
        "basis_price": close, "action": action,
        "entry": {"low": round(low, 4), "high": round(high, 4), "reference_price": close},
        "confirmation": "; ".join(timing.get("signals", [])) or "确认站上入场区并放量",
        "invalidation": "收盘跌破止损或结构支撑",
        "stop_loss": {"price": round(stop, 4)},
        "targets": {"conservative": _round_optional_price(targets[0]),
                    "primary": _round_optional_price(targets[1]),
                    "aggressive": _round_optional_price(targets[2])},
        "risk_reward": {"supplied": supplied_rr, "recomputed": recomputed_rr},
        "position": {"max_portfolio_pct": pos}, "horizon": dict(HORIZON),
        "validity": {"trading_sessions": VALID_TRADING_SESSIONS,
                     "valid_until": _next_validity_date(basis_date, market_sessions)},
        "target_source": target_source,
        "target_reason": (
            None if has_valid_ladder else
            (risk.get("warning") or "没有高于计划入场价的有效目标梯度")
        ),
        "counterargument": counterargument, "event_check": {"status": "not_implemented"},
        "indicators": {"atr": atr}, "wyckoff": copy.deepcopy(wyckoff or {})}

def validate_trade_plan(plan, policy, expected_date=None):
    reasons = []
    if not isinstance(plan, dict): return {"complete": False, "recomputed_rr": None, "reasons": ["trade_plan_missing"]}
    if plan.get("schema_version") != SCHEMA_VERSION: reasons.append("trade_plan_schema_invalid")
    e, t = plan.get("entry") or {}, plan.get("targets") or {}
    low, high = _finite_positive(e.get("low")), _finite_positive(e.get("high")); stop = _finite_positive((plan.get("stop_loss") or {}).get("price"))
    vals = [_finite_positive(t.get(k)) for k in ("conservative", "primary", "aggressive")]
    if expected_date and plan.get("basis_date") != expected_date: reasons.append("trade_plan_wrong_date")
    if not low or not high or low > high: reasons.append("trade_plan_missing_entry")
    if not plan.get("confirmation"): reasons.append("trade_plan_missing_confirmation")
    if not plan.get("invalidation"): reasons.append("trade_plan_missing_invalidation")
    if not stop or not low or stop >= low: reasons.append("trade_plan_invalid_stop")
    target_source = str(plan.get("target_source") or "unavailable")
    if target_source not in {"resistance", "atr_projection", "unavailable"}:
        reasons.append("trade_plan_target_source_invalid")
    if not all(vals): reasons.append("trade_plan_targets_unavailable")
    elif not high < vals[0] < vals[1] < vals[2]: reasons.append("trade_plan_targets_unordered")
    rr = round((vals[1]-high)/(high-stop), 2) if vals[1] and high and stop and high > stop else None
    if rr is not None and rr < MIN_PRIMARY_RR: reasons.append("trade_plan_rr_below_min")
    stored_rr = _finite_positive((plan.get("risk_reward") or {}).get("recomputed"))
    if rr is not None and (stored_rr is None or abs(stored_rr - rr) > 0.01):
        reasons.append("trade_plan_rr_mismatch")
    pos = _finite_positive((plan.get("position") or {}).get("max_portfolio_pct"))
    if not pos or pos > float(policy.get("max_portfolio_pct", 0) or 0): reasons.append("trade_plan_position_over_policy")
    counterargument = plan.get("counterargument")
    if not isinstance(counterargument, str) or not counterargument.strip(): reasons.append("trade_plan_missing_counterargument")
    event_status = (plan.get("event_check") or {}).get("status")
    if event_status not in EVENT_STATUSES: reasons.append("trade_plan_event_status_invalid")
    horizon = plan.get("horizon") or {}
    if horizon.get("min_trading_days") != HORIZON["min_trading_days"] or horizon.get("max_trading_days") != HORIZON["max_trading_days"]:
        reasons.append("trade_plan_horizon_invalid")
    validity = plan.get("validity") or {}
    if validity.get("trading_sessions") != VALID_TRADING_SESSIONS:
        reasons.append("trade_plan_validity_sessions_invalid")
    valid_until = validity.get("valid_until")
    basis_date = plan.get("basis_date") or expected_date
    try:
        if not valid_until or not basis_date or date.fromisoformat(str(valid_until)[:10]) <= date.fromisoformat(str(basis_date)[:10]):
            reasons.append("trade_plan_validity_date_invalid")
    except (TypeError, ValueError):
        reasons.append("trade_plan_validity_date_invalid")
    if plan.get("action") == "buy" and target_source != "resistance":
        reasons.append("trade_plan_target_source_not_executable")
    if plan.get("action") != "buy": reasons.append("trade_plan_not_ready")
    return {"complete": not reasons, "recomputed_rr": rr, "reasons": reasons}
