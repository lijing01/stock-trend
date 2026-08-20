"""Pure, risk-bounded trade plans for daily stock recommendations."""
import copy
import math
from datetime import date

import pandas as pd

from analysis.technical import (build_summary, calc_adx, calc_atr,
    calc_bollinger, calc_entry_signals, calc_ma_signals, calc_risk_reward,
    calc_rsi, calc_support_resistance)

SCHEMA_VERSION = "candidate-trade-plan/v1"
MIN_PRIMARY_RR = 1.5
RISK_BUDGET_PCT = 0.5
VALID_TRADING_SESSIONS = 3
HORIZON = {"min_trading_days": 20, "max_trading_days": 120}

def _finite_positive(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None

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

def build_candidate_trade_plan(code, kline, wyckoff, policy, basis_date, counterargument):
    df = _normalized_frame(kline)
    close = _finite_positive(df["close"].iloc[-1])
    if close is None or len(df) < 2:
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
    risk = calc_risk_reward(df, atr, levels, direction="bullish", is_etf=False)
    timing = calc_entry_signals(df, indicators, rr_ratio=risk.get("risk_reward_ratio"), is_etf=False)
    support = [x.get("price") for x in levels.get("support", []) if _finite_positive(x.get("price")) and x["price"] < close]
    low = max(close - .75 * atr_value, max(support[-1:] or [close - .75 * atr_value]))
    high = close + .25 * atr_value
    stop = _finite_positive(risk.get("stop_loss")) or close - 1.5 * atr_value
    if low <= stop: low = stop + .25 * atr_value
    one_r = max(high - stop, .01)
    technical_targets = risk.get("targets") or []
    targets = [x.get("price") if isinstance(x, dict) else x for x in technical_targets]
    targets = [x for x in targets if _finite_positive(x)]
    while len(targets) < 3: targets.append(high + one_r * (len(targets) + 1))
    targets = sorted(targets[:3])
    action = "buy" if timing.get("verdict") == "ready" else ("wait" if timing.get("verdict") == "wait" else "avoid")
    cap = float(policy.get("max_portfolio_pct", 0) or 0)
    pos = calculate_position_pct(high, stop, cap, policy.get("max_recommendations", 1))
    supplied_rr = risk.get("risk_reward_ratio")
    return {"schema_version": SCHEMA_VERSION, "code": code, "basis_date": basis_date,
        "basis_price": close, "action": action,
        "entry": {"low": round(low, 4), "high": round(high, 4), "reference_price": close},
        "confirmation": "; ".join(timing.get("signals", [])) or "确认站上入场区并放量",
        "invalidation": "收盘跌破止损或结构支撑",
        "stop_loss": {"price": round(stop, 4)},
        "targets": {"conservative": round(targets[0], 4), "primary": round(targets[1], 4), "aggressive": round(targets[2], 4)},
        "risk_reward": {"supplied": supplied_rr, "recomputed": round((targets[1]-high)/(high-stop), 2)},
        "position": {"max_portfolio_pct": pos}, "horizon": dict(HORIZON),
        "validity": {"trading_sessions": VALID_TRADING_SESSIONS, "valid_until": basis_date},
        "counterargument": counterargument, "event_check": {"status": "not_implemented"},
        "indicators": {"atr": atr}, "wyckoff": copy.deepcopy(wyckoff or {})}

def validate_trade_plan(plan, policy, expected_date=None):
    reasons = []
    if not isinstance(plan, dict): return {"complete": False, "recomputed_rr": None, "reasons": ["trade_plan_missing"]}
    e, t = plan.get("entry") or {}, plan.get("targets") or {}
    low, high = _finite_positive(e.get("low")), _finite_positive(e.get("high")); stop = _finite_positive((plan.get("stop_loss") or {}).get("price"))
    vals = [_finite_positive(t.get(k)) for k in ("conservative", "primary", "aggressive")]
    if expected_date and plan.get("basis_date") != expected_date: reasons.append("trade_plan_wrong_date")
    if not low or not high or low > high: reasons.append("trade_plan_missing_entry")
    if not plan.get("confirmation"): reasons.append("trade_plan_missing_confirmation")
    if not plan.get("invalidation"): reasons.append("trade_plan_missing_invalidation")
    if not stop or not low or stop >= low: reasons.append("trade_plan_invalid_stop")
    if not all(vals): reasons.append("trade_plan_missing_targets")
    elif not high < vals[0] < vals[1] < vals[2]: reasons.append("trade_plan_targets_unordered")
    rr = round((vals[1]-high)/(high-stop), 2) if vals[1] and high and stop and high > stop else None
    if rr is None or rr < MIN_PRIMARY_RR: reasons.append("trade_plan_rr_below_min")
    pos = _finite_positive((plan.get("position") or {}).get("max_portfolio_pct"))
    if not pos or pos > float(policy.get("max_portfolio_pct", 0) or 0): reasons.append("trade_plan_position_over_policy")
    if not plan.get("counterargument"): reasons.append("trade_plan_missing_counterargument")
    if (plan.get("event_check") or {}).get("status") is None: reasons.append("trade_plan_event_status_missing")
    if plan.get("action") != "buy": reasons.append("trade_plan_not_ready")
    return {"complete": not reasons, "recomputed_rr": rr, "reasons": reasons}
