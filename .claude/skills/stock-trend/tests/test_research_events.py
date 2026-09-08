"""Shared research-event identity and date-weighted alpha tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.research_events import assign_research_events, summarize_daily_alpha


class T(unittest.TestCase):
    def test_overlapping_intervals_use_first_event_and_market_isolated(self):
        records = [
            {"record_id": "later", "market": "SH", "code": "600000",
             "entry_date": "2026-08-25", "exit_date": "2026-09-10"},
            {"record_id": "first", "market": "SH", "code": "600000",
             "entry_date": "2026-08-21", "exit_date": "2026-08-25"},
            {"record_id": "hk", "market": "HK", "code": "600000",
             "entry_date": "2026-08-25", "exit_date": "2026-09-10"},
        ]
        result = assign_research_events(records, 20)
        self.assertEqual({row["record_id"] for row in result["events"]}, {"first", "hk"})
        self.assertEqual(result["record_to_event"]["later"], "20:first")
        self.assertNotEqual(result["record_to_event"]["hk"], result["record_to_event"]["first"])

    def test_missing_or_inverted_endpoints_are_invalid(self):
        result = assign_research_events([
            {"record_id": "missing", "market": "SH", "code": "A",
             "entry_date": "2026-01-01"},
            {"record_id": "inverted", "market": "SH", "code": "B",
             "entry_date": "2026-01-03", "exit_date": "2026-01-02"},
        ], 20)
        self.assertEqual(result["events"], [])
        self.assertEqual(len(result["invalid"]), 2)

    def test_non_trading_endpoints_are_invalid_with_weekday_or_frozen_calendar(self):
        weekend = {"record_id": "weekend", "market": "SH", "code": "A",
                   "entry_date": "2026-08-22", "exit_date": "2026-08-24"}
        holiday = {"record_id": "holiday", "market": "SH", "code": "B",
                   "entry_date": "2026-10-01", "exit_date": "2026-10-08"}
        result = assign_research_events([weekend], 20)
        self.assertEqual(result["events"], [])
        result = assign_research_events([holiday], 20, market_sessions=["2026-09-30", "2026-10-09"])
        self.assertEqual(result["events"], [])

    def test_daily_equal_weighting_and_invalid_values(self):
        rows = ([{"recommendation_date": "2026-08-20", "hs300_alpha": .10,
                  "evaluation_status": "complete"}] * 10 +
                [{"recommendation_date": "2026-08-21", "hs300_alpha": -.10,
                  "evaluation_status": "complete"},
                 {"recommendation_date": "2026-08-22", "hs300_alpha": float("nan"),
                  "evaluation_status": "complete"},
                 {"recommendation_date": "2026-08-23", "hs300_alpha": True,
                  "evaluation_status": "complete"}])
        result = summarize_daily_alpha(rows)
        self.assertAlmostEqual(result["mean_alpha"], 0.0)
        self.assertEqual(result["mature_dates"], 2)
        self.assertEqual(result["missing_records"], 2)


def run_research_event_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
