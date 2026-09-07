import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.evolution_proposals import build_proposal_run, generate_weekly_proposals, validate_proposals
from analysis.recommendation_diagnostics import (
    build_diagnostics, load_candidate_signal_items, load_primary_research_snapshots, save_diagnostics,
)


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
        self.assertIn("model_failed", run["content"]["adapter_status"])


def run_recommendation_diagnostics_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
