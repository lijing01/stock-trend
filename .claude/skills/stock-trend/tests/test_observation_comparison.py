"""Pure observation-artifact comparison tests for daily review."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import observation_comparison as comparison


def _row(code, *, joined="2026-09-01", score=60.0, state="valid",
         quality=None, event=None, config_extra=None):
    config = {
        "code": code, "market": "SH" if code.startswith("6") else "SZ",
        "joined_date": joined, "entry_phase": "吸筹",
    }
    config.update(config_extra or {})
    quality = quality or {"eligible": True, "reasons": []}
    return {
        "code": code, "market": config["market"], "joined_date": joined,
        "normalized_config": config,
        "row_config_sha256": comparison.canonical_digest(config),
        "status": "ready", "raw_composite_score": score,
        "quality_adjusted_score": score,
        "quality_method": "scanner-data-quality/v1",
        "quality_digest": comparison.canonical_digest(quality),
        "data_quality": quality,
        "wyckoff": {
            "event_health": {"state": state},
            "confirmed_event": event or {},
        },
    }


def _artifact(day, rows, *, schema="yaml-observation-analysis/v2",
              model="observation-six-dimension/v1", provisional=False):
    configs = [row.get("normalized_config") for row in rows]
    return {
        "schema": schema, "data_date": day, "status": "ready",
        "provisional": provisional, "scoring_model_version": model,
        "evidence_cutoff_date": day,
        "normalized_config_sha256": comparison.canonical_digest(configs),
        "items": rows,
    }


class ObservationComparisonTests(unittest.TestCase):
    def test_v1_is_collection_only_and_does_not_claim_yaml_mutation(self):
        previous = {
            "schema": "yaml-observation-analysis/v1", "data_date": "2026-09-29",
            "items": [{"code": "600519"}, {"code": "000001"}],
        }
        current = _artifact("2026-09-30", [_row("600519"), _row("300750")])
        result = comparison.compare_observation_artifacts(
            current, previous, expected_previous_date="2026-09-29",
            as_of_date="2026-09-30")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["comparison_scope"], "collection_only")
        self.assertEqual(result["collection_changes"], {
            "added": [{"market": "SZ", "code": "300750"}],
            "removed": [{"market": "SZ", "code": "000001"}],
            "wording": "artifact_record_set",
        })
        self.assertEqual(result["rows"][0]["score_status"], "unavailable")
        self.assertEqual(result["rows"][0]["reason_code"], "legacy_collection_only")

    def test_rejects_non_adjacent_future_and_provisional_artifacts(self):
        previous = _artifact("2026-09-28", [_row("600519")])
        current = _artifact("2026-09-30", [_row("600519", score=61)])
        self.assertEqual(comparison.compare_observation_artifacts(
            current, previous, expected_previous_date="2026-09-29")["reason_code"],
            "adjacent_artifact_missing")
        previous["data_date"] = "2026-09-29"
        self.assertEqual(comparison.compare_observation_artifacts(
            current, previous, expected_previous_date="2026-09-29",
            as_of_date="2026-09-29")["reason_code"], "future_artifact")
        current["provisional"] = True
        self.assertEqual(comparison.compare_observation_artifacts(
            current, previous, expected_previous_date="2026-09-29",
            as_of_date="2026-09-30")["reason_code"], "incomplete_trading_day")

    def test_score_and_structure_guards_cover_version_config_reentry_and_quality(self):
        prior = _row("600519", score=60)
        current = _row("600519", score=70)
        base_previous = _artifact("2026-09-29", [prior])

        versioned = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [current], model="observation-six-dimension/v2"),
            base_previous, expected_previous_date="2026-09-29")
        self.assertEqual(versioned["rows"][0]["reason_code"], "model_version_mismatch")

        changed = _row("600519", score=70, config_extra={"entry_phase": "拉升"})
        configured = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [changed]), base_previous,
            expected_previous_date="2026-09-29")
        self.assertEqual(configured["rows"][0]["reason_code"], "row_config_changed")

        reentered = _row("600519", joined="2026-09-30", score=70)
        reentry = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [reentered]), base_previous,
            expected_previous_date="2026-09-29")
        self.assertEqual(reentry["rows"][0]["reason_code"], "code_reentered")

        different_quality = _row(
            "600519", score=70,
            quality={"eligible": True, "reasons": [], "coverage_factor": .8})
        quality = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [different_quality]), base_previous,
            expected_previous_date="2026-09-29")
        self.assertEqual(quality["rows"][0]["reason_code"], "quality_changed")

    def test_prioritized_structure_recovery_and_confirmed_evidence_events(self):
        previous_rows = [
            _row("600001", state="valid"),
            _row("600002", state="invalid"),
            {**_row("600003"), "status": "degraded", "raw_composite_score": None,
             "quality_adjusted_score": None},
            _row("600004"),
        ]
        current_rows = [
            _row("600001", state="invalid"),
            _row("600002", state="valid"),
            _row("600003"),
            _row("600004", event={"type": "lps", "status": "confirmed",
                                   "confirmation_date": "2026-09-30"}),
        ]
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", current_rows),
            _artifact("2026-09-29", previous_rows),
            expected_previous_date="2026-09-29")
        self.assertEqual([event["type"] for event in result["events"]], [
            "structure_invalidated", "structure_restored", "data_recovered",
            "confirmed_evidence",
        ])
        self.assertEqual(result["events"][0]["priority"], 10)
        self.assertEqual(result["events"][-1]["event_type"], "lps")

    def test_real_compact_confirmation_date_is_supported(self):
        row = _row("600519", event={"type": "lps", "status": "confirmed",
                                    "detected_date": "20260930", "event_date": "20260928"})
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [row]), _artifact("2026-09-29", [_row("600519")]),
            expected_previous_date="2026-09-29")
        self.assertEqual(result["events"][0]["type"], "confirmed_evidence")
        self.assertEqual(result["events"][0]["confirmation_date"], "2026-09-30")

    def test_quality_changes_block_score_but_preserve_proven_structure(self):
        prior = _row("600519", state="confirmed_holding")
        current = _row("600519", state="structure_invalidated",
                       quality={"eligible": False, "reasons": ["capital_missing"]})
        current["status"] = "degraded"
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [current]), _artifact("2026-09-29", [prior]),
            expected_previous_date="2026-09-29")
        self.assertEqual(result["rows"][0]["score_status"], "unavailable")
        self.assertEqual([event["type"] for event in result["events"]], ["structure_invalidated"])

    def test_unconfirmed_buy_point_is_not_missing_data_recovery(self):
        prior = {**_row("600519"), "status": "degraded", "reasons": ["未确认维科夫买点"]}
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [_row("600519")]), _artifact("2026-09-29", [prior]),
            expected_previous_date="2026-09-29")
        self.assertNotIn("data_recovered", [event["type"] for event in result["events"]])

    def test_real_health_states_unknown_and_invalidation(self):
        previous = _artifact("2026-09-29", [_row("600519", state="confirmed_holding")])
        for state, expected in (("failed_breakout", "structure_invalidated"),
                                ("structure_invalidated", "structure_invalidated"),
                                ("state_unknown", None), ("not_evaluated", None)):
            result = comparison.compare_observation_artifacts(
                _artifact("2026-09-30", [_row("600519", state=state)]), previous,
                expected_previous_date="2026-09-29")
            self.assertEqual([event["type"] for event in result["events"]],
                             [expected] if expected else [])

    def test_confirmed_event_age_is_not_a_new_event(self):
        event = {"type": "lps", "status": "confirmed", "event_date": "2026-09-25",
                 "detected_date": "2026-09-28", "range_id": "r1", "age_bars": 1}
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [_row("600519", event={**event, "age_bars": 2})]),
            _artifact("2026-09-29", [_row("600519", event=event)]),
            expected_previous_date="2026-09-29")
        self.assertEqual(result["events"], [])

    def test_no_recovery_event_on_reentry_and_no_future_confirmation(self):
        prior = {**_row("600519"), "status": "degraded"}
        current = _row("600519", joined="2026-09-30")
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [current]), _artifact("2026-09-29", [prior]),
            expected_previous_date="2026-09-29")
        self.assertEqual(result["events"], [])
        future = _row("600519", event={"type": "lps", "status": "confirmed",
                                       "confirmation_date": "2026-10-09"})
        result = comparison.compare_observation_artifacts(
            _artifact("2026-09-30", [future]), _artifact("2026-09-29", [_row("600519")]),
            expected_previous_date="2026-09-29")
        self.assertEqual(result["events"], [])

    def test_cutoff_and_model_version_required_for_full_comparison(self):
        prior = _artifact("2026-09-29", [_row("600519")])
        current = _artifact("2026-09-30", [_row("600519")])
        for key, value in (("evidence_cutoff_date", "2026-10-09"),
                           ("scoring_model_version", None)):
            result = comparison.compare_observation_artifacts(
                {**current, key: value}, prior, expected_previous_date="2026-09-29")
            self.assertEqual(result["events"], [])
            self.assertNotEqual(result["rows"][0]["score_status"] if result["rows"] else "unavailable", "comparable")

    def test_comparable_score_delta_is_exposed_without_inference(self):
        previous = _artifact("2026-09-29", [_row("600519", score=60)])
        current = _artifact("2026-09-30", [_row("600519", score=64.5)])
        result = comparison.compare_observation_artifacts(
            current, previous, expected_previous_date="2026-09-29")
        row = result["rows"][0]
        self.assertEqual(row["score_status"], "comparable")
        self.assertEqual(row["raw_score_delta"], 4.5)
        self.assertEqual(row["quality_adjusted_score_delta"], 4.5)
        self.assertNotIn("buy", str(result).lower())


if __name__ == "__main__":
    unittest.main()
