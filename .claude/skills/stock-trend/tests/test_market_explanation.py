#!/usr/bin/env python3
"""Contract tests for the pure market-regime explanation builder."""

import copy
import io
import json
import math
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis.market_explanation import build_market_explanation
from analysis import market_regime
from analysis.market_regime import select_market_context, validate_market_context


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
            "data_status": "good",
            "data_date": "2026-09-07",
        }
        for key, score in COMPONENT_SCORES.items()
    }
    components["capital"].update({
        "metric": "market_main_force_net_inflow",
        "source_kind": "primary",
        "provider": "eastmoney",
        "data_date": "2026-09-07",
        "fetched_at": None,
    })
    context = {
        "generated_at": "2026-09-07 15:04:32",
        "data_date": "2026-09-07",
        "intraday": False,
        "regime": {
            "score": 53.4,
            "label": "弱势",
            "data_quality": "good",
            "missing_components": [],
            "partial_components": [],
        },
        "components": components,
        "indices": {
            "000001.SH": {
                "ok": True,
                "close": 3800.0,
                "above_ma20": False,
                "ma20_rising": True,
                "data_date": "2026-09-07",
                "source": "eastmoney",
            },
            "000300.SH": {
                "ok": True,
                "close": 4500.0,
                "above_ma20": False,
                "ma20_rising": False,
                "data_date": "2026-09-07",
                "source": "tencent",
            },
            "399001.SZ": {
                "ok": True,
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
    context["market_explanation"] = build_market_explanation(
        context, context["data_date"])
    return context


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
    def test_empty_limit_up_response_is_missing_not_a_zero_count(self):
        with patch.object(market_regime, "HAS_AKSHARE", True), \
             patch.object(market_regime, "ak", SimpleNamespace(
                 stock_zt_pool_em=lambda date: SimpleNamespace(empty=True))):
            result = market_regime.fetch_zt_stats("2026-09-07")

        self.assertEqual(result["data_date"], "2026-09-07")
        self.assertEqual(result["data_status"], "missing")
        self.assertIsNone(result["count"])

    def test_market_context_validator_requires_same_day_complete_evidence(self):
        ctx = fixture_close_context()
        self.assertTrue(validate_market_context(ctx, "2026-09-07")["valid"])

        stale = validate_market_context(ctx, "2026-09-08")
        self.assertFalse(stale["valid"])
        self.assertIn("market_context_stale", stale["reasons"])

        partial = fixture_close_context()
        partial["regime"]["data_quality"] = "partial"
        partial["regime"]["partial_components"] = ["volume"]
        verdict = validate_market_context(partial, "2026-09-07")
        self.assertFalse(verdict["valid"])
        self.assertIn("market_context_quality_partial", verdict["reasons"])

        unknown_quality = fixture_close_context()
        unknown_quality["regime"]["data_quality"] = "verified-ish"
        verdict = validate_market_context(unknown_quality, "2026-09-07")
        self.assertFalse(verdict["valid"])
        self.assertIn("market_context_quality_verified-ish", verdict["reasons"])

        missing_component = fixture_close_context()
        del missing_component["components"]["capital"]
        verdict = validate_market_context(missing_component, "2026-09-07")
        self.assertIn("market_context_components_invalid", verdict["reasons"])

        malformed_explanation = fixture_close_context()
        malformed_explanation["market_explanation"]["components"] = []
        verdict = validate_market_context(malformed_explanation, "2026-09-07")
        self.assertIn("market_context_explanation_invalid", verdict["reasons"])

        explanation_backfilled = fixture_close_context()
        explanation_backfilled["components"]["breadth"].pop("data_date")
        explanation_backfilled["market_explanation"] = build_market_explanation(
            explanation_backfilled, "2026-09-07")
        verdict = validate_market_context(explanation_backfilled, "2026-09-07")
        self.assertIn("market_context_component_date_invalid", verdict["reasons"])

    def test_current_date_context_is_valid_with_component_owned_dates(self):
        expected = date.today().isoformat()
        context = fixture_close_context()
        context["data_date"] = expected
        for component in context["components"].values():
            component["data_date"] = expected
        for item in context["indices"].values():
            item["data_date"] = expected
        for item in context["index_data_quality"].values():
            item["data_date"] = expected.replace("-", "")
        context.setdefault("capital_context", {})["data_date"] = expected
        context["market_explanation"] = build_market_explanation(context, expected)

        self.assertTrue(validate_market_context(context, expected)["valid"])

    def test_as_of_collection_excludes_newer_session_and_live_snapshots(self):
        rows = [
            {"trade_date": "2026-09-11", "close": 10, "amount": 100},
            {"trade_date": "2026-09-14", "close": 11, "amount": 110},
        ]
        diagnostics = []

        def index_rows(_code, lmt=80, retries=2, diagnostics=None):
            diagnostics.update({"source": "fixture", "data_date": "2026-09-14"})
            return list(rows)

        with patch.object(market_regime, "fetch_index_kline", side_effect=index_rows), \
             patch.object(market_regime, "fetch_sector_rankings") as sectors, \
             patch.object(market_regime, "fetch_market_activity") as activity, \
             patch.object(market_regime, "fetch_zt_stats", return_value={}) as zt, \
             patch.object(market_regime, "load_history", return_value={}), \
             patch.object(market_regime, "load_portfolio", return_value=[]):
            context = market_regime.collect_context(
                now=datetime(2026, 9, 14, 10, 0), as_of="2026-09-11")

        self.assertEqual(context["data_date"], "2026-09-11")
        self.assertEqual(context["collection_target_date"], "2026-09-11")
        self.assertTrue(context["current_snapshot_suppressed"])
        sectors.assert_not_called()
        activity.assert_not_called()
        zt.assert_called_once_with("2026-09-11")

    def test_prior_as_of_suppresses_undated_realtime_snapshots(self):
        rows = [{"trade_date": "2026-09-11", "close": 10, "amount": 100}]

        def index_rows(_code, lmt=80, retries=2, diagnostics=None):
            diagnostics.update({"source": "fixture", "data_date": "2026-09-11"})
            return list(rows)

        with patch.object(market_regime, "fetch_index_kline", side_effect=index_rows), \
             patch.object(market_regime, "fetch_sector_rankings") as sectors, \
             patch.object(market_regime, "fetch_market_activity") as activity, \
             patch.object(market_regime, "fetch_zt_stats", return_value={
                 "count": 88, "streak_count": 12, "max_streak": 5,
             }), \
             patch.object(market_regime, "load_history", return_value={}), \
             patch.object(market_regime, "load_portfolio", return_value=[]):
            context = market_regime.collect_context(
                now=datetime(2026, 9, 14, 10, 0), as_of="2026-09-11")

        sectors.assert_not_called()
        activity.assert_not_called()
        self.assertTrue(context["current_snapshot_suppressed"])
        self.assertEqual(context["components"]["breadth"]["data_status"], "missing")
        self.assertEqual(context["components"]["capital"]["data_status"], "missing")
        self.assertEqual(context["components"]["zt_emotion"]["data_status"], "missing")
        verdict = validate_market_context(context, "2026-09-11")
        self.assertFalse(verdict["valid"])

    def test_historical_zt_failure_is_missing_instead_of_zero_evidence(self):
        with patch.object(market_regime, "HAS_AKSHARE", True), \
             patch.object(market_regime.ak, "stock_zt_pool_em",
                          side_effect=RuntimeError("provider failed")):
            zt = market_regime.fetch_zt_stats("2026-09-11")

        self.assertEqual(zt["data_date"], "2026-09-11")
        self.assertEqual(zt["data_status"], "missing")
        self.assertIsNone(zt["count"])
        scored = market_regime.score_zt_emotion(zt, [40, 45, 50, 55, 60])
        self.assertEqual(scored["data_status"], "missing")

    def test_invalid_refresh_preserves_only_verified_same_day_context(self):
        cached = fixture_close_context()
        invalid = fixture_close_context()
        invalid["data_date"] = ""
        invalid["regime"]["data_quality"] = "missing"
        selected, refresh = select_market_context(invalid, cached, "2026-09-07")
        self.assertIs(selected, cached)
        self.assertEqual(refresh["status"], "preserved_verified_context")

        stale = fixture_close_context()
        stale["data_date"] = "2026-09-06"
        selected, refresh = select_market_context(invalid, stale, "2026-09-07")
        self.assertIsNone(selected)
        self.assertEqual(refresh["status"], "invalid_no_fallback")
        self.assertEqual(refresh["error"], "market_context_invalid")

    def test_market_cli_does_not_write_when_preserving_verified_context(self):
        cached = fixture_close_context()
        cached.update({"amount_yi": 100, "zt": {}, "top_sectors": [],
                       "bottom_sectors": [], "holdings": [], "plan": []})
        invalid = copy.deepcopy(cached)
        invalid["data_date"] = ""
        invalid["regime"]["data_quality"] = "missing"
        stream = io.StringIO()
        with patch.object(market_regime, "collect_context", return_value=invalid), \
             patch.object(market_regime, "load_context", return_value=cached), \
             patch.object(market_regime, "save_context") as save, \
             patch.object(sys, "argv", ["market_regime.py", "--json", "--no-html",
                                         "--as-of", "2026-09-07"]), \
             redirect_stdout(stream):
            market_regime.main()
        payload = stream.getvalue()[stream.getvalue().index("{"):]
        result, _ = json.JSONDecoder().raw_decode(payload)
        self.assertEqual(result["refresh"]["status"],
                         "preserved_verified_context")
        save.assert_not_called()

    def test_market_cli_reports_context_persistence_failure(self):
        collected = fixture_close_context()
        collected.update({"amount_yi": 100, "zt": {}, "top_sectors": [],
                          "bottom_sectors": [], "holdings": [], "plan": []})
        stream = io.StringIO()
        with patch.object(market_regime, "collect_context", return_value=collected), \
             patch.object(market_regime, "load_context", return_value=None), \
             patch.object(market_regime, "should_save_history", return_value=False), \
             patch.object(market_regime, "save_context", return_value=False), \
             patch.object(sys, "argv", ["market_regime.py", "--json", "--no-html",
                                         "--as-of", "2026-09-07"]), \
             redirect_stdout(stream):
            market_regime.main()

        payload = stream.getvalue()[stream.getvalue().index("{"):]
        result, _ = json.JSONDecoder().raw_decode(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "market_context_not_persisted")

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
        self.assertNotIn("regime_data_partial", result["blocking_reasons"])
        self.assertEqual(result["blocking_reasons"], ["regime_weak"])

    def test_fixed_components_expose_index_and_primary_capital_evidence(self):
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
        self.assertEqual(capital["source_kind"], "primary")
        self.assertEqual(capital["usage"], "scorable")
        self.assertEqual(capital["freshness"], "unknown")
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
        ctx["indices"]["399001.SZ"].update({"ok": False, "close": None})
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
                self.assertNotIn("regime_data_partial", result["blocking_reasons"])
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
