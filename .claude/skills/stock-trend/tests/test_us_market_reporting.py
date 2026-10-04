#!/usr/bin/env python3
"""Offline tests for U.S. market summary rendering and report updates."""

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from reporting.us_market_summary import (  # noqa: E402
    HTML_BLOCK_END,
    HTML_BLOCK_START,
    render_html,
    render_markdown,
    update_reports,
)


def sample_summary():
    return {
        "schema_version": "us-market-summary/v2",
        "basis_date": "2026-09-30",
        "a_share_anchor_date": "2026-09-30",
        "anchor_at": "2026-09-30T15:00:00+08:00",
        "anchor_qualification": "qualified",
        "anchor_reason": "calendar_verified",
        "as_of": "2026-10-03T16:44:58+08:00",
        "baseline_session": "2026-09-29",
        "expected_end_session": "2026-10-02",
        "sessions": ["2026-09-30", "2026-10-01", "2026-10-02"],
        "status": "complete",
        "data_quality": "complete",
        "source_status": "fetched",
        "provider": "Yahoo Finance",
        "fetched_at": "2026-10-03T16:45:30+08:00",
        "groups": {
            "indices": [
                {"symbol": "SPY", "name": "标普500", "interval_pct": 1.0, "daily_pct": 0.2,
                 "baseline_price": 100, "end_price": 101, "actual_end_session": "2026-10-02", "status": "ok", "daily": []},
                {"symbol": "QQQ", "name": "纳指100", "interval_pct": 2.0, "daily_pct": 0.4,
                 "actual_end_session": "2026-10-02", "status": "ok", "daily": []},
                {"symbol": "DIA", "name": "道指", "interval_pct": 0.5, "daily_pct": -0.1,
                 "actual_end_session": "2026-10-02", "status": "ok", "daily": []},
                {"symbol": "IWM", "name": "罗素2000", "interval_pct": None, "daily_pct": None,
                 "actual_end_session": None, "status": "missing", "daily": []},
            ],
            "sectors": [
                {"symbol": "XLK", "name": "科技", "interval_pct": 3.0, "daily_pct": 1.0,
                 "relative_spy_pp": 2.0, "actual_end_session": "2026-10-02", "status": "ok", "daily": []},
                {"symbol": "XLE", "name": "能源", "interval_pct": -2.0, "daily_pct": -0.5,
                 "relative_spy_pp": -3.0, "actual_end_session": "2026-10-02", "status": "ok", "daily": []},
            ],
            "stocks": [
                {"symbol": "MSFT", "name": "微软", "sector": "科技", "interval_pct": None,
                 "daily_pct": None, "actual_end_session": None, "status": "missing", "daily": []},
                {"symbol": "NVDA", "name": "英伟达", "sector": "科技", "interval_pct": 4.0,
                 "daily_pct": 1.2, "actual_end_session": "2026-10-02", "status": "ok",
                 "daily": [{"date": "2026-10-02", "price": 101.25, "change_pct": 1.2}]},
            ],
        },
        "errors": [],
    }


class RenderTests(unittest.TestCase):
    def test_unavailable_artifact_renders_without_crashing(self):
        html = render_html(None)
        markdown = render_markdown(None)
        self.assertIn("不可用", html)
        self.assertEqual(4, html.count('class="us-card"'))
        self.assertIn("数据缺失", markdown)

    def test_legacy_v1_artifact_still_renders(self):
        summary = sample_summary()
        summary["schema_version"] = "us-market-summary/v1"
        for key in ("a_share_anchor_date", "anchor_qualification", "anchor_reason"):
            summary.pop(key, None)
        html = render_html(summary)
        markdown = render_markdown(summary)
        self.assertIn("美股区间概要", html)
        self.assertIn("美股区间概要", markdown)
        self.assertIn("A股锚点资格：未核验", html)
        self.assertIn("美股锚点交易日：2026-09-30", html)

    def test_v2_missing_anchor_does_not_fall_back_to_basis_date(self):
        summary = sample_summary()
        summary.pop("a_share_anchor_date")
        summary.pop("anchor_qualification")
        summary.pop("anchor_reason")
        html = render_html(summary)
        markdown = render_markdown(summary)
        self.assertIn("美股锚点交易日：—", html)
        self.assertIn("A股锚点资格：未核验", html)
        self.assertIn("美股锚点交易日：—", markdown)

    def test_v2_missing_latest_session_does_not_use_actual_end_session(self):
        summary = sample_summary()
        summary.pop("latest_completed_session", None)
        summary["actual_end_session"] = "2026-10-01"
        html = render_html(summary)
        markdown = render_markdown(summary)
        self.assertIn("最近已完成美股交易日：—", html)
        self.assertIn("最近已完成美股交易日：—", markdown)
        self.assertNotIn("最近已完成美股交易日：2026-10-01", html)

    def test_external_values_are_escaped_and_missing_rows_sort_last(self):
        summary = sample_summary()
        summary["provider"] = '<img src=x onerror="bad">'
        summary["groups"]["stocks"][0]["name"] = "</td><script>bad()</script>"
        summary["errors"] = [{"message": "<b>failed</b>"}]
        html = render_html(summary)
        self.assertNotIn("<script>bad()</script>", html)
        self.assertNotIn("<b>failed</b>", html)
        self.assertIn("&lt;b&gt;failed&lt;/b&gt;", html)
        self.assertLess(html.index("英伟达"), html.index("&lt;/td&gt;&lt;script&gt;bad()"))

    def test_markdown_contains_sources_and_fixed_scope(self):
        markdown = render_markdown(sample_summary())
        self.assertIn("NYSE交易日历", markdown)
        self.assertIn("固定代表观察池，不代表全市场排行", markdown)
        self.assertIn("+2.00 个百分点", markdown)

    def test_v2_renders_latest_completed_session_and_anchor_qualification(self):
        summary = sample_summary()
        summary["latest_completed_session"] = "2026-10-02"
        summary["previous_session"] = "2026-10-01"
        html = render_html(summary)
        markdown = render_markdown(summary)
        self.assertIn("最近已完成美股交易日：2026-10-02", html)
        self.assertIn("A股锚点资格：已核验", html)
        self.assertIn("最近已完成美股交易日：2026-10-02", markdown)
        self.assertIn("A股锚点资格：已核验", markdown)

    def test_empty_v2_labels_no_new_session_but_keeps_latest_daily_return(self):
        summary = sample_summary()
        summary.update({
            "status": "empty",
            "data_quality": "complete",
            "sessions": [],
            "expected_end_session": None,
            "latest_completed_session": "2026-09-29",
            "previous_session": "2026-09-28",
        })
        summary["anchor_evidence"] = {
            "status": "qualified",
            "reason": "calendar_verified",
            "evidence_sha256": "anchor-fixture-sha",
        }
        for group in summary["groups"].values():
            for row in group:
                row["interval_pct"] = None
                row["actual_end_session"] = "2026-09-29"
                row["previous_price"] = 100
                row["latest_price"] = 101
        html = render_html(summary)
        markdown = render_markdown(summary)
        self.assertIn("区间无新增交易日", html)
        self.assertIn("区间无新增交易日", markdown)
        self.assertIn("最近已完成美股交易日：2026-09-29", html)
        self.assertIn("暂无新增收盘 · 完整", html)
        self.assertIn("最近一日价格 100 → 101", html)
        self.assertIn('data-anchor-evidence="anchor-fixture-sha"', html)
        self.assertIn("锚点证据摘要：anchor-fixture-sha", markdown)


class UpdateTests(unittest.TestCase):
    def _reports(self, root: Path):
        html = root / "daily-review.html"
        md = root / "daily-review.md"
        html_text = (
            "<html><body><h1>📅 今日复盘 2026-09-30</h1>"
            "<p>原始生成时间 16:44:58</p><p>市场评分34.2</p>"
            "<!-- OBSERVATION_LIST:START --><section>观察原文</section>"
            "<!-- OBSERVATION_LIST:END --><footer>免责声明</footer></body></html>"
        )
        md_text = (
            "## 📅 今日复盘 (2026-09-30)\n\n原始生成时间 16:44:58\n\n"
            "---\n> *本报告仅供学习参考，不构成任何投资建议。*\n"
        )
        html.write_text(html_text, encoding="utf-8")
        md.write_text(md_text, encoding="utf-8")
        return html, md, html_text, md_text

    def test_update_preserves_content_backs_up_once_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            html, md, html_original, md_original = self._reports(Path(directory))
            update_reports(html, sample_summary())
            first = html.read_text(encoding="utf-8")
            update_reports(html, sample_summary())
            second = html.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual(1, second.count(HTML_BLOCK_START))
            self.assertEqual(1, second.count(HTML_BLOCK_END))
            self.assertIn("原始生成时间 16:44:58", second)
            self.assertIn("市场评分34.2", second)
            self.assertIn("观察原文", second)
            self.assertLess(second.index(HTML_BLOCK_START), second.index("<!-- OBSERVATION_LIST:START -->"))
            self.assertEqual(html_original, Path(str(html) + ".pre-us-summary").read_text(encoding="utf-8"))
            self.assertEqual(md_original, Path(str(md) + ".pre-us-summary").read_text(encoding="utf-8"))
            self.assertEqual(1, md.read_text(encoding="utf-8").count(HTML_BLOCK_START))

    def test_basis_mismatch_refuses_without_writing_or_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            html, _, html_original, _ = self._reports(Path(directory))
            summary = sample_summary()
            summary["basis_date"] = "2026-10-01"
            with self.assertRaisesRegex(ValueError, "basis_date_mismatch"):
                update_reports(html, summary)
            self.assertEqual(html_original, html.read_text(encoding="utf-8"))
            self.assertFalse(Path(str(html) + ".pre-us-summary").exists())

    def test_markdown_is_optional_and_footer_is_unique_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "only.html"
            html.write_text(
                "<html><body><h1>今日复盘 2026-09-30</h1><p>keep</p><footer>disc</footer></body></html>",
                encoding="utf-8",
            )
            result = update_reports(html, sample_summary())
            self.assertIsNone(result["markdown_path"])
            content = html.read_text(encoding="utf-8")
            self.assertLess(content.index(HTML_BLOCK_START), content.index("<footer>"))
            self.assertIn("<p>keep</p>", content)


if __name__ == "__main__":
    unittest.main()
