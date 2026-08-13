#!/usr/bin/env python3
"""Tests for candidate recommendation data quality."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.recommendation_quality import assess_candidate_data, latest_data_date


def payload(rows, quality="good", source="fixture", fetched_at="20260806-160000"):
    return {
        "meta": {"data_source": source, "fetch_time": fetched_at},
        "summary": {"data_quality": quality},
        "data": rows,
    }


class TestRecommendationQuality(unittest.TestCase):
    def test_malformed_nested_payloads_are_non_actionable_not_exceptions(self):
        malformed = {"meta": [], "summary": "bad", "data": "bad"}
        result = assess_candidate_data(
            kline=malformed,
            capital=malformed,
            fundamental=malformed,
            as_of_date="2026-08-13",
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["coverage"], 0)
        self.assertIn("kline_stale", result["reasons"])

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

    def test_dimension_metadata_is_normalized(self):
        result = assess_candidate_data(
            kline=payload(
                [{"trade_date": "20260806"}],
                source="eastmoney",
                fetched_at="20260806-153100",
            ),
            capital=payload([{"date": "20260806"}]),
            fundamental=None,
            as_of_date="2026-08-06",
        )
        kline = result["dimensions"]["kline"]
        self.assertEqual(kline["source"], "eastmoney")
        self.assertEqual(kline["fetched_at"], "20260806-153100")
        self.assertEqual(kline["stale_reason"], "")

    def test_stale_capital_has_explicit_reason_and_lower_freshness(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260805"}]),
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        self.assertEqual(
            result["dimensions"]["capital"]["stale_reason"],
            "capital_stale",
        )
        self.assertEqual(result["freshness_factor"], 0.5)

    def test_stale_fundamental_fetch_is_not_counted_as_coverage(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=None,
            fundamental=payload(
                [], quality="good", fetched_at="20260805-160000"),
            as_of_date="2026-08-06",
        )
        fundamental = result["dimensions"]["fundamental"]
        self.assertFalse(fundamental["fresh"])
        self.assertEqual(fundamental["stale_reason"], "fundamental_stale")
        self.assertEqual(result["coverage"], 0.55)
        self.assertFalse(result["eligible"])

    def test_returned_error_dimension_blocks_eligibility(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=payload([], quality="error"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertIn("fundamental_error", result["reasons"])

    def test_real_capital_error_shape_blocks_eligibility(self):
        capital_error = {
            "meta": {
                "ts_code": "600519.SH",
                "data_source": "error",
                "error": "资金流向获取失败",
            },
            "data": [],
        }
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=capital_error,
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertIn("capital_error", result["reasons"])

    def test_nominally_successful_empty_capital_is_an_error(self):
        empty_capital = {
            "meta": {"data_source": "eastmoney", "record_count": 0},
            "data": [],
        }
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=empty_capital,
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        capital = result["dimensions"]["capital"]
        self.assertFalse(capital["available"])
        self.assertEqual(capital["stale_reason"], "capital_error")
        self.assertIn("capital_error", result["reasons"])
        self.assertNotIn("capital_date_missing", result["reasons"])

    def test_current_dated_capital_restores_full_quality(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260806"}], source="eastmoney"),
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        self.assertTrue(result["dimensions"]["capital"]["available"])
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["freshness_factor"], 1.0)
        self.assertEqual(result["confidence"], 1.0)

    def test_quality_factors_and_confidence_are_exposed(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=None,
            as_of_date="2026-08-06",
        )
        self.assertEqual(result["coverage_factor"], 0.8)
        self.assertEqual(result["freshness_factor"], 1.0)
        self.assertEqual(result["confidence"], 0.8)


def run_recommendation_quality_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationQuality)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_recommendation_quality_tests()
    raise SystemExit(1 if failed else 0)
