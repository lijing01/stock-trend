import tempfile
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.evolution_proposals import (build_proposal_run, claim_weekly_attempt,
                                           generate_weekly_proposals,
                                           record_weekly_attempt_response, resume_weekly_attempt,
                                           save_proposal_run, validate_proposals)
from analysis.recommendation_diagnostics import (
    build_diagnostics, load_candidate_signal_items, load_primary_research_snapshots, save_diagnostics,
)
from core.recommendation_snapshot import content_sha256


def _snapshot(day="2026-08-20", records=None, formal=True):
    return {"content": {
        "recommendation_date": day, "snapshot_type": "formal" if formal else "provisional",
        "official_snapshot": {"link_status": "linked" if formal else "unlinked"},
        "market_regime": {"score": 82},
        "records": records or [{"code": "600000", "basis_date": day, "final_status": "actionable",
          "scores": {"buy_point_level": "strict_level_2", "quality_adjusted_score": 88},
          "sector_persistence": {"sector_persistence_status": "actionable"}, "candidate": {}}],
    }}


def _outcome(day="2026-08-20", code="600000", status="complete"):
    return {"recommendation_date": day, "code": code, "windows": {"20": {
        "status": status, "signal_return": .12, "hs300_alpha": .08, "mae": -.04,
        "entry_date": "2026-08-21", "exit_date": "2026-09-17"}}}


class T(unittest.TestCase):
    def _refresh_diagnostics_digest(self, diagnostics):
        diagnostics["content_sha256"] = content_sha256(diagnostics["content"])
        diagnostics["diagnostic_id"] = diagnostics["content_sha256"][:16]

    def _valid_response(self, evidence_id="selection_status:actionable"):
        return {"proposals": [{
            "hypothesis": "test", "evidence_ids": [evidence_id],
            "counterexample": "weak dates", "change": {
                "variable": "buy_point_priority_bonus",
                "values": {"strict_level_2": 1},
            }, "expected_direction": "higher alpha",
            "failure_condition": "no lift", "validation_plan": "walk forward",
        }]}

    def test_single_dimension_diagnostics_keep_missing_outcomes_visible(self):
        records = _snapshot()["content"]["records"] + [{
            "code": "600001", "basis_date": "2026-08-20", "final_status": "observation",
            "scores": {"buy_point_level": "unknown", "quality_adjusted_score": 55},
            "sector_persistence": {"sector_persistence_status": "not_actionable"}, "candidate": {}}]
        result = build_diagnostics([_snapshot(records=records)], [_outcome()])
        overall = result["content"]["overall"]
        self.assertEqual(overall["records"], 2)
        self.assertEqual(overall["mature_events"], 1)
        self.assertEqual(overall["missing_outcome"], 1)
        group = result["content"]["dimensions"]["quality_score_band"]["high_80_plus"]
        self.assertAlmostEqual(group["mean_hs300_alpha"], .08)
        self.assertAlmostEqual(group["mean_mae"], -.04)
        self.assertIn("相关", result["content"]["correlation_notice"])

    def test_ineligible_and_provisional_samples_are_not_used(self):
        result = build_diagnostics([_snapshot(formal=False)], [_outcome()])
        self.assertEqual(result["content"]["overall"]["records"], 0)
        self.assertEqual(result["content"]["input"]["skipped_ineligible_records"], 1)

    def test_diagnostics_save_and_primary_loader_are_idempotent(self):
        result = build_diagnostics([_snapshot()], [_outcome()])
        with tempfile.TemporaryDirectory() as root:
            first = save_diagnostics(result, root)
            second = save_diagnostics(result, root)
            self.assertEqual((first["status"], second["status"]), ("created", "unchanged"))
            research = Path(root) / "research" / "2026-08-20" / "formal"
            research.mkdir(parents=True)
            (research / "primary.json").write_text("sample", encoding="utf-8")
            import json
            (research / "sample.json").write_text(json.dumps(_snapshot()), encoding="utf-8")
            self.assertEqual(len(load_primary_research_snapshots(Path(root) / "research")), 1)

    def test_outcome_loader_rejects_mixed_contracts_but_empty_is_valid(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(load_candidate_signal_items(root), [])
            Path(root, "a").mkdir(); Path(root, "b").mkdir()
            with self.assertRaisesRegex(ValueError, "contract_id"):
                load_candidate_signal_items(root)

    def test_legacy_sidecar_is_readable_but_not_point_in_time_qualified(self):
        with tempfile.TemporaryDirectory() as root:
            contract = Path(root) / "legacy-contract"
            contract.mkdir()
            (contract / "2026-08-20.json").write_text(
                __import__("json").dumps({"candidate_signal_items": [_outcome()]}),
                encoding="utf-8")
            items = load_candidate_signal_items(root)
        self.assertEqual(items[0]["point_in_time_status"], "legacy_unverified")
        diagnostics = build_diagnostics([_snapshot()], items)
        self.assertEqual(diagnostics["content"]["overall"]["mature_events"], 0)
        self.assertEqual(diagnostics["content"]["overall"]["legacy_unverified"], 1)

    def test_future_v2_evaluation_partition_is_excluded_by_cutoff(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "contract" / "v2" / "2026-10-01"
            path.mkdir(parents=True)
            payload = {"evaluation_version": "v2", "evaluation_as_of": "2026-10-01",
                       "candidate_signal_items": [_outcome()]}
            (path / "2026-08-20.json").write_text(__import__("json").dumps(payload), encoding="utf-8")
            self.assertEqual(load_candidate_signal_items(root, "contract", as_of="2026-09-01"), [])

    def test_cutoff_loader_selects_latest_evaluation_per_recommendation_date(self):
        with tempfile.TemporaryDirectory() as root:
            for as_of, alpha in (("2026-08-27", .1), ("2026-09-01", .2)):
                path = Path(root) / "contract" / "v2" / as_of
                path.mkdir(parents=True)
                item = _outcome()
                item["windows"]["20"]["hs300_alpha"] = alpha
                payload = {"evaluation_version": "v2", "evaluation_as_of": as_of,
                           "recommendation_date": "2026-08-20",
                           "candidate_signal_items": [item]}
                (path / "2026-08-20.json").write_text(__import__("json").dumps(payload), encoding="utf-8")
            items = load_candidate_signal_items(root, "contract", as_of="2026-09-02")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["windows"]["20"]["hs300_alpha"], .2)

    def test_evaluation_conflict_is_audit_visible_but_not_mature(self):
        item = _outcome()
        item["conflicts"] = [{"window": "20", "existing_sha256": "a", "incoming_sha256": "b"}]
        result = build_diagnostics([_snapshot()], [item])
        overall = result["content"]["overall"]
        self.assertEqual(overall["mature_events"], 0)
        self.assertEqual(overall["evaluation_conflicts"], 1)

    def test_v2_outcome_must_bind_to_research_record_and_snapshot(self):
        research = _snapshot()
        research["content_sha256"] = "research-snapshot-hash"
        research["content"]["records"][0]["record_id"] = "frozen-record"
        outcome = _outcome()
        outcome.update({"record_id": "different-record",
                        "research_snapshot_sha256": "other-hash",
                        "population_kind": "official_candidate_population"})
        result = build_diagnostics([research], [outcome])
        self.assertEqual(result["content"]["overall"]["mature_events"], 0)
        self.assertEqual(result["content"]["overall"]["missing_outcome"], 1)

    def test_proposal_gate_requires_valid_alpha_dates(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        diagnostics["content"]["overall"].update({"mature_events": 100,
                                                   "valid_alpha_events": 100,
                                                   "mature_dates": 20,
                                                   "valid_alpha_dates": 1,
                                                   "alpha_mature_dates": 1})
        self._refresh_diagnostics_digest(diagnostics)
        run = build_proposal_run(diagnostics, self._valid_response())
        self.assertEqual(run["content"]["status"], "continue_accumulating")
        self.assertEqual(run["content"]["rejected"], ["overall_research_not_ready"])

    def test_proposal_gate_boundary_counts_are_strict(self):
        for events, dates, expected in (
            (99, 20, "continue_accumulating"),
            (100, 19, "continue_accumulating"),
            (100, 20, "no_valid_proposal"),
        ):
            diagnostics = build_diagnostics([_snapshot()], [])
            diagnostics["content"]["overall"].update({
                "valid_alpha_events": events,
                "alpha_mature_dates": dates,
            })
            self._refresh_diagnostics_digest(diagnostics)
            run = build_proposal_run(diagnostics, self._valid_response())
            self.assertEqual(run["content"]["status"], expected)

    def test_group_proposal_gate_requires_thirty_deduplicated_events(self):
        def build_group(size):
            records, outcomes = [], []
            for number in range(size):
                code = f"{600000 + number}"
                records.append({"code": code, "basis_date": "2026-08-20",
                    "final_status": "actionable", "scores": {
                        "buy_point_level": "strict_level_2", "quality_adjusted_score": 88},
                    "sector_persistence": {"sector_persistence_status": "actionable"},
                    "candidate": {}})
                outcomes.append(_outcome("2026-08-20", code))
            return build_diagnostics([_snapshot(records=records)], outcomes)

        for size, expected in ((29, False), (30, True)):
            diagnostics = build_group(size)
            evidence = diagnostics["content"]["evidence_index"]["selection_status:actionable"]
            self.assertEqual(evidence["proposal_eligible"], expected)

    def test_unverified_diagnostics_cannot_form_proposal(self):
        bare = {"overall": {"mature_events": 100, "mature_dates": 20},
                "evidence_index": {"selection_status:actionable": {"proposal_eligible": True}}}
        run = build_proposal_run(bare, self._valid_response())
        self.assertEqual(run["content"]["status"], "continue_accumulating")
        self.assertEqual(run["content"]["rejected"], ["overall_research_not_ready"])

    def test_proposals_reject_bad_evidence_and_out_of_scope_change(self):
        records = []
        outcomes = []
        for number in range(30):
            day = f"2026-07-{number + 1:02d}" if number < 31 else "2026-08-01"
            code = f"{600000 + number}"
            records.append({"code": code, "basis_date": day, "final_status": "actionable",
                "scores": {"buy_point_level": "strict_level_2", "quality_adjusted_score": 88},
                "sector_persistence": {"sector_persistence_status": "actionable"}, "candidate": {}})
            outcomes.append(_outcome(day, code))
        diagnostics = build_diagnostics([_snapshot(records=records)], outcomes)
        response = {"proposals": [{"hypothesis": "test", "evidence_ids": ["buy_point_level:strict_level_2"],
          "counterexample": "weak dates", "change": {"variable": "market_gate", "values": {}},
          "expected_direction": "higher alpha", "failure_condition": "no lift", "validation_plan": "walk forward"}]}
        valid, errors = validate_proposals(diagnostics, response)
        self.assertEqual(valid, [])
        self.assertIn("parameter_out_of_scope", errors[0])
        run = build_proposal_run(diagnostics, response)
        self.assertEqual(run["content"]["status"], "continue_accumulating")

    def test_no_mature_samples_says_continue_accumulating(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        run = build_proposal_run(diagnostics)
        self.assertEqual(run["content"]["status"], "continue_accumulating")

    def test_model_failure_does_not_block_offline_research_result(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        run = generate_weekly_proposals(diagnostics, lambda material: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertEqual(run["content"]["status"], "continue_accumulating")
        self.assertEqual(run["content"]["adapter_status"], "model_not_attempted")

    def test_overall_gate_precedes_valid_group_response(self):
        records, outcomes = [], []
        for number in range(30):
            code = f"{600000 + number}"
            records.append({"code": code, "basis_date": "2026-08-20",
                "final_status": "actionable", "scores": {
                    "buy_point_level": "strict_level_2", "quality_adjusted_score": 88},
                "sector_persistence": {"sector_persistence_status": "actionable"},
                "candidate": {}})
            outcomes.append(_outcome("2026-08-20", code))
        diagnostics = build_diagnostics([_snapshot(records=records)], outcomes)
        run = build_proposal_run(diagnostics, self._valid_response("selection_status:actionable"))
        self.assertEqual(run["content"]["status"], "continue_accumulating")
        self.assertEqual(run["content"]["proposals"], [])
        self.assertEqual(run["content"]["rejected"], ["overall_research_not_ready"])

    def test_cutoff_keeps_late_label_pending_and_future_recommendation_out(self):
        late = _snapshot(day="2026-08-20")
        future = _snapshot(day="2026-09-20")
        outcome = _outcome("2026-08-20")
        outcome["windows"]["20"]["exit_date"] = "2026-09-20"
        result = build_diagnostics([late, future], [outcome], evaluation_as_of="2026-09-10")
        self.assertEqual(result["content"]["overall"]["records"], 1)
        self.assertEqual(result["content"]["overall"]["pending"], 1)
        self.assertEqual(result["content"]["overall"]["mature_events"], 0)

    def test_proposal_budget_counts_distinct_runs_and_is_idempotent(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        # No mature proposals are countable; verify the storage API's
        # idempotence separately with a synthetic ready diagnostics envelope.
        diagnostics["content"]["overall"].update({"mature_events": 100,
                                                   "valid_alpha_events": 100,
                                                   "mature_dates": 20,
                                                   "alpha_mature_dates": 20,
                                                   "valid_alpha_dates": 20})
        diagnostics["content"]["evidence_index"] = {
            "selection_status:actionable": {"proposal_eligible": True}}
        self._refresh_diagnostics_digest(diagnostics)
        with tempfile.TemporaryDirectory() as root:
            runs = []
            for number in range(3):
                response = self._valid_response()
                response["proposals"][0]["hypothesis"] = str(number)
                run = build_proposal_run(diagnostics, response, week=__import__("datetime").date(2026, 9, 7))
                runs.append(save_proposal_run(run, root))
            duplicate = save_proposal_run(
                build_proposal_run(diagnostics, self._valid_response(),
                                   week=__import__("datetime").date(2026, 9, 7)), root)
        self.assertEqual([item["status"] for item in runs], ["created", "created", "created"])
        self.assertEqual(duplicate["status"], "budget_exhausted")

    def test_same_proposal_run_save_is_idempotent(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        diagnostics["content"]["overall"].update({
            "valid_alpha_events": 100, "alpha_mature_dates": 20,
        })
        diagnostics["content"]["evidence_index"] = {
            "selection_status:actionable": {"proposal_eligible": True}}
        self._refresh_diagnostics_digest(diagnostics)
        run = build_proposal_run(diagnostics, self._valid_response(),
                                 week=__import__("datetime").date(2026, 9, 7))
        with tempfile.TemporaryDirectory() as root:
            first = save_proposal_run(run, root)
            second = save_proposal_run(run, root)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "unchanged")

    def test_weekly_attempt_claim_is_serialized(self):
        identity = {"diagnostic_id": "diag", "diagnostic_sha256": "digest",
                    "material_sha256": "material"}
        week = __import__("datetime").date(2026, 9, 7)

        def claim(number, root):
            return claim_weekly_attempt(
                root, week, "generation", attempt_id=f"attempt-{number}",
                input_identity=identity)

        with tempfile.TemporaryDirectory() as root:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda number: claim(number, root), range(4)))
        statuses = [result["status"] for result in results]
        self.assertEqual(statuses.count("claimed"), 1)
        self.assertEqual(statuses.count("budget_exhausted"), 3)

    def test_parameter_values_reject_bool_nan_inf_and_extra_fields(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        evidence_id = "selection_status:actionable"
        diagnostics["content"]["evidence_index"] = {evidence_id: {"proposal_eligible": True}}
        base = self._valid_response(evidence_id)["proposals"][0]
        for value in (True, float("nan"), float("inf")):
            response = {"proposals": [{**base, "change": {
                "variable": "buy_point_priority_bonus",
                "values": {"strict_level_2": value},
            }}]}
            valid, errors = validate_proposals(diagnostics, response)
            self.assertEqual(valid, [])
            self.assertIn("parameter_out_of_bounds", errors[0])
        response = {"proposals": [{**base, "change": {
            "variable": "buy_point_priority_bonus", "values": {"strict_level_2": 1},
            "unexpected": True,
        }}]}
        valid, errors = validate_proposals(diagnostics, response)
        self.assertEqual(valid, [])
        self.assertIn("parameter_out_of_scope", errors[0])

    def test_tampered_diagnostics_cannot_be_saved_as_proposal_run(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        run = build_proposal_run(diagnostics)
        run["content"]["status"] = "proposed"
        with self.assertRaisesRegex(ValueError, "proposal_run_digest"):
            save_proposal_run(run, tempfile.mkdtemp())

    def test_model_attempt_is_budgeted_once_and_not_repeated_after_failure(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        diagnostics["content"]["overall"].update({"mature_events": 100,
                                                   "valid_alpha_events": 100,
                                                   "mature_dates": 20,
                                                   "alpha_mature_dates": 20,
                                                   "valid_alpha_dates": 20})
        self._refresh_diagnostics_digest(diagnostics)
        calls = []
        def adapter(_material):
            calls.append(True)
            raise RuntimeError("offline")
        with tempfile.TemporaryDirectory() as root:
            first = generate_weekly_proposals(
                diagnostics, adapter, week=__import__("datetime").date(2026, 9, 7), root=root)
            second = generate_weekly_proposals(
                diagnostics, adapter, week=__import__("datetime").date(2026, 9, 7), root=root)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["content"]["usage"]["generation_status"], "failed")
        self.assertEqual(second["content"]["adapter_status"], "weekly_budget_exhausted")

    def test_attempt_response_can_be_recorded_and_replayed_by_id(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        diagnostics["content"]["overall"].update({"valid_alpha_events": 100,
                                                   "alpha_mature_dates": 20})
        self._refresh_diagnostics_digest(diagnostics)
        response = self._valid_response()
        calls = []
        def adapter(_material):
            calls.append(True)
            return response
        with tempfile.TemporaryDirectory() as root:
            first = generate_weekly_proposals(diagnostics, adapter,
                week=__import__("datetime").date(2026, 9, 7), root=root)
            attempt_id = first["content"]["generation_attempt_id"]
            recorded = record_weekly_attempt_response(root, __import__("datetime").date(2026, 9, 7),
                "generation", attempt_id, response)
            resumed = resume_weekly_attempt(root, __import__("datetime").date(2026, 9, 7),
                "generation", attempt_id)
            replay = generate_weekly_proposals(diagnostics, adapter,
                week=__import__("datetime").date(2026, 9, 7), root=root,
                attempt_id=attempt_id)
        self.assertEqual(recorded["status"], "unchanged")
        self.assertEqual(resumed["response_sha256"], recorded["response_sha256"])
        self.assertEqual(replay["content"]["adapter_status"], "replayed")
        self.assertEqual(len(calls), 1)

    def test_attempt_replay_is_bound_to_diagnostic_and_material_digest(self):
        diagnostics_a = build_diagnostics([_snapshot(day="2026-08-20")], [])
        diagnostics_b = build_diagnostics([_snapshot(day="2026-08-21")], [])
        for diagnostics in (diagnostics_a, diagnostics_b):
            diagnostics["content"]["overall"].update({
                "valid_alpha_events": 100, "alpha_mature_dates": 20,
            })
        diagnostics_b["content"]["correlation_notice"] = "different diagnostic input"
        for diagnostics in (diagnostics_a, diagnostics_b):
            self._refresh_diagnostics_digest(diagnostics)
        calls = []

        def adapter(_material):
            calls.append(True)
            return {"proposals": []}

        with tempfile.TemporaryDirectory() as root:
            first = generate_weekly_proposals(
                diagnostics_a, adapter, week=__import__("datetime").date(2026, 9, 7), root=root)
            attempt_id = first["content"]["generation_attempt_id"]
            mismatch = generate_weekly_proposals(
                diagnostics_b, adapter, week=__import__("datetime").date(2026, 9, 7), root=root,
                attempt_id=attempt_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(mismatch["content"]["adapter_status"],
                         "weekly_attempt_identity_mismatch")
        self.assertEqual(mismatch["content"]["usage"]["generation_status"],
                         "identity_mismatch")

    def test_replay_prefers_completed_repair_response(self):
        diagnostics = build_diagnostics([_snapshot()], [])
        diagnostics["content"]["overall"].update({
            "valid_alpha_events": 100, "alpha_mature_dates": 20,
        })
        diagnostics["content"]["evidence_index"] = {
            "selection_status:actionable": {"proposal_eligible": True}}
        self._refresh_diagnostics_digest(diagnostics)
        calls = {"generate": 0, "repair": 0}

        class Adapter:
            def generate(self, _material):
                calls["generate"] += 1
                return {"malformed": True}

            def repair(self, _material, _response, _errors):
                calls["repair"] += 1
                return self_response

        self_response = self._valid_response()
        with tempfile.TemporaryDirectory() as root:
            first = generate_weekly_proposals(
                diagnostics, Adapter(), week=__import__("datetime").date(2026, 9, 7), root=root)
            attempt_id = first["content"]["generation_attempt_id"]
            replay = generate_weekly_proposals(
                diagnostics, Adapter(), week=__import__("datetime").date(2026, 9, 7), root=root,
                attempt_id=attempt_id)
        self.assertEqual(first["content"]["status"], "proposed")
        self.assertEqual(replay["content"]["status"], "proposed")
        self.assertEqual(replay["content"]["adapter_status"], "replayed")
        self.assertEqual(calls, {"generate": 1, "repair": 1})

    def test_missing_anchor_cannot_become_later_independent_event(self):
        first = _snapshot(day="2026-08-20")
        second = _snapshot(day="2026-08-28")
        first["content"]["records"][0]["record_id"] = "first"
        second["content"]["records"][0]["record_id"] = "second"
        outcomes = [
            {"recommendation_date": "2026-08-20", "code": "600000", "windows": {"20": {
                "status": "data_error", "entry_date": "2026-08-21", "exit_date": "2026-09-17"}}},
            {"recommendation_date": "2026-08-28", "code": "600000", "windows": {"20": {
                "status": "complete", "hs300_alpha": .2, "entry_date": "2026-08-31", "exit_date": "2026-09-25"}}},
        ]
        result = build_diagnostics([first, second], outcomes)
        self.assertEqual(result["content"]["overall"]["mature_records"], 1)
        self.assertEqual(result["content"]["overall"]["mature_events"], 0)


def run_recommendation_diagnostics_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
