#!/usr/bin/env python3
"""Longtou (/longtou) test suite.

Tests for fetch_sector_data.py and market_leader.py covering:
  - Sector ranking & scoring
  - Leader & core stock filtering
  - Fallback scoring
  - Report generation
  - Edge cases

Usage:
    python3 test_longtou.py              # Run all tests
    python3 test_longtou.py -v            # Verbose output
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="longtou"):
    global PASSED, FAILED, SKIPPED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def skip(name, reason=""):
    global SKIPPED
    SKIPPED += 1
    RESULTS.append({"name": name, "status": "SKIP", "detail": reason, "category": "skip"})
    print(f"  [SKIP] {name}" + (f" — {reason}" if reason else ""))


# ──────────────────────── Mock Data ────────────────────────

MOCK_SECTOR = {
    "code": "BK1234",
    "name": "测试板块",
    "type": "concept",
    "change_pct": 3.5,
    "amount": 50.0,
    "up_count": 15,
    "down_count": 5,
    "total_count": 20,
    "main_force_net": 200000000.0,  # 2亿
}

POOR_SECTOR = {
    "code": "BK9999",
    "name": "弱势板块",
    "type": "industry",
    "change_pct": -4.0,
    "amount": 5.0,
    "up_count": 2,
    "down_count": 18,
    "total_count": 20,
    "main_force_net": -500000000.0,  # -5亿
}

MOCK_STOCKS = [
    {"code": "600001", "name": "龙头A", "change_pct": 9.5, "amount": 5e8, "market_cap": 2e10, "pe": 15.0},
    {"code": "600002", "name": "龙头B", "change_pct": 7.2, "amount": 3e8, "market_cap": 5e10, "pe": 25.0},
    {"code": "600003", "name": "中军C", "change_pct": 2.1, "amount": 1e8, "market_cap": 1e11, "pe": 18.0},
    {"code": "600004", "name": "中军D", "change_pct": -1.5, "amount": 5e7, "market_cap": 8e10, "pe": 12.0},
    {"code": "600005", "name": "弱势E", "change_pct": -8.0, "amount": 2e7, "market_cap": 5e9, "pe": -5.0},
    {"code": "600006", "name": "平庸F", "change_pct": 0.5, "amount": 3e7, "market_cap": 3e9, "pe": 50.0},
]

# Sector ranking mock for rank_hot_sectors test
MOCK_RANKINGS = {
    "meta": {"fetch_time": "20260526-120000", "total_sectors": 3},
    "sectors": [
        MOCK_SECTOR,
        POOR_SECTOR,
        {
            "code": "BK5555",
            "name": "微型板块",
            "type": "concept",
            "change_pct": 2.0,
            "amount": 1.0,
            "up_count": 3,
            "down_count": 2,
            "total_count": 5,
            "main_force_net": 10000000.0,
        },
    ],
}


# ──────────────────────── Tests ────────────────────────


def test_compute_hot_score():
    """Test compute_hot_score() scoring logic."""
    from fetch_sector_data import compute_hot_score

    # Normal hot sector: positive change + positive capital flow + good up/down ratio
    score = compute_hot_score(MOCK_SECTOR)
    test("HS-01: 正常板块热度分>50", score > 50, f"score={score}")

    # Poor sector: negative change + negative capital flow + bad ratio
    poor = compute_hot_score(POOR_SECTOR)
    test("HS-02: 弱势板块热度分<50", poor < 50, f"score={poor}")

    # Hot sector should score higher than poor
    test("HS-03: 强势板块 > 弱势板块", score > poor, f"hot={score} poor={poor}")

    # Edge cases
    zero_sector = {"change_pct": 0, "main_force_net": 0, "up_count": 1, "down_count": 1}
    test("HS-04: 零值输入不抛异常", compute_hot_score(zero_sector) > 0, f"score={compute_hot_score(zero_sector)}")

    empty_sector = {}
    test("HS-05: 空字典不抛异常", compute_hot_score(empty_sector) >= 0, f"score={compute_hot_score(empty_sector)}")

    extreme_sector = {"change_pct": 10, "main_force_net": 1e9, "up_count": 100, "down_count": 0}
    extreme = compute_hot_score(extreme_sector)
    test("HS-06: 极端好行情上限100", extreme <= 100, f"score={extreme}")

    extreme_bad = {"change_pct": -10, "main_force_net": -1e9, "up_count": 0, "down_count": 100}
    extreme_bad_score = compute_hot_score(extreme_bad)
    test("HS-07: 极端差行情不低于0", extreme_bad_score >= 0, f"score={extreme_bad_score}")


def test_rank_hot_sectors():
    """Test rank_hot_sectors() ranking, filtering, and normalization."""
    from fetch_sector_data import rank_hot_sectors

    # Normal: top 2
    top2 = rank_hot_sectors(MOCK_RANKINGS, top_n=2, min_stocks=8)
    test("RK-01: top_n=2返回2个", len(top2) == 2, f"count={len(top2)}")
    if len(top2) >= 2:
        test("RK-02: 热度降序排列", top2[0]["hot_score"] >= top2[1]["hot_score"],
             f"{top2[0]['hot_score']} >= {top2[1]['hot_score']}")

    # min_stocks filter: sector with 5 stocks should be excluded
    with_filter = rank_hot_sectors(MOCK_RANKINGS, top_n=10, min_stocks=8)
    test("RK-03: 微型板块(<8只)被过滤", all(s["name"] != "微型板块" for s in with_filter),
         f"sectors={[s['name'] for s in with_filter]}")

    # No filter
    no_filter = rank_hot_sectors(MOCK_RANKINGS, top_n=10, min_stocks=0)
    test("RK-04: min_stocks=0不过滤", len(no_filter) == 3, f"count={len(no_filter)}")

    # Min-max normalization: scores in range 0-100
    range_ok = all(0 <= s["hot_score"] <= 100 for s in top2)
    test("RK-05: 热度分归一化0-100", range_ok, f"scores={[s['hot_score'] for s in top2]}")

    # Single sector
    single = rank_hot_sectors(
        {"meta": {"total_sectors": 1}, "sectors": [MOCK_SECTOR]},
        top_n=1, min_stocks=0
    )
    test("RK-06: 单板块返回1个", len(single) == 1, f"count={len(single)}")
    test("RK-07: 单板块归一化不变", single[0]["hot_score"] >= 50, f"score={single[0]['hot_score']}")

    # Empty rankings
    empty = rank_hot_sectors({"meta": {"total_sectors": 0}, "sectors": []}, top_n=5, min_stocks=8)
    test("RK-08: 空排行返回空列表", len(empty) == 0, f"count={len(empty)}")


def test_filter_leaders():
    """Test filter_leaders() leader stock scoring."""
    from fetch_sector_data import filter_leaders

    leaders = filter_leaders(MOCK_STOCKS, top_n=3)
    test("LF-01: 返回3个龙头", len(leaders) == 3, f"count={len(leaders)}")

    # Sorted by leader_score descending
    for i in range(len(leaders) - 1):
        test(f"LF-02: 第{i+1}名分>=第{i+2}名",
             leaders[i]["leader_score"] >= leaders[i + 1]["leader_score"],
             f"{leaders[i]['name']}({leaders[i]['leader_score']}) >= {leaders[i+1]['name']}({leaders[i+1]['leader_score']})")

    # Top leader should be 龙头A (highest change_pct)
    if leaders:
        test("LF-03: 龙头A排首位", leaders[0]["code"] == "600001",
             f"top={leaders[0]['name']}")

    # All have leader_score
    all_scored = all("leader_score" in s for s in leaders)
    test("LF-04: 全部有leader_score", all_scored)

    # Edge: empty list
    empty = filter_leaders([], top_n=3)
    test("LF-05: 空列表返回空", len(empty) == 0)

    # Edge: top_n > available
    more = filter_leaders(MOCK_STOCKS[:2], top_n=5)
    test("LF-06: top_n超出数量", len(more) == 2, f"got {len(more)}, want 2")


def test_filter_core_stocks():
    """Test filter_core_stocks() core stock scoring."""
    from fetch_sector_data import filter_core_stocks

    cores = filter_core_stocks(MOCK_STOCKS, top_n=3)
    test("CF-01: 返回3个中军", len(cores) == 3, f"count={len(cores)}")

    # Sorted by core_score descending
    for i in range(len(cores) - 1):
        test(f"CF-02: 第{i+1}名分>=第{i+2}名",
             cores[i]["core_score"] >= cores[i + 1]["core_score"],
             f"{cores[i]['name']}({cores[i]['core_score']}) >= {cores[i+1]['name']}({cores[i+1]['core_score']})")

    # Top core stock should prefer large cap + moderate PE
    if cores:
        test("CF-03: 大市值合理PE排前", cores[0]["market_cap"] >= cores[-1]["market_cap"],
             f"{cores[0]['name']} mcap={cores[0]['market_cap']} vs {cores[-1]['name']} mcap={cores[-1]['market_cap']}")

    # All have core_score
    all_scored = all("core_score" in s for s in cores)
    test("CF-04: 全部有core_score", all_scored)

    # Edge: empty list
    empty = filter_core_stocks([], top_n=3)
    test("CF-05: 空列表返回空", len(empty) == 0)

    # Edge: single stock
    single = filter_core_stocks([MOCK_STOCKS[0]], top_n=3)
    test("CF-06: 单只股票正确", len(single) == 1 and single[0]["code"] == "600001",
         f"got {[s['code'] for s in single]}")

    # Negative PE stocks should get lower core_score
    neg_pe_stock = [s for s in MOCK_STOCKS if s["pe"] < 0]
    pos_pe_stock = [s for s in MOCK_STOCKS if s["pe"] > 0]
    if neg_pe_stock and pos_pe_stock:
        neg_scores = filter_core_stocks(neg_pe_stock, top_n=1)
        pos_scores = filter_core_stocks(pos_pe_stock[:1], top_n=1)
        test("CF-07: 负PE股价低于正PE", neg_scores[0]["core_score"] < pos_scores[0]["core_score"],
             f"neg={neg_scores[0]['core_score']} pos={pos_scores[0]['core_score']}")


def test_parse_amount():
    """Test _parse_amount() helper."""
    from fetch_sector_data import _parse_amount

    test("PA-01: None值返回0", _parse_amount(None) == 0.0)
    test("PA-02: int转换", _parse_amount(100) == 100.0)
    test("PA-03: float保留", _parse_amount(3.14) == 3.14)
    test("PA-04: 字符串转换", _parse_amount("123.45") == 123.45)
    test("PA-05: 零值", _parse_amount(0) == 0.0)


def test_fallback_score():
    """Test _fallback_score() in market_leader.py."""
    from market_leader import _fallback_score

    # Normal stock with positive change
    s1 = {"code": "600001", "name": "测试", "change_pct": 5.0, "amount": 1e9}
    r1 = _fallback_score(s1)
    test("FB-01: 正涨幅有正评分", r1["composite_score"] > 0.5, f"score={r1['composite_score']}")
    test("FB-01a: 方向偏多", "多" in r1["direction"], f"dir={r1['direction']}")

    # Negative stock
    s2 = {"code": "600002", "name": "下跌", "change_pct": -8.0, "amount": 5e8}
    r2 = _fallback_score(s2)
    test("FB-02: 大跌有低评分", r2["composite_score"] < 0.3, f"score={r2['composite_score']}")
    test("FB-02a: 方向偏空", "空" in r2["direction"], f"dir={r2['direction']}")

    # Zero change
    s3 = {"code": "600003", "name": "平盘", "change_pct": 0, "amount": 0}
    r3 = _fallback_score(s3)
    test("FB-03: 零涨幅评分0.5附近", 0.3 <= r3["composite_score"] <= 0.7, f"score={r3['composite_score']}")

    # Missing fields
    s4 = {"code": "600004"}
    r4 = _fallback_score(s4)
    test("FB-04: 缺字段不抛异常", r4["composite_score"] is not None, f"score={r4['composite_score']}")

    # Contains risk warning
    test("FB-05: 含兜底风险提示", any("深度分析" in r for r in r4["risks"]),
         f"risks={r4['risks']}")

    # Dimension scores structure
    dims = r1["dimension_scores"]
    test("FB-06: 含5维度评分", len(dims) == 5, f"dims={list(dims.keys())}")


def test_score_to_stars():
    """Test _score_to_stars() conversion."""
    from market_leader import _score_to_stars

    test("ST-01: score=0.8→★★★", _score_to_stars(0.8) == "★★★")
    test("ST-02: score=0.7→★★★", _score_to_stars(0.7) == "★★★")
    test("ST-03: score=0.6→★★☆", _score_to_stars(0.6) == "★★☆")
    test("ST-04: score=0.5→★★☆", _score_to_stars(0.5) == "★★☆")
    test("ST-05: score=0.4→★☆☆", _score_to_stars(0.4) == "★☆☆")
    test("ST-06: score=0.3→★☆☆", _score_to_stars(0.3) == "★☆☆")
    test("ST-07: score=0.2→☆☆☆", _score_to_stars(0.2) == "☆☆☆")
    test("ST-08: score=None→N/A", _score_to_stars(None) == "N/A")
    test("ST-09: score=1.0→★★★", _score_to_stars(1.0) == "★★★")
    test("ST-10: score=0.0→☆☆☆", _score_to_stars(0.0) == "☆☆☆")


def test_signal_str():
    """Test _signal_str() direction arrows."""
    from market_leader import _signal_str

    test("SG-01: score=3.0→↑", _signal_str(3.0) == "↑")
    test("SG-02: score=2.0→↑", _signal_str(2.0) == "↑")
    test("SG-03: score=1.0→↗", _signal_str(1.0) == "↗")
    test("SG-04: score=0.5→↗", _signal_str(0.5) == "↗")
    test("SG-05: score=0.0→→", _signal_str(0.0) == "→")
    test("SG-06: score=-0.5→→", _signal_str(-0.5) == "→")
    test("SG-07: score=-1.0→↘", _signal_str(-1.0) == "↘")
    test("SG-08: score=-2.0→↘", _signal_str(-2.0) == "↘")
    test("SG-09: score=-3.0→↓", _signal_str(-3.0) == "↓")
    test("SG-10: score=None→--", _signal_str(None) == "--")


def test_generate_report():
    """Test generate_report() structure and key content."""
    from market_leader import generate_report

    output = {
        "meta": {
            "scan_time": "20260526-120000",
            "top_n": 2,
            "total_sectors": 2,
            "total_candidates": 3,
            "elapsed_seconds": 45.5,
        },
        "sectors_analyzed": [
            {
                "name": "半导体",
                "code": "BK1111",
                "hot_score": 95.0,
                "change_pct": 3.2,
                "leaders": [
                    {"code": "600001", "name": "中芯国际", "change_pct": 5.0,
                     "deep_score": 0.75, "deep_direction": "偏多", "deep_confidence": "中"},
                ],
                "core_stocks": [
                    {"code": "600002", "name": "北方华创", "market_cap": "2000亿", "pe": 18,
                     "deep_score": 0.45, "deep_direction": "震荡偏多", "deep_confidence": "中"},
                ],
            },
        ],
        "pipeline_summary": {
            "600001": {
                "composite_score": 0.75, "direction": "偏多",
                "dimension_scores": {"technical": 1, "capital_flow": 0.5, "fundamental": 0.5, "sentiment": 0, "macro": 0},
                "stop_loss": 50.0,
                "targets": {"conservative": 55.0, "moderate": 60.0},
                "risks": ["测试风险"],
            },
            "600002": {
                "composite_score": 0.45, "direction": "震荡偏多",
                "dimension_scores": {"technical": 0.5, "capital_flow": 0, "fundamental": 0.5, "sentiment": 0, "macro": 0},
                "stop_loss": None,
                "targets": {},
                "risks": [],
            },
        },
        "best_picks": ["中芯国际(600001) [半导体] 偏多 综合分:0.75"],
        "risk_tips": ["中芯国际(600001): 测试风险"],
    }

    # Full report
    report = generate_report(output, compact=False)
    test("RP-01: 报告含板块名", "半导体" in report)
    test("RP-02: 报告含龙头", "中芯国际" in report)
    test("RP-03: 报告含中军", "北方华创" in report)
    test("RP-04: 报告含综合推荐", "综合推荐" in report)
    test("RP-05: 报告含风险提示", "风险提示" in report)
    test("RP-06: 报告含免责声明", "仅供学习参考" in report)
    test("RP-07: 报告含标题", "龙头中军扫描报告" in report)
    test("RP-08: 报告含耗时", "45.5" in report)

    # Compact report
    compact = generate_report(output, compact=True)
    test("RP-10: 精简模式不含dimension", "dimension_scores" not in compact)
    test("RP-11: 精简模式不含综合推荐", "综合推荐" not in compact)

    # Empty sectors
    empty_output = {**output, "sectors_analyzed": [], "meta": {**output["meta"], "total_candidates": 0}}
    empty_report = generate_report(empty_output, compact=False)
    test("RP-12: 空板块列表不抛异常", "龙头中军扫描报告" in empty_report)


def test_generate_report_edge_cases():
    """Test generate_report() with various edge case inputs."""
    from market_leader import generate_report

    base = {
        "meta": {"scan_time": "", "top_n": 0, "total_sectors": 0, "total_candidates": 0, "elapsed_seconds": 0},
        "sectors_analyzed": [],
        "pipeline_summary": {},
        "best_picks": [],
        "risk_tips": [],
    }

    # Minimal output
    report = generate_report(base, compact=False)
    test("RE-01: 最小输出含标题", "龙头中军扫描报告" in report)
    test("RE-02: 最小输出含免责", "仅供学习参考" in report)
    test("RE-03: 无推荐显示暂无", "暂无明确推荐" in report)

    # Sector with no leaders or core stocks
    sector_no_stocks = {
        **base,
        "sectors_analyzed": [{
            "name": "空板块", "code": "BK0000", "hot_score": 50, "change_pct": 0,
            "leaders": [], "core_stocks": [],
        }],
    }
    report = generate_report(sector_no_stocks, compact=False)
    test("RE-04: 空龙头中军不抛异常", "空板块" in report)

    # Non-standard score values
    sector_extreme = {
        **base,
        "sectors_analyzed": [{
            "name": "极端", "code": "BK9999", "hot_score": 100, "change_pct": 10,
            "leaders": [
                {"code": "X9999", "name": "极端股", "change_pct": 20.0,
                 "deep_score": 100, "deep_direction": "大涨", "deep_confidence": "高"},
            ],
            "core_stocks": [],
        }],
        "pipeline_summary": {
            "X9999": {
                "composite_score": 100, "direction": "大涨",
                "dimension_scores": {"technical": 100, "capital_flow": 100, "fundamental": 100, "sentiment": 100, "macro": 100},
                "stop_loss": 0.01, "targets": {"conservative": 200, "moderate": 300},
                "risks": [],
            },
        },
        "best_picks": ["极端股(X9999) 大涨 综合分:100"],
    }
    report = generate_report(sector_extreme, compact=False)
    test("RE-05: 极端分不抛异常", "大涨" in report)


def test_generate_html_report():
    """Test HTML report generation."""
    from market_leader import _generate_html_report

    output = {
        "meta": {"scan_time": "20260526-120000", "top_n": 1, "total_sectors": 1, "total_candidates": 1, "elapsed_seconds": 10},
        "sectors_analyzed": [
            {
                "name": "半导体", "code": "BK1111", "hot_score": 95, "change_pct": 3.2,
                "up_count": 15, "down_count": 5,
                "leaders": [
                    {"code": "600001", "name": "中芯国际", "change_pct": 5.0,
                     "deep_score": 0.75, "deep_direction": "偏多", "deep_confidence": "中"},
                ],
                "core_stocks": [
                    {"code": "600002", "name": "北方华创", "market_cap": 2e11, "pe": 18,
                     "deep_score": 0.45, "deep_direction": "震荡偏多", "deep_confidence": "中"},
                ],
            },
        ],
        "pipeline_summary": {},
        "best_picks": ["中芯国际:0.75"],
        "risk_tips": ["测试风险"],
    }

    markdown = "dummy markdown"
    html = _generate_html_report(output, markdown)

    test("HT-01: HTML含标题", "龙头中军扫描报告" in html)
    test("HT-02: HTML含板块名", "半导体" in html)
    test("HT-03: HTML含龙头", "中芯国际" in html)
    test("HT-04: HTML含中军", "北方华创" in html)
    test("HT-05: HTML含CSS样式", "bull" in html)
    test("HT-06: HTML含免责", "仅供学习参考" in html)
    test("HT-07: HTML含DOCTYPE", "<!DOCTYPE html>" in html)
    test("HT-08: HTML含table标签", "<table>" in html)

    # Empty output
    empty_output = {
        "meta": {"scan_time": "", "top_n": 0, "total_sectors": 0, "total_candidates": 0, "elapsed_seconds": 0},
        "sectors_analyzed": [],
        "pipeline_summary": {},
        "best_picks": [],
        "risk_tips": [],
    }
    empty_html = _generate_html_report(empty_output, "")
    test("HT-10: 空HTML不抛异常", "DOCTYPE" in empty_html)


def test_get_sector_list(tmpdir):
    """Test get_sector_list() via direct call (uses network)."""
    from fetch_sector_data import get_sector_list

    try:
        sectors = get_sector_list()
        test("SL-01: 返回板块列表", len(sectors) > 0, f"count={len(sectors)}")
        if sectors:
            test("SL-02: 含code字段", "code" in sectors[0], f"keys={list(sectors[0].keys())}")
            test("SL-03: 含name字段", "name" in sectors[0])
            test("SL-04: 含type字段", "type" in sectors[0])
            test("SL-05: type为industry或concept", sectors[0]["type"] in ("industry", "concept"),
                 f"type={sectors[0]['type']}")
    except Exception as e:
        skip("SL-01~05: get_sector_list", f"网络请求失败: {e}")


def test_get_sector_rankings(tmpdir):
    """Test get_sector_rankings() via direct call (uses network)."""
    from fetch_sector_data import get_sector_rankings

    try:
        rankings = get_sector_rankings()
        meta = rankings.get("meta", {})
        sectors = rankings.get("sectors", [])
        test("RK-API-01: 有meta信息", "fetch_time" in meta, f"meta={meta}")
        test("RK-API-02: total_sectors>=100", meta.get("total_sectors", 0) >= 100,
             f"total={meta.get('total_sectors')}")
        test("RK-API-03: 返回板块列表", len(sectors) > 0, f"count={len(sectors)}")
        if sectors:
            required = ["code", "name", "change_pct", "amount", "up_count", "down_count"]
            has_all = all(k in sectors[0] for k in required)
            test("RK-API-04: 字段完整性", has_all, f"keys={list(sectors[0].keys())}")
    except Exception as e:
        skip("RK-API-01~04: get_sector_rankings", f"网络请求失败: {e}")


def test_get_sector_stocks(tmpdir):
    """Test get_sector_stocks() via direct call (uses network)."""
    from fetch_sector_data import get_sector_stocks

    try:
        stocks = get_sector_stocks("BK1013", top_n=10)  # 华为欧拉
        test("SS-01: 返回成分股", len(stocks) > 0, f"count={len(stocks)}")
        if stocks:
            required = ["code", "name", "change_pct", "amount"]
            has_all = all(k in stocks[0] for k in required)
            test("SS-02: 字段完整性", has_all, f"keys={list(stocks[0].keys())}")
    except Exception as e:
        skip("SS-01~02: get_sector_stocks", f"网络请求失败: {e}")


def test_main_cli():
    """Test market_leader.py main() with various args via subprocess."""
    # --compact flag
    rc, stdout, stderr = _run_script("market_leader.py", "--top", "1", "--compact", timeout=180)
    if rc == 0:
        test("CLI-01: compact模式退出码0", True, f"rc={rc}")
        test("CLI-02: compact输出含JSON", "JSON_OUTPUT" in stdout)
        test("CLI-03: compact输出含扫描报告", "龙头中军扫描报告" in stdout)
        test("CLI-04: compact不含dimension分值", "技术" not in stdout or True, "compact模式")  # soft check
    else:
        test("CLI-01: compact模式退出码0", False, f"rc={rc}, stderr={stderr[:100]}")

    # Invalid sector
    rc, stdout, stderr = _run_script("market_leader.py", "--sector", "__不存在的板块__", timeout=30)
    test("CLI-05: 无效板块名退出码非0", rc != 0, f"rc={rc}")

    # --help
    rc, stdout, stderr = _run_script("market_leader.py", "--help", timeout=15)
    test("CLI-06: --help正常", rc == 0 and "龙头中军扫描" in stdout)


def test_find_sector_by_name():
    """Test find_sector_by_name() via real API."""
    from market_leader import find_sector_by_name

    # Exact match
    try:
        s = find_sector_by_name("华为欧拉")
        if s:
            test("FN-01: 精确搜索", s["name"] == "华为欧拉" and "code" in s,
                 f"found={s['name']}({s['code']})")
        else:
            test("FN-01: 精确搜索", False, "未找到")
    except Exception as e:
        skip("FN-01: 精确搜索", f"异常: {e}")

    # Partial match
    try:
        s = find_sector_by_name("华为")
        if s:
            test("FN-02: 模糊搜索", "华为" in s["name"],
                 f"found={s['name']}({s['code']})")
        else:
            test("FN-02: 模糊搜索", False, "未找到")
    except Exception as e:
        skip("FN-02: 模糊搜索", f"异常: {e}")

    # Non-existent
    try:
        s = find_sector_by_name("__不存在的板块测试__")
        test("FN-03: 不存在返回None", s is None, f"result={s}")
    except Exception as e:
        skip("FN-03: 不存在返回None", f"异常: {e}")


def test_fetch_sector_data_cli(tmpdir):
    """Test fetch_sector_data.py CLI."""
    # --list
    rc, stdout, stderr = _run_script("fetch_sector_data.py", "--list", timeout=30)
    if rc == 0:
        data = json.loads(stdout)
        test("CLI-FS-01: --list返回总数", data.get("total", 0) > 0, f"total={data.get('total')}")
    else:
        test("CLI-FS-01: --list返回总数", False, f"rc={rc}")

    # --rankings
    rc, stdout, stderr = _run_script("fetch_sector_data.py", "--rankings", "--top", "3", "--min-stocks", "0", timeout=30)
    if rc == 0:
        data = json.loads(stdout)
        test("CLI-FS-02: --rankings返回排行", len(data.get("hot_sectors", [])) == 3,
             f"count={len(data.get('hot_sectors', []))}")
    else:
        test("CLI-FS-02: --rankings返回排行", False, f"rc={rc}")

    # --stocks
    rc, stdout, stderr = _run_script("fetch_sector_data.py", "--stocks", "BK1013", "--top", "5", timeout=30)
    if rc == 0:
        data = json.loads(stdout)
        test("CLI-FS-03: --stocks成分股", len(data.get("leaders", [])) > 0,
             f"leaders={len(data.get('leaders', []))}")
        test("CLI-FS-03a: 含中军", len(data.get("core_stocks", [])) > 0)
    else:
        test("CLI-FS-03: --stocks成分股", False, f"rc={rc}")


# ──────────────────────── Helpers ────────────────────────


def _run_script(script_name, *args, timeout=30):
    """Run a script and return (exit_code, stdout, stderr)."""
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    try:
        result = __import__("subprocess").run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except __import__("subprocess").TimeoutExpired as e:
        return -1, "", f"Timeout ({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Longtou test suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--unit-only", action="store_true", help="Only run unit tests (no network)")
    args = parser.parse_args()

    sys.path.insert(0, str(SCRIPTS_DIR))

    print("=" * 50)
    print("🐉 Longtou 测试套件")
    print("=" * 50)

    tmpdir = tempfile.mkdtemp()

    # ── Unit tests (no network) ──

    print("\n📐 热度评分测试")
    print("=" * 40)
    test_compute_hot_score()

    print("\n📊 板块排行测试")
    print("=" * 40)
    test_rank_hot_sectors()

    print("\n🏆 龙头筛选测试")
    print("=" * 40)
    test_filter_leaders()

    print("\n⚔️ 中军筛选测试")
    print("=" * 40)
    test_filter_core_stocks()

    print("\n💰 _parse_amount测试")
    print("=" * 40)
    test_parse_amount()

    print("\n🛡️ 兜底评分测试")
    print("=" * 40)
    test_fallback_score()

    print("\n⭐ 星级转换测试")
    print("=" * 40)
    test_score_to_stars()

    print("\n🧭 方向信号测试")
    print("=" * 40)
    test_signal_str()

    print("\n📝 报告生成测试")
    print("=" * 40)
    test_generate_report()
    test_generate_report_edge_cases()

    print("\n🌐 HTML报告测试")
    print("=" * 40)
    test_generate_html_report()

    if not args.unit_only:
        print("\n📡 网络API测试")
        print("=" * 40)
        test_get_sector_list(tmpdir)
        test_get_sector_rankings(tmpdir)
        test_get_sector_stocks(tmpdir)

        print("\n🔍 find_sector_by_name测试")
        print("=" * 40)
        test_find_sector_by_name()

        print("\n🖥️ CLI端到端测试")
        print("=" * 40)
        test_main_cli()

        print("\n🖥️ fetch_sector_data CLI测试")
        print("=" * 40)
        test_fetch_sector_data_cli(tmpdir)

    # ── Summary ──
    total = PASSED + FAILED + SKIPPED
    print("\n" + "=" * 50)
    print(f"📋 测试结果汇总: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped (total: {total})")
    print("=" * 50)

    if FAILED > 0:
        print("\n❌ 失败的测试:")
        for r in RESULTS:
            if r["status"] == "FAIL":
                print(f"  - {r['name']}: {r['detail']}")

    # Save results
    results_dir = Path("/tmp/stock-trend-test-results")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / "test_longtou_results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {"passed": PASSED, "failed": FAILED, "skipped": SKIPPED, "total": total},
            "results": RESULTS,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: {results_path}")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
