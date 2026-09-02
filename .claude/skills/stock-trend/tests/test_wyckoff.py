"""Tests for Wyckoff analysis module."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from analysis import wyckoff as wyckoff_module
from analysis.wyckoff import (
    compute_atr, compute_ma, detect_swing_points, mark_climaxes,
    detect_trading_range, detect_trading_ranges, analyze_vsa, compute_cause_effect,
    wyckoff_score, generate_trading_implication, build_minor_phase,
    classify_accumulation, classify_markup, classify_distribution, classify_markdown,
    PHASE_ACCUMULATION, PHASE_MARKUP, PHASE_DISTRIBUTION, PHASE_MARKDOWN, PHASE_UNKNOWN,
    SUB_SC, SUB_AR, SUB_ST, SUB_LPS, SUB_SPRING, SUB_PRE_MARKUP,
    SUB_JAC, SUB_BU, SUB_CONTINUATION,
    SUB_BC, SUB_UTAD, SUB_LPSY, SUB_SOW, SUB_PRE_MARKDOWN,
    SUB_BREAKDOWN, SUB_PANIC, SUB_STOPPING_VOL,
    extract_ohlcv, _safe_float, _ma_of_last_n, _find_first_breakout_bar,
    _route_price_location, _choose_range_phase, detect_wyckoff_events,
    _is_lps_pullback, _current_event, _tr_state,
    is_buy_point, is_buy_signal,
    analyze, analyze_kline_dict, build_period_alignment, load_kline,
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


class TestMinorPhase(unittest.TestCase):
    def test_maps_existing_subphases_to_wyckoff_a_to_e_with_chinese_meaning(self):
        cases = [
            (PHASE_ACCUMULATION, SUB_SC, "A", "下跌动能开始衰竭"),
            (PHASE_ACCUMULATION, SUB_SPRING, "C", "下探测试抛压"),
            (PHASE_ACCUMULATION, SUB_LPS, "D", "需求占优"),
            (PHASE_MARKUP, SUB_JAC, "E", "价格已离开整理区"),
            (PHASE_DISTRIBUTION, SUB_UTAD, "C", "上冲测试需求后回落"),
            (PHASE_MARKDOWN, SUB_BREAKDOWN, "E", "价格向下离开整理区"),
        ]

        for phase, sub_phase, code, description in cases:
            with self.subTest(phase=phase, sub_phase=sub_phase):
                minor = build_minor_phase(phase, sub_phase)
                self.assertEqual(minor["code"], code)
                self.assertIn(f"阶段{code}", minor["name"])
                self.assertIn(description, minor["description"])

    def test_unconfirmed_structure_has_explicit_chinese_explanation(self):
        minor = build_minor_phase(PHASE_UNKNOWN, "")
        self.assertEqual(minor["code"], "-")
        self.assertEqual(minor["name"], "小级别阶段未确认")
        self.assertIn("A–E", minor["description"])

    def test_markup_lps_is_displayed_as_bu_lps_phase_d(self):
        minor = build_minor_phase(PHASE_MARKUP, SUB_LPS)
        self.assertEqual(minor["code"], "D")
        self.assertIn("LPS已确认", minor["name"])


class TestSosLps(unittest.TestCase):
    @staticmethod
    def _ohlcv_with_sos_bu_and_lps_confirmation():
        n = 60
        ohlcv = {
            "open": [100.0] * n, "high": [101.0] * n,
            "low": [99.0] * n, "close": [100.0] * n,
            "volume": [100.0] * n,
            "date": [f"202601{i + 1:02d}" for i in range(n)],
        }
        ohlcv["open"][50], ohlcv["high"][50], ohlcv["low"][50] = 110.0, 114.0, 109.0
        ohlcv["close"][50], ohlcv["volume"][50] = 113.0, 150.0
        ohlcv["open"][51], ohlcv["high"][51], ohlcv["low"][51] = 112.5, 113.5, 111.5
        ohlcv["close"][51], ohlcv["volume"][51] = 112.5, 110.0
        ohlcv["open"][52], ohlcv["high"][52], ohlcv["low"][52] = 111.5, 112.0, 110.2
        ohlcv["close"][52], ohlcv["volume"][52] = 111.2, 70.0
        ohlcv["open"][53], ohlcv["high"][53], ohlcv["low"][53] = 111.5, 114.0, 111.0
        ohlcv["close"][53], ohlcv["volume"][53] = 113.4, 95.0
        return ohlcv, [2.0] * n, {
            "id": "minor_sos_lps", "level": "minor", "support": 100.0,
            "resistance": 110.0, "support_idx": 10, "resistance_idx": 40,
            "touch_count": 5, "duration_bars": 30, "quality_score": 1.0,
            "is_clear_range": True,
        }

    def test_bu_candidate_is_not_a_confirmed_lps(self):
        ohlcv, atr, trading_range = self._ohlcv_with_sos_bu_and_lps_confirmation()
        as_of = {key: values[:53] for key, values in ohlcv.items()}
        events = detect_wyckoff_events(as_of, atr[:53], trading_range)
        bu = next(event for event in events if event["type"] == "bu")
        self.assertEqual(bu["status"], "candidate")
        self.assertFalse(any(
            event["type"] == "lps" and event["status"] == "confirmed"
            for event in events
        ))

    def test_confirmed_sos_is_followed_by_lps_only_after_reclaim(self):
        ohlcv, atr, trading_range = self._ohlcv_with_sos_bu_and_lps_confirmation()
        events = detect_wyckoff_events(ohlcv, atr, trading_range)
        sos = next(event for event in events if event["type"] == "sos")
        lps = next(event for event in events if event["type"] == "lps")
        self.assertEqual(sos["status"], "confirmed")
        self.assertEqual(lps["status"], "confirmed")
        self.assertEqual(lps["event_index"], 52)
        self.assertEqual(lps["detected_index"], 53)
        self.assertEqual(lps["parent_event"], "sos")
        rows = [
            {"open": ohlcv["open"][i], "high": ohlcv["high"][i],
             "low": ohlcv["low"][i], "close": ohlcv["close"][i],
             "vol": ohlcv["volume"][i], "date": ohlcv["date"][i]}
            for i in range(len(ohlcv["close"]))
        ]
        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[trading_range]):
            result = analyze_kline_dict({"meta": {"ts_code": "TEST"}, "data": rows})
        self.assertEqual(result["phase"]["primary"], PHASE_MARKUP)
        self.assertEqual(result["phase"]["primary_sub_phase"], SUB_LPS)
        self.assertEqual(result["phase"]["minor_phase"]["code"], "D")
        self.assertEqual(result["signal"]["event"], "lps")

    def test_lps_requires_shallow_low_volume_pullback(self):
        ohlcv, atr, trading_range = self._ohlcv_with_sos_bu_and_lps_confirmation()
        sos = {"event_index": 50, "detected_index": 51}
        self.assertTrue(_is_lps_pullback(ohlcv, atr, trading_range, sos, 52))
        ohlcv["volume"][52] = 140.0
        self.assertFalse(_is_lps_pullback(ohlcv, atr, trading_range, sos, 52))
        ohlcv["volume"][52] = 70.0
        ohlcv["close"][52] = 108.5
        self.assertFalse(_is_lps_pullback(ohlcv, atr, trading_range, sos, 52))

    def test_no_confirmed_sos_means_no_bu_or_lps(self):
        ohlcv, atr, trading_range = self._ohlcv_with_sos_bu_and_lps_confirmation()
        ohlcv["volume"][50] = 100.0
        events = detect_wyckoff_events(ohlcv, atr, trading_range)
        self.assertFalse(any(event["type"] in {"bu", "lps"} for event in events))

    def test_bu_expires_without_reclaim(self):
        ohlcv, atr, trading_range = self._ohlcv_with_sos_bu_and_lps_confirmation()
        ohlcv["close"][53] = 111.0
        ohlcv["high"][53] = 111.5
        events = detect_wyckoff_events(ohlcv, atr, trading_range)
        bu = next(event for event in events if event["type"] == "bu")
        self.assertEqual(bu["status"], "expired")
        self.assertFalse(any(event["type"] == "lps" for event in events))

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


class TestMinorWyckoffStructure(unittest.TestCase):
    """Regression tests for the small-scale event model (no market cache)."""

    def _event_fixture(self):
        n = 42
        ohlcv = {
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
            "close": [100.0] * n, "volume": [100.0] * n,
            "date": [f"202601{i + 1:02d}" for i in range(n)],
        }
        # Spring at index 30, reclaim at 31.
        ohlcv["low"][30], ohlcv["close"][30], ohlcv["volume"][30] = 97.8, 98.5, 70.0
        ohlcv["high"][31], ohlcv["close"][31] = 101.0, 100.5
        # Latest bar is a qualified SOS, but has no later hold confirmation.
        ohlcv["open"][-1], ohlcv["high"][-1], ohlcv["low"][-1] = 103.0, 106.0, 102.5
        ohlcv["close"][-1], ohlcv["volume"][-1] = 105.5, 150.0
        return ohlcv, [2.0] * n, {
            "id": "minor_10", "level": "minor", "support": 99.0, "resistance": 103.0,
            "support_idx": 10, "resistance_idx": 28, "touch_count": 5,
            "duration_bars": 18, "is_clear_range": True,
        }

    def test_router_has_no_overlap(self):
        tr = {"support": 100.0, "resistance": 110.0}
        self.assertEqual(_route_price_location(105.0, tr, 2.0), "in_range")
        self.assertEqual(_route_price_location(111.0, tr, 2.0), "upper_transition")
        self.assertEqual(_route_price_location(113.0, tr, 2.0), "above_range")
        self.assertEqual(_route_price_location(99.0, tr, 2.0), "lower_transition")
        self.assertEqual(_route_price_location(97.0, tr, 2.0), "below_range")

    def test_ambiguous_range_stays_unknown(self):
        selected, alternatives = _choose_range_phase((SUB_LPS, 0.60), (SUB_LPSY, 0.55))
        self.assertIsNone(selected)
        self.assertEqual([item["phase"] for item in alternatives],
                         [PHASE_ACCUMULATION, PHASE_DISTRIBUTION])

    def test_old_swings_are_excluded_from_recent_range(self):
        closes, atr = [100.0] * 200, [2.0] * 200
        swings = [
            {"index": 10, "type": "low", "price": 90.0},
            {"index": 35, "type": "high", "price": 110.0},
            {"index": 60, "type": "low", "price": 90.0},
            {"index": 70, "type": "high", "price": 110.0},
        ]
        self.assertIsNone(detect_trading_range(swings, closes, atr, max_bars=120))

    def test_minor_range_is_preserved_alongside_context_range(self):
        closes, atr = [105.0] * 250, [2.0] * 250
        swings = [
            {"index": 20, "type": "low", "price": 90.0},
            {"index": 50, "type": "high", "price": 110.0},
            {"index": 80, "type": "low", "price": 90.0},
            {"index": 110, "type": "high", "price": 110.0},
            {"index": 200, "type": "low", "price": 100.0},
            {"index": 215, "type": "high", "price": 106.0},
            {"index": 225, "type": "low", "price": 100.0},
            {"index": 235, "type": "high", "price": 106.0},
            {"index": 242, "type": "low", "price": 100.0},
        ]
        levels = {item["level"] for item in detect_trading_ranges(swings, closes, atr)}
        self.assertIn("context", levels)
        self.assertIn("minor", levels)

    def test_spring_history_and_current_sos_candidate_are_distinct(self):
        ohlcv, atr, tr = self._event_fixture()
        events = detect_wyckoff_events(ohlcv, atr, tr)
        spring = next(item for item in events if item["type"] == "spring")
        sos = next(item for item in events if item["type"] == "sos")
        self.assertEqual(spring["status"], "confirmed")
        self.assertGreater(spring["age_bars"], 8)
        self.assertEqual(sos["status"], "candidate")
        self.assertEqual(sos["age_bars"], 0)
        candidate = {
            "phase": {"primary": PHASE_MARKUP, "primary_sub_phase": SUB_JAC},
            "signal": {"status": sos["status"], "age_bars": sos["age_bars"]},
        }
        self.assertFalse(is_buy_signal(candidate))

    def test_buy_point_requires_confirmation_and_freshness(self):
        self.assertTrue(is_buy_point(PHASE_MARKUP, SUB_JAC, "confirmed", 0))
        self.assertFalse(is_buy_point(PHASE_MARKUP, SUB_JAC, "candidate", 0))
        self.assertFalse(is_buy_point(PHASE_MARKUP, SUB_JAC, "confirmed", 9))

    def test_candidate_sos_does_not_become_primary_jac(self):
        ohlcv, _, trading_range = self._event_fixture()
        trading_range["quality_score"] = 1.0
        rows = [
            {
                "open": ohlcv["open"][i], "high": ohlcv["high"][i],
                "low": ohlcv["low"][i], "close": ohlcv["close"][i],
                "vol": ohlcv["volume"][i], "date": ohlcv["date"][i],
            }
            for i in range(len(ohlcv["close"]))
        ]

        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[trading_range]), \
                patch("analysis.wyckoff.classify_markup", return_value=None):
            result = analyze_kline_dict({"meta": {"ts_code": "TEST"}, "data": rows})

        self.assertEqual(result["signal"]["status"], "candidate")
        self.assertNotEqual(result["phase"]["primary_sub_phase"], SUB_JAC)

    def test_confirmed_event_has_priority_over_newer_candidate(self):
        events = [
            {"type": "sos", "event_index": 10, "status": "confirmed", "age_bars": 2},
            {"type": "sos", "event_index": 11, "status": "candidate", "age_bars": 1},
        ]
        active = _current_event(events)
        self.assertEqual(active["event_index"], 10)
        self.assertEqual(active["status"], "confirmed")

    def test_tr_state_marks_confirmed_breakout_retest(self):
        tr = {"support": 14.5, "resistance": 16.42}
        events = [{
            "type": "sos", "event_index": 8, "event_date": "20260814",
            "status": "confirmed", "age_bars": 2,
        }]
        state = _tr_state(tr, [17.0, 16.4], 0.79, events)
        self.assertEqual(state["state"], "retest")
        self.assertEqual(state["confirmed_sos_date"], "20260814")

    def test_tr_state_marks_deep_return_after_confirmed_breakout_failed(self):
        tr = {"support": 14.5, "resistance": 16.42}
        events = [{
            "type": "sos", "event_index": 8, "event_date": "20260814",
            "status": "confirmed", "age_bars": 2,
        }]
        state = _tr_state(tr, [17.0, 15.9], 0.79, events)
        self.assertEqual(state["state"], "failed_breakout")

    def test_confirmed_sos_retest_is_not_current_jac(self):
        ohlcv, _, trading_range = self._event_fixture()
        # The latest close has returned to the former resistance area.
        ohlcv["close"][-1] = 102.0
        ohlcv["high"][-1] = 103.0
        ohlcv["low"][-1] = 101.5
        ohlcv["volume"][-1] = 70.0
        trading_range["quality_score"] = 1.0
        rows = [
            {
                "open": ohlcv["open"][i], "high": ohlcv["high"][i],
                "low": ohlcv["low"][i], "close": ohlcv["close"][i],
                "vol": ohlcv["volume"][i], "date": ohlcv["date"][i],
            }
            for i in range(len(ohlcv["close"]))
        ]
        confirmed_sos = {
            "type": "sos", "event_index": 40, "detected_index": 40,
            "event_date": rows[40]["date"], "detected_date": rows[40]["date"],
            "status": "confirmed", "age_bars": 1,
            "structure_level": "minor", "range_id": "minor_10",
            "confidence": 0.8,
        }
        with patch("analysis.wyckoff.detect_trading_ranges",
                   return_value=[trading_range]), \
                patch("analysis.wyckoff.detect_wyckoff_events",
                      return_value=[confirmed_sos]), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=((PHASE_MARKUP, SUB_JAC, 0.8), [])):
            result = analyze_kline_dict({"meta": {"ts_code": "TEST"}, "data": rows})

        self.assertEqual(result["tr_state"]["state"], "retest")
        self.assertEqual(result["phase"]["primary"], PHASE_ACCUMULATION)
        self.assertEqual(result["phase"]["primary_sub_phase"], SUB_PRE_MARKUP)
        self.assertEqual(result["signal"]["status"], "retest_pending")
        self.assertFalse(is_buy_signal(result))


class TestLongTermWyckoffContext(unittest.TestCase):
    @staticmethod
    def _trending_kline(count=80):
        rows = []
        for i in range(count):
            close = 100.0 + i * 0.2
            rows.append(_make_row(
                close - 0.1, close + 0.5, close - 0.5, close, 100.0,
                date=f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}",
            ))
        return {"meta": {"ts_code": "TEST"}, "data": rows}

    def test_ma_fallback_is_trend_context_not_primary_phase(self):
        result = analyze_kline_dict(self._trending_kline())

        self.assertEqual(result["phase"]["primary"], PHASE_UNKNOWN)
        self.assertEqual(result["trend_context"]["direction"], PHASE_MARKUP)

    def test_long_term_is_present_but_ineligible_below_250_bars(self):
        result = analyze_kline_dict(self._trending_kline(249))

        self.assertIn("long_term", result)
        self.assertFalse(result["long_term"]["eligible"])
        self.assertEqual(result["long_term"]["bars_available"], 249)
        self.assertEqual(result["long_term"]["minimum_bars"], 250)
        self.assertEqual(
            result["long_term"]["reason_code"], "insufficient_history")
        self.assertIn("249", result["long_term"]["reason"])

    def test_long_term_explains_missing_context_range(self):
        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[]):
            result = analyze_kline_dict(self._trending_kline(250))

        self.assertTrue(result["long_term"]["eligible"])
        self.assertEqual(
            result["long_term"]["reason_code"], "context_range_missing")
        self.assertIn("长期箱体", result["long_term"]["reason"])

    def test_long_term_explains_unclassified_context_range(self):
        context = {"id": "context_1", "level": "context", "support": 90.0,
                   "resistance": 110.0, "quality_score": 0.8, "support_idx": 0,
                   "resistance_idx": 200, "duration_bars": 200,
                   "is_clear_range": True}
        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[context]), \
                patch("analysis.wyckoff.detect_wyckoff_events", return_value=[]), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=(None, [])):
            result = analyze_kline_dict(self._trending_kline(250))

        self.assertEqual(
            result["long_term"]["reason_code"],
            "phase_evidence_insufficient",
        )
        self.assertIn("事件证据", result["long_term"]["reason"])

    def test_long_term_explains_ambiguous_context_evidence(self):
        context = {"id": "context_1", "level": "context", "support": 90.0,
                   "resistance": 110.0, "quality_score": 0.8, "support_idx": 0,
                   "resistance_idx": 200, "duration_bars": 200,
                   "is_clear_range": True}
        ambiguous = [
            {"phase": PHASE_ACCUMULATION, "confidence": 0.6},
            {"phase": PHASE_DISTRIBUTION, "confidence": 0.55},
        ]
        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[context]), \
                patch("analysis.wyckoff.detect_wyckoff_events", return_value=[]), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=(None, ambiguous)):
            result = analyze_kline_dict(self._trending_kline(250))

        self.assertEqual(
            result["long_term"]["reason_code"],
            "phase_evidence_ambiguous",
        )
        self.assertIn("证据接近", result["long_term"]["reason"])

    def test_period_alignment_blocks_countertrend_short_buy_signal(self):
        alignment = build_period_alignment(
            {"phase": PHASE_MARKUP, "sub_phase": SUB_JAC,
             "confidence": 0.7, "signal_status": "confirmed"},
            {"eligible": True, "phase": PHASE_DISTRIBUTION,
             "confidence": 0.7},
        )

        self.assertEqual(alignment["status"], "countertrend")
        self.assertEqual(alignment["recommendation_gate"], "observation")

    def test_long_term_phase_is_classified_from_context_not_short_trigger(self):
        context = {"id": "context_1", "level": "context", "support": 90.0,
                   "resistance": 110.0, "quality_score": 0.8, "support_idx": 0,
                   "resistance_idx": 200, "duration_bars": 200, "is_clear_range": True}
        minor = {"id": "minor_1", "level": "minor", "support": 99.0,
                 "resistance": 106.0, "quality_score": 0.8, "support_idx": 200,
                 "resistance_idx": 245, "duration_bars": 45, "is_clear_range": True}
        with patch("analysis.wyckoff.detect_trading_ranges", return_value=[context, minor]), \
                patch("analysis.wyckoff.detect_wyckoff_events", return_value=[]), \
                patch("analysis.wyckoff._classify_range_phase", side_effect=[
                    ((PHASE_MARKUP, SUB_JAC, 0.7), []),
                    ((PHASE_ACCUMULATION, SUB_LPS, 0.65), []),
                ]):
            result = analyze_kline_dict(self._trending_kline(250))

        self.assertEqual(result["short_term"]["phase"], PHASE_MARKUP)
        self.assertEqual(result["long_term"]["phase"], PHASE_ACCUMULATION)
        self.assertEqual(result["long_term"]["reason_code"], "")

    def test_post_lps_reconfirmation_requires_later_confirmed_sos_same_range(self):
        self.assertTrue(hasattr(wyckoff_module, "_is_post_lps_reconfirmation"))
        helper = wyckoff_module._is_post_lps_reconfirmation
        lps = {
            "type": "lps", "status": "confirmed", "event_index": 40,
            "detected_index": 41, "range_id": "minor_1",
        }
        later_sos = {
            "type": "sos", "status": "confirmed", "event_index": 50,
            "detected_index": 51, "range_id": "minor_1",
        }

        self.assertTrue(helper(later_sos, [lps, later_sos]))
        self.assertFalse(helper(
            {**later_sos, "status": "candidate"}, [lps, later_sos]))
        self.assertFalse(helper(
            {**later_sos, "range_id": "minor_2"}, [lps, later_sos]))
        self.assertFalse(helper(
            later_sos, [{**lps, "detected_index": 52}, later_sos]))

    def test_short_term_payload_marks_post_lps_sos_reconfirmation(self):
        context = {
            "id": "context_1", "level": "context", "support": 90.0,
            "resistance": 110.0, "quality_score": 0.8, "support_idx": 0,
            "resistance_idx": 200, "duration_bars": 200,
            "is_clear_range": True,
        }
        events = [
            {
                "type": "lps", "status": "confirmed", "event_index": 230,
                "detected_index": 231, "event_date": "20260907",
                "detected_date": "20260908", "age_bars": 18,
                "structure_level": "context", "range_id": "context_1",
                "confidence": 0.78,
            },
            {
                "type": "sos", "status": "confirmed", "event_index": 248,
                "detected_index": 249, "event_date": "20260925",
                "detected_date": "20260926", "age_bars": 0,
                "structure_level": "context", "range_id": "context_1",
                "confidence": 0.82,
            },
        ]
        with patch("analysis.wyckoff.detect_trading_ranges",
                   return_value=[context]), \
                patch("analysis.wyckoff.detect_wyckoff_events",
                      return_value=events), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=((PHASE_ACCUMULATION, SUB_LPS, 0.65), [])):
            result = analyze_kline_dict(self._trending_kline(250))

        self.assertEqual(result["short_term"]["sub_phase"], SUB_JAC)
        self.assertEqual(result["short_term"]["signal_status"], "confirmed")
        self.assertTrue(result["short_term"]["post_lps_reconfirmation"])


if __name__ == "__main__":
    unittest.main()
