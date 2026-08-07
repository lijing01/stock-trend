#!/usr/bin/env python3
"""Regression tests for capital-flow fallback and cache validation."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import capital_flow


VALID_FLOW = {"date": "20260807", "main_net_inflow": 1.0}


class TestCapitalFlowFallback(unittest.TestCase):
    def _fetch(self, eastmoney, tushare, estimate):
        with patch.object(capital_flow, "fetch_stock_capital_flow", return_value=eastmoney) as em, \
                patch.object(capital_flow, "fetch_stock_capital_flow_tushare", return_value=tushare) as ts, \
                patch.object(capital_flow, "estimate_capital_flow_from_kline", return_value=estimate) as kl:
            result = capital_flow.fetch_stock_capital_flow_with_fallbacks(
                "600519.SH", "1.600519", "600519"
            )
        return result, em, ts, kl

    def test_nonempty_eastmoney_result_skips_fallbacks(self):
        result, _, ts, kl = self._fetch([VALID_FLOW], None, None)
        self.assertEqual(result["meta"]["data_source"], "eastmoney")
        self.assertEqual(result["data"], [VALID_FLOW])
        ts.assert_not_called()
        kl.assert_not_called()

    def test_empty_eastmoney_uses_tushare(self):
        result, _, _, kl = self._fetch([], [VALID_FLOW], None)
        self.assertEqual(result["meta"]["data_source"], "tushare_fallback")
        kl.assert_not_called()

    def test_empty_eastmoney_and_tushare_use_kline_estimate(self):
        result, _, _, _ = self._fetch([], [], [VALID_FLOW])
        self.assertEqual(result["meta"]["data_source"], "kline_estimate")

    def test_all_empty_sources_return_error(self):
        result, _, _, _ = self._fetch([], [], [])
        self.assertEqual(result["meta"]["data_source"], "error")
        self.assertEqual(result["data"], [])

    def test_rows_without_valid_dates_fall_back(self):
        invalid = [{"date": "not-a-date", "main_net_inflow": 1.0}]
        result, _, _, kl = self._fetch(invalid, [VALID_FLOW], None)
        self.assertEqual(result["meta"]["data_source"], "tushare_fallback")
        kl.assert_not_called()


class TestCapitalFlowCacheValidation(unittest.TestCase):
    def test_empty_success_cache_is_invalid(self):
        cached = {"meta": {"data_source": "eastmoney"}, "data": []}
        self.assertFalse(capital_flow.is_valid_capital_result(cached))

    def test_nonempty_dated_cache_is_valid(self):
        cached = {"meta": {"data_source": "eastmoney"}, "data": [VALID_FLOW]}
        self.assertTrue(capital_flow.is_valid_capital_result(cached))

    def test_error_result_is_invalid(self):
        cached = {"meta": {"data_source": "error"}, "data": [VALID_FLOW]}
        self.assertFalse(capital_flow.is_valid_capital_result(cached))

    def _run_main(self, cached, fetched=None):
        outputs = []
        fetched = fetched or {
            "meta": {"data_source": "eastmoney", "record_count": 1},
            "data": [VALID_FLOW],
        }
        with patch.object(sys, "argv", ["capital_flow.py", "600519.SH"]), \
                patch.object(capital_flow, "load_cache", return_value=cached), \
                patch.object(capital_flow, "output_json", side_effect=lambda value, **_: outputs.append(value)), \
                patch.object(capital_flow, "resolve_secid", return_value="1.600519"), \
                patch.object(capital_flow, "fetch_stock_capital_flow_with_fallbacks", return_value=fetched) as fetch, \
                patch.object(capital_flow, "fetch_northbound_flow", return_value=None), \
                patch.object(capital_flow, "fetch_individual_northbound", return_value=None), \
                patch.object(capital_flow, "fetch_margin_detail", return_value=None), \
                patch.object(capital_flow, "fetch_longhubang", return_value=None), \
                patch.object(capital_flow, "save_cache") as save:
            capital_flow.main()
        return outputs[0], fetch, save

    def test_empty_success_cache_is_refetched(self):
        cached = {"meta": {"data_source": "eastmoney"}, "data": []}
        result, fetch, save = self._run_main(cached)
        fetch.assert_called_once()
        self.assertEqual(result["data"], [VALID_FLOW])
        save.assert_called_once()

    def test_valid_cache_is_reused(self):
        cached = {"meta": {"data_source": "eastmoney"}, "data": [VALID_FLOW]}
        result, fetch, save = self._run_main(cached)
        self.assertIs(result, cached)
        fetch.assert_not_called()
        save.assert_not_called()

    def test_error_result_is_not_cached(self):
        error = {"meta": {"data_source": "error"}, "data": []}
        result, _, save = self._run_main(None, fetched=error)
        self.assertEqual(result["meta"]["data_source"], "error")
        save.assert_not_called()


def run_capital_flow_tests():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestCapitalFlowFallback))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestCapitalFlowCacheValidation))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_capital_flow_tests()
    raise SystemExit(1 if failed else 0)
