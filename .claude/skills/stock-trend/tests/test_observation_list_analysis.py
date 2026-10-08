"""Focused offline tests for YAML observation analysis and artifact isolation."""

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import observation_list_analysis as observation
from scans import stock_scanner


def _quality(day="2026-09-29", *, source="eastmoney", coverage=1.0):
    return {
        "as_of_date": day, "capital_expected_date": day,
        "coverage": coverage, "coverage_factor": coverage,
        "freshness_factor": 1.0, "confidence": coverage,
        "eligible": True, "reasons": [],
        "dimensions": {
            name: {
                "returned": True, "available": True, "fresh": True,
                "expected_date": day, "data_date": day,
                "fetched_at": f"{day}T16:00:00+08:00", "source": source,
                "quality": "good", "stale_reason": "",
                "source_status": "live_success",
            }
            for name in ("kline", "capital", "fundamental")
        },
    }


def _v2_row(day="2026-09-29", *, joined="2026-09-21", quality=None,
            status="ready"):
    quality = quality if quality is not None else _quality(day)
    config = {"code": "600519", "market": "SH", "joined_date": joined,
              "entry_phase": "吸筹"}
    dimensions = {key: 60.0 for key in observation.DIMENSIONS}
    return {
        "code": "600519", "market": "SH", "joined_date": joined,
        "data_date": day, "normalized_config": config,
        "row_config_sha256": observation._canonical_digest(config),
        "status": status, "raw_dimensions": dimensions,
        "raw_composite_score": 60.0, "quality_adjusted_score": 60.0,
        "quality_method": observation.QUALITY_METHOD_VERSION,
        "quality_digest": observation.quality_comparison_digest(quality),
        "data_quality": quality, "source_evidence": {},
        "kline_diagnostics": {},
    }


def _v2_payload(row, *, generated="2026-09-29T15:30:00+08:00",
                provisional=False):
    return {
        "schema": observation.SCHEMA, "data_date": row["data_date"],
        "generated_at": generated, "provisional": provisional,
        "scoring_model_version": observation.SCORING_MODEL_VERSION,
        "evidence_cutoff_date": row["data_date"], "items": [row],
        "normalized_config_sha256": observation._canonical_digest(
            [row["normalized_config"]]),
    }


class ObservationAnalysisTests(unittest.TestCase):
    def test_quality_comparison_digest_excludes_dates_but_retains_method_inputs(self):
        first = _quality("2026-09-29")
        second = _quality("2026-09-30")
        self.assertEqual(observation.quality_comparison_digest(first),
                         observation.quality_comparison_digest(second))
        second["dimensions"]["capital"]["source"] = "tencent"
        self.assertNotEqual(observation.quality_comparison_digest(first),
                            observation.quality_comparison_digest(second))
        second = _quality("2026-09-30", coverage=.8)
        self.assertNotEqual(observation.quality_comparison_digest(first),
                            observation.quality_comparison_digest(second))

    def test_v2_freezes_normalized_config_and_historical_loader_self_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "observation.yaml"
            yaml_path.write_text(
                "observation_list:\n"
                "  - {code: '600519', date: '2026-09-21', entry_phase: 吸筹}\n",
                encoding="utf-8")
            artifact_path = Path(tmp) / "2026-09-29.json"
            result = observation.analyze_observation_list(
                "2026-09-29", yaml_path=yaml_path, artifact_path=artifact_path,
                candidate_builder=lambda codes, day: {}, save=True)
            self.assertEqual(result["schema"], "yaml-observation-analysis/v2")
            self.assertEqual(result["scoring_model_version"],
                             observation.SCORING_MODEL_VERSION)
            self.assertEqual(result["evidence_cutoff_date"], "2026-09-29")
            self.assertIsNotNone(
                observation._parse_datetime(result["generated_at"]).tzinfo)
            row = result["items"][0]
            self.assertEqual((row["market"], row["joined_date"]),
                             ("SH", "2026-09-21"))
            self.assertEqual(row["normalized_config"], {
                "code": "600519", "market": "SH",
                "joined_date": "2026-09-21", "entry_phase": "吸筹",
            })
            expected = hashlib.sha256(json.dumps(
                row["normalized_config"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(row["row_config_sha256"], expected)

            yaml_path.write_text("observation_list: []\n", encoding="utf-8")
            self.assertEqual(observation.load_artifact(
                "2026-09-29", artifact_path, yaml_path)["status"], "unavailable")
            historical = observation.load_historical_artifact(
                "2026-09-29", artifact_path, as_of=result["generated_at"])
            self.assertEqual(historical["history_compatibility"], "full")

    def test_historical_loader_rejects_tampering_duplicates_and_future_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            row = _v2_row()
            payload = _v2_payload(
                row, generated="2026-09-30T17:00:00+08:00")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path,
                as_of="2026-09-30T16:00:00+08:00")["reason_code"],
                "future_artifact")
            payload["generated_at"] = "2026-09-30T15:00:00+08:00"
            payload["items"] = [row, dict(row)]
            payload["normalized_config_sha256"] = observation._canonical_digest(
                [row["normalized_config"], row["normalized_config"]])
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "duplicate_identity")
            payload["items"] = [{**row, "row_config_sha256": "bad"}]
            payload["normalized_config_sha256"] = observation._canonical_digest(
                [row["normalized_config"]])
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "row_config_digest_mismatch")

    def test_v2_loader_validates_completion_quality_join_date_and_evidence_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            row = _v2_row()
            payload = _v2_payload(row)

            payload["generated_at"] = "2026-09-29T15:05:00+08:00"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "trading_day_incomplete")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path, require_completed=False)["history_compatibility"],
                "full")

            payload = _v2_payload(_v2_row(), provisional=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "trading_day_incomplete")

            row = _v2_row()
            row["data_quality"]["coverage"] = .8
            payload = _v2_payload(row)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "quality_digest_mismatch")

            row = _v2_row(joined="2026-09-30")
            payload = _v2_payload(row)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "joined_date_invalid")

            row = _v2_row()
            row["source_evidence"] = {
                "kline": {"returned_data_date": "2026-09-30"}}
            payload = _v2_payload(row)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "future_row_evidence")

    def test_v2_loader_rejects_naive_freeze_and_incomplete_ready_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            payload = _v2_payload(_v2_row(), generated="2026-09-29T15:30:00")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "invalid_generated_at")

            row = _v2_row()
            row["raw_dimensions"]["capital"] = None
            payload = _v2_payload(row)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "ready_score_invalid")

    def test_historical_loader_accepts_valid_v1_for_collection_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            path.write_text(json.dumps({
                "schema": "yaml-observation-analysis/v1",
                "data_date": "2026-09-29",
                "items": [{"code": "600519"}, {"code": "000001"}],
            }), encoding="utf-8")
            loaded = observation.load_historical_artifact("2026-09-29", path)
            self.assertEqual(loaded["history_compatibility"], "collection_only")
            self.assertEqual([item["market"] for item in loaded["items"]],
                             ["SH", "SZ"])

    def test_v1_collection_loader_validates_timestamp_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            payload = {
                "schema": observation.LEGACY_SCHEMA, "data_date": "2026-09-29",
                "items": [{"code": "600519"}], "generated_at": "not-a-time",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "invalid_generated_at")
            payload["generated_at"] = "2026-09-29T15:05:00"
            payload["provisional"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path)["reason_code"], "trading_day_incomplete")
            self.assertEqual(observation.load_historical_artifact(
                "2026-09-29", path, require_completed=False)[
                    "history_compatibility"], "collection_only")

    def test_missing_kline_records_exact_date_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "observation.yaml"
            yaml_path.write_text(
                "observation_list:\n"
                "  - {code: '301489', date: '2026-09-21', entry_phase: 未记录}\n",
                encoding="utf-8")

            def builder(codes, _data_date):
                return {codes[0]: {"candidate": {
                    "code": codes[0], "ts_code": "301489.SZ",
                    "name": "思泉新材", "sector_code": "BK1039",
                }, "sector_status": "ready"}}

            def analyzer(candidates, **kwargs):
                kwargs["kline_diagnostics"][candidates[0]["code"]] = {
                    "reason_code": "wrong_trading_date",
                    "expected_date": "2026-09-29",
                    "latest_date": "2026-09-28",
                    "record_count": 0,
                    "provider": "baostock",
                }
                return []

            result = observation.analyze_observation_list(
                "2026-09-29", yaml_path=yaml_path,
                candidate_builder=builder, analyzer=analyzer, save=False)
            row = result["items"][0]
            self.assertEqual(row["status"], "degraded")
            self.assertEqual(row["reasons"], [
                "K 线仅到 2026-09-28，要求 2026-09-29（BaoStock）"])
            self.assertEqual(row["kline_diagnostics"]["reason_code"],
                             "wrong_trading_date")
            result["provisional"] = True
            rendered = observation.market_regime.render_observation_list_html(result)
            self.assertIn("盘中临时分析", rendered)
            self.assertIn("K 线仅到 2026-09-28", rendered)
            self.assertIn("要求 2026-09-29，最新日期未知",
                          observation._kline_failure_text({
                              "reason_code": "fetch_failed",
                              "expected_date": "2026-09-29",
                              "provider_attempts": [
                                  {"source": "eastmoney", "status": "error"},
                                  {"source": "tencent_a", "status": "empty"},
                              ],
                          }))

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

            resolved = []

            def name_resolver(code, data_date):
                resolved.append((code, data_date))
                if code == "600519":
                    return {"name": "贵州茅台", "name_source": "fixture",
                            "name_quality": "identity_only"}
                return {}

            artifact_path = Path(tmp) / "analysis.json"
            result = observation.analyze_observation_list(
                "2026-09-22", yaml_path=yaml_path,
                artifact_path=artifact_path, candidate_builder=builder,
                analyzer=analyzer, name_resolver=name_resolver)
            self.assertEqual(result["schema"], observation.SCHEMA)
            self.assertEqual([item["code"] for item in result["items"]],
                             ["600519", "000001", "600519", "60336"])
            self.assertEqual(result["items"][0]["raw_composite_score"],
                             stock_scanner.composite_from_dimensions(
                                 {key: 60.0 for key in observation.DIMENSIONS}))
            self.assertEqual(result["items"][0]["name"], "贵州茅台")
            self.assertEqual(result["items"][0]["name_quality"], "identity_only")
            self.assertEqual(resolved, [("600519", "2026-09-22"), ("000001", "2026-09-22")])
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

    def test_complete_same_date_cohort_is_carried_into_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sector_root = root / "sector_stocks" / "history"
            snapshot_dir = sector_root / "2026-09-22"
            snapshot_dir.mkdir(parents=True)
            (snapshot_dir / "BK1.json").write_text(json.dumps({
                "data_date": "2026-09-22", "provider": "eastmoney",
                "stocks": [
                    {"code": "600519", "name": "贵州茅台", "change_pct": -1.0},
                    {"code": "000001", "name": "平安银行", "change_pct": 2.0},
                ],
            }), encoding="utf-8")
            ranking_path = root / "rankings.json"
            ranking_path.write_text(json.dumps({
                "data_date": "2026-09-22",
                "rankings": {"meta": {"complete": True, "provider": "eastmoney"},
                             "sectors": [
                                 {"code": "BK1", "name": "测试行业", "type": "industry",
                                  "change_pct": 1.0, "up_count": 2, "down_count": 0,
                                  "total_count": 2, "main_force_net": 100000000},
                                 {"code": "BK2", "name": "对照行业", "type": "industry",
                                  "change_pct": -1.0, "up_count": 1, "down_count": 3,
                                  "total_count": 4, "main_force_net": -100000000},
                             ]},
            }), encoding="utf-8")
            with patch.object(observation, "SECTOR_SNAPSHOT_DIR", sector_root), \
                    patch.object(observation, "RANKING_CACHE", ranking_path):
                built = observation.build_candidates(["600519", "000001"],
                                                     "2026-09-22")
            self.assertEqual(built["600519"]["sector_status"], "ready")
            self.assertEqual(built["000001"]["sector_status"], "ready")
            self.assertEqual(built["600519"]["peer_cohorts"]["BK1"], [-1.0, 2.0])
            membership = built["600519"]["candidate"]["sector_memberships"][0]
            self.assertNotEqual(membership["hot_score"], 50)
            self.assertEqual(membership["sector_type"], "industry")

    def test_incomplete_peer_snapshot_is_not_scored(self):
        source = {"code": "600519", "date": "2026-09-22", "entry_phase": "吸筹"}
        result = observation._row(
            source, "2026-09-22",
            {"sector_status": "peer_incomplete", "candidate": {}},
            {"raw_dimensions": {key: 60.0 for key in observation.DIMENSIONS},
             "data_quality": {"eligible": True, "reasons": []}},
        )
        self.assertIsNone(result["raw_dimensions"]["sector_strength"])
        self.assertIsNone(result["raw_composite_score"])
        self.assertIn("sector_peer_coverage_incomplete", result["reasons"])


if __name__ == "__main__":
    unittest.main()
