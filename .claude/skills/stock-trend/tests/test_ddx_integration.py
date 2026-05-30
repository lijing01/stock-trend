"""Test DDX and sector_mapper integration — 资金面数据."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers.ddx import fetch_ddx_ranking, compute_ddx_score
from fetchers.sector_mapper import aggregate_ddx_by_sector


# ──────────────── fetch_ddx_ranking ────────────────


def test_fetch_ddx_ranking_returns_list():
    result = fetch_ddx_ranking(top_n=50)
    assert isinstance(result, list)


def test_fetch_ddx_ranking_empty_ok():
    result = fetch_ddx_ranking(top_n=0)
    assert isinstance(result, list)


# ──────────────── compute_ddx_score ────────────────


def test_compute_ddx_score_high():
    assert compute_ddx_score({"ddx": 0.5, "ddx_days": 3}) == 100.0


def test_compute_ddx_score_moderate():
    assert compute_ddx_score({"ddx": 0.2, "ddx_days": 1}) == 80.0


def test_compute_ddx_score_negative():
    score = compute_ddx_score({"ddx": -0.3, "ddx_days": 0})
    assert 0 <= score <= 50


def test_compute_ddx_score_none():
    assert compute_ddx_score({}) == 50.0
    assert compute_ddx_score({"ddx": None}) == 50.0


# ──────────────── aggregate_ddx_by_sector ────────────────


def test_aggregate_ddx_empty_input():
    assert aggregate_ddx_by_sector([], {}) == []
    assert aggregate_ddx_by_sector([], {"mapping": {}}) == []


def test_aggregate_ddx_basic():
    ddx_list = [
        {"code": "600123", "ddx": 0.5, "ddx_days": 5, "super_order_ratio": 0.12},
        {"code": "600456", "ddx": 0.3, "ddx_days": 2, "super_order_ratio": 0.08},
    ]
    mapping = {
        "mapping": {
            "600123": [{"code": "BK1001", "name": "AI芯片", "type": "concept"}],
            "600456": [{"code": "BK1001", "name": "AI芯片", "type": "concept"}],
        }
    }
    result = aggregate_ddx_by_sector(ddx_list, mapping)
    assert len(result) == 1
    assert result[0]["sector_name"] == "AI芯片"
    assert result[0]["total_ddx_stocks"] == 2
    assert result[0]["ddx_inflow_count"] == 2


def test_aggregate_ddx_multi_sector():
    """Stock in multiple sectors should be counted in each."""
    ddx_list = [
        {"code": "600123", "ddx": 0.5, "ddx_days": 5, "super_order_ratio": 0.12},
    ]
    mapping = {
        "mapping": {
            "600123": [
                {"code": "BK1001", "name": "AI芯片", "type": "concept"},
                {"code": "BK2001", "name": "半导体", "type": "industry"},
            ],
        }
    }
    result = aggregate_ddx_by_sector(ddx_list, mapping)
    assert len(result) == 2
    names = {r["sector_name"] for r in result}
    assert "AI芯片" in names
    assert "半导体" in names
