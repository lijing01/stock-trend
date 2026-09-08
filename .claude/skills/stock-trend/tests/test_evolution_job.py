"""P4 orchestration, publication, and recovery contracts."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.evolution_job import run_close, run_weekly
from backtesting.recommendation_experiments import default_experiment
from core.candidate_research_snapshot import build_research_snapshot, save_research_snapshot
from core.evolution_registry import (load_active_policy, publish_experiment, register_experiment,
                                     register_holdout_consumption, rollback_active_policy,
                                     transition, transition_experiment)
from core.recommendation_snapshot import build_snapshot, content_sha256


class T(unittest.TestCase):
    def _research(self, day, code="600000"):
        candidate = {"code": code, "ts_code": code + ".SH", "composite_score": 80,
                     "quality_adjusted_score": 80, "data_quality": {"eligible": True}}
        return build_research_snapshot(
            [candidate], {"actionable": [candidate]}, day, {}, {}, [], 50,
            official_tracking={"status": "created"}, model_version="test")

    def test_close_preserves_missing_upstream_day_as_gap(self):
        with tempfile.TemporaryDirectory() as root:
            run = run_close("2026-09-07", history_root=root, attribution_root=Path(root) / "eval")
        self.assertEqual(run["content"]["status"], "upstream_gap")
        self.assertEqual(run["content"]["stage"], "preflight")

    def test_close_evaluates_history_even_when_current_formal_snapshot_is_missing(self):
        calls = []
        def tracker(**kwargs):
            calls.append(kwargs)
            return {"summary": {"status": "ready", "snapshots": 2,
                                 "mature_dates": 2, "deduplicated_mature_events": 4},
                    "candidate_signal_summary": {"status": "ready",
                                                  "valid_alpha_dates": 20,
                                                  "valid_alpha_events": 100}}
        with tempfile.TemporaryDirectory() as root:
            run = run_close("2026-09-07", history_root=root, attribution_root=Path(root) / "eval",
                            tracker=tracker)
        self.assertEqual(len(calls), 1)
        self.assertEqual(run["content"]["status"], "upstream_gap")
        self.assertEqual(run["content"]["stages"]["daily_collection"]["status"], "upstream_gap")
        self.assertEqual(run["content"]["stages"]["historical_maturity_evaluation"]["status"], "ready")

    def test_close_stage_uses_candidate_signal_maturity_before_trade_simulation(self):
        def tracker(**kwargs):
            return {"summary": {"status": "evidence_insufficient", "snapshots": 1,
                                 "mature_dates": 0, "deduplicated_mature_events": 0},
                    "candidate_signal_summary": {"status": "ready", "mature_dates": 20,
                                                  "deduplicated_mature_events": 100,
                                                  "alpha_mature_dates": 20}}
        with tempfile.TemporaryDirectory() as root:
            source = {"recommendation_date": "2026-09-07", "generated_at": "2026-09-07T15:00:00+08:00",
                      "snapshot_type": "formal", "model_version": "test", "policy": {},
                      "market_regime": {"score": 80}, "sectors": [], "candidates": [],
                      "scan_status": "complete", "buckets": {"actionable": [], "waiting_trigger": [],
                      "next_day_confirmation": [], "observation": [], "data_rejected": []}}
            path = Path(root, "2026-09-07.json")
            path.write_bytes(__import__("core.recommendation_snapshot", fromlist=["canonical_json"]).canonical_json(build_snapshot(source)))
            run = run_close("2026-09-07", history_root=root, tracker=tracker)
        self.assertEqual(run["content"]["historical_evaluation_status"], "ready")
        self.assertEqual(run["content"]["trade_simulation_status"], "evidence_insufficient")

    def test_close_dry_run_does_not_call_tracker(self):
        with tempfile.TemporaryDirectory() as root:
            source = {"recommendation_date": "2026-09-07", "generated_at": "2026-09-07T15:00:00+08:00",
                      "snapshot_type": "formal", "model_version": "test", "policy": {}, "market_regime": {"score": 80},
                      "sectors": [], "candidates": [], "scan_status": "complete",
                      "buckets": {"actionable": [], "waiting_trigger": [], "next_day_confirmation": [], "observation": [], "data_rejected": []}}
            Path(root, "2026-09-07.json").write_bytes(__import__("core.recommendation_snapshot", fromlist=["canonical_json"]).canonical_json(build_snapshot(source)))
            run = run_close("2026-09-07", history_root=root, dry_run=True,
                            tracker=lambda **_: self.fail("tracker must not run"))
        self.assertEqual(run["content"]["status"], "dry_run")

    def test_explicit_publish_and_rollback_keep_old_records_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            registry, releases = Path(root) / "registry", Path(root) / "releases"
            experiments, shadow = Path(root) / "experiments", Path(root) / "shadow"
            registered = register_experiment(default_experiment(), registry)
            identifier = registered["experiment_id"]
            validation = self._release_result(identifier, "validation-1")
            holdout = self._release_result(identifier, "holdout-1")
            shadow_result = self._shadow_result("shadow-1")
            experiments.mkdir()
            (experiments / "validation-1.json").write_text(
                __import__("json").dumps(validation), encoding="utf-8")
            (experiments / "holdout-1.json").write_text(
                __import__("json").dumps(holdout), encoding="utf-8")
            shadow.mkdir()
            (shadow / "shadow-1.json").write_text(
                __import__("json").dumps(shadow_result), encoding="utf-8")
            consumption = register_holdout_consumption(
                identifier, "batch-1", "input-1", "holdout-1",
                {"definition": default_experiment()}, registry)
            evidence = self._release_evidence(
                identifier, validation, holdout, shadow_result, consumption,
            )
            transition_experiment(identifier, "validated", {
                "validation_result": validation,
            }, registry)
            transition_experiment(identifier, "shadow", {
                "shadow_result": shadow_result,
            }, registry)
            transition_experiment(identifier, "eligible", evidence, registry)
            published = publish_experiment(identifier, evidence, registry, releases)
            self.assertEqual(published["status"], "published")
            self.assertEqual(load_active_policy(releases)["experiment_id"], identifier)
            rolled = rollback_active_policy({"operator": "test"}, releases)
            self.assertEqual(rolled["status"], "rolled_back_to_baseline")
            self.assertEqual(load_active_policy(releases)["experiment_id"], "baseline")

    @staticmethod
    def _release_result(experiment_id, result_id):
        content = {
            "schema_version": "recommendation-experiment/v2",
            "definition": default_experiment(),
            "contract_id": "contract-1",
            "calendar_verified": True,
            "partitions": {"status": "valid"},
            "maturity": {
                "baseline": {"valid_alpha_events": 100, "alpha_mature_dates": 20},
                "treatment": {"valid_alpha_events": 100, "alpha_mature_dates": 20},
                "confirmation_60d": {
                    "baseline_valid_alpha_events": 100,
                    "treatment_valid_alpha_events": 100,
                    "complete_paired_dates": 20,
                    "mean_delta": .01,
                },
            },
            "paired_dates": [{"date": f"2026-08-{number:02d}", "delta": .01}
                             for number in range(1, 21)],
            "block_pair_counts": {"1": 20, "2": 20, "3": 20},
            "coverage": {"baseline_dates": 60, "treatment_dates": 60,
                          "baseline_complete_ratio": 1.0,
                          "treatment_complete_ratio": 1.0},
            "interval": {"lower_95": .001, "valid_block_count": 2},
            "mae_tail_5pct": {"baseline": -.03, "treatment": -.03},
            "promotion": {"eligible": True},
            "input_manifest": {"input_sha256": "input-1"},
        }
        return {"experiment_id": result_id,
                "content_sha256": content_sha256(content), "content": content}

    @staticmethod
    def _shadow_result(result_id):
        content = {
            "schema_version": "recommendation-shadow/v1",
            "shadow_type": "forward", "formal_policy_affected": False,
            "retrospective": False, "independent_snapshot": True,
            "complete_paired_dates": 20, "valid_alpha_events": 100,
            "input_manifest": {"input_sha256": "shadow-input"},
        }
        return {"experiment_id": result_id,
                "content_sha256": content_sha256(content), "content": content}

    @staticmethod
    def _release_evidence(experiment_id, validation, holdout, shadow, consumption):
        return {
            "schema_version": "evolution-release-evidence/v2",
            "experiment_id": experiment_id,
            "validation_result_id": validation["experiment_id"],
            "holdout_result_id": holdout["experiment_id"],
            "shadow_result_id": shadow["experiment_id"],
            "contract_id": "contract-1",
            "validation_result": validation,
            "holdout_result": holdout,
            "shadow_result": shadow,
            "holdout_consumption": consumption,
        }

    def test_bare_evidence_cannot_reach_eligible(self):
        registered = register_experiment(default_experiment(), tempfile.mkdtemp())
        record = {
            "experiment_id": registered["experiment_id"],
            "content": {
                "schema_version": "evolution-registry/v2",
                "definition": default_experiment(),
                "state": "shadow", "history": [],
            },
        }
        with self.assertRaises(ValueError):
            transition(record, "eligible", {"result": "ok"})

    def test_legacy_registered_schema_cannot_reach_eligible(self):
        record = {
            "experiment_id": "legacy1",
            "content": {
                "schema_version": "evolution-registry/v1",
                "definition": default_experiment(),
                "state": "shadow", "history": [],
            },
        }
        with self.assertRaisesRegex(ValueError, "legacy_experiment_unverified"):
            transition(record, "eligible", {"result": "ok"})

    def test_holdout_consumption_is_idempotent_but_parameter_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            first = register_holdout_consumption(
                "experiment-1", "batch-1", "input-1", "result-1",
                {"bonus": {"strict_level_1": 0}}, root,
            )
            again = register_holdout_consumption(
                "experiment-1", "batch-1", "input-1", "result-1",
                {"bonus": {"strict_level_1": 0}}, root,
            )
            self.assertEqual(first["status"], "created")
            self.assertEqual(again["status"], "unchanged")
            self.assertEqual(first["consumption_id"], again["consumption_id"])
            with self.assertRaises(ValueError):
                register_holdout_consumption(
                    "experiment-1", "batch-1", "input-1", "result-2",
                    {"bonus": {"strict_level_1": 1}}, root,
                )
            attempts = list((Path(root) / "holdout_consumption" / "experiment-1"
                             / "attempts").glob("*.json"))
            self.assertEqual(len(attempts), 1)

    def test_weekly_dry_run_is_offline_and_idempotent_shape(self):
        with tempfile.TemporaryDirectory() as root:
            run = run_weekly("2026-09-07", research_root=Path(root) / "research",
                             attribution_root=Path(root) / "eval", dry_run=True)
        self.assertEqual(run["content"]["status"], "dry_run")
        self.assertIn("diagnostic_id", run["content"])

    def test_weekly_reads_new_research_root_and_applies_as_of_cutoff(self):
        with tempfile.TemporaryDirectory() as root:
            research_root = Path(root) / "research"
            save_research_snapshot(self._research("2026-09-07", "A"), research_root)
            save_research_snapshot(self._research("2026-09-08", "B"), research_root)
            run = run_weekly("2026-09-07", research_root=research_root,
                             attribution_root=Path(root) / "evaluations", dry_run=True)
        self.assertEqual(run["content"]["input"]["research_snapshots"], 1)
        self.assertEqual(run["content"]["as_of"], "2026-09-07")


def run_evolution_job_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
