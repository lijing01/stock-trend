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
