"""Test DDX integration in ths_theme.py — 资金面交叉验证."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.ths_theme import (
    compute_concept_scores,
    cross_reference_with_ddx,
    compute_combined_score,
    get_ddx_sector_data,
    market_summary,
    classify_concepts,
)
from fetchers.ddx import fetch_ddx_ranking, compute_ddx_score
from fetchers.sector_mapper import aggregate_ddx_by_sector


# ──────────────── Mock test data ────────────────

MOCK_CONCEPT_SCORES = [
    {"concept": "AI芯片", "hot_score": 85, "stock_count": 3, "max_streak": 3,
     "members": [{"code": "600123", "name": "兰陵科技", "limit_streak": 3}],
     "continuous_count": 2, "morning_ratio": 0.67, "avg_seal": 2e8,
     "continuous_ratio": 0.67, "morning_count": 2, "blown_count": 0,
     "blown_ratio": 0, "retest_count": 0},
    {"concept": "DeepSeek概念", "hot_score": 72, "stock_count": 2, "max_streak": 3,
     "members": [{"code": "002123", "name": "梦网科技", "limit_streak": 2}],
     "continuous_count": 2, "morning_ratio": 1.0, "avg_seal": 2.8e8,
     "continuous_ratio": 1.0, "morning_count": 2, "blown_count": 0,
     "blown_ratio": 0, "retest_count": 0},
    {"concept": "半导体", "hot_score": 55, "stock_count": 2, "max_streak": 1,
     "members": [{"code": "600456", "name": "芯原股份", "limit_streak": 1}],
     "continuous_count": 0, "morning_ratio": 0.5, "avg_seal": 1e8,
     "continuous_ratio": 0, "morning_count": 1, "blown_count": 1,
     "blown_ratio": 0.5, "retest_count": 0},
    {"concept": "独苗概念", "hot_score": 40, "stock_count": 1, "max_streak": 1,
     "members": [{"code": "600001", "name": "单一股", "limit_streak": 1}],
     "continuous_count": 0, "morning_ratio": 1.0, "avg_seal": 1e8,
     "continuous_ratio": 0, "morning_count": 1, "blown_count": 0,
     "blown_ratio": 0, "retest_count": 0},
]

MOCK_DDX_SECTORS = [
    {"sector_code": "BK1001", "sector_name": "AI芯片", "sector_type": "concept",
     "total_ddx_stocks": 15, "ddx_inflow_count": 12, "ddx_inflow_ratio": 0.8,
     "continuous_count": 8, "continuous_ratio": 0.533,
     "high_super_count": 5, "avg_ddx": 0.32, "avg_super_order_ratio": 0.08,
     "ddx_score": 85.0, "member_codes": ["600123", "600456"]},
    {"sector_code": "BK1002", "sector_name": "DeepSeek概念", "sector_type": "concept",
     "total_ddx_stocks": 10, "ddx_inflow_count": 7, "ddx_inflow_ratio": 0.7,
     "continuous_count": 4, "continuous_ratio": 0.4,
     "high_super_count": 3, "avg_ddx": 0.25, "avg_super_order_ratio": 0.06,
     "ddx_score": 72.0, "member_codes": ["002123"]},
]


# ──────────────── fetch_ddx_ranking ────────────────


def test_fetch_ddx_ranking_returns_list():
    """fetch_ddx_ranking returns list (may be empty in restricted env)."""
    result = fetch_ddx_ranking(top_n=50)
    assert isinstance(result, list)


def test_fetch_ddx_ranking_empty_ok():
    """Empty result is valid (graceful degradation)."""
    result = fetch_ddx_ranking(top_n=0)
    assert isinstance(result, list)


# ──────────────── compute_ddx_score ────────────────


def test_compute_ddx_score_high():
    """High DDX with days should score 100."""
    assert compute_ddx_score({"ddx": 0.5, "ddx_days": 3}) == 100.0


def test_compute_ddx_score_moderate():
    """Moderate DDX should score 80."""
    assert compute_ddx_score({"ddx": 0.2, "ddx_days": 1}) == 80.0


def test_compute_ddx_score_negative():
    """Negative DDX should score low but >= 0."""
    score = compute_ddx_score({"ddx": -0.3, "ddx_days": 0})
    assert 0 <= score <= 50


def test_compute_ddx_score_none():
    """None DDX should return neutral 50."""
    assert compute_ddx_score({}) == 50.0
    assert compute_ddx_score({"ddx": None}) == 50.0


# ──────────────── aggregate_ddx_by_sector ────────────────


def test_aggregate_ddx_empty_input():
    """Empty inputs should return empty list."""
    assert aggregate_ddx_by_sector([], {}) == []
    assert aggregate_ddx_by_sector([], {"mapping": {}}) == []


def test_aggregate_ddx_basic():
    """aggregate_ddx_by_sector should produce scored output."""
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
    assert result[0]["continuous_count"] == 1  # only 600123 has ddx_days>=3


# ──────────────── cross_reference_with_ddx ────────────────


def test_cross_reference_exact_match():
    """Exact name match should populate DDX fields."""
    result = cross_reference_with_ddx(MOCK_CONCEPT_SCORES, MOCK_DDX_SECTORS)
    ai = next(c for c in result if c["concept"] == "AI芯片")
    assert ai["ddx_cross"] is True
    assert ai["ddx_score"] == 85.0
    assert ai["ddx_inflow_count"] == 12


def test_cross_reference_no_match():
    """Unmatched concept gets ddx_cross=False."""
    result = cross_reference_with_ddx(MOCK_CONCEPT_SCORES, MOCK_DDX_SECTORS)
    um = next(c for c in result if c["concept"] == "独苗概念")
    assert um["ddx_cross"] is False
    assert um["ddx_score"] == 0


def test_cross_reference_empty_ddx():
    """Empty DDX data should return concept scores unchanged (no ddx_cross added)."""
    import copy
    # Use a fresh copy to avoid mutation from previous tests
    fresh = copy.deepcopy([MOCK_CONCEPT_SCORES[0].copy()])
    # Remove any ddx keys that might have been set by prior tests
    for k in list(fresh[0].keys()):
        if k.startswith("ddx_"):
            del fresh[0][k]
    result = cross_reference_with_ddx(fresh, [])
    # Should return same list, no ddx keys added
    assert len(result) == 1
    assert not any(k.startswith("ddx_") for k in result[0].keys())


# ──────────────── compute_combined_score ────────────────


def test_combined_score_blends():
    """Combined score mixes limit-up and DDX scores."""
    crossed = cross_reference_with_ddx(MOCK_CONCEPT_SCORES, MOCK_DDX_SECTORS)
    combined = compute_combined_score(crossed)
    ai = next(c for c in combined if c["concept"] == "AI芯片")
    # Should have both original and combined
    assert ai["hot_score_limit"] == 85.0
    assert ai["hot_score_ddx"] == 85.0
    # Combined = 85*0.7 + 85*0.3 = 85.0 (same since both equal)
    assert ai["hot_score"] == 85.0


def test_combined_score_unaffected():
    """Concepts without DDX match should keep original score."""
    crossed = cross_reference_with_ddx(MOCK_CONCEPT_SCORES, MOCK_DDX_SECTORS)
    combined = compute_combined_score(crossed)
    um = next(c for c in combined if c["concept"] == "独苗概念")
    assert um["hot_score"] == 40.0
    assert "hot_score_limit" not in um


# ──────────────── get_ddx_sector_data ────────────────


def test_get_ddx_sector_data_returns_list():
    """get_ddx_sector_data returns list (may be empty in restricted env)."""
    result = get_ddx_sector_data()
    assert isinstance(result, list)


# ──────────────── market_summary (DDX-aware) ────────────────


def test_market_summary_basic():
    """market_summary should return expected structure."""
    from tests.test_ths_theme import MOCK_STOCKS
    summary = market_summary(MOCK_STOCKS)
    assert "total" in summary
    assert "streak_dist" in summary
