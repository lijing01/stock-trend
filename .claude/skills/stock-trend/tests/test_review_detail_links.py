#!/usr/bin/env python3
"""Offline tests for patching component detail links into historical reviews."""

import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from bridge.review_detail_links import ReportUpdateError, update_reports  # noqa: E402
from core.report_file import atomic_write_text, report_lock  # noqa: E402


COMPONENTS = (
    ("index_trend", "大盘趋势", "0.0", "000001.SH 收盘下MA20↓; 000300.SH 收盘下MA20↓; 399001.SZ 收盘下MA20↓"),
    ("volume", "成交额", "17.6", "两市 14380亿,较20日均额 -22%"),
    ("breadth", "赚钱效应", "49.5", "涨跌 2561/2819,行业板块上涨占比 54%"),
    ("zt_emotion", "涨停情绪", "70.6", "涨停 52家(连板12,最高7板;历史不足,按绝对家数)+连板加成20"),
    ("capital", "资金", "42.1", "全市场主力净流入 -131.2亿"),
)


def md_cell(value):
    return value.replace("|", r"\|")


def legacy_html():
    summary_rows = "".join(
        f"<tr><td>{name}</td><td><strong>{score}</strong></td><td>{detail}</td></tr>"
        for _, name, score, detail in COMPONENTS
    )
    explanation_rows = "".join(
        "<tr>"
        f"<td>{name}<br><small>{key}</small></td><td>{score}</td>"
        "<td>0.2</td><td>1</td><td>completeness=complete</td>"
        f"<td>{detail}</td></tr>"
        for key, name, score, detail in COMPONENTS
    )
    return (
        '<!doctype html><html><body><div class="w">'
        "<h1>📅 今日复盘 2026-09-30</h1>"
        '<p class="dt">2026-10-03 16:44:58</p>'
        '<p class="dt">⚠️ 非交易日说明保留</p>'
        "<h2>① 市场环境</h2>"
        "<table><thead><tr><th>组件</th><th>得分</th><th>说明</th></tr></thead>"
        f"<tbody>{summary_rows}</tbody></table>"
        '<section class="market-explanation"><h2>市场环境解释</h2>'
        "<p>口径：close；基准日 2026-09-30；归一化分母 1.0</p>"
        "<!-- 评分解释历史注释保留 -->"
        "<table><thead><tr><th>组件</th><th>得分</th><th>权重</th><th>贡献</th>"
        f"<th>证据资格</th><th>说明</th></tr></thead><tbody>{explanation_rows}</tbody></table>"
        "</section>"
        "<h2>② 板块</h2><p>板块内容</p>"
        "<!-- US_MARKET_SUMMARY:START --><section>美股保留</section>"
        "<!-- US_MARKET_SUMMARY:END -->"
        "<!-- OBSERVATION_LIST:START --><section>观察列表原值</section>"
        "<!-- OBSERVATION_LIST:END -->"
        "<footer>免责声明</footer></div></body></html>"
    )


def legacy_markdown():
    summary_rows = "\n".join(
        f"| {name} | **{score}** | {md_cell(detail)} |"
        for _, name, score, detail in COMPONENTS
    )
    explanation_rows = "\n".join(
        f"| {name} ({key}) | {score} | 0.2 | 1 | completeness=complete | "
        f"{md_cell(detail)} |"
        for key, name, score, detail in COMPONENTS
    )
    return f"""## 📅 今日复盘 (2026-09-30)

▸ 生成时间: 2026-10-03 16:44:58

### ① 市场环境

| 组件 | 得分 | 说明 |
|---|---:|---|
{summary_rows}

### 市场环境解释

- 口径：close；基准日 2026-09-30；归一化分母 1.0

| 组件 | 得分 | 权重 | 贡献 | 证据资格 | 说明 |
|---|---:|---:|---:|---|---|
{explanation_rows}

### ② 板块

板块内容

<!-- US_MARKET_SUMMARY:START -->
美股保留
<!-- US_MARKET_SUMMARY:END -->

<!-- OBSERVATION_LIST:START -->
观察列表原值
<!-- OBSERVATION_LIST:END -->

---
免责声明
"""


class ReviewDetailLinksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.html = self.root / "daily-review.html"
        self.md = self.html.with_suffix(".md")
        self.html.write_text(legacy_html(), encoding="utf-8")
        self.md.write_text(legacy_markdown(), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_updates_both_reports_preserves_other_blocks_and_is_idempotent(self):
        original_html = self.html.read_text(encoding="utf-8")
        original_md = self.md.read_text(encoding="utf-8")
        original_explanation_table = re.search(
            r"<table><thead><tr><th>组件</th><th>得分</th><th>权重</th>.*?</table>",
            original_html,
            re.DOTALL,
        ).group(0)

        result = update_reports(self.html, "2026-09-30")
        updated_html = self.html.read_text(encoding="utf-8")
        updated_md = self.md.read_text(encoding="utf-8")

        self.assertEqual("updated", result["status"])
        self.assertEqual(2, len(result["written_paths"]))
        self.assertEqual(original_html, Path(str(self.html) + ".pre-detail-links").read_text(encoding="utf-8"))
        self.assertEqual(original_md, Path(str(self.md) + ".pre-detail-links").read_text(encoding="utf-8"))
        for content in (updated_html, updated_md):
            self.assertEqual(5, content.count("查看详情"))
            self.assertEqual(1, content.count("MARKET_DETAIL_LINKS:START"))
            self.assertEqual(1, content.count("MARKET_DETAIL_LINKS:END"))
            self.assertIn("美股保留", content)
            self.assertIn("观察列表原值", content)
            self.assertIn("免责声明", content)
        self.assertEqual(1, updated_html.count('id="market-component-summary"'))
        self.assertEqual(1, updated_md.count('id="market-component-summary"'))
        self.assertEqual(1, updated_html.count('class="summary-table-wrap"'))
        self.assertIn('style="overflow-x:auto;max-width:100%"', updated_html)
        self.assertIn("#market-component-summary{min-width:540px}", updated_html)
        self.assertLess(updated_html.index("market-explanation"), updated_html.index("MARKET_DETAIL_LINKS:START"))
        self.assertLess(updated_html.index("MARKET_DETAIL_LINKS:END"), updated_html.index("② 板块"))
        self.assertEqual(5, len(re.findall(r'<details class="market-detail"', updated_html)))
        self.assertIn('<details class="market-explanation">', updated_html)
        self.assertIn("<summary>评分解释</summary>", updated_html)
        self.assertIn('class="market-explanation-table-wrap"', updated_html)
        self.assertIn("table{min-width:760px}", updated_html)
        self.assertIn("<!-- 评分解释历史注释保留 -->", updated_html)
        self.assertIn(original_explanation_table, updated_html)

        second = update_reports(self.html, "2026-09-30")
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(updated_html, self.html.read_text(encoding="utf-8"))
        self.assertEqual(updated_md, self.md.read_text(encoding="utf-8"))
        self.assertEqual(1, self.html.read_text(encoding="utf-8").count('class="summary-table-wrap"'))

    def test_existing_generated_block_is_folded_without_recomputing_content(self):
        articles = "".join(
            f'<article class="market-detail" id="market-detail-{key}">'
            f"<h3>{name}</h3><!-- frozen-{key} --><p>历史冻结内容 {score}</p></article>"
            for key, name, score, _ in COMPONENTS
        )
        old_block = (
            "<!-- MARKET_DETAIL_LINKS:START -->"
            '<section class="market-detail-links"><h2>历史详情</h2>'
            f"{articles}</section><!-- MARKET_DETAIL_LINKS:END -->"
        )
        html_text = legacy_html().replace("<h2>② 板块</h2>", old_block + "<h2>② 板块</h2>")
        self.html.write_text(html_text, encoding="utf-8")

        update_reports(self.html, "2026-09-30")
        updated = self.html.read_text(encoding="utf-8")

        self.assertEqual(5, len(re.findall(r'<details class="market-detail"', updated)))
        self.assertNotIn('<article class="market-detail"', updated)
        for key, _, score, _ in COMPONENTS:
            self.assertIn(f"<!-- frozen-{key} -->", updated)
            self.assertIn(f"历史冻结内容 {score}", updated)
        second = update_reports(self.html, "2026-09-30")
        self.assertEqual("unchanged", second["status"])

    def test_complete_generated_reports_are_idempotent_and_keep_concurrent_update(self):
        from analysis import market_regime as market
        from test_market_detail_links import report_context

        ctx = report_context()
        state = {"status": "unavailable", "reason": "完整样本观察列表"}
        self.html.write_text(
            market._generate_html(ctx, "fixture", observation_state=state),
            encoding="utf-8",
        )
        self.md.write_text(
            market.generate_report(ctx, observation_state=state),
            encoding="utf-8",
        )

        first = update_reports(self.html, "2026-09-30")
        second = update_reports(self.html, "2026-09-30")
        self.assertEqual("unchanged", first["status"])
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(
            1,
            self.html.read_text(encoding="utf-8").count('class="summary-table-wrap"'),
        )

        lock_held = threading.Event()
        release = threading.Event()

        def observation_writer():
            with report_lock(self.html):
                current = self.html.read_text(encoding="utf-8")
                current = current.replace("完整样本观察列表", "完整样本并发更新")
                atomic_write_text(self.html, current)
                lock_held.set()
                release.wait(timeout=2)

        thread = threading.Thread(target=observation_writer)
        thread.start()
        self.assertTrue(lock_held.wait(timeout=2))
        result = {}
        detail_thread = threading.Thread(
            target=lambda: result.update(update_reports(self.html, "2026-09-30"))
        )
        detail_thread.start()
        time.sleep(0.03)
        release.set()
        thread.join(timeout=2)
        detail_thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(detail_thread.is_alive())
        self.assertEqual("unchanged", result["status"])
        self.assertIn("完整样本并发更新", self.html.read_text(encoding="utf-8"))

    def test_wrong_date_missing_or_duplicate_component_never_writes(self):
        cases = [
            ("wrong-date", legacy_html(), legacy_markdown(), "2026-10-01"),
            (
                "missing",
                legacy_html().replace(
                    "<tr><td>资金</td><td><strong>42.1</strong></td><td>全市场主力净流入 -131.2亿</td></tr>",
                    "",
                    1,
                ),
                legacy_markdown(),
                "2026-09-30",
            ),
            (
                "duplicate",
                legacy_html().replace(
                    "</tbody></table><section class=\"market-explanation\">",
                    "<tr><td>资金</td><td><strong>42.1</strong></td><td>重复</td></tr>"
                    "</tbody></table><section class=\"market-explanation\">",
                    1,
                ),
                legacy_markdown(),
                "2026-09-30",
            ),
        ]
        for label, html_text, md_text, basis in cases:
            with self.subTest(label=label):
                self.html.write_text(html_text, encoding="utf-8")
                self.md.write_text(md_text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    update_reports(self.html, basis)
                self.assertEqual(html_text, self.html.read_text(encoding="utf-8"))
                self.assertEqual(md_text, self.md.read_text(encoding="utf-8"))
                self.assertFalse(Path(str(self.html) + ".pre-detail-links").exists())
                self.assertFalse(Path(str(self.md) + ".pre-detail-links").exists())

    def test_markdown_mismatch_or_wrong_date_never_writes_html(self):
        for label, bad_markdown in (
            ("score", legacy_markdown().replace("| 成交额 | **17.6**", "| 成交额 | **99.9**", 1)),
            ("date", legacy_markdown().replace("今日复盘 (2026-09-30)", "今日复盘 (2026-10-01)", 1)),
            (
                "extra-row",
                legacy_markdown().replace(
                    "\n\n### 市场环境解释",
                    "\n| 资金 | **42.1** | 重复 |\n\n### 市场环境解释",
                    1,
                ),
            ),
            (
                "duplicate-heading",
                legacy_markdown().replace(
                    "### ② 板块", "### ① 市场环境\n\n### ② 板块", 1
                ),
            ),
        ):
            with self.subTest(label=label):
                self.html.write_text(legacy_html(), encoding="utf-8")
                self.md.write_text(bad_markdown, encoding="utf-8")
                with self.assertRaises(ValueError):
                    update_reports(self.html, "2026-09-30")
                self.assertEqual(legacy_html(), self.html.read_text(encoding="utf-8"))
                self.assertEqual(bad_markdown, self.md.read_text(encoding="utf-8"))

    def test_invalid_calendar_date_and_conflicting_table_anchor_never_write(self):
        original_html = self.html.read_text(encoding="utf-8")
        original_md = self.md.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "basis_date_invalid"):
            update_reports(self.html, "2026-02-30")
        self.assertEqual(original_html, self.html.read_text(encoding="utf-8"))
        self.assertEqual(original_md, self.md.read_text(encoding="utf-8"))

        conflicted = original_html.replace("<table><thead>", '<table id="other"><thead>', 1)
        self.html.write_text(conflicted, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "table_id_conflict"):
            update_reports(self.html, "2026-09-30")
        self.assertEqual(conflicted, self.html.read_text(encoding="utf-8"))
        self.assertEqual(original_md, self.md.read_text(encoding="utf-8"))

    def test_shared_lock_preserves_concurrent_observation_update(self):
        lock_held = threading.Event()
        release = threading.Event()

        def observation_writer():
            with report_lock(self.html):
                current = self.html.read_text(encoding="utf-8")
                current = current.replace("观察列表原值", "观察列表并发更新")
                atomic_write_text(self.html, current)
                lock_held.set()
                release.wait(timeout=2)

        thread = threading.Thread(target=observation_writer)
        thread.start()
        self.assertTrue(lock_held.wait(timeout=2))

        result = {}

        def detail_writer():
            result.update(update_reports(self.html, "2026-09-30"))

        detail_thread = threading.Thread(target=detail_writer)
        detail_thread.start()
        time.sleep(0.03)
        release.set()
        thread.join(timeout=2)
        detail_thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(detail_thread.is_alive())
        self.assertEqual("updated", result["status"])
        content = self.html.read_text(encoding="utf-8")
        self.assertIn("观察列表并发更新", content)
        self.assertIn("美股保留", content)
        self.assertEqual(5, content.count("查看详情"))

    def test_partial_write_error_reports_written_paths_and_rerun_recovers(self):
        from core import report_file

        real_atomic_write = report_file.atomic_write_text

        def fail_markdown_visible(path, text):
            path = Path(path)
            if path == self.md:
                raise OSError("simulated markdown failure")
            return real_atomic_write(path, text)

        with patch.object(report_file, "atomic_write_text", side_effect=fail_markdown_visible):
            with self.assertRaises(ReportUpdateError) as raised:
                update_reports(self.html, "2026-09-30")

        error = raised.exception
        self.assertEqual("partial", error.status)
        self.assertEqual([str(self.html.resolve())], list(error.written_paths))
        self.assertEqual(5, self.html.read_text(encoding="utf-8").count("查看详情"))
        self.assertEqual(0, self.md.read_text(encoding="utf-8").count("查看详情"))

        recovered = update_reports(self.html, "2026-09-30")
        self.assertEqual("updated", recovered["status"])
        self.assertEqual(5, self.md.read_text(encoding="utf-8").count("查看详情"))


if __name__ == "__main__":
    unittest.main()
