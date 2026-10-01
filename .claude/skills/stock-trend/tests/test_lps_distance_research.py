"""Adversarial tests for immutable LPS-distance shadow research."""

import copy
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.lps_distance_research import (
    DEFAULT_CONTRACT_ID, POPULATION_KIND, _block_bootstrap,
    classify_lps_record, distance_bin,
    evaluate_distance_days, run_daily_distance, save_artifact,
)
from core.candidate_research_snapshot import build_research_snapshot
from core.recommendation_snapshot import content_sha256
from scans.daily_candidates import classify_candidates, select_candidate_pool


DAY = "2026-09-21"


def candidate(code, priority, distance, *, lps=True, fetched="20260921-152000",
              cache_used=False, floor=9.0):
    trigger, atr = 10.0, 1.0
    close = trigger + distance * atr
    timing = {"sub_phase": "lps" if lps else "spring", "signal_status": "confirmed",
              "signal_age_bars": 1, "current_state": "confirmed_holding",
              "current_close": close, "trigger_close": trigger, "current_atr": atr,
              "trigger_extension_atr": round(distance, 4),
              "trigger_extension_pct": round(close / trigger - 1, 4),
              "status": "entry_fresh", "executable": True}
    return {"code": code, "ts_code": f"{code}.SH", "market": "SH",
            "raw_composite_score": priority, "composite_score": priority,
            "quality_adjusted_score": priority, "execution_priority_score": priority,
            "buy_point_priority_bonus": 0, "data_quality": {"eligible": True,
                "coverage_factor": 1.0, "freshness_factor": 1.0,
                "dimensions": {"kline": {"data_date": DAY}}},
            "source_evidence": {"kline": {"source": "fixture", "data_date": DAY,
                "fetched_at": fetched, "cache_used": cache_used, "status": "live_success"}},
            "sector_actionable": True, "score_eligible": True,
            "wyckoff": {"sub_phase": timing["sub_phase"], "signal_status": "confirmed",
                        "entry_timing": timing,
                        "event_health": {"state": "confirmed_holding", "structural_floor": floor}}}


def fixture():
    rows = [candidate("600001", 90, .8), candidate("600003", 85, .4, lps=False),
            candidate("600002", 80, .1)]
    policy = {"mode": "actionable", "max_recommendations": 3}
    selected = select_candidate_pool(copy.deepcopy(rows), 3, 50, policy, {})
    buckets = classify_candidates(selected, policy)
    formal = {"recommendation_date": DAY, "snapshot_type": "formal",
              "model_version": "daily-candidates/v4", "policy": policy,
              "candidates": selected, "buckets": buckets}
    official = {"content": formal, "content_sha256": content_sha256(formal)}
    research = build_research_snapshot(
        rows, buckets, DAY, policy, {"score": 70}, [], 50,
        official_tracking={"status": "created", "content_sha256": official["content_sha256"]},
        parameter_summary={"top": 3, "min_score": 50, "buy_point_priority_bonus": {}},
        selection_scope={"mode": "explicit_codes", "codes": [row["code"] for row in rows]},
        decision_at="2026-09-21T16:00:00+08:00")
    return research, official


def resign(research):
    content = research["content"]
    content["input_manifest"]["inputs"]["candidate_records"] = copy.deepcopy(content["records"])
    content["input_manifest"]["inputs"]["selection_scope"] = copy.deepcopy(content["selection_scope"])
    content["input_manifest"]["input_sha256"] = content_sha256(content["input_manifest"]["inputs"])
    research["content_sha256"] = content_sha256(content)


def sessions(start, count):
    result, current = [], date.fromisoformat(start)
    while len(result) < count:
        if current.weekday() < 5: result.append(current.isoformat())
        current += timedelta(days=1)
    return result


class LpsDistanceResearchTests(unittest.TestCase):
    def test_distance_boundaries_and_negative(self):
        self.assertEqual([distance_bin(v) for v in (-.01, 0, .5, .50001, 1, 1.01, None)],
                         ["negative", "0_to_0_5", "0_to_0_5", "0_5_to_1",
                          "0_5_to_1", "over_1", "insufficient"])

    def test_cached_midday_is_not_final_close_even_if_snapshot_is_late(self):
        research, _ = fixture()
        row = research["content"]["records"][0]
        row["candidate"]["source_evidence"]["kline"].update(
            fetched_at="20260921-133523", cache_used=True, status="cached_valid")
        row["captured_at"] = "2026-09-21T17:00:00+08:00"
        evidence = classify_lps_record(row, DAY)
        self.assertFalse(evidence["eligible"])
        self.assertIn("final_close_evidence_missing", evidence["reasons"])

    def test_explicit_close_without_availability_or_decision_time_is_rejected(self):
        research, _ = fixture(); row = research["content"]["records"][0]
        row["candidate"]["source_evidence"]["kline"].update(
            final_close_confirmed=True, fetched_at=None)
        self.assertFalse(classify_lps_record(row, DAY)["eligible"])
        row["candidate"]["source_evidence"]["kline"]["fetched_at"] = "20260921-152000"
        row["decision_at"] = None
        self.assertFalse(classify_lps_record(row, DAY)["eligible"])

    def test_only_eligible_lps_slots_are_permuted(self):
        research, official = fixture()
        result = run_daily_distance(research, official)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["baseline"]["top_k"]["3"], ["600001", "600003", "600002"])
        self.assertEqual(result["treatment"]["top_k"]["3"], ["600002", "600003", "600001"])

    def test_negative_and_insufficient_slots_stay_fixed(self):
        research, official = fixture()
        records = {row["code"]: row for row in research["content"]["records"]}
        timing = records["600001"]["candidate"]["wyckoff"]["entry_timing"]
        timing.update(current_close=9.8, trigger_extension_atr=-.2, trigger_extension_pct=-.02)
        records["600001"]["candidate"]["wyckoff"]["event_health"]["structural_floor"] = 9.0
        resign(research)
        result = run_daily_distance(research, official)
        self.assertEqual(result["treatment"]["top_k"]["3"], result["baseline"]["top_k"]["3"])

    def test_hash_and_scope_tampering_fail_closed(self):
        research, official = fixture()
        research["content"]["records"][0]["candidate"]["name"] = "tampered"
        self.assertEqual(run_daily_distance(research, official)["reason"], "snapshot_hash_mismatch")
        research, official = fixture()
        research["content"]["selection_scope"]["codes_sha256"] = "bad"
        resign(research)
        self.assertEqual(run_daily_distance(research, official)["status"], "scope_unverified")

    def test_outcomes_require_identity_cutoff_population_and_calendar(self):
        research, official = fixture(); daily = run_daily_distance(research, official)
        calendar = sessions(DAY, 21)
        outcomes = []
        for code, record_id in daily["record_ids"].items():
            outcomes.append({"record_id": record_id, "code": code, "market": "SH",
                "recommendation_date": DAY, "research_snapshot_sha256": daily["research_snapshot_sha256"],
                "contract_id": DEFAULT_CONTRACT_ID, "population_kind": POPULATION_KIND,
                "evaluation_as_of": "2026-10-30", "market_sessions": calendar,
                "windows": {"20": {"status": "complete", "entry_date": calendar[1],
                    "exit_date": calendar[20], "signal_return": .02, "hs300_alpha": .01, "mae": -.03}}})
        early = evaluate_distance_days([daily], outcomes, "2026-10-29")
        self.assertEqual(early["windows"]["20"]["3"]["paired_dates"], 0)
        ready = evaluate_distance_days([daily], outcomes, "2026-10-30")
        self.assertEqual(ready["windows"]["20"]["3"]["paired_dates"], 1)
        wrong = copy.deepcopy(outcomes); wrong[0]["population_kind"] = "other"
        self.assertEqual(evaluate_distance_days([daily], wrong, "2026-10-30")
                         ["windows"]["20"]["3"]["paired_dates"], 0)
        malformed = copy.deepcopy(outcomes); malformed[0]["windows"]["20"]["exit_date"] = calendar[19]
        self.assertEqual(evaluate_distance_days([daily], malformed, "2026-10-30")
                         ["windows"]["20"]["3"]["paired_dates"], 0)

    def test_conflicting_identity_and_overlapping_signal_are_not_double_counted(self):
        research, official = fixture(); daily = run_daily_distance(research, official)
        calendar = sessions(DAY, 21)
        row = {"record_id": daily["record_ids"]["600001"], "code": "600001", "market": "SH",
               "recommendation_date": DAY, "research_snapshot_sha256": daily["research_snapshot_sha256"],
               "contract_id": DEFAULT_CONTRACT_ID, "population_kind": POPULATION_KIND,
               "evaluation_as_of": "2026-10-30", "market_sessions": calendar,
               "windows": {"20": {"status": "complete", "entry_date": calendar[1],
                           "exit_date": calendar[20], "signal_return": .02, "hs300_alpha": .01, "mae": -.03}}}
        conflict = copy.deepcopy(row); conflict["windows"]["20"]["hs300_alpha"] = .02
        evaluated = evaluate_distance_days([daily], [row, conflict], "2026-10-30")
        self.assertEqual(evaluated["duplicate_outcome_identities"], 1)
        self.assertEqual(evaluated["windows"]["20"]["1"]["paired_dates"], 0)

    def test_save_is_idempotent_and_conflict_safe(self):
        artifact = {"schema_version": "x", "recommendation_date": DAY, "input_sha256": "abc"}
        with tempfile.TemporaryDirectory() as root:
            first = save_artifact(artifact, root); second = save_artifact(artifact, root)
            self.assertEqual(first, second)
            changed = dict(artifact, value=1)
            with self.assertRaisesRegex(ValueError, "artifact_conflict"):
                save_artifact(changed, root)

    def test_empty_dates_are_not_opportunities(self):
        empty = {"status": "completed", "recommendation_date": DAY,
                 "research_snapshot_sha256": "snap", "definition": {
                     "evaluation_contract_id": DEFAULT_CONTRACT_ID}, "record_ids": {}, "records": [],
                 "baseline": {"buckets": {}, "top_k": {"1": [], "3": [], "5": []}},
                 "treatment": {"buckets": {}, "top_k": {"1": [], "3": [], "5": []}}}
        result = evaluate_distance_days([empty], [], DAY)
        primary = result["windows"]["20"]["3"]
        self.assertEqual(primary["frozen_opportunity_dates"], 0)
        self.assertEqual(primary["empty_recommendation_dates"], [DAY])
        self.assertIsNone(primary["coverage_rate"])
        self.assertEqual(primary["all_frozen_date_coverage"], 0)

    def test_later_overlap_never_reuses_earlier_anchor_return(self):
        calendar = sessions(DAY, 8)
        days = [DAY, calendar[1]]
        daily, outcomes = [], []
        for index, signal_day in enumerate(days):
            record_id, snapshot_hash = f"r{index}", f"s{index}"
            top = {"1": ["600001"], "3": ["600001"], "5": ["600001"]}
            daily.append({"status": "completed", "recommendation_date": signal_day,
                "research_snapshot_sha256": snapshot_hash,
                "definition": {"evaluation_contract_id": DEFAULT_CONTRACT_ID},
                "record_ids": {"600001": record_id},
                "records": [{"record_id": record_id, "code": "600001", "market": "SH",
                    "is_lps": True, "eligible": True, "distance_atr": .2,
                    "distance_bin": "0_to_0_5", "formal_bucket": "actionable",
                    "sector_actionable": True, "signal_age_bars": 1}],
                "baseline": {"buckets": {"actionable": ["600001"], "waiting_trigger": []},
                             "top_k": copy.deepcopy(top)},
                "treatment": {"buckets": {"actionable": ["600001"], "waiting_trigger": []},
                              "top_k": copy.deepcopy(top)}})
            entry_index = index + 1
            outcomes.append({"record_id": record_id, "code": "600001", "market": "SH",
                "recommendation_date": signal_day, "research_snapshot_sha256": snapshot_hash,
                "contract_id": DEFAULT_CONTRACT_ID, "population_kind": POPULATION_KIND,
                "evaluation_as_of": calendar[-1], "market_sessions": calendar,
                "windows": {"5": {"status": "complete", "entry_date": calendar[entry_index],
                    "exit_date": calendar[entry_index + 4], "signal_return": .01 + index,
                    "hs300_alpha": .01 + index, "mae": -.02}}})
        result = evaluate_distance_days(daily, outcomes, calendar[-1])
        report = result["windows"]["5"]["1"]
        self.assertEqual(report["paired_dates"], 1)
        self.assertEqual(report["metrics"]["baseline"]["event_count"], 1)
        self.assertAlmostEqual(report["metrics"]["baseline"]["mean_hs300_alpha"], .01)

    def test_bootstrap_requires_consecutive_frozen_sessions(self):
        calendar = sessions(DAY, 8)
        consecutive = [{"date": day, "alpha_delta": .01} for day in calendar[:5]]
        gapped = [*consecutive[:2], *consecutive[3:], {"date": calendar[5], "alpha_delta": .01}]
        self.assertIsNotNone(_block_bootstrap(consecutive, calendar, draws=20))
        self.assertIsNone(_block_bootstrap(gapped, calendar, draws=20))
        longer = sessions(DAY, 12)
        two_runs = [{"date": day, "alpha_delta": .01} for day in longer if day != longer[5]]
        self.assertTrue(_block_bootstrap(two_runs, longer, draws=20)
                        ["preregistered_method_satisfied"])
        isolated_loss = [*consecutive, {"date": longer[7], "alpha_delta": -.9}]
        self.assertIsNone(_block_bootstrap(isolated_loss, longer, draws=20))

    def test_loader_conflicts_remain_conflicts(self):
        from analysis.lps_distance_research import _outcome_index
        row = {"record_id": "r", "research_snapshot_sha256": "s",
               "contract_id": DEFAULT_CONTRACT_ID, "population_kind": POPULATION_KIND,
               "evaluation_as_of": DAY, "point_in_time_status": "evaluation_conflict"}
        indexed, conflicts = _outcome_index([row], DAY, DEFAULT_CONTRACT_ID)
        self.assertFalse(indexed)
        self.assertIn(("r", "s", DEFAULT_CONTRACT_ID, POPULATION_KIND), conflicts)


def run_lps_distance_research_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LpsDistanceResearchTests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
