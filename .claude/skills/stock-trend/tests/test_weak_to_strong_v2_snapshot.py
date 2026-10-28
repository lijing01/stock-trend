#!/usr/bin/env python3
"""Versioned snapshot and lineage follow-up tests for weak-to-strong v2."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.weak_to_strong_v2_follow_up import build_v2_follow_up  # noqa: E402
from core.weak_to_strong_snapshot import (  # noqa: E402
    _v2_detector_identity,
    load_latest_snapshot,
    load_previous_snapshot,
    save_snapshot,
)


def item(stage, *, evidence="ready", lifecycle=None, cycle="cycle-a"):
    value = {
        "code": "600001.SH",
        "name": "测试股",
        "origin_pattern_type": "base_consolidation",
        "pattern_type": "deep_repair",
        "cycle_id": cycle,
        "detector_version": "2.0.0-research1",
        "parameter_hash": "parameters-a",
        "stage": stage,
        "stage_first_at": "2026-10-27",
        "signal_date": "2026-10-27",
        "confirmation": "close_confirmed",
        "evidence_status": evidence,
    }
    if lifecycle:
        value["lifecycle_status"] = lifecycle
    return value


def payload(day, group, row, *, detector="2.0.0-research1",
            parameters="parameters-a"):
    result = {
        "schema_version": "daily-weak-to-strong/v2",
        "as_of_date": day,
        "detector_version": detector,
        "parameter_hash": parameters,
        "preparing": [],
        "early_repair": [],
        "local_confirmation": [],
        "range_breakout": [],
        "provisional": [],
        "diagnostics": [],
    }
    result[group].append(row)
    return result


class WeakToStrongV2SnapshotTests(unittest.TestCase):
    def test_v2_identity_covers_detector_aggregation_reporting_and_follow_up(self):
        version, modules = _v2_detector_identity()
        self.assertTrue(version.startswith("2."))
        self.assertEqual(set(modules), {
            "analysis/daily_weak_to_strong.py",
            "analysis/daily_weak_to_strong_v2.py",
            "analysis/weak_to_strong_v2_follow_up.py",
            "scans/weak_to_strong_v2.py",
            "reporting/weak_to_strong.py",
        })

    def test_v2_snapshot_is_isolated_and_records_detector_module_identity(self):
        result = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        with tempfile.TemporaryDirectory() as root, patch(
                "core.weak_to_strong_snapshot._v2_detector_identity",
                return_value=("2.0.0-research1", {
                    "analysis/daily_weak_to_strong.py": "a" * 64,
                    "analysis/daily_weak_to_strong_v2.py": "b" * 64,
                })):
            saved = save_snapshot(
                result, {"600001.SH": {"bars": [1]}},
                {"identity": "calendar-a"}, {"mode": "observation"}, root,
                requested_version="v2",
                generated_at="2026-10-27T15:01:00+08:00")

            self.assertEqual(saved["status"], "saved")
            self.assertIn("/v2/daily/", saved["path"])
            frozen = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
            self.assertEqual(frozen["schema_version"], "weak-to-strong-snapshot/v2")
            self.assertEqual(frozen["detector_version"], "2.0.0-research1")
            self.assertEqual(len(frozen["detector_modules_sha256"]), 2)
            self.assertEqual(frozen["parameter_hash"], "parameters-a")
            self.assertEqual(
                load_latest_snapshot(root, "2026-10-27")["status"], "missing")
            loaded = load_latest_snapshot(
                root, "2026-10-27", requested_version="v2")
            self.assertEqual(loaded["status"], "loaded")
            self.assertEqual(loaded["snapshot"]["inputs"]["600001.SH"]["bars"], [1])

            previous = load_previous_snapshot(
                root, "2026-10-28", ["2026-10-27", "2026-10-28"],
                requested_version="v2")
            self.assertEqual(previous["status"], "loaded")

    def test_v2_snapshot_ignores_volatile_performance_timings(self):
        first = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        first["performance"] = {"compute_seconds": 0.1,
                                "max_security_seconds": 0.02,
                                "securities_computed": 1, "network_requests": 0}
        second = json.loads(json.dumps(first))
        second["performance"]["compute_seconds"] = 9.9
        second["performance"]["max_security_seconds"] = 8.8
        with tempfile.TemporaryDirectory() as root, patch(
                "core.weak_to_strong_snapshot._v2_detector_identity",
                return_value=("2.0.0-research1", {"detector": "a" * 64})):
            saved_first = save_snapshot(
                first, {}, {}, {}, root, requested_version="v2")
            saved_second = save_snapshot(
                second, {}, {}, {}, root, requested_version="v2")

        self.assertEqual(saved_first["content_sha256"], saved_second["content_sha256"])
        self.assertEqual(saved_second["status"], "existing")

    def test_stage_upgrade_keeps_cycle_identity(self):
        prior = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        current = payload(
            "2026-10-28", "local_confirmation", item("LOCAL_CONFIRMATION"))

        output = build_v2_follow_up(prior, current)

        self.assertEqual(output["status"], "loaded")
        self.assertEqual(output["rows"][0]["follow_up_status"], "STAGE_UPGRADED")
        self.assertEqual(output["rows"][0]["cycle_id"], "cycle-a")
        self.assertTrue(output["rows"][0]["active"])

    def test_absence_or_new_cycle_does_not_invalidate_prior_cycle(self):
        prior = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        absent = payload("2026-10-28", "diagnostics", {
            **item("NONE", cycle="cycle-b"), "confirmation": "none"})

        output = build_v2_follow_up(prior, absent)

        self.assertEqual(output["rows"][0]["follow_up_status"], "PENDING_REVIEW")
        self.assertEqual(output["rows"][0]["reason_codes"], ["cycle_lineage_changed"])
        self.assertTrue(output["rows"][0]["active"])

    def test_only_explicit_ready_lifecycle_can_end_cycle(self):
        prior = payload(
            "2026-10-27", "local_confirmation", item("LOCAL_CONFIRMATION"))
        invalidated = payload(
            "2026-10-28", "diagnostics",
            item("NONE", lifecycle="invalidated"))

        output = build_v2_follow_up(prior, invalidated)

        self.assertEqual(output["rows"][0]["follow_up_status"], "INVALIDATED")
        self.assertFalse(output["rows"][0]["active"])

        invalidated["diagnostics"][0]["evidence_status"] = "data_insufficient"
        pending = build_v2_follow_up(prior, invalidated)
        self.assertEqual(pending["rows"][0]["follow_up_status"], "PENDING_REVIEW")
        self.assertTrue(pending["rows"][0]["active"])

    def test_detector_or_parameter_change_never_links_cycles(self):
        prior = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        changed = payload(
            "2026-10-28", "early_repair", item("EARLY_REPAIR"),
            parameters="parameters-b")

        output = build_v2_follow_up(prior, changed)

        self.assertEqual(output["status"], "incompatible")
        self.assertEqual(output["reason"], "parameter_hash_changed")
        self.assertEqual(output["rows"], [])

    def test_pending_lineage_is_carried_until_current_evidence_returns(self):
        prior = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        missing = payload("2026-10-28", "diagnostics", {
            **item("NONE", cycle="cycle-b"), "confirmation": "none"})
        first_follow = build_v2_follow_up(prior, missing)
        missing["follow_up"] = first_follow
        returned = payload(
            "2026-10-29", "local_confirmation", item("LOCAL_CONFIRMATION"))

        output = build_v2_follow_up(missing, returned)

        self.assertEqual(output["status"], "loaded")
        self.assertEqual(output["rows"][0]["cycle_id"], "cycle-a")
        self.assertEqual(output["rows"][0]["follow_up_status"], "STAGE_UPGRADED")

    def test_nonempty_payload_without_version_identity_fails_closed(self):
        prior = payload("2026-10-27", "early_repair", item("EARLY_REPAIR"))
        current = payload(
            "2026-10-28", "local_confirmation", item("LOCAL_CONFIRMATION"))
        prior.pop("detector_version")

        output = build_v2_follow_up(prior, current)

        self.assertEqual(output["status"], "incompatible")
        self.assertEqual(output["reason"], "previous_detector_version_missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
