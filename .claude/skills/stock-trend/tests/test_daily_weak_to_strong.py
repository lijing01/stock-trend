#!/usr/bin/env python3
"""Tests for the pure daily weak-to-strong detector."""

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.daily_weak_to_strong import analyze_daily_weak_to_strong


def trading_days(count, start=date(2026, 1, 5)):
    values = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def row(day, close, high=None, low=None, volume=900, open_=None):
    high = close + 0.10 if high is None else high
    low = close - 0.10 if low is None else low
    return {
        "trade_date": day.replace("-", ""),
        "open": close if open_ is None else open_,
        "high": high,
        "low": low,
        "close": close,
        "vol": volume,
    }


def payload_with_pattern(adjustments=3, breakout=True, breakout_volume=850,
                         breakout_close=11.85):
    days = trading_days(30)
    rows = []
    # Alternating closes establish a known left boundary without an active run.
    for index in range(20):
        close = 10.0 + (0.03 if index % 2 else 0)
        if index == 19:
            close = 10.0  # End the preceding run before the reference upswing.
        rows.append(row(days[index], close, volume=700))
    rows.extend([
        row(days[20], 11.40, high=11.50, low=10.90, volume=1000),
        row(days[21], 11.80, high=12.00, low=11.30, volume=1000),
        row(days[22], 11.30, high=11.70, low=11.10, volume=650),
    ])
    if adjustments >= 2:
        rows.append(row(days[23], 11.10, high=11.40, low=10.90, volume=520))
    if adjustments >= 3:
        rows.append(row(days[24], 11.20, high=11.35, low=11.00, volume=400))
    if adjustments >= 4:
        rows.append(row(days[25], 11.18, high=11.30, low=11.02, volume=350))
    if breakout:
        next_day = days[22 + adjustments]
        rows.append(row(next_day, breakout_close, high=11.95, low=11.20,
                        volume=breakout_volume, open_=11.25))
    return {
        "meta": {"data_source": "fixture", "adj": "qfq"},
        "data": rows,
    }, days


def evidence(payload, days, confirmed=True):
    last_date = payload["data"][-1]["trade_date"]
    last_date = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    return {
        "as_of_date": last_date,
        "close_evidence": {"confirmed": confirmed, "source": "fixture",
                           "provisional": not confirmed},
        "calendar_evidence": {
            "source": "fixture-calendar",
            "identity": "calendar-v1",
            "coverage_start": days[0],
            "coverage_end": days[-1],
            "trading_dates": days,
        },
    }


class DailyWeakToStrongTests(unittest.TestCase):
    def analyze(self, payload, days, confirmed=True, as_of_date=None,
                calendar_evidence=None):
        kwargs = evidence(payload, days, confirmed)
        if as_of_date is not None:
            kwargs["as_of_date"] = as_of_date
        if calendar_evidence is not None:
            kwargs["calendar_evidence"] = calendar_evidence
        return analyze_daily_weak_to_strong(payload, **kwargs)

    def test_reference_pattern_confirms_without_future_data(self):
        payload, days = payload_with_pattern()
        result = self.analyze(payload, days)

        self.assertEqual(result["state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(result["evidence_status"], "ready")
        self.assertAlmostEqual(result["H"], 11.70)
        self.assertAlmostEqual(result["L"], 10.90)
        self.assertAlmostEqual(result["Va"], (650 + 520 + 400) / 3)
        self.assertAlmostEqual(result["va_v0_ratio"], ((650 + 520 + 400) / 3) / 1000)
        self.assertAlmostEqual(result["breakout_volume_ratio"], 850 / ((650 + 520 + 400) / 3))
        self.assertAlmostEqual(result["close_strength"], (11.85 - 11.20) / (11.95 - 11.20))

        future = copy.deepcopy(payload)
        future["data"].append(row(days[27], 50, high=51, low=49, volume=9999))
        replayed = self.analyze(future, days, as_of_date=result["as_of_date"])
        for key in ("state", "H", "L", "Va", "breakout_volume_ratio",
                    "cycle_id", "signal_date"):
            self.assertEqual(replayed[key], result[key])

    def test_supply_dry_is_a_visible_state_before_breakout(self):
        payload, days = payload_with_pattern(adjustments=3, breakout=False)
        result = self.analyze(payload, days)

        self.assertEqual(result["state"], "SUPPLY_DRY")
        self.assertEqual(result["adjustment_days"], 3)
        self.assertLessEqual(result["va_v0_ratio"], 0.8)
        self.assertLessEqual(result["last_va_ratio"], 0.9)

    def test_d5_is_last_breakout_opportunity(self):
        d5_payload, days = payload_with_pattern(adjustments=4, breakout=True)
        d5 = self.analyze(d5_payload, days)
        self.assertEqual(d5["state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(d5["adjustment_days"], 4)

        expired, days = payload_with_pattern(adjustments=4, breakout=False)
        expired["data"].append(row(days[26], 11.15, high=11.25, low=11.0,
                                   volume=300))
        d5_no_break = self.analyze(expired, days)
        self.assertEqual(d5_no_break["state"], "INVALID")
        self.assertIn("pullback_timeout", d5_no_break["reason_codes"])

        expired["data"].append(row(days[27], 11.90, high=12.0, low=11.2,
                                   volume=900))
        d6 = self.analyze(expired, days)
        self.assertNotEqual(d6["state"], "BREAKOUT_CONFIRMED")

    def test_unqualified_price_breakout_invalidates_cycle(self):
        payload, days = payload_with_pattern(breakout_volume=500)
        result = self.analyze(payload, days)

        self.assertEqual(result["state"], "INVALID")
        self.assertIn("breakout_unqualified", result["reason_codes"])
        self.assertFalse(result["checks"]["breakout_volume"]["passed"])

    def test_one_day_adjustment_exempts_last_volume_and_d2_confirms(self):
        payload, days = payload_with_pattern(adjustments=1)
        result = self.analyze(payload, days)
        self.assertEqual(result["state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(result["adjustment_days"], 1)
        self.assertEqual(result["checks"]["last_volume"]["status"], "not_applicable")

    def test_strict_price_and_inclusive_volume_boundaries(self):
        payload, days = payload_with_pattern(adjustments=1)
        payload["data"][-1]["close"] = 11.70  # Equal to H is not a breakout.
        payload["data"][-1]["open"] = 11.25
        equal = self.analyze(payload, days)
        self.assertNotEqual(equal["state"], "BREAKOUT_CONFIRMED")
        payload["data"][-1]["close"] = 11.85
        payload["data"][-1]["vol"] = 650 * 1.2
        at_boundary = self.analyze(payload, days)
        self.assertEqual(at_boundary["state"], "BREAKOUT_CONFIRMED")
        payload["data"][-1]["vol"] = 650 * 1.199
        below = self.analyze(payload, days)
        self.assertEqual(below["state"], "INVALID")

    def test_flat_breakout_and_bad_volume_never_confirm(self):
        payload, days = payload_with_pattern()
        payload["data"][-1].update(open=11.85, high=11.85, low=11.85, close=11.85)
        self.assertEqual(self.analyze(payload, days)["state"], "INVALID")
        payload["data"][-1]["vol"] = 0
        self.assertEqual(self.analyze(payload, days)["evidence_status"], "data_insufficient")

    def test_stale_unconfirmed_data_is_not_a_provisional_signal(self):
        payload, days = payload_with_pattern()
        kwargs = evidence(payload, days, confirmed=False)
        kwargs["close_evidence"].pop("provisional")
        result = analyze_daily_weak_to_strong(payload, **kwargs)
        self.assertNotEqual(result["state"], "PROVISIONAL_BREAKOUT")
        self.assertEqual(result["evidence_status"], "data_insufficient")

    def test_missing_window_trading_day_blocks_confirmation(self):
        payload, days = payload_with_pattern()
        payload["data"].pop(-3)
        result = self.analyze(payload, days)
        self.assertEqual(result["evidence_status"], "data_insufficient")
        self.assertIn("calendar_gap_or_suspension", result["reason_codes"])

    def test_inclusive_signal_thresholds_and_just_over_limits(self):
        def check(payload, days, mutator, expected_key):
            at_boundary = copy.deepcopy(payload)
            mutator(at_boundary, False)
            result = self.analyze(at_boundary, days)
            self.assertEqual(result["state"], "BREAKOUT_CONFIRMED", (expected_key, result))
            self.assertTrue(result["checks"][expected_key]["passed"])
            outside = copy.deepcopy(payload)
            mutator(outside, True)
            self.assertNotEqual(self.analyze(outside, days)["state"], "BREAKOUT_CONFIRMED", expected_key)

        payload, days = payload_with_pattern(adjustments=1)
        def drawdown(p, outside):
            p["data"][-2]["low"] = 12.0 * (0.88 - (0.0001 if outside else 0))
        check(payload, days, drawdown, "drawdown")

        def adjustment_volume(p, outside):
            p["data"][-2]["vol"] = 800.1 if outside else 800
            p["data"][-1]["vol"] = 1000
        check(payload, days, adjustment_volume, "adjustment_volume")

        def strength(p, outside):
            p["data"][-1].update(open=11.25, high=12.0, low=11.0,
                                 close=11.75 - (0.001 if outside else 0))
        check(payload, days, strength, "close_strength")

        def amplitude_limit(p, outside):
            p["data"][-1].update(open=11.25, high=12.5, low=11.2,
                                 close=11.7 * (1.05 + (0.0001 if outside else 0)))
        check(payload, days, amplitude_limit, "breakout_ratio")

        payload2, days2 = payload_with_pattern(adjustments=2)
        def last_volume(p, outside):
            p["data"][-2]["vol"] = 650 * 0.9 / 1.1 + (1 if outside else 0)
        check(payload2, days2, last_volume, "last_volume")

    def test_ma10_support_boundary(self):
        payload, days = payload_with_pattern(adjustments=1)
        for index in range(20):
            value = 11.2 + (0.03 if index % 2 else 0)
            if index == 19:
                value = 11.2
            payload["data"][index] = row(days[index], value, volume=700)
        previous = sum(x["close"] for x in payload["data"][13:22])
        threshold_close = 0.98 * previous / (10 - 0.98)
        for offset, expected in ((0, "BREAKOUT_CONFIRMED"), (-0.001, "WAIT")):
            fixture = copy.deepcopy(payload)
            close = threshold_close + offset
            fixture["data"][22].update(open=close, close=close, high=11.70, low=close - 0.05)
            result = self.analyze(fixture, days)
            self.assertEqual(result["state"], expected)
            if offset:
                self.assertTrue(any("structure_broken" in event["reason_codes"]
                                    for event in result["events"]))

    def test_intraday_match_is_never_formally_confirmed(self):
        payload, days = payload_with_pattern()
        result = self.analyze(payload, days, confirmed=False)

        self.assertEqual(result["state"], "PROVISIONAL_BREAKOUT")
        self.assertEqual(result["confirmation"], "provisional")
        self.assertTrue(result["provisional_match"])
        self.assertIn("close_unconfirmed", result["reason_codes"])

    def test_missing_calendar_downgrades_formal_breakout(self):
        payload, days = payload_with_pattern()
        result = self.analyze(payload, days, calendar_evidence={})

        self.assertEqual(result["state"], "UNVERIFIED_BREAKOUT")
        self.assertEqual(result["confirmation"], "evidence_incomplete")
        self.assertEqual(result["evidence_status"], "data_insufficient")
        self.assertIn("calendar_missing", result["reason_codes"])

    def test_conflicting_duplicate_and_unknown_basis_are_insufficient(self):
        payload, days = payload_with_pattern()
        conflict = copy.deepcopy(payload["data"][-1])
        conflict["close"] = conflict["close"] - 0.2
        payload["data"].append(conflict)
        payload["meta"].pop("adj")

        result = self.analyze(payload, days)

        self.assertEqual(result["evidence_status"], "data_insufficient")
        self.assertIn("kline_duplicate_conflict", result["reason_codes"])
        self.assertIn("basis_unknown", result["reason_codes"])

    def test_left_truncated_rise_cannot_create_cycle(self):
        days = trading_days(25)
        rows = [row(day, 10 + index * 0.1, volume=1000)
                for index, day in enumerate(days)]
        payload = {"meta": {"adj": "qfq", "data_source": "fixture"},
                   "data": rows}

        result = self.analyze(payload, days)

        self.assertEqual(result["state"], "WAIT")
        self.assertEqual(result["d0_date"], "")
        self.assertEqual(result["evidence_status"], "data_insufficient")
        self.assertIn("history_left_truncated", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
