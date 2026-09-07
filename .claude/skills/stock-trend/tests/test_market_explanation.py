#!/usr/bin/env python3
"""Contract tests for the pure market-regime explanation builder."""

import copy
import math
import sys
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis.market_explanation import build_market_explanation


COMPONENT_SCORES = {
    "index_trend": 13.3,
    "volume": 37.7,
    "breadth": 62.5,
    "zt_emotion": 100.0,
    "capital": 69.4,
}


def fixture_close_context():
    components = {
        key: {
            "score": score,
            "detail": f"{key} fixture",
            "data_status": "partial" if key == "capital" else "good",
        }
        for key, score in COMPONENT_SCORES.items()
    }
    components["capital"].update({
        "metric": "market_main_force_net_inflow",
        "source_kind": "alternative",
        "provider": "eastmoney",
        "data_date": "2026-09-07",
        "fetched_at": None,
    })
    return {
        "generated_at": "2026-09-07 15:04:32",
        "data_date": "2026-09-07",
        "intraday": False,
        "regime": {
            "score": 53.4,
            "label": "弱势",
            "data_quality": "partial",
            "missing_components": [],
            "partial_components": ["capital"],
        },
        "components": components,
        "indices": {
            "000001.SH": {
                "close": 3800.0,
                "above_ma20": False,
                "ma20_rising": True,
                "data_date": "2026-09-07",
                "source": "eastmoney",
            },
            "000300.SH": {
                "close": 4500.0,
                "above_ma20": False,
                "ma20_rising": False,
                "data_date": "2026-09-07",
                "source": "tencent",
            },
            "399001.SZ": {
                "close": 12000.0,
                "above_ma20": False,
                "ma20_rising": False,
                "data_date": "2026-09-07",
                "source": "baostock",
            },
        },
        "index_data_quality": {
            "000001.SH": {"source": "eastmoney", "data_date": "20260907"},
            "000300.SH": {"source": "tencent", "data_date": "20260907"},
            "399001.SZ": {"source": "baostock", "data_date": "20260907"},
        },
    }


def assert_all_finite(testcase, value):
    if isinstance(value, dict):
        for item in value.values():
            assert_all_finite(testcase, item)
    elif isinstance(value, list):
        for item in value:
            assert_all_finite(testcase, item)
    elif isinstance(value, float):
        testcase.assertTrue(math.isfinite(value), repr(value))


class MarketExplanationTests(unittest.TestCase):
    def test_close_score_reconciles(self):
        ctx = fixture_close_context()
        result = build_market_explanation(ctx, "2026-09-07")

        self.assertEqual(result["score"], 53.4)
        self.assertEqual(result["reconciliation"], "matched")
        self.assertAlmostEqual(
            sum(item["contribution"] for item in result["components"]),
            53.43,
        )
        self.assertIn("regime_weak", result["blocking_reasons"])
        self.assertIn("regime_data_partial", result["blocking_reasons"])
        self.assertEqual(
            result["blocking_reasons"],
            ["regime_data_partial", "regime_weak"],
        )

    def test_fixed_components_expose_index_and_alternative_capital_evidence(self):
        result = build_market_explanation(fixture_close_context(), "2026-09-07")

        self.assertEqual(result["schema_version"], "market-explanation/v1")
        self.assertEqual(result["basis_date"], "2026-09-07")
        self.assertEqual(result["basis_generated_at"], "2026-09-07 15:04:32")
        self.assertEqual(result["mode"], "close")
        self.assertEqual(
            [item["id"] for item in result["components"]],
            ["index_trend", "volume", "breadth", "zt_emotion", "capital"],
        )
        index = result["components"][0]["evidence"]
        self.assertEqual(index["expected_count"], 3)
        self.assertEqual(index["available_count"], 3)
        self.assertEqual(index["completeness"], "complete")
        self.assertEqual(len(index["indices"]), 3)
        capital = result["components"][-1]["evidence"]
        self.assertEqual(capital["metric"], "market_main_force_net_inflow")
        self.assertEqual(capital["source_kind"], "alternative")
        self.assertEqual(capital["usage"], "reference_only")
        self.assertEqual(capital["freshness"], "unknown")
        self.assertIn("capital_alternative_metric", result["quality_notes"])
        self.assertIn("source_timestamp_missing", capital["reasons"])

    def test_legacy_cache_uses_unknown_evidence_without_inventing_timestamps(self):
        ctx = fixture_close_context()
        ctx.pop("indices")
        ctx.pop("index_data_quality")
        for component in ctx["components"].values():
            for key in ("detail", "metric", "source_kind", "provider", "data_date"):
                component.pop(key, None)

        result = build_market_explanation(ctx, "2026-09-07")

        self.assertIn("legacy_context_evidence_unknown", result["quality_notes"])
        for component in result["components"]:
            evidence = component["evidence"]
            self.assertEqual(evidence["freshness"], "unknown")
            self.assertIsNone(evidence["fetched_at"])

    def test_partial_index_failure_records_expected_and_available_counts(self):
        ctx = fixture_close_context()
        ctx["indices"]["399001.SZ"].update({"close": None})
        ctx["index_data_quality"]["399001.SZ"].update(
            {"source": "error", "record_count": 0, "data_date": ""}
        )

        result = build_market_explanation(ctx, "2026-09-07")
        evidence = result["components"][0]["evidence"]

        self.assertEqual(evidence["expected_count"], 3)
        self.assertEqual(evidence["available_count"], 2)
        self.assertEqual(evidence["completeness"], "partial")
        self.assertIn("index_coverage_2/3", evidence["reasons"])
        failed = next(item for item in evidence["indices"] if item["code"] == "399001.SZ")
        self.assertEqual(failed["usage"], "unavailable")

    def test_missing_index_key_still_uses_the_formal_expected_count(self):
        ctx = fixture_close_context()
        ctx["indices"].pop("399001.SZ")
        ctx["index_data_quality"].pop("399001.SZ")

        evidence = build_market_explanation(ctx, "2026-09-07")["components"][0]["evidence"]

        self.assertEqual(evidence["expected_count"], 3)
        self.assertEqual(evidence["available_count"], 2)
        self.assertEqual(evidence["completeness"], "partial")

    def test_missing_status_score_uses_the_same_normalization_as_formal_regime(self):
        ctx = fixture_close_context()
        ctx["components"]["volume"].update({"score": 50.0, "data_status": "missing"})
        ctx["regime"].update({"score": 55.9, "data_quality": "missing"})

        result = build_market_explanation(ctx, "2026-09-07")

        self.assertEqual(result["normalization_denominator"], 1.0)
        self.assertAlmostEqual(sum(x["contribution"] for x in result["components"]), 55.89)
        self.assertEqual(result["reconciliation"], "matched")
        self.assertIn("regime_data_missing", result["blocking_reasons"])

    def test_non_finite_component_is_unavailable_and_never_leaks_to_output(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                ctx = fixture_close_context()
                ctx["components"]["capital"]["score"] = invalid
                result = build_market_explanation(ctx, "2026-09-07")
                capital = result["components"][-1]
                self.assertIsNone(capital["score"])
                self.assertEqual(capital["contribution"], 0.0)
                self.assertEqual(capital["evidence"]["usage"], "unavailable")
                self.assertIn("regime_data_missing", result["blocking_reasons"])
                self.assertLess(
                    result["blocking_reasons"].index("regime_data_missing"),
                    result["blocking_reasons"].index("regime_data_partial"),
                )
                self.assertEqual(result["reconciliation"], "unavailable")
                assert_all_finite(self, result)

    def test_invalid_or_mismatched_dates_are_conservative(self):
        invalid = build_market_explanation(fixture_close_context(), "2026-02-30")
        self.assertIn("regime_date_invalid", invalid["blocking_reasons"])
        self.assertEqual(invalid["reconciliation"], "unavailable")

        stale = build_market_explanation(fixture_close_context(), "2026-09-08")
        self.assertIn("regime_stale", stale["blocking_reasons"])
        self.assertEqual(stale["reconciliation"], "unavailable")

        malformed_ctx = fixture_close_context()
        malformed_ctx["data_date"] = "2026-02-30"
        malformed = build_market_explanation(malformed_ctx, "2026-09-07")
        self.assertIn("regime_date_invalid", malformed["blocking_reasons"])
        self.assertEqual(malformed["reconciliation"], "unavailable")

    def test_formal_threshold_boundaries_use_the_stored_score(self):
        cases = (
            (59.9, True),
            (60.0, False),
            (79.9, False),
            (80.0, False),
        )
        for score, is_weak in cases:
            with self.subTest(score=score):
                ctx = fixture_close_context()
                ctx["regime"]["score"] = score
                result = build_market_explanation(ctx, "2026-09-07")
                self.assertEqual("regime_weak" in result["blocking_reasons"], is_weak)

    def test_intraday_mix_reconciles_only_with_complete_anchor_evidence(self):
        ctx = fixture_close_context()
        ctx["intraday"] = True
        ctx["regime"]["score"] = 70.0
        ctx["intraday_evidence"] = {
            "anchor_score": 80.0,
            "blend_weight": 0.5,
            "projected_score": 60.0,
            "session_elapsed_fraction": 0.5,
        }

        result = build_market_explanation(ctx, "2026-09-07")

        self.assertEqual(result["mode"], "intraday")
        self.assertEqual(result["reconciliation"], "matched")
        intraday = result["intraday"]
        self.assertEqual(intraday["anchor_score"], 80.0)
        self.assertEqual(intraday["blend_weight"], 0.5)
        self.assertEqual(intraday["projected_score"], 60.0)
        self.assertEqual(intraday["formula_value"], 70.0)
        self.assertEqual(result["intraday_mix"]["session_elapsed_fraction"], 0.5)

        del ctx["intraday_evidence"]["anchor_score"]
        unavailable = build_market_explanation(ctx, "2026-09-07")
        self.assertEqual(unavailable["reconciliation"], "unavailable")
        self.assertIn("intraday_anchor_missing", unavailable["quality_notes"])

    def test_builder_does_not_mutate_frozen_input(self):
        ctx = fixture_close_context()
        before = copy.deepcopy(ctx)

        build_market_explanation(ctx, "2026-09-07")

        self.assertEqual(ctx, before)


if __name__ == "__main__":
    unittest.main()
