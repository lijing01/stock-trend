import copy
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from core.candidate_trade_plan import (build_candidate_trade_plan,
    calculate_position_pct, validate_trade_plan)

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


def run_candidate_trade_plan_tests():
    tests = (test_position_risk_budget, test_builder_schema_and_immutability,
             test_validator_recomputes_rr_and_rejects_bad_plan)
    passed = 0
    for test in tests:
        test()
        passed += 1
    return passed, 0

if __name__ == "__main__":
    test_position_risk_budget()
    test_builder_schema_and_immutability()
    test_validator_recomputes_rr_and_rejects_bad_plan()
    print("candidate trade-plan tests: PASS")
