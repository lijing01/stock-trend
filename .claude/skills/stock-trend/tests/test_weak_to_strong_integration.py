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
from core.weak_to_strong_snapshot import save_snapshot
from test_daily_weak_to_strong import payload_with_pattern, row, trading_days


class WeakToStrongIntegrationTests(unittest.TestCase):
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
        payload["watching"] = [{"name": "<script>", "code": "600001", "state": "SUPPLY_DRY"}]
        html = report.render_html(payload)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_sector_display_reuses_observation_pool_rendering(self):
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
                          "state": "SUPPLY_DRY", "sector_memberships": memberships}],
            "provisional": [], "diagnostics": [],
        }

        html = report.render_html(payload)
        for membership in memberships[:2]:
            observation_item = {
                "sector_name": membership["name"],
                "sector_hot_score": membership["hot_score"],
                **{key: value for key, value in membership.items()
                   if key not in {"code", "name", "hot_score"}},
            }
            self.assertIn(dc._sector_html(observation_item), html)
        self.assertEqual(html.count("class='weak-sector'"), 3)
        self.assertIn("sector-tag sector-hot", html)
        self.assertIn("sector-tag sector-warm", html)
        self.assertIn("高热&lt;板块&gt;", html)
        self.assertNotIn("高热<板块>", html)
        self.assertIn("持续性：快照覆盖 3/5", html)
        self.assertIn("排行 来源 eastmoney", html)
        self.assertIn("成分 来源 eastmoney", html)
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
