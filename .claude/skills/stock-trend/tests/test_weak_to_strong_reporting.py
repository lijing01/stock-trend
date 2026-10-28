#!/usr/bin/env python3
"""Focused report checks for weak-to-strong follow-up rows."""

import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from reporting import weak_to_strong as report


class WeakToStrongFollowUpReportingTests(unittest.TestCase):
    def payload(self):
        return {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 0, "data_insufficient": 0},
            "confirmed": [], "watching": [], "provisional": [], "diagnostics": [],
            "follow_up": {
                "status": "ready",
                "baseline": {"as_of_date": "2026-10-26", "selection_method": "run_index"},
                "items": [
                    {"name": "增强<股>", "code": "600001", "origin_signal_date": "2026-10-26",
                     "previous_status": "BREAKOUT_CONFIRMED", "follow_up_status": "STRENGTHENED",
                     "reason_codes": ["close_above_previous"], "as_of_date": "2026-10-27",
                     "current_close": 11.2, "previous_close": 10.8, "threshold_H": 10.5,
                     "evidence_status": "ready", "sector_memberships": [{"name": "强|板块"}]},
                    {"name": "延续股", "code": "600002", "follow_up_status": "CONTINUED"},
                    {"name": "失效股", "code": "600003", "follow_up_status": "INVALIDATED"},
                    {"name": "待复评股", "code": "600004", "follow_up_status": "PENDING_REVIEW",
                     "reason_codes": ["kline_missing"]},
                ],
            },
        }

    def test_markdown_renders_follow_up_table_and_escapes_pipes(self):
        text = report.render_markdown(self.payload())
        self.assertIn("### 上一交易日信号跟踪", text)
        self.assertIn("基线 2026-10-26；选择方式 run_index", text)
        for label in ("增强", "延续", "失效", "待复评"):
            self.assertIn(label, text)
        self.assertIn("强\\|板块", text)
        self.assertIn("收盘 11.2；前收 10.8；压力 H 10.5", text)

    def test_html_reuses_sector_display_and_escapes_values(self):
        text = report.render_html(self.payload())
        self.assertIn("weak-follow-up-table", text)
        self.assertIn("增强&lt;股&gt;", text)
        self.assertNotIn("增强<股>", text)
        self.assertIn("<div class='weak-sector'>强|板块</div>", text)
        self.assertIn("kline_missing", text)

    def test_empty_and_malformed_rows_are_tolerated(self):
        payload = self.payload()
        payload["follow_up"] = {"status": "missing", "reason": "上一交易日快照缺失",
                                "rows": "invalid"}
        self.assertIn("上一交易日快照缺失", report.render_markdown(payload))
        self.assertIn("上一交易日快照缺失", report.render_html(payload))

    def test_sort_payload_orders_signal_groups_and_missing_values_last(self):
        payload = {
            "confirmed": [
                {"code": "3", "breakout_volume_ratio": None, "close_strength": 1},
                {"code": "2", "breakout_volume_ratio": 1.5, "close_strength": .8,
                 "breakout_ratio": 1.04},
                {"code": "1", "breakout_volume_ratio": 1.5, "close_strength": .8,
                 "breakout_ratio": 1.02},
            ],
            "watching": [
                {"code": "2", "va_v0_ratio": math.nan, "drawdown_ratio": .01},
                {"code": "3", "va_v0_ratio": .5, "drawdown_ratio": .08,
                 "adjustment_days": 2},
                {"code": "1", "va_v0_ratio": .5, "drawdown_ratio": .04,
                 "adjustment_days": 3},
            ],
            "pullback": [
                {"code": "3", "two_day_decline": False, "drawdown_ratio": .01},
                {"code": "2", "two_day_decline": True, "drawdown_ratio": .08},
                {"code": "1", "two_day_decline": True, "drawdown_ratio": .04},
            ],
            "provisional": [
                {"code": "2", "breakout_volume_ratio": "bad", "close_strength": .9},
                {"code": "1", "breakout_volume_ratio": 1.3, "close_strength": .7},
            ],
        }

        returned = report.sort_payload(payload)

        self.assertIs(returned, payload)
        self.assertEqual(["1", "2", "3"], [row["code"] for row in payload["confirmed"]])
        self.assertEqual(["1", "3", "2"], [row["code"] for row in payload["watching"]])
        self.assertEqual(["1", "2", "3"], [row["code"] for row in payload["pullback"]])
        self.assertEqual(["1", "2"], [row["code"] for row in payload["provisional"]])

    def test_render_uses_stage_order_and_marks_pullback_as_observation(self):
        payload = {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 4, "data_insufficient": 0},
            "confirmed": [{"name": "确认", "code": "4", "state": "BREAKOUT_CONFIRMED"}],
            "watching": [{"name": "待突破", "code": "3", "state": "SUPPLY_DRY"}],
            "pullback": [{"name": "回调", "code": "2", "state": "PULLBACK",
                          "two_day_decline": True}],
            "provisional": [{"name": "盘中", "code": "1", "state": "PROVISIONAL_BREAKOUT"}],
            "diagnostics": [],
        }
        for text in (report.render_markdown(payload), report.render_html(payload)):
            self.assertLess(text.index("确认 (4)"), text.index("待突破 (3)"))
            self.assertLess(text.index("待突破 (3)"), text.index("回调 (2)"))
            self.assertLess(text.index("回调 (2)"), text.index("盘中 (1)"))
            self.assertIn("结构有效回调（观察，尚未缩量）", text)
            self.assertIn("连续两日收盘下跌", text)
            self.assertIn("各阶段按形态质量排列", text)

    def test_summary_and_rows_distinguish_intraday_from_missing_data(self):
        payload = {
            "scope": {"scope_complete": False, "collector_status": "partial"},
            "summary": {"evaluated": 2, "data_insufficient": 1,
                        "intraday_provisional": 2, "close_unconfirmed": 1},
            "confirmed": [],
            "watching": [{
                "name": "盘中缩量", "code": "1", "state": "SUPPLY_DRY",
                "confirmation": "provisional", "provisional": True,
                "evidence_status": "ready", "as_of_date": "2026-10-28",
                "kline_evidence": {"provider": "eastmoney",
                                   "fetched_at": "2026-10-28T13:20:00+08:00"},
            }],
            "pullback": [], "provisional": [], "diagnostics": [],
        }
        for text in (report.render_markdown(payload), report.render_html(payload)):
            self.assertIn("已评估 2 只，数据不足 1 只", text)
            self.assertIn("盘中临时 2 只", text)
            self.assertIn("收盘未确认 1 只", text)
            self.assertIn("缩量待突破（盘中临时）", text)
            self.assertIn("来源 eastmoney", text)
            self.assertIn("获取 2026-10-28T13:20:00+08:00", text)

    def test_follow_up_priority_then_recent_signal_date_then_code(self):
        payload = self.payload()
        payload["follow_up"]["items"] = [
            {"code": "4", "follow_up_status": "CONTINUED", "origin_signal_date": "2026-10-27"},
            {"code": "3", "follow_up_status": "STRENGTHENED", "origin_signal_date": "2026-10-25"},
            {"code": "2", "follow_up_status": "PENDING_REVIEW", "origin_signal_date": "2026-10-26"},
            {"code": "9", "follow_up_status": "INVALIDATED", "origin_signal_date": "2026-10-24"},
            {"code": "1", "follow_up_status": "INVALIDATED", "origin_signal_date": "2026-10-27"},
        ]

        self.assertEqual(["1", "9", "2", "3", "4"],
                         [row["code"] for row in report._follow_up_rows(payload)])
        report.sort_payload(payload)
        self.assertEqual(["1", "9", "2", "3", "4"],
                         [row["code"] for row in payload["follow_up"]["items"]])


if __name__ == "__main__":
    unittest.main()
