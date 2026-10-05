#!/usr/bin/env python3
"""Rendering tests for the daily-review previous-session comparison."""

import sys
import unittest
import re
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reporting.review_comparison import (
    render_html,
    render_markdown,
    render_review_comparison,
)


COMPONENTS = (
    ("index_trend", "大盘趋势", 3.0, 2.0, 0.75),
    ("volume", "成交额", -5.0, -3.0, -1.0),
    ("breadth", "赚钱效应", 8.0, 2.0, 2.0),
    ("zt_emotion", "涨停情绪", 4.0, 3.0, 0.8),
    ("capital", "资金", -2.0, -4.0, -0.2),
)


def comparable_fixture():
    components = {
        component_id: {
            "status": "comparable",
            "current": 50.0 + change,
            "previous": 50.0,
            "change": change,
            "reason": None,
        }
        for component_id, _, change, _, _ in COMPONENTS
    }
    reasons = [
        {
            "component_id": component_id,
            "name": name,
            "current_contribution": 10.0 + contribution_change,
            "previous_contribution": 10.0,
            "change": contribution_change,
        }
        for component_id, name, _, _, contribution_change in COMPONENTS
    ]
    return {
        "schema_version": "review-comparison/v1",
        "basis_date": "2026-10-09",
        "mode": "close",
        "prior_session_date": "2026-10-08",
        "actual_baseline_date": "2026-10-08",
        "session_gap": 1,
        "status": "comparable",
        "reasons": [],
        "score": {
            "status": "comparable", "current": 67.4,
            "previous": 64.9, "change": 2.5, "reason": None,
        },
        "components": components,
        "amount": {
            "status": "comparable", "current": 18435.9,
            "previous": 17600.0, "change": 835.9,
            "percent_change": 4.7494, "reason": None,
        },
        "main_reasons": reasons,
        "contribution_reconciliation": {"status": "matched", "difference": 0.0},
    }


class TestReviewComparisonReporting(unittest.TestCase):
    def test_shared_entry_point_renders_collection_failure(self):
        failed = {
            "status": "unavailable",
            "reason": "calendar <missing> [retry](javascript:alert(1))",
            "fields": {},
        }
        markdown = render_review_comparison(failed, "markdown")
        html = render_review_comparison(failed, "html")

        self.assertIn("unavailable", html)
        self.assertIn("calendar &lt;missing&gt;", html)
        self.assertIn(r"calendar \<missing\>", markdown)
        self.assertNotIn("[retry](javascript:", markdown)
        with self.assertRaises(ValueError):
            render_review_comparison(failed, "text")

    def test_close_comparison_renders_dates_score_components_and_amount(self):
        comparison = comparable_fixture()
        for rendered in (render_markdown(comparison), render_html(comparison)):
            self.assertIn("2026-10-09", rendered)
            self.assertIn("2026-10-08", rendered)
            self.assertIn("+2.50 分", rendered)
            self.assertIn("18,435.90 亿", rendered)
            self.assertIn("+835.90 亿", rendered)
            self.assertIn("+4.75%", rendered)
            self.assertIn("comparable", rendered)
            for _, name, *_ in COMPONENTS:
                self.assertIn(name, rendered)

    def test_html_home_summary_is_chinese_and_full_evidence_is_collapsed(self):
        comparison = comparable_fixture()
        html = render_html(comparison)

        summary, details = html.split("<details", 1)
        summary_text = re.sub(r"<[^>]+>", "", summary)
        self.assertIn('id="review-comparison"', summary)
        self.assertIn("主要变化", summary)
        self.assertIn("总分较上一交易日 +2.50 分", summary_text)
        self.assertIn("两市成交额 +835.90 亿（+4.75%）", summary_text)
        self.assertIn("赚钱效应贡献 +2.00 分", summary_text)
        self.assertIn("比较状态：可比", summary)
        self.assertNotIn("comparable", summary_text)
        self.assertNotIn("总分变化", summary)
        self.assertIn("展开完整比较与原始证据", details)
        self.assertIn("总分变化", details)
        self.assertIn("comparable", details)
        self.assertIn("REVIEW_COMPARISON:START", html)
        self.assertIn("REVIEW_COMPARISON:END", html)

    def test_html_unavailable_summary_hides_technical_reason_code(self):
        comparison = comparable_fixture()
        comparison.update({
            "status": "unavailable",
            "reasons": ["previous_session_snapshot_missing"],
            "main_reasons": [],
        })
        comparison["score"].update({
            "status": "unavailable",
            "change": None,
            "reason": "previous_session_snapshot_missing",
        })

        html = render_html(comparison)
        summary, details = html.split("<details", 1)
        summary_text = re.sub(r"<[^>]+>", "", summary)
        self.assertIn("比较状态：不可比较", summary)
        self.assertIn("缺少上一交易日的合格快照", summary)
        self.assertNotIn("unavailable", summary_text)
        self.assertNotIn("previous_session_snapshot_missing", summary_text)
        self.assertIn("previous_session_snapshot_missing", details)

    def test_html_summary_omits_noncomparable_score_and_amount_changes(self):
        comparison = comparable_fixture()
        comparison["score"].update({
            "status": "unavailable", "change": None,
            "reason": "model_version_mismatch",
        })
        comparison["amount"].update({
            "status": "unavailable", "change": None, "percent_change": None,
            "reason": "amount_date_unqualified",
        })

        html = render_html(comparison)
        summary = html.split("<details", 1)[0]
        summary_text = re.sub(r"<[^>]+>", "", summary)
        self.assertNotIn("总分较上一交易日", summary_text)
        self.assertNotIn("两市成交额", summary_text)
        self.assertIn("赚钱效应贡献 +2.00 分", summary_text)

    def test_intraday_summary_omits_score_and_amount_direction(self):
        comparison = comparable_fixture()
        comparison.update({
            "mode": "intraday",
            "previous_close_reference": {"date": "2026-10-08", "score": 64.9},
        })

        html = render_html(comparison)
        summary = html.split("<details", 1)[0]
        summary_text = re.sub(r"<[^>]+>", "", summary)
        self.assertIn("上一收盘参考总分为 64.90", summary_text)
        self.assertNotIn("总分较上一交易日", summary_text)
        self.assertNotIn("两市成交额", summary_text)

    def test_markdown_keeps_complete_uncollapsed_comparison(self):
        markdown = render_markdown(comparable_fixture())

        self.assertNotIn("<details", markdown)
        self.assertIn("总分变化", markdown)
        self.assertIn("五项变化", markdown)
        self.assertIn("两市成交额（独立比较）", markdown)
        self.assertIn("comparable", markdown)

    def test_main_reasons_are_abs_contribution_sorted_and_model_only(self):
        comparison = comparable_fixture()
        comparison["main_reasons"] = list(reversed(comparison["main_reasons"]))
        markdown = render_markdown(comparison)
        html = render_html(comparison)

        expected_order = ["赚钱效应", "成交额", "涨停情绪", "大盘趋势", "资金"]
        for rendered in (markdown, html):
            positions = [rendered.index(name, rendered.index("主要变化原因")) for name in expected_order]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("评分模型内的加权贡献变化", rendered)
            self.assertIn("不代表市场涨跌因果", rendered)

    def test_independent_amount_comparison_survives_score_incompatibility(self):
        comparison = comparable_fixture()
        comparison.update({"status": "reference_only", "reasons": ["model_version_mismatch"]})
        comparison["score"] = {
            "status": "unavailable", "current": 67.4, "previous": 64.9,
            "change": None, "reason": "model_version_mismatch",
        }
        for component in comparison["components"].values():
            component.update({
                "status": "unavailable", "change": None,
                "reason": "model_version_mismatch",
            })
        comparison["main_reasons"] = []

        for rendered in (render_markdown(comparison), render_html(comparison)):
            self.assertIn("reference", rendered)
            self.assertIn("model", rendered)
            self.assertIn("version", rendered)
            self.assertIn("mismatch", rendered)
            self.assertIn("+835.90 亿", rendered)
            self.assertIn("+4.75%", rendered)
            self.assertNotIn("+2.50 分", rendered)

    def test_intraday_uses_previous_close_reference_without_direction_claim(self):
        comparison = comparable_fixture()
        comparison.update({
            "mode": "intraday",
            "status": "reference_only",
            "reasons": ["intraday_vs_close_not_comparable"],
            "previous_close_reference": {
                "date": "2026-10-08",
                "score": 64.9,
                "components": {
                    component_id: {"score": 50.0}
                    for component_id, *_ in COMPONENTS
                },
            },
        })
        comparison["score"].update({
            "status": "reference_only", "change": None,
            "reason": "intraday_vs_close_not_comparable",
        })
        comparison["main_reasons"] = []

        for rendered in (render_markdown(comparison), render_html(comparison)):
            self.assertIn("上一收盘参考", rendered)
            self.assertIn("2026-10-08", rendered)
            self.assertIn("64.90", rendered)
            self.assertIn("盘中混合分不与正式收盘分作方向性比较", rendered)
            self.assertNotIn("+2.50 分", rendered)
            self.assertNotIn("主要变化原因", rendered)

    def test_unavailable_reason_and_actual_older_baseline_are_explicit(self):
        comparison = comparable_fixture()
        comparison.update({
            "prior_session_date": "2026-10-08",
            "actual_baseline_date": "2026-09-30",
            "session_gap": 4,
            "status": "unavailable",
            "reasons": ["previous_session_snapshot_missing"],
        })
        comparison["score"].update({
            "status": "unavailable", "change": None,
            "reason": "previous_session_snapshot_missing",
        })
        for rendered in (render_markdown(comparison), render_html(comparison)):
            self.assertIn("应比较的上一交易日", rendered)
            self.assertIn("2026-10-08", rendered)
            self.assertIn("实际参考日", rendered)
            self.assertIn("2026-09-30", rendered)
            self.assertIn("4 个交易日", rendered)
            self.assertIn("previous", rendered)
            self.assertIn("session", rendered)
            self.assertIn("snapshot", rendered)
            self.assertIn("missing", rendered)

    def test_rounding_residual_is_explicit_in_both_formats(self):
        comparison = comparable_fixture()
        comparison['contribution_reconciliation'] = {
            'status': 'matched', 'model_score_change': 3.035,
            'rounding_residual': -0.035,
        }
        for rendered in (render_markdown(comparison), render_html(comparison)):
            self.assertIn('+3.035', rendered)
            self.assertIn('-0.035', rendered)
            self.assertIn('舍入残差', rendered)

    def test_dynamic_values_are_escaped_in_html_and_markdown(self):
        comparison = comparable_fixture()
        attack = '<script>alert("x")</script> [伪链接](javascript:alert(1)) | **粗体**'
        comparison["reasons"] = [attack]
        comparison["score"]["reason"] = attack

        html = render_html(comparison)
        markdown = render_markdown(comparison)

        self.assertNotIn("<script>", html)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[伪链接](javascript:", markdown)
        self.assertNotIn("| **粗体**", markdown)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn(r"\<script\>", markdown)
        self.assertIn(r"\[伪链接\]", markdown)
        self.assertIn(r"\| \*\*粗体\*\*", markdown)


if __name__ == "__main__":
    unittest.main()
