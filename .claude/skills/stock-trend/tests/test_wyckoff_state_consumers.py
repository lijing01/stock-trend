"""Candidate and report consumers must agree on conflicting health states."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from scans import daily_candidates
from reporting import report


class StateConsumerTest(unittest.TestCase):
    def test_conflicting_states_fail_closed_in_both_consumers(self):
        payload = {
            "short_term": {"current_state": "confirmed_holding"},
            "signal": {"current_state": "confirmed_holding"},
            "event_health": {"state": "failed_breakout"},
        }
        self.assertEqual(daily_candidates._wyckoff_current_state(payload), "state_unknown")
        self.assertEqual(report._wyckoff_current_state(payload), "state_unknown")

    def test_legacy_single_state_remains_readable(self):
        payload = {"short_term": {"current_state": "confirmed_holding"}}
        self.assertEqual(daily_candidates._wyckoff_current_state(payload), "confirmed_holding")
        self.assertEqual(report._wyckoff_current_state(payload), "confirmed_holding")

    def test_signal_nested_health_conflict_is_blocked(self):
        payload = {"signal": {"current_state": "confirmed_holding",
                              "event_health": {"state": "failed_breakout"}}}
        self.assertEqual(daily_candidates._wyckoff_current_state(payload), "state_unknown")
        self.assertEqual(report._wyckoff_current_state(payload), "state_unknown")


if __name__ == "__main__":
    unittest.main()
