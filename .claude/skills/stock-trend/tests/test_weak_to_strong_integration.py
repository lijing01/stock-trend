#!/usr/bin/env python3
"""Integration checks for collecting V4 weak-to-strong inputs."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(TEST_DIR.parent / "scripts"))

from scans import stock_scanner as sc
from test_daily_weak_to_strong import payload_with_pattern, row, trading_days


class WeakToStrongIntegrationTests(unittest.TestCase):
    def _fixture(self):
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
        return payload, as_of, calendar

    def test_intraday_policy_marks_v4_input_close_evidence_provisional(self):
        payload, as_of, calendar = self._fixture()
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

        record = collected["600001.SH"]
        self.assertTrue(record["close_evidence"]["provisional"])
        self.assertEqual(record["kline_payload"], payload)
        self.assertEqual(record["code"], "600001")
        self.assertNotIn("result", record)
        self.assertFalse(hasattr(sc, "analyze_daily_weak_to_strong"))

    def test_wyckoff_filtered_stock_survives_as_v4_input_and_refreshes(self):
        payload, as_of, calendar = self._fixture()
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
        record = collected["600001.SH"]
        self.assertEqual(record["kline_payload"], payload)
        self.assertEqual(record["sector_memberships"], candidate["sector_memberships"])
        self.assertEqual(record["refresh_stage"], "initial")
        first_fingerprint = record["fingerprint"]

        refreshed = copy.deepcopy(payload)
        refreshed["data"][-1]["vol"] = 1
        with patch.object(sc, "_fetch_kline", return_value=refreshed), \
             patch.object(sc, "analyze_kline_dict", return_value={}), \
             patch.object(sc, "wyckoff_gate_pass", return_value=False):
            sc.run_phase2([candidate], max_workers=1, enable_wyckoff=True,
                          as_of_date=as_of, weak_to_strong_results=collected,
                          weak_to_strong_calendar=calendar, kline_budget_stage="topup")
        record = collected["600001.SH"]
        self.assertEqual(record["kline_payload"]["data"][-1]["vol"], 1)
        self.assertNotEqual(record["fingerprint"], first_fingerprint)
        self.assertEqual(record["refresh_stage"], "global")


def run_weak_to_strong_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WeakToStrongIntegrationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_weak_to_strong_tests()
    raise SystemExit(1 if failed else 0)
