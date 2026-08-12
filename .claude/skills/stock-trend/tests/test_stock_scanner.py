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

    def test_error_fundamental_payload_opens_circuit(self):
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
        self.assertEqual(health["fundamental"]["state"], "unavailable")


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
        for sub in ("jac", "backup"):
            with self.subTest(sub=sub):
                self.assertTrue(sc.wyckoff_gate_pass(_wk(phase="markup", sub=sub, conf=0.5)))

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

    def test_quality_adjusted_score_is_separate_from_raw_score(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts, as_of_date="": _make_dated_kline(60, ts)
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }
        baseline = sc.run_phase2(
            [_make_candidate("600001")], enable_wyckoff=True)[0]
        assessed = sc.run_phase2(
            [_make_candidate("600001")],
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
