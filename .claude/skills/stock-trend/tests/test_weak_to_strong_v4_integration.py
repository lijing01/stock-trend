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


def test_default_monthly_bundle_loaded_without_environment(tmp_path):
    import json
    path = tmp_path / "models" / "weak_to_strong_v4" / "2026-11.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model_version": "monthly-test"}))
    with patch.object(daily_candidates, "CACHE_DIR", tmp_path), patch(
        "analysis.weak_to_strong_v4.scan.scan_market",
        return_value={"research_candidates": {"T1": [], "T2": []}},
    ) as scan:
        result = daily_candidates.build_weak_to_strong_v4_output({}, "2026-11-09", {})
    assert scan.call_args.kwargs["bundle"]["model_version"] == "monthly-test"
    assert result["model_path"] == str(path)


def test_explicit_bundle_overrides_default_monthly_bundle(tmp_path):
    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"model_version": "explicit"}')
    with patch.object(daily_candidates, "CACHE_DIR", tmp_path), patch(
        "analysis.weak_to_strong_v4.scan.scan_market",
        return_value={"research_candidates": {"T1": [], "T2": []}},
    ) as scan:
        daily_candidates.build_weak_to_strong_v4_output({}, "2026-11-09", {}, explicit)
    assert scan.call_args.kwargs["bundle"]["model_version"] == "explicit"
