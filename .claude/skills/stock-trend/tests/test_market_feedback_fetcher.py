"""Offline tests for next-day market feedback inputs."""

import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import market_feedback


SHANGHAI = ZoneInfo("Asia/Shanghai")


def pool_payload(*, qdate="20261020", total=2):
    data = {
        "qdate": qdate,
        "pool": [
            {"c": "600001", "n": "沪股", "p": 12340, "lbc": 2},
            {"c": "300001", "n": "深股", "p": 5678, "lbc": 1},
        ],
    }
    if total is not None:
        data["tc"] = total
    return json.dumps({"rc": 0, "data": data})


def kline_row(day, pct=1.25):
    return {
        "trade_date": day, "open": 10.0, "close": 10.5,
        "high": 10.8, "low": 9.9, "vol": 12345, "pct_chg": pct,
    }


class MarketFeedbackFetcherTests(unittest.TestCase):
    def test_beijing_920_member_is_frozen_and_can_receive_native_quote(self):
        payload = json.loads(pool_payload())
        payload["data"]["pool"][1]["c"] = "920641"
        with patch.object(market_feedback, "fetch_url", return_value=json.dumps(payload)), \
                patch.object(market_feedback, "fetch_eastmoney", return_value=(
                    [kline_row("20261020", -12.3)], "北交所")) as fetch:
            result = market_feedback.fetch_inputs(
                "2026-10-20", [{"code": "920641.BJ"}], as_of="2026-10-20T16:00:00+08:00")
        self.assertTrue(result["pool"]["complete"])
        self.assertEqual(result["pool"]["members"][1]["code"], "920641.BJ")
        self.assertEqual(result["quotes"][0]["pct_chg"], -12.3)
        self.assertEqual(fetch.call_args.args[0], "0.920641")

    def test_pool_uses_provider_date_price_scale_and_verified_count(self):
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney", return_value=([], "")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", [],
                as_of=datetime(2026, 10, 20, 16, tzinfo=SHANGHAI))

        pool = result["pool"]
        self.assertEqual(pool["data_date"], "2026-10-20")
        self.assertEqual(pool["date_origin"], "provider")
        self.assertTrue(pool["complete"])
        self.assertEqual(pool["members"][0], {
            "code": "600001.SH", "name": "沪股", "close": 12.34,
            "streak": 2, "roles": ["limit_up"],
        })
        self.assertIn("+08:00", pool["fetched_at"])

    def test_pool_without_provider_count_or_date_match_is_not_complete(self):
        with patch.object(
                market_feedback, "fetch_url",
                return_value=pool_payload(qdate="20261017", total=None)):
            result = market_feedback.fetch_inputs(
                "2026-10-20", [], as_of="2026-10-20T16:00:00+08:00")

        self.assertEqual(result["pool"]["data_date"], "2026-10-17")
        self.assertEqual(result["pool"]["date_origin"], "provider")
        self.assertFalse(result["pool"]["complete"])

    def test_pool_duplicate_code_is_deduplicated_and_not_complete(self):
        payload = json.loads(pool_payload())
        payload["data"]["pool"][1]["c"] = "600001"
        with patch.object(
                market_feedback, "fetch_url",
                return_value=json.dumps(payload)):
            result = market_feedback.fetch_inputs(
                "2026-10-20", [], as_of="2026-10-20T16:00:00+08:00")

        self.assertFalse(result["pool"]["complete"])
        self.assertEqual([row["code"] for row in result["pool"]["members"]],
                         ["600001.SH"])

    def test_quotes_keep_all_prior_members_and_native_pct_change(self):
        rows = {
            "1.600001": [kline_row("20261019", 9.9), kline_row("20261020", -3.21)],
            "0.300001": [kline_row("20261020", 4.56)],
        }

        def fetch_kline(secid, freq, **kwargs):
            self.assertEqual(freq, "D")
            self.assertEqual(kwargs, {"lmt": 5, "timeout": 3, "fallback_timeout": 1})
            return rows[secid], "name"

        prior = [
            {"code": "600001.SH", "roles": ["limit_up"]},
            {"code": "300001.SZ", "roles": ["core"]},
            {"code": "830001.BJ", "roles": ["limit_up"]},
        ]
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney", side_effect=fetch_kline):
            result = market_feedback.fetch_inputs(
                "2026-10-20", prior, as_of="2026-10-20T16:00:00+08:00")

        self.assertEqual([row["code"] for row in result["quotes"]],
                         ["600001.SH", "300001.SZ", "830001.BJ"])
        self.assertEqual(result["quotes"][0]["pct_chg"], -3.21)
        self.assertEqual(result["quotes"][0]["data_date"], "2026-10-20")
        self.assertEqual(result["quotes"][0]["mode"], "close")
        self.assertEqual(result["quotes"][2]["status"], "missing")
        self.assertTrue(any(error["code"] == "830001.BJ" for error in result["errors"]))

    def test_missing_basis_row_does_not_fall_back_or_use_future_row(self):
        records = [kline_row("20261017", 2.0), kline_row("20261021", 3.0)]
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney", return_value=(records, "")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00")

        self.assertEqual(result["quotes"][0]["status"], "missing")
        self.assertIsNone(result["quotes"][0]["data_date"])
        self.assertIsNone(result["quotes"][0]["pct_chg"])

    def test_same_day_unconfirmed_row_is_intraday_and_keeps_raw_date(self):
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(
                    market_feedback, "fetch_eastmoney",
                    return_value=([kline_row("2026-10-20")], "")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T13:05:00+08:00")

        quote = result["quotes"][0]
        self.assertEqual(quote["mode"], "intraday")
        self.assertEqual(quote["source_time_raw"], "2026-10-20")

    def test_fetched_at_is_completion_time_but_mode_uses_request_cutoff(self):
        pool_done = datetime(2026, 10, 20, 15, 9, 30, tzinfo=SHANGHAI)
        quote_setup = datetime(2026, 10, 20, 15, 9, 40, tzinfo=SHANGHAI)
        quote_done = datetime(2026, 10, 20, 15, 10, 5, tzinfo=SHANGHAI)
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(
                    market_feedback, "fetch_eastmoney",
                    return_value=([kline_row("20261020")], "")), \
                patch.object(
                    market_feedback, "_now",
                    side_effect=[pool_done, quote_setup, quote_done]):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T15:09:00+08:00")

        self.assertEqual(result["pool"]["fetched_at"], pool_done.isoformat())
        self.assertEqual(result["quotes"][0]["fetched_at"], quote_done.isoformat())
        self.assertEqual(result["quotes"][0]["mode"], "intraday")

    def test_hard_budget_returns_promptly_and_preserves_full_universe(self):
        prior = [f"60000{i}.SH" for i in range(6)]

        def slow_fetch(*args, **kwargs):
            time.sleep(0.2)
            return [kline_row("20261020")], ""

        started = time.monotonic()
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney", side_effect=slow_fetch):
            result = market_feedback.fetch_inputs(
                "2026-10-20", prior, as_of="2026-10-20T16:00:00+08:00",
                budget_seconds=0.02)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(len(result["quotes"]), len(prior))
        self.assertTrue(all(row["status"] == "missing" for row in result["quotes"]))
        self.assertEqual(
            {error["code"] for error in result["errors"] if error["stage"] == "quote"},
            set(prior),
        )


if __name__ == "__main__":
    unittest.main()
