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
