"""Test weekly_report.py — 周主线报告."""

import sys
import json
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.weekly_report import (
    aggregate_sectors,
    classify_weekly,
    generate_report,
    _generate_html_report,
    load_market_snapshots,
    load_lhb_snapshots,
    load_lhb_snapshot_bundle,
    fetch_industry_data,
    fetch_current_industry_data,
    HAS_AKSHARE,
)
import analysis.weekly_report as weekly_report
import analysis.ths_theme as ths_theme
import fetchers.sector_data as sector_data


MOCK_MARKET_SNAPS = {
    "2026-05-25": [
        {"name": "白酒", "code": "BK0477", "hot_score": 85, "change_pct": 2.5, "up_ratio": 0.85, "rank": 1},
        {"name": "银行", "code": "BK0480", "hot_score": 75, "change_pct": 1.5, "up_ratio": 0.75, "rank": 2},
        {"name": "半导体", "code": "BK0500", "hot_score": 25, "change_pct": -2.0, "up_ratio": 0.20, "rank": 88},
    ],
    "2026-05-26": [
        {"name": "白酒", "code": "BK0477", "hot_score": 82, "change_pct": 2.0, "up_ratio": 0.80, "rank": 1},
        {"name": "电力", "code": "BK0481", "hot_score": 78, "change_pct": 1.8, "up_ratio": 0.78, "rank": 2},
    ],
    "2026-05-27": [
        {"name": "白酒", "code": "BK0477", "hot_score": 88, "change_pct": 3.0, "up_ratio": 0.90, "rank": 1},
        {"name": "银行", "code": "BK0480", "hot_score": 72, "change_pct": 1.0, "up_ratio": 0.70, "rank": 3},
        {"name": "电力", "code": "BK0481", "hot_score": 80, "change_pct": 2.2, "up_ratio": 0.82, "rank": 2},
    ],
}

MOCK_LHB_SNAPS = [
    {
        "date": "2026-05-27",
        "sectors": [
            {"sector_name": "白酒", "direction": "净买", "inst_net_yi": 2.5,
             "lhb_score": 75, "stock_count": 2, "inst_buy_count": 2, "inst_sell_count": 0,
             "member_names": ["茅台"], "change_pct": None},
        ],
    },
]

MOCK_TODAY = [
    {"name": "白酒", "hot_score": 90, "change_pct": 3.5, "up_ratio": 0.90,
     "net_flow": 1e9, "leader_name": "茅台", "rank": 1},
    {"name": "银行", "hot_score": 70, "change_pct": 1.0, "up_ratio": 0.70,
     "net_flow": 5e8, "leader_name": "工行", "rank": 5},
    {"name": "电力", "hot_score": 65, "change_pct": 0.5, "up_ratio": 0.60,
     "net_flow": 2e8, "leader_name": "华能", "rank": 10},
]


# ──────────────── aggregate_sectors ────────────────


def test_aggregate_empty():
    assert aggregate_sectors({}, [], []) == []


def test_aggregate_without_lhb_renormalizes_weights():
    scored = aggregate_sectors(
        {"2026-08-27": [{
            "name": "测试行业", "code": "BK0001", "hot_score": 80,
            "change_pct": 2, "net_flow": 100000000, "leader": "测试股",
        }]},
        [],
        [],
        lhb_meta={"available_days": 0, "status": "error"},
    )

    assert scored
    assert scored[0]["lhb_status"] == "error"
    assert scored[0]["score_weights"]["lhb"] == 0
    assert scored[0]["score_weights"]["base_total"] == 0.90
    assert scored[0]["weekly_score"] == round(
        (80 * 0.30 + 100 * 0.25 + 80 * 0.25 + 50 * 0.10) / 0.90, 1
    )


def test_current_industry_uses_eastmoney_after_ths_failure(monkeypatch):
    monkeypatch.setattr(
        ths_theme, "fetch_industry_data_with_evidence",
        lambda: {
            "data": [], "status": "error", "source": "none",
            "live_attempt": {"attempted": True, "provider_attempts": 1,
                              "reason": "dns", "status": "error"},
            "errors": ["ths_akshare: dns"],
        },
    )
    monkeypatch.setattr(
        sector_data, "get_sector_rankings",
        lambda **kwargs: {
            "payload": {
                "sectors": [{
                    "code": "BK0001", "name": "东方行业", "type": "industry",
                    "change_pct": 2.0, "amount": 10e8,
                    "up_count": 8, "down_count": 2,
                    "main_force_net": 1.5e8,
                }],
                "meta": {"errors": []},
            },
            "live_attempt": {"attempted": True, "provider_attempts": 1,
                              "reason": "", "status": "success"},
        },
    )

    evidence = fetch_current_industry_data()

    assert evidence["status"] == "live_success"
    assert evidence["source"] == "eastmoney_push2"
    assert evidence["data"]
    assert evidence["data"][0]["name"] == "东方行业"
    assert evidence["errors"] == ["ths_akshare: dns"]


def test_current_industry_failure_keeps_weekly_report_diagnostic(monkeypatch):
    monkeypatch.setattr(
        ths_theme, "fetch_industry_data_with_evidence",
        lambda: {
            "data": [], "status": "error", "source": "none",
            "live_attempt": {"attempted": True, "provider_attempts": 1,
                              "reason": "dns", "status": "error"},
            "errors": ["ths_akshare: dns"],
        },
    )
    monkeypatch.setattr(
        sector_data, "get_sector_rankings",
        lambda **kwargs: {
            "payload": {"sectors": [], "meta": {"errors": ["industry: dns"]}},
            "live_attempt": {"attempted": True, "provider_attempts": 1,
                              "reason": "dns", "status": "error"},
        },
    )

    evidence = fetch_current_industry_data()
    market = {"2026-08-27": [{"name": "历史行业", "hot_score": 70,
                               "change_pct": 1, "up_ratio": 0.7}]}
    scored = aggregate_sectors(
        market, [], evidence["data"],
        lhb_meta={"available_days": 0, "status": "error"},
    )
    meta = {
        "time": "test", "period": "test", "total_sectors": len(scored),
        "total_dates": 1, "industry_status": evidence["status"],
        "industry_source": evidence["source"],
        "industry_errors": evidence["errors"],
        "lhb_available_days": 0, "lhb_attempted_days": 1,
        "lhb_status_days": {"2026-08-27": "error"},
        "lhb_failure_reasons": ["dns"], "warnings": ["industry unavailable"],
    }
    report = generate_report(scored, classify_weekly(scored), meta)
    html = _generate_html_report(scored, classify_weekly(scored), meta)

    assert evidence["status"] == "error"
    assert scored
    assert "行业数据不可用" in report
    assert "龙虎榜" in report
    assert "行业数据不可用" in html


def test_load_lhb_snapshot_bundle_distinguishes_statuses(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "LHB_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(weekly_report, "date", date)
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    today = date.today()
    success_date = today.isoformat()
    legacy_date = (today - timedelta(days=1)).isoformat()
    missing_date = (today - timedelta(days=2)).isoformat()
    snapshot = {"date": success_date, "sectors": [{"sector_name": "测试行业"}]}
    (tmp_path / f"{success_date}.json").write_text(
        json.dumps(snapshot), encoding="utf-8")
    (status_dir / f"{success_date}.json").write_text(json.dumps({
        "date": success_date, "status": "live_success", "mapping_stale": True,
    }), encoding="utf-8")
    (tmp_path / f"{legacy_date}.json").write_text(json.dumps({
        "date": legacy_date, "sectors": [{"sector_name": "旧行业"}],
    }), encoding="utf-8")

    bundle = load_lhb_snapshot_bundle(days=3)

    assert bundle["available_days"] == 2
    assert bundle["attempted_days"] == 1
    assert bundle["status_days"][success_date] == "live_success"
    assert bundle["status_days"][legacy_date] == "legacy_snapshot"
    assert bundle["status_days"][missing_date] == "not_run"
    assert bundle["mapping_stale_days"] == [success_date]


def test_load_lhb_snapshot_bundle_handles_failed_sidecar_without_snapshot(
        tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "LHB_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(weekly_report, "date", date)
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    today = date.today().isoformat()
    (status_dir / f"{today}.json").write_text(json.dumps({
        "date": today, "status": "error", "failure_reasons": ["dns"],
    }), encoding="utf-8")

    bundle = load_lhb_snapshot_bundle(days=1)

    assert bundle["snapshots"] == []
    assert bundle["available_days"] == 0
    assert bundle["attempted_days"] == 1
    assert bundle["status"] == "error"
    assert bundle["failure_reasons"] == ["dns"]


def test_aggregate_basic():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    assert len(result) >= 3
    sectors = {s["name"]: s for s in result}
    assert "白酒" in sectors
    assert "银行" in sectors
    assert "电力" in sectors


def test_aggregate_scoring():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    bj = [s for s in result if s["name"] == "白酒"][0]
    assert bj["avg_hot"] > 0
    assert bj["appearance_days"] >= 3
    assert bj["frequency"] > 0
    assert bj["lhb_direction"] == "净买"
    assert bj["weekly_score"] > 0


def test_aggregate_lhb_crossref():
    """LHB data should cross-reference with sector names."""
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    bj = [s for s in result if s["name"] == "白酒"][0]
    assert bj["lhb_net_yi"] > 0
    assert bj["lhb_direction"] == "净买"


def test_aggregate_trend():
    """白酒 scores trending up: 85→82→88 → should detect."""
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    bj = [s for s in result if s["name"] == "白酒"][0]
    assert bj["trend"] in ("up", "down", "flat")


# ──────────────── classify_weekly ────────────────


def test_classify_has_all_tiers():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    cls = classify_weekly(result)
    assert "strong" in cls
    assert "active" in cls
    assert "weak" in cls


def test_classify_empty():
    cls = classify_weekly([])
    assert all(v == [] for v in cls.values())


# ──────────────── generate_report ────────────────


def test_generate_report():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    cls = classify_weekly(result)
    meta = {"time": "2026-05-31 12:00", "period": "2026-05-25 ~ 2026-05-31",
            "total_sectors": len(result), "total_dates": 3}
    report = generate_report(result, cls, meta)
    assert "周主线报告" in report
    assert "白酒" in report
    assert "中期主线" in report or "关注方向" in report


def test_generate_html():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    cls = classify_weekly(result)
    meta = {"time": "test", "period": "test", "total_sectors": len(result), "total_dates": 3}
    html = _generate_html_report(result, cls, meta)
    assert "周主线报告" in html
    assert "中期主线" in html


# ──────────────── load_* (live) ────────────────


def test_load_market_snapshots_live():
    snapshots = load_market_snapshots(days=3)
    assert isinstance(snapshots, dict)


def test_load_lhb_snapshots_live():
    snapshots = load_lhb_snapshots(days=3)
    assert isinstance(snapshots, list)


def test_fetch_industry_data_live():
    if not HAS_AKSHARE:
        return
    data = fetch_industry_data()
    if data:
        assert len(data) > 0
        assert "name" in data[0]
        assert "hot_score" in data[0]


def test_aggregate_sorted():
    """Results should be sorted by weekly_score descending."""
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    for i in range(len(result) - 1):
        assert result[i]["weekly_score"] >= result[i + 1]["weekly_score"]


def test_scores_in_range():
    result = aggregate_sectors(MOCK_MARKET_SNAPS, MOCK_LHB_SNAPS, MOCK_TODAY)
    for s in result:
        assert 0 <= s["weekly_score"] <= 100
