#!/usr/bin/env python3
"""Contract tests for the vendored frozen weak-to-strong V4 algorithm."""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.weak_to_strong_v4 import market, scan, v4_algorithm as v4


def payload(extra_future=False):
    days = [(date(2026, 10, 1) + timedelta(days=index)).isoformat()
            for index in range(25)]
    rows = []
    for index, day in enumerate(days):
        close = 10 + index * 0.01
        rows.append({
            "date": day,
            "open": close - 0.02,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1000 + index,
        })
    if extra_future:
        rows.append({
            "date": "2026-11-10",
            "open": 1,
            "high": 1.1,
            "low": 0.9,
            "close": 1,
            "volume": 999999,
        })
    return {"data": rows, "meta": {"adj": "qfq"}}, days[-1]


def v3_result(day, pattern="T1_LOWER_WICK"):
    event = {
        "event_id": "event-1",
        "cycle_id": "cycle-1",
        "date": day,
        "event_type": "stage_first",
        "stage": "LOCAL_CONFIRMATION",
    }
    selected = {
        "pattern": pattern,
        "status": "confirmed",
        "candidate_date": day,
        "bonus": 6,
    }
    return {
        "code": "SH.600000",
        "as_of_date": day,
        "evaluation_date": day,
        "evidence_status": "ready",
        "current_bar_evaluated": True,
        "confirmation": "close_confirmed",
        "signal_date": day,
        "cycle_id": "cycle-1",
        "stage": "LOCAL_CONFIRMATION",
        "lifecycle_status": "active",
        "origin_pattern_type": "base_consolidation",
        "pattern_type": "deep_repair",
        "detector_version": "test",
        "parameter_hash": "hash",
        "metrics": {
            "trigger_distance_atr": 0.2,
            "structure_age": 12,
            "close_strength": 0.8,
            "volume_prior5_ratio": 1.1,
        },
        "checks": {},
        "risk_flags": [],
        "structure": {"signal_base_low": 9, "failure_line": 8.9},
        "events": [event],
        "secondary_matches": [],
        "kline_evidence": {"data_date": day},
        "score": {"status": "ready", "total": 78},
        "probe": {"status": "ready", "selected": selected},
    }


def test_adapter_preserves_identity_and_ignores_future_bars():
    source, day = payload()
    adapted = v4.adapt_v3_result_to_v4_record(
        source, v3_result(day), as_of_date=day,
    )
    future, _ = payload(extra_future=True)
    with_future = v4.adapt_v3_result_to_v4_record(
        future, v3_result(day), as_of_date=day,
    )
    assert adapted["event_id"] == "event-1"
    assert adapted["cycle_id"] == "cycle-1"
    assert adapted["signal_type"] == "T1"
    assert adapted["features"] == with_future["features"]
    assert adapted["features"]["feature_asof"] == day


def test_adapter_refuses_stale_event_and_unsupported_probe():
    source, day = payload()
    stale = v3_result(day)
    stale["signal_date"] = "2026-10-20"
    with pytest.raises(ValueError, match="newly confirmed"):
        v4.adapt_v3_result_to_v4_record(source, stale, as_of_date=day)
    with pytest.raises(ValueError, match="selected T1 or T2"):
        v4.adapt_v3_result_to_v4_record(
            source, v3_result(day, pattern="T3_OTHER"), as_of_date=day,
        )


def test_standalone_entry_returns_ready_prediction(monkeypatch):
    source, day = payload()
    monkeypatch.setattr(v4, "validate_v4_shadow_bundle", lambda bundle: None)
    monkeypatch.setattr(v4, "analyze_daily_weak_to_strong", lambda *a, **k: {"v1": True})
    monkeypatch.setattr(v4, "analyze_daily_weak_to_strong_v3",
                        lambda *a, **k: v3_result(day))

    def score(bundle, record, *, feature_snapshot):
        assert feature_snapshot["snapshot_hash"]
        return {
            "score_status": "ready",
            "fallback_reason": None,
            "signal_type": "T1",
            "signal_date": day,
            "v4_score": 85,
            "expected_net_return": 0.01,
            "estimated_win_rate": 0.6,
        }

    monkeypatch.setattr(v4, "score_v4_shadow_record", score)
    result = v4.analyze_daily_weak_to_strong_v4(
        source,
        bundle={"model_version": "monthly"},
        as_of_date=day,
        code="SH.600000",
    )
    assert result["status"] == "ready"
    assert result["prediction"]["event_id"] == "event-1"


def test_market_classifier_ranks_t1_and_t2_independently():
    results = []
    for symbol, signal_type, expected, win in (
        ("A", "T1", 0.01, 0.6),
        ("B", "T1", 0.02, 0.5),
        ("C", "T2", 0.03, 0.7),
    ):
        results.append({
            "status": "ready",
            "symbol": symbol,
            "as_of_date": "2026-11-06",
            "prediction": {
                "score_status": "ready",
                "signal_type": signal_type,
                "expected_net_return": expected,
                "estimated_win_rate": win,
                "event_id": symbol,
            },
        })
    report = market.classify_v4_market_results(results)
    assert [item["symbol"] for item in report["research_candidates"]["T1"]] == ["B", "A"]
    assert [item["symbol"] for item in report["research_candidates"]["T2"]] == ["C"]


def test_scan_missing_model_reports_explicit_fallback(monkeypatch):
    monkeypatch.setattr(scan, "analyze_daily_weak_to_strong", lambda *a, **k: {})
    monkeypatch.setattr(scan, "analyze_daily_weak_to_strong_v3", lambda *a, **k: {
        "stage": "EARLY_REPAIR",
        "evidence_status": "ready",
        "current_bar_evaluated": True,
        "lifecycle_status": "active",
    })
    report = scan.scan_market(
        [{"code": "SH.600000", "kline_payload": {}}],
        as_of_date="2026-11-06",
        bundle=None,
    )
    assert report["model_status"] == "model_missing"
    assert report["research_candidates"] == {"T1": [], "T2": []}
    assert report["data_insufficient"][0]["reason"] == "model_missing"
