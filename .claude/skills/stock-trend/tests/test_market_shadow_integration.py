#!/usr/bin/env python3
"""Integration contracts for the opt-in market-style shadow report."""

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.recommendation_snapshot import content_sha256
from scans import daily_candidates as dc
from analysis.market_style import annotate_candidates_for_shadow


def candidate(code="600519.SH"):
    return {
        "code": code,
        "name": "测试候选",
        "composite_score": 80.0,
        "quality_adjusted_score": 80.0,
        "data_quality": {"eligible": True, "coverage": 1.0, "reasons": []},
        "sector_actionable": True,
        "score_eligible": True,
        "wyckoff": {"sub_phase": "LPS", "confidence": 0.6},
    }


def buckets(item):
    return {
        "actionable": [item], "waiting_trigger": [],
        "next_day_confirmation": [], "observation": [],
        "data_rejected": [],
    }


def shadow(context_digest="ctx-1"):
    return {
        "schema_version": "market-style-shadow/v1",
        "model_version": "style-ma20/v1",
        "parameter_version": "style-observer/v1",
        "basis_date": "2026-09-07",
        "snapshot_type": "formal",
        "legacy_context_sha256": context_digest,
        "styles": {
            "000300.SH": {"code": "000300.SH", "name": "沪深300",
                           "score": 100, "status": "strong"},
        },
        "status": "complete",
        "formal_policy_affected": False,
    }


class TestMarketShadowIntegration(unittest.TestCase):
    def test_loader_rejects_context_date_and_version_mismatch(self):
        regime = {"data_date": "2026-09-07", "context_sha256": "ctx-1"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow.json"
            path.write_text(json.dumps(shadow()), encoding="utf-8")
            loaded = dc._load_style_shadow(path, regime)
            self.assertEqual(loaded["status"], "ready")

            path.write_text(json.dumps(shadow("ctx-2")), encoding="utf-8")
            loaded = dc._load_style_shadow(path, regime)
            self.assertEqual(loaded["status"], "mismatch")
            self.assertIn("legacy_context_sha256_mismatch", loaded["reasons"])

            invalid = shadow()
            invalid["model_version"] = "style-ma20/v0"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            loaded = dc._load_style_shadow(path, regime)
            self.assertEqual(loaded["status"], "mismatch")
            self.assertIn("model_version_mismatch", loaded["reasons"])

    def test_annotation_never_changes_formal_candidates_or_buckets(self):
        item = candidate()
        formal = buckets(item)
        before = copy.deepcopy(formal)
        annotated, annotated_buckets = annotate_candidates_for_shadow(
            [item], formal, shadow(), {
                "records": [{
                    "index_code": "000300.SH", "member_code": "600519.SH",
                    "effective_from": "2026-01-01", "effective_to": None,
                    "known_at": "2026-09-01", "source": "fixture",
                }],
            })

        self.assertEqual(formal, before)
        self.assertNotIn("style_shadow", item)
        self.assertEqual(annotated[0]["style_shadow"]["shadow_bucket"],
                         "actionable")
        self.assertEqual(annotated_buckets["actionable"][0]["code"],
                         "600519.SH")
        self.assertFalse(annotated[0]["style_shadow"]["action_changed"])

    def test_renderers_explicitly_label_experimental_observation(self):
        item = candidate()
        policy = {"mode": "actionable", "max_recommendations": 5,
                  "reasons": []}
        style_report = {
            "status": "ready", "basis_date": "2026-09-07",
            "formal_policy_affected": False,
            "styles": shadow()["styles"],
            "candidate_run": {"status": "created"},
        }
        report_candidates, report_buckets = annotate_candidates_for_shadow(
            [item], buckets(item), shadow(), {
                "records": [{
                    "index_code": "000300.SH", "member_code": "600519.SH",
                    "effective_from": "2026-01-01", "effective_to": None,
                    "known_at": "2026-09-01", "source": "fixture",
                }],
            })
        markdown = dc.generate_report(
            report_candidates, [], 0.1, policy, report_buckets,
            style_shadow=style_report)
        html = dc._generate_html(
            report_candidates, [], 0.1, "20260907-151126", policy,
            report_buckets,
            style_shadow=style_report)
        output = dc.build_json_output(
            report_candidates, [], 0.1, policy, report_buckets,
            style_shadow=style_report)

        self.assertIn("市场风格影子观察", markdown)
        self.assertIn("实验观察，不参与推荐", markdown)
        self.assertIn("风格影子观察", markdown)
        self.assertIn("市场风格影子观察", html)
        self.assertIn("实验观察，不参与推荐", html)
        self.assertEqual(output["style_shadow"]["status"], "ready")
        self.assertFalse(output["style_shadow"]["formal_policy_affected"])

    def test_candidate_run_payload_is_independent_from_official_snapshot(self):
        item = candidate()
        policy = {"mode": "actionable", "max_recommendations": 5,
                  "reasons": []}
        formal = dc._save_recommendation_snapshot(
            [item], [], policy, buckets(item), "2026-09-07", {},
            market_regime={"data_date": "2026-09-07"})
        self.assertIsNotNone(formal)

        payload = dc._build_candidate_shadow_payload(
            "2026-09-07", "formal", [item], buckets(item),
            {"status": "ready", "path": "/tmp/style.json",
             "content_sha256": "style-digest"},
            {"status": formal["status"],
             "content_sha256": formal.get("content_sha256")},
            {"scan_status": "complete"},
            scanned_candidates=[item, candidate("000001.SZ")],
            membership_records=[{"source": "fixture", "member_code": "600519.SH"}],
        )
        self.assertEqual(payload["sample_scope"], "scanned_population")
        self.assertEqual(payload["style_shadow_content_sha256"],
                         "style-digest")
        self.assertEqual(payload["formal_policy_affected"], False)
        self.assertTrue(payload["candidate_set_sha256"])
        self.assertEqual(payload["scanned_candidate_count"], 2)
        self.assertEqual(payload["candidate_records"][1]["selection_status"],
                         "not_selected")
        self.assertEqual(payload["membership_record_count"], 1)
        self.assertEqual(
            payload["membership_input_sha256"],
            content_sha256([{"source": "fixture", "member_code": "600519.SH"}]),
        )
        self.assertEqual(payload["formal_snapshot_tracking"]["status"],
                         formal["status"])

    def test_main_keeps_official_snapshot_formal_and_reports_shadow_copy(self):
        item = candidate()

        def fake_scan(*_args, metrics=None, **_kwargs):
            metrics.update({"batch_count": 1})
            return [copy.deepcopy(item)]

        captured = []
        style = shadow()
        memberships = [{
            "index_code": "000300.SH", "member_code": "600519.SH",
            "effective_from": "2026-01-01", "effective_to": None,
            "known_at": "2026-09-01", "source": "fixture",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            style_path = Path(tmp) / "style.json"
            membership_path = Path(tmp) / "memberships.json"
            style_path.write_text(json.dumps(style), encoding="utf-8")
            membership_path.write_text(json.dumps(memberships), encoding="utf-8")

            def fake_save(source):
                captured.append(copy.deepcopy(source))
                return type("Result", (), {
                    "status": "created", "path": None,
                    "content_sha256": "formal-digest", "reason": None,
                    "normalization_warnings": [],
                })()

            with patch.object(dc, "load_regime_context", return_value={
                "score": 80, "label": "强势", "data_date": "2026-09-07",
                "context_sha256": "ctx-1",
            }), patch("fetchers.sector_data.get_last_trading_day",
                      return_value=("2026-09-07", "snapshot")), \
                 patch.object(dc, "resolve_recommendation_date",
                              return_value="2026-09-07"), \
                 patch.object(dc, "is_recommendation_session", return_value=False), \
                 patch.object(dc, "pick_hot_sectors", return_value=[{
                     "code": "BK1", "name": "测试板块", "sector_score": 60,
                 }]), patch.object(dc, "scan_sectors", side_effect=fake_scan), \
                 patch.object(dc, "save_snapshot_if_official", side_effect=fake_save), \
                 patch.object(dc, "_save_candidate_shadow_run", return_value={
                     "status": "created", "path": "shadow.json",
                     "content_sha256": "candidate-shadow", "reason": None,
                 }), patch.object(dc, "REPORTS_DIR", Path(tmp)), \
                 patch.object(sys, "argv", [
                     "daily_candidates.py", "--json", "--no-html",
                     "--style-shadow", str(style_path),
                     "--memberships", str(membership_path),
                 ]):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    dc.main()

        output = json.loads(stdout.getvalue())
        self.assertEqual(len(captured), 1)
        self.assertNotIn("style_shadow", captured[0]["candidates"][0])
        self.assertNotIn("style_shadow", captured[0]["buckets"]["actionable"][0])
        self.assertEqual(output["style_shadow"]["status"], "ready")
        self.assertNotIn("shadow", output["style_shadow"])
        self.assertEqual(
            output["candidates"][0]["style_shadow"]["matched_style_state"],
            "strong",
        )
        self.assertEqual(output["recommendations"][0]["code"], "600519.SH")


if __name__ == "__main__":
    unittest.main()
