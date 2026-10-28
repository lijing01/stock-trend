#!/usr/bin/env python3
"""Integration checks for the independent weak-to-strong scan and report."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from scans import daily_candidates as dc
from scans import stock_scanner as sc
from reporting import weak_to_strong as report
from core.weak_to_strong_snapshot import (
    load_latest_snapshot,
    load_previous_snapshot,
    save_snapshot,
)
from test_daily_weak_to_strong import payload_with_pattern, row, trading_days


class WeakToStrongIntegrationTests(unittest.TestCase):
    def test_incomplete_intraday_shape_is_visible_with_explicit_limits(self):
        candidate = {"code": "600001", "name": "测试股"}
        item = {"code": "600001", "name": "测试股", "state": "SUPPLY_DRY",
                "provisional": True, "confirmation": "provisional",
                "current_bar_evaluated": True, "evidence_status": "data_insufficient",
                "reason_codes": ["calendar_missing", "close_unconfirmed"]}
        collected = {"600001.SH": {"result": item}}
        output = dc.build_weak_to_strong_output(
            collected, [candidate], [], "2026-10-28", {}, {}, {})
        self.assertEqual(["600001"], [row["code"] for row in output["provisional"]])
        self.assertEqual(1, len(output["diagnostics"]))
        for rendered in (report.render_html(output), report.render_markdown(output)):
            self.assertIn("收盘未确认", rendered)
            self.assertIn("数据不足", rendered)
            self.assertIn("calendar_missing", rendered)
            self.assertIn("主表未展示", rendered)
        item["current_bar_evaluated"] = False
        output = dc.build_weak_to_strong_output(
            collected, [candidate], [], "2026-10-28", {}, {}, {})
        self.assertEqual([], output["provisional"])

    def test_intraday_policy_overrides_erroneous_provider_close_confirmation(self):
        payload, _ = payload_with_pattern()
        days = trading_days(100)
        prefix = [row(days[i], 10 + (0.03 if i % 2 else 0), volume=700)
                  for i in range(50)]
        for index, bar in enumerate(payload["data"]):
            bar["trade_date"] = days[50 + index].replace("-", "")
        payload["data"] = prefix + payload["data"]
        payload["meta"]["close_confirmed"] = True
        as_of = days[len(payload["data"]) - 1]
        calendar = {"trading_dates": days, "coverage_start": days[0],
                    "coverage_end": days[-1], "identity": "fixture"}
        candidate = {"code": "600001", "ts_code": "600001.SH",
                     "name": "测试股", "sector_memberships": []}
        collected = {}

        with patch.object(sc, "_fetch_kline", return_value=payload), \
             patch.object(sc, "_fetch_capital_flow", return_value=None), \
             patch.object(sc, "_fetch_fundamental", return_value=None):
            sc.run_phase2(
                [candidate], max_workers=1, as_of_date=as_of,
                weak_to_strong_results=collected,
                weak_to_strong_calendar=calendar,
                weak_to_strong_intraday=True,
                require_wyckoff_gate=False,
                defer_enrichment=True)

        result = collected["600001.SH"]["result"]
        self.assertEqual(result["state"], "PROVISIONAL_BREAKOUT")
        self.assertEqual(result["confirmation"], "provisional")
        self.assertTrue(result["provisional"])
        self.assertNotEqual(result["state"], "BREAKOUT_CONFIRMED")

    def test_intraday_states_are_evaluated_and_post_close_unconfirmed_is_diagnostic(self):
        candidates = [
            {"code": "600001", "name": "盘中缩量"},
            {"code": "600002", "name": "盘中回调"},
            {"code": "600003", "name": "盘后未确认"},
            {"code": "600004", "name": "真实缺数"},
        ]
        collected = {
            "600001.SH": {"result": {
                "code": "600001", "name": "盘中缩量", "state": "SUPPLY_DRY",
                "confirmation": "provisional", "provisional": True,
                "evidence_status": "ready", "reason_codes": ["close_unconfirmed"],
            }},
            "600002.SH": {"result": {
                "code": "600002", "name": "盘中回调", "state": "PULLBACK",
                "confirmation": "provisional", "provisional": True,
                "evidence_status": "ready", "reason_codes": ["close_unconfirmed"],
            }, "kline_payload": {"data": []}},
            "600003.SH": {"result": {
                "code": "600003", "name": "盘后未确认", "state": "SUPPLY_DRY",
                "confirmation": "none", "provisional": False,
                "evidence_status": "ready", "reason_codes": ["close_unconfirmed"],
            }},
            "600004.SH": {"result": {
                "code": "600004", "name": "真实缺数", "state": "WAIT",
                "confirmation": "none", "provisional": False,
                "evidence_status": "data_insufficient", "reason_codes": ["history_insufficient"],
            }},
        }

        output = dc.build_weak_to_strong_output(
            collected, candidates, [], "2026-10-28", {}, {},
            {"scope_complete": True}, calendar_dates=[])

        self.assertEqual(["600001"], [row["code"] for row in output["watching"]])
        self.assertEqual(["600002"], [row["code"] for row in output["pullback"]])
        self.assertEqual({"attempted": 4, "evaluated": 2, "data_insufficient": 1,
                          "close_unconfirmed": 1, "intraday_provisional": 2,
                          "confirmed": 0, "watching": 1, "provisional": 0},
                         output["summary"])
        self.assertIn("600003", [row["code"] for row in output["diagnostics"]])

    def test_pullback_observation_preserves_diagnostics_and_calendar_evidence(self):
        dates = ["2026-10-23", "2026-10-26", "2026-10-27"]
        candidate = {"code": "300005", "name": "测试股"}
        item = {"code": "300005", "name": "测试股", "state": "PULLBACK",
                "evidence_status": "ready", "drawdown_ratio": 0.08}
        rows = [{"trade_date": day.replace("-", ""), "close": close}
                for day, close in zip(dates, (10.3, 10.2, 9.95))]
        collected = {"300005.SZ": {"result": item, "kline_payload": {"data": rows}}}
        output = dc.build_weak_to_strong_output(collected, [candidate], [], dates[-1],
                                               {}, {}, {}, calendar_dates=dates)
        self.assertTrue(output["pullback"][0]["two_day_decline"])
        self.assertEqual(len(output["diagnostics"]), 1)
        self.assertEqual(output["summary"]["evaluated"], 1)
        rows.pop(1)
        output = dc.build_weak_to_strong_output(collected, [candidate], [], dates[-1],
                                               {}, {}, {}, calendar_dates=dates)
        self.assertIsNone(output["pullback"][0]["two_day_decline"])
        item["evidence_status"] = "data_insufficient"
        output = dc.build_weak_to_strong_output(collected, [candidate], [], dates[-1],
                                               {}, {}, {}, calendar_dates=dates)
        self.assertEqual(output["pullback"], [])
        self.assertEqual(output["summary"]["data_insufficient"], 1)

    def test_wyckoff_filtered_stock_survives_in_independent_report(self):
        payload, _ = payload_with_pattern()
        days = trading_days(100)
        prefix = [row(days[i], 10 + (0.03 if i % 2 else 0), volume=700)
                  for i in range(50)]
        for index, bar in enumerate(payload["data"]):
            bar["trade_date"] = days[50 + index].replace("-", "")
        payload["data"] = prefix + payload["data"]
        payload["meta"]["close_confirmed"] = True
        as_of = days[len(payload["data"]) - 1]
        calendar = {"trading_dates": days, "coverage_start": days[0],
                    "coverage_end": days[-1], "identity": "fixture"}
        candidate = {"code": "600001", "ts_code": "600001.SH", "name": "测试股",
                     "sector_memberships": [{"code": "BK01", "name": "测试板块"}]}
        collected = {}
        with patch.object(sc, "_fetch_kline", return_value=payload), \
             patch.object(sc, "analyze_kline_dict", return_value={}), \
             patch.object(sc, "wyckoff_gate_pass", return_value=False):
            formal = sc.run_phase2([candidate], max_workers=1, enable_wyckoff=True,
                                   as_of_date=as_of, weak_to_strong_results=collected,
                                   weak_to_strong_calendar=calendar)
        self.assertEqual(formal, [])
        self.assertEqual(collected["600001.SH"]["result"]["state"], "BREAKOUT_CONFIRMED")
        buckets = {key: [] for key in ("actionable", "waiting_trigger", "next_day_confirmation",
                                      "observation", "data_rejected", "unenriched_observation")}
        policy = {"mode": "observation", "reasons": ["regime_weak"]}
        output = dc.build_weak_to_strong_output(collected, [candidate], [], as_of,
                                                policy, buckets, {"scope_complete": True})
        self.assertEqual(len(output["confirmed"]), 1)
        self.assertEqual(output["confirmed"][0]["context"]["formal_bucket_or_not_selected"], "not_selected")
        self.assertIn("日线弱转强", report.render_markdown(output))
        self.assertIn("测试股", report.render_html(output))
        self.assertEqual(buckets["actionable"], [])
        report_policy = dc.build_recommendation_policy({"score": 30, "data_date": as_of}, as_of)
        json_output = dc.build_json_output([], [], 0, report_policy, buckets,
                                           weak_to_strong=output)
        markdown = dc.generate_report([], [], 0, report_policy, buckets, weak_to_strong=output)
        html = dc._generate_html([], [], 0, "20261027-120000", report_policy, buckets,
                                 weak_to_strong=output)
        self.assertEqual(json_output["weak_to_strong"]["confirmed"][0]["code"], "600001")
        self.assertIn("测试股", markdown)
        self.assertIn("测试股", html)

        refreshed = copy.deepcopy(payload)
        refreshed["data"][-1]["vol"] = 1
        with patch.object(sc, "_fetch_kline", return_value=refreshed), \
             patch.object(sc, "analyze_kline_dict", return_value={}), \
             patch.object(sc, "wyckoff_gate_pass", return_value=False):
            sc.run_phase2([candidate], max_workers=1, enable_wyckoff=True,
                          as_of_date=as_of, weak_to_strong_results=collected,
                          weak_to_strong_calendar=calendar, kline_budget_stage="topup")
        self.assertEqual(collected["600001.SH"]["result"]["state"], "INVALID")
        updated = dc.build_weak_to_strong_output(collected, [candidate], [], as_of,
                                                 policy, buckets, {"scope_complete": True})
        self.assertEqual(updated["confirmed"], [])

    def test_empty_scope_and_escape(self):
        payload = {"scope": {"scope_complete": True, "collector_status": "ready"},
                   "summary": {"evaluated": 0, "data_insufficient": 0},
                   "confirmed": [], "watching": [], "provisional": [], "diagnostics": []}
        self.assertIn("无满足条件", report.render_html(payload))
        payload["watching"] = [{"name": "<script>", "code": "600001",
                                "state": "SUPPLY_DRY", "evidence_status": "ready"}]
        html = report.render_html(payload)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_sector_display_shows_names_only(self):
        memberships = [
            {"code": "BK01", "name": "高热<板块>", "hot_score": 85,
             "history_window_days": 5, "history_coverage_days": 3,
             "hot_appearance_days": 2, "hot_streak": 1,
             "persistence_status": "verified",
             "ranking_source": "eastmoney", "ranking_data_date": "2026-10-27",
             "ranking_quality": "good", "membership_source": "eastmoney",
             "membership_data_date": "2026-10-27", "membership_quality": "good"},
            {"code": "BK02", "name": "温热板块", "hot_score": 65,
             "persistence_status": "single_day_pulse"},
            {"code": "BK03", "name": "热度缺失板块"},
        ]
        payload = {
            "scope": {"scope_complete": True, "collector_status": "ready"},
            "summary": {"evaluated": 1, "data_insufficient": 0},
            "confirmed": [],
            "watching": [{"name": "测试股", "code": "600001",
                          "state": "SUPPLY_DRY", "evidence_status": "ready",
                          "sector_memberships": memberships}],
            "provisional": [], "diagnostics": [],
        }

        html = report.render_html(payload)
        self.assertEqual(html.count("class='weak-sector'"), 3)
        self.assertIn("高热&lt;板块&gt;", html)
        self.assertNotIn("高热<板块>", html)
        self.assertNotIn("持续性：快照覆盖 3/5", html)
        self.assertNotIn("排行 来源 eastmoney", html)
        self.assertNotIn("成分 来源 eastmoney", html)
        self.assertIn("<span class='sector-tag sector-hot'>高热&lt;板块&gt;</span>", html)
        self.assertIn("<span class='sector-tag sector-warm'>温热板块</span>", html)
        self.assertIn("<div class='weak-sector'>热度缺失板块</div>", html)
        self.assertNotIn("sector-cool'>热度缺失板块", html)

        payload["watching"][0]["sector_memberships"] = []
        self.assertIn("<td>未记录</td>", report.render_html(payload))

    def test_snapshot_is_idempotent_and_distinct_inputs_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            payload = {"as_of_date": "2026-10-27", "scope": {}, "confirmed": []}
            first = save_snapshot(payload, {"600001": [1]}, {}, {}, root)
            second = save_snapshot(payload, {"600001": [1]}, {}, {}, root)
            third = save_snapshot(payload, {"600001": [2]}, {}, {}, root)
            self.assertEqual(first["status"], "saved")
            self.assertEqual(second["status"], "existing")
            self.assertEqual(first["path"], second["path"])
            self.assertNotEqual(first["path"], third["path"])
            provisional = save_snapshot(payload, {}, {}, {}, root, provisional=True)
            self.assertIn("provisional", provisional["path"])
            self.assertTrue(Path(first["path"]).exists())

    def test_snapshot_latest_run_index_and_previous_trading_date(self):
        with tempfile.TemporaryDirectory() as root:
            payload = {"as_of_date": "2026-10-26", "scope": {}, "confirmed": []}
            first = save_snapshot(
                payload, {"600001": [1]}, {}, {}, root,
                generated_at="2026-10-26T15:01:00+08:00")
            second = save_snapshot(
                payload, {"600001": [2]}, {}, {}, root,
                generated_at="2026-10-26T15:02:00+08:00")
            third = save_snapshot(
                payload, {"600001": [3]}, {}, {}, root,
                generated_at="2026-10-26T15:00:00+08:00")

            loaded = load_previous_snapshot(
                root, "2026-10-27", ["2026-10-24", "2026-10-26", "2026-10-27"])

            self.assertEqual(loaded["status"], "loaded")
            self.assertEqual(loaded["previous_trading_date"], "2026-10-26")
            self.assertEqual(loaded["snapshot"]["inputs"], {"600001": [2]})
            self.assertEqual(loaded["tracking"]["content_sha256"],
                             second["content_sha256"])
            self.assertEqual(loaded["tracking"]["selection_method"],
                             "run_index_generated_at_sequence")
            self.assertNotEqual(first["run_index_path"], second["run_index_path"])
            self.assertNotEqual(second["run_index_path"], third["run_index_path"])

            missing_current = load_previous_snapshot(
                root, "2026-10-27", ["2026-10-24", "2026-10-26"])
            self.assertEqual(missing_current["status"], "invalid")
            self.assertEqual(missing_current["reason"],
                             "as_of_date_not_in_trading_calendar")

    def test_snapshot_latest_corruption_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            payload = {"as_of_date": "2026-10-26", "scope": {}, "confirmed": []}
            save_snapshot(payload, {"600001": [1]}, {}, {}, root)
            latest = save_snapshot(payload, {"600001": [2]}, {}, {}, root)
            Path(latest["path"]).write_text("{}", encoding="utf-8")

            loaded = load_latest_snapshot(root, "2026-10-26")

            self.assertEqual(loaded["status"], "invalid")
            self.assertEqual(loaded["reason"], "snapshot_hash_mismatch")

    def test_snapshot_legacy_mtime_fallback_is_visible(self):
        with tempfile.TemporaryDirectory() as root:
            payload = {"as_of_date": "2026-10-26", "scope": {}, "confirmed": []}
            saved = save_snapshot(payload, {"600001": [1]}, {}, {}, root)
            Path(saved["run_index_path"]).unlink()

            loaded = load_latest_snapshot(root, "2026-10-26")

            self.assertEqual(loaded["status"], "loaded")
            self.assertEqual(loaded["tracking"]["selection_method"],
                             "legacy_mtime_fallback")

    def test_snapshot_invalid_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            payload = {"as_of_date": "2026-10-26", "scope": {}, "confirmed": []}
            saved = save_snapshot(payload, {"600001": [1]}, {}, {}, root)
            index_dir = Path(saved["run_index_path"]).parent
            (index_dir / "00000000000000000000-broken.json").write_text(
                "{}", encoding="utf-8")

            loaded = load_latest_snapshot(root, "2026-10-26")

            self.assertEqual(loaded["status"], "invalid")
            self.assertEqual(loaded["reason"], "run_index_invalid")


def run_weak_to_strong_tests():
    from test_daily_weak_to_strong import DailyWeakToStrongTests
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(DailyWeakToStrongTests),
                                unittest.defaultTestLoader.loadTestsFromTestCase(WeakToStrongIntegrationTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_weak_to_strong_tests()
    raise SystemExit(1 if failed else 0)
