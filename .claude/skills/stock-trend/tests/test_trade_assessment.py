"""Offline integration checks for independent immutable trade reports."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from analysis.trade_assessment import assess_snapshot, run, save_artifact
from test_lps_distance_research import fixture
from test_lps_trade_assessment import context


class TradeAssessmentIntegrationTests(unittest.TestCase):
    def test_complete_frozen_context_produces_plan_and_actual_exit_costs(self):
        research, official = fixture()
        research["content"]["policy"]["max_portfolio_pct"] = 30
        official["content"]["policy"]["max_portfolio_pct"] = 30
        # Rebuild both hashes and frozen manifest after the fixture policy edit.
        from test_lps_distance_research import resign
        from core.recommendation_snapshot import content_sha256
        official["content_sha256"] = content_sha256(official["content"])
        research["content"]["official_snapshot"]["content_sha256"] = official["content_sha256"]
        resign(research)
        record = next(r for r in research["content"]["records"] if r["code"] == "600002")
        day = research["content"]["recommendation_date"]
        days = [day, "2026-09-22", "2026-09-23"]
        ctx = context()
        ctx.update(known_at=day + "T15:30:00+08:00", market_sessions=days)
        ctx["calendar_evidence"]["complete_through"] = days[-1]
        ctx["security_meta"].update(code="600002", as_of=day)
        ctx["target_evidence"]["basis_date"] = day
        ctx["price_scale_evidence"]["known_at"] = ctx["known_at"]
        key = f"{record['record_id']}@{research['content_sha256']}"
        market = {"calendar": {"calendar_id": "cn-fixture", "source": "fixture",
                               "sessions": days, "complete_through": days[-1]},
                  "corporate_actions": {"source": "fixture", "no_actions": True,
                                        "complete_from": day, "complete_through": days[-1]},
                  "metadata": {**ctx["security_meta"], "price_scale": "raw", "rules_valid_through": days[-1]},
                  "rows": [{"date": d, "open": 10.2, "high": 10.4, "low": 10.1,
                            "close": 10.2, "volume": 1000} for d in days]}
        market["rows"][-1].update(open=13.1, high=13.2, low=13, close=13.1)
        result = assess_snapshot(research, official, {key: {"decision_context": ctx, "market_data": market}}, days[-1])
        item = next(i for i in result["items"] if i["code"] == "600002")
        self.assertEqual(item["status"], "可制定交易计划", item)
        self.assertEqual(item["plan"]["immutable_identity"], key)
        self.assertTrue(all(r["opportunity_status"] == "completed" for r in item["simulations"]))
        self.assertTrue(all(r["returns"]["net_return"] > 0 for r in item["simulations"]))

    def test_invalid_population_cannot_plan(self):
        research, official = fixture()
        research["content"]["records"][0]["code"] = "tampered"
        result = assess_snapshot(research, official, {}, "2026-10-01")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["reason"], "snapshot_hash_mismatch")

    def test_missing_context_retains_lps_opportunities(self):
        research, official = fixture()
        before = copy.deepcopy((research, official))
        result = assess_snapshot(research, official, {}, "2026-10-01")
        self.assertEqual(len(result["items"]), 2)
        for item in result["items"]:
            self.assertEqual(item["status"], "数据不足")
            self.assertEqual(len(item["simulations"]), 4)
            self.assertTrue(all(row["opportunity_status"] != "completed" for row in item["simulations"]))
        self.assertEqual((research, official), before)

    def test_context_identity_is_not_code_only(self):
        research, official = fixture()
        contexts = {"600001": {"decision_context": {"sentinel": True}}}
        with patch("analysis.trade_assessment.build_lps_trade_assessment", return_value={"status": "数据不足", "plan": {}}) as builder:
            assess_snapshot(research, official, contexts, "2026-10-01")
            self.assertTrue(all(call.args[3] == {} for call in builder.call_args_list))

    def test_save_is_idempotent_and_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("analysis.trade_assessment.load_primary_research_snapshots", return_value=[]):
                artifact = run("2026-10-01", state_root=folder)
            paths = save_artifact(artifact, Path(folder) / "cache", Path(folder) / "reports")
            again = save_artifact(artifact, Path(folder) / "cache", Path(folder) / "reports")
            self.assertEqual(paths, again)
            self.assertEqual(set(paths), {"cache_json", "json", "md", "html"})
            modified = copy.deepcopy(artifact)
            modified["disclaimer"] = "changed"
            with self.assertRaises(ValueError):
                save_artifact(modified, Path(folder) / "cache", Path(folder) / "reports")

    def test_legacy_context_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            run("2026-10-01", {"schema_version": "legacy", "opportunities": {}})


def run_trade_assessment_tests():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TradeAssessmentIntegrationTests))
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    unittest.main()
