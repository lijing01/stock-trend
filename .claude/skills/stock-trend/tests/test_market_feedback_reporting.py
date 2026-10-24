"""Shared daily-review feedback display and optional collection boundaries."""
import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analysis import market_regime as market
from reporting.market_feedback import render_market_feedback


class FeedbackReportingTests(unittest.TestCase):
    def test_all_members_including_loser_and_missing_in_both_formats(self):
        state = {
            "basis_date": "2026-10-20", "cohort_date": "2026-10-19", "mode": "intraday",
            "as_of": "2026-10-20T13:37:00+08:00",
            "summary": {"total": 3, "available": 2, "coverage": 2/3,
                        "up": 1, "down": 1, "flat": 0, "missing": 1,
                        "mean_pct_chg": -1, "median_pct_chg": -1,
                        "worst_pct_chg": -5, "positive_ratio": .5, "negative_ratio": .5},
            "items": [{"code": "600000.SH", "name": "下跌股", "roles": ["limit_up", "height_core"],
                       "streak": 3, "pct_chg": -5, "status": "down"},
                      {"code": "000001.SZ", "name": "上涨股", "roles": ["limit_up"],
                       "streak": 1, "pct_chg": 3, "status": "up"},
                      {"code": "000002.SZ", "name": "缺价股", "roles": ["limit_up"],
                       "streak": 1, "pct_chg": None, "status": "missing"}],
        }
        for fmt in ("html", "markdown"):
            text = render_market_feedback(state, fmt)
            for expected in ("下跌股", "上涨股", "缺价股", "-5.00%", "66.67%", "行情缺失", "2026-10-19", "13:37", "连板高度核心"):
                self.assertIn(expected, text)
            self.assertIn("quote.eastmoney.com/sh600000.html", text)

    def test_unavailable_is_not_zero_and_html_is_escaped(self):
        for fmt in ("html", "markdown"):
            text = render_market_feedback({"reason": "缺少昨日冻结名单 <script>"}, fmt)
            self.assertIn("缺少昨日冻结名单", text)
            self.assertNotIn("<script>", text)
            self.assertNotIn("冻结 0 只", text)

    def test_rule_mismatch_reason_is_readable_in_both_formats(self):
        state = {"comparison": {"status": "unavailable", "reason": "feedback_rule_mismatch"}}
        for fmt in ("html", "markdown"):
            text = render_market_feedback(state, fmt)
            self.assertIn("两次反馈规则版本不同", text)
            self.assertNotIn("feedback_rule_mismatch", text)

    def test_v2_dates_all_missing_and_line_diagnostics_are_explicit(self):
        state = {
            "schema_version": "market-feedback/v2",
            "market_basis_date": "2026-10-22",
            "feedback_basis_date": "2026-10-23",
            "basis_date": "2026-10-23",
            "cohort_date": "2026-10-22",
            "mode": "close",
            "as_of": "2026-10-23T15:48:00+08:00",
            "reasons": ["market_context_stale"],
            "summary": {"total": 2, "available": 0, "coverage": 0,
                        "up": 0, "down": 0, "flat": 0, "suspended": 0,
                        "missing": 2, "mean_pct_chg": None,
                        "median_pct_chg": None, "positive_ratio": None,
                        "negative_ratio": None, "worst_pct_chg": None},
            "items": [
                {"code": "600001.SH", "name": "接口失败", "roles": ["limit_up"],
                 "streak": 1, "status": "missing", "reason": "provider_error",
                 "provider_attempts": [{"provider": "eastmoney", "status": "error",
                                        "reason": "provider_error",
                                        "latest_data_date": "2026-10-22"}]},
                {"code": "600002.SH", "name": "旧记录", "roles": ["limit_up"],
                 "streak": 1, "status": "missing"},
            ],
        }
        for fmt in ("html", "markdown"):
            text = render_market_feedback(state, fmt)
            for expected in (
                    "市场评分依据日：2026-10-22", "反馈依据日：2026-10-23",
                    "市场评分证据滞后；反馈单独采集", "全部样本行情缺失",
                    "提供方请求失败", "eastmoney", "最新交易日 2026-10-22",
                    "旧记录未保存详细原因"):
                self.assertIn(expected, text)

    def test_recovered_provider_failure_is_advisory_and_source_is_visible(self):
        state = {
            "schema_version": "market-feedback/v2", "basis_date": "2026-10-23",
            "feedback_basis_date": "2026-10-23", "market_basis_date": "2026-10-23",
            "cohort_date": "2026-10-22", "mode": "close",
            "as_of": "2026-10-23T15:48:00+08:00",
            "diagnostics": {"advisories": ["primary_provider_failed_but_recovered"]},
            "summary": {"total": 1, "available": 1, "coverage": 1,
                        "up": 1, "down": 0, "flat": 0, "suspended": 0,
                        "missing": 0, "mean_pct_chg": 2, "median_pct_chg": 2,
                        "positive_ratio": 1, "negative_ratio": 0,
                        "worst_pct_chg": 2},
            "items": [{"code": "600001.SH", "name": "已恢复", "roles": ["limit_up"],
                       "streak": 1, "pct_chg": 2, "status": "up",
                       "quote_source": "verified_native_backup",
                       "quote_fetched_at": "2026-10-23T15:47:59+08:00",
                       "source_time_raw": "2026-10-23T15:00:00+08:00",
                       "provider_attempts": [
                           {"provider": "eastmoney", "status": "error",
                            "reason": "provider_error"},
                           {"provider": "verified_native_backup", "status": "success"}],
                       }],
        }
        for fmt in ("html", "markdown"):
            text = render_market_feedback(state, fmt)
            visible = text.replace("\\_", "_")
            self.assertIn("已恢复", visible)
            self.assertIn("最终来源 verified_native_backup", visible)
            self.assertIn("来源时刻 2026-10-23T15:00:00+08:00", visible)
            self.assertIn("主行情源失败后已由合格备用源恢复", visible)
            self.assertNotIn("部分昨日样本行情采集失败，仍保留为缺失", visible)

    def test_diagnostics_are_escaped_in_both_formats(self):
        state = {
            "schema_version": "market-feedback/v2", "basis_date": "2026-10-23",
            "summary": {"total": 1, "available": 0, "coverage": 0,
                        "up": 0, "down": 0, "flat": 0, "suspended": 0,
                        "missing": 1},
            "items": [{"code": "600001.SH", "name": "异常", "status": "missing",
                       "reason": "bad <script>",
                       "provider_attempts": [{"provider": "evil|source",
                                              "reason": "bad <img>"}]}],
        }
        for fmt in ("html", "markdown"):
            text = render_market_feedback(state, fmt)
            self.assertNotIn("<script>", text)
            self.assertNotIn("<img>", text)

    def test_optional_collection_failure_preserves_context(self):
        ctx = {"data_date": "2026-10-20", "regime": {"score": 60}}
        original = copy.deepcopy(ctx)
        with patch.object(market, "_review_calendar", side_effect=OSError("fixture")):
            result = market.collect_market_feedback(ctx, as_of=datetime.fromisoformat("2026-10-20T16:00:00+08:00"),
                                                    refresh_mode="live_refresh")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(ctx, original)

    def test_existing_report_and_json_share_feedback(self):
        ctx = {"data_date": "2026-10-20", "generated_at": "2026-10-20 16:00:00",
               "regime": {"score": 60}, "components": {}, "amount_yi": None,
               "zt": {}, "top_sectors": [], "bottom_sectors": [],
               "market_feedback": {"reason": "首次启用：昨日名单未冻结", "items": []}}
        state = {"status": "unavailable", "items": []}
        for text in (market.generate_report(ctx, observation_state=state),
                     market._generate_html(ctx, "fixture", observation_state=state)):
            self.assertIn("昨日强势股今日反馈", text)
            self.assertIn("首次启用", text)
        self.assertEqual(market.build_agent_output(ctx)["market_feedback"], ctx["market_feedback"])


if __name__ == "__main__":
    unittest.main()
