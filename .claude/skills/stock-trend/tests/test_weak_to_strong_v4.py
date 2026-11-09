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


def early_result(*, pattern="T1_LOWER_WICK", status="candidate",
                 failure_line=9.0, close=9.0, provisional=True,
                 complete_score_fields=True):
    metrics = {"close": close}
    if complete_score_fields:
        metrics.update({
            "ma10": 8.8, "ma20": 8.7, "ma10_slope_3": 0.01,
            "ma20_slope_3": 0.005, "ma_flat_tolerance": 0.001,
            "volume_prior5_ratio": 1.10,
        })
    return {
        "status": "not_scored",
        "reason": "V4 requires a newly confirmed V3 event on as_of_date",
        "symbol": "SH.600000",
        "name": "浦发银行",
        "as_of_date": "2026-11-06",
        "prediction": None,
        "v3_result": {
            "evidence_status": "ready",
            "current_bar_evaluated": True,
            "evaluation_date": "2026-11-06",
            "confirmation": "provisional" if provisional else "close_confirmed",
            "provisional": provisional,
            "signal_date": "",
            "stage": "EARLY_REPAIR",
            "lifecycle_status": "active",
            "metrics": metrics,
            "structure": {"failure_line": failure_line},
            "probe": {"status": "ready", "selected": {
                "pattern": pattern,
                "status": status,
                "candidate_id": "probe-1",
                "candidate_date": "2026-11-05",
                "candidate_age_sessions": 1,
                "high": 10.2,
                "low": 9.3,
                "volume": 700,
                "anchor_volume": 1000,
                "anchor_range": 1.5,
                "atr14": 1.0,
                "features": {
                    "range": 1.0, "lower_ratio": 0.5,
                    "close_pos": 0.7,
                },
                "status_events": ([
                    {"status": "candidate", "date": "2026-11-05"},
                    {"status": "confirmed", "date": "2026-11-06"},
                ] if status == "confirmed" else [
                    {"status": "candidate", "date": "2026-11-05"},
                ]),
            }},
        },
    }


def test_market_classifier_exposes_active_t1_t2_probes_as_early_observations():
    report = market.classify_v4_market_results([
        early_result(),
        early_result(pattern="T2_LOW_VOLUME_RETEST", status="confirmed",
                     provisional=False),
    ])
    observations = report["early_observations"]
    assert {item["early_observation"]["signal_type"] for item in observations} == {"T1", "T2"}
    t1 = next(item for item in observations
              if item["early_observation"]["signal_type"] == "T1")
    assert t1["market_classification"] == "early_observation"
    assert t1["research_only"] is True
    assert {key: value for key, value in t1["early_observation"].items()
            if key not in {
                "priority_score", "type_rank", "ranking_cohort", "observation_score",
                "observation_score_max", "observation_score_status",
                "observation_score_components", "observation_score_meta",
            }} == {
        "signal_type": "T1",
        "probe_pattern": "T1_LOWER_WICK",
        "probe_status": "candidate",
        "candidate_date": "2026-11-05",
        "candidate_age_sessions": 1,
        "candidate_high": 10.2,
        "candidate_low": 9.3,
        "current_close": 9.0,
        "structure_stage": "EARLY_REPAIR",
        "structure_failure_line": 9.0,
        "provisional": True,
        "confirmation_state": "probe_unconfirmed",
    }
    score = t1["early_observation"]["priority_score"]
    assert score["status"] == "trial_unvalidated"
    assert score["max_score"] == 90
    assert score["coverage"] == 0.9
    assert score["missing_optional_dimensions"] == ["market_sector"]
    assert t1["early_observation"]["observation_score"] == score["score"]
    assert t1["early_observation"]["observation_score_status"] == "trial_unvalidated"
    assert t1["early_observation"]["type_rank"] == 1
    assert t1["early_observation"]["ranking_cohort"] == "T1:90"
    assert report["research_candidates"] == {"T1": [], "T2": []}
    assert report["summary"]["early_observations"] == 2
    t2 = next(item for item in observations
              if item["early_observation"]["signal_type"] == "T2")
    assert t2["early_observation"]["confirmation_state"] == "probe_confirmed_no_new_event"
    assert t2["early_observation"]["priority_score"]["dimensions"][
        "confirmation_distance"
    ]["confirmation_already_met"] is True
    assert t1["early_observation"]["priority_score"]["dimensions"][
        "confirmation_distance"
    ]["confirmation_already_met"] is False


def test_market_classifier_keeps_close_confirmed_new_event_out_of_early_observation():
    result = early_result(provisional=False)
    result["v3_result"]["signal_date"] = result["as_of_date"]
    report = market.classify_v4_market_results([result])
    assert report["early_observations"] == []
    assert len(report["structural_observations"]) == 1


@pytest.mark.parametrize("status", ["invalidated", "expired"])
def test_market_classifier_excludes_terminal_probe_from_early_observation(status):
    report = market.classify_v4_market_results([early_result(status=status)])
    assert report["early_observations"] == []
    assert len(report["structural_observations"]) == 1


@pytest.mark.parametrize(
    ("failure_line", "close", "reason"),
    [
        (None, 9.5, "early_observation_failure_line_missing"),
        (9.0, 8.99, "early_observation_structure_breached"),
    ],
)
def test_market_classifier_requires_valid_unbreached_structure_line(
    failure_line, close, reason,
):
    report = market.classify_v4_market_results([
        early_result(failure_line=failure_line, close=close),
    ])
    assert report["early_observations"] == []
    assert reason in report["data_insufficient"][0]["classification_reasons"]


def test_early_observation_score_refuses_missing_core_dimensions():
    report = market.classify_v4_market_results([
        early_result(complete_score_fields=False),
    ])
    observation = report["early_observations"][0]["early_observation"]
    score = observation["priority_score"]
    assert score["status"] == "data_insufficient"
    assert score["score"] is None
    assert "trend_structure" in score["missing_core_dimensions"]
    assert "confirmation_distance" in score["missing_core_dimensions"]
    assert "type_rank" not in observation


def test_early_observation_score_uses_current_market_sector_context():
    result = early_result()
    result["observation_context"] = {
        "as_of_date": result["as_of_date"],
        "market_score": 70,
        "sector_score": 80,
    }
    report = market.classify_v4_market_results([result])
    score = report["early_observations"][0]["early_observation"]["priority_score"]
    assert score["max_score"] == 100
    assert score["coverage"] == 1.0
    assert score["dimensions"]["market_sector"]["score"] == 7.5
    assert score["missing_optional_dimensions"] == []


def test_early_observation_ranks_t1_and_t2_independently():
    low = early_result(close=9.0)
    low["symbol"] = "T1-low"
    high = early_result(close=10.1, failure_line=9.9)
    high["symbol"] = "T1-high"
    t2 = early_result(pattern="T2_LOW_VOLUME_RETEST", close=10.0)
    t2["symbol"] = "T2-only"
    report = market.classify_v4_market_results([low, high, t2])
    ranks = {
        item["symbol"]: item["early_observation"]["type_rank"]
        for item in report["early_observations"]
    }
    assert ranks == {"T1-low": 2, "T1-high": 1, "T2-only": 1}
    for item in report["early_observations"]:
        score = item["early_observation"]["priority_score"]
        assert score["ranking_scope"] == "within_signal_type_and_coverage_only"
        assert score["calibration_status"] == "unvalidated"


def test_early_observation_does_not_mix_different_score_coverage():
    partial = early_result(close=9.0)
    partial["symbol"] = "partial"
    full = early_result(close=9.0)
    full["symbol"] = "full"
    full["observation_context"] = {
        "as_of_date": full["as_of_date"], "market_score": 60, "sector_score": 60,
    }
    report = market.classify_v4_market_results([partial, full])
    observations = {item["symbol"]: item["early_observation"]
                    for item in report["early_observations"]}
    assert observations["partial"]["type_rank"] == 1
    assert observations["partial"]["ranking_cohort"] == "T1:90"
    assert observations["full"]["type_rank"] == 1
    assert observations["full"]["ranking_cohort"] == "T1:100"


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
