#!/usr/bin/env python3
"""Regression tests for shared K-line payload and command contracts."""

import sys
import unittest
from pathlib import Path

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
