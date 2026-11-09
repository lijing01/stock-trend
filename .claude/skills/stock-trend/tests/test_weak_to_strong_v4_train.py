#!/usr/bin/env python3
"""Focused safety tests for monthly V4 training."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.weak_to_strong_v4 import train


def record(signal="2026-10-23", exit_date="2026-10-29"):
    return {"signal_date": signal, "exit_date": exit_date, "event_id": "event-1",
            "feature_asof": signal}


def test_monthly_boundaries_use_five_session_purge():
    calendar = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28",
                "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    assert train._boundaries(calendar, "2026-11-02") == {
        "validation_start": "2026-11-02", "training_cutoff": "2026-10-30",
        "purge_cutoff": "2026-10-23", "valid_until": "2026-11-03",
    }


def test_future_and_unclosed_records_are_excluded():
    boundaries = {"purge_cutoff": "2026-10-23", "training_cutoff": "2026-10-30"}
    assert train._eligible_record(record(), boundaries)
    assert not train._eligible_record(record(signal="2026-10-26"), boundaries)
    assert not train._eligible_record(record(exit_date="2026-11-02"), boundaries)


def test_eligible_records_require_identity_and_point_in_time_features():
    boundaries = {"purge_cutoff": "2026-10-23", "training_cutoff": "2026-10-30"}
    missing = record()
    missing["event_id"] = ""
    with pytest.raises(ValueError, match="stable event_id"):
        train._eligible_record(missing, boundaries)
    mismatch = record()
    mismatch["feature_asof"] = "2026-10-24"
    with pytest.raises(ValueError, match="feature_asof"):
        train._eligible_record(mismatch, boundaries)


def test_immutable_write_is_idempotent_and_rejects_changes(tmp_path):
    path = tmp_path / "model.json"
    train._atomic_write(path, {"value": 1})
    train._atomic_write(path, {"value": 1})
    with pytest.raises(ValueError, match="immutable model"):
        train._atomic_write(path, {"value": 2})
