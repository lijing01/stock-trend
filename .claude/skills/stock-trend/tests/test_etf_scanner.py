"""Tests for ETF Scanner quick_score functions."""
import sys
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from etf_scanner import (
    code_to_ts_code, score_momentum, score_volume,
    score_capital_flow, score_shares_trend, score_iopv,
    compute_quick_score, build_combined_ranking,
)


def test_code_to_ts_code_shanghai():
    """5xxxxx codes map to .SH"""
    assert code_to_ts_code("513180") == "513180.SH"
    assert code_to_ts_code("510050") == "510050.SH"
    assert code_to_ts_code("588000") == "588000.SH"


def test_code_to_ts_code_shenzhen():
    """159xxx codes map to .SZ"""
    assert code_to_ts_code("159915") == "159915.SZ"
    assert code_to_ts_code("159949") == "159949.SZ"


def test_code_to_ts_code_int_input():
    """Integer input should be converted to string"""
    assert code_to_ts_code(513180) == "513180.SH"
    assert code_to_ts_code(159915) == "159915.SZ"


def test_score_momentum_bullish():
    """Bullish kline (prices uptrend) should score > 60"""
    kline = []
    price = 100.0
    for i in range(80):
        price += 1.0 + (i % 3) * 0.5
        kline.append({"close": round(price, 3), "vol": 1000000, "amount": price * 1000000})
    s = score_momentum(kline)
    assert s > 60, f"Expected >60, got {s}"


def test_score_momentum_bearish():
    """Bearish kline (prices downtrend) should score < 45"""
    kline = []
    price = 100.0
    for i in range(80):
        price -= 1.0 + (i % 3) * 0.5
        kline.append({"close": round(max(price, 10), 3), "vol": 1000000, "amount": 1000000})
    s = score_momentum(kline)
    assert s < 45, f"Expected <45, got {s}"


def test_score_momentum_insufficient_data():
    """Fewer than 20 klines should return neutral 50"""
    kline = [{"close": 100.0, "vol": 1000, "amount": 100000}] * 10
    s = score_momentum(kline)
    assert s == 50.0


def test_score_volume_high():
    """High volume ratio should score high"""
    kline = ([{"close": 100, "vol": 1000000, "amount": 100000000}] * 55 +
             [{"close": 101, "vol": 2000000, "amount": 200000000}] * 5)
    s = score_volume(kline)
    assert s > 60, f"Expected >60, got {s}"


def test_score_capital_flow_positive():
    """Positive main force net flow should score > 50"""
    data = {"data": [{"main_net_inflow": 50000000},
                     {"main_net_inflow": 30000000},
                     {"main_net_inflow": 40000000}]}
    s = score_capital_flow(data)
    assert s > 60, f"Expected >60, got {s}"


def test_score_capital_flow_none():
    """Missing capital flow data should return neutral 50"""
    assert score_capital_flow(None) == 50.0


def test_score_shares_trend_growth():
    """Positive shares growth should score > 60"""
    data = {"recent_flows": [{"shares_billion": 1.0},
                              {"shares_billion": 1.05},
                              {"shares_billion": 1.12}]}
    s = score_shares_trend(data)
    assert s > 70, f"Expected >70, got {s}"


def test_score_iopv_discount():
    """Moderate discount should score high"""
    data = {"nav": {"iopv_premium_pct": -0.3}}
    s = score_iopv(data)
    assert s > 60, f"Expected >60, got {s}"


def test_score_iopv_premium():
    """Premium should score low"""
    data = {"nav": {"iopv_premium_pct": 0.8}}
    s = score_iopv(data)
    assert s < 40, f"Expected <40, got {s}"


def test_compute_quick_score_normal():
    """Normal case: weights + kline = numeric score"""
    kline = [{"close": 100 + i, "vol": 1000000, "amount": 100000000}
             for i in range(80)]
    result = {"code": "513180", "ts_code": "513180.SH",
              "kline": kline, "capital_flow": None, "etf_data": None}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    assert scored["quick_score"] is not None
    assert 0 <= scored["quick_score"] <= 100


def test_compute_quick_score_no_kline():
    """No kline data should return None quick_score"""
    result = {"code": "513180", "ts_code": "513180.SH",
              "error": "kline_insufficient"}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    assert scored["quick_score"] is None


def test_build_combined_ranking():
    """Combined ranking with deep scores should weight correctly"""
    p1 = [{"code": "A", "ts_code": "A.SH", "quick_score": 80, "dimensions": {}, "category": "科技"},
          {"code": "B", "ts_code": "B.SH", "quick_score": 60, "dimensions": {}, "category": "宽基"}]
    p2 = {"A": {"deep_score": 90, "verdict": "up", "confidence": "high",
                 "name": "ETF_A", "risks": []},
          "B": {"deep_score": 50, "verdict": "neutral", "confidence": "low",
                 "name": "ETF_B", "risks": []}}
    combined = build_combined_ranking(p1, p2, {})
    assert combined[0]["code"] == "A"
    assert combined[0]["combined_score"] == round(0.3 * 80 + 0.7 * 90, 1)
    assert combined[1]["code"] == "B"


def test_score_momentum_flat():
    """Flat kline (no trend) should score near 50"""
    kline = []
    price = 100.0
    for i in range(80):
        # Symmetrical oscillation around 100, no net drift
        price += 0.2 if i % 2 == 0 else -0.2
        kline.append({"close": round(price, 3), "vol": 1000000, "amount": 100000000})
    s = score_momentum(kline)
    assert 40 <= s <= 65, f"Expected ~50-60, got {s}"


def test_phase1_real_etfs():
    """Integration test: run Phase 1 on first 3 ETFs from watchlist."""
    from etf_scanner import run_phase1

    test_wl = {
        "categories": [{"name": "测试", "etfs": [{"code": "510050"}, {"code": "512880"}, {"code": "513180"}]}],
        "settings": {
            "top_n": 3,
            "quick_kline_days": 60,
            "phase2_timeout": 45,
            "min_amount": 0,
            "max_workers": 2,
            "quick_score_weights": {"momentum": 30, "volume": 20, "capital_flow": 20, "shares_trend": 15, "iopv": 15}
        }
    }
    raw, ranked = run_phase1(test_wl, test_wl["settings"])
    assert len(ranked) >= 1
    for r in ranked:
        assert r["quick_score"] is not None
        assert 0 <= r["quick_score"] <= 100
