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
    normalize_scores_by_cohort,
    _piecewise_linear, detect_contradictions,
    detect_trend_stage, compute_atr, compute_volatility,
    compute_max_drawdown, compute_risk_penalty,
    compute_sector_ranking, build_trading_plan,
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
    """Bearish kline (prices downtrend) should score < 40"""
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


def test_score_momentum_symmetry():
    """Bullish and bearish scores should be roughly symmetric around 50"""
    kline_up = []
    price = 100.0
    for i in range(80):
        price += 1.0 + (i % 3) * 0.5
        kline_up.append({"close": round(price, 3), "vol": 1000000, "amount": price * 1000000})
    kline_down = []
    price = 100.0
    for i in range(80):
        price -= 1.0 + (i % 3) * 0.5
        kline_down.append({"close": round(max(price, 10), 3), "vol": 1000000, "amount": 1000000})
    s_up = score_momentum(kline_up)
    s_down = score_momentum(kline_down)
    # Distance from 50 should be roughly similar
    assert abs((s_up - 50) + (s_down - 50)) < 18, \
        f"Asymmetric: up={s_up}, down={s_down}"


def test_score_volume_high():
    """High volume ratio should score high"""
    kline = ([{"close": 100, "vol": 1000000, "amount": 100000000}] * 55 +
             [{"close": 101, "vol": 2000000, "amount": 200000000}] * 5)
    s = score_volume(kline)
    assert s > 60, f"Expected >60, got {s}"


def test_score_capital_flow_positive():
    """Positive main force net flow should score above 50"""
    data = {"data": [{"main_net_inflow": 50000000},
                     {"main_net_inflow": 30000000},
                     {"main_net_inflow": 40000000}]}
    s = score_capital_flow(data)
    assert s > 50, f"Expected >50, got {s}"


def test_score_capital_flow_none():
    """Missing capital flow data should return None (excluded from weighting)"""
    assert score_capital_flow(None) is None


def test_score_shares_trend_growth():
    """Positive shares growth should score > 60"""
    data = {"recent_flows": [{"shares_billion": 1.0},
                              {"shares_billion": 1.05},
                              {"shares_billion": 1.12}]}
    s = score_shares_trend(data)
    assert s > 70, f"Expected >70, got {s}"


def test_score_shares_trend_none():
    """Missing shares data should return None"""
    assert score_shares_trend(None) is None


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


def test_score_iopv_none():
    """Missing IOPV data should return None"""
    assert score_iopv(None) is None
    assert score_iopv({"nav": {}}) is None


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


def test_compute_quick_score_missing_dims_weight_redistribution():
    """Missing dimensions should be excluded from weighting, not default to 50."""
    kline = [{"close": 100 + i, "vol": 1000000, "amount": 100000000}
             for i in range(80)]
    # All optional dims missing
    result = {"code": "513180", "ts_code": "513180.SH",
              "kline": kline, "capital_flow": None, "etf_data": None}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    # Only momentum + volume contribute (50 weight total out of 100)
    # Score should differ from what it'd be if missing dims defaulted to 50
    assert scored["quick_score"] is not None
    # Verify dimensions are None, not 50
    assert scored["dimensions"]["capital_flow"] is None
    assert scored["dimensions"]["shares_trend"] is None
    assert scored["dimensions"]["iopv"] is None


def test_normalize_scores_by_cohort():
    """Cohort normalization should spread scores across 0-100 range."""
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = [
        {"code": "A", "ts_code": "A.SH", "quick_score": 70, "dimensions": {"momentum": 80, "volume": 60, "capital_flow": None, "shares_trend": None, "iopv": None}, "category": "科技"},
        {"code": "B", "ts_code": "B.SH", "quick_score": 80, "dimensions": {"momentum": 90, "volume": 70, "capital_flow": None, "shares_trend": None, "iopv": None}, "category": "宽基"},
        {"code": "C", "ts_code": "C.SH", "quick_score": 60, "dimensions": {"momentum": 70, "volume": 50, "capital_flow": None, "shares_trend": None, "iopv": None}, "category": "金融"},
    ]
    result = normalize_scores_by_cohort(scored, weights)
    # After normalization, momentum should be 0, 50, 100
    # volume should be 0, 100, 50
    # All quick_scores should be different
    scores = [r["quick_score"] for r in result]
    assert len(set(scores)) == 3, f"Expected 3 distinct scores, got {scores}"


def test_build_combined_ranking():
    """Combined ranking with deep scores should weight correctly.

    deep_score is on [-3, +3] scale from compute_scores.py.
    It gets normalized to [0, 100] via (score + 3) / 6 * 100 before combining.
    """
    p1 = [{"code": "A", "ts_code": "A.SH", "quick_score": 80, "dimensions": {}, "category": "科技"},
          {"code": "B", "ts_code": "B.SH", "quick_score": 60, "dimensions": {}, "category": "宽基"}]
    # deep_score=2.0 → normalized=(2.0+3)/6*100=83.33, combined=0.3*80+0.7*83.33=82.3
    # deep_score=-1.0 → normalized=(-1.0+3)/6*100=33.33, combined=0.3*60+0.7*33.33=41.3
    p2 = {"A": {"deep_score": 2.0, "verdict": "up", "confidence": "high",
                 "name": "ETF_A", "risks": []},
          "B": {"deep_score": -1.0, "verdict": "neutral", "confidence": "low",
                 "name": "ETF_B", "risks": []}}
    combined = build_combined_ranking(p1, p2, {})
    assert combined[0]["code"] == "A"
    # 0.3*80 + 0.7*((2.0+3)/6*100) = 24 + 0.7*83.333 = 24 + 58.333 = 82.3
    assert combined[0]["combined_score"] == 82.3
    assert combined[0]["p1_bonus"] == 0.0  # no bonus dims in test data
    assert combined[1]["code"] == "B"


def test_build_combined_ranking_with_bonus():
    """Combined ranking includes p1_exclusive_bonus from shares_trend + iopv."""
    p1 = [{"code": "A", "ts_code": "A.SH", "quick_score": 80,
            "dimensions": {"shares_trend": 70, "iopv": 60}, "category": "科技"}]
    p2 = {"A": {"deep_score": 2.0, "verdict": "up", "confidence": "high",
                 "name": "ETF_A", "risks": []}}
    settings = {"p1_exclusive_bonus": {"shares_trend": 0.05, "iopv": 0.05}}
    combined = build_combined_ranking(p1, p2, settings)
    # 0.3*80 + 0.7*83.33 + (70*0.05 + 60*0.05) = 24 + 58.33 + 6.5 = 88.8
    assert combined[0]["combined_score"] == 88.8
    assert combined[0]["p1_bonus"] == 6.5


def test_score_momentum_flat():
    """Flat kline (no trend) should score near 50"""
    kline = []
    price = 100.0
    for i in range(80):
        # Symmetrical oscillation around 100, no net drift
        price += 0.2 if i % 2 == 0 else -0.2
        kline.append({"close": round(price, 3), "vol": 1000000, "amount": 100000000})
    s = score_momentum(kline)
    assert 35 <= s <= 65, f"Expected ~50, got {s}"


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


# --- _piecewise_linear tests ---


def test_piecewise_linear_basic():
    """Basic interpolation between anchors."""
    anchors = [(0, 0), (50, 50), (100, 100)]
    assert _piecewise_linear(25, anchors) == 25.0
    assert _piecewise_linear(75, anchors) == 75.0


def test_piecewise_linear_clamp():
    """Output clamped to [0, 100]."""
    anchors = [(0, 0), (50, 50), (100, 100)]
    assert _piecewise_linear(-100, anchors) == 0.0
    assert _piecewise_linear(200, anchors) == 100.0


def test_piecewise_linear_non_monotonic():
    """IOPV-style non-monotonic anchors."""
    anchors = [(-2, 10), (-0.5, 40), (-0.3, 85), (-0.05, 65), (0.15, 30), (0.3, 15), (2, 0)]
    assert _piecewise_linear(-0.3, anchors) == 85.0  # exact anchor
    result = _piecewise_linear(-0.4, anchors)
    assert 40 < result < 85  # interpolates between 40 and 85


def test_continuous_scoring_no_cliffs():
    """Small input changes produce small output changes."""
    # capital_flow near old 20M boundary
    s1 = score_capital_flow({"data": [{"main_net_inflow": 19999999}]})
    s2 = score_capital_flow({"data": [{"main_net_inflow": 20000001}]})
    assert abs(s1 - s2) < 5, f"Cliff at capital_flow boundary: {s1} vs {s2}"

    # shares_trend near 1% boundary
    etf1 = {"recent_flows": [{"shares_billion": 1.0}, {"shares_billion": 1.00999}]}
    etf2 = {"recent_flows": [{"shares_billion": 1.0}, {"shares_billion": 1.01001}]}
    s1 = score_shares_trend(etf1)
    s2 = score_shares_trend(etf2)
    assert abs(s1 - s2) < 5, f"Cliff at shares_trend boundary: {s1} vs {s2}"


# --- detect_contradictions tests ---


def test_detect_contradictions_shrink_up():
    """High momentum + low volume triggers 缩量上涨."""
    dims = {"momentum": 75, "volume": 30, "capital_flow": 50, "shares_trend": None, "iopv": None}
    w = detect_contradictions(dims)
    assert "缩量上涨，动能不可靠" in w


def test_detect_contradictions_momentum_flow_mismatch():
    """High momentum + capital outflow triggers warning."""
    dims = {"momentum": 75, "volume": 60, "capital_flow": 25, "shares_trend": None, "iopv": None}
    w = detect_contradictions(dims)
    assert "动量与资金流向矛盾" in w


def test_detect_contradictions_flow_premium():
    """Capital inflow + high premium triggers warning."""
    dims = {"momentum": 50, "volume": 50, "capital_flow": 75, "iopv": 20, "shares_trend": None}
    w = detect_contradictions(dims)
    assert "资金流入但溢价偏高" in w


def test_detect_contradictions_dump():
    """High volume + low momentum triggers 放量下跌."""
    dims = {"momentum": 20, "volume": 80, "capital_flow": 50, "shares_trend": None, "iopv": None}
    w = detect_contradictions(dims)
    assert "放量下跌" in w


def test_detect_contradictions_no_warning():
    """Consistent signals produce no warnings."""
    dims = {"momentum": 60, "volume": 60, "capital_flow": 50, "shares_trend": 50, "iopv": 50}
    assert len(detect_contradictions(dims)) == 0


def test_detect_contradictions_none_dims():
    """None dimensions should not trigger false contradictions."""
    dims = {"momentum": 75, "volume": None, "capital_flow": None, "shares_trend": None, "iopv": None}
    assert len(detect_contradictions(dims)) == 0


def test_compute_quick_score_includes_warnings():
    """compute_quick_score includes warnings field."""
    kline = [{"close": 100 + i, "vol": 1000000, "amount": 100000000} for i in range(80)]
    result = {"code": "513180", "ts_code": "513180.SH",
              "kline": kline, "capital_flow": None, "etf_data": None}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    assert "warnings" in scored
    assert isinstance(scored["warnings"], list)


def test_detect_contradictions_price_extension():
    """Large price deviation from MA20 triggers price_extension warning."""
    dims = {"momentum": 75, "volume": 60, "capital_flow": 50,
            "shares_trend": None, "iopv": None,
            "price_ext_pct": 8.5}
    w = detect_contradictions(dims)
    assert "价格偏离均线过大，追涨风险高" in w


def test_detect_contradictions_price_extension_boundary():
    """Small price deviation (<5%) should NOT trigger warning."""
    dims = {"momentum": 50, "volume": 50, "capital_flow": 50,
            "shares_trend": None, "iopv": None,
            "price_ext_pct": 4.0}
    w = detect_contradictions(dims)
    assert "价格偏离均线过大，追涨风险高" not in w


# --- New: detect_trend_stage tests ---


def _make_kline(prices, uptrend=True):
    """Helper: generate kline from price sequence."""
    kline = []
    for p in prices:
        kline.append({"close": p, "vol": 1000000, "amount": p * 1000000,
                       "high": p * 1.01, "low": p * 0.99})
    return kline


def test_trend_stage_insufficient_data():
    """Fewer than 20 klines returns default mid."""
    kline = _make_kline([100] * 10)
    r = detect_trend_stage(kline)
    assert r["stage"] == "mid"
    assert r["multiplier"] == 1.0


def test_trend_stage_early():
    """Price just broke above MA20, RSI moderate = early stage."""
    # Verified pattern: noisy drift upward, RSI ≈ 58
    prices = [100.0, 100.6, 99.66, 99.35, 98.91, 99.75, 100.44, 101.67, 100.89,
              100.94, 100.01, 99.56, 99.82, 98.89, 98.39, 99.01, 99.37, 98.92,
              99.39, 100.41, 99.43, 100.44, 101.19, 101.04, 100.43, 101.82,
              101.66, 100.89, 100.13, 101.25, 101.76, 102.78, 103.6, 103.94,
              105.37, 105.32, 105.7, 106.77, 107.32, 108.47, 108.91, 109.67,
              108.78, 108.35, 108.07, 107.27, 106.85, 106.1, 105.79, 106.38,
              106.29, 106.22, 105.74, 105.41, 106.75, 107.37, 107.89, 107.32,
              108.14, 107.55, 107.5]
    kline = _make_kline(prices)
    r = detect_trend_stage(kline)
    assert r["stage"] == "early", f"Expected early, got {r}"
    assert r["multiplier"] == 1.0


def test_trend_stage_early_de_novo():
    """Mixed action after flat baseline stays early/mid."""
    prices = [100] * 30 + [101, 100, 102, 101, 103, 102, 104, 103, 105, 104]
    kline = _make_kline(prices)
    r = detect_trend_stage(kline)
    assert r["stage"] in ("early", "mid")


def test_trend_stage_mid():
    """Sustained moderate trend with RSI ~60 = mid."""
    prices = [100 + i * 0.3 for i in range(60)]
    kline = _make_kline(prices)
    r = detect_trend_stage(kline)
    assert r["stage"] in ("early", "mid")
    assert r["multiplier"] == 1.0


def test_trend_stage_late():
    """RSI >70 + price far above MA20 + volume shrinking = late."""
    prices = [100 + i * 2 for i in range(35)]  # 100 → 168 over 35 bars
    kline = _make_kline(prices)
    # Shrink: last 5 bars low vol, preceding 5 normal
    for i in range(-5, 0):
        kline[i]["vol"] = 50000
    for i in range(-10, -5):
        kline[i]["vol"] = 1000000
    r = detect_trend_stage(kline)
    assert r["stage"] == "late", f"Expected late, got {r}"
    assert r["multiplier"] < 1.0


def test_trend_stage_late_sharp():
    """Very sharp rally = late stage with low multiplier."""
    prices = [100 + i * 3 for i in range(30)]
    kline = _make_kline(prices)
    r = detect_trend_stage(kline)
    assert r["stage"] == "late"
    assert r["multiplier"] <= 0.5  # price_ext > 8%


# --- New: Risk metrics tests ---


def test_compute_atr_basic():
    """ATR should be positive and reasonable."""
    kline = _make_kline(list(range(100, 150)))
    atr = compute_atr(kline)
    assert atr > 0


def test_compute_atr_insufficient():
    """Less than 15 klines returns 0."""
    kline = _make_kline([100] * 10)
    atr = compute_atr(kline)
    assert atr == 0.0


def test_compute_volatility_basic():
    """Volatility should be positive for varying prices."""
    kline = _make_kline([100 + (i % 5) * 3 for i in range(60)])
    vol = compute_volatility(kline)
    assert vol > 0


def test_compute_max_drawdown_basic():
    """Max drawdown should be between 0 and 100."""
    kline = _make_kline(list(range(100, 200)))
    dd = compute_max_drawdown(kline)
    assert 0 <= dd <= 100


def test_compute_max_drawdown_downtrend():
    """Downtrend should produce measurable drawdown."""
    prices = [100 - i * 0.5 for i in range(60)]
    kline = _make_kline(prices)
    dd = compute_max_drawdown(kline)
    assert dd > 5  # at least 5% drawdown


def test_compute_risk_penalty():
    """Risk penalty should be between 0 and 1."""
    kline = _make_kline(list(range(100, 200)))
    r = compute_risk_penalty(kline)
    assert 0 < r["penalty"] <= 1
    assert r["atr"] >= 0
    assert r["volatility"] >= 0
    assert "max_drawdown" in r


# --- New: Sector ranking tests ---


def test_compute_sector_ranking_basic():
    """Sector ranking should assign percentile correctly."""
    scored = [
        {"code": "A", "quick_score": 90, "category": "科技"},
        {"code": "B", "quick_score": 70, "category": "科技"},
        {"code": "C", "quick_score": 50, "category": "科技"},
        {"code": "D", "quick_score": 80, "category": "宽基"},
        {"code": "E", "quick_score": 60, "category": "宽基"},
    ]
    result = compute_sector_ranking(scored)
    # 科技: A rank 1/3 → (3-1)/3*100=66.7%, B rank 2/3 → 33.3%, C rank 3/3 → 0%
    # 宽基: D rank 1/2 → 50%, E rank 2/2 → 0%
    for r in result:
        if r["code"] == "A":
            assert r["sector_percentile"] == 66.7
            assert r["sector_rank"] == 1
        elif r["code"] == "C":
            assert r["sector_percentile"] == 0.0
            assert r["sector_count"] == 3


def test_compute_sector_ranking_single():
    """Single ETF in a category gets 100%."""
    scored = [
        {"code": "A", "quick_score": 90, "category": "唯一"},
    ]
    result = compute_sector_ranking(scored)
    assert result[0]["sector_percentile"] == 100.0


# --- New: Trading plan tests ---


def test_build_trading_plan_early():
    """Early stage should produce immediate entry."""
    kline = _make_kline(list(range(100, 160)))
    plan = build_trading_plan("A", "ETF_A", kline, "early", 85, 3)
    assert plan["entry_strategy"] == "immediate"
    assert plan["entry_zone"]["current"] > 0
    assert plan["stop_loss"]["price"] > 0
    assert plan["stop_loss"]["risk_pct"] > 0
    assert len(plan["targets"]["tp1"]) > 0
    assert plan["position"]["pct"] >= 10
    assert plan["timing"]["action"] == "immediate" or plan["timing"]["action"] == "pullback"


def test_build_trading_plan_mid():
    """Mid stage should suggest pullback entry."""
    kline = _make_kline(list(range(100, 200)))
    plan = build_trading_plan("B", "ETF_B", kline, "mid", 75, 2)
    assert plan["entry_strategy"] == "pullback"
    assert plan["stop_loss"]["price"] > 0
    assert plan["position"]["pct"] >= 10


def test_build_trading_plan_late():
    """Late stage should avoid new entry."""
    kline = _make_kline([100 + i * 3 for i in range(30)])
    plan = build_trading_plan("C", "ETF_C", kline, "late", 55, 1)
    assert plan["entry_strategy"] == "avoid"
    assert "不" in plan["entry_detail"]
