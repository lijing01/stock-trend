#!/usr/bin/env python3
"""Frozen-sample validation checks for weak-to-strong v2."""

import json
import sys
import tempfile
import unittest
from pathlib import Path


TESTS = Path(__file__).resolve().parent
SKILL_ROOT = TESTS.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from research.validate_weak_to_strong_v2 import (  # noqa: E402
    CASES, load_fixture, render_markdown, validate_fixture,
)


FIXTURE = TESTS / "fixtures" / "weak_to_strong_v2" / "frozen_inputs.json"


class WeakToStrongV2ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_fixture(FIXTURE)
        cls.report = validate_fixture(cls.fixture, performance_runs=0)

    def test_fixture_is_source_bound_and_research_only(self):
        self.assertFalse(self.fixture["formal_close_evidence"])
        self.assertEqual("simulated_for_research",
                         self.fixture["close_evidence_used_by_validation"])
        self.assertEqual(5, len(self.fixture["sources"]))
        self.assertTrue(all(len(item["source_sha256"]) == 64
                            for item in self.fixture["sources"].values()))
        self.assertEqual(64, len(self.fixture["calendar"]["source_sha256"]))

    def test_all_six_intervals_and_causal_invariance(self):
        self.assertEqual(len(CASES), len(self.report["intervals"]))
        self.assertEqual(143, self.report["counts"]["interval_rows"])
        self.assertEqual(142, self.report["counts"]["unique_security_dates"])
        self.assertEqual(143, self.report["counts"]["prefix_invariance_passed"])
        self.assertEqual(143, self.report["counts"]["future_append_invariance_passed"])

    def test_key_semantics_and_parent_overhead_are_frozen(self):
        by_key = {(item["code"], item["date"]): item
                  for item in self.report["key_expectations"]}
        self.assertEqual("RANGE_BREAKOUT", by_key[("002724.SZ", "2026-09-21")]["stage"])
        self.assertEqual("LOCAL_CONFIRMATION", by_key[("000678.SZ", "2026-10-22")]["stage"])
        self.assertEqual(13.90, by_key[("000678.SZ", "2026-10-22")]["parent_high"])
        self.assertEqual(46.81, by_key[("000021.SZ", "2026-06-16")]["parent_high"])
        self.assertEqual(31.60, by_key[("600641.SH", "2026-06-10")]["parent_high"])
        self.assertIn("not backfilled", self.report["causal_anchor_note"])

    def test_wanxiang_is_fixed_negative_control(self):
        control = self.report["negative_control"]
        self.assertEqual("000559.SZ", control["code"])
        self.assertTrue(all(control["checks"].values()))
        self.assertLess(control["metrics"]["close_strength"], 0.60)

    def test_output_is_serializable_and_labels_performance_scope(self):
        encoded = json.dumps(self.report, allow_nan=False)
        self.assertIn("simulated_for_research", encoded)
        markdown = render_markdown(self.report)
        self.assertIn("冻结五股批量，非全市场", markdown)
        self.assertIn("continue_accumulating", markdown)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "validation.json"
            path.write_text(json.dumps(self.report), encoding="utf-8")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
