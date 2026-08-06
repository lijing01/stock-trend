#!/usr/bin/env python3
"""Tests for /candidates recommendation policy."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans.daily_candidates import (
    _generate_html,
    build_json_output,
    build_recommendation_policy,
    candidate_rank_score,
    classify_candidates,
    enrich_sector_context,
    generate_report,
    is_recommendation_session,
    merge_sector_resonance,
    resolve_recommendation_date,
)
from scans import daily_candidates as dc


def candidate(code, eligible=True, adjusted_score=80.0,
              sector_actionable=True, score_eligible=True):
    return {
        "code": code,
        "name": f"测试{code}",
        "sector_name": "测试板块",
        "composite_score": 80.0,
        "quality_adjusted_score": adjusted_score,
        "sector_actionable": sector_actionable,
        "sector_type": "mainline" if sector_actionable else "single_day_pulse",
        "score_eligible": score_eligible,
        "wyckoff": {"sub_phase": "LPS", "confidence": 0.6},
        "signals": {},
        "data_quality": {
            "eligible": eligible,
            "coverage": 0.8 if eligible else 0.55,
            "reasons": [] if eligible else ["coverage_below_70pct"],
        },
    }


class TestRecommendationPolicy(unittest.TestCase):
    def test_weekend_uses_latest_trading_date(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 8, 10, 0),
            regime_date="2026-08-07",
            last_trading_date="2026-08-07",
        )
        self.assertEqual(result, "2026-08-07")

    def test_calendar_fallback_handles_first_day_of_month(self):
        from fetchers import sector_data

        class FrozenDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 8, 1, 10, 0)

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"), \
             patch.object(sector_data, "datetime", FrozenDateTime):
            trade_date, source = sector_data.get_last_trading_day()
        self.assertEqual(trade_date, "2026-07-31")
        self.assertEqual(source, "calendar")

    def test_premarket_uses_previous_close_date(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-08-05",
            last_trading_date="2026-08-05",
        )
        self.assertEqual(result, "2026-08-05")

    def test_premarket_rejects_stale_snapshot_and_regime_dates(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-07-01",
            last_trading_date="2026-07-01",
        )
        self.assertEqual(result, "2026-08-05")

    def test_premarket_rejects_date_from_previous_week(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-07-30",
            last_trading_date="2026-07-30",
        )
        self.assertEqual(result, "2026-08-05")

    def test_lunch_break_is_still_intraday_provisional(self):
        self.assertTrue(is_recommendation_session(
            datetime(2026, 8, 6, 12, 0)))

    def test_sector_with_three_day_persistence_is_actionable(self):
        ranked = [{
            "code": "BK1", "name": "持续主线",
            "absolute_hot_score": 70, "hot_score": 90,
            "change_pct": 2.0,
        }]
        history = {
            "2026-08-04": [{"code": "BK1", "hot_score": 65,
                            "net_flow": 1e8}],
            "2026-08-05": [{"code": "BK1", "hot_score": 70,
                            "net_flow": 2e8}],
            "2026-08-06": [{"code": "BK1", "hot_score": 75,
                            "net_flow": 3e8}],
        }
        sector = enrich_sector_context(
            ranked, history, hs300_change=0.5)[0]
        self.assertEqual(sector["sector_type"], "mainline")
        self.assertTrue(sector["sector_actionable"])
        self.assertEqual(sector["persistence_days"], 3)
        self.assertGreater(sector["relative_strength"], 0)
        self.assertGreater(sector["capital_persistence"], 50)

    def test_sector_without_history_is_single_day_observation(self):
        ranked = [{
            "code": "BK1", "name": "单日脉冲",
            "absolute_hot_score": 70, "hot_score": 100,
            "change_pct": 3.0,
        }]
        sector = enrich_sector_context(ranked, {}, hs300_change=0.0)[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])

    def test_sector_missing_latest_days_is_not_a_mainline(self):
        ranked = [{
            "code": "BK1", "name": "过期热点",
            "absolute_hot_score": 80, "hot_score": 90,
            "change_pct": 2.0,
        }]
        history = {
            "2026-08-02": [{"code": "BK1", "hot_score": 90}],
            "2026-08-03": [{"code": "BK1", "hot_score": 90}],
            "2026-08-04": [{"code": "BK1", "hot_score": 90}],
            "2026-08-05": [{"code": "BK2", "hot_score": 80}],
            "2026-08-06": [{"code": "BK2", "hot_score": 80}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])

    def test_short_history_does_not_claim_three_day_persistence(self):
        ranked = [{
            "code": "BK1", "name": "新热点",
            "absolute_hot_score": 70, "hot_score": 80,
        }]
        history = {
            "2026-08-05": [{"code": "BK1", "hot_score": 70}],
            "2026-08-06": [{"code": "BK1", "hot_score": 80}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertIsNone(sector["persistence_3d"])

    def test_missing_sector_day_counts_as_zero_in_persistence_window(self):
        ranked = [{
            "code": "BK1", "name": "间断热点",
            "absolute_hot_score": 70, "hot_score": 90,
        }]
        history = {
            "2026-08-04": [{"code": "BK1", "hot_score": 90}],
            "2026-08-05": [{"code": "BK2", "hot_score": 80}],
            "2026-08-06": [{"code": "BK1", "hot_score": 90}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertEqual(sector["persistence_3d"], 60.0)
        self.assertFalse(sector["sector_actionable"])

    def test_stale_sector_history_is_observation_only(self):
        ranked = [{
            "code": "BK1", "name": "过期连续热点",
            "absolute_hot_score": 70, "hot_score": 90,
        }]
        history = {
            "2026-07-01": [{"code": "BK1", "hot_score": 90}],
            "2026-07-02": [{"code": "BK1", "hot_score": 90}],
            "2026-07-03": [{"code": "BK1", "hot_score": 90}],
        }
        sector = enrich_sector_context(
            ranked, history, as_of_date="2026-08-06")[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])

    def test_capital_persistence_measures_positive_days_not_amount(self):
        ranked = [{
            "code": "BK1", "name": "资金不连续",
            "absolute_hot_score": 70, "hot_score": 80,
        }]
        history = {
            "2026-08-02": [{"code": "BK1", "hot_score": 70,
                            "net_flow": 100e8}],
            "2026-08-03": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-04": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-05": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-06": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertLess(sector["capital_persistence"], 50)
        self.assertEqual(sector["capital_positive_days"], 1)
        self.assertEqual(sector["capital_streak"], 0)

    def test_sector_resonance_merges_zt_and_lhb_scores_by_name(self):
        ranked = [{"code": "BK1", "name": "半导体"}]
        merged = merge_sector_resonance(ranked, [{
            "name": "半导体", "zt_score": 75, "lhb_score": 65,
        }])
        self.assertEqual(merged[0]["zt_score"], 75)
        self.assertEqual(merged[0]["lhb_score"], 65)

    def test_rank_score_prefers_quality_adjusted_score(self):
        self.assertEqual(
            candidate_rank_score(candidate("1", adjusted_score=63.5)), 63.5)

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
                      "quality_adjusted_score": 40,
                      "data_quality": {"eligible": True}}],
            "BK2": [{"code": "2", "composite_score": 80,
                      "quality_adjusted_score": 80,
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

    def test_strong_regime_never_promotes_single_day_pulse(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1", sector_actionable=False)], policy)
        self.assertEqual(buckets["actionable"], [])
        self.assertEqual(buckets["observation"][0]["code"], "1")
        self.assertIn(
            "single_day_pulse",
            buckets["observation"][0]["observation_reasons"],
        )

    def test_low_quality_score_stays_in_observation_pool(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates([
            candidate("1", adjusted_score=40, score_eligible=False),
        ], policy)
        self.assertEqual(buckets["actionable"], [])
        self.assertEqual(buckets["observation"][0]["code"], "1")
        self.assertIn(
            "quality_adjusted_below_min_score",
            buckets["observation"][0]["observation_reasons"],
        )

    def test_top_limit_prioritizes_promotable_candidate(self):
        pulse = candidate("pulse", adjusted_score=100,
                          sector_actionable=False)
        valid = candidate("valid", adjusted_score=80)
        selected = dc.select_candidate_pool(
            [pulse, valid], top=1, min_score=50)
        self.assertEqual([item["code"] for item in selected], ["valid"])

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
        self.assertIn("质量分", report)
        self.assertIn("coverage_below_70pct", report)
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
        self.assertEqual(output["candidates"][0]["quality_adjusted_score"], 80.0)


def run_daily_candidates_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationPolicy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_daily_candidates_tests()
    raise SystemExit(1 if failed else 0)
