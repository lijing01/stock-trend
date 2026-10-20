#!/usr/bin/env python3
"""Offline tests for historical daily-review sector link backfills."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis.market_regime import render_sector_rankings  # noqa: E402
from bridge.review_detail_links import ReportUpdateError  # noqa: E402
from bridge.review_sector_links import update_reports  # noqa: E402


DATA_DATE = "2026-10-20"
GENERATED_AT = "2026-10-20 13:37:44"


def frozen_context():
    top = [
        {
            "code": f"BK{1295 + index:04d}",
            "name": f"强势板块{index + 1}",
            "change_pct": 12.60 - index,
            "provider": "eastmoney",
        }
        for index in range(10)
    ]
    bottom = [
        {
            "code": f"BK{1401 + index:04d}",
            "name": f"弱势板块{index + 1}",
            "change_pct": -1.25 - index,
            "provider": "eastmoney",
        }
        for index in range(3)
    ]
    return {
        "data_date": DATA_DATE,
        "generated_at": GENERATED_AT,
        "top_sectors": top,
        "bottom_sectors": bottom,
    }


def legacy_html(context):
    top = "".join(
        f"<li><strong>{row['name']}</strong> {row['change_pct']:+.2f}%</li>"
        for row in context["top_sectors"]
    )
    bottom = "".join(
        f"<li><strong>{row['name']}</strong> {row['change_pct']:+.2f}%</li>"
        for row in context["bottom_sectors"]
    )
    return (
        "<!doctype html><html><body><div class=\"w\">\n"
        f"<h1>📅 今日复盘 {context['data_date']}</h1>\n"
        f"<p class=\"dt\">{context['generated_at']}</p>\n"
        "<p>板块前内容必须逐字保留</p>\n"
        "<h2 id=\"market-sectors\">② 板块</h2>\n"
        f"<p><strong>当日排名最强前10:</strong></p><ul>{top}</ul>\n"
        f"<p><strong>当日排名最弱前3:</strong></p><ul>{bottom}</ul>\n"
        "<section id=\"sector-persistence\"><h3>板块持续性</h3><p>原值</p></section>\n"
        "<footer>报告尾部必须逐字保留</footer></div></body></html>\n"
    )


def legacy_markdown(context):
    top = "\n".join(
        f"- {row['name']} {row['change_pct']:+.2f}%"
        for row in context["top_sectors"]
    )
    bottom = "\n".join(
        f"- {row['name']} {row['change_pct']:+.2f}%"
        for row in context["bottom_sectors"]
    )
    return f"""## 📅 今日复盘 ({context['data_date']})

▸ 生成时间: {context['generated_at']}

Markdown 板块前内容必须逐字保留

### ② 板块

**当日排名最强前10**:
{top}

**当日排名最弱前3**:
{bottom}

### 板块持续性

原值

Markdown 尾部必须逐字保留
"""


def outside_rankings(content, start_marker, end_marker):
    start = content.index(start_marker) + len(start_marker)
    end = content.index(end_marker, start)
    return content[:start], content[end:]


class ReviewSectorLinksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.context = frozen_context()
        self.context_path = self.root / "market_regime.json"
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
            backup = Path(str(path) + ".pre-sector-links")
            if backup.exists():
                backup.unlink()

    def test_updates_pair_preserves_outside_bytes_backups_and_is_idempotent(self):
        original_html = self.html.read_text(encoding="utf-8")
        original_markdown = self.markdown.read_text(encoding="utf-8")
        html_outside = outside_rankings(
            original_html,
            '<h2 id="market-sectors">② 板块</h2>',
            '<section id="sector-persistence">',
        )
        markdown_outside = outside_rankings(
            original_markdown,
            "### ② 板块\n\n",
            "### 板块持续性",
        )

        result = update_reports(self.html, self.context_path)
        updated_html = self.html.read_text(encoding="utf-8")
        updated_markdown = self.markdown.read_text(encoding="utf-8")

        self.assertEqual("updated", result["status"])
        self.assertEqual(2, len(result["written_paths"]))
        self.assertEqual(
            original_html,
            Path(str(self.html) + ".pre-sector-links").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            original_markdown,
            Path(str(self.markdown) + ".pre-sector-links").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            html_outside,
            outside_rankings(
                updated_html,
                '<h2 id="market-sectors">② 板块</h2>',
                '<section id="sector-persistence">',
            ),
        )
        self.assertEqual(
            markdown_outside,
            outside_rankings(
                updated_markdown,
                "### ② 板块\n\n",
                "### 板块持续性",
            ),
        )
        self.assertIn(render_sector_rankings(self.context, "html"), updated_html)
        self.assertIn(render_sector_rankings(self.context, "markdown"), updated_markdown)
        self.assertEqual(13, updated_html.count("quote.eastmoney.com"))
        self.assertEqual(13, updated_markdown.count("quote.eastmoney.com"))

        second = update_reports(self.html, self.context_path)
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(updated_html, self.html.read_text(encoding="utf-8"))
        self.assertEqual(updated_markdown, self.markdown.read_text(encoding="utf-8"))

    def test_context_date_time_or_list_mismatch_never_writes_reports(self):
        mutations = {
            "context-date": lambda: self.context.update(data_date="2026-10-19"),
            "context-time": lambda: self.context.update(generated_at="2026-10-20 13:38:00"),
            "html-list": lambda: self.html.write_text(
                legacy_html(self.context).replace(
                    "强势板块4</strong> +9.60%",
                    "另一板块</strong> +9.60%",
                    1,
                ),
                encoding="utf-8",
            ),
            "markdown-list": lambda: self.markdown.write_text(
                legacy_markdown(self.context).replace("弱势板块2 -2.25%", "另一板块 -2.25%", 1),
                encoding="utf-8",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.context = frozen_context()
                self._reset_files()
                mutate()
                self.context_path.write_text(
                    json.dumps(self.context, ensure_ascii=False), encoding="utf-8"
                )
                original_html = self.html.read_text(encoding="utf-8")
                original_markdown = self.markdown.read_text(encoding="utf-8")
                with self.assertRaises(ValueError):
                    update_reports(self.html, self.context_path)
                self.assertEqual(original_html, self.html.read_text(encoding="utf-8"))
                self.assertEqual(original_markdown, self.markdown.read_text(encoding="utf-8"))
                self.assertFalse(Path(str(self.html) + ".pre-sector-links").exists())
                self.assertFalse(Path(str(self.markdown) + ".pre-sector-links").exists())

    def test_missing_companion_rejected_without_html_write(self):
        original_html = self.html.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "html_report_path_required"):
            update_reports(self.markdown, self.context_path)
        self.markdown.unlink()

        with self.assertRaisesRegex(ValueError, "markdown_companion_missing"):
            update_reports(self.html, self.context_path)

        self.assertEqual(original_html, self.html.read_text(encoding="utf-8"))
        self.assertFalse(Path(str(self.html) + ".pre-sector-links").exists())

    def test_partial_write_reports_path_and_retry_recovers(self):
        from core import report_file

        real_atomic_write = report_file.atomic_write_text

        def fail_markdown_report(path, text):
            if Path(path) == self.markdown:
                raise OSError("simulated markdown write failure")
            return real_atomic_write(path, text)

        with patch.object(report_file, "atomic_write_text", side_effect=fail_markdown_report):
            with self.assertRaises(ReportUpdateError) as raised:
                update_reports(self.html, self.context_path)

        self.assertEqual("partial", raised.exception.status)
        self.assertEqual([str(self.html.resolve())], list(raised.exception.written_paths))
        self.assertIn(
            render_sector_rankings(self.context, "html"),
            self.html.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            render_sector_rankings(self.context, "markdown"),
            self.markdown.read_text(encoding="utf-8"),
        )

        recovered = update_reports(self.html, self.context_path)
        self.assertEqual("updated", recovered["status"])
        self.assertEqual([str(self.markdown.resolve())], recovered["written_paths"])
        self.assertIn(
            render_sector_rankings(self.context, "markdown"),
            self.markdown.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
