#!/usr/bin/env python3
"""Tests for /candidates recommendation policy."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans.daily_candidates import (
    _generate_html,
    build_json_output,
    build_recommendation_policy,
    classify_candidates,
    generate_report,
)
from scans import daily_candidates as dc


def candidate(code, eligible=True):
    return {
        "code": code,
        "name": f"测试{code}",
        "sector_name": "测试板块",
        "composite_score": 80.0,
        "wyckoff": {"sub_phase": "LPS", "confidence": 0.6},
        "signals": {},
        "data_quality": {
            "eligible": eligible,
            "coverage": 0.8 if eligible else 0.55,
            "reasons": [] if eligible else ["coverage_below_70pct"],
        },
    }


class TestRecommendationPolicy(unittest.TestCase):
    def test_pick_hot_sectors_uses_absolute_threshold(self):
        rankings = {"sectors": [
            {"code": "BK1", "name": "弱中最强", "change_pct": -1.0,
             "main_force_net": -1e8, "up_count": 2, "down_count": 8},
            {"code": "BK2", "name": "更弱", "change_pct": -3.0,
             "main_force_net": -2e8, "up_count": 2, "down_count": 8},
        ]}
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings):
            picked = dc.pick_hot_sectors(top_n=20, min_hot=45, min_stocks=1)
        self.assertEqual(picked, [])

    def test_scan_expands_until_eligible_count_reaches_target(self):
        calls = []

        def fake_gather(batch, top_n_per_sector):
            calls.append(list(batch))
            return {"candidates": [{"code": batch[0]}]}

        results = {
            "BK1": [{"code": "1", "composite_score": 80,
                      "data_quality": {"eligible": False}}],
            "BK2": [{"code": "2", "composite_score": 80,
                      "data_quality": {"eligible": True}}],
        }

        def fake_phase2(candidates, enable_wyckoff, as_of_date):
            return results[candidates[0]["code"]]

        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            scored = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=1,
                min_score=50, as_of_date="2026-08-06")
        self.assertEqual(len(calls), 2)
        self.assertEqual({item["code"] for item in scored}, {"1", "2"})

    def test_missing_regime_allows_observation_only(self):
        policy = build_recommendation_policy(None, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertEqual(policy["max_recommendations"], 0)

    def test_stale_regime_allows_observation_only(self):
        regime = {"score": 90, "data_date": "2026-08-05"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("regime_stale", policy["reasons"])

    def test_weak_regime_allows_observation_only(self):
        regime = {"score": 59, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")

    def test_intraday_output_is_provisional_observation(self):
        regime = {"score": 90, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_neutral_regime_limits_waiting_list_to_two(self):
        regime = {"score": 70, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2"), candidate("3")], policy)
        self.assertEqual(policy["mode"], "waiting_trigger")
        self.assertEqual(len(buckets["waiting_trigger"]), 2)
        self.assertEqual(len(buckets["observation"]), 1)

    def test_strong_regime_never_promotes_ineligible_candidate(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2", eligible=False)], policy)
        self.assertEqual([item["code"] for item in buckets["actionable"]], ["1"])
        self.assertEqual([item["code"] for item in buckets["observation"]], ["2"])

    def test_report_renders_all_buckets_and_full_disclaimer(self):
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        buckets = {
            "actionable": [candidate("1")],
            "waiting_trigger": [],
            "observation": [candidate("2", eligible=False)],
        }
        report = generate_report(
            buckets["actionable"] + buckets["observation"],
            [("BK1", "测试板块", 80)],
            1.0,
            policy,
            buckets,
        )
        self.assertIn("## 今日可执行", report)
        self.assertIn("## 等待触发", report)
        self.assertIn("## 观察池", report)
        self.assertIn("股市有风险，投资需谨慎", report)

    def test_html_renders_all_buckets_and_full_disclaimer(self):
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        buckets = {
            "actionable": [candidate("1")],
            "waiting_trigger": [],
            "observation": [candidate("2", eligible=False)],
        }
        html = _generate_html(
            buckets["actionable"] + buckets["observation"],
            [("BK1", "测试板块", 80)],
            1.0,
            "20260806-160000",
            policy,
            buckets,
        )
        self.assertIn("今日可执行", html)
        self.assertIn("等待触发", html)
        self.assertIn("观察池", html)
        self.assertIn("股市有风险，投资需谨慎", html)

    def test_json_output_keeps_candidates_and_adds_action_buckets(self):
        items = [candidate("1")]
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        buckets = {
            "actionable": items,
            "waiting_trigger": [],
            "observation": [],
        }
        output = build_json_output(items, [("BK1", "测试板块", 80)],
                                   1.0, policy, buckets)
        self.assertEqual(output["candidates"], items)
        self.assertEqual(output["recommendations"], items)
        self.assertEqual(output["waiting_trigger"], [])
        self.assertEqual(output["observation"], [])
        self.assertEqual(output["policy"], policy)


def run_daily_candidates_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationPolicy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_daily_candidates_tests()
    raise SystemExit(1 if failed else 0)
