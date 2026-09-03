"""Tests for backtesting/wyckoff_backtest.py"""
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backtesting.wyckoff_backtest import (
    run_backtest, _classify_signal, _select_signals, _stats,
    _forward_return, _forward_excursion, _build_result, slice_kline,
    _conf_band, _score_band, _render_md, _generate_html,
)

PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="wyckoff_backtest"):
    global PASSED, FAILED, SKIPPED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def _mk_kline(seed, base, n=250):
    """Deterministic synthetic kline."""
    random.seed(seed)
    rows = []
    d0 = 20250100
    p = base
    for i in range(n):
        p = p * (1 + random.uniform(-0.015, 0.015))
        rows.append({
            "date": str(d0 + i), "open": p, "close": p,
            "high": p * 1.01, "low": p * 0.99,
            "vol": 1000000 + random.randint(0, 100000),
            "amount": p * 1000000,
        })
    return rows


def _analysis(phase, sub, conf, score=0.0):
    return {
        "meta": {},
        "phase": {"primary": phase, "primary_sub_phase": sub, "confidence": conf},
        "wyckoff_score": score,
    }


# ── Unit tests ───────────────────────────────────────


def test_classify_buy():
    sig = _classify_signal(_analysis("accumulation", "lps", 0.7, 2.0), 0.3)
    test("WBT-01: classify buy point", sig is not None and sig["sub_phase"] == "lps"
         and sig["confidence"] == 0.7)


def test_classify_nonbuy():
    sig = _classify_signal(_analysis("distribution", "sow", 0.7, -2.0), 0.3)
    test("WBT-02: distribution not a buy sub-phase", sig is not None
         and sig["sub_phase"] == "sow")


def test_classify_error():
    sig = _classify_signal({"meta": {"error": "insufficient data"}}, 0.3)
    test("WBT-03: errored analysis → None", sig is None)


def test_classify_missing_sub():
    sig = _classify_signal({"meta": {}, "phase": {"primary": "accumulation",
                           "primary_sub_phase": "", "confidence": 0.5}}, 0.3)
    test("WBT-04: empty sub_phase → None", sig is None)


def test_candidate_signal_is_not_selected():
    analysis = _analysis("markup", "jac", 0.8, 2.0)
    analysis["signal"] = {"status": "candidate", "age_bars": 0}
    from analysis.wyckoff import is_buy_signal
    test("WBT-04a: candidate event is not buy signal", not is_buy_signal(analysis))


def test_score_100_normalize():
    sig = _classify_signal(_analysis("accumulation", "spring", 0.7, 3.0), 0.3)
    test("WBT-05: score_100 = 100 at +3", sig is not None and sig["score_100"] == 100.0)


def test_slice_kline():
    rows = _mk_kline(1, 100.0, 10)
    sliced = slice_kline(rows, rows[5]["date"])
    test("WBT-06: slice to date", len(sliced) == 6)


def test_forward_return():
    rows = [
        {"date": f"202601{i + 1:02d}", "close": float(100 + i)}
        for i in range(12)
    ]
    r = _forward_return(rows, 0, rows[10]["date"])
    expected = round((110.0 - 100.0) / 100.0, 6)
    test("WBT-07: target date close is used", r == expected,
         f"got={r}, expected={expected}")


def test_forward_return_beyond():
    rows = _mk_kline(3, 50.0, 20)
    r = _forward_return(rows, 18, "20991231")
    test("WBT-08: beyond range → None", r is None)


def test_classify_signal_keeps_strict_buy_level():
    analysis = {
        "meta": {},
        "phase": {"primary": "markup", "primary_sub_phase": "jac",
                  "confidence": 0.9},
        "signal": {"status": "confirmed", "age_bars": 0},
        "short_term": {
            "sub_phase": "jac", "signal_status": "confirmed",
            "signal_age_bars": 0, "post_lps_reconfirmation": True,
        },
        "wyckoff_score": 2.0,
    }
    signal = _classify_signal(analysis, 0.3)
    test("WBT-08c: strict reconfirmed JAC is level 3",
         signal is not None and signal["buy_point_level"] == 3)

    analysis["short_term"]["post_lps_reconfirmation"] = False
    signal = _classify_signal(analysis, 0.3)
    test("WBT-08d: first JAC is ungraded",
         signal is not None and signal["buy_point_level"] is None)


def test_forward_excursion():
    rows = [
        {"date": "20260101", "close": 10.0, "high": 10.0, "low": 10.0},
        {"date": "20260102", "close": 10.5, "high": 11.0, "low": 9.0},
        {"date": "20260103", "close": 10.8, "high": 12.0, "low": 9.5},
    ]
    result = _forward_excursion(rows, 0, "20260103")
    test("WBT-08e: forward path returns MAE/MFE",
         result == {"mae": -0.1, "mfe": 0.2}, f"result={result}")


def test_forward_return_20_and_60_days():
    rows = [
        {
            "date": (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d"),
            "close": float(100 + i),
        }
        for i in range(61)
    ]
    ret20 = _forward_return(rows, 0, rows[20]["date"])
    ret60 = _forward_return(rows, 0, rows[60]["date"])
    test("WBT-08a: exact 20-day return", ret20 == 0.2,
         f"got={ret20}, expected=0.2")
    test("WBT-08b: exact 60-day return", ret60 == 0.6,
         f"got={ret60}, expected=0.6")


def test_select_signals_dedup():
    obs = [(0, "a", "lps", 0.5, 60.0, {}), (5, "a", "lps", 0.5, 60.0, {}),
           (15, "a", "lps", 0.6, 65.0, {}), (40, "a", "spring", 0.8, 80.0, {})]
    sel = _select_signals(obs, 10)
    test("WBT-09: episode dedup keeps 3", len(sel) == 3
         and sel[0][0] == 0 and sel[1][0] == 15 and sel[2][0] == 40)


def test_stats_math():
    st = _stats([0.05, -0.02, 0.03, 0.01, -0.01])
    test("WBT-10: win_rate 3/5", st["win_rate"] == 0.6)
    test("WBT-11: avg = 0.012", abs(st["avg"] - 0.012) < 1e-9)
    test("WBT-12: avg_loss", abs(st["avg_loss"] - 0.015) < 1e-9)


def test_stats_empty():
    test("WBT-13: empty stats → None", _stats([]) is None)


def test_bands():
    test("WBT-14: conf band 70+", _conf_band({"confidence": 0.8}) == "置信≥0.7")
    test("WBT-15: score band 70+", _score_band({"score_100": 75}) == "100分≥70(强势)")


def test_level_aggregation_and_evidence_status():
    def signal(level, ret, mae, sub_phase):
        return {
            "code": "600001", "ts_code": "600001.SH", "name": "test",
            "date": "20260101", "phase": "accumulation",
            "sub_phase": sub_phase, "confidence": 0.8, "score_100": 70.0,
            "buy_point_level": level, "returns": {"5": ret},
            "excursions": {"5": {"mae": mae, "mfe": 0.1}},
        }

    result = _build_result(
        [{"ts_code": "600001.SH"}],
        [signal(1, 0.01, -0.02, "spring"),
         signal(2, 0.02, -0.04, "lps"),
         signal(3, 0.03, -0.06, "jac"),
         signal(None, -0.01, -0.08, "jac")],
        {"5": [0.01, 0.0, -0.01, 0.02]},
        (5,),
        {"lookback_days": 80, "sample_interval": 5,
         "min_confidence": 0.3, "min_gap": 10, "sample_dates": 5},
    )
    test("WBT-16: exact buy-level buckets exist",
         result["by_buy_level"]["5"]["level_1"]["count"] == 1
         and result["by_buy_level"]["5"]["level_2"]["count"] == 1
         and result["by_buy_level"]["5"]["level_3"]["count"] == 1
         and result["by_buy_level"]["5"]["ungraded"]["count"] == 1)
    test("WBT-17: level risk stats are aggregated",
         result["risk_by_buy_level"]["5"]["level_2"]["avg_mae"] == -0.04)
    test("WBT-18: small level samples stay evidence-insufficient",
         result["evidence"]["status"] == "evidence_insufficient")


# ── Integration tests ──────────────────────────────────


def test_run_backtest_synthetic():
    km = {
        "600519.SH": {"data": _mk_kline(1, 100.0)},
        "000001.SZ": {"data": _mk_kline(2, 50.0)},
    }
    stocks = [
        {"code": "600519", "ts_code": "600519.SH", "name": "t1"},
        {"code": "000001", "ts_code": "000001.SZ", "name": "t2"},
    ]
    r = run_backtest(stocks, km, lookback_days=80, eval_windows=(5, 10),
                     sample_interval=5)
    meta = r["meta"]
    test("WBT-I01: runs without error", "error" not in meta)
    test("WBT-I02: baseline observed", r["summary"]["5"]["baseline"]["count"] > 0)
    test("WBT-I03: signals is list", isinstance(r["signals"], list))
    test("WBT-I04: per-window summary present",
         "5" in r["summary"] and "10" in r["summary"])
    test("WBT-I05: by_sub_phase buckets exist", isinstance(r["by_sub_phase"]["5"], dict))
    test("WBT-I06: strategy_stats present", "sample_count" in r["strategy_stats"])
    # every signal carries a date + sub_phase
    for s in r["signals"]:
        test("WBT-I07: signal has date/sub", bool(s.get("date")) and bool(s.get("sub_phase")))


def test_baseline_does_not_depend_on_phase_detection():
    km = {
        "600519.SH": {"data": _mk_kline(1, 100.0)},
        "000001.SZ": {"data": _mk_kline(2, 50.0)},
    }
    stocks = [
        {"code": "600519", "ts_code": "600519.SH", "name": "t1"},
        {"code": "000001", "ts_code": "000001.SZ", "name": "t2"},
    ]
    with patch("backtesting.wyckoff_backtest.analyze_kline_dict",
               return_value={"meta": {"error": "unclassified"}}):
        result = run_backtest(
            stocks, km, lookback_days=80, eval_windows=(5,), sample_interval=5)
    baseline = result["summary"]["5"]["baseline"]
    test("WBT-I08: baseline survives zero classifications",
         baseline is not None and baseline["count"] > 0,
         f"baseline={baseline}")
    test("WBT-I09: zero classifications produce zero signals",
         result["meta"]["signal_count"] == 0)


def test_run_backtest_error_path():
    km = {"600519.SH": {"data": []}, "000001.SZ": {"data": _mk_kline(2, 50.0, 20)}}
    stocks = [
        {"code": "600519", "ts_code": "600519.SH", "name": "t1"},
        {"code": "000001", "ts_code": "000001.SZ", "name": "t2"},
    ]
    r = run_backtest(stocks, km, lookback_days=80, eval_windows=(5, 10))
    test("WBT-I08: insufficient valid stocks → error", "error" in r["meta"])


def test_renderers():
    km = {
        "600519.SH": {"data": _mk_kline(1, 100.0)},
        "000001.SZ": {"data": _mk_kline(2, 50.0)},
    }
    stocks = [
        {"code": "600519", "ts_code": "600519.SH", "name": "t1"},
        {"code": "000001", "ts_code": "000001.SZ", "name": "t2"},
    ]
    r = run_backtest(stocks, km, lookback_days=80, eval_windows=(5, 10), sample_interval=5)
    md = _render_md(r)
    html = _generate_html(r, "20260101-000000")
    test("WBT-I09: MD renders", "维科夫买点回测" in md)
    test("WBT-I10: MD exposes buy-level evidence", "按买点等级" in md and "证据状态" in md)
    test("WBT-I11: HTML renders", "winChart" in html and "plotly" in html)
    test("WBT-I12: HTML exposes buy-level evidence", "按买点等级" in html and "证据状态" in html)


def test_render_zero_signals():
    """Zero-signal result must not crash renderers (None summary)."""
    r = {
        "meta": {"timestamp": "", "stocks_tested": 3, "signal_count": 0,
                 "min_confidence": 0.3, "min_gap": 10, "eval_windows": [5, 10, 20],
                 "sample_dates": 5},
        "summary": {"5": {"signals": None, "baseline": {"count": 10, "win_rate": 0.4}, "alpha": {}},
                    "10": {"signals": None, "baseline": {"count": 10, "win_rate": 0.4}, "alpha": {}},
                    "20": {"signals": None, "baseline": {"count": 10, "win_rate": 0.4}, "alpha": {}}},
        "by_confidence": {"5": {}}, "by_sub_phase": {"5": {}},
        "by_phase": {"5": {}}, "by_score_100": {"5": {}},
        "ic": {"5": {}, "10": {}, "20": {}},
        "signals": [], "per_stock": [],
    }
    html = _generate_html(r, "ts")
    md = _render_md(r)
    test("WBT-I13: zero-signal HTML renders", "winChart" in html)
    test("WBT-I14: zero-signal MD renders", "维科夫买点回测" in md)


# ── Runner ────────────────────────────────────────────


def run_wyckoff_backtest_tests():
    print("\n🏛 维科夫买点回测测试 (Wyckoff Backtest)")
    print("=" * 60)
    test_classify_buy()
    test_classify_nonbuy()
    test_classify_error()
    test_classify_missing_sub()
    test_candidate_signal_is_not_selected()
    test_score_100_normalize()
    test_slice_kline()
    test_forward_return()
    test_forward_return_beyond()
    test_classify_signal_keeps_strict_buy_level()
    test_forward_excursion()
    test_forward_return_20_and_60_days()
    test_select_signals_dedup()
    test_stats_math()
    test_stats_empty()
    test_bands()
    test_level_aggregation_and_evidence_status()
    test_run_backtest_synthetic()
    test_baseline_does_not_depend_on_phase_detection()
    test_run_backtest_error_path()
    test_renderers()
    test_render_zero_signals()
    print(f"\nWyckoff Backtest 结果: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return PASSED, FAILED


if __name__ == "__main__":
    run_wyckoff_backtest_tests()
    if FAILED > 0:
        sys.exit(1)
