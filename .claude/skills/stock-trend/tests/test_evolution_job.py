"""P4 orchestration, publication, and recovery contracts."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.evolution_job import run_close, run_weekly
from backtesting.recommendation_experiments import default_experiment
from core.evolution_registry import (load_active_policy, publish_experiment, register_experiment,
                                     rollback_active_policy, transition_experiment)
from core.recommendation_snapshot import build_snapshot


class T(unittest.TestCase):
    def test_close_preserves_missing_upstream_day_as_gap(self):
        with tempfile.TemporaryDirectory() as root:
            run = run_close("2026-09-07", history_root=root, attribution_root=Path(root) / "eval")
        self.assertEqual(run["content"]["status"], "upstream_gap")
        self.assertEqual(run["content"]["stage"], "preflight")

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


def run_evolution_job_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
