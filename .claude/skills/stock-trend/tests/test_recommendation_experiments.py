"""Offline contracts for P3 rolling validation and strategy shadowing."""
import sys
import tempfile
import unittest
import copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtesting.recommendation_experiments import (
    _bootstrap, _replay_selection, default_experiment, run_walk_forward,
    save_experiment,
)
from core.evolution_registry import register_experiment, transition
from scans.daily_candidates import classify_candidates, select_candidate_pool


def snapshot(day, code, quality, level):
    return {"content": {"recommendation_date": day, "snapshot_type": "formal",
        "official_snapshot": {"link_status": "linked"}, "parameter_summary": {"top": 1, "min_score": 50},
        "records": [{"code": code, "basis_date": day,
          "record_id": f"{day}:SH:{code}",
          "scores": {"composite_score": quality, "quality_adjusted_score": quality, "buy_point_level": level},
          "preselection": {"composite_score": quality, "quality_adjusted_score": quality,
                            "data_quality_eligible": True, "sector_actionable": True,
                            "score_eligible": True, "qualification_status": "eligible"},
          "candidate": {"composite_score": quality, "quality_adjusted_score": quality, "buy_point_level": level,
                        "data_quality": {"eligible": True}, "sector_actionable": True,
                        "score_eligible": True}}]}}


def outcome(day, code, alpha):
    return {"recommendation_date": day, "code": code,
            "windows": {"20": {"status": "complete", "hs300_alpha": alpha, "mae": -.03,
                                 "exit_date": day}}}


def research_record(code, quality=80, level="strict_level_1", eligible=True,
                    sector_actionable=True):
    candidate = {
        "code": code,
        "composite_score": quality,
        "quality_adjusted_score": quality,
        "buy_point_level": level,
        "data_quality": {"eligible": eligible},
        "sector_actionable": sector_actionable,
        "score_eligible": eligible,
    }
    return {
        "record_id": f"2026-08-20:SH:{code}",
        "code": code,
        "candidate": copy.deepcopy(candidate),
        "scores": {
            "composite_score": quality,
            "quality_adjusted_score": quality,
            "buy_point_level": level,
        },
        "preselection": {
            "composite_score": quality,
            "quality_adjusted_score": quality,
            "data_quality_eligible": eligible,
            "sector_actionable": sector_actionable,
            "score_eligible": eligible,
            "qualification_status": "eligible" if eligible else "ineligible",
        },
    }


class T(unittest.TestCase):
    def test_requires_three_time_ordered_purged_blocks(self):
        result = run_walk_forward([snapshot("2026-01-02", "1", 80, "strict_level_1")], [])
        self.assertEqual(result["content"]["status"], "continue_accumulating")
        self.assertIn("partitions", result["content"]["reason"])

    def test_no_difference_is_visible_not_a_promotion(self):
        snapshots, outcomes = [], []
        # 43 dates lets the fixed 20-session purge leave three OOS blocks.
        for number in range(43):
            day = f"2026-01-{number + 1:02d}"
            snapshots.append(snapshot(day, f"{number:06d}", 80, "strict_level_2"))
            outcomes.append(outcome(day, f"{number:06d}", .02))
        result = run_walk_forward(snapshots, outcomes)
        self.assertEqual(result["content"]["status"], "continue_accumulating")
        self.assertIn("partitions", result["content"]["reason"])
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

    def test_replay_selection_matches_production_order_and_buckets(self):
        policy = {"mode": "actionable", "max_recommendations": 2}
        records = [
            research_record("A", quality=80, level="strict_level_1"),
            research_record("B", quality=80, level="strict_level_2"),
            research_record("C", quality=79, level="strict_level_3", eligible=False),
        ]
        frozen = [copy.deepcopy(row["candidate"]) for row in records]
        expected = select_candidate_pool(
            frozen, 2, 50, policy=copy.deepcopy(policy),
            priority_bonuses={"strict_level_1": 1, "strict_level_2": 3,
                              "strict_level_3": 2},
        )
        expected_buckets = classify_candidates(expected, copy.deepcopy(policy))
        replay = _replay_selection(
            records,
            {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2},
            2,
            50,
            policy,
        )
        self.assertEqual(
            [row["code"] for row in replay["selected"]],
            [row["code"] for row in expected],
        )
        self.assertEqual(
            {name: [row["code"] for row in rows]
             for name, rows in replay["buckets"].items()},
            {name: [row["code"] for row in rows]
             for name, rows in expected_buckets.items()},
        )
        self.assertEqual(replay["status"], "ok")

    def test_replay_rejects_missing_frozen_qualification_instead_of_skipping(self):
        broken = research_record("A")
        broken.pop("preselection")
        replay = _replay_selection([broken], {}, 1, 50,
                                   {"mode": "actionable", "max_recommendations": 1})
        self.assertEqual(replay["status"], "rejected")
        self.assertIn("replay_input_missing_preselection", replay["reasons"])

    def test_moving_bootstrap_records_contiguous_session_blocks(self):
        result = _bootstrap(
            list(range(12)), seed=7, draws=20, block_length=3,
            dates=[f"2026-08-{number:02d}" for number in range(1, 13)],
        )
        self.assertEqual(result["method"], "moving_trading_session_block_bootstrap")
        self.assertEqual(result["block_length"], 3)
        self.assertGreaterEqual(result["valid_block_count"], 2)
        for block in result["valid_blocks"]:
            self.assertEqual(block, list(range(block[0], block[0] + 3)))

    def test_missing_complete_partitions_cannot_validate_experiment(self):
        snapshots, outcomes = [], []
        for number in range(100):
            day = f"2026-02-{number + 1:02d}"
            snapshots.append(snapshot(day, f"{number:06d}", 80, "strict_level_2"))
            outcomes.append(outcome(day, f"{number:06d}", .02))
        result = run_walk_forward(snapshots, outcomes)
        self.assertEqual(result["content"]["status"], "continue_accumulating")
        self.assertIn("partitions", result["content"]["reason"])


def run_recommendation_experiment_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
