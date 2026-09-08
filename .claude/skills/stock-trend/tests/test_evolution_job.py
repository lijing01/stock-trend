"""P4 orchestration, publication, and recovery contracts."""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.evolution_job import monitoring_snapshot, run_close, run_weekly
from backtesting.recommendation_experiments import _bootstrap, default_experiment
from core.candidate_research_snapshot import build_research_snapshot, save_research_snapshot
from core.evolution_registry import (load_active_policy, publish_experiment, register_experiment,
                                     register_holdout_consumption, rollback_active_policy,
                                     transition, transition_experiment, verify_release_evidence,
                                     _verify_holdout_result, _verify_shadow_result,
                                     _verify_validation_result)
from core.recommendation_snapshot import build_snapshot, content_sha256


class T(unittest.TestCase):
    @staticmethod
    def _frozen_definition(sessions=None):
        sessions = sessions or [
            (date(2026, 1, 1) + timedelta(days=number)).isoformat()
            for number in range(400)
        ]
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

    def test_monitor_uses_business_date_and_latest_retry_not_hash_filename(self):
        with tempfile.TemporaryDirectory() as root:
            close = Path(root, "jobs", "close")
            close.mkdir(parents=True)
            runs = [
                ("zzzz", "2026-09-01", "2026-09-01T15:01:00+08:00", "failed"),
                ("aaaa", "2026-09-01", "2026-09-01T15:02:00+08:00", "completed"),
                ("yyyy", "2026-09-02", "2026-09-02T15:01:00+08:00", "completed"),
                ("bbbb", "2026-09-03", "2026-09-03T15:01:00+08:00", "completed"),
                ("xxxx", "2026-09-04", "2026-09-04T15:01:00+08:00", "completed"),
                ("cccc", "2026-09-05", "2026-09-05T15:01:00+08:00", "completed"),
            ]
            for name, as_of, completed_at, status in runs:
                Path(close, name + ".json").write_text(__import__("json").dumps({
                    "content": {"kind": "close", "as_of": as_of,
                                "completed_at": completed_at, "status": status}
                }), encoding="utf-8")
            result = monitoring_snapshot(
                job_root=Path(root, "jobs"), attribution_root=Path(root, "evaluations"),
                expected_trading_days=["2026-09-01", "2026-09-02", "2026-09-03",
                                       "2026-09-04", "2026-09-05"],
            )
        self.assertEqual(result["content"]["data_failure_rate"], 0.0)
        self.assertEqual(result["content"]["status"], "insufficient_data")
        self.assertEqual(result["content"]["close_run_days"], 5)

    def test_monitor_never_reports_healthy_without_tasks_or_mature_outcomes(self):
        with tempfile.TemporaryDirectory() as root:
            result = monitoring_snapshot(job_root=Path(root, "jobs"),
                                         attribution_root=Path(root, "evaluations"),
                                         expected_trading_days=[])
        self.assertEqual(result["content"]["status"], "insufficient_data")
        self.assertIn("close_runs_missing", result["content"]["insufficient_reasons"])
        self.assertIn("mature_outcomes_missing", result["content"]["insufficient_reasons"])

    def test_monitor_contract_failure_falls_back_once_without_touching_formal_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            jobs, releases, history = Path(root, "jobs", "close"), Path(root, "releases"), Path(root, "history")
            jobs.mkdir(parents=True); releases.mkdir(); history.mkdir()
            formal = history / "2026-09-01.json"; formal.write_text('{"formal":"unchanged"}', encoding="utf-8")
            pointer = {"experiment_id": "experiment-1", "definition": self._frozen_definition(),
                       "previous_experiment_id": "baseline",
                       "previous_definition": {"strict_level_1": 1, "strict_level_2": 3,
                                               "strict_level_3": 2}}
            (releases / "active_policy.json").write_text(__import__("json").dumps(pointer), encoding="utf-8")
            Path(jobs, "contract-error.json").write_text(__import__("json").dumps({"content": {
                "kind": "close", "as_of": "2026-09-01", "completed_at": "2026-09-01T15:00:00+08:00",
                "status": "failed", "failure_class": "contract"}}), encoding="utf-8")
            first = monitoring_snapshot(job_root=Path(root, "jobs"), attribution_root=Path(root, "evaluations"),
                                        release_root=releases, expected_trading_days=["2026-09-01"])
            second = monitoring_snapshot(job_root=Path(root, "jobs"), attribution_root=Path(root, "evaluations"),
                                         release_root=releases, expected_trading_days=["2026-09-01"])
            self.assertEqual(formal.read_text(encoding="utf-8"), '{"formal":"unchanged"}')
        self.assertEqual(first["content"]["status"], "fallback_required")
        self.assertEqual(first["content"]["fallback"]["status"], "recovered")
        self.assertEqual(second["content"]["fallback"]["status"], "already_baseline")

    def test_explicit_publish_and_rollback_keep_old_records_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            registry, releases = Path(root) / "registry", Path(root) / "releases"
            experiments, shadow = Path(root) / "experiments", Path(root) / "shadow"
            registered = register_experiment(self._frozen_definition(), registry)
            identifier = registered["experiment_id"]
            validation = self._release_result(identifier, "validation-1")
            shadow_result = self._shadow_result("shadow-1", identifier)
            experiments.mkdir()
            (experiments / "validation-1.json").write_text(
                __import__("json").dumps(validation), encoding="utf-8")
            shadow.mkdir()
            (shadow / "shadow-1.json").write_text(
                __import__("json").dumps(shadow_result), encoding="utf-8")
            holdout_probe = self._release_result(identifier, "holdout-1")
            consumption = register_holdout_consumption(
                identifier, "batch-1",
                holdout_probe["content"]["input_manifest"]["input_sha256"], "holdout-1",
                {"definition": default_experiment()}, registry)
            holdout = self._release_result(identifier, "holdout-1", consumption)
            (experiments / "holdout-1.json").write_text(
                __import__("json").dumps(holdout), encoding="utf-8")
            evidence = self._release_evidence(
                identifier, validation, holdout, shadow_result, consumption,
            )
            inline_evidence = dict(evidence, validation_result=validation)
            with self.assertRaisesRegex(ValueError, "embedded_result_forbidden"):
                verify_release_evidence(inline_evidence, identifier, registry,
                                        experiments, shadow)
            transition_experiment(identifier, "validated", {
                "validation_result_id": "validation-1",
            }, registry)
            transition_experiment(identifier, "shadow", {
                "shadow_result_id": "shadow-1",
            }, registry)
            transition_experiment(identifier, "eligible", evidence, registry)
            published = publish_experiment(identifier, evidence, registry, releases)
            self.assertEqual(published["status"], "published")
            self.assertEqual(load_active_policy(releases)["experiment_id"], identifier)
            rolled = rollback_active_policy({"operator": "test"}, releases)
            self.assertEqual(rolled["status"], "rolled_back_to_baseline")
            self.assertEqual(load_active_policy(releases)["experiment_id"], "baseline")

    @staticmethod
    def _release_result(experiment_id, result_id, consumption=None):
        sessions = [(date(2026, 1, 1) + timedelta(days=number)).isoformat()
                    for number in range(400)]
        partitions = {
            "status": "valid", "calendar_verified": True, "purge_sessions": 60,
            "freeze_at": sessions[0], "discovery": sessions[:10],
            "validation": {"validation_1": sessions[70:90],
                            "validation_2": sessions[160:180],
                            "validation_3": sessions[250:270]},
            "final_holdout": sessions[340:360],
            "all_dates": sessions[:10] + sessions[70:90] + sessions[160:180]
            + sessions[250:270] + sessions[340:360],
        }
        is_holdout = result_id.startswith("holdout")
        paired_days = sessions[340:360] if is_holdout else (
            sessions[70:90] + sessions[160:180] + sessions[250:270])
        blocks = (["holdout"] * len(paired_days) if is_holdout else
                  ["1"] * 20 + ["2"] * 20 + ["3"] * 20)
        paired = [{"date": day, "block": block,
                   "baseline_mean": 0.0, "treatment_mean": .01,
                   "delta": .01} for day, block in zip(paired_days, blocks)]
        coverage_rows = [{"date": day, "block": block,
                          "expected_date": True, "baseline_count": 1,
                          "treatment_count": 1, "baseline_complete": True,
                          "treatment_complete": True, "baseline_missing": [],
                          "treatment_missing": [], "status": "paired"}
                         for day, block in zip(paired_days, blocks)]
        event_rows, event_rows_60d = [], []
        event_days = paired_days
        for number in range(20 if is_holdout else 100):
            day = event_days[number % len(event_days)]
            position = sessions.index(day)
            event_rows.append({"record_id": f"{result_id}-event-{number:03d}",
                               "market": "SH", "code": f"{number:06d}",
                               "entry_date": day, "exit_date": sessions[position + 19],
                               "recommendation_date": day,
                               "hs300_alpha": 0.0, "mae": -.03,
                               "evaluation_complete": True})
            if not is_holdout:
                event_rows_60d.append({"record_id": f"{result_id}-event-{number:03d}",
                                       "market": "SH", "code": f"{number:06d}",
                                       "entry_date": day, "exit_date": sessions[position + 59],
                                       "recommendation_date": day,
                                       "hs300_alpha": 0.0, "mae": -.03,
                                       "evaluation_complete": True})
        event_treatment = [dict(row, record_id=row["record_id"] + "-t",
                                code=f"T{number:05d}", hs300_alpha=.01)
                           for number, row in enumerate(event_rows)]
        event60_treatment = [dict(row, record_id=row["record_id"] + "-t",
                                  code=f"T{number:05d}", hs300_alpha=.01)
                             for number, row in enumerate(event_rows_60d)]
        for row in coverage_rows:
            row["baseline_count"] = sum(
                event["recommendation_date"] == row["date"] for event in event_rows)
            row["treatment_count"] = sum(
                event["recommendation_date"] == row["date"] for event in event_treatment)
        mae_rows = {
            "baseline": [{"record_id": row["record_id"],
                           "recommendation_date": row["recommendation_date"],
                           "market": row["market"], "code": row["code"],
                           "mae": row["mae"], "side": "baseline",
                           "evaluation_status": "complete"}
                          for row in event_rows],
            "treatment": [{"record_id": row["record_id"],
                            "recommendation_date": row["recommendation_date"],
                            "market": row["market"], "code": row["code"],
                            "mae": row["mae"], "side": "treatment",
                            "evaluation_status": "complete"}
                           for row in event_treatment],
        }
        if is_holdout:
            interval = {}
            block_counts = {"holdout": 20}
            maturity = {"baseline": {"valid_alpha_events": 20, "alpha_mature_dates": 20},
                        "treatment": {"valid_alpha_events": 20, "alpha_mature_dates": 20},
                        "valid_alpha_events": 20, "alpha_mature_dates": 20}
        else:
            interval = _bootstrap([.01] * 60, seed=20260907, draws=2000,
                                  block_length=20, dates=paired_days,
                                  partitions=partitions["validation"],
                                  trading_sessions=sessions)
            block_counts = {"1": 20, "2": 20, "3": 20}
            maturity = {
                "baseline": {"valid_alpha_events": 100, "alpha_mature_dates": 60},
                "treatment": {"valid_alpha_events": 100, "alpha_mature_dates": 60},
                "confirmation_60d": {
                    "baseline_valid_alpha_events": 100,
                    "treatment_valid_alpha_events": 100,
                    "complete_paired_dates": 60, "mean_delta": .01,
                },
                "valid_alpha_events": 100, "alpha_mature_dates": 60,
            }
        marker = {"status": "consumed" if consumption else "unconsumed",
                  "dates": sessions[340:360], "freeze_at": sessions[0]}
        if consumption:
            marker.update({"consumption_id": consumption["consumption_id"],
                           "research_batch_id": consumption["research_batch_id"]})
        definition = T._frozen_definition(sessions)
        role = "final_holdout" if is_holdout else "validation"
        candidate_items = []
        baseline_60_by_code = {row["code"]: row for row in event_rows_60d}
        treatment_60_by_code = {row["code"]: row for row in event60_treatment}
        for row in event_rows + event_treatment:
            row60 = (baseline_60_by_code if row in event_rows else treatment_60_by_code).get(row["code"])
            windows = {
                "20": {"status": "complete", "entry_date": row["entry_date"],
                       "exit_date": row["exit_date"], "hs300_alpha": row["hs300_alpha"],
                       "mae": row.get("mae")},
            }
            if row60 is not None:
                windows["60"] = {"status": "complete", "entry_date": row60["entry_date"],
                                 "exit_date": row60["exit_date"],
                                 "hs300_alpha": row60["hs300_alpha"],
                                 "mae": row60.get("mae")}
            candidate_items.append({
                "record_id": row["record_id"],
                "recommendation_date": row["recommendation_date"],
                "market": row["market"], "code": row["code"],
                "contract_id": definition["contract_id"],
                "evaluation_contract": {"contract_id": definition["contract_id"]},
                "windows": windows,
            })
        candidate_items.sort(key=lambda item: (item["recommendation_date"], item["market"],
                                                item["code"], item["record_id"]))
        manifest_inputs = {
            "partition_role": role,
            "partition_dates": paired_days,
            "partition_dates_sha256": content_sha256(paired_days),
            "research_run_ids": ["synthetic-run"],
            "research_content_sha256": ["synthetic-content"],
            "candidate_signal_items": candidate_items,
            "definition": definition,
            "primary_window": 20,
            "confirmation_window": 60,
            "trading_sessions": sessions,
        }
        manifest_digest = content_sha256(manifest_inputs)
        content = {
            "schema_version": "recommendation-experiment/v2",
            "definition": definition, "experiment_id": experiment_id,
            "contract_id": definition["contract_id"], "partition_role": role,
            "calendar_verified": True, "trading_sessions": sessions,
            "partitions": partitions, "maturity": maturity,
            "paired_dates": paired,
            "confirmation_60d_pairs": ([] if is_holdout else [
                {"date": day, "block": block, "baseline_complete": True,
                 "treatment_complete": True, "baseline_mean": 0.0,
                 "treatment_mean": .01, "delta": .01}
                for day, block in zip(paired_days, blocks)]),
            "block_pair_counts": block_counts,
            "coverage": {"baseline_dates": len(paired), "treatment_dates": len(paired),
                          "baseline_complete_ratio": 1.0,
                          "treatment_complete_ratio": 1.0},
            "interval": interval, "mae_tail_5pct": {"baseline": -.03, "treatment": -.03},
            "coverage_rows": coverage_rows,
            "event_rows": {"baseline": event_rows, "treatment": event_treatment,
                            "baseline_60d": event_rows_60d,
                            "treatment_60d": event60_treatment},
            "mae_rows": mae_rows, "holdout": marker,
            "promotion": {"eligible": True},
            "input_manifest": {
                "schema_version": "recommendation-evolution-storage/v1",
                "archive_mode": "embedded_or_immutable_reference",
                "inputs": manifest_inputs,
                "input_sha256": manifest_digest,
                **({"derived_input_sha256": manifest_digest} if is_holdout else {}),
                "credential_policy": "no_model_credentials_stored",
            },
        }
        if consumption:
            content["input_manifest"].update({
                "holdout_consumption_id": consumption["consumption_id"],
                "holdout_research_batch_id": consumption["research_batch_id"],
                "holdout_input_manifest_sha256": consumption["input_manifest_sha256"],
            })
        return {"experiment_id": result_id,
                "content_sha256": content_sha256(content), "content": content}

    @staticmethod
    def _shadow_result(result_id, experiment_id="experiment-1"):
        sessions = [(date(2026, 1, 1) + timedelta(days=number)).isoformat()
                    for number in range(400)]
        paired_days = sessions[:20]
        coverage_rows = [{"date": day, "block": "shadow", "baseline_count": 5,
                          "treatment_count": 5, "expected_date": True,
                          "baseline_complete": True,
                          "treatment_complete": True, "status": "paired"}
                         for day in paired_days]
        event_rows = [{"record_id": f"{result_id}-event-{number:03d}",
                       "market": "SH", "code": f"{number:06d}",
                       "entry_date": paired_days[number % 20],
                       "exit_date": sessions[number % 20 + 19],
                       "recommendation_date": paired_days[number % 20],
                       "hs300_alpha": 0.0, "evaluation_complete": True}
                      for number in range(100)]
        source_runs = []
        for number, day in enumerate(paired_days):
            baseline_selected = [
                {"market": "SH", "code": f"{number + 20 * offset:06d}"}
                for offset in range(5)
            ]
            treatment_selected = [dict(item) for item in baseline_selected]
            selection = {
                "top": 5, "min_score": 50.0, "policy": {"mode": "actionable"},
                "baseline": {"selected": baseline_selected,
                              "buckets": {"actionable": baseline_selected}},
                "treatment": {"selected": treatment_selected,
                               "buckets": {"actionable": treatment_selected}},
            }
            source_input = {"basis_date": day, "selection": selection}
            source_snapshot = {
                "schema_version": "recommendation-strategy-shadow/v1",
                "basis_date": day, "snapshot_type": "formal",
                "definition": T._frozen_definition(sessions),
                "experiment_id": experiment_id,
                "contract_id": T._frozen_definition(sessions)["contract_id"],
                "shadow_type": "forward", "retrospective": False,
                "independent_snapshot": True, "formal_policy_affected": False,
                "selection": selection,
                "input_manifest": {"inputs": source_input,
                                    "input_sha256": content_sha256(source_input)},
            }
            source_runs.append({"basis_date": day, "snapshot_type": "formal",
                                "content_sha256": content_sha256(source_snapshot),
                                "input_sha256": content_sha256(source_input),
                                "selection": selection,
                                "snapshot": source_snapshot})
        shadow_inputs = {"partition_role": "forward_shadow", "source_runs": source_runs}
        content = {
            "schema_version": "recommendation-shadow/v1",
            "shadow_type": "forward", "formal_policy_affected": False,
            "retrospective": False, "independent_snapshot": True,
            "evaluation_status": "forward_aggregated",
            "experiment_id": experiment_id,
            "definition": T._frozen_definition(sessions),
            "contract_id": T._frozen_definition(sessions)["contract_id"],
            "complete_paired_dates": 20, "valid_alpha_events": 100,
            "mean_delta": .01,
            "paired_dates": [{"date": day, "baseline_mean": 0.0,
                               "treatment_mean": .01, "delta": .01}
                              for day in paired_days],
            "coverage": {"baseline_dates": 20, "treatment_dates": 20,
                          "baseline_complete_ratio": 1.0,
                          "treatment_complete_ratio": 1.0},
            "source_runs": source_runs,
            "input_manifest": {"input_sha256": content_sha256(shadow_inputs),
                                "inputs": shadow_inputs},
            "trading_sessions": sessions,
            "coverage_rows": coverage_rows,
            "event_rows": {"baseline": event_rows,
                            "treatment": [dict(row, record_id=row["record_id"] + "-t",
                                                hs300_alpha=.01)
                                          for row in event_rows]},
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
            "contract_id": T._frozen_definition()["contract_id"],
            "holdout_consumption": consumption,
        }

    def test_bare_evidence_cannot_reach_eligible(self):
        registered = register_experiment(self._frozen_definition(), tempfile.mkdtemp())
        record = {
            "experiment_id": registered["experiment_id"],
            "content": {
                "schema_version": "evolution-registry/v2",
                "definition": self._frozen_definition(),
                "state": "shadow", "history": [],
            },
        }
        with self.assertRaises(ValueError):
            transition(record, "eligible", {"result": "ok"})

    def test_validation_release_requires_raw_confirmation_rows(self):
        result = self._release_result("experiment-1", "validation-1")
        result["content"].pop("confirmation_60d_pairs")
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "confirmation_rows_missing"):
            _verify_validation_result(result, "validation-1", require_eligible=True)

    def test_validation_release_recomputes_means_from_event_alpha(self):
        result = self._release_result("experiment-1", "validation-1")
        result["content"]["event_rows"]["baseline"][0]["hs300_alpha"] = .5
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "(paired_mean_mismatch|alpha_input_mismatch)"):
            _verify_validation_result(result, "validation-1", require_eligible=True)

    def test_release_manifest_must_bind_production_candidate_inputs(self):
        result = self._release_result("experiment-1", "validation-1")
        result["content"]["input_manifest"]["inputs"] = {
            "partition_role": "validation",
            "partition_dates": result["content"]["paired_dates"],
        }
        result["content"]["input_manifest"]["input_sha256"] = content_sha256(
            result["content"]["input_manifest"]["inputs"])
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "manifest_inputs_incomplete"):
            _verify_validation_result(result, "validation-1", require_eligible=True)

    def test_shadow_event_must_match_source_selection_identity(self):
        result = self._shadow_result("shadow-1")
        result["content"]["event_rows"]["baseline"][0]["code"] = "FAKE"
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "shadow_selection_event_mismatch"):
            _verify_shadow_result(result, "shadow-1")

    def test_shadow_selection_requires_every_selected_event(self):
        result = self._shadow_result("shadow-1")
        source = result["content"]["source_runs"][0]
        extra = {"market": "SH", "code": "EXTRA"}
        baseline = source["selection"]["baseline"]
        baseline["selected"] = [*baseline["selected"], extra]
        baseline["buckets"]["actionable"] = [*baseline["buckets"]["actionable"], dict(extra)]
        source_input = source["snapshot"]["input_manifest"]["inputs"]
        input_digest = content_sha256(source_input)
        source["snapshot"]["input_manifest"]["input_sha256"] = input_digest
        source["input_sha256"] = input_digest
        source["content_sha256"] = content_sha256(source["snapshot"])
        result["content"]["coverage_rows"][0]["baseline_count"] = 6
        shadow_inputs = result["content"]["input_manifest"]["inputs"]
        result["content"]["input_manifest"]["input_sha256"] = content_sha256(shadow_inputs)
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "shadow_selection_event_set_mismatch"):
            _verify_shadow_result(result, "shadow-1")

    def test_shadow_selection_requires_mature_event(self):
        result = self._shadow_result("shadow-1")
        result["content"]["event_rows"]["baseline"][0]["evaluation_complete"] = False
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "shadow_selection_event_incomplete"):
            _verify_shadow_result(result, "shadow-1")

    def test_registry_requires_a_frozen_calendar_and_partition_definition(self):
        with self.assertRaisesRegex(ValueError, "trading_sessions_required"):
            register_experiment(default_experiment(), tempfile.mkdtemp())

    def test_holdout_release_requires_consumed_marker(self):
        consumption = {"consumption_id": "c1", "research_batch_id": "batch-1",
                       "input_manifest_sha256": "holdout-input", "result_id": "holdout-1"}
        result = self._release_result("experiment-1", "holdout-1", consumption)
        result["content"].pop("holdout")
        result["content_sha256"] = content_sha256(result["content"])
        with self.assertRaisesRegex(ValueError, "holdout_result_not_consumed"):
            _verify_holdout_result(result, "holdout-1")

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

    def test_holdout_consumption_claim_is_global_across_experiments(self):
        with tempfile.TemporaryDirectory() as root:
            register_holdout_consumption(
                "experiment-1", "batch-1", "input-1", "result-1", {}, root)
            with self.assertRaisesRegex(ValueError, "holdout_already_consumed"):
                register_holdout_consumption(
                    "experiment-2", "batch-1", "input-1", "result-2", {}, root)
            with self.assertRaisesRegex(ValueError, "holdout_already_consumed"):
                register_holdout_consumption(
                    "experiment-3", "batch-2", "input-1", "result-3", {}, root)
            claims = list((Path(root) / "holdout_consumption" / "claims").glob("*.json"))
            self.assertEqual(len(claims), 1)

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

    def test_cold_start_offline_loop_stops_before_release_and_monitor_is_explicitly_insufficient(self):
        with tempfile.TemporaryDirectory() as root:
            research_root = Path(root) / "research"
            save_research_snapshot(self._research("2026-09-01", "A"), research_root)
            weekly = run_weekly(
                "2026-09-01", research_root=research_root,
                attribution_root=Path(root) / "evaluations",
                diagnostics_root=Path(root) / "diagnostics",
                proposal_root=Path(root) / "proposals",
            )
            monitor = monitoring_snapshot(
                job_root=Path(root) / "jobs", attribution_root=Path(root) / "evaluations",
                expected_trading_days=["2026-09-01"], as_of="2026-09-01",
                release_root=Path(root) / "releases",
            )
            self.assertEqual(weekly["content"]["status"], "completed")
            self.assertEqual(monitor["content"]["status"], "insufficient_data")
            self.assertFalse((Path(root) / "releases" / "active_policy.json").exists())


def run_evolution_job_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__": unittest.main()
