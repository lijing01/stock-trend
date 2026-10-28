#!/usr/bin/env python3
"""Tests for the causal grouped weak-to-strong v2 detector."""

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.daily_weak_to_strong_v2 import analyze_daily_weak_to_strong_v2


def trading_days(count, start=date(2026, 1, 5)):
    values = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def bar(day, close, *, high=None, low=None, open_=None, volume=1000):
    return {
        "trade_date": day.replace("-", ""),
        "open": close if open_ is None else open_,
        "high": close + 0.1 if high is None else high,
        "low": close - 0.1 if low is None else low,
        "close": close,
        "vol": volume,
    }


def fixture(final_rows, warmup=120):
    days = trading_days(warmup + len(final_rows) + 5)
    rows = [bar(days[index], 10 + index * 0.001, volume=900)
            for index in range(warmup)]
    for offset, values in enumerate(final_rows):
        rows.append(bar(days[warmup + offset], **values))
    payload = {"meta": {"data_source": "fixture", "adj": "qfq"}, "data": rows}
    return payload, days


def evidence(payload, days, confirmed=True):
    raw = payload["data"][-1]["trade_date"]
    target = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return {
        "as_of_date": target,
        "close_evidence": {"confirmed": confirmed, "provisional": not confirmed,
                           "source": "fixture"},
        "calendar_evidence": {"source": "fixture", "identity": "calendar-v2",
                              "coverage_start": days[0], "coverage_end": days[-1],
                              "trading_dates": days},
    }


class DailyWeakToStrongV2Tests(unittest.TestCase):
    def analyze(self, payload, days, **kwargs):
        supplied = evidence(payload, days)
        supplied.update(kwargs)
        return analyze_daily_weak_to_strong_v2(
            payload, code="000001.SZ", name="fixture", **supplied)

    def test_range_breakout_is_causal_and_keeps_parent_structure(self):
        payload, days = fixture([
            {"close": 10.05, "high": 10.15, "low": 10.0},
            {"close": 9.85, "high": 10.0, "low": 9.75},
            {"close": 9.80, "high": 9.95, "low": 9.70, "volume": 700},
            {"close": 9.90, "high": 10.0, "low": 9.80, "volume": 700},
            {"close": 9.95, "high": 10.05, "low": 9.85, "volume": 700},
            {"close": 10.00, "high": 10.08, "low": 9.90, "volume": 700},
            {"close": 10.40, "open_": 10.02, "high": 10.42, "low": 9.98,
             "volume": 1800},
        ])
        result = self.analyze(payload, days)
        self.assertEqual(result["stage"], "RANGE_BREAKOUT")
        self.assertEqual(result["confirmation"], "close_confirmed")
        self.assertEqual(result["signal_date"], result["as_of_date"])
        self.assertGreater(result["structure"]["anchor_high"], 10.0)
        self.assertEqual(result["metrics"]["metric_date"], result["as_of_date"])
        self.assertEqual(result["metrics"]["cycle_id"], result["cycle_id"])

        future = copy.deepcopy(payload)
        future["data"].append(bar(days[len(payload["data"])], 2.0,
                                  high=2.1, low=1.9, volume=9999))
        replayed = self.analyze(future, days, as_of_date=result["as_of_date"])
        for key in ("stage", "cycle_id", "signal_date", "structure", "metrics"):
            self.assertEqual(replayed[key], result[key])

    def test_early_repair_requires_real_ma_reclaim_and_strong_close(self):
        payload, days = fixture([
            {"close": 10.00, "high": 10.75, "low": 9.95},
            {"close": 9.75, "high": 10.70, "low": 9.65},
            {"close": 9.70, "high": 10.65, "low": 9.60},
            {"close": 9.72, "high": 10.60, "low": 9.62},
            {"close": 9.74, "high": 10.55, "low": 9.64},
            {"close": 9.76, "high": 10.50, "low": 9.66},
            {"close": 10.08, "open_": 9.77, "high": 10.12, "low": 9.75,
             "volume": 1200},
        ])
        result = self.analyze(payload, days)
        self.assertEqual(result["stage"], "EARLY_REPAIR")
        self.assertTrue(result["checks"]["ma_reclaim"]["passed"])
        self.assertTrue(result["checks"]["daily_return"]["passed"])
        self.assertLess(result["metrics"]["close"], result["metrics"]["h5"])

        weak_close = copy.deepcopy(payload)
        weak_close["data"][-1].update(open=9.77, high=10.40, low=9.70, close=10.08)
        rejected = self.analyze(weak_close, days)
        self.assertNotEqual(rejected["stage"], "EARLY_REPAIR")
        self.assertFalse(rejected["checks"].get("close_strength", {"passed": False})["passed"])

    def test_three_percent_bounce_below_both_mas_is_not_repair(self):
        payload, days = fixture([
            {"close": 10.00, "high": 10.10, "low": 9.90},
            {"close": 9.63, "high": 9.75, "low": 9.55},
            {"close": 9.26, "high": 9.35, "low": 9.22},
            {"close": 9.54, "open_": 9.44, "high": 9.80, "low": 9.33,
             "volume": 1050},
        ])
        result = self.analyze(payload, days)
        self.assertNotIn(result["stage"], {"EARLY_REPAIR", "LOCAL_CONFIRMATION",
                                           "RANGE_BREAKOUT"})
        self.assertFalse(result["metrics"]["above_ma10"])
        self.assertFalse(result["metrics"]["above_ma20"])
        self.assertLess(result["metrics"]["close_strength"], 0.60)

    def test_deep_repair_preserves_origin_and_cycle_identity(self):
        payload, days = fixture([
            {"close": 9.9, "high": 10.0, "low": 9.8},
            {"close": 9.6, "high": 9.8, "low": 9.5},
            {"close": 8.7, "high": 9.0, "low": 8.5},
            {"close": 8.6, "high": 8.8, "low": 8.4},
            {"close": 8.65, "high": 8.75, "low": 8.5},
            {"close": 8.70, "high": 8.80, "low": 8.6},
        ])
        result = self.analyze(payload, days)
        self.assertEqual(result["origin_pattern_type"], "base_consolidation")
        self.assertEqual(result["pattern_type"], "deep_repair")
        self.assertEqual(result["stage"], "PREPARING")
        self.assertTrue(result["cycle_id"])
        self.assertGreater(result["metrics"]["drawdown_from_parent"], 0.12)

    def test_strict_v1_candidate_is_independent_and_wait_has_no_stale_metrics(self):
        payload, days = fixture([
            {"close": 10.13, "high": 10.20, "low": 10.05},
        ])
        target = evidence(payload, days)["as_of_date"]
        v1 = {"state": "BREAKOUT_CONFIRMED", "evidence_status": "ready",
              "signal_date": target, "cycle_id": "strict-cycle", "d0_date": target,
              "H": 10.0, "va_v0_ratio": 0.7, "breakout_volume_ratio": 1.3,
              "close_strength": 0.9, "breakout_ratio": 1.013, "checks": {}}
        result = self.analyze(payload, days, v1_result=v1)
        self.assertEqual(result["origin_pattern_type"], "short_pullback")
        self.assertNotEqual(result["cycle_id"], "strict-cycle")
        self.assertEqual(result["metrics"]["cycle_id"], result["cycle_id"])
        self.assertEqual(len(result["cycle_id"]), 64)

        stale = dict(v1, state="WAIT", signal_date="", breakout_volume_ratio=99)
        clean = self.analyze(payload, days, v1_result=stale)
        self.assertEqual(clean["stage"], "NONE")
        self.assertNotIn("breakout_volume_ratio", clean["metrics"])

    def test_missing_calendar_or_close_never_confirms(self):
        payload, days = fixture([
            {"close": 10.05, "high": 10.15, "low": 10.0},
            {"close": 9.85, "high": 10.0, "low": 9.75},
            {"close": 9.80, "high": 9.95, "low": 9.70},
            {"close": 9.90, "high": 10.0, "low": 9.80},
            {"close": 9.95, "high": 10.05, "low": 9.85},
            {"close": 10.00, "high": 10.08, "low": 9.90},
            {"close": 10.40, "open_": 10.02, "high": 10.42, "low": 9.98,
             "volume": 1800},
        ])
        missing = self.analyze(payload, days, calendar_evidence=None)
        self.assertEqual(missing["evidence_status"], "data_insufficient")
        self.assertIn("calendar_missing", missing["reason_codes"])

        kwargs = evidence(payload, days, confirmed=False)
        provisional = analyze_daily_weak_to_strong_v2(
            payload, code="000001.SZ", name="fixture", **kwargs)
        self.assertEqual(provisional["confirmation"], "provisional")
        self.assertEqual(provisional["evidence_status"], "data_insufficient")
        self.assertIn("close_unconfirmed", provisional["reason_codes"])

    def test_bad_config_fails_closed(self):
        payload, days = fixture([])
        result = self.analyze(payload, days, config={"range_window": 5})
        self.assertEqual(result["evidence_status"], "data_insufficient")
        self.assertEqual(result["reason_codes"], ["config_invalid"])

        non_finite = self.analyze(payload, days, config={"deep_drawdown": float("nan")})
        self.assertEqual(non_finite["reason_codes"], ["config_invalid"])

        normalized = self.analyze(payload, days, config={"range_window": 20.0})
        self.assertNotIn("config_invalid", normalized["reason_codes"])

    def _breakout_fixture(self):
        return fixture([
            {"close": 10.05, "high": 10.15, "low": 10.0},
            {"close": 9.85, "high": 10.0, "low": 9.75},
            {"close": 9.80, "high": 9.95, "low": 9.70, "volume": 700},
            {"close": 9.90, "high": 10.0, "low": 9.80, "volume": 700},
            {"close": 9.95, "high": 10.05, "low": 9.85, "volume": 700},
            {"close": 10.00, "high": 10.08, "low": 9.90, "volume": 700},
            {"close": 10.40, "open_": 10.02, "high": 10.42, "low": 9.98,
             "volume": 1800},
        ])

    def test_terminal_day_keeps_cycle_identity_for_follow_up(self):
        payload, days = self._breakout_fixture()
        active = self.analyze(payload, days)
        failure = active["structure"]["failure_line"]
        self.assertIsNotNone(failure)
        next_day = days[len(payload["data"])]
        payload["data"].append(bar(next_day, failure - 0.05,
                                   high=failure + 0.02, low=failure - 0.10))
        ended = self.analyze(payload, days)
        self.assertEqual(ended["cycle_id"], active["cycle_id"])
        self.assertEqual(ended["lifecycle_status"], "invalidated")
        self.assertEqual(ended["structure"]["lifecycle_status"], "invalidated")
        self.assertEqual(ended["events"][-1]["lifecycle_status"], "invalidated")

    def test_reclaim_requires_two_complete_closes_below_frozen_trigger(self):
        payload, days = self._breakout_fixture()
        active = self.analyze(payload, days)
        trigger = active["structure"]["trigger_level"]
        cycle = active["cycle_id"]

        def append_close(value, *, strong=False):
            day = days[len(payload["data"])]
            payload["data"].append(bar(
                day, value, open_=value - 0.15 if strong else value,
                high=value + 0.03, low=value - 0.18 if strong else value - 0.03,
                volume=1600))

        append_close(trigger - 0.03)
        once_below = self.analyze(payload, days)
        self.assertIn("lost_level", once_below["risk_flags"])
        append_close(trigger + 0.04, strong=True)
        too_soon = self.analyze(payload, days)
        self.assertEqual(too_soon["cycle_id"], cycle)
        self.assertEqual(too_soon["stage"], "PREPARING")
        self.assertEqual(too_soon["signal_date"], "")

        append_close(trigger - 0.04)
        append_close(trigger - 0.02)
        append_close(trigger + 0.05, strong=True)
        reclaimed = self.analyze(payload, days)
        self.assertEqual(reclaimed["cycle_id"], cycle)
        self.assertEqual(reclaimed["signal_date"], reclaimed["as_of_date"])
        self.assertEqual(reclaimed["events"][-1]["event_type"], "reclaim")
        self.assertEqual(reclaimed["structure"]["trigger_level"], trigger)


if __name__ == "__main__":
    unittest.main()
