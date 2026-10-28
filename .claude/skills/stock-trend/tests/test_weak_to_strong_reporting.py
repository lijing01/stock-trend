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

    def test_markdown_excludes_follow_up_only_rows_from_main_table(self):
        text = report.render_markdown(self.payload())
        self.assertIn("基线 2026-10-26；选择方式 run_index", text)
        self.assertNotIn("增强<股>", text)
        self.assertIn("数据不足或仅有历史跟踪 4", text)

    def test_html_reuses_sector_display_and_escapes_main_table_values(self):
        payload = self.payload()
        payload["confirmed"] = [{
            "name": "增强<股>", "code": "600001", "cycle_id": "cycle-a",
            "state": "BREAKOUT_CONFIRMED", "evidence_status": "ready",
            "sector_memberships": [{"name": "强|板块"}],
        }]
        payload["follow_up"]["items"][0]["origin_cycle_id"] = "cycle-a"
        text = report.render_html(payload)
        self.assertEqual(1, text.count("<table"))
        self.assertIn("增强&lt;股&gt;", text)
        self.assertNotIn("增强<股>", text)
        self.assertIn("<div class='weak-sector'>强|板块</div>", text)
        self.assertNotIn("kline_missing", text)

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
            "confirmed": [{"name": "确认", "code": "4", "state": "BREAKOUT_CONFIRMED",
                           "evidence_status": "ready"}],
            "watching": [{"name": "待突破", "code": "3", "state": "SUPPLY_DRY",
                          "evidence_status": "ready"}],
            "pullback": [{"name": "回调", "code": "2", "state": "PULLBACK",
                          "two_day_decline": True, "evidence_status": "ready"}],
            "provisional": [{"name": "盘中", "code": "1", "state": "PROVISIONAL_BREAKOUT",
                             "evidence_status": "ready"}],
            "diagnostics": [],
        }
        for text in (report.render_markdown(payload), report.render_html(payload)):
            self.assertLess(text.index("确认 (4)"), text.index("待突破 (3)"))
            self.assertLess(text.index("待突破 (3)"), text.index("回调 (2)"))
            self.assertNotIn("盘中 (1)", text)
            self.assertIn("结构有效回调（观察，尚未缩量）", text)
            self.assertIn("连续两日收盘下跌", text)
            self.assertIn("同阶段按均线方向、风险和形态质量排列", text)

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
            self.assertNotIn("缩量待突破（盘中临时，未收盘）", text)
            self.assertIn("盘中临时 1", text)

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

    def test_merged_rows_match_cycle_and_preserve_distinct_cycles(self):
        payload = self.payload()
        payload["confirmed"] = [{"name": "同股", "code": "600001", "cycle_id": "A",
                                 "state": "BREAKOUT_CONFIRMED", "evidence_status": "ready"}]
        payload["follow_up"]["items"] = [
            {"name": "同股", "code": "600001", "origin_cycle_id": "A",
             "follow_up_status": "ENHANCED", "tracking_count": 2},
            {"name": "旧周期", "code": "600001", "origin_cycle_id": "B",
             "follow_up_status": "INVALIDATED"},
        ]
        for text in (report.render_markdown(payload), report.render_html(payload)):
            self.assertEqual(1, text.count("同股 (600001)"))
            self.assertIn("跟踪 2 次", text)
            self.assertNotIn("跟踪次数未知", text)
            self.assertNotIn("周期 B", text)
            self.assertNotIn("旧周期", text)

    def test_main_table_filters_deduplicates_and_caps_total_at_thirty(self):
        payload = {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 40, "data_insufficient": 1},
            "confirmed": [
                {"name": f"确认{index}", "code": f"{index:06d}",
                 "cycle_id": f"cycle-{index}", "state": "BREAKOUT_CONFIRMED",
                 "evidence_status": "ready", "breakout_volume_ratio": index,
                 "close_strength": .8, "breakout_ratio": 1.01}
                for index in range(1, 36)
            ],
            "watching": [
                {"name": "低阶段重复", "code": "000035", "cycle_id": "duplicate",
                 "state": "SUPPLY_DRY", "evidence_status": "ready", "va_v0_ratio": .4},
                {"name": "数据不足", "code": "800001", "cycle_id": "insufficient",
                 "state": "SUPPLY_DRY", "evidence_status": "data_insufficient"},
                {"name": "盘中临时", "code": "800002", "cycle_id": "provisional",
                 "state": "SUPPLY_DRY", "evidence_status": "ready", "provisional": True},
                {"name": "待复评", "code": "800003", "cycle_id": "pending",
                 "state": "SUPPLY_DRY", "evidence_status": "ready"},
                {"name": "失效", "code": "800004", "cycle_id": "invalid",
                 "state": "SUPPLY_DRY", "evidence_status": "ready"},
            ],
            "pullback": [], "provisional": [], "diagnostics": [],
            "follow_up": {"items": [
                {"name": "待复评", "code": "800003", "origin_cycle_id": "pending",
                 "follow_up_status": "PENDING_REVIEW", "evidence_status": "ready"},
                {"name": "失效", "code": "800004", "origin_cycle_id": "invalid",
                 "follow_up_status": "INVALIDATED", "evidence_status": "ready"},
                {"name": "仅跟踪", "code": "800005", "origin_cycle_id": "old",
                 "follow_up_status": "CONTINUED", "evidence_status": "ready"},
            ]},
        }

        shown, excluded = report.select_v1_rows(payload)

        self.assertEqual(30, len(shown))
        shown_codes = [item["code"] for item, _follow in shown]
        self.assertEqual(30, len(set(shown_codes)))
        self.assertEqual("000035", shown_codes[0])
        self.assertNotIn("000005", shown_codes)
        self.assertFalse({"800001", "800002", "800003", "800004", "800005"}
                         & set(shown_codes))
        self.assertGreaterEqual(sum(excluded.values()), 10)
        self.assertEqual(35, len(payload["confirmed"]), "selection must not truncate raw evidence")

        for rendered in (report.render_markdown(payload), report.render_html(payload)):
            self.assertIn("确认35 (000035)", rendered)
            self.assertNotIn("确认5 (000005)", rendered)
            self.assertNotIn("待复评 (800003)", rendered)
            self.assertIn("展示 30", rendered)

    def test_main_table_keeps_fewer_than_thirty_without_top_up(self):
        payload = {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 2, "data_insufficient": 1},
            "confirmed": [{"name": "有效", "code": "1", "cycle_id": "a",
                           "state": "BREAKOUT_CONFIRMED", "evidence_status": "ready"}],
            "watching": [{"name": "不足", "code": "2", "cycle_id": "b",
                          "state": "SUPPLY_DRY", "evidence_status": "data_insufficient"}],
            "pullback": [], "provisional": [], "diagnostics": [],
        }

        shown, _excluded = report.select_v1_rows(payload)

        self.assertEqual(["1"], [item["code"] for item, _follow in shown])
        self.assertIn("展示 1", report.render_markdown(payload))

    def test_main_table_describes_and_orders_v1_ma_direction(self):
        def item(code, metrics):
            return {
                "name": code, "code": code, "cycle_id": code,
                "state": "SUPPLY_DRY", "evidence_status": "ready",
                "metrics": metrics, "va_v0_ratio": .5,
                "drawdown_ratio": .04, "adjustment_days": 2,
            }

        payload = {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 4, "data_insufficient": 0},
            "confirmed": [],
            "watching": [
                item("1", {}),
                item("2", {"ma10_slope_3": .002,
                            "ma20_slope_3": .002,
                            "ma_flat_tolerance": .002}),
                item("3", {"ma10_slope_3": -.01,
                            "ma20_slope_3": .003,
                            "ma_flat_tolerance": .002}),
                item("4", {"ma10_slope_3": .003,
                            "ma20_slope_3": .004,
                            "ma_flat_tolerance": .002}),
            ],
            "pullback": [], "provisional": [], "diagnostics": [],
        }

        shown, _stats = report.select_v1_rows(payload)

        self.assertEqual(["4", "3", "2", "1"],
                         [row["code"] for row, _follow in shown])
        for rendered in (report.render_markdown(payload), report.render_html(payload)):
            self.assertIn("MA10/MA20向上", rendered)
            self.assertIn("MA20向上", rendered)
            self.assertIn("均线未形成上行", rendered)
            self.assertIn("均线方向未记录", rendered)

    def test_legacy_ma_direction_field_remains_compatible(self):
        both_up = {"ma_direction": "both_up"}
        ma20_up = {"metrics": {"ma_direction": "ma20_up"}}
        unknown = {}

        self.assertEqual(0, report._v1_trend_tier(both_up))
        self.assertEqual(1, report._v1_trend_tier(ma20_up))
        self.assertEqual(3, report._v1_trend_tier(unknown))

    def test_main_table_excludes_rows_from_an_older_data_date(self):
        payload = {
            "as_of_date": "2026-10-28",
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 2, "data_insufficient": 0},
            "confirmed": [
                {"name": "当日", "code": "1", "cycle_id": "a",
                 "state": "BREAKOUT_CONFIRMED", "evidence_status": "ready",
                 "as_of_date": "2026-10-28"},
                {"name": "旧日", "code": "2", "cycle_id": "b",
                 "state": "BREAKOUT_CONFIRMED", "evidence_status": "ready",
                 "as_of_date": "2026-10-27"},
            ],
            "watching": [], "pullback": [], "provisional": [], "diagnostics": [],
        }

        shown, excluded = report.select_v1_rows(payload)

        self.assertEqual(["1"], [item["code"] for item, _follow in shown])
        self.assertEqual(1, excluded["excluded_stale_date"])
        self.assertNotIn("旧日 (2)", report.render_markdown(payload))


if __name__ == "__main__":
    unittest.main()
