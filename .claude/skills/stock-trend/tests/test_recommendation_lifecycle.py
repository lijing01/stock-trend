"""End-to-end proof for snapshot persistence and staged attribution maturity."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.recommendation_snapshot import build_snapshot, save_official_snapshot
from analysis.recommendation_attribution import evaluate_recommendation


def _source():
    candidate = {
        "code": "600000",
        "trade_plan_status": "complete",
        "trade_plan": {
            "action": "buy",
            "entry": {"low": 10.0, "high": 10.5},
            "stop_loss": {"price": 9.0},
            "targets": {"conservative": 12.0, "primary": 14.0,
                        "aggressive": 16.0},
        },
    }
    return {
        "recommendation_date": "2026-08-20",
        "generated_at": "2026-08-20T15:00:00+08:00",
        "snapshot_type": "formal",
        "model_version": "daily-candidates/v1",
        "policy": {"mode": "actionable", "provisional": False},
        "market_regime": {"data_date": "2026-08-20"},
        "sectors": [], "candidates": [candidate],
        "buckets": {"actionable": [candidate], "waiting_trigger": [],
                    "next_day_confirmation": [], "observation": []},
        "scan_status": "complete",
    }


class TestRecommendationLifecycle(unittest.TestCase):
    def test_snapshot_is_immutable_while_windows_mature(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = build_snapshot(_source())
            first = save_official_snapshot(snapshot, root=root)
            before = first.path.read_bytes()
            dates = [f"2026-08-{day:02d}" for day in range(21, 29)]
            rows = [{"date": d, "open": 10.2, "high": 11.0,
                     "low": 10.0, "close": 10.5, "vol": 100}
                    for d in dates]
            recommendation = _source()["buckets"]["actionable"][0]
            recommendation["recommendation_date"] = "2026-08-20"
            at4 = evaluate_recommendation(
                {"recommendation_date": "2026-08-20", **recommendation},
                "2026-08-24", dates, rows, windows=(5,))
            self.assertEqual(at4["windows"]["5"]["status"], "pending")
            at5 = evaluate_recommendation(
                {"recommendation_date": "2026-08-20", **recommendation},
                "2026-08-25", dates, rows, windows=(5,))
            self.assertEqual(at5["windows"]["5"]["status"], "complete")
            self.assertEqual(before, first.path.read_bytes())

    def test_provisional_source_does_not_create_official_file(self):
        source = _source()
        source["snapshot_type"] = "provisional"
        source["policy"]["provisional"] = True
        with tempfile.TemporaryDirectory() as root:
            from core.recommendation_snapshot import save_snapshot_if_official
            result = save_snapshot_if_official(source, root=root)
            self.assertEqual(result.status, "skipped_provisional")
            self.assertEqual(list(Path(root).glob("*.json")), [])


def run_recommendation_lifecycle_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationLifecycle)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
