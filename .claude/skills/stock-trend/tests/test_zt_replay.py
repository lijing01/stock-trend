"""Test zt_replay.py — 涨停复盘爬虫.

Uses mock HTML snippets to validate HTML parsing logic.
Live fetching from 同花顺 is environment-dependent (may be blocked).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.ths_utils import extract_table_rows, parse_amount, fetch_page

# ──────── Mock HTML for unit tests ────────

MOCK_ZT_HTML_SIMPLE = """
<table class="m-table">
<tr>
    <td>1</td>
    <td><a href="/stock/600123/">600123</a></td>
    <td><a href="/stock/600123/">兰陵科技</a></td>
    <td>DeepSeek概念,AI芯片,国产替代</td>
    <td>09:35</td>
    <td>3连板</td>
    <td>2.35亿</td>
</tr>
<tr>
    <td>2</td>
    <td><a href="/stock/000001/">000001</a></td>
    <td><a href="/stock/000001/">平安银行</a></td>
    <td>银行,高股息</td>
    <td>10:20</td>
    <td>首板</td>
    <td>1.20亿</td>
</tr>
<tr>
    <td>3</td>
    <td><a href="/stock/300999/">300999</a></td>
    <td><a href="/stock/300999/">消费电子</a></td>
    <td>消费电子,智能穿戴</td>
    <td>14:30</td>
    <td>2连板</td>
    <td>0.85亿</td>
</tr>
</table>
"""

MOCK_ZT_HTML_WITH_BLOWN = """
<table>
<tr><td>1</td><td>002123</td><td>梦网科技</td><td>AI智能体,华为</td><td>09:45</td><td>2连板</td><td>炸板</td></tr>
<tr><td>2</td><td>600456</td><td>宝钛股份</td><td>军工,大飞机</td><td>09:30</td><td>首板</td><td>涨停</td><td>3.50亿</td></tr>
</table>
"""

MOCK_ZT_HTML_EMPTY = """
<html><head><title>同花顺</title></head><body>无涨停</body></html>
"""


def test_extract_table_rows():
    """extract_table_rows should parse <tr>/<td> correctly."""
    rows = extract_table_rows(MOCK_ZT_HTML_SIMPLE)
    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}"
    assert "600123" in rows[0][1]
    assert "兰陵科技" in rows[0][2]
    assert "平安银行" in rows[1][2]


def test_parse_amount_yi():
    """parse_amount should handle 亿 suffix."""
    assert parse_amount("2.35亿") == 2.35e8
    assert parse_amount("0.85亿") == 0.85e8
    assert parse_amount("3.50亿") == 3.5e8


def test_parse_amount_wan():
    """parse_amount should handle 万 suffix."""
    assert parse_amount("5000万") == 5e7
    assert parse_amount("120万") == 1.2e6


def test_parse_amount_range():
    """parse_amount should handle range like 1.2-2.3亿."""
    val = parse_amount("1.2-2.3亿")
    # Should be mid point: (1.2+2.3)/2 = 1.75亿
    assert abs(val - 1.75e8) < 1e6, f"Expected ~1.75e8, got {val}"


def test_parse_amount_raw_number():
    """parse_amount should handle raw number."""
    assert parse_amount("12345") == 12345.0
    assert parse_amount("") == 0.0
    assert parse_amount("-") == 0.0


def test_empty_table_returns_empty():
    """extract_table_rows on non-table HTML returns empty list."""
    rows = extract_table_rows(MOCK_ZT_HTML_EMPTY)
    assert len(rows) == 0


# ──────── zt_replay parsing tests ────────


def _parse_stocks_from_mock(html: str) -> list[dict]:
    """Helper: run zt_replay parsing logic on mock HTML."""
    from fetchers.zt_replay import extract_table_rows, _parse_concepts, _parse_limit_streak, _classify_limit_type, _detect_board, parse_amount
    import re

    rows = extract_table_rows(html)
    results = []
    seen = set()
    for cells in rows:
        if len(cells) < 4:
            continue
        code = None
        for c in cells[:3]:
            m = re.search(r'\b(\d{6})\b', c)
            if m:
                code = m.group(1)
                break
        if not code or code in seen:
            continue
        seen.add(code)

        name = cells[2] if len(cells) > 2 else ""
        concepts_raw = ""
        streak_raw = "1"
        for cell in cells[3:]:
            stripped = cell.strip()
            # Skip cells that look like limit-streak or封单 amounts
            if re.search(r'[连板首板板]', cell):
                streak_raw = cell
                continue
            if re.search(r'[亿万]', cell):
                continue  # seal amount, not concept
            # Match Chinese-only OR mixed Chinese/English concept strings
            if (re.match(r'^[一-鿿，,、\s]{2,60}$', stripped) or
                re.match(r'^[一-鿿A-Za-z0-9，,、/\s]{2,80}$', stripped)):
                concepts_raw = cell

        concepts = _parse_concepts(concepts_raw)
        limit_streak = _parse_limit_streak(streak_raw)
        limit_type = _classify_limit_type(cells, limit_streak)
        board = _detect_board(code)

        results.append({
            "code": code,
            "name": name,
            "concepts": concepts,
            "limit_streak": limit_streak,
            "limit_type": limit_type,
            "board": board,
        })
    return results


def test_parse_mock_simple():
    stocks = _parse_stocks_from_mock(MOCK_ZT_HTML_SIMPLE)
    assert len(stocks) == 3, f"Expected 3, got {len(stocks)}"

    s1 = stocks[0]
    assert s1["code"] == "600123"
    assert "DeepSeek概念" in s1["concepts"]
    assert "AI芯片" in s1["concepts"]
    assert s1["limit_streak"] == 3
    assert s1["limit_type"] == "firm"
    assert s1["board"] == "sh"

    s2 = stocks[1]
    assert s2["code"] == "000001"
    assert s2["limit_streak"] == 1
    assert s2["board"] == "sz"

    s3 = stocks[2]
    assert s3["code"] == "300999"
    assert s3["limit_streak"] == 2
    assert s3["board"] == "sz"  # 300xxx is sz


def test_parse_blown_limit_type():
    """炸板 should be classified as blown."""
    stocks = _parse_stocks_from_mock(MOCK_ZT_HTML_WITH_BLOWN)
    assert len(stocks) == 2
    s1 = next(s for s in stocks if s["code"] == "002123")
    assert s1["limit_type"] == "blown", f"Expected blown, got {s1['limit_type']}"

    s2 = next(s for s in stocks if s["code"] == "600456")
    assert s2["limit_type"] == "firm", f"Expected firm, got {s2['limit_type']}"


def test_parse_concepts_various():
    from fetchers.zt_replay import _parse_concepts

    assert _parse_concepts("DeepSeek概念,AI芯片") == ["DeepSeek概念", "AI芯片"]
    assert _parse_concepts("人工智能;大数据") == ["人工智能", "大数据"]
    assert _parse_concepts("-") == []
    assert _parse_concepts("") == []


def test_aggregate_by_concept():
    from fetchers.zt_replay import aggregate_by_concept

    stocks = [
        {"code": "600123", "name": "A", "concepts": ["AI", "芯片"], "limit_streak": 3, "first_limit_time": "09:30", "timing_bucket": "morning_early", "seal_amount": 1e8},
        {"code": "600456", "name": "B", "concepts": ["AI"], "limit_streak": 1, "first_limit_time": "10:00", "timing_bucket": "morning_early", "seal_amount": 5e7},
        {"code": "000001", "name": "C", "concepts": ["芯片"], "limit_streak": 2, "first_limit_time": "14:00", "timing_bucket": "afternoon", "seal_amount": 2e7},
    ]

    agg = aggregate_by_concept(stocks)
    assert len(agg) >= 2

    ai = next(c for c in agg if c["concept"] == "AI")
    assert ai["stock_count"] == 2
    assert ai["max_streak"] == 3

    chip = next(c for c in agg if c["concept"] == "芯片")
    assert chip["stock_count"] == 2


# ──────── CLI integration tests ────────


def test_cli_today_json():
    """CLI with --json — handles both data and empty gracefully."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "fetchers.zt_replay", "--json"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent / "scripts")
    )
    assert result.returncode == 0
    # In non-trading day or blocked env, output starts with ⚠
    # On trading day with data, output is valid JSON list
    if result.stdout.strip().startswith("⚠"):
        return  # graceful — non-trading day or env block
    data = json.loads(result.stdout)
    assert isinstance(data, list)


def test_cli_aggregate_concepts():
    """CLI --aggregate concepts — handles both data and empty gracefully."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "fetchers.zt_replay", "--aggregate", "concepts", "--json"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent / "scripts")
    )
    assert result.returncode == 0
    if result.stdout.strip().startswith("⚠"):
        return  # graceful
    data = json.loads(result.stdout)
    assert isinstance(data, list)


# ──────── ths_utils unit tests ────────


def test_ths_utils_fetch_page_handles_403():
    """fetch_page should return None on non-accessible domains (graceful)."""
    html = fetch_page("https://data.10jqka.com.cn/financial/zt/", timeout=5)
    # In restricted env returns None, in local may return HTML
    assert html is None or "涨停" in html
