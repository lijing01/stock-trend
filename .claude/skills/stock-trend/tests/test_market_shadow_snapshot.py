#!/usr/bin/env python3
"""Tests for independent style-shadow persistence and time-aware membership."""

import copy
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.market_style import match_candidate_styles
from core import market_shadow_snapshot as ms


def shadow_payload(score=100):
    return {
        "schema_version": "market-style-shadow/v1",
        "model_version": "style-ma20/v1",
        "parameter_version": "style-observer/v1",
        "basis_date": "2026-09-07",
        "snapshot_type": "formal",
        "legacy_context_sha256": "context-digest",
        "styles": {
            "000300.SH": {
                "code": "000300.SH", "name": "沪深300", "score": score,
                "status": "strong" if score == 100 else "weak",
            },
        },
        "status": "complete",
        "formal_policy_affected": False,
    }


def membership(index_code="000300.SH", member_code="600519.SH",
               known_at="2026-09-01", effective_from="2026-01-01",
               effective_to=None, source="fixture"):
    return {
        "index_code": index_code,
        "member_code": member_code,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "known_at": known_at,
        "source": source,
    }


class TestMarketShadowSnapshot(unittest.TestCase):
    def test_valid_membership_is_matched_at_basis_date(self):
        result = match_candidate_styles(
            "600519.SH", [membership()], "2026-09-07")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["index_codes"], ["000300.SH"])
        self.assertEqual(result["records"][0]["member_code"], "600519.SH")

    def test_future_known_at_and_out_of_window_are_unknown(self):
        result = match_candidate_styles(
            "600519.SH", [membership(known_at="2026-09-08")], "2026-09-07")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("known_at_future", result["reasons"])

        result = match_candidate_styles(
            "600519.SH", [membership(effective_from="2026-09-08")],
            "2026-09-07")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("effective_window_miss", result["reasons"])

    def test_invalid_code_source_and_overlapping_records_are_not_ready(self):
        invalid = membership(source="")
        result = match_candidate_styles(
            "600519.SH", [invalid], "2026-09-07")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("source_missing", result["reasons"])

        invalid_code = match_candidate_styles(
            "600519.XY", [membership()], "2026-09-07")
        self.assertIn("candidate_code_format_invalid", invalid_code["reasons"])

        result = match_candidate_styles(
            "600519.SH", [membership(), membership(known_at="2026-09-02")],
            "2026-09-07")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("effective_interval_conflict", result["reasons"])

    def test_multiple_index_memberships_are_preserved(self):
        result = match_candidate_styles(
            "600519.SH",
            [membership("000300.SH"), membership("000905.SH")],
            "2026-09-07")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["index_codes"], ["000300.SH", "000905.SH"])
        self.assertEqual(len(result["records"]), 2)

    def test_same_input_is_idempotent_and_different_input_has_separate_path(self):
        payload = shadow_payload()
        with tempfile.TemporaryDirectory() as tmp:
            first = ms.save_shadow_run(payload, tmp)
            second = ms.save_shadow_run(copy.deepcopy(payload), tmp)
            changed = ms.save_shadow_run(shadow_payload(0), tmp)

            self.assertEqual(first.status, "created")
            self.assertEqual(second.status, "unchanged")
            self.assertEqual(changed.status, "created")
            self.assertNotEqual(first.path, changed.path)
            self.assertTrue(Path(first.path).exists())
            self.assertEqual(Path(first.path).parent.name, "2026-09-07")
            self.assertEqual(Path(first.path).parent.parent.name, "formal")
            self.assertFalse((Path(tmp) / "recommendation_history").exists())

    def test_saved_snapshot_load_validates_envelope_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ms.save_shadow_run(shadow_payload(), tmp)
            loaded = ms.load_shadow_run(result.path)
            self.assertEqual(loaded["content_sha256"], result.content_sha256)

            tampered = Path(tmp) / "tampered.json"
            value = json.loads(Path(result.path).read_text(encoding="utf-8"))
            value["status"] = "tampered"
            tampered.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                ms.load_shadow_run(tampered)

    def test_concurrent_same_content_does_not_lose_snapshot(self):
        payload = shadow_payload()
        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(
                    lambda _item: ms.save_shadow_run(payload, tmp), range(8)))
            self.assertEqual({result.status for result in results},
                             {"created", "unchanged"})
            files = list(Path(tmp).rglob("*.json"))
            self.assertEqual(len(files), 1)
            json.loads(files[0].read_text(encoding="utf-8"))

    def test_atomic_write_failure_is_structured_and_cleans_temp_files(self):
        payload = shadow_payload()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ms.os, "link", side_effect=OSError("disk full")):
                result = ms.save_shadow_run(payload, tmp)
            self.assertEqual(result.status, "write_failed")
            self.assertEqual(list(Path(tmp).rglob(".tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
