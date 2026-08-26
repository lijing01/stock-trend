#!/usr/bin/env python3
"""Regression tests for market-data freshness and capital-flow fallback."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import capital_flow, kline, kline_eastmoney
from pipeline import runner


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


class TestKlineExpectedDateValidation(unittest.TestCase):
    @staticmethod
    def _tushare_frame(trade_date):
        import pandas as pd

        return pd.DataFrame([{
            "trade_date": trade_date,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 10.0,
            "vol": 1000.0,
            "amount": 10000.0,
            "pct_chg": 2.0,
        }])

    def test_tushare_rejects_and_does_not_cache_stale_fresh_payload(self):
        outputs = []
        stale_frame = self._tushare_frame("20260825")
        with patch.object(sys, "argv", [
                "kline.py", "600519.SH", "--expected-date", "2026-08-26",
            ]), \
                patch.object(kline, "load_cache", return_value=None), \
                patch.object(kline, "resolve_token", return_value="token"), \
                patch.object(kline, "fetch_kline", return_value=(stale_frame, "tushare_sdk")), \
                patch.object(kline, "output_json", side_effect=lambda value, **_: outputs.append(value)), \
                patch.object(kline, "save_cache") as save:
            kline.main()

        result = outputs[0]
        self.assertEqual(result["meta"]["data_source"], "error")
        self.assertEqual(result["meta"]["error_type"], "stale_data")
        self.assertEqual(result["meta"]["cache_validation"], {
            "expected_date": "2026-08-26",
            "latest_date": "2026-08-25",
            "valid": False,
        })
        self.assertEqual(result["data"], [])
        save.assert_not_called()

    def test_tushare_cli_bypasses_ttl_valid_stale_cache_then_rejects_stale_refetch(self):
        outputs = []
        stale_payload = {
            "meta": {"data_source": "tushare_sdk", "record_count": 1},
            "data": [{"trade_date": "20260825", "close": 10.0}],
        }
        stale_frame = self._tushare_frame("20260825")
        with patch.object(sys, "argv", [
                "kline.py", "600519.SH", "--expected-date", "2026-08-26",
            ]), \
                patch.object(kline, "load_cache", return_value=stale_payload) as load, \
                patch.object(kline, "resolve_token", return_value="token"), \
                patch.object(kline, "fetch_kline",
                             return_value=(stale_frame, "tushare_sdk")) as fetch, \
                patch.object(kline, "output_json",
                             side_effect=lambda value, **_: outputs.append(value)), \
                patch.object(kline, "save_cache") as save:
            kline.main()

        load.assert_called_once()
        fetch.assert_called_once()
        self.assertEqual(outputs[0]["meta"]["error_type"], "stale_data")
        self.assertFalse(outputs[0]["meta"]["cache_validation"]["valid"])
        save.assert_not_called()

    def test_eastmoney_rejects_and_does_not_cache_stale_fresh_payload(self):
        outputs = []
        stale_records = [{
            "trade_date": "20260825",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "vol": 1000.0,
            "amount": 10000.0,
        }]
        with patch.object(sys, "argv", [
                "kline_eastmoney.py", "600519.SH",
                "--expected-date", "2026-08-26",
            ]), \
                patch.object(kline_eastmoney, "load_cache", return_value=None), \
                patch.object(kline_eastmoney, "build_secid", return_value="1.600519"), \
                patch("core.eastmoney_utils.rotate_em_host",
                      return_value=((stale_records, "贵州茅台"), "push2his.eastmoney.com")), \
                patch.object(kline_eastmoney, "output_json",
                             side_effect=lambda value, **_: outputs.append(value)), \
                patch.object(kline_eastmoney, "save_cache") as save:
            kline_eastmoney.main()

        result = outputs[0]
        self.assertEqual(result["meta"]["data_source"], "error")
        self.assertEqual(result["meta"]["error_type"], "stale_data")
        self.assertEqual(result["meta"]["cache_validation"], {
            "expected_date": "2026-08-26",
            "latest_date": "2026-08-25",
            "valid": False,
        })
        self.assertEqual(result["data"], [])
        save.assert_not_called()


class TestPipelineExpectedDatePropagation(unittest.TestCase):
    @staticmethod
    def _write_json(path, payload):
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def _run_pipeline(self, freq, expected_date="2026-08-26", calendar_result=None):
        commands = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_run_script(cmd, label="", timeout=30):
                commands[label] = list(cmd)
                if label == "fetch_kline_tushare":
                    output = cmd[cmd.index("-o") + 1]
                    self._write_json(output, {
                        "meta": {"data_source": "error", "error_type": "permission"},
                        "data": [],
                    })
                elif label == "fetch_kline_eastmoney":
                    output = cmd[cmd.index("-o") + 1]
                    self._write_json(output, {
                        "meta": {"data_source": "eastmoney", "record_count": 1},
                        "data": [{
                            "trade_date": "20260826", "open": 10.0, "high": 10.5,
                            "low": 9.8, "close": 10.2, "vol": 1000.0,
                        }],
                    })
                elif label == "fetch_capital_flow":
                    output = cmd[cmd.index("-o") + 1]
                    self._write_json(output, {
                        "meta": {"data_source": "eastmoney", "record_count": 1},
                        "data": [{"date": "20260826", "main_net_inflow": 1.0}],
                    })
                return {
                    "success": True, "returncode": 0, "stdout": "", "stderr": "",
                }

            argv = [
                "pipeline/runner.py", "600519.SH", "--asset", "E",
                "--freq", freq, "--output-dir", tmpdir,
                "--no-fundamental", "--no-macro",
            ]
            if expected_date:
                argv.extend(["--expected-date", expected_date])
            calendar_patch = patch(
                "fetchers.sector_data.get_last_trading_day",
                return_value=calendar_result,
            ) if calendar_result is not None else None
            with patch.object(sys, "argv", argv), \
                    patch.object(runner, "clean_cache", return_value=0), \
                    patch.object(runner, "run_script", side_effect=fake_run_script):
                if calendar_patch:
                    with calendar_patch:
                        runner.main()
                else:
                    runner.main()
            pipeline_output = json.loads(
                (Path(tmpdir) / "pipeline_output.json").read_text(encoding="utf-8"))
        return commands, pipeline_output

    def test_daily_propagates_one_expected_date_to_all_daily_fetches(self):
        commands, output = self._run_pipeline("D")
        for label in (
                "fetch_kline_tushare", "fetch_kline_eastmoney", "fetch_capital_flow"):
            cmd = commands[label]
            self.assertEqual(cmd[cmd.index("--expected-date") + 1], "2026-08-26")
        self.assertEqual(output["meta"]["expected_date"], "2026-08-26")
        self.assertEqual(output["meta"]["expected_date_source"], "cli")

    def test_weekly_does_not_gate_fetches_by_expected_date(self):
        commands, output = self._run_pipeline("W")
        for label in (
                "fetch_kline_tushare", "fetch_kline_eastmoney", "fetch_capital_flow"):
            self.assertNotIn("--expected-date", commands[label])
        self.assertIsNone(output["meta"]["expected_date"])
        self.assertEqual(output["meta"]["expected_date_source"], "not_applicable")

    def test_daily_calendar_resolution_failure_is_explicit_in_pipeline_errors(self):
        commands, output = self._run_pipeline(
            "D", expected_date=None, calendar_result=(None, ""))
        for label in (
                "fetch_kline_tushare", "fetch_kline_eastmoney", "fetch_capital_flow"):
            self.assertNotIn("--expected-date", commands[label])
        self.assertIsNone(output["meta"]["expected_date"])
        self.assertEqual(output["meta"]["expected_date_source"], "unavailable")
        self.assertIn("Expected daily trading date unavailable", output["errors"])

def run_capital_flow_tests():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestCapitalFlowFallback))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestCapitalFlowCacheValidation))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestKlineExpectedDateValidation))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
        TestPipelineExpectedDatePropagation))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_capital_flow_tests()
    raise SystemExit(1 if failed else 0)
