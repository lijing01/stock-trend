"""One natural-language entry must preserve dates, gaps and policy notices."""
import copy
import hashlib
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
        self.candidate_arguments = []
        self.policy = copy.deepcopy(BASELINE)
        self.weekly_fails = False
        self.scan_fails = False
        self.market_fails = False
        self.recover = False

        def run_script(script, arguments):
            self.calls.append(script)
            if "daily_candidates" in script:
                self.candidate_arguments.append(list(arguments))
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
        self.assertIn("--news", self.candidate_arguments[0])

    def test_post_close_automatically_uses_fixed_scan_scope(self):
        self.run_job(hour=16)
        self.assertIn("--post-close-final", self.candidate_arguments[0])

        self.candidate_arguments.clear()
        self.run_job(hour=11)
        self.assertNotIn("--post-close-final", self.candidate_arguments[0])

    def test_explicit_no_news_is_not_overridden(self):
        job.run_today(["--no-news"], now=datetime(
            2026, 9, 9, 16, tzinfo=job.SHANGHAI), state_root=self.root)
        self.assertIn("--no-news", self.candidate_arguments[0])
        self.assertNotIn("--news", self.candidate_arguments[0])

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

    def test_cli_status_finds_review_html_task(self):
        from analysis import market_regime
        from bridge import today_background
        html_path = self.root / "daily-review.html"
        html_path.write_text(market_regime.render_observation_list_html(pending=True), encoding="utf-8")
        task = today_background.ensure_review_html_task(
            html_path, root=self.root / "background")
        stream = io.StringIO()
        with patch.object(job, "DEFAULT_BACKGROUND_ROOT", self.root / "background"), \
                redirect_stdout(stream):
            code = job.main(["--status", task["task_id"], "--json"])
        result = json.loads(stream.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["task_id"], task["task_id"])
        self.assertEqual(result["status"], "queued")

    def test_subprocess_accepts_market_progress_around_json(self):
        from subprocess import CompletedProcess
        meta = {"generated_at": "2026-09-09 16:00:00", "data_date": "2026-09-09"}
        result = {"meta": meta, "regime": {"score": 80}}
        (self.root / "market_regime.json").write_text(json.dumps({**meta, "regime": result["regime"]}))
        html_path = self.root / "daily-review.html"
        html_path.write_text("html", encoding="utf-8")
        payload = ('[1/5] 拉取数据\n' + json.dumps(result, indent=2)
                   + f'\nHTML: {html_path}\nDone in 1.0s\n')
        with patch.object(job, "CACHE_DIR", self.root), patch.object(
                job.subprocess, "run", return_value=CompletedProcess([], 0, payload)) as run:
            result = RUN_SCRIPT("analysis/market_regime.py", ["--no-html"])
        self.assertEqual(result["regime"]["score"], 80)
        self.assertEqual(result["report_paths"]["html"], str(html_path.resolve()))
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

    def test_background_mode_returns_report_before_auxiliary_stages(self):
        task = {"task_id": "task-1", "status": "queued"}
        with patch.object(job, "_launch_background", return_value=task):
            result = job.run_today(now=datetime(2026, 9, 9, 16, tzinfo=job.SHANGHAI),
                                   state_root=self.root, postprocess="background")
        self.assertEqual(result["workflow"]["status"], "report_ready")
        self.assertEqual(result["workflow"]["postprocess"]["task_id"], "task-1")
        self.assertEqual(result["workflow"]["close"]["status"], "background")
        self.assertEqual(result["recommendations"], [{"code": "600000"}])

    def test_daily_review_path_is_emitted_before_review_update(self):
        daily_path = self.root / "daily-review.html"
        candidate_path = self.root / "candidates.html"
        daily_path.write_text("daily", encoding="utf-8")
        candidate_path.write_text("candidate", encoding="utf-8")
        calls = []
        events = []

        def run_script(script, arguments):
            calls.append(script)
            events.append(script)
            if "market_regime" in script:
                return {
                    "meta": {"generated_at": "2026-09-09 16:00:00", "data_date": "2026-09-09"},
                    "regime": {"score": 80},
                    "report_paths": {"html": str(daily_path)},
                }
            return {
                "recommendations": [{"code": "600000"}],
                "meta": {"observation_pool_artifact": str(self.root / "candidate-pool.json")},
                "report_paths": {"html": str(candidate_path)},
            }

        queued = []
        with patch.object(job, "_run_script", side_effect=run_script), \
                patch.object(job, "_launch_review_html_update",
                             side_effect=lambda path, **kwargs: events.append("review_html")
                             or queued.append((path, kwargs))
                             or {"task_id": "review-1", "status": "running"}):
            result = self.run_job()

        self.assertEqual(calls, ["analysis/market_regime.py", "scans/daily_candidates.py"])
        self.assertEqual(events, ["analysis/market_regime.py",
                                  "scans/daily_candidates.py", "review_html"])
        self.assertEqual(queued, [(str(daily_path), {
            "data_date": "2026-09-09",
            "background_root": self.root / "background"})])
        self.assertEqual(result["report_paths"]["daily_review_html"], str(daily_path))
        self.assertEqual(result["report_paths"]["html"], str(candidate_path))
        self.assertEqual(result["workflow"]["review_html"]["task_id"], "review-1")

    def test_background_worker_persists_final_status_and_stages(self):
        from bridge import today_background
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifest = {"as_of": "2026-09-09", "trading_sessions": ["2026-09-09"],
                        "job_root": str(root / "jobs"), "budget_seconds": 30}
            task = today_background.ensure_task(manifest, root=root / "background")
            package = lambda kind, content: job.evolution._package(kind, content)
            with patch.object(today_background.evolution, "run_close",
                              return_value=package("close", {"status": "completed"})), \
                 patch.object(today_background.evolution, "run_weekly",
                              return_value=package("weekly", {"status": "completed"})), \
                 patch.object(today_background.evolution, "monitoring_snapshot",
                              return_value=package("monitor", {"status": "healthy"})):
                result = today_background.run_task(task["task_id"], root=root / "background")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(today_background.read_status(task["task_id"], root=root / "background")["status"],
                             "completed")
            self.assertTrue((root / "background" / task["task_id"] / "result.json").exists())

    def test_review_html_background_task_updates_same_path(self):
        from analysis import market_regime
        from bridge import today_background
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            html_path = root / "daily-review.html"
            html_path.write_text(
                "before\n" + market_regime.render_observation_list_html(pending=True) + "\nafter",
                encoding="utf-8",
            )
            yaml_path = root / "observation_list.yaml"
            yaml_path.write_text("observation_list:\n  - code: '001207'\n", encoding="utf-8")
            artifact = root / "observation-analysis.json"
            artifact.write_text(json.dumps({
                "schema": "yaml-observation-analysis/v1", "status": "ready", "data_date": "2026-09-21",
                "config_sha256": hashlib.sha256(yaml_path.read_bytes()).hexdigest(),
                "items": [{"code": "001207", "name": "测试股", "date": "2026-09-01",
                           "entry_phase": "吸筹", "composite_score": 60,
                           "quality_adjusted_score": 55, "raw_dimensions": {},
                           "wyckoff": {}, "data_quality": {"status": "ready"}}]},
                ensure_ascii=False), encoding="utf-8")
            task = today_background.ensure_review_html_task(
                html_path, data_date="2026-09-21", artifact_path=artifact,
                yaml_path=yaml_path, config_sha256=hashlib.sha256(yaml_path.read_bytes()).hexdigest(),
                root=root / "background")
            with patch("analysis.observation_list_analysis.analyze_observation_list"):
                result = today_background.run_review_html_task(task["task_id"], root=root / "background")
            status = today_background.read_review_html_status(
                task["task_id"], root=root / "background")
            updated = html_path.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(status["status"], "completed")
            self.assertIn("观察列表", updated)
            self.assertIn("001207", updated)
            self.assertIn("before", updated)
            self.assertIn("after", updated)

    def test_research_stages_wrap_formal_stages_in_sync_mode(self):
        from bridge import today_background
        with patch.object(today_background, "_factor_daily", side_effect=lambda *_:
                          self.calls.append("factor_daily") or {"status": "completed"}), \
             patch.object(today_background, "_factor_evaluation", side_effect=lambda *_:
                          self.calls.append("factor_evaluation") or {"status": "continue_accumulating"}):
            result = self.run_job()
        self.assertEqual(self.calls, ["analysis/market_regime.py", "scans/daily_candidates.py",
                                      "factor_daily", "close:2026-09-09", "weekly:2026-09-09",
                                      "monitor:2026-09-09", "factor_evaluation"])
        self.assertEqual(result["workflow"]["factor_ablation_evaluation"]["status"],
                         "continue_accumulating")

    def test_background_resume_retries_failed_research_and_reuses_core_checkpoints(self):
        from bridge import today_background
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifest = {"as_of": "2026-09-09", "trading_sessions": ["2026-09-09"],
                        "state_root": str(root), "job_root": str(root / "jobs"),
                        "budget_seconds": 30, "research_eligible": True}
            task = today_background.ensure_task(manifest, root=root / "background")
            package = lambda kind, status: job.evolution._package(kind, {"status": status})
            calls = []

            def daily(*_):
                calls.append("daily")
                if calls.count("daily") == 1:
                    raise TimeoutError("forced")
                return {"status": "completed", "path": str(root / "daily.json")}

            with patch.object(today_background, "_factor_daily", side_effect=daily), \
                 patch.object(today_background, "_factor_evaluation", side_effect=lambda *_:
                              calls.append("evaluation") or {"status": "continue_accumulating"}), \
                 patch.object(today_background.evolution, "run_close", side_effect=lambda *_:
                              calls.append("close") or package("close", "completed")), \
                 patch.object(today_background.evolution, "run_weekly", side_effect=lambda *_:
                              calls.append("weekly") or package("weekly", "completed")), \
                 patch.object(today_background.evolution, "monitoring_snapshot", side_effect=lambda **_:
                              calls.append("monitor") or package("monitor", "healthy")):
                first = today_background.run_task(task["task_id"], root=root / "background")
                second = today_background.run_task(task["task_id"], root=root / "background")
            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["stages"]["factor_ablation_daily"]["status"], "timed_out")
            self.assertEqual(second["status"], "completed")
            self.assertEqual(calls, ["daily", "close", "weekly", "monitor", "evaluation", "daily"])
            stages = today_background.read_status(task["task_id"], root=root / "background")["stages"]
            self.assertEqual(stages["factor_ablation_daily"]["status"], "completed")
            self.assertTrue(stages["close"]["input_sha256"])

    def test_resumed_worker_status_is_not_masked_by_previous_result(self):
        from bridge import today_background
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            task = today_background.ensure_task({"as_of": "2026-09-09", "job_root": str(root)},
                                                root=root / "background")
            directory = root / "background" / task["task_id"]
            today_background._write(directory / "result.json", {
                "status": "partial", "finished_at": "2026-09-09T16:00:00+08:00"})
            today_background.update_status(task["task_id"], root=root / "background",
                                           status="running", started_at="2026-09-09T16:01:00+08:00")
            status = today_background.read_status(task["task_id"], root=root / "background")
            self.assertEqual(status["status"], "running")

    def test_shared_series_loader_deduplicates_benchmark_and_sector(self):
        from analysis import recommendation_attribution as attribution
        loader = attribution.SharedSeriesLoader()
        stock_calls = []; benchmark_calls = []; sector_calls = []
        stock = {"data": [{"date": "2026-09-09", "close": 10}], "meta": {}}
        with patch("scans.stock_scanner._fetch_kline", side_effect=lambda *args, **kwargs:
                   stock_calls.append(args[0]) or stock), \
             patch("analysis.market_regime.fetch_index_kline", side_effect=lambda *args, **kwargs:
                   benchmark_calls.append(args[0]) or [{"date": "2026-09-09", "close": 1}]), \
             patch("fetchers.sector_kline.fetch_single_kline", side_effect=lambda *args, **kwargs:
                   sector_calls.append(args[0]) or [{"date": "2026-09-09", "close": 1}]):
            loader("600000", {"sector_code": "BK001"}, "2026-09-08", "2026-09-09")
            loader("600001", {"sector_code": "BK001"}, "2026-09-08", "2026-09-09")
        self.assertEqual(len(stock_calls), 2)
        self.assertEqual(benchmark_calls, ["000300.SH"])
        self.assertEqual(sector_calls, ["BK001"])
        self.assertEqual(loader.stats["cache_hits"], 2)


def run_today_tests():
    result = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(TodayTests))
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
