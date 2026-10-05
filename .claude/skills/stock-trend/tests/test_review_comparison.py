"""Stage-3 adjacent-session comparison and immutable snapshot tests."""

import copy
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analysis.review_comparison import (
    build_review_snapshot,
    load_eligible_snapshot,
    persist_review_snapshot,
    prepare_comparison,
)


COMPONENTS = ("index_trend", "volume", "breadth", "zt_emotion", "capital")
WEIGHTS = dict(zip(COMPONENTS, (0.25, 0.20, 0.25, 0.20, 0.10)))


def calendar(*days):
    return {
        "schema": "review-time/v1",
        "trading_dates": list(days),
        "coverage": {"start": days[0], "end": days[-1]},
        "source": "authoritative-test-calendar",
    }


def context(day, scores=(70, 60, 80, 50, 40), amount=20000, *, intraday=False):
    denominator = sum(WEIGHTS.values())
    components = {
        key: {"score": score, "data_status": "good"}
        for key, score in zip(COMPONENTS, scores)
    }
    explanation_components = []
    for key, score in zip(COMPONENTS, scores):
        evidence = {"completeness": "complete", "usage": "scorable",
                    "metric": key + "_metric", "provider": "test-provider",
                    "source_kind": "primary"}
        if key == "index_trend":
            evidence.update(expected_count=3, available_count=3,
                            index_codes=["000001.SH", "000300.SH", "399001.SZ"])
        explanation_components.append({
            "id": key, "score": score, "weight": WEIGHTS[key],
            "contribution": score * WEIGHTS[key] / denominator,
            "evidence": evidence,
        })
    score = round(sum(s * WEIGHTS[k] for k, s in zip(COMPONENTS, scores)), 1)
    qualification = {"status": "qualified", "eligible": True,
                     "expected_date": day, "date_alignment": "matched"}
    return {
        "data_date": day,
        # Production keeps this display timestamp naive; prepare_comparison
        # must freeze with its explicit aware as_of instead.
        "generated_at": day + " 16:00:00",
        "model_version": "market-regime-score/v1",
        "methodology_version": "daily-review-close/v1",
        "intraday": intraday,
        "regime": {"score": score, "normalization_denominator": denominator,
                   "data_quality": "good"},
        "components": components,
        "amount_yi": amount,
        "amount_evidence": {"data_date": day, "date_origin": "provider",
                            "completeness": "complete", "usage": "scorable",
                            "per_index": {"000001.SH": {}, "399106.SZ": {}}},
        "detail_inputs": {
            "index_trend": {"indices": [
                {"code": "000001.SH"}, {"code": "000300.SH"},
                {"code": "399001.SZ"}]},
            "volume": {"coverage": "Shanghai+Shenzhen", "history_sample_count": 20},
            "breadth": {"coverage": "eastmoney_region_boards", "industry_count": 31},
            "zt_emotion": {"history_sample_count": 20},
        },
        "conclusion_qualification": qualification,
        "market_explanation": {
            "mode": "intraday" if intraday else "close",
            "score": score,
            "normalization_denominator": denominator,
            "reconciliation": "matched",
            "components": explanation_components,
            "conclusion_qualification": qualification,
        },
    }


class ReviewComparisonTests(unittest.TestCase):
    def test_complete_adjacent_close_reconciles_contributions_and_amount(self):
        current = context("2026-09-30", (80, 60, 70, 55, 45), 24000)
        previous = build_review_snapshot(
            context("2026-09-29", (70, 55, 75, 50, 40), 20000),
            frozen_at="2026-09-29T16:00:00+08:00")
        result = prepare_comparison(
            current, calendar("2026-09-28", "2026-09-29", "2026-09-30"),
            as_of="2026-09-30T16:30:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": previous})

        self.assertEqual(result["status"], "comparable")
        self.assertEqual(result["prior_session_date"], "2026-09-29")
        self.assertEqual(result["score"]["status"], "comparable")
        self.assertEqual(result["amount"]["change"], 4000.0)
        self.assertEqual(result["amount"]["percent_change"], 20.0)
        self.assertEqual(result["contribution_reconciliation"]["status"], "matched")
        self.assertAlmostEqual(
            sum(row["change"] for row in result["main_reasons"]),
            result["contribution_reconciliation"]["model_score_change"])
        self.assertAlmostEqual(
            result["score"]["change"],
            result["contribution_reconciliation"]["model_score_change"]
            + result["contribution_reconciliation"]["rounding_residual"])
        magnitudes = [abs(row["change"]) for row in result["main_reasons"]]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))

    def test_never_skips_missing_adjacent_session(self):
        older = build_review_snapshot(context("2026-09-28"),
                                      frozen_at="2026-09-28T16:00:00+08:00")
        result = prepare_comparison(
            context("2026-09-30"),
            calendar("2026-09-28", "2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-28": older})
        self.assertEqual(result["prior_session_date"], "2026-09-29")
        self.assertIsNone(result["actual_baseline_date"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("adjacent_snapshot_missing", result["reasons"])

    def test_version_denominator_coverage_and_qualification_are_strict(self):
        base = context("2026-09-29")
        mutations = {
            "model_version_mismatch": lambda value: value.update(model_version="v0"),
            "normalization_denominator_mismatch": lambda value: value["market_explanation"].update(
                normalization_denominator=0.9),
            "indicator_coverage_mismatch": lambda value: value["market_explanation"]["components"][0][
                "evidence"].update(available_count=2),
            "previous_close_unqualified": lambda value: value["conclusion_qualification"].update(
                eligible=False, status="partial"),
        }
        for expected_reason, mutate in mutations.items():
            with self.subTest(expected_reason):
                changed = copy.deepcopy(base)
                mutate(changed)
                if expected_reason == "previous_close_unqualified":
                    changed["market_explanation"]["conclusion_qualification"] = changed[
                        "conclusion_qualification"]
                previous = build_review_snapshot(
                    changed, frozen_at="2026-09-29T16:00:00+08:00")
                result = prepare_comparison(
                    context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
                    as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
                    source_history={"2026-09-29": previous})
                self.assertNotEqual(result["score"]["status"], "comparable")
                self.assertIn(expected_reason, result["score"]["reasons"])

    def test_legacy_history_can_compare_amount_but_not_model_scores(self):
        legacy = {
            "date": "2026-09-29", "regime_score": 70,
            "frozen_at": "2026-09-29T16:00:00+08:00",
            "components": dict.fromkeys(COMPONENTS, 70), "amount_yi": 20000,
            "amount_evidence": {"data_date": "2026-09-29", "date_origin": "provider",
                                "completeness": "complete", "usage": "scorable",
                                "per_index": {"000001.SH": {}, "399106.SZ": {}}},
        }
        result = prepare_comparison(
            context("2026-09-30", amount=22000),
            calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": legacy})
        self.assertEqual(result["score"]["status"], "unavailable")
        self.assertIn("model_version_missing", result["score"]["reasons"])
        self.assertEqual(result["amount"]["status"], "comparable")
        self.assertEqual(result["amount"]["percent_change"], 10.0)

    def test_intraday_exposes_previous_close_reference_without_direction_claim(self):
        previous = build_review_snapshot(context("2026-09-29"),
                                         frozen_at="2026-09-29T16:00:00+08:00")
        result = prepare_comparison(
            context("2026-09-30", intraday=True),
            calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T10:30:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": previous})
        self.assertEqual(result["status"], "reference_only")
        self.assertEqual(result["score"]["status"], "reference_only")
        self.assertIsNone(result["score"]["change"])
        self.assertEqual(result["previous_close_reference"]["date"], "2026-09-29")
        self.assertEqual(result["main_reasons"], [])

    def test_explicit_intraday_flag_dominates_stale_close_explanation(self):
        current = context("2026-09-30", intraday=True)
        current["market_explanation"]["mode"] = "close"
        snapshot = build_review_snapshot(
            current, frozen_at="2026-09-30T16:00:00+08:00")
        self.assertEqual(snapshot["mode"], "intraday")
        self.assertFalse(snapshot["eligible_close"])

    def test_raw_component_precision_drives_model_contributions(self):
        current = context("2026-09-30", scores=(70.27, 60.19, 80.33, 50.44, 40.51))
        for row in current["market_explanation"]["components"]:
            row["score"] = round(row["score"], 1)
        snapshot = build_review_snapshot(
            current, frozen_at="2026-09-30T16:00:00+08:00")
        self.assertEqual(snapshot["components"]["index_trend"]["score"], 70.27)
        self.assertAlmostEqual(snapshot["components"]["index_trend"]["contribution"],
                               70.27 * 0.25)

    def test_scope_metadata_changes_block_score_comparison(self):
        changes = (
            lambda value: value["market_explanation"]["components"][0]["evidence"].update(
                provider="other-provider"),
            lambda value: value["detail_inputs"]["breadth"].update(industry_count=30),
            lambda value: value["detail_inputs"]["volume"].update(history_sample_count=19),
            lambda value: value["detail_inputs"]["zt_emotion"].update(history_sample_count=4),
        )
        for change in changes:
            with self.subTest(change=change):
                previous_ctx = context("2026-09-29")
                change(previous_ctx)
                previous = build_review_snapshot(
                    previous_ctx, frozen_at="2026-09-29T16:00:00+08:00")
                result = prepare_comparison(
                    context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
                    as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
                    source_history={"2026-09-29": previous})
                self.assertIn("indicator_coverage_mismatch", result["score"]["reasons"])

    def test_amount_requires_exact_two_market_coverage_and_close_mode(self):
        for mutate in (
            lambda value: value["amount_evidence"].update(per_index={}),
            lambda value: value["amount_evidence"].update(
                per_index={"000001.SH": {}, "399106.SZ": {}, "000300.SH": {}}),
        ):
            with self.subTest(mutate=mutate):
                previous_ctx = context("2026-09-29")
                mutate(previous_ctx)
                result = prepare_comparison(
                    context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
                    as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
                    source_history={"2026-09-29": previous_ctx})
                self.assertEqual(result["amount"]["status"], "unavailable")
                self.assertIn("amount_range_unqualified", result["amount"]["reasons"])

        legacy = context("2026-09-29")
        legacy.pop("model_version")
        legacy["intraday"] = True
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": legacy})
        self.assertNotEqual(result["amount"]["status"], "comparable")

    def test_calendar_must_be_authoritative_cover_current_and_be_adjacent(self):
        for bad_calendar, reason in (
            ({"schema": "wrong", "trading_dates": ["2026-09-30"]}, "calendar_invalid"),
            (calendar("2026-09-29"), "calendar_current_session_missing"),
            (calendar("2026-09-30"), "previous_session_outside_calendar_coverage"),
        ):
            with self.subTest(reason):
                result = prepare_comparison(
                    context("2026-09-30"), bad_calendar,
                    as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused")
                self.assertEqual(result["status"], "unavailable")
                self.assertIn(reason, result["reasons"])


class ReviewSnapshotTests(unittest.TestCase):
    def test_historical_load_scans_behind_future_latest_pointer(self):
        with tempfile.TemporaryDirectory() as root:
            older = build_review_snapshot(
                context("2026-09-30"), frozen_at="2026-09-30T16:00:00+08:00")
            newer = build_review_snapshot(
                context("2026-09-30", scores=(90, 80, 70, 60, 50)),
                frozen_at="2026-09-30T18:00:00+08:00")
            persist_review_snapshot(older, root)
            persist_review_snapshot(newer, root)
            loaded = load_eligible_snapshot(
                "2026-09-30", root, as_of="2026-09-30T17:00:00+08:00")
            self.assertEqual(loaded["content_sha256"], older["content_sha256"])

    def test_legacy_amount_requires_verified_freeze_or_provider_timestamp(self):
        base = {
            "date": "2026-09-29", "regime_score": 70,
            "components": dict.fromkeys(COMPONENTS, 70), "amount_yi": 20000,
            "amount_evidence": {"data_date": "2026-09-29", "date_origin": "provider",
                                "completeness": "complete", "usage": "scorable",
                                "per_index": {"000001.SH": {}, "399106.SZ": {}}},
        }
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": base})
        self.assertIn("freeze_unknown", result["amount"]["reasons"])

        verified = copy.deepcopy(base)
        verified["amount_evidence"].update(
            provider="test-provider", fetched_at="2026-09-29T16:00:00+08:00")
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": verified})
        self.assertEqual(result["amount"]["status"], "comparable")

        preclose = copy.deepcopy(base)
        preclose["amount_evidence"].update(
            provider="test-provider", fetched_at="2026-09-29T15:09:59+08:00")
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": preclose})
        self.assertIn("legacy_close_not_completed", result["amount"]["reasons"])

        future = copy.deepcopy(verified)
        future["frozen_at"] = "2026-10-01T16:00:00+08:00"
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": future})
        self.assertIn("adjacent_snapshot_missing", result["reasons"])

    def test_persistence_error_does_not_block_comparison(self):
        previous = build_review_snapshot(
            context("2026-09-29"), frozen_at="2026-09-29T16:00:00+08:00")
        with patch("analysis.review_comparison.persist_review_snapshot",
                   side_effect=OSError("disk full")):
            result = prepare_comparison(
                context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
                as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
                persist=True, source_history={"2026-09-29": previous})
        self.assertEqual(result["score"]["status"], "comparable")
        self.assertEqual(result["snapshot_persistence"],
                         {"status": "error", "reason": "OSError"})

    def test_close_eligibility_requires_shanghai_1510_completion(self):
        early = build_review_snapshot(
            context("2026-09-30"), frozen_at="2026-09-30T15:09:59+08:00")
        complete = build_review_snapshot(
            context("2026-09-30"), frozen_at="2026-09-30T07:10:00+00:00")
        self.assertFalse(early["close_completed"])
        self.assertFalse(early["eligible_close"])
        self.assertTrue(complete["close_completed"])
        self.assertTrue(complete["eligible_close"])

    def test_prepare_accepts_actual_freeze_after_baseline_cutoff(self):
        previous = build_review_snapshot(
            context("2026-09-29"), frozen_at="2026-09-29T16:00:00+08:00")
        result = prepare_comparison(
            context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
            as_of="2026-09-30T16:00:00+08:00",
            frozen_at="2026-09-30T16:05:00+08:00", cache_dir="/unused",
            source_history={"2026-09-29": previous})
        self.assertEqual(result["score"]["status"], "comparable")

    def test_crafted_unqualified_or_unreconciled_snapshot_cannot_compare(self):
        for key, value, reason in (
            ("eligible_close", False, "previous_close_not_eligible"),
            ("reconciliation", "mismatch", "snapshot_reconciliation_unqualified"),
        ):
            with self.subTest(key=key):
                previous = build_review_snapshot(
                    context("2026-09-29"), frozen_at="2026-09-29T16:00:00+08:00")
                previous[key] = value
                previous.pop("content_sha256")
                from core.recommendation_snapshot import content_sha256
                previous["content_sha256"] = content_sha256(previous)
                result = prepare_comparison(
                    context("2026-09-30"), calendar("2026-09-29", "2026-09-30"),
                    as_of="2026-09-30T17:00:00+08:00", cache_dir="/unused",
                    source_history={"2026-09-29": previous})
                self.assertIn(reason, result["score"]["reasons"])

    def test_content_addressed_history_and_eligible_pointer_are_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = build_review_snapshot(
                context("2026-09-30"), frozen_at="2026-09-30T16:00:00+08:00")
            results = []
            threads = [threading.Thread(
                target=lambda: results.append(persist_review_snapshot(snapshot, root)))
                for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            directory = Path(root) / "review_snapshots" / "2026-09-30"
            artifacts = [path for path in directory.glob("*.json") if path.name != "eligible.json"]
            self.assertEqual(len(artifacts), 1)
            self.assertTrue(all(item["status"] in {"created", "unchanged"} for item in results))
            loaded = load_eligible_snapshot(
                "2026-09-30", root, as_of="2026-09-30T17:00:00+08:00")
            self.assertEqual(loaded["content_sha256"], snapshot["content_sha256"])
            pointer = json.loads((directory / "eligible.json").read_text(encoding="utf-8"))
            self.assertEqual(pointer["content_sha256"], snapshot["content_sha256"])

            revised = build_review_snapshot(
                context("2026-09-30", scores=(90, 80, 70, 60, 50)),
                frozen_at="2026-09-30T16:05:00+08:00")
            persist_review_snapshot(revised, root)
            artifacts = [path for path in directory.glob("*.json") if path.name != "eligible.json"]
            self.assertEqual(len(artifacts), 2)
            self.assertTrue((directory / f"{snapshot['content_sha256']}.json").exists())

    def test_ineligible_does_not_move_pointer_and_future_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            qualified = build_review_snapshot(
                context("2026-09-30"), frozen_at="2026-09-30T16:00:00+08:00")
            persist_review_snapshot(qualified, root)
            partial_ctx = context("2026-09-30")
            partial_ctx["conclusion_qualification"].update(eligible=False, status="partial")
            partial_ctx["market_explanation"]["conclusion_qualification"] = partial_ctx[
                "conclusion_qualification"]
            ineligible = build_review_snapshot(
                partial_ctx, frozen_at="2026-09-30T16:10:00+08:00")
            result = persist_review_snapshot(ineligible, root)
            self.assertEqual(result["status"], "stored_ineligible")
            loaded = load_eligible_snapshot(
                "2026-09-30", root, as_of="2026-09-30T17:00:00+08:00")
            self.assertEqual(loaded["content_sha256"], qualified["content_sha256"])
            self.assertIsNone(load_eligible_snapshot(
                "2026-09-30", root, as_of="2026-09-30T15:59:59+08:00"))

    def test_source_digest_is_bound_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = context("2026-09-30")
            snapshot = build_review_snapshot(
                ctx, frozen_at="2026-09-30T16:00:00+08:00")
            self.assertTrue(snapshot["source_digest"])
            persist_review_snapshot(snapshot, root)
            artifact = (Path(root) / "review_snapshots" / "2026-09-30"
                        / f"{snapshot['content_sha256']}.json")
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            payload["score"] = 1
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(load_eligible_snapshot(
                "2026-09-30", root, as_of="2026-09-30T17:00:00+08:00"))


if __name__ == "__main__":
    unittest.main()
