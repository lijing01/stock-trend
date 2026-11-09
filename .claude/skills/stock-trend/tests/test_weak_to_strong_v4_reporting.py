#!/usr/bin/env python3
"""Focused reporting tests for the primary weak-to-strong V4 view."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from reporting import weak_to_strong as report


def candidate(symbol, signal_type, rank, expected, win, *, name=None):
    return {
        "status": "ready",
        "as_of_date": "2026-11-06",
        "symbol": symbol,
        "name": name or symbol,
        "v4_type_rank": rank,
        "prediction": {
            "score_status": "ready",
            "signal_type": signal_type,
            "signal_date": "2026-11-06",
            "feature_asof": "2026-11-06",
            "v4_score": 88 - rank,
            "v3_score": 70 - rank,
            "expected_net_return": expected,
            "estimated_win_rate": win,
            "model_version": "2026-11-monthly",
            "model_hash": "hash-<unsafe>",
            "event_id": f"event-{symbol}",
        },
    }


class WeakToStrongV4ReportingTests(unittest.TestCase):
    def payload(self):
        return {
            "schema_version": "daily-weak-to-strong/v4-market/v1",
            "as_of_date": "2026-11-06",
            "research_candidates": {
                "T1": [candidate("T1-A", "T1", 1, .021, .63, name="甲|股")],
                "T2": [candidate("T2-A", "T2", 1, .018, .58, name="乙<股>")],
            },
            "early_observations": [{
                "symbol": "EARLY", "name": "试盘|股",
                "early_observation": {
                    "signal_type": "T1", "probe_status": "candidate",
                    "candidate_date": "2026-11-05", "candidate_age_sessions": 1,
                    "candidate_high": 10.8, "structure_stage": "EARLY_REPAIR",
                    "structure_failure_line": 9.65, "provisional": True,
                    "confirmation_state": "probe_unconfirmed",
                },
                "v3_result": {"provisional": True},
            }],
            "structural_observations": [{
                "symbol": "WATCH", "name": "观察股", "reason": "no fresh event",
                "classification_reasons": ["no fresh event"],
            }],
            "no_active_signal": [{
                "symbol": "NONE", "classification_reasons": ["no fresh event"],
            }],
            "data_insufficient": [{
                "symbol": "BAD", "status": "fallback", "reason": "model_expired",
                "classification_reasons": ["model_expired"],
            }],
            "summary": {
                "t1_research_candidates": 1, "t2_research_candidates": 1,
                "early_observations": 1,
                "structural_observations": 1, "no_active_signal": 1,
                "data_insufficient": 1,
            },
        }

    def test_renders_t1_t2_scores_model_dates_and_coverage(self):
        payload = self.payload()
        markdown = report.render_v4_markdown(payload)
        html = report.render_v4_html(payload)

        for rendered in (markdown, html):
            self.assertLess(rendered.index("T1 新确认"), rendered.index("T2 新确认"))
            self.assertIn("2.10%", rendered)
            self.assertIn("63.00%", rendered)
            self.assertIn("2026-11-monthly", rendered)
            self.assertIn("2026-11-06 / 2026-11-06", rendered)
            self.assertIn("结构观察 1 只，无活跃信号 1 只，数据不足 1 只", rendered)
            self.assertIn("提前观察 1 只", rendered)
            self.assertIn("T1 新确认：支撑位或 MA20 附近的下影线试盘", rendered)
            self.assertIn("T2 新确认：放量下跌后的缩量回踩试盘", rendered)

    def test_early_observation_shows_probe_structure_and_no_model_metrics(self):
        payload = self.payload()
        payload["research_candidates"] = {"T1": [], "T2": []}

        markdown = report.render_v4_markdown(payload)
        html = report.render_v4_html(payload)

        for rendered in (markdown, html):
            self.assertIn("T1/T2 未排名观察（未确认、仅观察）", rendered)
            self.assertIn("试盘", rendered)
            self.assertIn("EARLY", rendered)
            self.assertIn("T1 / 待确认", rendered)
            self.assertIn("早期修复", rendered)
            self.assertIn("9.65", rendered)
            self.assertIn("试盘高点×1.002（10.822）", rendered)
            self.assertIn("成交量/前5日均量 ≥ 1.15", rendered)
            self.assertIn("盘中临时，收盘待复评", rendered)
            self.assertIn("未参与同类型 Top 20 排名", rendered)
            self.assertIn("— / —（未评分）", rendered)
        self.assertIn("试盘\\|股", markdown)
        self.assertIn("试盘|股", html)

    def test_confirmed_probe_says_no_new_event_instead_of_unconfirmed_structure(self):
        payload = self.payload()
        payload["early_observations"][0]["early_observation"].update(
            probe_status="confirmed", provisional=False,
            confirmation_state="probe_confirmed_structure_pending",
        )
        payload["early_observations"][0]["v3_result"]["provisional"] = False

        for rendered in (report.render_v4_markdown(payload), report.render_v4_html(payload)):
            self.assertIn("T1 / 试盘已确认", rendered)
            self.assertIn("历史试盘确认条件已满足；当日无新结构确认事件", rendered)
            self.assertIn("收盘数据；历史试盘已确认，当日无新确认事件", rendered)
            self.assertNotIn("等待收盘 > 试盘高点", rendered)
            self.assertNotIn("试盘未确认", rendered)

    def test_early_scores_rank_within_type_cap_at_20_and_keep_missing_unranked(self):
        payload = self.payload()
        payload["early_observations"] = []
        for signal_type, count in (("T1", 23), ("T2", 22)):
            for index in range(count):
                payload["early_observations"].append({
                    "symbol": f"{signal_type}-{index:02d}",
                    "name": f"{signal_type}观察{index:02d}",
                    "early_observation": {
                        "signal_type": signal_type,
                        "probe_status": "candidate",
                        "candidate_date": "2026-11-05",
                        "candidate_age_sessions": index % 3,
                        "observation_score": 100 - index,
                        "observation_score_max": 100,
                        "observation_score_status": "trial_unvalidated",
                        "ranking_cohort": f"{signal_type}:100",
                        "observation_score_components": {
                            "trend_structure": 25 - index / 10,
                            "risk_space": 18,
                            "confirmation_distance": 17,
                            "probe_quality": 13,
                            "freshness": 9,
                            "market_sector": 8,
                        },
                    },
                })
        payload["early_observations"].append({
            "symbol": "T1-90MAX", "name": "九十分覆盖",
            "early_observation": {
                "signal_type": "T1", "probe_status": "candidate",
                "observation_score": 90, "observation_score_max": 90,
                "observation_score_status": "trial_unvalidated",
                "ranking_cohort": "T1:90",
            },
        })
        payload["early_observations"].append({
            "symbol": "NO-SCORE", "name": "未评分",
            "early_observation": {"signal_type": "T1", "probe_status": "candidate"},
        })

        selection = report.select_early_observations(payload)
        self.assertEqual([90, 100], [c["max_score"] for c in selection["cohorts"]["T1"]])
        t1_100 = selection["cohorts"]["T1"][1]
        t2_100 = selection["cohorts"]["T2"][0]
        self.assertEqual(20, len(t1_100["items"]))
        self.assertEqual(20, len(t2_100["items"]))
        self.assertEqual("T1-00", t1_100["items"][0]["symbol"])
        self.assertEqual("T2-00", t2_100["items"][0]["symbol"])
        self.assertEqual(3, t1_100["omitted"])
        self.assertEqual(2, t2_100["omitted"])
        self.assertEqual("NO-SCORE", selection["unscored"][0]["symbol"])

        for rendered in (report.render_v4_markdown(payload), report.render_v4_html(payload)):
            self.assertIn("T1 提前观察 Top 20（满分 90）", rendered)
            self.assertIn("T1 提前观察 Top 20（满分 100）", rendered)
            self.assertIn("T2 提前观察 Top 20（满分 100）", rendered)
            self.assertIn("不同满分代表评分覆盖范围不同", rendered)
            self.assertIn("试运行阶段，尚未回测", rendered)
            self.assertIn("T1/T2 分数可比性尚未验证", rendered)
            self.assertIn("仅在同类型内排序", rendered)
            self.assertIn("趋势结构 25", rendered)
            self.assertIn("100 / 100（试运行（未验证））", rendered)
            self.assertIn("未排名观察", rendered)
            self.assertNotIn("T1观察20 (T1-20)", rendered)
            self.assertNotIn("T2观察20 (T2-20)", rendered)

    def test_diagnostics_keep_statuses_separate_and_deduplicate_reasons(self):
        for rendered in (report.render_v4_markdown(self.payload()),
                         report.render_v4_html(self.payload())):
            self.assertIn("结构观察", rendered)
            self.assertIn("无活跃信号", rendered)
            self.assertIn("数据不足", rendered)
            self.assertEqual(1, rendered.count("model_expired"))

    def test_dynamic_values_are_escaped(self):
        markdown = report.render_v4_markdown(self.payload())
        html = report.render_v4_html(self.payload())

        self.assertIn("甲\\|股", markdown)
        self.assertNotIn("乙<股>", html)
        self.assertIn("乙&lt;股&gt;", html)
        self.assertNotIn("hash-<unsafe>", html)
        self.assertIn("hash-&lt;unsafe&gt;", html)

    def test_display_selection_preserves_type_local_ranks_and_caps_total(self):
        payload = self.payload()
        payload["research_candidates"]["T1"] = [
            candidate(f"T1-{index:02d}", "T1", index, .02, .6)
            for index in range(1, 30)
        ]
        payload["research_candidates"]["T2"] = [
            candidate(f"T2-{index:02d}", "T2", index, .01, .5)
            for index in range(1, 4)
        ]

        selection = report.build_v4_display_selection(payload)

        self.assertEqual("weak-to-strong-display-selection/v4", selection["schema_version"])
        self.assertEqual(30, selection["stats"]["displayed"])
        self.assertEqual(2, selection["stats"]["omitted_by_limit"])
        self.assertEqual("T1", selection["items"][28]["signal_type"])
        self.assertEqual("T2", selection["items"][29]["signal_type"])
        self.assertEqual(1, selection["items"][29]["type_rank"])

    def test_empty_candidates_still_explain_data_insufficient(self):
        payload = self.payload()
        payload["research_candidates"] = {"T1": [], "T2": []}
        markdown = report.render_v4_markdown(payload)

        self.assertIn("当日没有可评分的 V4 T1/T2 新确认信号", markdown)
        self.assertIn("BAD", markdown)
        self.assertIn("model_expired", markdown)

    def test_intraday_structure_remains_visible_when_model_is_missing(self):
        payload = self.payload()
        payload["model_status"] = "model_missing"
        payload["data_insufficient"] = [{
            "symbol": "000009", "reason": "model_missing",
            "classification_reasons": ["model_missing", "close_unconfirmed"],
            "v3_result": {"provisional": True, "current_bar_evaluated": True,
                          "evidence_status": "ready", "stage_label": "早期修复"},
        }]
        for rendered in (report.render_v4_markdown(payload), report.render_v4_html(payload)):
            self.assertIn("000009", rendered)
            self.assertIn("盘中当日K线已评估，结构：早期修复", rendered)
            self.assertIn("V4 模型未配置", rendered)
            self.assertIn("盘后需重新拉取日线并复评", rendered)


if __name__ == "__main__":
    unittest.main()
