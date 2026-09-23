"""Frozen six-dimension ablation integrity and within-bucket rank tests."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.factor_ablation import (
    DEFAULT_CONTRACT_ID, WEIGHTS, evaluate_ablation_days, run_daily_ablation,
)
from core.candidate_research_snapshot import build_research_snapshot
from core.recommendation_snapshot import content_sha256
from scans.daily_candidates import classify_candidates, select_candidate_pool


DAY = "2026-09-21"


def candidate(code, dimensions):
    raw = round(sum(dimensions[name] * weight for name, weight in WEIGHTS.items()), 1)
    return {"code": code, "ts_code": f"{code}.SH", "raw_dimensions": dimensions,
            "raw_composite_score": raw, "composite_score": raw,
            "quality_adjusted_score": raw, "execution_priority_score": raw,
            "buy_point_priority_bonus": 0.0,
            "data_quality": {"eligible": True, "coverage_factor": 1.0,
                             "freshness_factor": 1.0},
            "sector_actionable": True, "score_eligible": True}


def fixture():
    high_momentum = dict.fromkeys(WEIGHTS, 70.0)
    high_momentum["momentum"] = 100.0
    broad_strength = dict.fromkeys(WEIGHTS, 77.0)
    rows = [candidate("600001", high_momentum), candidate("600002", broad_strength)]
    policy = {"mode": "actionable", "max_recommendations": 2}
    selected = select_candidate_pool(copy.deepcopy(rows), 2, 50, policy, {})
    buckets = classify_candidates(selected, policy)
    formal = {"recommendation_date": DAY, "snapshot_type": "formal",
              "model_version": "daily-candidates/v4", "policy": policy,
              "candidates": selected, "buckets": buckets}
    official = {"content": formal, "content_sha256": content_sha256(formal)}
    scope = {"mode": "explicit_codes", "codes": [row["code"] for row in rows]}
    research = build_research_snapshot(
        rows, buckets, DAY, policy, {}, [], 50,
        official_tracking={"status": "created", "content_sha256": official["content_sha256"]},
        parameter_summary={"top": 2, "min_score": 50,
                           "buy_point_priority_bonus": {}},
        selection_scope=scope)
    return research, official


class FactorAblationTests(unittest.TestCase):
    def test_single_removal_changes_only_rank(self):
        research, official = fixture()
        result = run_daily_ablation(research, official)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["baseline"]["selected_codes"], ["600001", "600002"])
        self.assertEqual(result["treatments"]["remove_momentum"]["selected_codes"],
                         ["600002", "600001"])
        self.assertEqual(result["treatments"]["remove_momentum"]["top_k"]["1"],
                         ["600002"])
        self.assertEqual(set(result["treatments"]),
                         {f"remove_{name}" for name in WEIGHTS})

    def test_legacy_snapshot_scope_is_unverified(self):
        research, official = fixture()
        research["content"].pop("selection_scope")
        research["content"]["input_manifest"]["inputs"].pop("selection_scope")
        research["content"]["input_manifest"]["input_sha256"] = content_sha256(
            research["content"]["input_manifest"]["inputs"])
        research["content_sha256"] = content_sha256(research["content"])
        result = run_daily_ablation(research, official)
        self.assertEqual(result["status"], "scope_unverified")

    def test_five_dimensional_fallback_is_rejected(self):
        research, official = fixture()
        research["content"]["records"][0]["candidate"]["raw_dimensions"].pop("wyckoff")
        research["content"]["input_manifest"]["inputs"]["candidate_records"] = copy.deepcopy(
            research["content"]["records"])
        research["content"]["input_manifest"]["input_sha256"] = content_sha256(
            research["content"]["input_manifest"]["inputs"])
        research["content_sha256"] = content_sha256(research["content"])
        result = run_daily_ablation(research, official)
        self.assertEqual(result["status"], "input_incomplete")
        self.assertEqual(result["invalid_records"][0]["reason"], "missing_six_dimensions")

    def test_score_mismatch_rejects_whole_day(self):
        research, official = fixture()
        research["content"]["records"][0]["scores"]["raw_composite_score"] += .1
        research["content"]["input_manifest"]["inputs"]["candidate_records"] = copy.deepcopy(
            research["content"]["records"])
        research["content"]["input_manifest"]["input_sha256"] = content_sha256(
            research["content"]["input_manifest"]["inputs"])
        research["content_sha256"] = content_sha256(research["content"])
        result = run_daily_ablation(research, official)
        self.assertEqual(result["status"], "input_incomplete")
        self.assertEqual(result["invalid_records"][0]["reason"],
                         "score_reconstruction_mismatch")

    def test_official_mismatch_blocks_treatments(self):
        research, official = fixture()
        official["content"]["candidates"] = list(reversed(official["content"]["candidates"]))
        official["content_sha256"] = content_sha256(official["content"])
        research["content"]["official_snapshot"]["content_sha256"] = official["content_sha256"]
        research["content"]["input_manifest"]["inputs"]["official_snapshot"]["content_sha256"] = official["content_sha256"]
        research["content"]["input_manifest"]["input_sha256"] = content_sha256(
            research["content"]["input_manifest"]["inputs"])
        research["content_sha256"] = content_sha256(research["content"])
        result = run_daily_ablation(research, official)
        self.assertEqual(result["status"], "baseline_mismatch")
        self.assertFalse(result["treatments"])

    def test_outcome_pair_requires_exact_identity_and_cutoff(self):
        research, official = fixture()
        daily = run_daily_ablation(research, official)
        outcomes = []
        for code, record_id in daily["record_ids"].items():
            outcomes.append({
                "record_id": record_id, "code": code,
                "recommendation_date": DAY,
                "research_snapshot_sha256": daily["research_snapshot_sha256"],
                "contract_id": DEFAULT_CONTRACT_ID,
                "evaluation_as_of": "2026-10-26",
                "windows": {"20": {"status": "complete",
                                   "hs300_alpha": .1 if code == "600002" else 0}},
            })
        early = evaluate_ablation_days([daily], outcomes, "2026-10-25")
        self.assertEqual(early["comparisons"]["remove_momentum"]["20"]["1"]["paired_dates"], 0)
        ready = evaluate_ablation_days([daily], outcomes, "2026-10-26")
        self.assertEqual(ready["comparisons"]["remove_momentum"]["20"]["1"]["paired_dates"], 1)
        self.assertAlmostEqual(ready["comparisons"]["remove_momentum"]["20"]["1"]["mean_delta"], .1)
        conflict = evaluate_ablation_days([daily], outcomes + [copy.deepcopy(outcomes[0])],
                                          "2026-10-26")
        self.assertEqual(conflict["duplicate_outcome_identities"], 1)
        self.assertEqual(conflict["comparisons"]["remove_momentum"]["20"]["1"]["paired_dates"], 0)

    def test_primary_report_keeps_absolute_return_mae_and_market_strata(self):
        research, official = fixture()
        daily = run_daily_ablation(research, official)
        outcomes = []
        for number, (code, record_id) in enumerate(daily["record_ids"].items()):
            outcomes.append({
                "record_id": record_id, "code": code,
                "recommendation_date": DAY,
                "research_snapshot_sha256": daily["research_snapshot_sha256"],
                "contract_id": DEFAULT_CONTRACT_ID,
                "evaluation_as_of": "2026-10-26",
                "dimensions": {"market_regime_band": "weak_<60" if number == 0 else "strong_80_plus"},
                "windows": {"20": {"status": "complete",
                                   "hs300_alpha": .1 if number else 0,
                                   "signal_return": .05 if number else -.02,
                                   "mae": -.03 if number else -.08}},
            })
        result = evaluate_ablation_days([daily], outcomes, "2026-10-26")
        report = result["primary_report"]["remove_momentum"]
        metrics = report["metrics"]
        self.assertAlmostEqual(metrics["baseline"]["absolute_return_mean"], .015)
        self.assertAlmostEqual(metrics["baseline"]["win_rate"], .5)
        self.assertAlmostEqual(metrics["baseline"]["mae_mean"], -.055)
        self.assertIn("weak_<60", report["market_stratification"]["baseline"])
        self.assertEqual(report["date_distribution"], [DAY])
        self.assertEqual(result["coverage_gate"]["minimum"], .9)


if __name__ == "__main__":
    unittest.main()
