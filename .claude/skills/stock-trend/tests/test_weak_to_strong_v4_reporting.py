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
                    "observation_score": 70, "observation_score_max": 90,
                    "observation_score_status": "trial_unvalidated",
                    "observation_score_components": {
                        "trend_structure": 20, "risk_space": 15,
                        "confirmation_distance": 15, "probe_quality": 10,
                        "freshness": 10,
                    },
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
            self.assertLess(rendered.index("T2 新确认"), rendered.index("T1 新确认"))
            self.assertIn("2.10%", rendered)
            self.assertIn("63.00%", rendered)
            self.assertIn("2026-11-monthly", rendered)
            self.assertIn("2026-11-06 / 2026-11-06", rendered)
            self.assertIn("结构观察 1 只，无活跃信号 1 只，数据不足 1 只", rendered)
            self.assertIn("提前观察 1 只", rendered)
            self.assertIn("T1 新确认：支撑位或 MA20 附近的下影线试盘", rendered)
            self.assertIn("T2 新确认：放量下跌后的缩量回踩试盘", rendered)
            self.assertIn("总名额优先 T2", rendered)
            self.assertIn("不代表 T1/T2 跨类型收益或胜率可直接比较", rendered)

    def test_early_observation_shows_probe_structure_and_no_model_metrics(self):
        payload = self.payload()
        payload["research_candidates"] = {"T1": [], "T2": []}

        markdown = report.render_v4_markdown(payload)
        html = report.render_v4_html(payload)

        for rendered in (markdown, html):
            self.assertIn("T1/T2 合并提前观察 Top 20（满分 90）", rendered)
            self.assertIn("试盘", rendered)
            self.assertIn("EARLY", rendered)
            self.assertIn("T1 / 待确认", rendered)
            self.assertIn("早期修复", rendered)
            self.assertIn("9.65", rendered)
            self.assertIn("试盘高点×1.002（10.822）", rendered)
            self.assertIn("成交量/前5日均量 ≥ 1.15", rendered)
            self.assertIn("盘中临时，收盘待复评", rendered)
            self.assertIn("70 / 90（试运行（未验证））", rendered)
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

    def test_early_scores_merge_types_on_common_core_and_cap_total_at_20(self):
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
        payload["early_observations"].append({
            "symbol": "INVALID-SCORE", "name": "无效评分",
            "early_observation": {
                "signal_type": "T2", "probe_status": "candidate",
                "observation_score": None,
                "observation_score_status": "data_insufficient",
                "observation_score_components": {
                    "trend_structure": 25, "risk_space": 18,
                    "confirmation_distance": 17, "probe_quality": 13,
                    "freshness": 9,
                },
            },
        })

        selection = report.select_early_observations(payload)
        self.assertEqual(90, selection["max_score"])
        self.assertEqual(20, len(selection["items"]))
        self.assertEqual("T2-00", selection["items"][0]["symbol"])
        self.assertEqual("T1-00", selection["items"][1]["symbol"])
        self.assertEqual(25, selection["omitted_by_limit"])
        self.assertEqual(3, selection["unscored_total"])
        self.assertEqual(
            82, selection["items"][0]["early_observation"]["observation_score"])
        self.assertNotIn(
            "market_sector",
            selection["items"][0]["early_observation"]["observation_score_components"],
        )

        for rendered in (report.render_v4_markdown(payload), report.render_v4_html(payload)):
            self.assertIn("T1/T2 合并提前观察 Top 20（满分 90）", rendered)
            self.assertNotIn("T1 提前观察 Top 20", rendered)
            self.assertNotIn("T2 提前观察 Top 20", rendered)
            self.assertIn("统一使用五项共同分量", rendered)
            self.assertIn("市场板块分不计入", rendered)
            self.assertIn("试运行阶段，尚未回测", rendered)
            self.assertIn("跨类型分数可比性尚未验证", rendered)
            self.assertIn("观察分相同时优先 T2", rendered)
            self.assertIn("趋势结构 25", rendered)
            self.assertIn("82 / 90（试运行（未验证））", rendered)
            self.assertNotIn("未排名观察", rendered)
            self.assertNotIn("T1观察10 (T1-10)", rendered)
            self.assertNotIn("T2观察10 (T2-10)", rendered)
            self.assertIn("原始评分无效", rendered)

    def test_all_unrankable_observations_show_count_without_a_table(self):
        payload = self.payload()
        observation = payload["early_observations"][0]["early_observation"]
        observation["observation_score"] = None
        observation["observation_score_status"] = "data_insufficient"

        for rendered in (report.render_v4_markdown(payload), report.render_v4_html(payload)):
            self.assertIn("T1/T2 提前观察评分（未确认、仅观察）", rendered)
            self.assertIn("本次没有可排名候选；1 只", rendered)
            self.assertNotIn("T1/T2 合并提前观察 Top 20", rendered)
            self.assertNotIn("70 / 90", rendered)

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
            for index in range(1, 36)
        ]
        payload["research_candidates"]["T2"] = [
            candidate(f"T2-{index:02d}", "T2", index, .01, .5)
            for index in range(1, 4)
        ]

        selection = report.build_v4_display_selection(payload)

        self.assertEqual("weak-to-strong-display-selection/v4", selection["schema_version"])
        self.assertEqual(["T2", "T1"], selection["type_priority"])
        self.assertEqual("display_capacity_only_user_preference",
                         selection["priority_policy"])
        self.assertEqual(
            "common_core_score_desc_then_t2_tie_priority_then_age_then_symbol",
            selection["early_observation_order"],
        )
        self.assertEqual(30, selection["stats"]["displayed"])
        self.assertEqual(8, selection["stats"]["omitted_by_limit"])
        self.assertEqual(["T2", "T2", "T2"],
                         [item["signal_type"] for item in selection["items"][:3]])
        self.assertEqual("T1", selection["items"][3]["signal_type"])
        self.assertEqual(1, selection["items"][0]["type_rank"])

        for rendered in (report.render_v4_markdown(payload),
                         report.render_v4_html(payload)):
            self.assertLess(rendered.index("T2 新确认"), rendered.index("T1 新确认"))
            self.assertIn("T2-03", rendered)
            self.assertNotIn("T1-28", rendered)

    def test_early_priority_uses_score_before_t2_tie_preference(self):
        payload = self.payload()

        def observation(symbol, signal_type, score, age):
            return {
                "symbol": symbol,
                "early_observation": {
                    "signal_type": signal_type,
                    "probe_status": "candidate",
                    "candidate_age_sessions": age,
                    "observation_score": score,
                    "observation_score_max": 90,
                    "observation_score_status": "trial_unvalidated",
                    "observation_score_components": {
                        "trend_structure": score - 40,
                        "risk_space": 10,
                        "confirmation_distance": 10,
                        "probe_quality": 10,
                        "freshness": 10,
                    },
                },
            }

        payload["early_observations"] = [
            observation("T2-LOW", "T2", 70, 0),
            observation("T1-HIGH", "T1", 80, 5),
            observation("T1-TIE", "T1", 75, 0),
            observation("T2-TIE", "T2", 75, 5),
        ]

        selected = report.select_early_observations(payload)["items"]

        self.assertEqual(
            ["T1-HIGH", "T2-TIE", "T1-TIE", "T2-LOW"],
            [item["symbol"] for item in selected],
        )

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
