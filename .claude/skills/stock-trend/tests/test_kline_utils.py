#!/usr/bin/env python3
"""Regression tests for shared K-line payload and command contracts."""

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from core.kline_utils import (  # noqa: E402
    build_kline_fetch_command,
    cache_validation,
    is_usable_kline_payload,
    latest_kline_date,
    normalize_kline_date,
    reject_stale_payload,
    reject_stale_kline_payload,
    validate_kline_coverage,
)
from fetchers import kline_eastmoney  # noqa: E402
from scans import stock_scanner  # noqa: E402


class TestKlinePayloadContract(unittest.TestCase):
    def test_normalize_kline_date_accepts_supported_formats(self):
        self.assertEqual(normalize_kline_date("20260813"), "2026-08-13")
        self.assertEqual(normalize_kline_date(" 2026-08-13 "), "2026-08-13")
        self.assertEqual(normalize_kline_date("2026-02-30"), "")
        self.assertEqual(normalize_kline_date(""), "")

    def test_latest_kline_date_ignores_invalid_and_unsorted_rows(self):
        payload = {
            "data": [
                {"trade_date": "20260812"},
                {"date": "2026-08-14"},
                {"trade_date": "bad"},
                "not-a-row",
                {"trade_date": "20260813"},
            ]
        }
        self.assertEqual(latest_kline_date(payload), "2026-08-14")
        self.assertEqual(latest_kline_date({"data": []}), "")
        self.assertEqual(latest_kline_date({"data": {}}), "")

    def test_cache_validation_supports_expected_date_formats(self):
        payload = {"data": [{"trade_date": "20260813"}]}
        self.assertEqual(
            cache_validation(payload, "2026-08-13"),
            {"expected_date": "2026-08-13", "latest_date": "2026-08-13", "valid": True},
        )
        self.assertTrue(cache_validation(payload, "20260813")["valid"])
        self.assertEqual(
            validate_kline_coverage(payload, "20260813"),
            cache_validation(payload, "20260813"),
        )
        self.assertFalse(cache_validation(payload, "2026-08-14")["valid"])

    def test_reject_stale_payload_preserves_stale_contract(self):
        payload = {
            "meta": {"data_source": "eastmoney", "record_count": 1},
            "data": [{"trade_date": "20260812", "close": 10}],
        }
        result = reject_stale_payload(payload, "2026-08-13")
        self.assertEqual(result, {
            "meta": {
                "data_source": "error",
                "error_type": "stale_data",
                "stale_data_source": "eastmoney",
                "record_count": 0,
                "error": "数据最新日期2026-08-12早于预期交易日2026-08-13",
                "cache_validation": {
                    "expected_date": "2026-08-13",
                    "latest_date": "2026-08-12",
                    "valid": False,
                },
            },
            "data": [],
        })
        self.assertEqual(
            reject_stale_kline_payload(
                {"meta": {"data_source": "eastmoney"}, "data": []},
                "2026-08-13",
            ),
            reject_stale_payload(
                {"meta": {"data_source": "eastmoney"}, "data": []},
                "2026-08-13",
            ),
        )

    def test_is_usable_kline_payload_checks_latest_ohlc(self):
        payload = {
            "meta": {"data_source": "tushare"},
            "data": [
                {"trade_date": "20260812", "open": 9, "high": 10,
                 "low": 8, "close": 9.5},
                {"trade_date": "20260813", "open": 9.5, "high": 10,
                 "low": 9, "close": 9.8},
            ],
        }
        self.assertTrue(is_usable_kline_payload(payload))
        payload["data"][-1].pop("close")
        self.assertFalse(is_usable_kline_payload(payload))

        payload["data"][-1]["close"] = 9.8
        payload["meta"]["data_source"] = "error"
        self.assertFalse(is_usable_kline_payload(payload))


class TestKlineProviderFallback(unittest.TestCase):
    @staticmethod
    def _rows(last_date):
        return [{"trade_date": last_date, "open": 10, "high": 11,
                 "low": 9, "close": 10.5, "vol": 100}]

    def test_stale_eastmoney_continues_to_current_tencent(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "kline.json"
            argv = ["kline_eastmoney.py", "301489.SZ", "--no-cache",
                    "--expected-date", "2026-09-29", "-o", str(output)]
            with patch.object(sys, "argv", argv), \
                    patch("core.eastmoney_utils.rotate_em_host", return_value=(
                        (self._rows("20260928"), "思泉新材"),
                        "push2his.eastmoney.com")), \
                    patch.object(kline_eastmoney, "fetch_tencent_a_stock",
                         return_value=(self._rows("20260929"), "思泉新材")), \
                    patch.object(kline_eastmoney, "fetch_baostock",
                         side_effect=AssertionError("BaoStock should not run")), \
                    patch.object(kline_eastmoney, "save_cache"):
                kline_eastmoney.main()
            payload = json.loads(output.read_text())
            self.assertEqual(payload["meta"]["data_source"], "tencent_a")
            self.assertEqual(payload["meta"]["cache_validation"]["latest_date"],
                             "2026-09-29")
            self.assertEqual([a["status"] for a in payload["meta"]["provider_attempts"]],
                             ["stale", "success"])

    def test_all_stale_sources_return_dated_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "kline.json"
            argv = ["kline_eastmoney.py", "301489.SZ", "--no-cache",
                    "--expected-date", "2026-09-29", "-o", str(output)]
            with patch.object(sys, "argv", argv), \
                    patch("core.eastmoney_utils.rotate_em_host", return_value=(
                        (self._rows("20260928"), "思泉新材"),
                        "push2his.eastmoney.com")), \
                    patch.object(kline_eastmoney, "fetch_tencent_a_stock",
                         return_value=(self._rows("20260927"), "思泉新材")), \
                    patch.object(kline_eastmoney, "fetch_baostock",
                         return_value=(self._rows("20260928"), "思泉新材")), \
                    patch.object(kline_eastmoney, "save_cache"):
                kline_eastmoney.main()
            payload = json.loads(output.read_text())
            self.assertEqual(payload["meta"]["error_type"], "stale_data")
            self.assertEqual(payload["data"], [])
            self.assertEqual([a["status"] for a in payload["meta"]["provider_attempts"]],
                             ["stale", "stale", "stale"])
            self.assertEqual(payload["meta"]["cache_validation"]["latest_date"],
                             "2026-09-28")

    def test_current_but_short_eastmoney_continues_to_complete_tencent(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "kline.json"
            argv = ["kline_eastmoney.py", "301489.SZ", "--no-cache",
                    "--expected-date", "2026-09-29", "--min-records", "60",
                    "-o", str(output)]
            first = date(2026, 8, 1)
            complete = [self._rows((first + timedelta(days=i)).strftime("%Y%m%d"))[0]
                        for i in range(60)]
            with patch.object(sys, "argv", argv), \
                    patch("core.eastmoney_utils.rotate_em_host", return_value=(
                        (self._rows("20260929"), "思泉新材"),
                        "push2his.eastmoney.com")), \
                    patch.object(kline_eastmoney, "fetch_tencent_a_stock",
                         return_value=(complete, "思泉新材")), \
                    patch.object(kline_eastmoney, "fetch_baostock",
                         side_effect=AssertionError("BaoStock should not run")), \
                    patch.object(kline_eastmoney, "save_cache"):
                kline_eastmoney.main()
            payload = json.loads(output.read_text())
            self.assertEqual(payload["meta"]["data_source"], "tencent_a")
            self.assertEqual(len(payload["data"]), 60)
            self.assertEqual([a["status"] for a in payload["meta"]["provider_attempts"]],
                             ["insufficient_bars", "success"])

    def test_all_current_but_short_sources_are_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "kline.json"
            argv = ["kline_eastmoney.py", "301489.SZ", "--no-cache",
                    "--expected-date", "2026-09-29", "--min-records", "60",
                    "-o", str(output)]
            short = self._rows("20260929")
            with patch.object(sys, "argv", argv), \
                    patch("core.eastmoney_utils.rotate_em_host", return_value=(
                        (short, "思泉新材"), "push2his.eastmoney.com")), \
                    patch.object(kline_eastmoney, "fetch_tencent_a_stock",
                         return_value=(short, "思泉新材")), \
                    patch.object(kline_eastmoney, "fetch_baostock",
                         return_value=(short, "思泉新材")), \
                    patch.object(kline_eastmoney, "save_cache") as save:
                kline_eastmoney.main()
            payload = json.loads(output.read_text())
            self.assertEqual(payload["meta"]["error_type"], "insufficient_data")
            self.assertEqual(payload["meta"]["usable_record_count"], 1)
            self.assertEqual(payload["data"], [])
            save.assert_not_called()


class TestScannerKlineRefresh(unittest.TestCase):
    def test_failed_refresh_preserves_last_good_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "301489" / "kline.json"
            cache.parent.mkdir()
            first = date(2026, 7, 31)
            rows = [{"trade_date": (first + timedelta(days=i)).strftime("%Y%m%d"),
                     "open": 10, "high": 11, "low": 9, "close": 10.5}
                    for i in range(60)]
            old = {"meta": {"data_source": "eastmoney"}, "data": rows}
            cache.write_text(json.dumps(old))

            def failed_fetch(cmd, **_kwargs):
                output = Path(cmd[cmd.index("-o") + 1])
                output.write_text(json.dumps({
                    "meta": {"data_source": "error", "error_type": "stale_data",
                             "stale_data_source": "baostock",
                             "cache_validation": {"latest_date": "2026-09-28"}},
                    "data": [],
                }))
                return {"success": True, "error": "", "stderr": ""}

            with patch.object(stock_scanner, "CACHE_DIR", tmp), \
                    patch.object(stock_scanner, "run_script", side_effect=failed_fetch):
                result = stock_scanner._fetch_kline(
                    "301489.SZ", as_of_date="2026-09-29")
            self.assertEqual(json.loads(cache.read_text()), old)
            self.assertEqual(result["meta"]["error_type"], "stale_data")


class TestKlineCommandContract(unittest.TestCase):
    def test_tushare_command_snapshot(self):
        command = build_kline_fetch_command(
            "tushare", "600519.SH", "/tmp/kline.json",
            asset="E", freq="D", adj="qfq", no_cache=True,
            expected_date="2026-08-13", start_date="20260214",
            python_executable="python-test",
        )
        self.assertEqual(command, [
            "python-test",
            str(SCRIPTS_DIR / "fetchers" / "kline.py"),
            "600519.SH", "--asset", "E", "--freq", "D", "--adj", "qfq",
            "-o", "/tmp/kline.json", "--no-cache",
            "--expected-date", "2026-08-13", "--start-date", "20260214",
        ])

    def test_eastmoney_command_preserves_provider_args_and_limit_order(self):
        command = build_kline_fetch_command(
            "eastmoney", "600519.SH", "/tmp/kline.json",
            asset="E", freq="D", expected_date="2026-08-13", limit=250,
            provider_args=("--em-timeout", "4", "--fallback-timeout", "8"),
            python_executable="python-test",
        )
        self.assertEqual(command, [
            "python-test",
            str(SCRIPTS_DIR / "fetchers" / "kline_eastmoney.py"),
            "600519.SH", "--asset", "E", "--freq", "D",
            "--em-timeout", "4", "--fallback-timeout", "8",
            "-o", "/tmp/kline.json", "--expected-date", "2026-08-13",
            "--lmt", "250",
        ])

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            build_kline_fetch_command(
                "unknown", "600519.SH", "/tmp/kline.json",
                asset="E", freq="D",
            )


if __name__ == "__main__":
    unittest.main()
