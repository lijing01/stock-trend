"""Offline contracts for P3 rolling validation and strategy shadowing."""
import sys
import tempfile
import unittest
import copy
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtesting.recommendation_experiments import (
    _bootstrap, _replay_selection, _resolve_time_partitions, default_experiment,
    run_walk_forward, save_experiment,
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
                            "score_eligible": True, "qualification_status": "eligible",
                            "min_score": 50},
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
            "min_score": 50,
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

    def test_partition_contract_rejects_invalid_dates_and_short_purge(self):
        definition = default_experiment()
        definition.update({
            "freeze_at": "2026-01-01",
            "partitions": {
                "discovery": {"start": "2026-01-02", "end": "2026-01-02"},
                "validation_1": {"start": "2026-01-03", "end": "2026-01-03"},
                "validation_2": {"start": "2026-01-04", "end": "2026-01-04"},
                "validation_3": {"start": "2026-01-32", "end": "2026-02-01"},
                "final_holdout": {"start": "2026-05-01", "end": "2026-05-01"},
            },
        })
        result = _resolve_time_partitions(
            definition,
            ["2026-01-02", "2026-01-03", "2026-01-04", "2026-05-01"],
            [f"2026-01-{day:02d}" for day in range(1, 32)]
            + [f"2026-02-{day:02d}" for day in range(1, 29)]
            + [f"2026-03-{day:02d}" for day in range(1, 32)]
            + [f"2026-04-{day:02d}" for day in range(1, 31)]
            + ["2026-05-01"],
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("partition_validation_3", result["reason"])

    def test_bootstrap_does_not_cross_partition_boundaries(self):
        dates = [f"2026-08-{number:02d}" for number in range(1, 13)]
        result = _bootstrap(
            list(range(12)), seed=7, draws=5, block_length=3, dates=dates,
            partitions={"validation_1": dates[:6], "validation_2": dates[6:]},
        )
        labels = {day: name for name, group in {
            "validation_1": dates[:6], "validation_2": dates[6:],
        }.items() for day in group}
        self.assertTrue(all(len({labels[dates[index]] for index in block}) == 1
                            for block in result["valid_blocks"]))

    def test_bootstrap_does_not_compress_missing_calendar_sessions(self):
        dates = ["2026-08-03", "2026-08-04", "2026-08-06", "2026-08-07"]
        result = _bootstrap(
            [1, 2, 3, 4], seed=7, draws=5, block_length=3, dates=dates,
            trading_sessions=["2026-08-03", "2026-08-04", "2026-08-05",
                              "2026-08-06", "2026-08-07"],
        )
        self.assertEqual(result["valid_block_count"], 0)
        self.assertEqual(result["status"], "insufficient_data")

    def test_primary_ready_without_60d_confirmation_stays_shadow_only(self):
        sessions = []
        current = date(2026, 1, 1)
        while len(sessions) < 370:
            sessions.append(current.isoformat())
            current += timedelta(days=1)
        definition = default_experiment()
        definition.update({
            "trading_sessions": sessions,
            "freeze_at": sessions[0],
            "partitions": {
                "discovery": {"start": sessions[0], "end": sessions[9]},
                "validation_1": {"start": sessions[70], "end": sessions[89]},
                "validation_2": {"start": sessions[160], "end": sessions[179]},
                "validation_3": {"start": sessions[250], "end": sessions[269]},
                "final_holdout": {"start": sessions[340], "end": sessions[359]},
            },
        })
        snapshots, outcomes = [], []
        validation_dates = (sessions[70:90] + sessions[160:180]
                            + sessions[250:270])
        for number, day in enumerate(validation_dates):
            records = []
            for suffix in ("A", "B"):
                code = f"{number:03d}{suffix}"
                candidate = {
                    "code": code, "composite_score": 80,
                    "quality_adjusted_score": 80,
                    "data_quality": {"eligible": True},
                    "sector_actionable": True, "score_eligible": True,
                }
                preselection = {
                    "composite_score": 80,
                    "quality_adjusted_score": 80,
                    "data_quality_eligible": True,
                    "sector_actionable": True,
                    "score_eligible": True,
                    "qualification_status": "eligible",
                    "min_score": 50,
                }
                records.append({"record_id": f"{day}:SH:{code}",
                                "code": code, "candidate": candidate,
                                "preselection": preselection})
                position = sessions.index(day)
                outcomes.append({"recommendation_date": day, "code": code,
                                 "windows": {"20": {
                                     "status": "complete", "hs300_alpha": .02,
                                     "mae": -.03, "entry_date": day,
                                     "exit_date": sessions[position + 19],
                                 }}})
            snapshots.append({"content": {
                "recommendation_date": day, "snapshot_type": "formal",
                "official_snapshot": {"link_status": "linked"},
                "parameter_summary": {"top": 2, "min_score": 50},
                "policy": {"mode": "actionable", "max_recommendations": 2},
                "records": records,
            }})
        result = run_walk_forward(snapshots, outcomes, definition=definition)
        self.assertEqual(result["content"]["status"], "validated")
        self.assertTrue(result["content"]["promotion"]["shadow_only"])
        self.assertFalse(result["content"]["promotion"]["gates"]["confirmation_60d"])
        self.assertEqual(result["content"]["maturity"]["confirmation_60d"]["complete_paired_dates"], 0)


def run_recommendation_experiment_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
