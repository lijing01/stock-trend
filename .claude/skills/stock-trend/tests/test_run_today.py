"""One natural-language entry must preserve dates, gaps and policy notices."""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from bridge import run_today as job

RUN_SCRIPT = job._run_script


BASELINE = {"status": "baseline", "experiment_id": "baseline",
            "priority_bonuses": {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}}
ACTIVE = {"status": "active", "experiment_id": "experiment-1", "release_id": "release-1",
          "priority_bonuses": {"strict_level_1": 0, "strict_level_2": 0, "strict_level_3": 0}}
SESSIONS = ["2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14"]


class TodayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.policy = copy.deepcopy(BASELINE)
        self.weekly_fails = False
        self.scan_fails = False
        self.market_fails = False
        self.recover = False

        def run_script(script, arguments):
            self.calls.append(script)
            if (self.market_fails and "market_regime" in script) or (self.scan_fails and "daily_candidates" in script):
                raise RuntimeError("fixture failure")
            return {"policy": {"evolution_version": self.policy["experiment_id"]},
                    "recommendations": [{"code": "600000"}], "meta": {"tracking": {"status": "created"}}}

        def close(as_of):
            self.calls.append("close:" + as_of)
            return job.evolution._package("close", {"as_of": as_of, "status": "completed"})

        def weekly(as_of):
            self.calls.append("weekly:" + as_of)
            if self.weekly_fails:
                raise ValueError("fixture weekly failure")
            return job.evolution._package("weekly", {"as_of": as_of, "status": "completed",
                                                       "input": {"research_snapshots": 3}})

        def monitor(**kwargs):
            self.calls.append("monitor:" + kwargs["as_of"])
            self.assertTrue(list((self.root / "jobs" / "close").glob("*.json")))
            self.assertTrue(all(day <= kwargs["as_of"] for day in kwargs["expected_trading_days"]))
            if self.recover:
                self.policy = copy.deepcopy(BASELINE)
            return job.evolution._package("monitor", {"status": "insufficient_data", "fallback": None})

        for target, replacement in (("_run_script", run_script), ("load_active_policy", lambda: copy.deepcopy(self.policy)),
                                     ("_load_authoritative_trading_dates", lambda now: set(SESSIONS)),
                                     ("evolution.run_close", close), ("evolution.run_weekly", weekly),
                                     ("evolution.monitoring_snapshot", monitor)):
            patcher = patch("bridge.run_today." + target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_job(self, hour=16, day=9):
        return job.run_today(now=datetime(2026, 9, day, hour, tzinfo=job.SHANGHAI),
                             state_root=self.root)

    def test_runs_in_order_and_preserves_recommendations(self):
        result = self.run_job()
        self.assertEqual(self.calls, ["analysis/market_regime.py", "scans/daily_candidates.py",
                                     "close:2026-09-09", "weekly:2026-09-09", "monitor:2026-09-09"])
        self.assertEqual(result["recommendations"], [{"code": "600000"}])
        self.assertEqual(result["notifications"], [])

    def test_intraday_evaluates_only_previous_close(self):
        result = self.run_job(hour=11)
        self.assertEqual(result["workflow"]["as_of"], "2026-09-08")
        self.assertIn("close:2026-09-08", self.calls)

    def test_weekend_evaluates_friday(self):
        result = self.run_job(day=12)
        self.assertEqual(result["workflow"]["as_of"], "2026-09-11")

    def test_calendar_missing_preserves_scan_and_skips_evaluation(self):
        with patch.object(job, "_load_authoritative_trading_dates", return_value=set()):
            result = self.run_job()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["workflow"]["close"]["reason"], "trading_calendar_unavailable")
        self.assertTrue(result["recommendations"])

    def test_historical_calendar_does_not_claim_current_coverage(self):
        with patch.object(job, "_load_authoritative_trading_dates", return_value={"2025-12-31"}):
            result = self.run_job()
        self.assertEqual(result["workflow"]["as_of"], "2025-12-31")
        self.assertEqual(result["workflow"]["calendar"]["status"], "historical_only")
        self.assertEqual(result["workflow"]["calendar"]["coverage_end"], "2025-12-31")
        self.assertEqual(result["workflow"]["status"], "partial")

    def test_weekend_with_history_only_calendar_still_evaluates_friday(self):
        with patch.object(job, "_load_authoritative_trading_dates", return_value={"2026-09-11"}):
            self.run_job(day=12)
        self.assertIn("close:2026-09-11", self.calls)
        self.assertIn("monitor:2026-09-11", self.calls)

    def test_weekly_success_is_deduplicated_but_close_retries(self):
        self.run_job()
        result = self.run_job(day=10)
        self.assertEqual(sum(call.startswith("weekly:") for call in self.calls), 1)
        self.assertEqual(sum(call.startswith("close:") for call in self.calls), 2)
        self.assertEqual(result["workflow"]["weekly"]["reason"], "already_completed_this_week")
        self.run_job(day=14)
        self.assertEqual(sum(call.startswith("weekly:") for call in self.calls), 2)

    def test_weekly_failure_does_not_block_monitor_and_can_retry(self):
        self.weekly_fails = True
        result = self.run_job()
        self.assertEqual(result["workflow"]["weekly"]["status"], "failed")
        self.assertIn("monitor:2026-09-09", self.calls)
        self.weekly_fails = False
        self.run_job()
        self.assertEqual(sum(call.startswith("weekly:") for call in self.calls), 2)

    def test_empty_weekly_run_does_not_consume_the_week(self):
        def empty(as_of):
            return job.evolution._package("weekly", {"as_of": as_of, "status": "completed",
                                                       "input": {"research_snapshots": 0}})
        with patch.object(job.evolution, "run_weekly", side_effect=empty) as weekly:
            self.run_job()
            self.run_job()
        self.assertEqual(weekly.call_count, 2)

    def test_weekday_holiday_uses_actual_calendar(self):
        with patch.object(job, "_load_authoritative_trading_dates", return_value=set(SESSIONS) - {"2026-09-09"}):
            result = self.run_job()
        self.assertEqual(result["workflow"]["as_of"], "2026-09-08")

    def test_corrupt_weekly_log_does_not_block_retry(self):
        directory = self.root / "jobs" / "weekly"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text('{"content": []}')
        self.run_job()
        self.assertIn("weekly:2026-09-09", self.calls)

    def test_market_failure_skips_scan_instead_of_using_old_context(self):
        self.market_fails = True
        result = self.run_job()
        self.assertNotIn("scans/daily_candidates.py", self.calls)
        self.assertEqual(result["workflow"]["candidates"]["reason"], "market_refresh_failed")
        self.assertNotIn("recommendations", result)

    def test_scan_failure_still_allows_history_evaluation(self):
        self.scan_fails = True
        result = self.run_job()
        self.assertEqual(result["workflow"]["candidates"]["status"], "failed")
        self.assertIn("close:2026-09-09", self.calls)

    def test_policy_change_notified_once_with_parameter_diff(self):
        self.run_job()
        self.policy = copy.deepcopy(ACTIVE)
        result = self.run_job()
        notice = result["notifications"][0]
        self.assertEqual(notice["previous"]["experiment_id"], "baseline")
        self.assertEqual(notice["current"]["experiment_id"], "experiment-1")
        self.assertEqual(notice["parameter_changes"]["strict_level_2"], {"before": 3, "after": 0})
        self.assertEqual(self.run_job()["notifications"], [])

    def test_first_active_policy_is_announced(self):
        self.policy = copy.deepcopy(ACTIVE)
        self.assertTrue(self.run_job()["notifications"])

    def test_recovery_during_monitor_announces_scan_and_final_versions(self):
        self.policy = copy.deepcopy(ACTIVE)
        self.run_job()
        self.recover = True
        result = self.run_job()
        self.assertEqual(result["policy"]["evolution_version"], "experiment-1")
        self.assertEqual(result["workflow"]["active_policy"]["experiment_id"], "baseline")
        self.assertEqual(result["notifications"][0]["source"], "during_run")

    def test_invalid_pointer_is_visible_even_on_first_call(self):
        self.policy["status"] = "invalid_pointer_fallback"
        self.assertEqual(self.run_job()["notifications"][0]["kind"], "policy_fallback")

    def test_safety_incident_notified_even_if_already_on_baseline(self):
        monitor = job.evolution._package("monitor", {"status": "fallback_required",
                                                    "fallback": {"status": "already_baseline"}})
        with patch.object(job.evolution, "monitoring_snapshot", return_value=monitor):
            result = self.run_job()
        self.assertEqual(result["notifications"][0]["kind"], "policy_safety_alert")
        self.assertEqual(result["notifications"][0]["fallback"]["status"], "already_baseline")
        self.assertEqual(result["workflow"]["status"], "partial")

    def test_dry_run_has_no_calls_or_writes(self):
        result = job.run_today(dry_run=True, state_root=self.root)
        self.assertEqual(result["workflow"]["status"], "dry_run")
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_dry_run_emits_parseable_json_and_forwards_options(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = job.main(["--dry-run", "--json", "--top", "5", "--no-html"])
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["workflow"]["candidate_args"], ["--top", "5", "--no-html"])

    def test_subprocess_accepts_market_progress_around_json(self):
        from subprocess import CompletedProcess
        meta = {"generated_at": "2026-09-09 16:00:00", "data_date": "2026-09-09"}
        result = {"meta": meta, "regime": {"score": 80}}
        (self.root / "market_regime.json").write_text(json.dumps({**meta, "regime": result["regime"]}))
        payload = '[1/5] 拉取数据\n' + json.dumps(result, indent=2) + '\nDone in 1.0s\n'
        with patch.object(job, "CACHE_DIR", self.root), patch.object(
                job.subprocess, "run", return_value=CompletedProcess([], 0, payload)) as run:
            result = RUN_SCRIPT("analysis/market_regime.py", ["--no-html"])
        self.assertEqual(result["regime"]["score"], 80)
        self.assertEqual(run.call_args.kwargs["cwd"], Path(__file__).resolve().parents[4])
        self.assertEqual(run.call_args.kwargs["env"]["TZ"], "Asia/Shanghai")

    def test_market_success_with_stale_saved_context_is_rejected(self):
        from subprocess import CompletedProcess
        (self.root / "market_regime.json").write_text(json.dumps({"generated_at": "old", "regime": {"score": 80}}))
        payload = json.dumps({"meta": {"generated_at": "new"}, "regime": {"score": 80}})
        with patch.object(job, "CACHE_DIR", self.root), patch.object(
                job.subprocess, "run", return_value=CompletedProcess([], 0, payload)):
            with self.assertRaisesRegex(ValueError, "market_context_not_persisted"):
                RUN_SCRIPT("analysis/market_regime.py", [])

    def test_subprocess_missing_json_is_failure(self):
        from subprocess import CompletedProcess
        with patch.object(job.subprocess, "run", return_value=CompletedProcess([], 0, "No data")):
            with self.assertRaises(ValueError):
                RUN_SCRIPT("analysis/market_regime.py", [])

    def test_policy_state_write_failure_preserves_result_and_notice(self):
        self.policy = copy.deepcopy(ACTIVE)
        with patch.object(job, "_atomic_write", side_effect=OSError("fixture read only")):
            result = self.run_job()
        self.assertTrue(result["recommendations"])
        self.assertTrue(result["notifications"])
        self.assertEqual(result["workflow"]["notification_tracking"]["status"], "failed")

    def test_utc_clock_is_converted_to_shanghai_close(self):
        from datetime import timezone
        result = job.run_today(now=datetime(2026, 9, 9, 8, tzinfo=timezone.utc), state_root=self.root)
        self.assertEqual(result["workflow"]["as_of"], "2026-09-09")


def run_today_tests():
    result = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(TodayTests))
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
