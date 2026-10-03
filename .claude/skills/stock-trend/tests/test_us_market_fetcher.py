"""Offline tests for the bounded Yahoo Finance US-market fetcher."""

from datetime import date
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import us_market


class UsMarketFrameTests(unittest.TestCase):
    def test_parses_ticker_first_multiindex_and_nulls_nonfinite_values(self):
        columns = pd.MultiIndex.from_product([
            ["SPY", "QQQ"],
            ["Close", "Adj Close", "Dividends", "Stock Splits"],
        ])
        frame = pd.DataFrame([
            [100, 99, 0, 0, 200, 198, 0, 0],
            [101, float("nan"), 1, 0, 202, 201, 0, float("inf")],
        ], index=pd.to_datetime(["2026-09-29", "2026-09-30"]), columns=columns)

        rows, errors = us_market._frame_to_rows(
            frame, ["SPY", "QQQ"], date(2026, 9, 30), date(2026, 9, 30))

        self.assertEqual(errors, {})
        self.assertEqual(rows["SPY"], [{
            "date": "2026-09-30", "close": 101.0, "adj_close": None,
            "dividends": 1.0, "stock_splits": 0.0,
        }])
        self.assertIsNone(rows["QQQ"][0]["stock_splits"])

    def test_parses_field_first_multiindex(self):
        columns = pd.MultiIndex.from_tuples([
            ("Close", "SPY"), ("Adj Close", "SPY"),
            ("Dividends", "SPY"), ("Stock Splits", "SPY"),
        ])
        frame = pd.DataFrame(
            [[100, 99, 0, 0]], index=pd.to_datetime(["2026-10-01"]), columns=columns)

        rows, errors = us_market._frame_to_rows(
            frame, ["SPY"], date(2026, 10, 1), date(2026, 10, 1))

        self.assertEqual(errors, {})
        self.assertEqual(rows["SPY"][0]["adj_close"], 99.0)

    def test_parses_flat_single_symbol_frame(self):
        frame = pd.DataFrame({
            "Close": [100], "Adj Close": [99], "Dividends": [0], "Stock Splits": [0],
        }, index=pd.to_datetime(["2026-10-02"]))
        rows, errors = us_market._frame_to_rows(
            frame, ["SPY"], date(2026, 10, 2), date(2026, 10, 2))
        self.assertEqual(errors, {})
        self.assertEqual(rows["SPY"][0]["date"], "2026-10-02")

    def test_duplicate_session_rejects_symbol_instead_of_choosing_a_value(self):
        frame = pd.DataFrame({
            "Close": [100, 101], "Adj Close": [99, 100],
            "Dividends": [0, 0], "Stock Splits": [0, 0],
        }, index=pd.to_datetime(["2026-10-02", "2026-10-02"]))
        rows, errors = us_market._frame_to_rows(
            frame, ["SPY"], date(2026, 10, 2), date(2026, 10, 2))
        self.assertEqual(rows["SPY"], [])
        self.assertEqual(errors["SPY"], "duplicate_date: 2026-10-02")


class UsMarketBoundaryTests(unittest.TestCase):
    def test_worker_uses_inclusive_end_and_explicit_adjustment_options(self):
        frame = pd.DataFrame({
            "Close": [100], "Adj Close": [99], "Dividends": [0], "Stock Splits": [0],
        }, index=pd.to_datetime(["2026-10-02"]))
        captured = {}

        def download(**kwargs):
            captured.update(kwargs)
            return frame

        with patch.dict(sys.modules, {"yfinance": SimpleNamespace(download=download)}):
            result = us_market._worker_fetch({
                "symbols": ["SPY"], "start": "2026-10-01", "end": "2026-10-02",
                "request_timeout": 7,
            })

        self.assertEqual(result["errors"], {})
        self.assertEqual(captured["end"], "2026-10-03")
        self.assertEqual(captured["timeout"], 7.0)
        self.assertFalse(captured["auto_adjust"])
        self.assertFalse(captured["prepost"])
        self.assertTrue(captured["actions"])
        self.assertEqual(captured["threads"], 1)

    def test_parent_accepts_only_marked_json_amid_noisy_stdout(self):
        payload = {
            "provider": "ignored", "fetched_at": "2026-10-03T00:00:00+00:00",
            "rows": {"SPY": [{"date": "2026-10-02", "close": 1.0}]}, "errors": {},
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="provider noise\n" + us_market._RESULT_PREFIX + json.dumps(payload) + "\n",
            stderr="",
        )
        with patch.object(us_market.subprocess, "run", return_value=completed) as run:
            result = us_market.fetch_us_market(["spy"], "2026-10-01", "2026-10-02")

        self.assertEqual(result["provider"], us_market.PROVIDER)
        self.assertEqual(result["rows"]["SPY"][0]["date"], "2026-10-02")
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["end"], "2026-10-02")

    def test_timeout_degrades_every_symbol(self):
        with patch.object(
            us_market.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=0.01),
        ):
            result = us_market.fetch_us_market(["SPY", "QQQ"], "2026-10-01", "2026-10-02", 0.01)

        self.assertEqual(result["rows"], {"SPY": [], "QQQ": []})
        self.assertIn("timed out", result["errors"]["SPY"])
        self.assertIn("+00:00", result["fetched_at"])

    def test_worker_import_failure_is_explicit(self):
        real_import = __import__

        def reject_yfinance(name, *args, **kwargs):
            if name == "yfinance":
                raise ModuleNotFoundError("test missing dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_yfinance):
            result = us_market._worker_fetch({
                "symbols": ["SPY"], "start": "2026-10-01", "end": "2026-10-02",
            })
        self.assertEqual(result["rows"]["SPY"], [])
        self.assertIn("yfinance unavailable", result["errors"]["SPY"])


if __name__ == "__main__":
    unittest.main()
