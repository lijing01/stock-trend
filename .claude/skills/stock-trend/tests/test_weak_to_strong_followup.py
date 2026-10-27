#!/usr/bin/env python3
"""Tests for weak-to-strong continuous follow-up classification."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.weak_to_strong_followup import (  # noqa: E402
    build_follow_up_baseline,
    evaluate_follow_up,
)


def current(state="WAIT", cycle_id="", *, ready=True, date="2026-10-28",
            basis="qfq", confirmed=True):
    return {
        "as_of_date": date, "state": state, "cycle_id": cycle_id,
        "evidence_status": "ready" if ready else "data_insufficient",
        "kline_evidence": {
            "basis": basis,
            "close_confirmation": {"confirmed": confirmed, "source": "fixture"},
        },
    }


class WeakToStrongFollowUpTests(unittest.TestCase):
    def confirmed_baseline(self):
        return build_follow_up_baseline({
            "code": "600001", "ts_code": "600001.SH", "name": "测试股",
            "state": "BREAKOUT_CONFIRMED", "signal_date": "2026-10-27",
            "cycle_id": "cycle-a", "H": 10.0, "L": 9.5, "Ct": 10.2,
            "kline_evidence": {"basis": "qfq"}}, "2026-10-27")

    def test_confirmed_signal_enhances_continues_and_invalidates_at_boundaries(self):
        baseline = self.confirmed_baseline()
        enhanced = evaluate_follow_up(baseline, current(), latest_close=10.3,
                                      data_date="2026-10-28")
        continued = evaluate_follow_up(baseline, current(), latest_close=10.0,
                                       data_date="2026-10-28")
        invalid = evaluate_follow_up(baseline, current(), latest_close=9.99,
                                     data_date="2026-10-28")
        self.assertEqual(enhanced["follow_up_status"], "ENHANCED")
        self.assertEqual(enhanced["latest_valid_close"], 10.3)
        self.assertEqual(continued["follow_up_status"], "CONTINUED")
        self.assertTrue(continued["active"])
        self.assertEqual(invalid["follow_up_status"], "INVALIDATED")
        self.assertFalse(invalid["active"])

    def test_incomplete_evidence_preserves_previous_valid_close(self):
        baseline = self.confirmed_baseline()
        result = evaluate_follow_up(baseline, current(ready=False), latest_close=9.0,
                                    data_date="2026-10-28")
        self.assertEqual(result["follow_up_status"], "PENDING_REVIEW")
        self.assertEqual(result["latest_valid_close"], 10.2)
        self.assertIn("current_evidence_incomplete", result["reason_codes"])

    def test_unconfirmed_basis_change_and_date_mismatch_are_pending(self):
        baseline = self.confirmed_baseline()
        result = evaluate_follow_up(
            baseline, current(date="2026-10-27", basis="hfq", confirmed=False),
            latest_close=9.0, data_date="2026-10-28")
        self.assertEqual(result["follow_up_status"], "PENDING_REVIEW")
        for reason in ("close_unconfirmed", "data_date_mismatch", "adjustment_basis_changed"):
            self.assertIn(reason, result["reason_codes"])

    def test_new_cycle_does_not_overwrite_confirmed_follow_up(self):
        result = evaluate_follow_up(
            self.confirmed_baseline(), current("SUPPLY_DRY", "cycle-b"),
            latest_close=10.4, data_date="2026-10-28")
        self.assertEqual(result["follow_up_status"], "PENDING_REVIEW")
        self.assertEqual(result["reason_codes"], ["new_cycle_replaced_original"])

    def test_missing_adjustment_basis_does_not_trigger_invalidation(self):
        result = evaluate_follow_up(self.confirmed_baseline(), current(basis=""),
                                    latest_close=9.0, data_date="2026-10-28")
        self.assertEqual(result["follow_up_status"], "PENDING_REVIEW")
        self.assertIn("adjustment_basis_missing", result["reason_codes"])

    def test_supply_dry_requires_same_cycle(self):
        baseline = build_follow_up_baseline({
            "code": "600002", "state": "SUPPLY_DRY", "cycle_id": "cycle-w",
            "H": 8.0, "kline_evidence": {"basis": "qfq"}}, "2026-10-27",
            latest_close=7.8)
        cases = [("BREAKOUT_CONFIRMED", "ENHANCED"),
                 ("SUPPLY_DRY", "CONTINUED"),
                 ("INVALID", "INVALIDATED"),
                 ("PULLBACK", "PENDING_REVIEW")]
        for state, expected in cases:
            with self.subTest(state=state):
                result = evaluate_follow_up(
                    baseline, current(state, "cycle-w"), latest_close=8.1,
                    data_date="2026-10-28")
                self.assertEqual(result["follow_up_status"], expected)
        unlinked = evaluate_follow_up(
            baseline, current("BREAKOUT_CONFIRMED", "cycle-other"),
            latest_close=8.1, data_date="2026-10-28")
        self.assertEqual(unlinked["follow_up_status"], "PENDING_REVIEW")

    def test_supply_dry_breakout_transitions_to_confirmed_follow_up(self):
        baseline = build_follow_up_baseline({
            "code": "600002", "state": "SUPPLY_DRY", "cycle_id": "cycle-w",
            "H": 8.0, "kline_evidence": {"basis": "qfq"}}, "2026-10-27",
            latest_close=7.8)
        breakout = current("BREAKOUT_CONFIRMED", "cycle-w")
        breakout["signal_date"] = "2026-10-28"
        day_one = evaluate_follow_up(baseline, breakout, latest_close=8.1,
                                     data_date="2026-10-28")
        self.assertEqual(day_one["origin_state"], "BREAKOUT_CONFIRMED")
        self.assertEqual(day_one["initial_origin_state"], "SUPPLY_DRY")
        self.assertEqual(day_one["origin_signal_date"], "2026-10-28")
        next_baseline = build_follow_up_baseline(day_one, "2026-10-28")
        self.assertEqual(next_baseline["previous_close"], 8.1)
        day_two = evaluate_follow_up(
            next_baseline, current("WAIT", "cycle-w", date="2026-10-29"),
            latest_close=8.2, data_date="2026-10-29")
        self.assertEqual(day_two["follow_up_status"], "ENHANCED")

    def test_result_seeds_next_day_and_invalidated_exits(self):
        day_one = evaluate_follow_up(self.confirmed_baseline(), current(),
                                     latest_close=10.3, data_date="2026-10-28")
        next_baseline = build_follow_up_baseline(day_one, "2026-10-28")
        self.assertEqual(next_baseline["previous_close"], 10.3)
        self.assertEqual(next_baseline["origin_signal_date"], "2026-10-27")
        invalid = evaluate_follow_up(next_baseline, current(date="2026-10-29"),
                                     latest_close=9.9, data_date="2026-10-29")
        self.assertIsNone(build_follow_up_baseline(invalid, "2026-10-29"))


def run_weak_to_strong_followup_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WeakToStrongFollowUpTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_weak_to_strong_followup_tests()
    raise SystemExit(1 if failed else 0)
