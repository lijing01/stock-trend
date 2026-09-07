#!/usr/bin/env python3
"""Pure calculation tests for the market-style shadow observer."""

import copy
import math
import sys
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.market_style import (
    STYLE_DEFINITIONS,
    annotate_candidates_for_shadow,
    build_style_run,
    compute_style_observations,
    fetch_style_rows,
)
from core.recommendation_snapshot import content_sha256


def make_rows(values, start=date(2026, 8, 3)):
    rows = []
    current = start
    for value in values:
        while current.weekday() >= 5:
            current += timedelta(days=1)
        rows.append({
            "trade_date": current.isoformat(),
            "close": value,
        })
        current += timedelta(days=1)
    return rows


def context_for(rows_by_code):
    basis = max(row["trade_date"] for rows in rows_by_code.values()
                for row in rows)
    return {
        "data_date": basis,
        "generated_at": "2026-09-07 15:20:00",
        "regime": {"score": 53.4, "label": "弱势"},
    }


class TestMarketStyle(unittest.TestCase):
    def test_fixed_five_styles_and_four_ma20_buckets(self):
        rows_by_code = {
            "000300.SH": make_rows([100 + i for i in range(25)]),
            "000905.SH": make_rows([200 - i * 3 for i in range(25)]),
            "000852.SH": make_rows([100] * 20 + [200, 200, 200, 200, 110]),
            "399006.SZ": make_rows([200] * 20 + [1, 1, 1, 1, 190]),
            "000688.SH": make_rows([200 - i * 2 for i in range(25)]),
        }
        context = context_for(rows_by_code)
        result = compute_style_observations(context, rows_by_code)

        self.assertEqual(result["schema_version"], "market-style-shadow/v1")
        self.assertEqual(result["formal_policy_affected"], False)
        self.assertEqual(
            [definition["code"] for definition in STYLE_DEFINITIONS.values()],
            list(rows_by_code),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["styles"]["000300.SH"]["score"], 100)
        self.assertEqual(result["styles"]["000905.SH"]["score"], 0)
        self.assertEqual(result["styles"]["000852.SH"]["score"], 40)
        self.assertEqual(result["styles"]["399006.SZ"]["score"], 60)
        self.assertEqual(result["styles"]["000688.SH"]["score"], 0)

    def test_returns_distance_and_slope_use_only_observed_closes(self):
        rows = make_rows([100 + i for i in range(25)])
        code = "000300.SH"
        result = compute_style_observations(
            context_for({code: rows}), {code: rows})["styles"][code]
        metrics = result["metrics"]

        self.assertAlmostEqual(metrics["return_5d"], 5 / 119, places=6)
        self.assertAlmostEqual(metrics["return_20d"], 20 / 104, places=6)
        self.assertGreater(metrics["ma20_distance_pct"], 0)
        self.assertGreater(metrics["ma20_slope_pct"], 0)
        self.assertEqual(metrics["sample_count"], 25)

    def test_requires_exact_basis_date_and_at_least_21_valid_rows(self):
        full = make_rows([100 + i for i in range(25)])
        context = context_for({"000300.SH": full})
        context["data_date"] = full[-1]["trade_date"]

        short = full[:20]
        result = compute_style_observations(
            context, {"000300.SH": short})["styles"]["000300.SH"]
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("insufficient_valid_rows", result["reasons"])

        old = full[:-1]
        result = compute_style_observations(
            context, {"000300.SH": old})["styles"]["000300.SH"]
        self.assertIsNone(result["score"])
        self.assertIn("basis_date_not_last", result["reasons"])

    def test_rejects_future_duplicate_and_non_finite_rows_without_forward_fill(self):
        rows = make_rows([100 + i for i in range(25)])
        context = context_for({"000300.SH": rows})
        future = copy.deepcopy(rows)
        future[-1]["trade_date"] = "2026-09-08"
        result = compute_style_observations(
            context, {"000300.SH": future})["styles"]["000300.SH"]
        self.assertIsNone(result["score"])
        self.assertIn("future_record", result["reasons"])

        duplicate = copy.deepcopy(rows)
        duplicate[-1]["trade_date"] = duplicate[-2]["trade_date"]
        result = compute_style_observations(
            context, {"000300.SH": duplicate})["styles"]["000300.SH"]
        self.assertIsNone(result["score"])
        self.assertIn("duplicate_trade_date", result["reasons"])

        invalid = copy.deepcopy(rows)
        invalid[-1]["close"] = float("nan")
        result = compute_style_observations(
            context, {"000300.SH": invalid})["styles"]["000300.SH"]
        self.assertIsNone(result["score"])
        self.assertIn("non_finite_close", result["reasons"])
        self.assertFalse(any(
            isinstance(value, float) and not math.isfinite(value)
            for value in result.values()
        ))

    def test_partial_and_all_missing_statuses_are_structured(self):
        rows = make_rows([100 + i for i in range(25)])
        context = context_for({"000300.SH": rows})
        result = compute_style_observations(
            context, {"000300.SH": rows, "000905.SH": []})

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["styles"]["000905.SH"]["status"], "unknown")
        self.assertEqual(result["styles"]["000905.SH"]["evidence"]["usage"],
                         "unavailable")

        result = compute_style_observations(
            context, {code: [] for code in STYLE_DEFINITIONS})
        self.assertEqual(result["status"], "missing")
        self.assertTrue(all(item["score"] is None
                            for item in result["styles"].values()))

    def test_input_is_not_mutated_and_unknown_codes_are_ignored(self):
        rows = make_rows([100 + i for i in range(25)])
        rows_by_code = {"000300.SH": rows, "UNKNOWN": rows}
        before = copy.deepcopy(rows_by_code)
        result = compute_style_observations(
            context_for(rows_by_code), rows_by_code)

        self.assertEqual(rows_by_code, before)
        self.assertNotIn("UNKNOWN", result["styles"])

    def test_candidate_annotation_is_diagnostic_and_keeps_legacy_bucket(self):
        shadow = {
            "schema_version": "market-style-shadow/v1",
            "model_version": "style-ma20/v1",
            "parameter_version": "style-observer/v1",
            "basis_date": "2026-09-07",
            "styles": {
                "000300.SH": {"score": 100, "status": "strong"},
                "000905.SH": {"score": 0, "status": "weak"},
            },
            "status": "complete",
            "formal_policy_affected": False,
        }
        candidates = [{"code": "600519.SH", "composite_score": 80}]
        buckets = {
            "actionable": [], "waiting_trigger": [],
            "next_day_confirmation": [],
            "observation": candidates, "data_rejected": [],
        }
        memberships = {
            "records": [
                {"index_code": "000300.SH", "member_code": "600519.SH",
                 "effective_from": "2026-01-01", "effective_to": None,
                 "known_at": "2026-09-01", "source": "fixture"},
                {"index_code": "000905.SH", "member_code": "600519.SH",
                 "effective_from": "2026-01-01", "effective_to": None,
                 "known_at": "2026-09-01", "source": "fixture"},
            ],
        }
        annotated, annotated_buckets = annotate_candidates_for_shadow(
            candidates, buckets, shadow, memberships)

        row = annotated[0]
        self.assertEqual(row["style_shadow"]["matched_style_state"], "mixed")
        self.assertEqual(row["style_shadow"]["legacy_bucket"], "observation")
        self.assertEqual(row["style_shadow"]["shadow_bucket"], "observation")
        self.assertFalse(row["style_shadow"]["action_changed"])
        self.assertNotIn("style_shadow", candidates[0])
        self.assertEqual(annotated_buckets["observation"][0]["code"],
                         "600519.SH")

    def test_fetcher_is_bounded_and_reports_provider_failure(self):
        active = 0
        maximum = 0
        calls = []
        lock = threading.Lock()

        def fake_fetcher(code, lmt=80):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append((code, lmt))
            time.sleep(0.01)
            with lock:
                active -= 1
            if code == "000852.SH":
                raise RuntimeError("fixture provider failure")
            return []

        rows, diagnostics = fetch_style_rows(
            {"basis_date": "2026-09-07"}, fetcher=fake_fetcher,
            total_timeout=1, per_index_timeout=1)

        self.assertEqual(len(calls), 5)
        self.assertLessEqual(maximum, 2)
        self.assertIn("000300.SH", rows)
        self.assertEqual(diagnostics["000852.SH"]["source"], "error")

    def test_build_run_records_legacy_input_digest_and_raw_inputs(self):
        rows = make_rows([100 + i for i in range(25)])
        context = context_for({"000300.SH": rows})
        result = build_style_run(context, {"000300.SH": rows})

        self.assertEqual(result["input_mode"], "supplied")
        self.assertEqual(result["legacy_context_sha256"], content_sha256(context))
        self.assertEqual(result["raw_inputs"]["000300.SH"], rows)
        self.assertNotIn("000300.SH", context.get("raw_inputs", {}))


if __name__ == "__main__":
    unittest.main()
