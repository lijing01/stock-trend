#!/usr/bin/env python3
"""Offline contract tests for daily-review component detail links."""

import copy
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import market_regime as mr
from reporting import market_detail_links as detail_links


COMPONENTS = [
    ("index_trend", "大盘趋势"),
    ("volume", "成交额"),
    ("breadth", "赚钱效应"),
    ("zt_emotion", "涨停情绪"),
    ("capital", "资金"),
]


def report_context():
    return {
        "generated_at": "2026-10-03 16:44:58",
        "data_date": "2026-09-30",
        "regime": {
            "score": 34.2,
            "label": "弱势",
            "advice": "降仓观察",
            "data_quality": "partial",
            "missing_components": [],
            "partial_components": ["breadth", "zt_emotion", "capital"],
        },
        "components": {
            "index_trend": {
                "score": 0.0,
                "detail": "000001.SH 收盘下MA20↓; 000300.SH 收盘下MA20↓; 399001.SZ 收盘下MA20↓",
                "data_status": "good",
            },
            "volume": {
                "score": 17.6,
                "detail": "两市 14380亿,较20日均额 -22%",
                "data_status": "good",
            },
            "breadth": {
                "score": 49.5,
                "detail": "涨跌 2561/2819,行业板块上涨占比 54%",
                "up": 2561,
                "down": 2819,
                "data_status": "partial",
            },
            "zt_emotion": {
                "score": 70.6,
                "detail": "涨停 52家(连板12,最高7板;历史不足,按绝对家数)+连板加成20",
                "data_status": "partial",
            },
            "capital": {
                "score": 42.1,
                "detail": "全市场主力净流入 -131.2亿",
                "data_status": "partial",
            },
        },
        "indices": {
            "000001.SH": {"ok": True, "close": 3882.78, "ma20": 3890.1},
            "000300.SH": {"ok": True, "close": 4638.1, "ma20": 4650.2},
            "399001.SZ": {"ok": True, "close": 13526.51, "ma20": 13610.4},
        },
        "index_data_quality": {},
        "amount_yi": 14380,
        "zt": {"count": 52, "streak_count": 12, "max_streak": 7},
        "top_sectors": [],
        "bottom_sectors": [],
        "stale_note": "非交易日，指数依据最近交易日 2026-09-30。",
        "intraday_note": "",
    }


class MarketDetailLinksTests(unittest.TestCase):
    def test_component_order_links_and_official_source_mapping(self):
        self.assertEqual(list(detail_links.COMPONENTS), COMPONENTS)

        html = detail_links.render_details(report_context(), format="html")
        markdown = detail_links.render_details(report_context(), format="markdown")

        self.assertIn("<style>", html)
        self.assertIn(".market-detail-links", html)
        self.assertIn(".market-detail", html)

        for key, name in COMPONENTS:
            target = f"market-detail-{key}"
            self.assertIn(target, detail_links.summary_link(key, format="html"))
            self.assertIn(target, detail_links.summary_link(key, format="markdown"))
            self.assertIn(name, html)
            self.assertIn(name, markdown)

        expected_sources = {
            "https://quote.eastmoney.com/q/1.000001.html": "上证",
            "https://quote.eastmoney.com/q/1.000300.html": "沪深300",
            "https://quote.eastmoney.com/q/0.399001.html": "深证成指",
            "https://stock.10jqka.com.cn/wenduji/": "两市成交",
            "https://quote.eastmoney.com/center/gridlist.html#hs_a_board": "沪深京A股",
            "https://quote.eastmoney.com/center/gridlist.html#industry_board": "行业",
            "https://quote.eastmoney.com/ztb/?from=ztzt": "涨停",
            "https://data.eastmoney.com/zjlx/dpzjlx.html": "资金",
        }
        for url, label in expected_sources.items():
            for rendered in (html, markdown):
                self.assertIn(url, rendered)
                self.assertIn(label, rendered)

        self.assertNotIn("399106.html", html)
        self.assertNotIn("399106.html", markdown)

    def test_report_basis_date_is_not_replaced_by_component_evidence_date(self):
        ctx = report_context()
        ctx["market_explanation"] = {
            "basis_date": "2026-09-30",
            "components": [
                {
                    "id": "volume",
                    "score": 17.6,
                    "detail": "冻结解释",
                    "evidence": {
                        "data_date": "2026-09-29",
                        "source_timestamp": "2026-09-29T15:00:00+08:00",
                    },
                }
            ],
        }

        for rendered in (
            detail_links.render_details(ctx, format="html"),
            detail_links.render_details(ctx, format="markdown"),
        ):
            self.assertIn("报告依据日：2026-09-30", rendered)
            self.assertIn("组件证据日：2026-09-29", rendered)
            self.assertIn("来源事件时间：2026-09-29T15:00:00+08:00", rendered)

    def test_index_raw_close_is_shown_but_missing_ma20_is_not_inferred(self):
        ctx = report_context()
        ctx["indices"] = {
            "000001.SH": {"ok": True, "close": 3882.78},
            "000300.SH": {"ok": True, "close": 4638.1},
            "399001.SZ": {"ok": True, "close": 13526.51},
        }

        for rendered in (
            detail_links.render_details(ctx, format="html"),
            detail_links.render_details(ctx, format="markdown"),
        ):
            for close in ("3882.78", "4638.1", "13526.51"):
                self.assertIn(close, rendered)
            self.assertRegex(rendered, r"MA20.{0,20}未保存")

    def test_rendering_escapes_frozen_detail_in_html_and_markdown(self):
        ctx = report_context()
        attack = '<script>alert("x")</script> [伪链接](javascript:alert(1))'
        ctx["components"]["volume"]["detail"] = attack

        html = detail_links.render_details(ctx, format="html")
        markdown = detail_links.render_details(ctx, format="markdown")

        for rendered in (html, markdown):
            self.assertNotIn("<script>", rendered)
            self.assertNotIn('href="javascript:', rendered)
            self.assertIn("伪链接", rendered)
        self.assertNotIn("[伪链接](javascript:", markdown)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn(r"\<script\>", markdown)
        self.assertIn(r"\[伪链接\]", markdown)

    def test_missing_legacy_values_are_explicit_without_inventing_evidence(self):
        ctx = {
            "data_date": "2026-09-30",
            "components": {key: {} for key, _ in COMPONENTS},
        }

        for rendered in (
            detail_links.render_details(ctx, format="html"),
            detail_links.render_details(ctx, format="markdown"),
        ):
            self.assertNotIn("None", rendered)
            self.assertNotIn("nan", rendered.lower())
            self.assertNotRegex(rendered, r"20日均额\s*[=:：]\s*[-+]?\d")
            self.assertNotRegex(rendered, r"MA20\s*[=:：]\s*[-+]?\d")
            self.assertNotRegex(rendered, r"收盘价\s*[=:：]\s*[-+]?\d")
            self.assertRegex(rendered, r"未(?:保存|记录|提供|知)|—")

    def test_rendering_is_pure_and_keeps_frozen_scores_and_context(self):
        ctx = report_context()
        before = copy.deepcopy(ctx)

        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            html = detail_links.render_details(ctx, format="html")
            markdown = detail_links.render_details(ctx, format="markdown")

        self.assertEqual(ctx, before)
        for key, _ in COMPONENTS:
            score = str(before["components"][key]["score"])
            detail = before["components"][key]["detail"]
            self.assertIn(score, html)
            self.assertIn(score, markdown)
            self.assertIn(detail, html)
            self.assertIn(detail, markdown)

    def test_daily_review_reports_have_five_stable_summary_targets_and_details(self):
        ctx = report_context()
        before = copy.deepcopy(ctx)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            mr, "CACHE_DIR", Path(directory)
        ), patch.object(
            mr,
            "load_observation_analysis",
            return_value={"status": "unavailable", "items": [], "reason": "offline fixture"},
        ) as observation_load, patch(
            "urllib.request.urlopen", side_effect=AssertionError("network forbidden")
        ):
            markdown = mr.generate_report(ctx)
            html = mr._generate_html(ctx, "20261003-164458")
            self.assertEqual(list(Path(directory).iterdir()), [])

        observation_load.assert_called_once_with("2026-09-30")
        self.assertEqual(ctx, before)
        self.assertEqual(markdown.count("查看详情"), 5)
        self.assertEqual(html.count("查看详情"), 5)

        for key, _ in COMPONENTS:
            target = f"market-detail-{key}"
            self.assertEqual(markdown.count(f"](#{target})"), 1)
            self.assertEqual(html.count(f'href="#{target}"'), 1)
            self.assertEqual(len(re.findall(rf'id=["\']{re.escape(target)}["\']', html)), 1)
            self.assertEqual(len(re.findall(rf'id=["\']{re.escape(target)}["\']', markdown)), 1)

        self.assertEqual(len(re.findall(r'id=["\']market-component-summary["\']', html)), 1)
        self.assertEqual(len(re.findall(r'id=["\']market-component-summary["\']', markdown)), 1)
        self.assertIn('<a id="market-component-summary"></a>\n\n| 组件', markdown)
        self.assertIn("MARKET_DETAIL_LINKS:START", html)
        self.assertIn("MARKET_DETAIL_LINKS:END", html)
        self.assertIn("MARKET_DETAIL_LINKS:START", markdown)
        self.assertIn("MARKET_DETAIL_LINKS:END", markdown)


if __name__ == "__main__":
    unittest.main()
