"""Offline contracts for P3 rolling validation and strategy shadowing."""
import sys
import tempfile
import unittest
import copy
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtesting.recommendation_experiments import (
    _bootstrap, _metrics_for_rows, _outcomes, _replay_selection,
    _partition_input_manifest,
    _resolve_time_partitions, build_forward_shadow_result, default_experiment, run_final_holdout,
    run_walk_forward, save_experiment,
)
from core.research_events import assign_research_events
from core.recommendation_snapshot import content_sha256
from core.evolution_registry import _verify_holdout_result, register_experiment, transition
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
            "contract_id": default_experiment()["contract_id"],
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
    @staticmethod
    def _frozen_definition():
        sessions = [(date(2026, 1, 1) + timedelta(days=number)).isoformat()
                    for number in range(400)]
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
        return definition

    def test_final_holdout_is_separate_consumed_result(self):
        sessions = []
        current = date(2026, 1, 1)
        while len(sessions) < 400:
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
        for number, day in enumerate(sessions[340:360]):
            records = []
            position = sessions.index(day)
            for code, level, score in ((f"A{number:02d}", "strict_level_3", 80),
                                       (f"B{number:02d}", "strict_level_1", 80.5)):
                candidate = {"code": code, "composite_score": score,
                             "quality_adjusted_score": score,
                             "buy_point_level": level,
                             "wyckoff": {"short_term": {
                                 "sub_phase": "jac" if level == "strict_level_3" else "spring",
                                 "signal_status": "confirmed", "signal_age_bars": 0,
                                 "post_lps_reconfirmation": True}},
                             "data_quality": {"eligible": True},
                             "sector_actionable": True, "score_eligible": True}
                records.append({"record_id": f"{day}:SH:{code}", "code": code,
                                "market": "SH", "candidate": candidate,
                                "preselection": {"composite_score": score,
                                    "quality_adjusted_score": score,
                                    "data_quality_eligible": True,
                                    "sector_actionable": True, "score_eligible": True,
                                    "qualification_status": "eligible", "min_score": 50}})
                outcomes.append({"recommendation_date": day, "market": "SH", "code": code,
                                 "record_id": f"{day}:SH:{code}",
                                 "contract_id": definition["contract_id"],
                                 "evaluation_contract": {"contract_id": definition["contract_id"]},
                                 "windows": {"20": {"status": "complete",
                                     "hs300_alpha": .1 if code.startswith("B") else 0,
                                     "mae": -.03, "entry_date": day,
                                     "exit_date": sessions[position + 19]}}})
            snapshots.append({"content": {"recommendation_date": day,
                "snapshot_type": "formal", "official_snapshot": {"link_status": "linked"},
                "parameter_summary": {"top": 1, "min_score": 50},
                "policy": {"mode": "actionable", "max_recommendations": 1},
                "records": records}})
        receipt_manifest = _partition_input_manifest(
            snapshots, outcomes, definition, "final_holdout", sessions[340:360], sessions)
        result = run_final_holdout(
            snapshots, outcomes, definition=definition,
            holdout_consumption={"status": "created", "consumption_id": "c1",
                                 "experiment_id": "experiment-1",
                                 "research_batch_id": "batch-1", "result_id": "holdout-1",
                                 "input_manifest_sha256": receipt_manifest["input_sha256"]},
            experiment_id="experiment-1")
        self.assertEqual(result["experiment_id"], "holdout-1")
        self.assertEqual(result["content"]["status"], "holdout_confirmed", result["content"]["promotion"])
        self.assertTrue(result["content"]["promotion"]["gates"]["primary_delta_positive"])
        self.assertEqual(_verify_holdout_result(result, "holdout-1")["result_id"], "holdout-1")

    def test_final_holdout_rejects_path_like_result_identity(self):
        with self.assertRaisesRegex(ValueError, "result_id_invalid"):
            run_final_holdout([], [], definition=default_experiment(),
                              holdout_consumption={
                                  "status": "created", "consumption_id": "c1",
                                  "research_batch_id": "b1",
                                  "input_manifest_sha256": "h1",
                                  "result_id": "../escape",
                              })

    def test_final_holdout_rejects_receipt_hash_not_matching_partition_inputs(self):
        definition = self._frozen_definition()
        with self.assertRaisesRegex(ValueError, "holdout_input_manifest_mismatch"):
            run_final_holdout(
                [], [], definition=definition, experiment_id="experiment-1",
                holdout_consumption={"status": "created", "consumption_id": "c1",
                                     "experiment_id": "experiment-1",
                                     "research_batch_id": "batch-1",
                                     "input_manifest_sha256": "arbitrary-receipt",
                                     "result_id": "holdout-1"})

    def test_result_persistence_rejects_path_escape(self):
        result = run_walk_forward([], [])
        result["experiment_id"] = "../escape"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "result_id_invalid"):
                save_experiment(result, root)

    def test_forward_shadow_aggregate_requires_measured_evaluation(self):
        selection = {"top": 1, "min_score": 50.0, "policy": {"mode": "actionable"},
                     "baseline": {"selected": [], "buckets": {"actionable": []}},
                     "treatment": {"selected": [], "buckets": {"actionable": []}}}
        source_input = {"basis_date": "2027-01-01", "selection": selection}
        source = {
            "schema_version": "recommendation-strategy-shadow/v1",
            "basis_date": "2027-01-01", "snapshot_type": "formal",
            "definition": default_experiment(), "experiment_id": "exp-1",
            "contract_id": default_experiment()["contract_id"],
            "shadow_type": "forward", "retrospective": False,
            "independent_snapshot": True, "formal_policy_affected": False,
            "selection": selection,
            "input_manifest": {"inputs": source_input,
                                "input_sha256": content_sha256(source_input)},
        }
        source["content_sha256"] = content_sha256(source)
        source["input_digest"] = source["content_sha256"]
        with self.assertRaisesRegex(ValueError, "shadow_evaluation_required"):
            build_forward_shadow_result(source)

    def test_forward_shadow_aggregate_binds_each_event_to_source_selection(self):
        from test_evolution_job import T as ReleaseFixtures
        aggregate = ReleaseFixtures._shadow_result("shadow-1")
        source_runs = []
        for source in aggregate["content"]["source_runs"]:
            snapshot = source["snapshot"]
            source_runs.append({
                **snapshot,
                "content_sha256": source["content_sha256"],
                "input_digest": source["content_sha256"],
            })
        evaluation = {
            "paired_dates": aggregate["content"]["paired_dates"],
            "coverage_rows": aggregate["content"]["coverage_rows"],
            "event_rows": aggregate["content"]["event_rows"],
            "trading_sessions": aggregate["content"]["trading_sessions"],
            "valid_alpha_events": 100,
            "coverage": aggregate["content"]["coverage"],
            "input_manifest": {"input_sha256": "evaluation-input"},
        }
        result = build_forward_shadow_result(source_runs, evaluation)
        self.assertEqual(result["content"]["complete_paired_dates"], 20)
        self.assertEqual(result["content"]["valid_alpha_events"], 100)

    def test_forward_shadow_rejects_unmatured_selected_event(self):
        from test_evolution_job import T as ReleaseFixtures
        aggregate = ReleaseFixtures._shadow_result("shadow-1")
        source_runs = []
        for source in aggregate["content"]["source_runs"]:
            snapshot = source["snapshot"]
            source_runs.append({
                **snapshot,
                "content_sha256": source["content_sha256"],
                "input_digest": source["content_sha256"],
            })
        evaluation = {
            "paired_dates": aggregate["content"]["paired_dates"],
            "coverage_rows": aggregate["content"]["coverage_rows"],
            "event_rows": copy.deepcopy(aggregate["content"]["event_rows"]),
            "trading_sessions": aggregate["content"]["trading_sessions"],
            "valid_alpha_events": 100,
            "coverage": aggregate["content"]["coverage"],
            "input_manifest": {"input_sha256": "evaluation-input"},
        }
        evaluation["event_rows"]["baseline"][0]["evaluation_complete"] = False
        with self.assertRaisesRegex(ValueError, "shadow_selection_event_incomplete"):
            build_forward_shadow_result(source_runs, evaluation)

    def test_replay_identity_keeps_same_code_on_different_markets(self):
        records = []
        for market in ("SH", "HK"):
            row = research_record("000001")
            row["market"] = market
            row["record_id"] = f"2026-08-20:{market}:000001"
            records.append(row)
        result = _replay_selection(records, {"strict_level_1": 0,
                                             "strict_level_2": 0,
                                             "strict_level_3": 0}, 2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["selected"]), 2)

    def test_missing_earliest_overlap_cannot_become_new_mature_event(self):
        outcomes = _outcomes([
            {"recommendation_date": "2026-01-01", "market": "SH", "code": "A",
             "record_id": "r1", "windows": {"20": {"status": "pending",
                 "entry_date": "2026-01-01", "exit_date": "2026-01-20"}}},
            {"recommendation_date": "2026-01-05", "market": "SH", "code": "A",
             "record_id": "r2", "windows": {"20": {"status": "complete", "hs300_alpha": .1,
                 "entry_date": "2026-01-05", "exit_date": "2026-01-23"}}},
        ])
        rows = [{"code": "A", "_research_market": "SH"}]
        skipped = []
        first = _metrics_for_rows(rows, "2026-01-01", outcomes, None, 20,
                                  "baseline", skipped)
        second = _metrics_for_rows(rows, "2026-01-05", outcomes, None, 20,
                                   "baseline", skipped)
        assigned = assign_research_events(first["events"] + second["events"], 20)
        mature_ids = first["complete_event_ids"] | second["complete_event_ids"]
        mature = [event for event in assigned["events"]
                  if event["record_id"] in mature_ids]
        self.assertEqual(len(assigned["events"]), 1)
        self.assertEqual(mature, [])

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
            saved = register_experiment(self._frozen_definition(), root)
            again = register_experiment(self._frozen_definition(), root)
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
                                 "contract_id": definition["contract_id"],
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
        # A 60-day-pending run is eligible for shadow only after every
        # primary gate (including positive block-bootstrap CI) passes.  This
        # fixture has zero delta, so it must continue accumulating instead of
        # being labelled validated.
        self.assertEqual(result["content"]["status"], "continue_accumulating")
        self.assertFalse(result["content"]["promotion"]["shadow_only"])
        self.assertFalse(result["content"]["promotion"]["gates"]["confirmation_60d"])
        self.assertEqual(result["content"]["maturity"]["confirmation_60d"]["complete_paired_dates"], 0)


def run_recommendation_experiment_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
