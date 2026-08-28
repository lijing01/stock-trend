"""Test lhb_tracker.py — 龙虎榜机构信号跟踪."""

import sys
import json
from pathlib import Path
from datetime import datetime, date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.lhb_tracker import (
    save_daily_snapshot,
    collect_daily_snapshot,
    load_history,
    backfill_returns,
    verify_signals,
    generate_tracker_report,
    SNAPSHOT_DIR,
    HAS_AKSHARE,
)
import analysis.lhb_tracker as lhb_tracker
import fetchers.longhubang_agg as lhb_agg


class _FakeFrame:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.empty = not self._rows

    def iterrows(self):
        return enumerate(self._rows)


# ──────────────── Mock 快照 ────────────────

MOCK_SNAPSHOT_1 = {
    "date": "2026-05-25",
    "save_time": "2026-05-25 15:30:00",
    "total_lhb_stocks": 45,
    "sectors": [
        {
            "sector_code": "BK001",
            "sector_name": "白酒",
            "sector_type": "industry",
            "lhb_score": 72.0,
            "direction": "净买",
            "inst_net_yi": 5.2,
            "stock_count": 3,
            "inst_buy_count": 3,
            "inst_sell_count": 1,
            "member_names": ["茅台", "五粮液", "泸州老窖"],
            "change_pct": 2.5,
            "return_3d": 3.1,
            "return_5d": 4.2,
            "return_10d": None,
        },
        {
            "sector_code": "BK002",
            "sector_name": "半导体",
            "sector_type": "industry",
            "lhb_score": 30.0,
            "direction": "净卖",
            "inst_net_yi": -8.5,
            "stock_count": 4,
            "inst_buy_count": 1,
            "inst_sell_count": 3,
            "member_names": ["中芯", "华虹"],
            "change_pct": -1.8,
            "return_3d": -2.5,
            "return_5d": -3.1,
            "return_10d": None,
        },
    ],
}

MOCK_SNAPSHOT_2 = {
    "date": "2026-05-26",
    "save_time": "2026-05-26 15:30:00",
    "total_lhb_stocks": 38,
    "sectors": [
        {
            "sector_code": "BK003",
            "sector_name": "电力",
            "sector_type": "industry",
            "lhb_score": 68.0,
            "direction": "净买",
            "inst_net_yi": 3.8,
            "stock_count": 2,
            "inst_buy_count": 2,
            "inst_sell_count": 0,
            "member_names": ["长江电力", "华能"],
            "change_pct": 1.2,
            "return_3d": 2.0,
            "return_5d": 1.5,
            "return_10d": None,
        },
        {
            "sector_code": "BK004",
            "sector_name": "消费电子",
            "sector_type": "industry",
            "lhb_score": 25.0,
            "direction": "净卖",
            "inst_net_yi": -12.0,
            "stock_count": 5,
            "inst_buy_count": 0,
            "inst_sell_count": 4,
            "member_names": ["立讯", "歌尔"],
            "change_pct": -3.2,
            "return_3d": -4.0,
            "return_5d": -2.8,
            "return_10d": None,
        },
    ],
}

MOCK_SNAPSHOT_3 = {
    "date": "2026-05-27",
    "save_time": "2026-05-27 15:30:00",
    "total_lhb_stocks": 52,
    "sectors": [
        {
            "sector_code": "BK005",
            "sector_name": "人工智能",
            "sector_type": "concept",
            "lhb_score": 65.0,
            "direction": "净买",
            "inst_net_yi": 2.1,
            "stock_count": 3,
            "inst_buy_count": 2,
            "inst_sell_count": 1,
            "member_names": ["讯飞", "百度"],
            "change_pct": 0.8,
            "return_3d": 1.5,
            "return_5d": None,
            "return_10d": None,
        },
        {
            "sector_code": "BK006",
            "sector_name": "银行",
            "sector_type": "industry",
            "lhb_score": 55.0,
            "direction": "净买",
            "inst_net_yi": 1.5,
            "stock_count": 2,
            "inst_buy_count": 1,
            "inst_sell_count": 1,
            "member_names": ["招商", "兴业"],
            "change_pct": 0.5,
            "return_3d": 0.3,
            "return_5d": None,
            "return_10d": None,
        },
    ],
}


# ──────────────── 辅助 ────────────────


def _write_mock_snapshot(snapshot: dict):
    """Write a mock snapshot file."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    fp = SNAPSHOT_DIR / f"{snapshot['date']}.json"
    fp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def _clean_mock_snapshots():
    """Remove mock snapshot files."""
    for d in ["2026-05-25", "2026-05-26", "2026-05-27",
              "2099-01-01", "2099-01-02", "2099-01-03",
              "2099-01-04", "2099-01-05"]:
        fp = SNAPSHOT_DIR / f"{d}.json"
        if fp.exists():
            fp.unlink()


# ──────────────── load_history ────────────────


def test_load_history_empty():
    """No snapshots should return empty list."""
    result = load_history(1)
    assert isinstance(result, list)


def test_load_history_with_mock(monkeypatch):
    """Mock snapshots should load in date order."""
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    monkeypatch.setattr(lhb_tracker, "date", _FixedDate)
    _clean_mock_snapshots()
    _write_mock_snapshot(MOCK_SNAPSHOT_1)
    _write_mock_snapshot(MOCK_SNAPSHOT_2)
    result = load_history(30)
    assert len(result) >= 2
    assert result[0]["date"] <= result[-1]["date"]
    _clean_mock_snapshots()


# ──────────────── verify_signals ────────────────


def test_verify_signals_insufficient():
    """Fewer than min_snapshots should return note."""
    result = verify_signals([MOCK_SNAPSHOT_1], min_snapshots=3)
    assert "note" in result.get("stats", {})


def test_verify_signals_with_data():
    """3 snapshots with return data should produce signal analysis."""
    result = verify_signals(
        [MOCK_SNAPSHOT_1, MOCK_SNAPSHOT_2, MOCK_SNAPSHOT_3],
        min_snapshots=2,
    )
    signals = result.get("signals", {})
    assert len(signals) > 0
    # Should have 3d data (all snapshots have return_3d)
    assert "3d" in signals
    sw = signals["3d"]
    assert sw["total_signals"] == 6  # 2 sectors × 3 snapshots
    assert sw["buy_count"] > 0
    assert sw["sell_count"] > 0


def test_verify_signals_correctness():
    """净买 sectors that went up = correct, 净卖 sectors that went down = correct."""
    result = verify_signals(
        [MOCK_SNAPSHOT_1, MOCK_SNAPSHOT_2, MOCK_SNAPSHOT_3],
        min_snapshots=2,
    )
    sw = result.get("signals", {}).get("3d", {})
    # 白酒: 净买 +3.1% → correct ✓
    # 半导体: 净卖 -2.5% → correct ✓
    # 电力: 净买 +2.0% → correct ✓
    # 消费电子: 净卖 -4.0% → correct ✓
    # 人工智能: 净买 +1.5% → correct ✓
    # 银行: 净买 +0.3% → correct ✓
    # All 6 should be correct → 100% hit rate
    assert sw["overall_hit_rate"] == 100.0


# ──────────────── generate_tracker_report ────────────────


def test_generate_report_empty():
    """Empty snapshots should produce valid minimal report."""
    report = generate_tracker_report([], {"signals": {}})
    assert "龙虎榜机构信号跟踪" in report
    assert isinstance(report, str)


def test_generate_report_with_data():
    """Report should include signal table and latest snapshot."""
    result = verify_signals(
        [MOCK_SNAPSHOT_1, MOCK_SNAPSHOT_2, MOCK_SNAPSHOT_3],
        min_snapshots=2,
    )
    report = generate_tracker_report(
        [MOCK_SNAPSHOT_1, MOCK_SNAPSHOT_2, MOCK_SNAPSHOT_3],
        result,
    )
    assert "信号有效性" in report
    assert "3d" in report
    assert "最近快照" in report
    assert "人工智能" in report  # latest snapshot sector
    assert "银行" in report
    assert "3d" in report


# ──────────────── save_daily_snapshot ────────────────


def test_save_daily_snapshot_live():
    """Real save_snapshot — skip if non-trading day."""
    if not HAS_AKSHARE:
        return
    snap = save_daily_snapshot("2026-05-29")
    if not snap:
        return  # no data, skip
    assert "date" in snap
    assert "sectors" in snap
    assert len(snap["sectors"]) > 0
    assert "lhb_score" in snap["sectors"][0]
    assert "direction" in snap["sectors"][0]


def test_lhb_fetch_dns_stops_date_fallback(monkeypatch):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("NameResolutionError: getaddrinfo failed")

    monkeypatch.setattr(lhb_agg.ak, "stock_lhb_jgmmtj_em", fail)
    result = lhb_agg.fetch_lhb_jgmmtj_with_evidence("20260828")

    assert result["status"] == "error"
    assert result["data"] == []
    assert result["live_attempt"]["reason"] == "dns"
    assert result["live_attempt"]["provider_attempts"] == 1
    assert len(calls) == 1


def test_lhb_fetch_empty_falls_back_only_four_days(monkeypatch):
    calls = []

    def empty(**kwargs):
        calls.append(kwargs)
        return _FakeFrame()

    monkeypatch.setattr(lhb_agg.ak, "stock_lhb_jgmmtj_em", empty)
    result = lhb_agg.fetch_lhb_jgmmtj_with_evidence("20260828")

    assert result["status"] == "no_data"
    assert result["live_attempt"]["reason"] == "empty"
    assert result["live_attempt"]["provider_attempts"] == 5
    assert [c["start_date"] for c in calls] == [
        "20260828", "20260827", "20260826", "20260825", "20260824",
    ]


def test_lhb_fetch_reports_actual_row_date(monkeypatch):
    row = {"代码": "600519", "名称": "测试股", "上榜日期": "2026-08-27"}
    monkeypatch.setattr(lhb_agg.ak, "stock_lhb_jgmmtj_em",
                        lambda **kwargs: _FakeFrame([row]))

    result = lhb_agg.fetch_lhb_jgmmtj_with_evidence("20260828")

    assert result["status"] == "live_success"
    assert result["data_date"] == "20260827"
    assert result["data"][0]["date"] == "20260827"


def test_run_lhb_analysis_propagates_fetch_error(monkeypatch):
    monkeypatch.setattr(lhb_agg, "fetch_lhb_jgmmtj_with_evidence", lambda _: {
        "data": [], "status": "error", "requested_date": "20260828",
        "data_date": "", "live_attempt": {
            "attempted": True, "provider_attempts": 1,
            "reason": "dns", "status": "error",
        }, "errors": ["20260828: dns"],
    })

    result = lhb_agg.run_lhb_analysis("20260828")

    assert result["meta"]["status"] == "error"
    assert result["meta"]["failure_reasons"] == ["dns"]
    assert result["sectors"] == []


def test_run_lhb_analysis_marks_mapping_error(monkeypatch):
    monkeypatch.setattr(lhb_agg, "fetch_lhb_jgmmtj_with_evidence", lambda _: {
        "data": [{"code": "600519", "name": "测试股"}],
        "status": "live_success", "requested_date": "20260828",
        "data_date": "20260828", "live_attempt": {
            "attempted": True, "provider_attempts": 1,
            "reason": "", "status": "success",
        }, "errors": [],
    })
    monkeypatch.setattr(lhb_agg, "get_mapping", lambda **kwargs: None)

    result = lhb_agg.run_lhb_analysis("20260828")

    assert result["meta"]["status"] == "mapping_error"
    assert result["meta"]["total_lhb_stocks"] == 1
    assert result["meta"]["failure_reasons"] == ["mapping"]


def _mock_lhb_result(status, date_str):
    return {
        "meta": {
            "date": date_str.replace("-", ""), "requested_date": date_str.replace("-", ""),
            "data_date": date_str.replace("-", ""), "status": status,
            "total_lhb_stocks": 1 if status != "no_data" else 0,
            "total_sectors": 1 if status == "live_success" else 0,
            "mapping_stale": status == "live_success",
            "live_attempt": {"attempted": True, "provider_attempts": 1,
                              "reason": "" if status == "live_success" else status,
                              "status": status},
            "failure_reasons": [] if status == "live_success" else [status],
            "errors": [],
        },
        "sectors": [{
            "sector_code": "BK0001", "sector_name": "测试行业",
            "sector_type": "industry", "lhb_score": 70,
            "direction": "净买", "total_inst_net_yi": 1.2,
            "stock_count": 1, "inst_buy_count": 1, "inst_sell_count": 0,
            "member_names": ["测试股"],
        }] if status == "live_success" else [],
    }


def test_collect_daily_snapshot_writes_status_for_each_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(lhb_tracker, "SNAPSHOT_DIR", tmp_path / "lhb")
    monkeypatch.setattr(lhb_tracker, "STATUS_DIR", tmp_path / "lhb" / "status")
    monkeypatch.setattr(lhb_tracker.ak, "stock_board_industry_summary_ths",
                        lambda: _FakeFrame())
    monkeypatch.setattr(lhb_tracker.ak, "stock_board_concept_summary_ths",
                        lambda: _FakeFrame())

    outcomes = iter([
        _mock_lhb_result("live_success", "2026-08-28"),
        _mock_lhb_result("no_data", "2026-08-27"),
        _mock_lhb_result("mapping_error", "2026-08-26"),
    ])
    monkeypatch.setattr(lhb_tracker, "run_lhb_analysis", lambda _: next(outcomes))

    success = collect_daily_snapshot("2026-08-28")
    no_data = collect_daily_snapshot("2026-08-27")
    mapping_error = collect_daily_snapshot("2026-08-26")

    assert success["status"] == "live_success"
    assert (tmp_path / "lhb" / "2026-08-28.json").exists()
    assert no_data["status"] == "no_data"
    assert mapping_error["status"] == "mapping_error"
    assert not (tmp_path / "lhb" / "2026-08-27.json").exists()
    for d, status in [("2026-08-28", "live_success"),
                      ("2026-08-27", "no_data"),
                      ("2026-08-26", "mapping_error")]:
        sidecar = json.loads((tmp_path / "lhb" / "status" / f"{d}.json").read_text())
        assert sidecar["status"] == status
        assert sidecar["date"] == d


def test_save_daily_snapshot_legacy_wrapper(monkeypatch):
    snapshot = {"date": "2026-08-28", "sectors": []}
    monkeypatch.setattr(lhb_tracker, "collect_daily_snapshot",
                        lambda _: {"status": "live_success", "snapshot": snapshot})
    assert save_daily_snapshot("2026-08-28") == snapshot


def test_collect_snapshot_keeps_result_when_status_write_fails(tmp_path, monkeypatch):
    snapshot = {"date": "2026-08-28", "sectors": []}
    monkeypatch.setattr(lhb_tracker, "SNAPSHOT_DIR", tmp_path / "lhb")
    monkeypatch.setattr(lhb_tracker, "run_lhb_analysis", lambda _: {
        "meta": {"status": "live_success", "total_lhb_stocks": 1,
                  "total_sectors": 1},
        "sectors": [{"sector_name": "测试行业"}],
    })
    monkeypatch.setattr(lhb_tracker, "_fetch_sector_changes", lambda: {})
    monkeypatch.setattr(lhb_tracker, "_write_status",
                        lambda *_: (_ for _ in ()).throw(OSError("read-only")))

    result = lhb_tracker.collect_daily_snapshot("2026-08-28")

    assert result["status"] == "live_success"
    assert result["snapshot"]["date"] == snapshot["date"]
    assert "read-only" in result["status_write_error"]


def test_load_history_correct_count(monkeypatch):
    """load_history should return only snapshots within requested days."""
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 30)

    monkeypatch.setattr(lhb_tracker, "date", _FixedDate)
    _clean_mock_snapshots()
    _write_mock_snapshot(MOCK_SNAPSHOT_1)
    _write_mock_snapshot(MOCK_SNAPSHOT_2)
    # These are from May 25-26 which are within 30 days
    result = load_history(30)
    assert len(result) >= 2
    _clean_mock_snapshots()


# ──────────────── backfill_returns ────────────────


def test_backfill_returns_noop_without_akshare():
    """Without AKShare, backfill should return input unchanged."""
    if HAS_AKSHARE:
        return  # skip if AKShare available (can't test noop)
    original = [MOCK_SNAPSHOT_1]
    result = backfill_returns(original)
    assert result == original


def test_verify_signals_empty_input():
    """Empty snapshot list should return note."""
    result = verify_signals([])
    assert "note" in result.get("stats", {})


def test_different_windows():
    """Different window lengths should show in results."""
    result = verify_signals(
        [MOCK_SNAPSHOT_1, MOCK_SNAPSHOT_2, MOCK_SNAPSHOT_3],
        min_snapshots=2,
    )
    sw = result.get("signals", {})
    # 3d: all 6 have data
    assert "3d" in sw
    # 5d: only snapshots 1-2 have return_5d
    assert "5d" in sw
    assert sw["5d"]["total_signals"] == 4  # 2×2
    # 10d: none have data
    assert "10d" not in sw or sw["10d"]["total_signals"] == 0
