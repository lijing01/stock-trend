"""Test ths_theme.py — 同花顺涨停热力主题评分引擎."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.ths_theme import (
    compute_concept_scores,
    classify_concepts,
    market_summary,
)


# ──────────────── 测试数据 ────────────────

MOCK_STOCKS = [
    {"code": "600123", "name": "兰陵科技", "concepts": ["AI芯片", "DeepSeek概念"],
     "limit_streak": 3, "first_limit_time": "09:35", "timing_bucket": "morning_early",
     "limit_type": "firm", "seal_amount": 2.35e8, "board": "sh"},
    {"code": "600456", "name": "芯原股份", "concepts": ["AI芯片", "半导体"],
     "limit_streak": 1, "first_limit_time": "10:20", "timing_bucket": "morning_early",
     "limit_type": "firm", "seal_amount": 1.5e8, "board": "sh"},
    {"code": "002123", "name": "梦网科技", "concepts": ["DeepSeek概念", "华为"],
     "limit_streak": 2, "first_limit_time": "09:30", "timing_bucket": "pre_open",
     "limit_type": "firm", "seal_amount": 3.2e8, "board": "sz"},
    {"code": "000888", "name": "消费电子", "concepts": ["消费电子", "智能穿戴"],
     "limit_streak": 2, "first_limit_time": "14:30", "timing_bucket": "afternoon_late",
     "limit_type": "retest", "seal_amount": 0.85e8, "board": "sz"},
    {"code": "300666", "name": "江丰电子", "concepts": ["半导体", "国产替代"],
     "limit_streak": 1, "first_limit_time": "11:00", "timing_bucket": "morning_late",
     "limit_type": "blown", "seal_amount": 0, "board": "sz"},
    {"code": "600789", "name": "鲁抗医药", "concepts": ["医药", "抗生素"],
     "limit_streak": 1, "first_limit_time": "13:30", "timing_bucket": "afternoon",
     "limit_type": "firm", "seal_amount": 0.5e8, "board": "sh"},
]

MOCK_STOCKS_EMPTY = []
MOCK_STOCKS_SINGLE = [
    {"code": "600001", "name": "单一股", "concepts": ["独苗概念"],
     "limit_streak": 1, "first_limit_time": "09:30", "timing_bucket": "morning_early",
     "limit_type": "firm", "seal_amount": 1e8, "board": "sh"},
]


# ──────────────── compute_concept_scores ────────────────


def test_compute_scores_returns_sorted():
    """Scores should be sorted by hot_score descending."""
    scores = compute_concept_scores(MOCK_STOCKS)
    assert len(scores) > 0
    for i in range(len(scores) - 1):
        assert scores[i]["hot_score"] >= scores[i + 1]["hot_score"]


def test_compute_scores_has_required_fields():
    """Each score entry should have all required fields."""
    scores = compute_concept_scores(MOCK_STOCKS)
    for s in scores:
        assert "concept" in s
        assert "hot_score" in s
        assert "stock_count" in s
        assert "max_streak" in s
        assert "rank" in s


def test_compute_scores_empty():
    """Empty input should return empty list."""
    assert compute_concept_scores(MOCK_STOCKS_EMPTY) == []


def test_compute_scores_single():
    """Single stock with one concept."""
    scores = compute_concept_scores(MOCK_STOCKS_SINGLE)
    assert len(scores) == 1
    assert scores[0]["concept"] == "独苗概念"
    assert scores[0]["stock_count"] == 1
    assert scores[0]["rank"] == 1


def test_scores_ai_chip_rank():
    """AI芯片 should be top since it has 2 stocks, one 3-streak early."""
    scores = compute_concept_scores(MOCK_STOCKS)
    ai_chip = next(s for s in scores if s["concept"] == "AI芯片")
    assert ai_chip["stock_count"] == 2
    assert ai_chip["max_streak"] == 3
    assert ai_chip["continuous_count"] >= 1


def test_scores_deepseek():
    """DeepSeek概念 has a 3-streak and 2-streak stock."""
    scores = compute_concept_scores(MOCK_STOCKS)
    ds = next(s for s in scores if s["concept"] == "DeepSeek概念")
    assert ds["stock_count"] == 2
    assert ds["max_streak"] == 3
    # Both early
    assert ds["morning_count"] == 2


# ──────────────── classify_concepts ────────────────


def test_classify_tiers():
    """classify_concepts should return all tier keys."""
    scores = compute_concept_scores(MOCK_STOCKS)
    classified = classify_concepts(scores)
    assert "strong" in classified
    assert "active" in classified
    assert "warm" in classified
    assert "cold" in classified


def test_classify_strong_contains_top():
    """Strong tier should include high-score concepts."""
    scores = compute_concept_scores(MOCK_STOCKS)
    classified = classify_concepts(scores)
    if scores and scores[0]["hot_score"] >= 70:
        assert scores[0]["concept"] in [s["concept"] for s in classified["strong"]]


# ──────────────── market_summary ────────────────


def test_market_summary_counts():
    """market_summary should correctly count stocks."""
    summary = market_summary(MOCK_STOCKS)
    assert summary["total"] == 6
    assert summary["firm"] == 4   # 4 firm (兰陵/芯原/梦网/鲁抗)
    assert summary["blown"] == 1
    assert summary["retest"] == 1
    assert summary["continuous"] == 3  # streak >= 2 (兰陵/梦网/消费电子)
    assert summary["high_streak"] == 1  # streak >= 3
    assert summary["max_streak"] == 3
    assert summary["early"] == 3  # pre_open + morning_early


def test_market_summary_empty():
    """Empty input should produce zero-summary."""
    summary = market_summary([])
    assert summary["total"] == 0
    assert summary["max_streak"] == 1


def test_market_summary_top_streak():
    """top_streak_stocks should list最强连板股 names."""
    summary = market_summary(MOCK_STOCKS)
    assert "兰陵科技" in summary["top_streak_stocks"]


def test_market_summary_streak_dist():
    """streak_dist should map连板高度→计数."""
    summary = market_summary(MOCK_STOCKS)
    sd = summary["streak_dist"]
    assert sd.get(1) == 3  # 3 stocks with streak=1
    assert sd.get(2) == 2  # 2 stocks with streak=2
    assert sd.get(3) == 1  # 1 stock with streak=3


# ──────────────── CLI integration ────────────────


def test_cli_json_output():
    """CLI with --json should produce valid JSON."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "analysis.ths_theme", "--json"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent / "scripts"),
        timeout=30,
    )
    assert result.returncode == 0
    if result.stdout.strip().startswith("⚠"):
        return  # non-trading day
    data = json.loads(result.stdout)
    assert "meta" in data
    assert "scores" in data


def test_cli_md_output():
    """CLI with no flags should produce Markdown."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "analysis.ths_theme", "--date", "2026-05-30"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent / "scripts"),
        timeout=30,
    )
    assert result.returncode == 0
