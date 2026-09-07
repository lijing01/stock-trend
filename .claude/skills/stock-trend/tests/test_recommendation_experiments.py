"""Offline contracts for P3 rolling validation and strategy shadowing."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtesting.recommendation_experiments import default_experiment, run_walk_forward, save_experiment
from core.evolution_registry import register_experiment, transition


def snapshot(day, code, quality, level):
    return {"content": {"recommendation_date": day, "snapshot_type": "formal",
        "official_snapshot": {"link_status": "linked"}, "parameter_summary": {"top": 1},
        "records": [{"code": code, "basis_date": day,
          "scores": {"quality_adjusted_score": quality, "buy_point_level": level},
          "candidate": {"quality_adjusted_score": quality, "buy_point_level": level,
                        "data_quality": {"eligible": True}, "sector_actionable": True,
                        "score_eligible": True}}]}}


def outcome(day, code, alpha):
    return {"recommendation_date": day, "code": code,
            "windows": {"20": {"status": "complete", "hs300_alpha": alpha, "mae": -.03,
                                 "exit_date": day}}}


class T(unittest.TestCase):
    def test_requires_three_time_ordered_purged_blocks(self):
        result = run_walk_forward([snapshot("2026-01-02", "1", 80, "strict_level_1")], [])
        self.assertEqual(result["content"]["status"], "continue_accumulating")
        self.assertIn("purged", result["content"]["reason"])

    def test_no_difference_is_visible_not_a_promotion(self):
        snapshots, outcomes = [], []
        # 43 dates lets the fixed 20-session purge leave three OOS blocks.
        for number in range(43):
            day = f"2026-01-{number + 1:02d}"
            snapshots.append(snapshot(day, f"{number:06d}", 80, "strict_level_2"))
            outcomes.append(outcome(day, f"{number:06d}", .02))
        result = run_walk_forward(snapshots, outcomes)
        self.assertEqual(result["content"]["status"], "validated")
        self.assertTrue(all(row["delta"] == 0 for row in result["content"]["paired_dates"]))
        self.assertFalse(result["content"]["promotion"]["eligible"])

    def test_registry_is_immutable_and_transition_guarded(self):
        with tempfile.TemporaryDirectory() as root:
            saved = register_experiment(default_experiment(), root)
            again = register_experiment(default_experiment(), root)
            self.assertEqual((saved["status"], again["status"]), ("created", "unchanged"))
        record = {"experiment_id": "x", "content": {"state": "draft", "history": []}}
        self.assertEqual(transition(record, "validated", {"test": True})["content"]["state"], "validated")
        with self.assertRaises(ValueError): transition(record, "active", {"test": True})

    def test_result_persistence_is_idempotent(self):
        result = run_walk_forward([], [])
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(save_experiment(result, root)["status"], "created")
            self.assertEqual(save_experiment(result, root)["status"], "unchanged")


def run_recommendation_experiment_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
