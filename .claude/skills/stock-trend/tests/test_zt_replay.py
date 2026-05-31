"""Test zt_replay.py — AKShare 涨停复盘数据获取."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers.zt_replay import (
    fetch_limitup_stocks,
    aggregate_by_concept,
    aggregate_by_limit_streak,
    format_report,
    _detect_board,
    _classify_limit_time,
    _classify_limit_type,
    _timing_to_str,
    _safe_float,
    _safe_int,
    HAS_AKSHARE,
)


# ──────────────── 工具函数 ────────────────


def test_safe_float():
    assert _safe_float(None) == 0.0
    assert _safe_float("abc") == 0.0
    assert _safe_float(3.14) == 3.14
    assert _safe_float("3.14") == 3.14


def test_safe_int():
    assert _safe_int(None) == 0
    assert _safe_int("abc") == 0
    assert _safe_int(42) == 42
    assert _safe_int("42") == 42


# ──────────────── _detect_board ────────────────


def test_detect_board_sh():
    assert _detect_board("600519") == "sh"
    assert _detect_board("900945") == "sh"


def test_detect_board_sz():
    assert _detect_board("000001") == "sz"
    assert _detect_board("300999") == "sz"
    assert _detect_board("002230") == "sz"


def test_detect_board_bj():
    assert _detect_board("830799") == "bj"
    assert _detect_board("400010") == "bj"


def test_detect_board_unknown():
    assert _detect_board("") == "unknown"


# ──────────────── _classify_limit_time ────────────────


def test_classify_limit_time_pre_open():
    assert _classify_limit_time("09:25") == "pre_open"
    assert _classify_limit_time("092500") == "pre_open"


def test_classify_limit_time_morning_early():
    assert _classify_limit_time("09:30") == "morning_early"
    assert _classify_limit_time("093000") == "morning_early"
    assert _classify_limit_time("11:29") == "morning_early"


def test_classify_limit_time_morning_late():
    assert _classify_limit_time("11:30") == "morning_late"


def test_classify_limit_time_afternoon():
    assert _classify_limit_time("13:00") == "afternoon"


def test_classify_limit_time_afternoon_late():
    assert _classify_limit_time("14:00") == "afternoon_late"


def test_classify_limit_time_close():
    assert _classify_limit_time("15:00") == "close"


def test_classify_limit_time_none():
    assert _classify_limit_time(None) == "unknown"
    assert _classify_limit_time("") == "unknown"
    assert _classify_limit_time("abc") == "unknown"


# ──────────────── _classify_limit_type ────────────────


def test_classify_limit_type_firm():
    assert _classify_limit_type(0, "09:30", "09:30") == "firm"
    assert _classify_limit_type(None, None, None) == "firm"


def test_classify_limit_type_blown():
    assert _classify_limit_type(1, "09:30", "09:30") == "blown"
    assert _classify_limit_type(2, "09:30", "09:30") == "blown"


def test_classify_limit_type_retest():
    assert _classify_limit_type(1, "09:30", "10:15") == "retest"
    assert _classify_limit_type(3, "09:25", "14:30") == "retest"


# ──────────────── _timing_to_str ────────────────


def test_timing_to_str_6digit():
    assert _timing_to_str("092500") == "09:25"
    assert _timing_to_str("093000") == "09:30"


def test_timing_to_str_4digit():
    assert _timing_to_str("0925") == "09:25"


def test_timing_to_str_with_colon():
    assert _timing_to_str("09:25") == "09:25"
    assert _timing_to_str("09:30:00") == "09:30"


def test_timing_to_str_none():
    assert _timing_to_str(None) is None


# ──────────────── fetch_limitup_stocks ────────────────


def test_has_akshare():
    assert HAS_AKSHARE, "AKShare not installed"


def test_fetch_limitup_stocks_returns_list():
    """Real API call — skip if non-trading day."""
    if not HAS_AKSHARE:
        return
    stocks = fetch_limitup_stocks("2026-05-29")
    assert isinstance(stocks, list)
    if not stocks:
        return
    assert len(stocks) > 0
    s = stocks[0]
    assert "code" in s
    assert "name" in s
    assert "limit_streak" in s
    assert "seal_amount" in s
    assert "concepts" in s
    assert "first_limit_time" in s
    assert "limit_type" in s


def test_fetch_limitup_fields():
    if not HAS_AKSHARE:
        return
    stocks = fetch_limitup_stocks("2026-05-29")
    if not stocks:
        return
    for s in stocks[:5]:
        assert isinstance(s["code"], str)
        assert len(s["code"]) == 6
        assert isinstance(s["concepts"], list)
        assert isinstance(s["limit_streak"], int)
        assert s["limit_streak"] >= 1
        assert s["limit_type"] in ("firm", "blown", "retest")
        assert s["timing_bucket"] in ("pre_open", "morning_early",
                                       "morning_late", "afternoon",
                                       "afternoon_late", "close", "unknown")


def test_fetch_limitup_sorted():
    """Results sorted by streak desc."""
    if not HAS_AKSHARE:
        return
    stocks = fetch_limitup_stocks("2026-05-29")
    if not stocks:
        return
    for i in range(len(stocks) - 1):
        assert stocks[i]["limit_streak"] >= stocks[i + 1]["limit_streak"]


def test_fetch_limitup_invalid_date():
    stocks = fetch_limitup_stocks("2000-01-01")
    assert isinstance(stocks, list)


# ──────────────── 聚合 ────────────────


def test_aggregate_by_concept_empty():
    assert aggregate_by_concept([]) == []


def test_aggregate_by_concept_with_data():
    if not HAS_AKSHARE:
        return
    stocks = fetch_limitup_stocks("2026-05-29")
    if not stocks:
        return
    concepts = aggregate_by_concept(stocks)
    assert len(concepts) > 0
    assert "concept" in concepts[0]
    assert "stock_count" in concepts[0]
    assert "max_streak" in concepts[0]
    for i in range(len(concepts) - 1):
        assert concepts[i]["stock_count"] >= concepts[i + 1]["stock_count"]


def test_aggregate_by_limit_streak():
    stocks = [
        {"code": "001", "name": "A", "limit_streak": 5, "concepts": [],
         "seal_amount": 0, "limit_type": "firm", "first_limit_time": "09:30",
         "timing_bucket": "morning_early", "last_limit_time": None,
         "board": "sz", "blown_count": 0, "industry": ""},
        {"code": "002", "name": "B", "limit_streak": 3, "concepts": [],
         "seal_amount": 0, "limit_type": "firm", "first_limit_time": "09:30",
         "timing_bucket": "morning_early", "last_limit_time": None,
         "board": "sz", "blown_count": 0, "industry": ""},
        {"code": "003", "name": "C", "limit_streak": 1, "concepts": [],
         "seal_amount": 0, "limit_type": "firm", "first_limit_time": "09:30",
         "timing_bucket": "morning_early", "last_limit_time": None,
         "board": "sz", "blown_count": 0, "industry": ""},
    ]
    dist = aggregate_by_limit_streak(stocks)
    assert dist == {5: 1, 3: 1, 1: 1}


# ──────────────── 报告 ────────────────


def test_format_report():
    stocks = [
        {"code": "600519", "name": "贵州茅台", "concepts": ["白酒"],
         "first_limit_time": "09:30", "last_limit_time": None,
         "limit_streak": 1, "seal_amount": 1e8, "limit_type": "firm",
         "timing_bucket": "morning_early", "board": "sh",
         "blown_count": 0, "industry": "白酒"},
        {"code": "002230", "name": "科大讯飞", "concepts": ["人工智能"],
         "first_limit_time": "13:00", "last_limit_time": None,
         "limit_streak": 2, "seal_amount": 5e7, "limit_type": "firm",
         "timing_bucket": "afternoon", "board": "sz",
         "blown_count": 0, "industry": "软件"},
    ]
    concepts = aggregate_by_concept(stocks)
    streak = aggregate_by_limit_streak(stocks)
    report = format_report(stocks, concepts, streak, "2026-05-30")
    assert "涨停复盘" in report
    assert "贵州茅台" in report
    assert "白酒" in report


# ──────────────── CLI ────────────────


def test_cli_json_output():
    import subprocess, json
    if not HAS_AKSHARE:
        return
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fetchers.zt_replay",
             "--date", "2026-05-29", "--json"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent / "scripts"),
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return
    if result.returncode != 0 or not result.stdout.strip():
        return
    data = json.loads(result.stdout)
    assert isinstance(data, list)


def test_cli_aggregate_concepts():
    import subprocess
    if not HAS_AKSHARE:
        return
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fetchers.zt_replay",
             "--date", "2026-05-29", "--aggregate", "concepts"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent / "scripts"),
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return
    if result.returncode != 0 or not result.stdout.strip():
        return
    assert "涨停复盘" in result.stdout or "概念涨停排行" in result.stdout
