#!/usr/bin/env python3
"""stock_scanner Wyckoff 漏斗 (P0-2) 测试套件.

覆盖:
  - normalize_wyckoff_score: [-3,+3] -> [0,100]
  - wyckoff_gate_pass: 买点子阶段过滤(吸筹/拉升才留)
  - score_wyckoff: 100 分制维度 + 高置信度买点加成
  - run_phase2 漏斗: monkeypatch 数据源,验证 gate 过滤 + wyckoff 维度 + 复合分重配
  - 无 --wyckoff 时行为不变(无 wyckoff 维度)

Usage:
    python3 test_stock_scanner.py [-v]
"""

import sys
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans import stock_scanner as sc
from fetchers import sector_data as sd
from fetchers import fundamental as fd


def _make_kline(n=60, ts_code="TEST"):
    """Synthetic flat K-line (flat price/vol → neutral momentum/volume scores)."""
    rows = []
    price = 10.0
    for i in range(n):
        rows.append({
            "trade_date": f"20260101+{i:03d}",
            "open": price, "high": price + 0.2, "low": price - 0.2,
            "close": price, "vol": 1000.0, "pre_close": price,
        })
    return {"meta": {"ts_code": ts_code, "name": "测试"}, "data": rows}


def _wk(phase="accumulation", sub="lps", conf=0.6, score=2.0):
    """Fabricated wyckoff analysis dict (same shape as analyze_kline_dict)."""
    return {
        "phase": {"primary": phase, "primary_name": "吸筹阶段",
                  "primary_sub_phase": sub, "sub_phase_name": f"子阶段:{sub}",
                  "confidence": conf},
        "range": {"support": 9.0, "resistance": 11.0},
        "vsa_signals": [],
        "cause_effect": {},
        "wyckoff_score": score,
        "wyckoff_signals": {"verdict": "bullish", "key_signals": [],
                            "trading_implication": "做好突破入场准备。"},
        "long_term": {"eligible": True, "phase": "accumulation",
                      "phase_name": "吸筹阶段", "confidence": 0.7,
                      "range": {"support": 8.0, "resistance": 12.0}},
    }


def _make_candidate(code="600001"):
    return {
        "code": code, "ts_code": f"{code}.SH", "name": f"测试{code}",
        "sector_code": "BK0477", "sector_name": "测试板块",
        "sector_hot_score": 80, "change_pct": 1.0, "amount": 1e8,
        "market_cap": 1e10, "pe": 20.0,
    }


def _make_dated_kline(n=60, ts_code="TEST", trade_date="20260806"):
    kline = _make_kline(n, ts_code)
    for row in kline["data"]:
        row["trade_date"] = trade_date
    return kline


class TestNormalize(unittest.TestCase):
    def test_range_mapping(self):
        self.assertAlmostEqual(sc.normalize_wyckoff_score(-3.0), 0.0)
        self.assertAlmostEqual(sc.normalize_wyckoff_score(0.0), 50.0)
        self.assertAlmostEqual(sc.normalize_wyckoff_score(3.0), 100.0)
        self.assertAlmostEqual(sc.normalize_wyckoff_score(2.0), 83.3333, places=3)
        self.assertAlmostEqual(sc.normalize_wyckoff_score(-2.0), 16.6667, places=3)

    def test_clamp(self):
        self.assertEqual(sc.normalize_wyckoff_score(5.0), 100.0)
        self.assertEqual(sc.normalize_wyckoff_score(-5.0), 0.0)


class TestMetadata(unittest.TestCase):
    def _valid_kline(self, trade_date="20260813"):
        payload = _make_dated_kline(trade_date=trade_date)
        payload["meta"].update({
            "data_source": "eastmoney", "data_quality": "good",
        })
        return payload

    def _valid_capital(self, trade_date="20260813"):
        return {
            "meta": {
                "data_source": "eastmoney", "data_quality": "good",
                "fetch_time": "20260813-160000",
            },
            "data": [{"date": trade_date, "main_net_inflow": 1}],
        }

    def _valid_fundamental(self):
        return {
            "meta": {
                "data_source": "akshare", "fetch_time": "20260813-160000",
            },
            "summary": {"data_quality": "good", "roe": 12},
        }

    def test_cache_validators_reject_error_and_incomplete_payloads(self):
        kline = self._valid_kline()
        kline["errors"] = ["partial provider failure"]
        capital = self._valid_capital()
        capital["meta"]["data_source"] = "error"
        fundamental = self._valid_fundamental()
        fundamental["summary"]["data_quality"] = "error"

        self.assertFalse(sc._validate_kline_cache(
            kline, "2026-08-13")["valid"])
        self.assertFalse(sc._validate_capital_cache(
            capital, "2026-08-13", cache_age_seconds=1,
            ttl_seconds=300)["valid"])
        self.assertFalse(sc._validate_fundamental_cache(
            fundamental, cache_age_seconds=1,
            ttl_seconds=1800)["valid"])

        missing_fundamental_quality = self._valid_fundamental()
        del missing_fundamental_quality["summary"]["data_quality"]
        self.assertFalse(sc._validate_fundamental_cache(
            missing_fundamental_quality, cache_age_seconds=1,
            ttl_seconds=1800)["valid"])

        insufficient = self._valid_kline()
        insufficient["data"] = insufficient["data"][:29]
        verdict = sc._validate_kline_cache(
            insufficient, "2026-08-13")
        self.assertFalse(verdict["valid"])
        self.assertIn("insufficient_data", verdict["reasons"])

    def test_kline_validator_requires_sixty_bars_for_production_wyckoff(self):
        payload = self._valid_kline()
        payload["data"] = payload["data"][:59]
        verdict = sc._validate_kline_cache(payload, "2026-08-13")
        self.assertFalse(verdict["valid"])
        self.assertIn("insufficient_data", verdict["reasons"])

    def test_capital_validator_requires_numeric_flow_evidence(self):
        date_only = self._valid_capital()
        date_only["data"] = [{"date": "20260813"}]
        verdict = sc._validate_capital_cache(
            date_only, "2026-08-13", cache_age_seconds=1,
            ttl_seconds=300)
        self.assertFalse(verdict["valid"])
        self.assertIn("flow_metrics_missing", verdict["reasons"])

        zero_flow = self._valid_capital()
        zero_flow["data"][0]["main_net_inflow"] = 0
        self.assertTrue(sc._validate_capital_cache(
            zero_flow, "2026-08-13", cache_age_seconds=1,
            ttl_seconds=300)["valid"])

    def test_capital_numeric_flow_must_be_on_expected_date_row(self):
        payload = self._valid_capital()
        payload["data"] = [
            {"date": "20260812", "main_net_inflow": 10},
            {"date": "20260813"},
        ]
        verdict = sc._validate_capital_cache(
            payload, "2026-08-13", cache_age_seconds=1,
            ttl_seconds=300)
        self.assertFalse(verdict["valid"])
        self.assertIn("flow_metrics_missing", verdict["reasons"])

    def test_kline_validator_counts_only_structurally_usable_dated_rows(self):
        payload = self._valid_kline()
        payload["data"][-1] = {"trade_date": "20260813", "close": 10}
        verdict = sc._validate_kline_cache(payload, "2026-08-13")
        self.assertFalse(verdict["valid"])
        self.assertIn("insufficient_data", verdict["reasons"])

    def test_fundamental_partial_allows_optional_subsource_errors(self):
        payload = self._valid_fundamental()
        payload["summary"]["data_quality"] = "partial"
        payload["errors"] = ["valuation history unavailable"]
        verdict = sc._validate_fundamental_cache(
            payload, cache_age_seconds=1, ttl_seconds=1800)
        self.assertTrue(verdict["valid"])

        payload["error"] = "fatal"
        self.assertFalse(sc._validate_fundamental_cache(
            payload, cache_age_seconds=1,
            ttl_seconds=1800)["valid"])

    def test_cache_validators_do_not_crash_on_malformed_nested_values(self):
        malformed = [
            [],
            {"meta": [], "summary": [], "data": []},
            {"meta": "bad", "summary": "bad", "data": "bad"},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertFalse(sc._validate_kline_cache(
                    payload, "2026-08-13")["valid"])
                self.assertFalse(sc._validate_capital_cache(
                    payload, "2026-08-13", 1, 300)["valid"])
                self.assertFalse(sc._validate_fundamental_cache(
                    payload, "2026-08-13", 1, 1800)["valid"])

    def test_lunch_break_uses_intraday_cache_ttls(self):
        lunch = datetime(2026, 8, 13, 12, 0)
        self.assertEqual(sc._cache_ttl_seconds("capital", lunch), 300)
        self.assertEqual(
            sc._cache_ttl_seconds("fundamental", lunch), 1800)
        after_close = datetime(2026, 8, 13, 16, 0)
        self.assertEqual(
            sc._cache_ttl_seconds("capital", after_close), 57600)
        self.assertEqual(
            sc._cache_ttl_seconds("fundamental", after_close), 57600)

    def test_capital_validator_enforces_expected_trading_date_and_ttl(self):
        payload = self._valid_capital("20260812")
        wrong_date = sc._validate_capital_cache(
            payload, "2026-08-13", cache_age_seconds=1,
            ttl_seconds=300)
        self.assertFalse(wrong_date["valid"])
        self.assertIn("wrong_trading_date", wrong_date["reasons"])

        payload = self._valid_capital("20260813")
        self.assertTrue(sc._validate_capital_cache(
            payload, "2026-08-13", cache_age_seconds=299,
            ttl_seconds=300)["valid"])
        expired = sc._validate_capital_cache(
            payload, "2026-08-13", cache_age_seconds=301,
            ttl_seconds=300)
        self.assertFalse(expired["valid"])
        self.assertIn("cache_expired", expired["reasons"])

    def test_fundamental_validator_supports_intraday_and_after_hours_ttl(self):
        payload = self._valid_fundamental()
        self.assertTrue(sc._validate_fundamental_cache(
            payload, cache_age_seconds=1799,
            ttl_seconds=1800)["valid"])
        self.assertFalse(sc._validate_fundamental_cache(
            payload, cache_age_seconds=1801,
            ttl_seconds=1800)["valid"])
        self.assertTrue(sc._validate_fundamental_cache(
            payload, cache_age_seconds=57599,
            ttl_seconds=57600)["valid"])
        self.assertFalse(sc._validate_fundamental_cache(
            payload, cache_age_seconds=57601,
            ttl_seconds=57600)["valid"])

    def test_validators_use_injected_latest_trading_day_on_closed_days(self):
        cases = [
            ("weekend", "2026-08-14"),
            ("holiday", "2026-09-30"),
            ("pre_holiday", "2026-09-30"),
        ]
        for label, expected in cases:
            with self.subTest(label=label):
                compact = expected.replace("-", "")
                self.assertTrue(sc._validate_kline_cache(
                    self._valid_kline(compact), expected)["valid"])
                self.assertTrue(sc._validate_capital_cache(
                    self._valid_capital(compact), expected,
                    cache_age_seconds=1,
                    ttl_seconds=57600)["valid"])

    def test_valid_kline_cache_skips_subprocess(self):
        cached = self._valid_kline()
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=cached), \
             patch.object(sc, "run_script") as run:
            result = sc._fetch_kline(
                "600001.SH", as_of_date="2026-08-13")
        self.assertEqual(result, cached)
        run.assert_not_called()

    def test_wrong_date_capital_cache_invokes_fetcher(self):
        cached = self._valid_capital("20260812")
        refreshed = self._valid_capital("20260813")
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", side_effect=[cached, refreshed]), \
             patch.object(sc, "_cache_file_age_seconds", return_value=1), \
             patch.object(sc, "_cache_ttl_seconds", return_value=300), \
             patch.object(sc, "run_script",
                          return_value={"success": True}) as run:
            result = sc._fetch_capital_flow(
                "600001.SH", expected_trading_date="2026-08-13")
        self.assertEqual(result, refreshed)
        run.assert_called_once()

    def test_capital_subprocess_skips_optional_enrichment(self):
        refreshed = self._valid_capital("20260813")
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(sc, "CACHE_DIR", tmpdir), \
                patch.object(sc, "_read_json", side_effect=[None, refreshed]), \
                patch.object(sc, "run_script",
                             return_value={"success": True}) as run:
            sc._fetch_capital_flow(
                "600001.SH", expected_trading_date="2026-08-13")

        cmd = run.call_args.args[0]
        self.assertIn("--skip-extended", cmd)
        self.assertEqual(cmd[cmd.index("--expected-date") + 1], "2026-08-13")

    def test_invalid_cache_only_payload_is_diagnostic_and_non_actionable(self):
        capital = self._valid_capital("20260812")
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=capital), \
             patch.object(sc, "_cache_file_age_seconds", return_value=600), \
             patch.object(sc, "_cache_ttl_seconds", return_value=300), \
             patch.object(sc, "run_script") as run:
            diagnostic = sc._fetch_capital_flow(
                "600001.SH", cache_only=True,
                expected_trading_date="2026-08-13")

        run.assert_not_called()
        self.assertFalse(
            diagnostic["meta"]["cache_validation"]["valid"])
        quality = sc.assess_candidate_data(
            self._valid_kline(), diagnostic, self._valid_fundamental(),
            as_of_date="2026-08-13")
        self.assertFalse(quality["eligible"])
        self.assertIn("capital_error", quality["reasons"])

    def test_malformed_cache_only_payload_is_diagnostic_and_non_actionable(self):
        malformed = {"meta": [], "summary": "bad", "data": "bad"}
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=malformed), \
             patch.object(sc, "run_script") as run:
            diagnostic = sc._fetch_capital_flow(
                "600001.SH", cache_only=True,
                expected_trading_date="2026-08-13")

        run.assert_not_called()
        self.assertFalse(
            diagnostic["meta"]["cache_validation"]["valid"])
        quality = sc.assess_candidate_data(
            self._valid_kline(), diagnostic, self._valid_fundamental(),
            as_of_date="2026-08-13")
        self.assertFalse(quality["eligible"])

    def test_read_json_backfills_fetch_time_for_legacy_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "kline.json"
            path.write_text(json.dumps({
                "meta": {"data_source": "eastmoney"},
                "data": [{"trade_date": "20260806"}],
            }), encoding="utf-8")
            loaded = sc._read_json(path)
        self.assertTrue(loaded["meta"]["fetch_time"])

    def test_stale_kline_cache_is_refreshed_for_recommendation_date(self):
        stale = _make_dated_kline(trade_date="20260807")
        fresh = _make_dated_kline(trade_date="20260810")
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", side_effect=[stale, fresh]), \
             patch.object(sc, "run_script", return_value={"success": True}):
            result = sc._fetch_kline("600001.SH", as_of_date="2026-08-10")
        self.assertEqual(result["data"][-1]["trade_date"], "20260810")

    def test_valid_capital_cache_skips_subprocess(self):
        payload = {
            "meta": {"data_source": "eastmoney", "fetch_time": "20260812-160000"},
            "data": [{"date": "20260812", "main_net_inflow": 1}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=payload), \
             patch.object(sc, "run_script") as run:
            path = Path(tmpdir) / "600001"
            path.mkdir()
            cache_file = path / "capital_flow.json"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            result = sc._fetch_capital_flow("600001.SH")
        self.assertEqual(result, payload)
        run.assert_not_called()

    def test_error_fundamental_payload_degrades_but_keeps_retrying(self):
        candidates = [_make_candidate("600001"), _make_candidate("600002")]
        health = {}
        error_payload = {
            "meta": {"data_source": "akshare"},
            "summary": {"data_quality": "error"},
        }
        with patch.object(
                sc, "_fetch_kline",
                side_effect=lambda ts, **kwargs: _make_kline(60, ts)), \
             patch.object(sc, "_fetch_capital_flow", return_value=None), \
             patch.object(sc, "_fetch_fundamental",
                          return_value=error_payload):
            sc.run_phase2(
                candidates, enable_wyckoff=False,
                source_health=health, max_workers=1)
        # Two failures degrade the source (throttle + retry) but must not
        # hard-stop it: that would orphan the rest of the run to stale cache.
        self.assertEqual(health["fundamental"]["state"], "degraded")

    def test_error_fundamental_payload_hard_stops_after_many_failures(self):
        candidates = [
            _make_candidate(f"6000{i:02d}") for i in range(1, 10)]
        health = {}
        error_payload = {
            "meta": {"data_source": "akshare"},
            "summary": {"data_quality": "error"},
        }
        with patch.object(
                sc, "_fetch_kline",
                side_effect=lambda ts, **kwargs: _make_kline(60, ts)), \
             patch.object(sc, "_fetch_capital_flow", return_value=None), \
             patch.object(sc, "_fetch_fundamental",
                          return_value=error_payload):
            sc.run_phase2(
                candidates, enable_wyckoff=False,
                source_health=health, max_workers=1)
        # A genuinely dead source still hard-stops after the higher threshold.
        self.assertEqual(
            health["fundamental"]["state"], "unavailable")

    def test_error_kline_cache_does_not_skip_subprocess_refresh(self):
        cached = _make_dated_kline(trade_date="20260813")
        cached["meta"].update({
            "data_source": "error", "data_quality": "error",
            "error": "provider parse failed",
        })
        refreshed = _make_dated_kline(trade_date="20260813")
        refreshed["meta"]["data_source"] = "eastmoney"
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json",
                          side_effect=[cached, refreshed]), \
             patch.object(sc, "run_script",
                          return_value={"success": True}) as run:
            result = sc._fetch_kline(
                "600001.SH", as_of_date="2026-08-13")

        run.assert_called_once()
        self.assertEqual(result["meta"]["data_source"], "eastmoney")

    def test_error_fundamental_cache_does_not_skip_subprocess_refresh(self):
        cached = {
            "meta": {"data_source": "akshare"},
            "summary": {"data_quality": "error", "error": "empty frame"},
        }
        refreshed = {
            "meta": {"data_source": "akshare"},
            "summary": {"data_quality": "good"},
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json",
                          side_effect=[cached, refreshed]), \
             patch.object(sc, "_cache_file_is_fresh", return_value=True), \
             patch.object(sc, "run_script",
                          return_value={"success": True}) as run:
            result = sc._fetch_fundamental("600001.SH")

        run.assert_called_once()
        self.assertEqual(result["summary"]["data_quality"], "good")
        self.assertIn("--fast", run.call_args.args[0])

    def test_error_fundamental_refresh_preserves_provider_reason(self):
        error_payload = {
            "meta": {
                "data_source": "error",
                "provider_failures": [{
                    "provider": "eastmoney_quote", "reason": "dns",
                }],
            },
            "summary": {"data_quality": "error"},
            "errors": ["eastmoney_quote:dns"],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json",
                          side_effect=[None, error_payload]), \
             patch.object(sc, "_cache_file_is_fresh", return_value=False), \
             patch.object(sc, "run_script",
                          return_value={"success": True}):
            wrapped = sc._fetch_fundamental(
                "600001.SH", with_evidence=True)

        self.assertEqual(wrapped["live_attempt"]["reason"], "dns")

    def test_valid_fundamental_cache_skips_subprocess(self):
        cached = {
            "meta": {"data_source": "akshare"},
            "summary": {"data_quality": "good"},
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=cached), \
             patch.object(sc, "_cache_file_is_fresh", return_value=True), \
             patch.object(sc, "run_script") as run:
            result = sc._fetch_fundamental("600001.SH")

        self.assertEqual(result, cached)
        run.assert_not_called()


class TestGatherPerformance(unittest.TestCase):
    def test_supplied_sector_context_skips_ranking_request(self):
        stocks = [{
            "code": "600001", "name": "测试股份", "market_cap": 1e10,
            "change_pct": 1.0, "amount": 1e8, "pe": 20,
        }]
        context = {"BK0001": {"name": "测试板块", "hot_score": 88}}
        with patch.object(sd, "get_sector_rankings") as rankings, \
             patch.object(sd, "get_sector_stocks", return_value=stocks):
            result = sc.gather_candidates(
                ["BK0001"], sector_context=context)
        rankings.assert_not_called()
        self.assertEqual(result["candidates"][0]["sector_hot_score"], 88)

    def test_cache_only_result_does_not_reset_open_circuit(self):
        stocks = [{
            "code": "600001", "name": "测试股份", "market_cap": 1e10,
            "change_pct": 1.0, "amount": 1e8, "pe": 20,
            "membership_source": "cache", "membership_quality": "degraded",
        }]
        health = {"sector_membership": {
            "state": "unavailable", "failures": 2,
        }}
        with patch.object(sd, "get_sector_stocks") as live, \
             patch.object(sd, "get_sector_stocks_cached", return_value=stocks):
            sc.gather_candidates(
                ["BK0001"], sector_context={"BK0001": {}},
                source_health=health)
        live.assert_not_called()
        self.assertEqual(
            health["sector_membership"]["state"], "unavailable")

    def test_same_batch_duplicate_retains_complete_membership_evidence(self):
        shared = {
            "code": "600001", "name": "测试股份", "market_cap": 1e10,
            "change_pct": 1.0, "amount": 1e8, "pe": 20,
            "membership_source": "realtime",
            "membership_data_date": "2026-08-13",
            "membership_quality": "good",
        }
        context = {
            "BK1": {
                "name": "板块一", "hot_score": 70,
                "sector_score": 70, "sector_actionable": True,
                "persistence_score": 60, "relative_strength": 0.6,
                "ranking_position": 2, "ranking_source": "realtime",
                "ranking_data_date": "2026-08-13",
                "ranking_quality": "good",
            },
            "BK2": {
                "name": "板块二", "hot_score": 90,
                "sector_score": 90, "sector_actionable": True,
                "persistence_score": 80, "relative_strength": 1.1,
                "ranking_position": 1, "ranking_source": "realtime",
                "ranking_data_date": "2026-08-13",
                "ranking_quality": "good",
            },
        }
        with patch.object(
                sd, "get_sector_stocks",
                side_effect=lambda *_args, **_kwargs: [dict(shared)]):
            result = sc.gather_candidates(
                ["BK1", "BK2"], sector_context=context, max_workers=1)

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["sector_code"], "BK2")
        memberships = {m["code"]: m for m in candidate["sector_memberships"]}
        self.assertEqual(set(memberships), {"BK1", "BK2"})
        self.assertEqual(memberships["BK1"]["ranking_position"], 2)
        self.assertEqual(memberships["BK2"]["ranking_position"], 1)

    def test_fresh_actionable_membership_beats_stale_higher_score(self):
        memberships = [{
            "code": "BK1", "sector_actionable": True,
            "sector_score": 99, "membership_source": "cache",
            "membership_quality": "degraded",
        }, {
            "code": "BK2", "sector_actionable": True,
            "sector_score": 70, "membership_source": "realtime",
            "membership_quality": "good",
        }]

        primary = sc.select_primary_sector_membership(memberships)

        self.assertEqual(primary["code"], "BK2")

class TestSourceEvidenceAdapters(unittest.TestCase):
    def test_subprocess_timeout_is_capped_by_absolute_deadline(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sc, "CACHE_DIR", tmpdir), \
             patch.object(sc, "_read_json", return_value=None), \
             patch.object(sc, "run_script", return_value={
                 "success": False, "stderr": "Timeout"}) as run:
            sc._fetch_capital_flow(
                "600001.SH", with_evidence=True,
                live_deadline=sc.time.monotonic() + 0.05)

        timeout = run.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 0.05)
    def test_fetch_json_failure_reports_exact_attempts_and_reason(self):
        with patch.object(
                sd.urllib.request, "urlopen",
                side_effect=OSError("Name or service not known")), \
             patch.object(sd.time, "sleep"):
            with self.assertRaises(sd.ProviderFetchError) as raised:
                sd._fetch_json(
                    "https://push2.eastmoney.com/test", retries=2,
                    with_evidence=True)

        self.assertEqual(raised.exception.provider_attempts, 3)
        self.assertEqual(raised.exception.reason, "dns")

    def test_membership_cache_fallback_preserves_live_failure_evidence(self):
        stocks = [{"code": "600001", "name": "测试", "market_cap": 1e10}]
        error = sd.ProviderFetchError("dns", 2, "dns")
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "_fetch_json", side_effect=error):
            sd.save_sector_stocks_cache("BK1", stocks)
            wrapped = sd.get_sector_stocks("BK1", with_evidence=True)

        self.assertEqual(wrapped["payload"][0]["membership_source"], "cache")
        self.assertEqual(wrapped["live_attempt"]["provider_attempts"], 2)
        self.assertEqual(wrapped["live_attempt"]["reason"], "dns")
        self.assertTrue(wrapped["live_attempt"]["cache_used"])

    def test_gather_candidates_propagates_membership_fallback_evidence(self):
        stocks = [{
            "code": "600001", "name": "测试股份", "market_cap": 1e10,
            "change_pct": 1.0, "amount": 1e8, "pe": 20,
        }]
        wrapped = sd.source_result(stocks, sd.live_attempt(
            attempted=True, provider_attempts=2, reason="dns",
            cache_used=True, stale=True))
        with patch.object(sd, "get_sector_stocks", return_value=wrapped):
            result = sc.gather_candidates(
                ["BK0001"], sector_context={"BK0001": {}},
                source_health=sc.RunSourceHealth())

        evidence = result["candidates"][0]["membership_fetch_evidence"]
        self.assertEqual(evidence["reason"], "dns")
        self.assertEqual(evidence["provider_attempts"], 2)
        self.assertTrue(evidence["cache_used"])

    def test_membership_deadline_fallback_is_attached_to_cached_stock(self):
        stocks = [{
            "code": "600001", "name": "测试股份", "market_cap": 1e10,
            "change_pct": 1.0, "amount": 1e8, "pe": 20,
        }]
        health = sc.RunSourceHealth()
        health.live_deadline = sc.time.monotonic() - 1
        with patch.object(sd, "get_sector_stocks_cached",
                          return_value=stocks):
            result = sc.gather_candidates(
                ["BK0001"], sector_context={"BK0001": {}},
                source_health=health)

        candidate = result["candidates"][0]
        self.assertEqual(
            candidate["membership_fallback_reason"],
            "cache_only_deadline")
        self.assertEqual(
            candidate["membership_fetch_evidence"]["reason"], "deadline")

    def test_fast_fundamental_path_uses_quote_before_heavy_apis(self):
        quote = {
            "pe_ttm": 18.5, "pb": 1.7, "market_cap_billion": 123.4,
        }
        with patch.object(fd, "_fetch_em_quote_fallback",
                          return_value=quote) as fallback:
            result = fd.fetch_a_share_fundamentals_fast("600001")

        fallback.assert_called_once_with("600001", with_evidence=True)
        self.assertEqual(result["data_quality"], "partial")
        self.assertEqual(result["_data_source"], "eastmoney_quote")
        self.assertEqual(result["pe_ttm"], 18.5)

    def test_full_fundamental_mode_does_not_reuse_fast_cache(self):
        fast_payload = {
            "meta": {"data_source": "eastmoney_quote", "fetch_mode": "fast"},
            "summary": {"data_quality": "partial", "pe_ttm": 18.5},
        }
        with patch.object(fd, "load_cache",
                          side_effect=[fast_payload, None]) as load, \
             patch.object(fd, "fetch_a_share_fundamentals",
                          return_value={"data_quality": "good"}) as full, \
             patch.object(fd, "output_json"), \
             patch.object(fd, "save_cache"), \
             patch.object(sys, "argv", ["fundamental.py", "600001.SH"]):
            fd.main()

        full.assert_called_once_with("600001")
        self.assertEqual(
            load.call_args_list[0].args[0], "fundamental_600001.SH_full")
        self.assertEqual(
            load.call_args_list[1].args[0], "fundamental_600001.SH")

    def test_fast_fundamental_preserves_provider_failure_evidence(self):
        with patch.object(fd, "_fetch_em_quote_fallback",
                          side_effect=RuntimeError("DNS lookup failed")), \
             patch.object(fd, "fetch_a_share_fundamentals_tushare",
                          side_effect=RuntimeError("provider timeout")):
            result = fd.fetch_a_share_fundamentals_fast("600001")

        self.assertEqual(result["data_quality"], "error")
        self.assertEqual(
            [failure["reason"] for failure in result["_provider_failures"]],
            ["dns", "timeout"],
        )
        self.assertIn("eastmoney_quote:dns", result["_errors"])
        self.assertIn("tushare_daily_basic:timeout", result["_errors"])

    def test_partial_quote_fundamental_score_uses_available_valuation(self):
        low = {"summary": {"data_quality": "partial", "pe_ttm": 5, "pb": 1}}
        high = {"summary": {"data_quality": "partial", "pe_ttm": 500, "pb": 20}}

        self.assertGreater(
            sc.score_fundamental_quick(_make_candidate(), low),
            sc.score_fundamental_quick(_make_candidate(), high),
        )

    def test_run_phase2_keeps_per_stock_source_evidence(self):
        candidate = _make_candidate("600001")
        kline = _make_kline(60, candidate["ts_code"])
        fundamental = {
            "meta": {"data_source": "error"},
            "summary": {"data_quality": "error"},
        }

        def evidenced(fetch_payload, attempt):
            return lambda *args, with_evidence=False, **kwargs: (
                sc.source_result(fetch_payload, attempt)
                if with_evidence else fetch_payload
            )

        with patch.object(sc, "_fetch_kline", side_effect=evidenced(
                kline, sc.live_attempt(attempted=True, provider_attempts=1))), \
             patch.object(sc, "_fetch_capital_flow", side_effect=evidenced(
                 None, sc.live_attempt(attempted=True, provider_attempts=1,
                                      reason="timeout"))), \
             patch.object(sc, "_fetch_fundamental", side_effect=evidenced(
                 fundamental, sc.live_attempt(
                     attempted=True, provider_attempts=1, reason="timeout"))):
            item = sc.run_phase2(
                [candidate], max_workers=1,
                source_health=sc.RunSourceHealth())[0]

        self.assertEqual(
            item["source_evidence"]["fundamental"]["reason"], "timeout")
        self.assertEqual(
            item["source_evidence"]["capital"]["reason"], "timeout")

    def test_same_day_live_membership_pe_is_safe_fundamental_fallback(self):
        candidate = _make_candidate("600001")
        candidate.update({
            "pe": 18.0,
            "membership_source": "realtime",
            "membership_quality": "good",
            "membership_data_date": "2026-08-06",
        })
        capital = {
            "meta": {"data_source": "eastmoney"},
            "data": [{"date": "20260806", "main_net_inflow": 1}],
        }
        error_fundamental = {
            "meta": {"data_source": "error"},
            "summary": {"data_quality": "error"},
        }
        with patch.object(sc, "_fetch_kline",
                          return_value=_make_dated_kline(
                              60, candidate["ts_code"], "20260806")), \
             patch.object(sc, "_fetch_capital_flow", return_value=capital), \
             patch.object(sc, "_fetch_fundamental",
                          return_value=error_fundamental):
            item = sc.run_phase2(
                [candidate], as_of_date="2026-08-06", max_workers=1)[0]

        fundamental = item["data_quality"]["dimensions"]["fundamental"]
        self.assertTrue(fundamental["available"])
        self.assertEqual(fundamental["quality"], "partial")
        self.assertEqual(fundamental["source"], "sector_membership_quote")
        self.assertNotIn("fundamental_error", item["data_quality"]["reasons"])
        self.assertEqual(
            item["source_evidence"]["fundamental"]["fallback_source"],
            "sector_membership_quote")

    def test_ranking_adapter_aggregates_exact_provider_attempts(self):
        rows = [{
            "f12": f"BK{i}", "f14": f"板块{i}", "f3": 1,
            "f104": 3, "f105": 2,
        } for i in range(5)]
        payload = {"rc": 0, "data": {"diff": rows}}
        attempts = [
            sc.source_result(payload, sc.live_attempt(
                attempted=True, provider_attempts=2)),
            sc.source_result(payload, sc.live_attempt(
                attempted=True, provider_attempts=1)),
        ]
        with patch.object(sd, "_fetch_json", side_effect=attempts), \
             patch.object(sd.time, "sleep"):
            wrapped = sd.get_sector_rankings(with_evidence=True)

        self.assertEqual(wrapped["live_attempt"]["provider_attempts"], 3)
        self.assertEqual(wrapped["live_attempt"]["reason"], "")


class TestSectorConstituentFallback(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "rc": 0,
            "data": {"diff": [{
                "f12": "600001", "f14": "测试股份", "f3": 1.2,
                "f8": 2e8, "f20": 1e10, "f37": 20,
            }]},
        }

    def test_live_sector_stocks_are_cached_with_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "_fetch_json", return_value=self.payload):
            stocks = sd.get_sector_stocks("BK0001")
            cache_file = Path(tmpdir) / "BK0001.json"
            self.assertTrue(cache_file.exists())

        self.assertEqual(stocks[0]["membership_source"], "realtime")
        self.assertEqual(stocks[0]["membership_quality"], "good")

    def test_live_sector_cache_persists_refresh_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "_fetch_json", return_value=self.payload):
            sd.get_sector_stocks("BK0001")
            cache = json.loads(
                (Path(tmpdir) / "BK0001.json").read_text(encoding="utf-8"))

        self.assertEqual(cache["schema_version"], 2)
        self.assertEqual(cache["provider"], "eastmoney")
        self.assertEqual(cache["data_date"], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(cache["fetched_at"], cache["cached_at"])

    def test_tagged_sector_cache_prefers_explicit_data_date(self):
        cached_at = datetime.now().isoformat()
        payload = {
            "schema_version": 2,
            "cached_at": cached_at,
            "fetched_at": cached_at,
            "data_date": "2026-08-26",
            "provider": "eastmoney",
            "stocks": [{"code": "600001", "name": "测试股份"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)):
            (Path(tmpdir) / "BK0001.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stocks = sd.get_sector_stocks_cached("BK0001")

        self.assertEqual(stocks[0]["membership_data_date"], "2026-08-26")

    def test_sector_code_cannot_escape_cache_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR",
                          Path(tmpdir) / "sector_stocks"), \
             patch.object(sd, "_fetch_json") as fetch:
            escaped = Path(tmpdir) / "escaped.json"
            with self.assertRaisesRegex(ValueError, "invalid sector code"):
                sd.save_sector_stocks_cache(
                    "../escaped", [{"code": "600001"}])
            with self.assertRaisesRegex(ValueError, "invalid sector code"):
                sd.load_sector_stocks_cache("../escaped")
            with self.assertRaisesRegex(ValueError, "invalid sector code"):
                sd.get_sector_stocks("../escaped")

            self.assertFalse(escaped.exists())
            fetch.assert_not_called()

    def test_sector_stocks_fall_back_to_snapshot_after_live_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)):
            with patch.object(sd, "_fetch_json", return_value=self.payload):
                sd.get_sector_stocks("BK0001")
            with patch.object(
                    sd, "_fetch_json", side_effect=RuntimeError("dns")):
                stocks = sd.get_sector_stocks("BK0001")

        self.assertEqual(stocks[0]["membership_source"], "cache")
        self.assertEqual(stocks[0]["membership_quality"], "degraded")
        self.assertTrue(stocks[0]["membership_data_date"])
        self.assertEqual(
            stocks[0]["membership_fallback_reason"], "dns")
        self.assertIn("membership_cache_age_hours", stocks[0])

    def test_empty_live_sector_stocks_fall_back_to_snapshot(self):
        empty_payload = {"rc": 0, "data": {"diff": []}}
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)):
            with patch.object(sd, "_fetch_json", return_value=self.payload):
                sd.get_sector_stocks("BK0001")
            with patch.object(sd, "_fetch_json", return_value=empty_payload):
                stocks = sd.get_sector_stocks("BK0001")

        self.assertEqual(stocks[0]["membership_source"], "cache")
        self.assertEqual(stocks[0]["membership_quality"], "degraded")

    def test_default_sector_stocks_keeps_provider_attempt_count(self):
        fetched = sc.source_result(
            self.payload,
            sc.live_attempt(attempted=True, provider_attempts=2),
        )
        with patch.object(sd, "_fetch_json", return_value=fetched):
            stocks = sd.get_sector_stocks("BK0001")

        self.assertEqual(stocks[0]["membership_source"], "realtime")
        self.assertEqual(stocks[0]["membership_provider_attempts"], 2)

    def test_empty_live_sector_stocks_without_snapshot_raise(self):
        empty_payload = {"rc": 0, "data": {"diff": []}}
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "_fetch_json", return_value=empty_payload):
            with self.assertRaisesRegex(RuntimeError, "无有效成分股且无可用快照"):
                sd.get_sector_stocks("BK0001")

    def test_invalid_live_sector_rows_without_snapshot_raise(self):
        invalid_payload = {
            "rc": 0,
            "data": {"diff": [{"f12": "", "f14": "无代码"}]},
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "_fetch_json", return_value=invalid_payload):
            with self.assertRaisesRegex(RuntimeError, "无有效成分股且无可用快照"):
                sd.get_sector_stocks("BK0001")

    def test_live_snapshot_write_failure_is_exposed(self):
        with patch.object(sd, "_fetch_json", return_value=self.payload), \
             patch.object(sd, "save_sector_stocks_cache",
                          side_effect=OSError("disk full")):
            stocks = sd.get_sector_stocks("BK0001")

        self.assertEqual(stocks[0]["membership_source"], "realtime")
        self.assertEqual(stocks[0]["membership_quality"], "partial")
        self.assertIn("disk full", stocks[0]["membership_cache_error"])

    def test_sector_stocks_cache_accepts_boundary_and_rejects_expired(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 7, 12, 0, 0)

        payload = {
            "cached_at": "2026-07-08T12:00:00",
            "stocks": [{"code": "600001", "name": "测试股份"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
             patch.object(sd, "datetime", FrozenDateTime):
            path = Path(tmpdir) / "BK0001.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertIsNotNone(sd.load_sector_stocks_cache("BK0001"))

            payload["cached_at"] = "2026-07-08T11:59:59"
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(sd.load_sector_stocks_cache("BK0001"))


class TestGatePass(unittest.TestCase):
    def test_accumulation_buy_points_pass(self):
        for sub in ("spring", "lps", "secondary_test", "pre_markup"):
            with self.subTest(sub=sub):
                self.assertTrue(sc.wyckoff_gate_pass(_wk(sub=sub, conf=0.6)))

    def test_markup_buy_points_pass(self):
        for sub in ("jac",):
            with self.subTest(sub=sub):
                self.assertTrue(sc.wyckoff_gate_pass(_wk(phase="markup", sub=sub, conf=0.5)))

    def test_markup_bu_candidate_is_observation_only(self):
        wk = _wk(phase="markup", sub="backup", conf=0.7)
        wk["signal"] = {"status": "candidate", "age_bars": 0, "event": "bu"}
        self.assertFalse(sc.wyckoff_gate_pass(wk))

    def test_confirmed_markup_lps_is_a_buy_point_but_candidate_is_not(self):
        wk = _wk(phase="markup", sub="lps", conf=0.7, score=2.0)
        wk["signal"] = {"status": "confirmed", "age_bars": 1, "event": "lps"}
        self.assertTrue(sc.wyckoff_gate_pass(wk))
        wk["signal"]["status"] = "candidate"
        self.assertFalse(sc.wyckoff_gate_pass(wk))

    def test_distribution_markdown_rejected(self):
        self.assertFalse(sc.wyckoff_gate_pass(_wk(phase="distribution", sub="lpsy", conf=0.7, score=-2.0)))
        self.assertFalse(sc.wyckoff_gate_pass(_wk(phase="markdown", sub="breakdown", conf=0.7, score=-2.5)))

    def test_non_buy_subphase_rejected(self):
        self.assertFalse(sc.wyckoff_gate_pass(_wk(sub="selling_climax", conf=0.6)))
        self.assertFalse(sc.wyckoff_gate_pass(_wk(phase="markup", sub="continuation", conf=0.6)))

    def test_low_confidence_rejected(self):
        self.assertFalse(sc.wyckoff_gate_pass(_wk(sub="lps", conf=0.2)))

    def test_candidate_and_stale_signals_rejected(self):
        candidate = _wk(phase="markup", sub="jac", conf=0.8)
        candidate["signal"] = {"status": "candidate", "age_bars": 0}
        self.assertFalse(sc.wyckoff_gate_pass(candidate))
        stale = _wk(phase="markup", sub="jac", conf=0.8)
        stale["signal"] = {"status": "confirmed", "age_bars": 9}
        self.assertFalse(sc.wyckoff_gate_pass(stale))

    def test_none_and_unknown(self):
        self.assertFalse(sc.wyckoff_gate_pass(None))
        self.assertFalse(sc.wyckoff_gate_pass(_wk(phase="phase_unknown", sub="", conf=0.3)))


class TestScoreWyckoff(unittest.TestCase):
    def test_base_normalized(self):
        self.assertAlmostEqual(sc.score_wyckoff(_wk(sub="lps", score=2.0, conf=0.6)),
                               88.3333, places=3)  # 83.33 + 5 bonus
        self.assertAlmostEqual(sc.score_wyckoff(_wk(sub="lps", score=2.0, conf=0.4)),
                               83.3333, places=3)  # conf<0.5 → no bonus
        self.assertAlmostEqual(sc.score_wyckoff(_wk(phase="distribution", sub="lpsy", score=-2.0, conf=0.7)),
                               16.6667, places=3)

    def test_none_neutral(self):
        self.assertEqual(sc.score_wyckoff(None), 50.0)


class TestRunPhase2Funnel(unittest.TestCase):
    def test_expected_trading_date_reaches_all_cache_validators(self):
        seen = {"kline": [], "capital": [], "fundamental": []}

        def fetch_kline(ts, as_of_date="", cache_only=False):
            seen["kline"].append(as_of_date)
            return _make_dated_kline(60, ts, "20260813")

        def fetch_capital(ts, cache_only=False, expected_trading_date=""):
            seen["capital"].append(expected_trading_date)
            return {
                "meta": {"data_source": "eastmoney"},
                "data": [{"date": "20260813", "main_net_inflow": 1}],
            }

        def fetch_fundamental(ts, cache_only=False,
                              expected_trading_date=""):
            seen["fundamental"].append(expected_trading_date)
            return {
                "meta": {"data_source": "akshare",
                         "fetch_time": "20260813-160000"},
                "summary": {"data_quality": "good"},
            }

        with patch.object(sc, "_fetch_kline", side_effect=fetch_kline), \
             patch.object(sc, "_fetch_capital_flow", side_effect=fetch_capital), \
             patch.object(sc, "_fetch_fundamental",
                          side_effect=fetch_fundamental):
            sc.run_phase2(
                [_make_candidate()], as_of_date="2026-08-13",
                max_workers=1)

        self.assertEqual(seen["kline"], ["2026-08-13"])
        self.assertEqual(seen["capital"], ["2026-08-13"])
        self.assertEqual(seen["fundamental"], ["2026-08-13"])

    def setUp(self):
        self.orig_kline = sc._fetch_kline
        self.orig_cap = sc._fetch_capital_flow
        self.orig_fund = sc._fetch_fundamental
        self.orig_analyze = sc.analyze_kline_dict
        sc._fetch_capital_flow = lambda ts: None
        sc._fetch_fundamental = lambda ts: None

    def tearDown(self):
        sc._fetch_kline = self.orig_kline
        sc._fetch_capital_flow = self.orig_cap
        sc._fetch_fundamental = self.orig_fund
        sc.analyze_kline_dict = self.orig_analyze

    def test_funnel_keeps_buy_point_drops_others(self):
        wk_map = {
            "600001.SH": _wk(sub="lps", conf=0.6, score=2.0),          # 吸筹 LPS → 留
            "600002.SH": _wk(phase="distribution", sub="lpsy",          # 派发 LPSY → 弃
                             conf=0.7, score=-2.0),
        }
        sc.analyze_kline_dict = lambda kline: wk_map.get(kline["meta"]["ts_code"], _wk())

        def fake_kline(ts_code, as_of_date=""):
            n = 60 if ts_code in wk_map else 20   # 600003 数据不足 → 弃
            return _make_kline(n, ts_code)

        sc._fetch_kline = fake_kline

        candidates = [_make_candidate("600001"), _make_candidate("600002"),
                      _make_candidate("600003")]
        scored = sc.run_phase2(candidates, enable_wyckoff=True)

        self.assertEqual([s["code"] for s in scored], ["600001"])
        item = scored[0]
        self.assertAlmostEqual(item["dimensions"]["wyckoff"], 88.3, delta=0.1)
        self.assertEqual(item["wyckoff"]["sub_phase"], "子阶段:lps")
        self.assertEqual(item["wyckoff"]["confidence"], 0.6)
        self.assertEqual(item["wyckoff"]["alignment"]["recommendation_gate"], "actionable")
        self.assertEqual(item["wyckoff"]["long_term"]["phase"], "accumulation")
        # 复合分重配后包含 wyckoff 权重
        self.assertGreater(item["composite_score"], 50.0)

    def test_expensive_dimensions_only_fetch_for_wyckoff_passes(self):
        candidates = [_make_candidate("600001"), _make_candidate("600002")]
        sc._fetch_kline = lambda ts, as_of_date="", cache_only=False: _make_kline(60, ts)
        sc.analyze_kline_dict = lambda kline: (
            _wk(sub="lps", conf=0.6) if
            kline["meta"]["ts_code"] == "600001.SH" else
            _wk(phase="distribution", sub="lpsy", conf=0.7)
        )
        capital_calls = []
        fundamental_calls = []
        sc._fetch_capital_flow = lambda ts, cache_only=False: capital_calls.append(ts)
        sc._fetch_fundamental = lambda ts, cache_only=False: fundamental_calls.append(ts)

        result = sc.run_phase2(candidates, enable_wyckoff=True)

        self.assertEqual([item["code"] for item in result], ["600001"])
        self.assertEqual(capital_calls, ["600001.SH"])
        self.assertEqual(fundamental_calls, ["600001.SH"])

    def test_no_wyckoff_dim_when_disabled(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_kline(60, ts)
        scored = sc.run_phase2([_make_candidate("600001")], enable_wyckoff=False)
        self.assertEqual(len(scored), 1)
        self.assertNotIn("wyckoff", scored[0]["dimensions"])
        self.assertNotIn("wyckoff", scored[0])

    def test_trade_plan_source_survives_scanner_and_only_resistance_is_complete(self):
        resistance_plan = {"action": "buy", "target_source": "resistance"}
        atr_plan = {"action": "wait", "target_source": "atr_projection"}
        candidates = [_make_candidate("600001"), _make_candidate("600002")]
        sc._fetch_kline = lambda ts, as_of_date="": _make_kline(60, ts)
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        with patch.object(sc, "build_candidate_trade_plan",
                          side_effect=[resistance_plan, atr_plan]), \
             patch.object(sc, "validate_trade_plan", side_effect=[
                 {"complete": True, "reasons": []},
                 {"complete": False, "reasons": [
                     "trade_plan_target_source_not_executable"]},
             ]):
            scored = sc.run_phase2(
                candidates, enable_wyckoff=False,
                trade_plan_policy={"mode": "actionable"})

        self.assertEqual(
            [item["trade_plan_target_source"] for item in scored],
            ["resistance", "atr_projection"],
        )
        self.assertEqual(scored[0]["trade_plan_status"], "complete")
        self.assertEqual(scored[1]["trade_plan_status"], "incomplete")

    def test_composite_uses_raw_dimension_values_before_rounding(self):
        sc._fetch_kline = lambda ts, as_of_date="": _make_kline(60, ts)
        raw = {
            "momentum": 60.0,
            "volume_price": 50.0,
            "capital": 40.0,
            "fundamental": 30.0,
            "sector_strength": 19.66,
        }
        with patch.object(sc, "score_momentum", return_value=raw["momentum"]), \
             patch.object(sc, "score_volume_price",
                          return_value=raw["volume_price"]), \
             patch.object(sc, "score_capital", return_value=raw["capital"]), \
             patch.object(sc, "score_fundamental_quick",
                          return_value=raw["fundamental"]), \
             patch.object(sc, "score_sector_membership",
                          return_value=raw["sector_strength"]):
            item = sc.run_phase2(
                [_make_candidate("600001")], enable_wyckoff=False)[0]

        expected = round(
            raw["momentum"] * 0.30 + raw["volume_price"] * 0.20
            + raw["capital"] * 0.20 + raw["fundamental"] * 0.15
            + raw["sector_strength"] * 0.15,
            1,
        )
        self.assertEqual(item["composite_score"], expected)
        self.assertEqual(item["dimensions"]["momentum"], 60.0)

    def test_quality_adjusted_score_is_separate_from_raw_score(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(60, ts)
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }
        candidate = _make_candidate("600001")
        candidate["membership_data_date"] = "2026-08-06"
        baseline = sc.run_phase2(
            [candidate], enable_wyckoff=True)[0]
        assessed = sc.run_phase2(
            [candidate],
            enable_wyckoff=True,
            as_of_date="2026-08-06",
        )[0]
        self.assertEqual(assessed["composite_score"], baseline["composite_score"])
        self.assertEqual(
            assessed["raw_composite_score"], assessed["composite_score"])
        self.assertEqual(
            assessed["quality_adjusted_score"],
            round(assessed["raw_composite_score"] * 0.8, 1),
        )
        self.assertTrue(assessed["data_quality"]["eligible"])
        self.assertEqual(assessed["data_quality"]["coverage"], 0.8)

    def test_stale_kline_remains_observable_but_not_eligible(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(
            60, ts, trade_date="20260805")
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }
        result = sc.run_phase2(
            [_make_candidate("600001")],
            enable_wyckoff=True,
            as_of_date="2026-08-06",
        )
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["data_quality"]["eligible"])
        self.assertIn("kline_stale", result[0]["data_quality"]["reasons"])
        self.assertLess(
            result[0]["quality_adjusted_score"],
            result[0]["raw_composite_score"],
        )

    def test_cached_sector_membership_is_observation_only(self):
        candidate = _make_candidate("600001")
        candidate.update({
            "membership_source": "cache",
            "membership_quality": "degraded",
            "membership_data_date": "2026-08-05",
        })
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(60, ts)
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }

        result = sc.run_phase2(
            [candidate], enable_wyckoff=True, as_of_date="2026-08-06")

        self.assertFalse(result[0]["data_quality"]["eligible"])
        self.assertIn(
            "sector_membership_stale",
            result[0]["data_quality"]["reasons"],
        )
        self.assertEqual(result[0]["membership_source"], "cache")
        self.assertLess(
            result[0]["quality_adjusted_score"],
            result[0]["raw_composite_score"],
        )

    def test_realtime_membership_with_wrong_date_is_observation_only(self):
        candidate = _make_candidate("600001")
        candidate.update({
            "membership_source": "realtime",
            "membership_quality": "good",
            "membership_data_date": "2026-08-05",
        })
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(60, ts)
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }

        item = sc.run_phase2(
            [candidate], enable_wyckoff=False,
            as_of_date="2026-08-06")[0]

        self.assertFalse(item["data_quality"]["eligible"])
        self.assertIn(
            "sector_membership_stale", item["data_quality"]["reasons"])

    def test_snapshot_write_failure_has_distinct_observation_reason(self):
        candidate = _make_candidate("600001")
        candidate.update({
            "membership_source": "realtime",
            "membership_quality": "partial",
            "membership_data_date": "2026-08-06",
            "membership_cache_error": "disk full",
        })
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(60, ts)
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }

        result = sc.run_phase2(
            [candidate], enable_wyckoff=True, as_of_date="2026-08-06")

        self.assertIn(
            "sector_membership_cache_write_failed",
            result[0]["data_quality"]["reasons"],
        )
        self.assertEqual(result[0]["membership_cache_error"], "disk full")


class TestFilters(unittest.TestCase):
    def test_counterargument_is_stable_and_non_empty(self):
        self.assertEqual(
            sc._candidate_counterargument({"warnings": []}),
            "若量价确认失败或收盘跌破结构支撑，则交易逻辑失效",
        )
        self.assertEqual(
            sc._candidate_counterargument({"warnings": ["风险A", "风险B"]}),
            "风险A；风险B",
        )

    def test_is_a_share(self):
        for ok in ("600519", "000001", "300750", "688001", "601166"):
            with self.subTest(code=ok):
                self.assertTrue(sc._is_a_share(ok))
        for bad in ("513180", "159740", "00700", "00001", "", None):
            with self.subTest(code=bad):
                self.assertFalse(sc._is_a_share(bad))

    def test_is_st(self):
        self.assertTrue(sc._is_st("ST某某"))
        self.assertTrue(sc._is_st("*ST某某"))
        self.assertTrue(sc._is_st("某某退"))
        self.assertFalse(sc._is_st("某某股份"))
        self.assertFalse(sc._is_st(None))


def run_stock_scanner_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_stock_scanner_tests()
    raise SystemExit(1 if failed else 0)
