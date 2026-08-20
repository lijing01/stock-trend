import copy
import math
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from core.candidate_trade_plan import (build_candidate_trade_plan,
    calculate_position_pct, validate_trade_plan)
from analysis.technical import calc_risk_reward

def make_kline(start=100.0, rows=80):
    return {"meta": {"adj": "qfq"}, "data": [{
        "trade_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
        "open": start+i*.12-.15, "high": start+i*.12+.8,
        "low": start+i*.12-.8, "close": start+i*.12,
        "pre_close": start+i*.12-.12, "vol": 1_000_000+i*10_000,
    } for i in range(rows)]}

def policy():
    return {"mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "provisional": False}

def test_position_risk_budget():
    assert calculate_position_pct(100, 95, 60, 5, .5) == 10.0
    assert calculate_position_pct(100, 99, 60, 5, .5) == 12.0
    assert calculate_position_pct(100, 100, 60, 5, .5) == 0.0

def test_builder_schema_and_immutability():
    k = make_kline(); original = copy.deepcopy(k)
    plan = build_candidate_trade_plan("600000", k, {"sub_phase": "LPS"}, policy(), "2026-08-20", "跌破支撑失效")
    assert plan["schema_version"] == "candidate-trade-plan/v1"
    assert plan["entry"]["low"] <= plan["entry"]["high"]
    assert plan["stop_loss"]["price"] < plan["entry"]["low"]
    if plan["target_source"] == "unavailable":
        assert all(value is None for value in plan["targets"].values())
        assert plan["risk_reward"]["recomputed"] is None
    else:
        assert plan["targets"]["conservative"] < plan["targets"]["primary"] < plan["targets"]["aggressive"]
    assert plan["horizon"] == {"min_trading_days": 20, "max_trading_days": 120}
    assert plan["validity"]["trading_sessions"] == 3
    assert k == original

def test_validator_recomputes_rr_and_rejects_bad_plan():
    p = build_candidate_trade_plan("x", make_kline(), {}, policy(), "2026-08-20", "risk")
    p["risk_reward"]["recomputed"] = 99
    p["targets"]["primary"] = p["entry"]["high"]
    verdict = validate_trade_plan(p, policy(), "2026-08-20")
    assert verdict["complete"] is False
    assert "trade_plan_targets_unordered" in verdict["reasons"] or "trade_plan_rr_below_min" in verdict["reasons"]

def test_builder_uses_technical_targets():
    risk = {
        "stop_loss": 95,
        "target_conservative": 110,
        "target_moderate": 115,
        "target_aggressive": 120,
        "risk_reward_ratio": 3.0,
        "target_source": "resistance",
    }
    with patch("core.candidate_trade_plan.calc_risk_reward", return_value=risk), \
         patch("core.candidate_trade_plan.calc_entry_signals", return_value={"verdict": "ready", "signals": []}):
        plan = build_candidate_trade_plan(
            "600000", make_kline(), {}, policy(), "2026-08-20",
            "若量价确认失败则逻辑失效")
    assert plan["targets"] == {"conservative": 110, "primary": 115, "aggressive": 120}
    assert plan["target_source"] == "resistance"


def test_risk_reward_uses_planned_entry_reference_and_source():
    df = pd.DataFrame(make_kline(start=100.0, rows=2)["data"])
    result = calc_risk_reward(
        df,
        {"atr": 2.0, "atr_pct": 2.0},
        {"resistance": [
            {"price": 103.0}, {"price": 120.0},
            {"price": 130.0}, {"price": 140.0},
        ]},
        direction="bullish",
        is_etf=False,
        entry_price=105.0,
    )
    assert result["entry_reference"] == 105.0
    assert result["target_source"] == "resistance"
    assert result["target_conservative"] == 120.0
    assert result["risk_reward_ratio"] == round(
        (result["target_moderate"] - 105.0)
        / (105.0 - result["stop_loss"]), 2
    )


def test_risk_reward_atr_projection_is_not_fixed_two_r():
    df = pd.DataFrame(make_kline(start=100.0, rows=2)["data"])
    result = calc_risk_reward(
        df,
        {"atr": 2.0, "atr_pct": 2.0},
        {"resistance": []},
        direction="bullish",
        is_etf=False,
        entry_price=105.0,
    )
    assert result["target_source"] == "atr_projection"
    assert result["target_conservative"] < result["target_moderate"]
    assert result["target_moderate"] < result["target_aggressive"]
    assert result["risk_reward_ratio"] == round(
        (result["target_moderate"] - result["entry_reference"])
        / (result["entry_reference"] - result["stop_loss"]), 2
    )


def test_invalid_entry_reference_disables_target_calculation():
    df = pd.DataFrame(make_kline(start=100.0, rows=2)["data"])
    result = calc_risk_reward(
        df,
        {"atr": 2.0, "atr_pct": 2.0},
        {"resistance": [{"price": 120.0}, {"price": 130.0},
                        {"price": 140.0}]},
        direction="bullish",
        is_etf=False,
        entry_price="invalid",
    )
    assert result["entry_reference"] is None
    assert result["target_source"] == "unavailable"
    assert result["target_conservative"] is None
    assert result["target_moderate"] is None
    assert result["target_aggressive"] is None
    assert result["risk_reward_ratio"] is None


def test_builder_does_not_create_synthetic_targets_when_ladder_unavailable():
    risk = {
        "stop_loss": 95,
        "target_conservative": 100,
        "target_moderate": 101,
        "target_aggressive": 102,
        "risk_reward_ratio": None,
        "target_source": "unavailable",
    }
    with patch("core.candidate_trade_plan.calc_risk_reward", return_value=risk), \
         patch("core.candidate_trade_plan.calc_entry_signals", return_value={
             "verdict": "ready", "signals": []}):
        plan = build_candidate_trade_plan(
            "600000", make_kline(), {}, policy(), "2026-08-20", "risk")
    assert plan["target_source"] == "unavailable"
    assert plan["targets"] == {
        "conservative": None, "primary": None, "aggressive": None}
    assert plan["risk_reward"]["recomputed"] is None
    assert plan["action"] == "wait"
    assert "high +" not in repr(plan["targets"])


def test_builder_keeps_atr_projection_observation_only():
    risk = {
        "stop_loss": 95,
        "target_conservative": 112,
        "target_moderate": 115,
        "target_aggressive": 118,
        "risk_reward_ratio": 1.8,
        "target_source": "atr_projection",
    }
    with patch("core.candidate_trade_plan.calc_risk_reward", return_value=risk), \
         patch("core.candidate_trade_plan.calc_entry_signals", return_value={
             "verdict": "ready", "signals": []}):
        plan = build_candidate_trade_plan(
            "600000", make_kline(), {}, policy(), "2026-08-20", "risk")
    assert plan["action"] == "wait"
    assert plan["target_source"] == "atr_projection"
    plan["action"] = "buy"
    verdict = validate_trade_plan(plan, policy(), "2026-08-20")
    assert verdict["complete"] is False
    assert "trade_plan_target_source_not_executable" in verdict["reasons"]

def test_builder_validity_is_not_only_basis_date():
    plan = build_candidate_trade_plan("600000", make_kline(), {}, policy(), "2026-08-20", "risk")
    assert plan["validity"]["valid_until"] != "2026-08-20"

def test_builder_empty_kline_returns_avoid_plan():
    plan = build_candidate_trade_plan("600000", {"data": []}, {}, policy(), "2026-08-20", "risk")
    assert plan["action"] == "avoid"


def run_candidate_trade_plan_tests():
    tests = (test_position_risk_budget, test_builder_schema_and_immutability,
             test_validator_recomputes_rr_and_rejects_bad_plan,
             test_builder_uses_technical_targets,
             test_risk_reward_uses_planned_entry_reference_and_source,
             test_risk_reward_atr_projection_is_not_fixed_two_r,
             test_invalid_entry_reference_disables_target_calculation,
             test_builder_does_not_create_synthetic_targets_when_ladder_unavailable,
             test_builder_keeps_atr_projection_observation_only,
             test_builder_validity_is_not_only_basis_date,
             test_builder_empty_kline_returns_avoid_plan)
    passed = 0
    for test in tests:
        test()
        passed += 1
    return passed, 0

if __name__ == "__main__":
    test_position_risk_budget()
    test_builder_schema_and_immutability()
    test_validator_recomputes_rr_and_rejects_bad_plan()
    test_builder_uses_technical_targets()
    test_risk_reward_uses_planned_entry_reference_and_source()
    test_risk_reward_atr_projection_is_not_fixed_two_r()
    test_invalid_entry_reference_disables_target_calculation()
    test_builder_does_not_create_synthetic_targets_when_ladder_unavailable()
    test_builder_keeps_atr_projection_observation_only()
    test_builder_validity_is_not_only_basis_date()
    test_builder_empty_kline_returns_avoid_plan()
    print("candidate trade-plan tests: PASS")
