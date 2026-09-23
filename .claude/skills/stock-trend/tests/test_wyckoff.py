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
    format_minor_phase_text,
    classify_accumulation, classify_markup, classify_distribution, classify_markdown,
    PHASE_ACCUMULATION, PHASE_MARKUP, PHASE_DISTRIBUTION, PHASE_MARKDOWN, PHASE_UNKNOWN,
    SUB_SC, SUB_AR, SUB_ST, SUB_LPS, SUB_SPRING, SUB_PRE_MARKUP,
    SUB_JAC, SUB_BU, SUB_CONTINUATION,
    SUB_BC, SUB_UTAD, SUB_LPSY, SUB_SOW, SUB_PRE_MARKDOWN,
    SUB_BREAKDOWN, SUB_PANIC, SUB_STOPPING_VOL,
    extract_ohlcv, _safe_float, _ma_of_last_n, _find_first_breakout_bar,
    _route_price_location, _choose_range_phase, detect_wyckoff_events,
    _is_lps_pullback, _current_event, _tr_state,
    find_event_trading_range, evaluate_confirmed_lps_health,
    evaluate_confirmed_spring_health, evaluate_confirmed_jac_health,
    LPS_EVENT_HEALTH_RULE_VERSION,
    is_buy_point, is_buy_signal, is_executable_buy_signal,
    build_entry_timing, classify_entry_timing, classify_buy_point_level,
    read_current_event_state, LPS_STATE_UNKNOWN_REASON_CODE,
    PERIOD_ALIGNMENT_RULE_VERSION,
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


class TestMinorPhaseDisplay(unittest.TestCase):
    def test_formats_phase_description_and_trigger_kline(self):
        text = format_minor_phase_text({
            "sub_phase": SUB_LPS,
            "minor_phase": {
                "name": "阶段D：需求确认",
                "description": "需求占优，回踩缩量后等待向上确认",
                "trigger": {"date": "20260923", "low": 17.2, "close": 17.35},
            },
        })
        self.assertEqual(
            text,
            "阶段D：需求确认（需求占优，回踩缩量后等待向上确认）；"
            "触发K线 20260923 低17.2 收17.35",
        )

    def test_preserves_lps_event_health_display(self):
        text = format_minor_phase_text({
            "short_term": {"sub_phase": SUB_LPS, "current_state": "failed_breakout"},
            "minor_phase": {"name": "阶段D：LPS历史已确认，突破失败",
                            "description": "旧事件不可执行"},
        })
        self.assertEqual(text, "阶段D：LPS历史已确认，当前突破失败、等待重新构筑（旧事件不可执行）")


class TestEntryTiming(unittest.TestCase):
    def test_fresh_lps_is_executable(self):
        timing = build_entry_timing("lps", "confirmed", 0, False, 102, 100, 2)
        self.assertEqual(timing["status"], "entry_fresh")
        self.assertTrue(timing["executable"])
        self.assertAlmostEqual(timing["trigger_extension_atr"], 1.0)

    def test_late_confirmation_is_stale(self):
        timing = build_entry_timing("lps", "confirmed", 4, False, 102, 100, 2)
        self.assertEqual(timing["reason_code"], "wyckoff_signal_stale")
        self.assertFalse(timing["executable"])

    def test_breakout_chasing_is_overextended(self):
        timing = build_entry_timing("lps", "confirmed", 0, False, 110, 100, 2)
        self.assertEqual(timing["status"], "entry_overextended")
        self.assertEqual(timing["reason_code"], "entry_overextended")

    def test_first_jac_waits_for_retest(self):
        timing = build_entry_timing("jac", "confirmed", 0, False, 101, 100, 2)
        self.assertEqual(timing["reason_code"], "first_jac_wait_retest")
        self.assertFalse(timing["executable"])

    def test_missing_trigger_distance_is_unknown(self):
        timing = build_entry_timing("lps", "confirmed", 0, False, 102, None, 2)
        self.assertEqual(timing["status"], "entry_distance_unknown")
        self.assertFalse(timing["executable"])

    def test_analysis_executable_gate_reads_additive_timing(self):
        analysis = {
            "phase": {"primary": PHASE_MARKUP, "primary_sub_phase": SUB_LPS},
            "signal": {"status": "confirmed", "age_bars": 0},
            "entry_timing": {"status": "entry_fresh", "executable": True},
        }
        self.assertTrue(is_executable_buy_signal(analysis))
        self.assertEqual(classify_entry_timing(analysis)["status"], "entry_fresh")

    def test_lps_health_block_precedes_stale_gate(self):
        timing = build_entry_timing(
            "lps", "confirmed", 8, False, 11.5, 11.95, 0.4,
            "follow_through_weakened",
        )
        self.assertEqual(
            timing["reason_code"], "wyckoff_lps_follow_through_weakened")
        self.assertFalse(timing["executable"])

    def test_old_callers_without_health_state_remain_compatible(self):
        timing = build_entry_timing("lps", "confirmed", 0, False, 102, 100, 2)
        self.assertTrue(timing["executable"])
        self.assertEqual(timing["current_state"], "not_evaluated")


class TestConfirmedLpsHealth(unittest.TestCase):
    @staticmethod
    def _fixture(current_close=11.95, *, current_low=None,
                 intervening_closes=None):
        closes = [11.8, 11.9, 11.95, 12.05, 12.0]
        if intervening_closes:
            closes.extend(intervening_closes)
        closes.append(current_close)
        lows = [value - 0.08 for value in closes]
        lows[2] = 11.93
        if current_low is not None:
            lows[-1] = current_low
        ohlcv = {
            "close": closes,
            "low": lows,
            "date": [f"202609{index + 5:02d}" for index in range(len(closes))],
        }
        atr = [0.4] * len(closes)
        event = {
            "type": "lps", "status": "confirmed",
            "event_index": 2, "detected_index": 3,
            "event_date": "20260907", "detected_date": "20260908",
            "range_id": "minor_191", "breakout_atr": 0.3,
        }
        event_range = {
            "id": "minor_191", "support": 9.8, "resistance": 10.74,
        }
        return event, event_range, ohlcv, atr

    def test_healthy_lps_holds_trigger_region(self):
        args = self._fixture(11.94)
        health = evaluate_confirmed_lps_health(*args)
        self.assertEqual(health["state"], "confirmed_holding")
        self.assertEqual(health["structural_floor"], 10.65)

    def test_close_below_trigger_but_above_range_floor_is_weakened(self):
        args = self._fixture(11.5)
        health = evaluate_confirmed_lps_health(*args)
        self.assertEqual(health["state"], "follow_through_weakened")
        self.assertEqual(
            health["reason_code"], "wyckoff_lps_follow_through_weakened")

    def test_close_below_original_range_floor_is_hard_failure(self):
        args = self._fixture(10.6)
        health = evaluate_confirmed_lps_health(*args)
        self.assertEqual(health["state"], "failed_breakout")
        self.assertEqual(health["reason_code"], "wyckoff_failed_breakout")

    def test_wick_below_range_floor_with_close_recovery_is_not_hard_failure(self):
        args = self._fixture(11.94, current_low=10.2)
        health = evaluate_confirmed_lps_health(*args)
        self.assertEqual(health["state"], "confirmed_holding")

    def test_hard_failure_is_sticky_after_later_close_recovery(self):
        args = self._fixture(11.94, intervening_closes=[10.6])
        health = evaluate_confirmed_lps_health(*args)
        self.assertEqual(health["state"], "failed_breakout")
        self.assertEqual(health["breach_index"], 5)

    def test_missing_event_range_is_unknown_and_not_executable(self):
        event, _, ohlcv, atr = self._fixture(11.94)
        ranges = [{"id": "swing_137", "resistance": 12.2}]
        self.assertIsNone(find_event_trading_range(event, ranges))
        health = evaluate_confirmed_lps_health(event, None, ohlcv, atr)
        self.assertEqual(health["state"], "state_unknown")
        self.assertEqual(
            health["reason_code"], LPS_STATE_UNKNOWN_REASON_CODE)
        timing = build_entry_timing(
            "lps", "confirmed", 1, False, 11.94, 11.95, 0.4,
            health["state"],
        )
        self.assertFalse(timing["executable"])
        self.assertEqual(
            timing["reason_code"], LPS_STATE_UNKNOWN_REASON_CODE)
        self.assertIsNone(classify_buy_point_level({
            "short_term": {
                "sub_phase": "lps", "signal_status": "confirmed",
                "signal_age_bars": 1, "current_state": health["state"],
            }
        }))

    def test_buy_level_state_falls_back_to_signal_then_event_health(self):
        short_term = {
            "sub_phase": "lps", "signal_status": "confirmed",
            "signal_age_bars": 1,
        }
        signal_only = {
            "short_term": dict(short_term),
            "signal": {"current_state": "follow_through_weakened"},
        }
        health_only = {
            "short_term": dict(short_term),
            "event_health": {"state": "state_unknown"},
        }

        self.assertEqual(
            read_current_event_state(signal_only), "follow_through_weakened")
        self.assertIsNone(classify_buy_point_level(signal_only))
        self.assertEqual(
            read_current_event_state(health_only), "state_unknown")
        self.assertIsNone(classify_buy_point_level(health_only))

    def test_buy_level_state_prefers_short_term_over_fallback_payloads(self):
        payload = {
            "short_term": {
                "sub_phase": "lps", "signal_status": "confirmed",
                "signal_age_bars": 1, "current_state": "confirmed_holding",
            },
            "signal": {"current_state": "failed_breakout"},
            "event_health": {"state": "state_unknown"},
        }

        self.assertEqual(
            read_current_event_state(payload), "confirmed_holding")
        self.assertEqual(classify_buy_point_level(payload)["number"], 2)

    def test_nonhealthy_lps_is_not_a_shared_buy_signal(self):
        for current_state in (
                "follow_through_weakened", "failed_breakout", "state_unknown"):
            with self.subTest(current_state=current_state):
                self.assertFalse(is_buy_signal({
                    "phase": {
                        "primary": PHASE_MARKUP,
                        "primary_sub_phase": SUB_LPS,
                    },
                    "signal": {"status": "confirmed", "age_bars": 0},
                    "short_term": {"current_state": current_state},
                }))

    def test_603517_semantics_keep_history_but_block_weakened_lps(self):
        rows = [
            _make_row(11.75, 12.0, 11.6, 11.8, 100.0,
                      date=f"202607{index + 1:02d}")
            for index in range(60)
        ]
        rows[56] = _make_row(
            12.0, 12.05, 11.93, 11.95, 80.0, date="20260907")
        rows[57] = _make_row(
            11.98, 12.15, 11.96, 12.1, 90.0, date="20260909")
        rows[59] = _make_row(
            11.9, 11.92, 11.45, 11.5, 140.0, date="20260915")
        minor = {
            "id": "minor_191", "level": "minor", "support": 9.8,
            "resistance": 10.74, "quality_score": 0.8,
            "support_idx": 10, "resistance_idx": 50,
            "duration_bars": 40, "is_clear_range": True,
        }
        swing = {
            "id": "swing_137", "level": "swing", "support": 10.0,
            "resistance": 12.1, "quality_score": 0.9,
            "support_idx": 0, "resistance_idx": 55,
            "duration_bars": 55, "is_clear_range": True,
        }
        lps = {
            "type": "lps", "status": "confirmed",
            "event_index": 56, "detected_index": 57,
            "event_date": "20260907", "detected_date": "20260909",
            "age_bars": 2, "bars_since_event": 3,
            "structure_level": "minor", "range_id": "minor_191",
            "confidence": 0.78, "breakout_atr": 0.3,
        }

        def events_for_range(_ohlcv, _atr, trading_range):
            return [dict(lps)] if trading_range["id"] == "minor_191" else []

        with patch("analysis.wyckoff.detect_trading_ranges",
                   return_value=[minor, swing]), \
                patch("analysis.wyckoff._select_current_range",
                      return_value=swing), \
                patch("analysis.wyckoff.detect_wyckoff_events",
                      side_effect=events_for_range), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=((PHASE_MARKUP, SUB_LPS, 0.7), [])):
            result = analyze_kline_dict({
                "meta": {"ts_code": "603517.SH", "end_date": "20260915"},
                "data": rows,
            })

        self.assertEqual(result["confirmed_event"]["status"], "confirmed")
        self.assertEqual(result["confirmed_event"]["event_date"], "20260907")
        self.assertEqual(result["confirmed_event"]["detected_date"], "20260909")
        self.assertEqual(result["confirmed_event"]["range_id"], "minor_191")
        self.assertEqual(
            result["short_term"]["current_state"], "follow_through_weakened")
        self.assertEqual(
            result["event_health"]["rule_version"],
            LPS_EVENT_HEALTH_RULE_VERSION,
        )
        self.assertNotEqual(
            result["short_term"]["current_state"], "failed_breakout")
        self.assertFalse(result["entry_timing"]["executable"])
        self.assertEqual(
            result["entry_timing"]["reason_code"],
            "wyckoff_lps_follow_through_weakened",
        )
        self.assertIsNone(classify_buy_point_level(result))
        self.assertIn("历史已确认", result["phase"]["minor_phase"]["name"])
        self.assertEqual(result["confirmed_event"], lps)


class TestConfirmedSpringJacHealth(unittest.TestCase):
    @staticmethod
    def _spring_fixture(current_close=10.5, *, current_low=None,
                        intervening_closes=None):
        closes = [10.0, 10.2, 9.2, 10.3, 10.4]
        if intervening_closes:
            closes.extend(intervening_closes)
        closes.append(current_close)
        lows = [value - 0.1 for value in closes]
        lows[2] = 8.8
        if current_low is not None:
            lows[-1] = current_low
        ohlcv = {
            "close": closes,
            "low": lows,
            "date": [f"202609{index + 5:02d}" for index in range(len(closes))],
        }
        atr = [0.5] * len(closes)
        event = {
            "type": "spring", "status": "confirmed",
            "event_index": 2, "detected_index": 3,
            "event_date": "20260907", "detected_date": "20260908",
            "range_id": "minor_spring", "breakout_atr": 0.5,
        }
        event_range = {
            "id": "minor_spring", "support": 10.0, "resistance": 12.0,
        }
        return event, event_range, ohlcv, atr

    @staticmethod
    def _jac_fixture(current_close=12.2, *, intervening_closes=None):
        closes = [10.0, 10.2, 12.4, 12.3]
        if intervening_closes:
            closes.extend(intervening_closes)
        closes.append(current_close)
        ohlcv = {
            "close": closes,
            "low": [value - 0.1 for value in closes],
            "date": [f"202609{index + 5:02d}" for index in range(len(closes))],
        }
        atr = [0.5] * len(closes)
        event = {
            "type": "sos", "status": "confirmed",
            "event_index": 2, "detected_index": 3,
            "event_date": "20260907", "detected_date": "20260908",
            "range_id": "minor_jac", "breakout_atr": 0.5,
        }
        event_range = {
            "id": "minor_jac", "support": 10.0, "resistance": 12.0,
        }
        return event, event_range, ohlcv, atr

    def test_confirmed_spring_holds_and_wick_is_tolerated(self):
        args = self._spring_fixture(current_close=10.5, current_low=8.5)
        health = evaluate_confirmed_spring_health(*args)
        self.assertEqual(health["state"], "confirmed_holding")
        self.assertEqual(health["structural_floor"], 8.8)

    def test_spring_close_below_event_low_is_invalidated(self):
        args = self._spring_fixture(current_close=8.7)
        health = evaluate_confirmed_spring_health(*args)
        self.assertEqual(health["state"], "structure_invalidated")
        self.assertEqual(
            health["reason_code"], "wyckoff_spring_structure_invalidated")

    def test_spring_invalidation_is_sticky_after_recovery(self):
        args = self._spring_fixture(
            current_close=10.5, intervening_closes=[8.7])
        health = evaluate_confirmed_spring_health(*args)
        self.assertEqual(health["state"], "structure_invalidated")
        self.assertEqual(health["breach_index"], 5)

    def test_spring_missing_event_range_is_unknown(self):
        event, _, ohlcv, atr = self._spring_fixture()
        health = evaluate_confirmed_spring_health(event, None, ohlcv, atr)
        self.assertEqual(health["state"], "state_unknown")
        self.assertEqual(
            health["reason_code"], "wyckoff_spring_state_unknown")

    def test_confirmed_jac_holds_above_original_resistance(self):
        args = self._jac_fixture(current_close=12.2)
        health = evaluate_confirmed_jac_health(*args)
        self.assertEqual(health["state"], "confirmed_holding")
        self.assertEqual(health["structural_floor"], 11.5)

    def test_jac_return_to_box_top_is_retest_pending(self):
        args = self._jac_fixture(current_close=11.8)
        health = evaluate_confirmed_jac_health(*args)
        self.assertEqual(health["state"], "retest_pending")
        self.assertEqual(
            health["reason_code"], "wyckoff_jac_retest_pending")

    def test_jac_deep_return_to_box_is_failed(self):
        args = self._jac_fixture(current_close=11.4)
        health = evaluate_confirmed_jac_health(*args)
        self.assertEqual(health["state"], "failed_breakout")
        self.assertEqual(
            health["reason_code"], "wyckoff_jac_failed_breakout")

    def test_jac_failure_is_sticky_after_recovery(self):
        args = self._jac_fixture(
            current_close=12.2, intervening_closes=[11.4])
        health = evaluate_confirmed_jac_health(*args)
        self.assertEqual(health["state"], "failed_breakout")
        self.assertEqual(health["breach_index"], 4)

    def test_jac_missing_event_range_is_unknown(self):
        event, _, ohlcv, atr = self._jac_fixture()
        health = evaluate_confirmed_jac_health(event, None, ohlcv, atr)
        self.assertEqual(health["state"], "state_unknown")
        self.assertEqual(
            health["reason_code"], "wyckoff_jac_state_unknown")

    def test_nonhealthy_spring_and_jac_states_fail_closed(self):
        cases = (
            ("spring", "structure_invalidated", "spring"),
            ("jac", "retest_pending", "sos"),
            ("jac", "failed_breakout", "sos"),
            ("jac", "state_unknown", "sos"),
        )
        for sub_phase, state, event_type in cases:
            with self.subTest(sub_phase=sub_phase, state=state):
                payload = {
                    "short_term": {
                        "sub_phase": sub_phase,
                        "signal_status": "confirmed",
                        "signal_age_bars": 0,
                        "post_lps_reconfirmation": True,
                        "current_state": state,
                    },
                }
                self.assertIsNone(classify_buy_point_level(payload))
                self.assertFalse(is_buy_signal({
                    "phase": {
                        "primary": PHASE_MARKUP,
                        "primary_sub_phase": sub_phase,
                    },
                    "signal": {"status": "confirmed", "age_bars": 0},
                    "short_term": {"current_state": state},
                }))
                timing = build_entry_timing(
                    sub_phase, "confirmed", 0, True,
                    12.0, 12.0, 0.5, state, event_type,
                )
                self.assertFalse(timing["executable"])


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

    def test_is_buy_signal_rejects_all_nonhealthy_lps_states(self):
        for current_state in (
                "follow_through_weakened", "failed_breakout", "state_unknown"):
            with self.subTest(current_state=current_state):
                analysis = {
                    "phase": {
                        "primary": PHASE_MARKUP,
                        "primary_sub_phase": SUB_LPS,
                    },
                    "signal": {"status": "confirmed", "age_bars": 0},
                    "short_term": {"current_state": current_state},
                }
                self.assertTrue(is_buy_point(
                    PHASE_MARKUP, SUB_LPS, "confirmed", 0))
                self.assertFalse(is_buy_signal(analysis))

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

    @staticmethod
    def _rows_from_ohlcv(ohlcv):
        return [
            {
                "open": ohlcv["open"][index],
                "high": ohlcv["high"][index],
                "low": ohlcv["low"][index],
                "close": ohlcv["close"][index],
                "vol": ohlcv["volume"][index],
                "date": ohlcv["date"][index],
            }
            for index in range(len(ohlcv["close"]))
        ]

    def test_confirmed_spring_health_is_propagated_into_analysis_gates(self):
        ohlcv, _, trading_range = self._event_fixture()
        ohlcv["low"][38], ohlcv["close"][38] = 97.0, 98.0
        ohlcv["close"][41] = 96.5
        trading_range.update({"support": 99.0, "resistance": 103.0,
                              "quality_score": 1.0})
        spring = {
            "type": "spring", "event_index": 38, "detected_index": 39,
            "event_date": ohlcv["date"][38], "detected_date": ohlcv["date"][39],
            "status": "confirmed", "age_bars": 2,
            "structure_level": "minor", "range_id": "minor_10",
            "confidence": 0.8,
        }
        with patch("analysis.wyckoff.detect_trading_ranges",
                   return_value=[trading_range]), \
                patch("analysis.wyckoff.detect_wyckoff_events",
                      return_value=[spring]), \
                patch("analysis.wyckoff._classify_range_phase",
                      return_value=((PHASE_ACCUMULATION, SUB_SPRING, 0.8), [])):
            result = analyze_kline_dict({
                "meta": {"ts_code": "TEST"},
                "data": self._rows_from_ohlcv(ohlcv),
            })

        self.assertEqual(result["event_health"]["event_type"], "spring")
        self.assertEqual(
            result["short_term"]["current_state"], "structure_invalidated")
        self.assertEqual(result["signal"]["current_state"], "structure_invalidated")
        self.assertFalse(result["entry_timing"]["executable"])
        self.assertIsNone(classify_buy_point_level(result))
        self.assertFalse(is_buy_signal(result))

    def test_confirmed_jac_health_drives_retest_and_sticky_failure(self):
        cases = (
            (11.8, [], "retest_pending", "wyckoff_jac_retest_pending"),
            (12.2, [(40, 11.4)], "failed_breakout", "wyckoff_jac_failed_breakout"),
        )
        for latest_close, intervening, expected_state, expected_reason in cases:
            with self.subTest(expected_state=expected_state):
                ohlcv, _, trading_range = self._event_fixture()
                ohlcv["close"][38] = 12.4
                ohlcv["close"][41] = latest_close
                for index, close in intervening:
                    ohlcv["close"][index] = close
                trading_range.update({"support": 10.0, "resistance": 12.0,
                                      "quality_score": 1.0})
                sos = {
                    "type": "sos", "event_index": 38, "detected_index": 39,
                    "event_date": ohlcv["date"][38],
                    "detected_date": ohlcv["date"][39],
                    "status": "confirmed", "age_bars": 2,
                    "structure_level": "minor", "range_id": "minor_10",
                    "confidence": 0.8, "breakout_atr": 0.5,
                }
                with patch("analysis.wyckoff.detect_trading_ranges",
                           return_value=[trading_range]), \
                        patch("analysis.wyckoff.detect_wyckoff_events",
                              return_value=[sos]), \
                        patch("analysis.wyckoff._classify_range_phase",
                              return_value=((PHASE_MARKUP, SUB_JAC, 0.8), [])):
                    result = analyze_kline_dict({
                        "meta": {"ts_code": "TEST"},
                        "data": self._rows_from_ohlcv(ohlcv),
                    })

                self.assertEqual(result["event_health"]["event_type"], "jac")
                self.assertEqual(
                    result["short_term"]["current_state"], expected_state)
                self.assertEqual(result["signal"]["current_state"], expected_state)
                self.assertEqual(
                    result["event_health"]["reason_code"], expected_reason)
                self.assertFalse(result["entry_timing"]["executable"])
                self.assertIsNone(classify_buy_point_level(result))
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

    def test_period_alignment_blocks_all_nonhealthy_lps_states(self):
        cases = {
            "follow_through_weakened": (
                "current_health_follow_through_weakened", "后续转弱"),
            "failed_breakout": (
                "current_health_failed_breakout", "突破失败"),
            "state_unknown": (
                "current_health_state_unknown", "健康状态未知"),
        }
        for current_state, (expected_status, label_fragment) in cases.items():
            with self.subTest(current_state=current_state):
                alignment = build_period_alignment(
                    {
                        "phase": PHASE_MARKUP, "sub_phase": SUB_LPS,
                        "signal_status": "confirmed", "signal_age_bars": 0,
                        "current_state": current_state,
                    },
                    {
                        "eligible": True, "phase": PHASE_MARKUP,
                        "confidence": 0.8,
                    },
                )

                self.assertEqual(
                    alignment["rule_version"], PERIOD_ALIGNMENT_RULE_VERSION)
                self.assertEqual(alignment["status"], expected_status)
                self.assertEqual(
                    alignment["recommendation_gate"], "observation")
                self.assertEqual(alignment["current_state"], current_state)
                self.assertIn(label_fragment, alignment["label"])

    def test_period_alignment_blocks_jac_and_spring_unhealthy_states(self):
        cases = (
            (SUB_JAC, "retest_pending", "current_health_retest_pending"),
            (SUB_JAC, "failed_breakout", "current_health_failed_breakout"),
            (SUB_SPRING, "structure_invalidated",
             "current_health_structure_invalidated"),
        )
        for sub_phase, current_state, expected_status in cases:
            with self.subTest(sub_phase=sub_phase, current_state=current_state):
                alignment = build_period_alignment(
                    {
                        "phase": PHASE_MARKUP, "sub_phase": sub_phase,
                        "signal_status": "confirmed", "signal_age_bars": 0,
                        "current_state": current_state,
                    },
                    {"eligible": True, "phase": PHASE_MARKUP, "confidence": 0.8},
                )
                self.assertEqual(alignment["status"], expected_status)
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

    def test_classify_buy_point_level_is_strict_and_fresh(self):
        self.assertTrue(hasattr(wyckoff_module, "classify_buy_point_level"))
        classify = wyckoff_module.classify_buy_point_level

        def payload(sub_phase, *, status="confirmed", age=0,
                    reconfirmed=False):
            return {
                "short_term": {
                    "sub_phase": sub_phase,
                    "signal_status": status,
                    "signal_age_bars": age,
                    "post_lps_reconfirmation": reconfirmed,
                }
            }

        self.assertEqual(classify(payload("spring"))["number"], 1)
        self.assertEqual(classify(payload("lps"))["number"], 2)
        self.assertEqual(
            classify(payload("jac", reconfirmed=True))["number"], 3)
        self.assertIsNone(classify(payload("jac", reconfirmed=False)))
        self.assertIsNone(classify(payload("lps", status="candidate")))
        self.assertIsNone(classify(payload("lps", age=4)))
        self.assertIsNone(classify(payload("spring", age=9)))
        self.assertIsNone(classify(payload("lps", age=11)))
        self.assertIsNone(
            classify(payload("jac", age=9, reconfirmed=True)))

        unknown = payload("lps")
        del unknown["short_term"]["signal_age_bars"]
        self.assertIsNone(classify(unknown))

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
