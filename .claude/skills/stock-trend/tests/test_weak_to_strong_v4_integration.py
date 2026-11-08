"""Contracts between the daily scanner and the V4 market adapter."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scans import daily_candidates


def test_v4_receives_collected_bars_and_calendar():
    records = {"000001.SZ": {"code": "000001", "kline_payload": {"data": []}}}
    calendar = {"source": "test"}
    market = {"research_candidates": {"T1": [], "T2": []}}
    with patch("analysis.weak_to_strong_v4.scan.scan_market", return_value=market) as scan:
        result = daily_candidates.build_weak_to_strong_v4_output(records, "2026-01-02", calendar)
    assert list(scan.call_args.args[0]) == list(records.values())
    assert scan.call_args.kwargs == {"as_of_date": "2026-01-02", "bundle": None,
                                     "calendar_evidence": calendar}
    assert "display_selection" in result


def test_missing_explicit_bundle_is_invalid_not_missing(tmp_path):
    result = daily_candidates.build_weak_to_strong_v4_output(
        {}, "2026-01-02", {}, str(tmp_path / "missing.json"))
    assert result["model_status"] == "model_invalid"
