"""Focused offline tests for YAML observation analysis and artifact isolation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import observation_list_analysis as observation
from scans import stock_scanner


class ObservationAnalysisTests(unittest.TestCase):
    def test_yaml_order_bad_rows_and_scanner_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "observation.yaml"
            yaml_path.write_text(
                "observation_list:\n"
                "  - {code: '600519', date: '2026-09-21', entry_phase: 吸筹}\n"
                "  - {code: '000001', date: '2026-09-20', entry_phase: 拉升}\n"
                "  - {code: '600519', date: '2026-09-19', entry_phase: 未记录}\n"
                "  - {code: '60336', date: '2026-09-19', entry_phase: 未记录}\n",
                encoding="utf-8")
            calls = []

            def builder(codes, data_date):
                self.assertEqual(codes, ["600519", "000001"])
                self.assertEqual(data_date, "2026-09-22")
                return {
                    code: {"candidate": {"code": code, "ts_code": code + ".SH",
                                          "name": code, "sector_code": "BK1"},
                           "sector_status": "ready"}
                    for code in codes
                }

            def analyzer(candidates, **kwargs):
                calls.append(kwargs)
                code = candidates[0]["code"]
                if code == "000001":
                    raise RuntimeError("provider failed")
                dimensions = {key: 60.0 for key in observation.DIMENSIONS}
                return [{
                    "code": code, "name": code,
                    "raw_dimensions": dimensions,
                    "raw_composite_score": stock_scanner.composite_from_dimensions(
                        dimensions),
                    "quality_adjusted_score": 60.0,
                    "data_quality": {"eligible": True, "reasons": []},
                }]

            artifact_path = Path(tmp) / "analysis.json"
            result = observation.analyze_observation_list(
                "2026-09-22", yaml_path=yaml_path,
                artifact_path=artifact_path, candidate_builder=builder,
                analyzer=analyzer)
            self.assertEqual(result["schema"], observation.SCHEMA)
            self.assertEqual([item["code"] for item in result["items"]],
                             ["600519", "000001", "600519", "60336"])
            self.assertEqual(result["items"][0]["raw_composite_score"],
                             stock_scanner.composite_from_dimensions(
                                 {key: 60.0 for key in observation.DIMENSIONS}))
            self.assertEqual(result["items"][1]["status"], "degraded")
            self.assertIn("重复代码", result["items"][2]["reasons"])
            self.assertIn("无效 A 股代码", result["items"][3]["reasons"])
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call["enable_wyckoff"] and
                                call["require_wyckoff_gate"] is False and
                                call["as_of_date"] == "2026-09-22"
                                for call in calls))
            self.assertEqual(json.loads(artifact_path.read_text())["schema"],
                             observation.SCHEMA)
            self.assertEqual(observation.load_artifact(
                "2026-09-22", artifact_path, yaml_path)["items"],
                result["items"])
            yaml_path.write_text("observation_list: []\n", encoding="utf-8")
            self.assertEqual(observation.load_artifact(
                "2026-09-22", artifact_path, yaml_path)["status"],
                "unavailable")

    def test_exact_date_membership_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            sector_root = Path(tmp) / "sector_stocks" / "history"
            snapshot_dir = sector_root / "2026-09-22"
            snapshot_dir.mkdir(parents=True)
            (snapshot_dir / "BK1.json").write_text(json.dumps({
                "data_date": "2026-09-22", "provider": "eastmoney",
                "stocks": [{"code": "600519", "name": "贵州茅台"}],
            }), encoding="utf-8")
            ranking_path = Path(tmp) / "rankings.json"
            ranking_path.write_text(json.dumps({
                "data_date": "2026-09-21",
                "rankings": {"sectors": [{"code": "BK1", "name": "白酒"}]},
            }), encoding="utf-8")
            with patch.object(observation, "SECTOR_SNAPSHOT_DIR", sector_root), \
                    patch.object(observation, "RANKING_CACHE", ranking_path):
                built = observation.build_candidates(["600519", "000001"],
                                                     "2026-09-22")
            self.assertEqual(built["600519"]["candidate"]["name"], "贵州茅台")
            self.assertEqual(built["600519"]["sector_status"],
                             "ranking_missing")
            self.assertIn("缺少", built["000001"]["error"])

    def test_historical_analysis_passes_cutoff_date_to_scanner(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "observation.yaml"
            yaml_path.write_text(
                "observation_list:\n"
                "  - {code: '600519', date: '2026-09-21', entry_phase: 吸筹}\n",
                encoding="utf-8")

            def builder(codes, data_date):
                return {"600519": {"candidate": {
                    "code": "600519", "ts_code": "600519.SH",
                    "name": "贵州茅台", "sector_code": "BK1",
                }}}

            calls = []

            def analyzer(candidates, **kwargs):
                calls.append(kwargs)
                return [{
                    "code": candidates[0]["code"], "name": "贵州茅台",
                    "raw_dimensions": {key: 60.0 for key in observation.DIMENSIONS},
                    "raw_composite_score": 60.0,
                    "quality_adjusted_score": 60.0,
                    "data_quality": {"eligible": True, "reasons": []},
                    "wyckoff": {"signal": {"is_buy_signal": True}},
                }]

            result = observation.analyze_observation_list(
                "2020-01-02", yaml_path=yaml_path,
                candidate_builder=builder, analyzer=analyzer, save=False)
            self.assertEqual(result["items"][0]["status"], "ready")
            self.assertEqual(calls[0]["as_of_date"], "2020-01-02")


if __name__ == "__main__":
    unittest.main()
