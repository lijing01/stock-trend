#!/usr/bin/env python3
"""Offline tests for historical market-observation backfills."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis import market_regime  # noqa: E402
from bridge.review_detail_links import ReportUpdateError  # noqa: E402
from bridge.review_market_observations import update_reports  # noqa: E402
from reporting.market_detail_links import render_details  # noqa: E402


COMPONENTS = (
    ("index_trend", "大盘趋势", 0.0, "三大指数均位于MA20下方"),
    ("volume", "成交额", 17.6, "两市 14380亿,较20日均额 -22%"),
    ("breadth", "赚钱效应", 49.5, "涨跌 2561/2819,行业板块上涨占比 54%"),
    ("zt_emotion", "涨停情绪", 70.6, "原始涨停 52家；模型外推 78家"),
    ("capital", "资金", 42.1, "全市场主力净流入 -131.2亿"),
)


def frozen_context():
    return {
        "data_date": "2026-10-20",
        "generated_at": "2026-10-20 13:37:44",
        "regime": {"score": 46.2, "label": "弱势", "advice": "降仓观察"},
        "components": {
            key: {"score": score, "detail": detail}
            for key, _, score, detail in COMPONENTS
        },
        "detail_inputs": {
            "index_trend": {"indices": []},
            "volume": {"amount_yi": 14380, "history_average_yi": 18435.9},
            "breadth": {"up": 2561, "down": 2819},
            "zt_emotion": {"count": 52, "streak_count": 12, "max_streak": 7},
            "capital": {"main_force_yi": -131.2},
        },
    }


def _old_html_overview():
    rows = "".join(
        f"<tr><td>{name}</td><td><strong>{score}</strong></td><td>{detail}</td>"
        '<td><a href="#old">查看详情</a></td></tr>'
        for _, name, score, detail in COMPONENTS
    )
    return (
        "<h2>① 市场环境</h2>\n"
        '<div class="summary-table-wrap"><table id="market-component-summary">'
        "<thead><tr><th>组件</th><th>得分</th><th>说明</th><th>详情</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>\n"
        '<p class="old-observation">涨跌家数与模型结果混合展示</p>\n'
    )


def _old_markdown_overview():
    rows = "\n".join(
        f"| {name} | **{score}** | {detail.replace('|', chr(92) + '|')} | [查看详情](#old) |"
        for _, name, score, detail in COMPONENTS
    )
    return (
        "### ① 市场环境\n\n"
        '<a id="market-component-summary"></a>\n\n'
        "| 组件 | 得分 | 说明 | 详情 |\n"
        "|---|---:|---|---|\n"
        f"{rows}\n\n"
        "▸ 涨跌家数与模型结果混合展示\n\n"
    )


def _old_details(ctx, output_format):
    current = render_details(ctx, output_format)
    return current.replace("市场评分详情与财经入口", "旧版五项详情", 1)


def legacy_html(ctx):
    return (
        "<!doctype html><html><body><div class=\"w\">\n"
        f"<h1>📅 今日复盘 {ctx['data_date']}</h1>\n"
        f"<p class=\"dt\">{ctx['generated_at']}</p>\n"
        "<p>市场环境前字节保留</p>\n"
        f"{_old_html_overview()}"
        '<h2 id="market-sectors">② 板块</h2>\n'
        "<section>板块、美股、观察列表保持原样</section>\n"
        f"{_old_details(ctx, 'html')}\n"
        "<footer>尾部保持原样</footer></div></body></html>\n"
    )


def legacy_markdown(ctx):
    return (
        f"## 📅 今日复盘 ({ctx['data_date']})\n\n"
        f"▸ 生成时间: {ctx['generated_at']}\n\n"
        "Markdown 市场环境前字节保留\n\n"
        f"{_old_markdown_overview()}"
        '<a id="market-sectors"></a>\n'
        "### ② 板块\n\n"
        "板块、美股、观察列表保持原样\n\n"
        f"{_old_details(ctx, 'markdown')}\n\n"
        "---\n尾部保持原样\n"
    )


def mask_changed_blocks(content, output_format):
    if output_format == "html":
        content = re.sub(
            r"<h2>① 市场环境</h2>.*?(?=<h2 id=\"market-sectors\">)",
            "<OVERVIEW>", content, flags=re.DOTALL,
        )
    else:
        content = re.sub(
            r"(?m)^### ① 市场环境$.*?(?=^<a id=\"market-sectors\"></a>$)",
            "<OVERVIEW>\n", content, flags=re.DOTALL | re.MULTILINE,
        )
    return re.sub(
        r"<!-- MARKET_DETAIL_LINKS:START -->.*?<!-- MARKET_DETAIL_LINKS:END -->",
        "<DETAILS>", content, flags=re.DOTALL,
    )


class ReviewMarketObservationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.context = frozen_context()
        self.context_path = self.root / "frozen-context.json"
        self.html = self.root / "daily-review.html"
        self.markdown = self.html.with_suffix(".md")
        self._reset_files()

    def tearDown(self):
        self.temporary.cleanup()

    def _reset_files(self):
        self.context_path.write_text(
            json.dumps(self.context, ensure_ascii=False), encoding="utf-8"
        )
        self.html.write_text(legacy_html(self.context), encoding="utf-8")
        self.markdown.write_text(legacy_markdown(self.context), encoding="utf-8")
        for path in (self.html, self.markdown):
            backup = Path(str(path) + ".pre-p0-observations")
            if backup.exists():
                backup.unlink()

    def _run(self):
        self.assertTrue(callable(market_regime.render_market_overview))
        return update_reports(self.html, self.context_path)

    def test_updates_pair_preserves_scores_outside_bytes_backups_and_is_idempotent(self):
        original_html = self.html.read_text(encoding="utf-8")
        original_markdown = self.markdown.read_text(encoding="utf-8")

        first = self._run()
        updated_html = self.html.read_text(encoding="utf-8")
        updated_markdown = self.markdown.read_text(encoding="utf-8")

        self.assertEqual("updated", first["status"])
        self.assertEqual(2, len(first["written_paths"]))
        self.assertEqual(
            mask_changed_blocks(original_html, "html"),
            mask_changed_blocks(updated_html, "html"),
        )
        self.assertEqual(
            mask_changed_blocks(original_markdown, "markdown"),
            mask_changed_blocks(updated_markdown, "markdown"),
        )
        for _, _, score, detail in COMPONENTS:
            for content in (updated_html, updated_markdown):
                self.assertIn(str(score), content)
                self.assertIn(detail, content)
        for content in (updated_html, updated_markdown):
            self.assertEqual(1, content.count("MARKET_OBSERVATIONS:START"))
            self.assertEqual(1, content.count("MARKET_DETAIL_LINKS:START"))
            self.assertIn("原始观测", content)
            self.assertIn("模型输入", content)
        self.assertEqual(
            original_html,
            Path(str(self.html) + ".pre-p0-observations").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            original_markdown,
            Path(str(self.markdown) + ".pre-p0-observations").read_text(encoding="utf-8"),
        )

        second = self._run()
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(updated_html, self.html.read_text(encoding="utf-8"))
        self.assertEqual(updated_markdown, self.markdown.read_text(encoding="utf-8"))

    def test_date_time_and_each_component_mismatch_reject_without_writes(self):
        mutations = [
            ("date", lambda: self.context.update(data_date="2026-10-19")),
            ("time", lambda: self.context.update(generated_at="2026-10-20 13:38:00")),
        ]
        for key, _, _, _ in COMPONENTS:
            mutations.append((key + "-score", lambda key=key: self.context["components"][key].update(score=99.9)))
            mutations.append((key + "-detail", lambda key=key: self.context["components"][key].update(detail="不匹配详情")))
        for label, mutate in mutations:
            with self.subTest(label=label):
                self.context = frozen_context()
                self._reset_files()
                mutate()
                self.context_path.write_text(
                    json.dumps(self.context, ensure_ascii=False), encoding="utf-8"
                )
                old_html = self.html.read_text(encoding="utf-8")
                old_markdown = self.markdown.read_text(encoding="utf-8")
                with self.assertRaises(ValueError):
                    self._run()
                self.assertEqual(old_html, self.html.read_text(encoding="utf-8"))
                self.assertEqual(old_markdown, self.markdown.read_text(encoding="utf-8"))
                self.assertFalse(Path(str(self.html) + ".pre-p0-observations").exists())
                self.assertFalse(Path(str(self.markdown) + ".pre-p0-observations").exists())

    def test_wrong_suffix_and_missing_markdown_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "html_report_path_required"):
            update_reports(self.markdown, self.context_path)
        self.markdown.unlink()
        original = self.html.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "markdown_companion_missing"):
            update_reports(self.html, self.context_path)
        self.assertEqual(original, self.html.read_text(encoding="utf-8"))

    def test_each_report_identity_and_detail_mismatch_rejects_before_writes(self):
        detail = self.context["components"]["capital"]["detail"]
        html_with_bad_detail = legacy_html(self.context)
        prefix, separator, suffix = html_with_bad_detail.rpartition(detail)
        self.assertTrue(separator)
        html_with_bad_detail = prefix + "详情被篡改" + suffix
        markdown_with_bad_detail = legacy_markdown(self.context)
        prefix, separator, suffix = markdown_with_bad_detail.rpartition(detail)
        self.assertTrue(separator)
        markdown_with_bad_detail = prefix + "详情被篡改" + suffix
        cases = (
            ("html-date", legacy_html(self.context).replace("2026-10-20</h1>", "2026-10-19</h1>", 1), legacy_markdown(self.context)),
            ("markdown-date", legacy_html(self.context), legacy_markdown(self.context).replace("(2026-10-20)", "(2026-10-19)", 1)),
            ("html-time", legacy_html(self.context).replace("13:37:44</p>", "13:38:00</p>", 1), legacy_markdown(self.context)),
            ("markdown-time", legacy_html(self.context), legacy_markdown(self.context).replace("13:37:44", "13:38:00", 1)),
            ("html-detail", html_with_bad_detail, legacy_markdown(self.context)),
            ("markdown-detail", legacy_html(self.context), markdown_with_bad_detail),
        )
        for label, html_text, markdown_text in cases:
            with self.subTest(label=label):
                self.html.write_text(html_text, encoding="utf-8")
                self.markdown.write_text(markdown_text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self._run()
                self.assertEqual(html_text, self.html.read_text(encoding="utf-8"))
                self.assertEqual(markdown_text, self.markdown.read_text(encoding="utf-8"))
                self.assertFalse(Path(str(self.html) + ".pre-p0-observations").exists())
                self.assertFalse(Path(str(self.markdown) + ".pre-p0-observations").exists())

    def test_reads_only_explicit_pair_and_context(self):
        real_read_text = Path.read_text
        observed = []

        def tracked_read_text(path, *args, **kwargs):
            observed.append(Path(path).resolve())
            return real_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=tracked_read_text):
            self._run()
        self.assertEqual(
            {self.html.resolve(), self.markdown.resolve(), self.context_path.resolve()},
            set(observed),
        )

    def test_partial_write_reports_path_and_retry_recovers(self):
        from core import report_file

        real_atomic_write = report_file.atomic_write_text

        def fail_markdown(path, text):
            if Path(path) == self.markdown:
                raise OSError("simulated markdown write failure")
            return real_atomic_write(path, text)

        with patch.object(report_file, "atomic_write_text", side_effect=fail_markdown):
            with self.assertRaises(ReportUpdateError) as raised:
                self._run()
        self.assertEqual("partial", raised.exception.status)
        self.assertEqual([str(self.html.resolve())], list(raised.exception.written_paths))
        self.assertIn("MARKET_OBSERVATIONS:START", self.html.read_text(encoding="utf-8"))
        self.assertNotIn("MARKET_OBSERVATIONS:START", self.markdown.read_text(encoding="utf-8"))

        recovered = self._run()
        self.assertEqual("updated", recovered["status"])
        self.assertEqual([str(self.markdown.resolve())], recovered["written_paths"])
        self.assertIn(
            "MARKET_OBSERVATIONS:START", self.markdown.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
