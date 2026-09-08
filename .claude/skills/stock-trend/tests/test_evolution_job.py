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
                                     rollback_active_policy, transition_experiment)
from core.recommendation_snapshot import build_snapshot


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
            registered = register_experiment(default_experiment(), registry)
            identifier = registered["experiment_id"]
            transition_experiment(identifier, "validated", {"result": "ok"}, registry)
            transition_experiment(identifier, "shadow", {"result": "ok"}, registry)
            transition_experiment(identifier, "eligible", {"result": "ok"}, registry)
            published = publish_experiment(identifier, {"operator": "test"}, registry, releases)
            self.assertEqual(published["status"], "published")
            self.assertEqual(load_active_policy(releases)["experiment_id"], identifier)
            rolled = rollback_active_policy({"operator": "test"}, releases)
            self.assertEqual(rolled["status"], "rolled_back_to_baseline")
            self.assertEqual(load_active_policy(releases)["experiment_id"], "baseline")

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
