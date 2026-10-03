#!/usr/bin/env python3
"""Offline tests for the U.S. market window and calculation contract."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.us_market_summary import build_summary, load_watchlist, resolve_window


def tiny_config():
    return {
        "config_sha256": "fixture",
        "indices": [{"symbol": "SPY", "name": "S&P", "sector": "大盘"}],
        "sectors": [{"symbol": "XLK", "name": "科技", "sector": "科技"}],
        "stocks": [{"symbol": "AAPL", "name": "苹果", "sector": "科技"}],
    }


def payload(values):
    return {
        "provider": "fixture",
        "fetched_at": "2026-10-03T08:00:00Z",
        "rows": {
            symbol: [
                {"date": day, "close": value, "adj_close": value,
                 "dividends": 0, "stock_splits": 0}
                for day, value in rows
            ]
            for symbol, rows in values.items()
        },
        "errors": {},
    }


class TestResolveWindow(unittest.TestCase):
    def test_china_holiday_window_has_three_completed_us_sessions(self):
        result = resolve_window("2026-09-30", "2026-10-03T16:44:58+08:00")
        self.assertEqual(result["baseline_session"], "2026-09-29")
        self.assertEqual(result["expected_end_session"], "2026-10-02")
        self.assertEqual(result["sessions"], ["2026-09-30", "2026-10-01", "2026-10-02"])
        self.assertEqual(result["status"], "complete")

    def test_intraday_session_is_not_included_until_close(self):
        before = resolve_window("2026-09-30", "2026-09-30T15:59:59-04:00")
        after = resolve_window("2026-09-30", "2026-09-30T16:00:00-04:00")
        self.assertEqual(before["sessions"], [])
        self.assertEqual(before["status"], "empty")
        self.assertEqual(after["sessions"], ["2026-09-30"])

    def test_dst_and_early_close_use_exchange_local_time(self):
        summer = resolve_window("2026-07-01", "2026-07-01T20:00:00Z")
        winter = resolve_window("2026-11-04", "2026-11-04T21:00:00Z")
        early_before = resolve_window("2026-11-27", "2026-11-27T12:59:59-05:00")
        early_after = resolve_window("2026-11-27", "2026-11-27T13:00:00-05:00")
        self.assertEqual(summer["sessions"], ["2026-07-01"])
        self.assertEqual(winter["sessions"], ["2026-11-04"])
        self.assertEqual(early_before["sessions"], [])
        self.assertEqual(early_after["sessions"], ["2026-11-27"])

    def test_requires_aware_cutoff_and_fails_outside_calendar_coverage(self):
        with self.assertRaisesRegex(ValueError, "timezone_aware"):
            resolve_window("2026-09-30", datetime(2026, 10, 3, 12))
        result = resolve_window("2027-01-04", "2027-01-05T12:00:00-05:00")
        self.assertEqual(result["status"], "calendar_unavailable")

    def test_cutoff_before_anchor_is_invalid_and_calendar_errors_are_contained(self):
        result = resolve_window("2026-10-03", "2026-10-02T12:00:00+08:00")
        self.assertEqual(result["status"], "invalid_window")
        self.assertEqual(result["reason"], "as_of_before_anchor")
        self.assertIsNone(result["baseline_session"])

        with tempfile.TemporaryDirectory() as directory:
            bad_calendar = Path(directory) / "calendar.json"
            bad_calendar.write_text(json.dumps({
                "version": "bad",
                "timezone": "Not/AZone",
                "regular_close": "bad",
                "coverage": {"start": "2026-01-01", "end": "2026-12-31"},
                "holidays": [],
                "early_closes": {},
            }), encoding="utf-8")
            result = resolve_window(
                "2026-10-03", "2026-10-04T12:00:00+08:00", bad_calendar)
        self.assertEqual(result["status"], "calendar_unavailable")


class TestConfig(unittest.TestCase):
    def test_versioned_watchlist_has_expected_complete_groups(self):
        config = load_watchlist()
        self.assertEqual([len(config[key]) for key in ("indices", "sectors", "stocks")], [4, 11, 17])
        self.assertEqual(len(config["config_sha256"]), 64)
        self.assertEqual(config["indices"][0]["symbol"], "SPY")


class TestBuildSummary(unittest.TestCase):
    def setUp(self):
        self.window = resolve_window("2026-09-30", "2026-10-03T16:44:58+08:00")

    def test_exact_endpoints_compounding_daily_and_relative_spy(self):
        data = payload({
            "SPY": [("2026-09-29", 100), ("2026-09-30", 110),
                    ("2026-10-01", 99), ("2026-10-02", 104)],
            "XLK": [("2026-09-29", 100), ("2026-09-30", 105),
                    ("2026-10-01", 105), ("2026-10-02", 110)],
            "AAPL": [("2026-09-29", 50), ("2026-09-30", 55),
                     ("2026-10-01", 49.5), ("2026-10-02", 49.5)],
        })
        result = build_summary(self.window, tiny_config(), data)
        spy = result["groups"]["indices"][0]
        xlk = result["groups"]["sectors"][0]
        apple = result["groups"]["stocks"][0]
        self.assertEqual(spy["interval_pct"], 4.0)
        self.assertAlmostEqual(spy["daily_pct"], (104 / 99 - 1) * 100, places=6)
        self.assertEqual(xlk["interval_pct"], 10.0)
        self.assertEqual(xlk["relative_spy_pp"], 6.0)
        self.assertEqual(apple["interval_pct"], -1.0)
        self.assertEqual(result["data_quality"], "complete")

    def test_missing_interior_is_partial_but_exact_endpoints_still_compute(self):
        data = payload({
            "SPY": [("2026-09-29", 100), ("2026-09-30", 101),
                    ("2026-10-01", 102), ("2026-10-02", 103)],
            "XLK": [("2026-09-29", 100), ("2026-09-30", 101),
                    ("2026-10-02", 106)],
            "AAPL": [("2026-09-29", 100), ("2026-09-30", 101),
                     ("2026-10-01", 102), ("2026-10-02", 103)],
        })
        row = build_summary(self.window, tiny_config(), data)["groups"]["sectors"][0]
        self.assertEqual(row["interval_pct"], 6.0)
        self.assertEqual(row["status"], "partial")
        self.assertIsNone(row["daily"][-1]["change_pct"])

    def test_missing_endpoint_raw_close_zero_nan_and_duplicate_are_not_rankable(self):
        base = payload({
            "SPY": [("2026-09-29", 100), ("2026-09-30", 101),
                    ("2026-10-01", 102), ("2026-10-02", 103)],
            "XLK": [("2026-09-29", 100), ("2026-10-02", 105)],
            "AAPL": [("2026-09-29", 100), ("2026-10-02", 105)],
        })
        base["rows"]["XLK"][-1]["adj_close"] = None
        base["rows"]["XLK"][-1]["close"] = 999
        base["rows"]["AAPL"] += [copy.deepcopy(base["rows"]["AAPL"][-1])]
        result = build_summary(self.window, tiny_config(), base)
        xlk = result["groups"]["sectors"][0]
        apple = result["groups"]["stocks"][0]
        self.assertIsNone(xlk["interval_pct"])
        self.assertEqual(xlk["status"], "unavailable")
        self.assertIsNone(apple["interval_pct"])
        self.assertEqual(apple["status"], "invalid")
        self.assertEqual(result["data_quality"], "partial")

    def test_empty_window_does_not_attempt_calculation(self):
        window = resolve_window("2026-09-30", "2026-09-30T15:00:00+08:00")
        result = build_summary(window, tiny_config(), payload({}))
        self.assertEqual(result["data_quality"], "empty")
        self.assertEqual(result["groups"]["indices"], [])


if __name__ == "__main__":
    unittest.main()
