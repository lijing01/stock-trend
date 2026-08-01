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
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans import stock_scanner as sc


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
    }


def _make_candidate(code="600001"):
    return {
        "code": code, "ts_code": f"{code}.SH", "name": f"测试{code}",
        "sector_code": "BK0477", "sector_name": "测试板块",
        "sector_hot_score": 80, "change_pct": 1.0, "amount": 1e8,
        "market_cap": 1e10, "pe": 20.0,
    }


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

        def fake_kline(ts_code):
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
        # 复合分重配后包含 wyckoff 权重
        self.assertGreater(item["composite_score"], 50.0)

    def test_no_wyckoff_dim_when_disabled(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts: _make_kline(60, ts)
        scored = sc.run_phase2([_make_candidate("600001")], enable_wyckoff=False)
        self.assertEqual(len(scored), 1)
        self.assertNotIn("wyckoff", scored[0]["dimensions"])
        self.assertNotIn("wyckoff", scored[0])


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


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
