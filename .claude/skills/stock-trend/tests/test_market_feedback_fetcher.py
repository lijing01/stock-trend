"""Offline tests for next-day market feedback inputs."""

import json
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import market_feedback


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
            self.assertEqual(kwargs["lmt"], 5)
            self.assertEqual(kwargs["timeout"], 3)
            self.assertEqual(kwargs["fallback_timeout"], 1)
            self.assertEqual(kwargs["adjustment"], 0)
            self.assertIsInstance(kwargs["deadline"], float)
            self.assertTrue(hasattr(kwargs["cancel_event"], "is_set"))
            self.assertNotIn("host", kwargs)
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
        self.assertEqual(result["quotes"][0]["pct_chg_origin"], "provider")
        self.assertEqual(result["quotes"][0]["price_basis"], "native")
        self.assertTrue(result["quotes"][0]["close_confirmed"])
        self.assertEqual(result["quotes"][0]["finality"], "live_daily_close")
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
        self.assertEqual(result["quotes"][0]["failure_reason"],
                         "missing_target_session")
        self.assertEqual(result["quotes"][0]["latest_provider_date"],
                         "2026-10-21")
        self.assertTrue(result["quotes"][0]["provider_attempts"])

    def test_alternate_eastmoney_node_recovers_primary_failure(self):
        calls = []

        def fetch_kline(secid, freq, **kwargs):
            calls.append(kwargs.get("host", "push2his.eastmoney.com"))
            if "host" not in kwargs:
                raise RuntimeError(
                    "东方财富API请求失败: https://host/path?ut=secret-token")
            return [kline_row("20261020", -2.5)], ""

        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=fetch_kline):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00")

        quote = result["quotes"][0]
        self.assertEqual(quote["status"], "ok")
        self.assertEqual(quote["pct_chg"], -2.5)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(quote["provider_attempts"][0]["status"], "error")
        self.assertEqual(quote["provider_attempts"][-1]["status"], "success")
        self.assertNotIn("secret-token", json.dumps(
            quote["provider_attempts"], ensure_ascii=False))

    def test_tencent_native_quote_recovers_primary_failure_before_extra_em_node(self):
        fixture = json.loads((
            FIXTURES / "tencent_native_quote_20261023.json").read_text())
        response = fixture["responses"]["600519.SH"]
        em_hosts = []

        def fetch_kline(_secid, _freq, **kwargs):
            em_hosts.append(kwargs.get("host", "primary"))
            raise TimeoutError("eastmoney unavailable")

        def fetch_http(url, **_kwargs):
            if url.startswith(market_feedback.POOL_URL):
                return pool_payload(qdate="20261023")
            self.assertIn("param=sh600519,day,,,5,qfq", url)
            return json.dumps(response)

        with patch.object(market_feedback, "fetch_url", side_effect=fetch_http), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=fetch_kline):
            result = market_feedback.fetch_inputs(
                "2026-10-23", ["600519.SH"],
                as_of="2026-10-23T16:40:00+08:00")

        quote = result["quotes"][0]
        self.assertEqual(em_hosts, ["primary"])
        self.assertEqual(quote["status"], "ok")
        self.assertEqual(quote["source"], "tencent_native_quote")
        self.assertEqual(quote["pct_chg"], 1.25)
        self.assertEqual(quote["pct_chg_origin"], "provider")
        self.assertEqual(quote["price_basis"], "native")
        self.assertEqual(quote["volume_unit"], "provider_native_volume")
        self.assertTrue(quote["close_confirmed"])
        self.assertEqual(quote["source_time_raw"], "20261023161458")
        self.assertEqual(
            [attempt["provider"] for attempt in quote["provider_attempts"]],
            ["eastmoney", "tencent_native"],
        )

    def test_tencent_native_rejects_wrong_source_day_and_falls_through(self):
        fixture = json.loads((
            FIXTURES / "tencent_native_quote_20261023.json").read_text())
        response = fixture["responses"]["000001.SZ"]

        def fetch_http(url, **_kwargs):
            if url.startswith(market_feedback.POOL_URL):
                return pool_payload(qdate="20261022")
            return json.dumps(response)

        with patch.object(market_feedback, "fetch_url", side_effect=fetch_http), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=TimeoutError("eastmoney unavailable")):
            result = market_feedback.fetch_inputs(
                "2026-10-22", ["000001.SZ"],
                as_of="2026-10-22T16:40:00+08:00")

        quote = result["quotes"][0]
        self.assertEqual(quote["status"], "missing")
        tencent = [attempt for attempt in quote["provider_attempts"]
                   if attempt["provider"] == "tencent_native"]
        self.assertEqual(tencent[0]["reason"], "source_date_mismatch")

    def test_tencent_close_marker_requires_exact_market_flag(self):
        fixture = json.loads((
            FIXTURES / "tencent_native_quote_20261023.json").read_text())
        response = fixture["responses"]["600519.SH"]
        response["data"]["sh600519"]["qt"]["market"] = [
            "2026-10-23 16:32:28|NEWSH_close_已收盘|SZ_close_已收盘"
        ]

        def fetch_http(url, **_kwargs):
            if url.startswith(market_feedback.POOL_URL):
                return pool_payload(qdate="20261023")
            return json.dumps(response)

        with patch.object(market_feedback, "fetch_url", side_effect=fetch_http), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=TimeoutError("eastmoney unavailable")):
            result = market_feedback.fetch_inputs(
                "2026-10-23", ["600519.SH"],
                as_of="2026-10-23T16:40:00+08:00")

        tencent = [attempt for attempt in result["quotes"][0]["provider_attempts"]
                   if attempt["provider"] == "tencent_native"]
        self.assertEqual(tencent[0]["reason"], "close_not_confirmed")

    def test_safe_error_redacts_bare_credentials_and_is_bounded(self):
        error = market_feedback._safe_error(
            "token=topsecret api_key=alsosecret authorization=BearerSecret "
            "ut=thirdsecret " + "x" * 500)
        self.assertNotIn("topsecret", error)
        self.assertNotIn("alsosecret", error)
        self.assertNotIn("BearerSecret", error)
        self.assertNotIn("thirdsecret", error)
        self.assertLessEqual(len(error), 300)

    def test_all_quote_failures_preserve_terminal_diagnostics(self):
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=TimeoutError("provider timed out")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH", "300001.SZ"],
                as_of="2026-10-20T16:00:00+08:00")

        self.assertTrue(all(
            quote["failure_reason"] == "timeout"
            and quote["provider_attempts"]
            for quote in result["quotes"]
        ))
        errors = [error for error in result["errors"]
                  if error["stage"] == "quote"]
        self.assertEqual({error["code"] for error in errors},
                         {"600001.SH", "300001.SZ"})
        self.assertTrue(all(error["reason"] == "timeout" for error in errors))

    def test_pool_failure_does_not_block_prior_quote_collection(self):
        with patch.object(market_feedback, "fetch_url",
                          side_effect=RuntimeError("pool unavailable")), \
                patch.object(market_feedback, "fetch_eastmoney", return_value=(
                    [kline_row("20261020", 3.5)], "")) as fetch:
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00")

        self.assertFalse(result["pool"]["complete"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["quotes"][0]["status"], "ok")

    def test_slow_pool_is_rejected_after_its_budget_and_quotes_continue(self):
        def slow_pool(*_args, **_kwargs):
            time.sleep(0.08)
            return pool_payload()

        started = time.monotonic()
        with patch.object(market_feedback, "fetch_url", side_effect=slow_pool), \
                patch.object(market_feedback, "fetch_eastmoney", return_value=(
                    [kline_row("20261020", 3.5)], "")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00", timeout=0.02,
                budget_seconds=0.2)
            snapshot = json.dumps(result, ensure_ascii=False, sort_keys=True)
            time.sleep(0.1)

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(result["pool"]["complete"])
        self.assertEqual(result["quotes"][0]["status"], "ok")
        self.assertEqual(snapshot, json.dumps(
            result, ensure_ascii=False, sort_keys=True))

    def test_exact_day_with_missing_native_change_is_invalid(self):
        row = kline_row("20261020")
        row["pct_chg"] = None
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney",
                             return_value=([row], "")):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00")

        quote = result["quotes"][0]
        self.assertEqual(quote["status"], "missing")
        self.assertEqual(quote["reason"], "invalid_quote")
        self.assertIsNone(quote["pct_chg"])

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
        self.assertFalse(quote["close_confirmed"])
        self.assertEqual(quote["finality"], "intraday_live_daily")
        self.assertEqual(quote["source_time_raw"], "2026-10-20")

    def test_fetched_at_is_completion_time_but_mode_uses_request_cutoff(self):
        pool_done = datetime(2026, 10, 20, 15, 9, 30, tzinfo=SHANGHAI)
        quote_setup = datetime(2026, 10, 20, 15, 9, 40, tzinfo=SHANGHAI)
        quote_done = datetime(2026, 10, 20, 15, 10, 5, tzinfo=SHANGHAI)
        final_done = datetime(2026, 10, 20, 15, 10, 6, tzinfo=SHANGHAI)
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(
                    market_feedback, "fetch_eastmoney",
                    return_value=([kline_row("20261020")], "")), \
                patch.object(
                    market_feedback, "_now",
                    side_effect=[pool_done, quote_setup, quote_setup,
                                 quote_done, final_done]):
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
        self.assertTrue(all(
            row["failure_reason"] in {"budget_exhausted", "not_started"}
            for row in result["quotes"]
        ))

    def test_quote_fetch_never_exceeds_four_concurrent_workers(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        def counted_fetch(*_args, **_kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.02)
                return [kline_row("20261020")], ""
            finally:
                with guard:
                    active -= 1

        prior = [f"6000{i:02d}.SH" for i in range(12)]
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=counted_fetch):
            result = market_feedback.fetch_inputs(
                "2026-10-20", prior,
                as_of="2026-10-20T16:00:00+08:00")

        self.assertEqual(len(result["quotes"]), len(prior))
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, 4)

    def test_late_primary_completion_cannot_start_more_work_or_mutate_result(self):
        calls = []

        def slow_fetch(*args, **kwargs):
            host = kwargs.get("host", "primary")
            calls.append(host)
            if host == "primary":
                time.sleep(0.08)
            raise RuntimeError("late failure")

        cancel_event = market_feedback.threading.Event()
        deadline = time.monotonic() + 0.02
        with patch.object(market_feedback, "fetch_url", return_value=pool_payload()), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=slow_fetch):
            result = market_feedback.fetch_inputs(
                "2026-10-20", ["600001.SH"],
                as_of="2026-10-20T16:00:00+08:00", budget_seconds=0.02,
                deadline=deadline, cancel_event=cancel_event)
            snapshot = json.dumps(result, ensure_ascii=False, sort_keys=True)
            calls_at_return = list(calls)
            cancel_event.set()
            time.sleep(0.12)

        self.assertEqual(calls_at_return[0], "primary")
        self.assertEqual(calls, calls_at_return)
        self.assertEqual(snapshot, json.dumps(
            result, ensure_ascii=False, sort_keys=True))

    def test_one_stuck_primary_does_not_block_other_codes_native_recovery(self):
        fixture = json.loads((
            FIXTURES / "tencent_native_quote_20261023.json").read_text())
        active = 0
        peak = 0
        guard = threading.Lock()

        def enter(call):
            def wrapped(*args, **kwargs):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    return call(*args, **kwargs)
                finally:
                    with guard:
                        active -= 1
            return wrapped

        @enter
        def fetch_kline(secid, _freq, **_kwargs):
            if secid == "1.600519":
                time.sleep(0.4)
            raise TimeoutError("primary unavailable")

        @enter
        def fetch_http(url, **_kwargs):
            if url.startswith(market_feedback.POOL_URL):
                return pool_payload(qdate="20261023")
            key = "600519.SH" if "sh600519" in url else "000001.SZ"
            return json.dumps(fixture["responses"][key])

        with patch.object(market_feedback, "fetch_url", side_effect=fetch_http), \
                patch.object(market_feedback, "fetch_eastmoney",
                             side_effect=fetch_kline):
            result = market_feedback.fetch_inputs(
                "2026-10-23", ["600519.SH", "000001.SZ"],
                as_of="2026-10-23T16:40:00+08:00",
                budget_seconds=0.5)

        by_code = {quote["code"]: quote for quote in result["quotes"]}
        self.assertEqual(by_code["000001.SZ"]["source"],
                         "tencent_native_quote")
        self.assertLessEqual(peak, 4)


if __name__ == "__main__":
    unittest.main()
