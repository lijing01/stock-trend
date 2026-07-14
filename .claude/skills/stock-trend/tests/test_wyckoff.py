"""Tests for Wyckoff analysis module."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from analysis.wyckoff import (
    compute_atr, compute_ma, detect_swing_points, mark_climaxes,
    detect_trading_range, analyze_vsa, compute_cause_effect,
    wyckoff_score, generate_trading_implication,
    classify_accumulation, classify_markup, classify_distribution, classify_markdown,
    PHASE_ACCUMULATION, PHASE_MARKUP, PHASE_DISTRIBUTION, PHASE_MARKDOWN, PHASE_UNKNOWN,
    SUB_SC, SUB_AR, SUB_ST, SUB_LPS, SUB_SPRING, SUB_PRE_MARKUP,
    SUB_JAC, SUB_BU, SUB_CONTINUATION,
    SUB_BC, SUB_UTAD, SUB_LPSY, SUB_SOW, SUB_PRE_MARKDOWN,
    SUB_BREAKDOWN, SUB_PANIC, SUB_STOPPING_VOL,
    extract_ohlcv, _safe_float, _ma_of_last_n, _find_first_breakout_bar,
    analyze, load_kline,
)


def _make_row(open_p, high, low, close, volume, date="20260101"):
    return {"open": open_p, "high": high, "low": low, "close": close, "vol": volume, "date": date}


class TestSafeFloat(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_safe_float(3.14), 3.14)
        self.assertEqual(_safe_float("3.14"), 3.14)
        self.assertEqual(_safe_float(0), 0.0)

    def test_invalid(self):
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float(""))


class TestComputeMA(unittest.TestCase):
    def test_basic(self):
        values = [1, 2, 3, 4, 5]
        result = compute_ma(values, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertEqual(result[2], 2.0)

    def test_empty(self):
        self.assertEqual(compute_ma([], 3), [])


class TestDetectSwingPoints(unittest.TestCase):
    def test_known_swing(self):
        closes = [10, 12, 15, 13, 11, 10, 9]
        highs =  [11, 13, 16, 14, 12, 11, 10]
        lows =   [9, 11, 14, 12, 10, 9, 8]
        volumes = [100] * 7
        atr = compute_atr(highs, lows, closes, period=3)
        atr = [a if a is not None else 2.0 for a in atr]
        swings = detect_swing_points(closes, highs, lows, volumes, atr, lookback=1)
        self.assertTrue(any(s["type"] == "high" and s["price"] == 16 for s in swings))


class TestWyckoffScore(unittest.TestCase):
    def test_accumulation_lps(self):
        self.assertEqual(wyckoff_score(PHASE_ACCUMULATION, SUB_LPS), 2.0)

    def test_markup_jac(self):
        self.assertEqual(wyckoff_score(PHASE_MARKUP, SUB_JAC), 2.0)

    def test_distribution_bc(self):
        self.assertEqual(wyckoff_score(PHASE_DISTRIBUTION, SUB_BC), -1.0)

    def test_markdown_breakdown(self):
        self.assertEqual(wyckoff_score(PHASE_MARKDOWN, SUB_BREAKDOWN), -2.5)

    def test_unknown(self):
        self.assertEqual(wyckoff_score("phase_unknown", ""), 0.0)

    def test_unmapped_returns_default(self):
        self.assertEqual(wyckoff_score(PHASE_ACCUMULATION, "nonexistent_sub"), 0.0)

    def test_clamping(self):
        self.assertAlmostEqual(wyckoff_score(PHASE_ACCUMULATION, SUB_LPS), 2.0)
        self.assertAlmostEqual(wyckoff_score(PHASE_MARKDOWN, SUB_BREAKDOWN), -2.5)


class TestTradingImplication(unittest.TestCase):
    def test_accumulation_st(self):
        imp = generate_trading_implication(PHASE_ACCUMULATION, SUB_ST)
        self.assertIn("二次测试", imp)

    def test_markup_jac(self):
        imp = generate_trading_implication(PHASE_MARKUP, SUB_JAC)
        self.assertIn("JAC", imp)

    def test_distribution_bc(self):
        imp = generate_trading_implication(PHASE_DISTRIBUTION, SUB_BC)
        self.assertIn("BC", imp)

    def test_markdown_panic(self):
        imp = generate_trading_implication(PHASE_MARKDOWN, SUB_PANIC)
        self.assertIn("恐慌", imp)

    def test_unknown(self):
        imp = generate_trading_implication(PHASE_UNKNOWN, "")
        self.assertIn("无明显", imp)

    def test_all_subphases_have_implications(self):
        """Every defined (phase, sub_phase) pair should have a non-empty implication."""
        for (phase, sub), _score in [
            ((PHASE_ACCUMULATION, SUB_SC), 0),
            ((PHASE_ACCUMULATION, SUB_AR), 0),
            ((PHASE_ACCUMULATION, SUB_ST), 0),
            ((PHASE_ACCUMULATION, SUB_SPRING), 0),
            ((PHASE_ACCUMULATION, SUB_LPS), 0),
            ((PHASE_ACCUMULATION, SUB_PRE_MARKUP), 0),
            ((PHASE_MARKUP, SUB_JAC), 0),
            ((PHASE_MARKUP, SUB_BU), 0),
            ((PHASE_MARKUP, SUB_CONTINUATION), 0),
            ((PHASE_DISTRIBUTION, SUB_BC), 0),
            ((PHASE_DISTRIBUTION, SUB_UTAD), 0),
            ((PHASE_DISTRIBUTION, SUB_LPSY), 0),
            ((PHASE_DISTRIBUTION, SUB_SOW), 0),
            ((PHASE_DISTRIBUTION, SUB_PRE_MARKDOWN), 0),
            ((PHASE_MARKDOWN, SUB_BREAKDOWN), 0),
            ((PHASE_MARKDOWN, SUB_PANIC), 0),
            ((PHASE_MARKDOWN, SUB_STOPPING_VOL), 0),
        ]:
            with self.subTest(phase=phase, sub=sub):
                imp = generate_trading_implication(phase, sub)
                self.assertTrue(imp, f"Empty implication for {phase}/{sub}")


class TestCauseEffect(unittest.TestCase):
    def test_upward_breakout(self):
        tr = {"support": 100, "resistance": 120, "range_height": 20,
              "duration_bars": 40, "touch_count": 5, "is_clear_range": True}
        result = compute_cause_effect(tr, 125)
        self.assertEqual(len(result["targets"]), 3)
        self.assertEqual(result["targets"][0]["price"], 145)
        self.assertEqual(result["horizontal_count"], 40)

    def test_downward_breakout(self):
        tr = {"support": 100, "resistance": 120, "range_height": 20,
              "duration_bars": 40, "touch_count": 5, "is_clear_range": True}
        result = compute_cause_effect(tr, 95)
        self.assertEqual(len(result["targets"]), 3)
        self.assertEqual(result["targets"][0]["price"], 75)  # 95 - 20

    def test_inside_range(self):
        tr = {"support": 100, "resistance": 120, "range_height": 20,
              "duration_bars": 40, "touch_count": 5, "is_clear_range": True}
        result = compute_cause_effect(tr, 110)
        self.assertEqual(result["targets"], [])


class TestVSA(unittest.TestCase):
    def test_absorption_signal(self):
        """High volume, narrow range, close mid → absorption."""
        closes = [100, 101, 102]
        highs =  [101, 102, 103]
        lows =   [99, 100, 101]
        opens =  [100, 100, 102]
        volumes = [100, 100, 300]  # volume spike on bar 2
        ohlcv = {"close": closes, "high": highs, "low": lows, "open": opens, "volume": volumes}
        atr = [2.0, 2.0, 2.0]
        signals = analyze_vsa(ohlcv, atr, ma50=[100, 100, 100])
        types = [s["type"] for s in signals]
        self.assertIn("absorption", types)

    def test_no_supply_signal(self):
        """Low volume, narrow down bar → no supply."""
        closes = [100, 99.5, 99]
        highs =  [100.5, 100, 99.8]
        lows =   [99.5, 99, 98.8]
        opens =  [100, 100, 99.5]
        volumes = [100, 30, 20]  # declining volume
        ohlcv = {"close": closes, "high": highs, "low": lows, "open": opens, "volume": volumes}
        # Use higher ATR so spread_ratio (spread/ATR) < 0.6 triggers no_supply
        atr = [3.0, 3.0, 3.0]
        signals = analyze_vsa(ohlcv, atr, ma50=[100, 100, 100])
        types = [s["type"] for s in signals]
        self.assertIn("no_supply", types)


class TestAnalyze(unittest.TestCase):
    def test_analyze_empty_data(self):
        """analyze() should return error meta for empty/missing data."""
        result = analyze("/nonexistent/path.json")
        self.assertIn("error", result.get("meta", {}))

    def test_analyze_insufficient_bars(self):
        """Fewer than 30 bars returns error."""
        rows = [_make_row(10 + i, 11 + i, 9 + i, 10 + i, 100) for i in range(20)]
        kline = {"meta": {"ts_code": "TEST"}, "data": rows}
        path = "/tmp/test_wyckoff_insufficient.json"
        with open(path, "w") as f:
            json.dump(kline, f)
        try:
            result = analyze(path)
            self.assertIn("error", result.get("meta", {}))
        finally:
            Path(path).unlink(missing_ok=True)


class TestExtractOHLCV(unittest.TestCase):
    def test_basic_extraction(self):
        rows = [
            {"open": 10, "high": 12, "low": 9, "close": 11, "vol": 1000, "date": "20260101"},
            {"open": 11, "high": 13, "low": 10, "close": 12, "vol": 1500, "date": "20260102"},
        ]
        result = extract_ohlcv(rows)
        self.assertEqual(result["close"], [11, 12])
        self.assertEqual(result["volume"], [1000, 1500])
        self.assertEqual(len(result["open"]), 2)

    def test_skips_invalid_rows(self):
        rows = [
            {"open": 10, "high": 12, "low": 9, "close": 11, "vol": 1000},
            {"open": None, "high": None, "low": None, "close": None, "vol": None},
            {"open": 12, "high": 14, "low": 11, "close": 13, "vol": 2000},
        ]
        result = extract_ohlcv(rows)
        self.assertEqual(len(result["close"]), 2)

    def test_supports_volume_field(self):
        rows = [{"open": 10, "high": 12, "low": 9, "close": 11, "volume": 1000}]
        result = extract_ohlcv(rows)
        self.assertEqual(result["volume"], [1000])


class TestFindFirstBreakoutBar(unittest.TestCase):
    def test_recent_breakout(self):
        """Price broke above resistance 3 bars ago."""
        closes = [100, 100, 100, 105, 107, 110]
        tr = {"resistance": 102, "support": 95, "is_clear_range": True}
        result = _find_first_breakout_bar(closes, tr, 5)
        self.assertEqual(result, 2)  # broke out at bar 5-2=3

    def test_no_breakout(self):
        """Price never above resistance."""
        closes = [90, 91, 92, 93, 94, 95]
        tr = {"resistance": 100, "support": 90, "is_clear_range": True}
        result = _find_first_breakout_bar(closes, tr, 5)
        self.assertIsNone(result)

    def test_resistance_equal_to_high(self):
        """Price touches but doesn't break resistance."""
        closes = [95, 98, 100, 98, 96, 97]
        tr = {"resistance": 100, "support": 90, "is_clear_range": True}
        result = _find_first_breakout_bar(closes, tr, 5)
        self.assertIsNone(result)


class TestMarkClimaxes(unittest.TestCase):
    def test_selling_climax(self):
        """Pivot low with high vol and long lower shadow."""
        swings = [{"index": 5, "type": "low", "price": 90, "volume_ratio": 3.0, "is_climax": False}]
        highs = [100]*10
        lows = [95]*10
        closes = [98]*10
        volumes = [100]*10
        atr = [2.0]*10
        result = mark_climaxes(swings, highs, lows, closes, volumes, atr)
        self.assertTrue(result[0]["is_climax"])
        self.assertEqual(result[0].get("climax_type"), "selling")


class TestMaLifecycle(unittest.TestCase):
    def test_ma_of_last_n(self):
        values = [10, 20, 30, 40, 50]
        self.assertEqual(_ma_of_last_n(values, 4, 3), 40)  # (30+40+50)/3
        self.assertEqual(_ma_of_last_n(values, 0, 3), 10)


if __name__ == "__main__":
    unittest.main()
