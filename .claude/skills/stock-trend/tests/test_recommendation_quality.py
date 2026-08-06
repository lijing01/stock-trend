#!/usr/bin/env python3
"""Tests for candidate recommendation data quality."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.recommendation_quality import assess_candidate_data, latest_data_date


def payload(rows, quality="good"):
    return {"summary": {"data_quality": quality}, "data": rows}


class TestRecommendationQuality(unittest.TestCase):
    def test_latest_date_accepts_trade_date_and_date(self):
        data = {"data": [{"trade_date": "20260805"}, {"date": "2026-08-06"}]}
        self.assertEqual(latest_data_date(data), "2026-08-06")

    def test_fresh_kline_and_one_secondary_dimension_are_eligible(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=None,
            as_of_date="2026-08-06",
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["coverage"], 0.8)

    def test_stale_kline_is_never_eligible(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260805"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertIn("kline_stale", result["reasons"])

    def test_missing_secondary_dimensions_fail_coverage(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=None,
            fundamental=None,
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["coverage"], 0.55)
        self.assertIn("coverage_below_70pct", result["reasons"])

    def test_fundamental_error_is_missing(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=None,
            fundamental=payload([], quality="error"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["dimensions"]["fundamental"]["available"])


def run_recommendation_quality_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationQuality)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_recommendation_quality_tests()
    raise SystemExit(1 if failed else 0)
