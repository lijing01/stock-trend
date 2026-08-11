#!/usr/bin/env python3
"""scores.py --mode wyckoff (P0-3) 独立 100 分制测试套件.

覆盖:
  - run_wyckoff_mode: 分数公式 阶段70%+VSA20%+置信度10%,verdict,买点判定,输出文件
  - data_quality limited/insufficient 降级
  - 非买点子阶段判定
  - error/missing wyckoff.json → SystemExit
  - _to_float 安全解析

Usage:
    python3 test_scores_wyckoff_mode.py [-v]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import scores as sc


def _wy_fixture(score_3=2.0, phase="accumulation", sub="lps", conf=0.6,
                quality="good", vsa=None, err=None):
    """Fabricated wyckoff.json (same shape as wyckoff.py analyze output)."""
    if err is not None:
        return {"meta": {"error": err}, "phase": {"primary": "phase_unknown"}}
    if vsa is None:
        vsa = [
            {"description": "缩量止跌", "strength": 3, "bar_index": 0},
            {"description": "上冲受阻", "strength": 2, "bar_index": 1},
        ]
    return {
        "meta": {"ts_code": "600001.SH", "name": "测试", "data_quality": quality,
                 "calc_date": "20260801", "kline_days": 250},
        "phase": {"primary": phase, "primary_name": "吸筹阶段",
                  "primary_sub_phase": sub, "sub_phase_name": f"子阶段:{sub}",
                  "confidence": conf},
        "range": {"support": 9.0, "resistance": 11.0},
        "vsa_signals": vsa,
        "cause_effect": {"horizontal_count": 30, "vertical_height": 2.0,
                         "targets": [], "time_projection_days": 15},
        "wyckoff_score": score_3,
        "wyckoff_signals": {"verdict": "bullish", "key_signals": [],
                            "trading_implication": "做好突破入场准备。"},
    }


class TestRunWyckoffMode(unittest.TestCase):
    def _write(self, tmp, data, name="wyckoff.json"):
        p = Path(tmp) / name
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return str(p)

    def test_lps_buy_point_strong(self):
        # LPS 2.0, conf 0.6, VSA [3,2] → 0.7*83.33+0.2*83.33+0.1*60 = 81.0
        with tempfile.TemporaryDirectory() as tmp:
            wy_path = self._write(tmp, _wy_fixture())
            ns = argparse.Namespace(wyckoff_data=wy_path,
                                    output=str(Path(tmp) / "out.json"), code=None)
            out_path, out = sc.run_wyckoff_mode(ns, None)

            self.assertAlmostEqual(out["score_100"], 81.0, delta=0.2)
            self.assertEqual(out["verdict"], "强势买点")
            self.assertTrue(out["is_buy_point"])
            self.assertAlmostEqual(out["components"]["phase_score"], 83.3, delta=0.1)
            self.assertAlmostEqual(out["components"]["vsa_score"], 83.3, delta=0.1)
            self.assertAlmostEqual(out["components"]["confidence_score"], 60.0, delta=0.1)
            self.assertTrue(Path(out_path).exists())

    def test_limited_quality_discount(self):
        # limited → 阶段分×0.9; VSA 空→50; conf 缺→50
        # score 1.5 → phase_norm=75 ×0.9=67.5 → 0.7*67.5+0.2*50+0.1*50=62.25
        with tempfile.TemporaryDirectory() as tmp:
            wy_path = self._write(tmp, _wy_fixture(score_3=1.5, conf=None,
                                                   quality="limited", vsa=[]))
            ns = argparse.Namespace(wyckoff_data=wy_path,
                                    output=str(Path(tmp) / "out.json"), code=None)
            _, out = sc.run_wyckoff_mode(ns, None)

            self.assertAlmostEqual(out["score_100"], 62.2, delta=0.2)
            self.assertEqual(out["verdict"], "买点候选")  # 55-70 + 买点
            self.assertAlmostEqual(out["components"]["vsa_score"], 50.0, delta=0.1)
            self.assertAlmostEqual(out["components"]["confidence_score"], 50.0, delta=0.1)

    def test_distribution_not_buy_point(self):
        # LPSY 派发 → 非买点; score_100 < 40 → 空头
        with tempfile.TemporaryDirectory() as tmp:
            wy_path = self._write(tmp, _wy_fixture(
                score_3=-2.0, phase="distribution", sub="lpsy", conf=0.7))
            ns = argparse.Namespace(wyckoff_data=wy_path,
                                    output=str(Path(tmp) / "out.json"), code=None)
            _, out = sc.run_wyckoff_mode(ns, None)

            self.assertFalse(out["is_buy_point"])
            self.assertEqual(out["verdict"], "空头")
            self.assertLess(out["score_100"], 40.0)

    def test_insufficient_neutral(self):
        # insufficient → 阶段分强制 50
        with tempfile.TemporaryDirectory() as tmp:
            wy_path = self._write(tmp, _wy_fixture(
                score_3=2.0, quality="insufficient", vsa=[]))
            ns = argparse.Namespace(wyckoff_data=wy_path,
                                    output=str(Path(tmp) / "out.json"), code=None)
            _, out = sc.run_wyckoff_mode(ns, None)
            self.assertAlmostEqual(out["components"]["phase_score"], 50.0, delta=0.1)

    def test_error_meta_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            wy_path = self._write(tmp, _wy_fixture(err="bad data"))
            ns = argparse.Namespace(wyckoff_data=wy_path,
                                    output=str(Path(tmp) / "out.json"), code=None)
            with self.assertRaises(SystemExit):
                sc.run_wyckoff_mode(ns, None)

    def test_missing_file_exits(self):
        ns = argparse.Namespace(wyckoff_data="/nonexistent/wyckoff.json",
                                output="/tmp/out.json", code=None)
        with self.assertRaises(SystemExit):
            sc.run_wyckoff_mode(ns, None)

    def test_composite_preserves_technical_quality(self):
        """维科夫样本不足不能覆盖技术指标的数据质量。"""
        technical = {
            "summary": {
                "total_score": 1.0,
                "direction": "偏多",
                "confidence": 0.7,
                "data_quality": "good",
            },
            "latest": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tech_path = self._write(tmp, technical, "technical.json")
            wy_path = self._write(tmp, _wy_fixture(quality="limited"))
            out_path = str(Path(tmp) / "scores.json")
            result = subprocess.run(
                [sys.executable, str(Path(sc.__file__)),
                 "--technical", tech_path, "--wyckoff-data", wy_path,
                 "-o", out_path],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out_path, encoding="utf-8") as f:
                output = json.load(f)

            self.assertEqual(output["data_quality"], "good")
            self.assertEqual(output["wyckoff_data_quality"], "limited")
            self.assertEqual(output["automated_sources"]["wyckoff"], "limited")


class TestToFloat(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sc._to_float(3), 3.0)
        self.assertEqual(sc._to_float("2.5"), 2.5)
        self.assertEqual(sc._to_float(None), 0.0)
        self.assertEqual(sc._to_float("abc", 1.0), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
