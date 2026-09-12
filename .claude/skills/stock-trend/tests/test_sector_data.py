#!/usr/bin/env python3
"""Sector data and ranking test suite.

Tests for the retained sector_data helpers covering:
  - Sector ranking, scoring, filtering, and deduplication
  - DDX and capital-flow enrichment/degradation
  - Optional sector data API and CLI behavior

Usage:
    python3 test_sector_data.py              # Run all tests
    python3 test_sector_data.py -v            # Verbose output
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="sector_data"):
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
    from fetchers.sector_data import compute_hot_score

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
    from fetchers.sector_data import rank_hot_sectors

    # Normal: top 2
    top2 = rank_hot_sectors(MOCK_RANKINGS, top_n=2, min_stocks=8, min_up_ratio=0)
    test("RK-01: top_n=2返回2个", len(top2) == 2, f"count={len(top2)}")
    if len(top2) >= 2:
        test("RK-02: 热度降序排列", top2[0]["hot_score"] >= top2[1]["hot_score"],
             f"{top2[0]['hot_score']} >= {top2[1]['hot_score']}")

    # min_stocks filter: sector with 5 stocks should be excluded
    with_filter = rank_hot_sectors(MOCK_RANKINGS, top_n=10, min_stocks=8, min_up_ratio=0)
    test("RK-03: 微型板块(<8只)被过滤", all(s["name"] != "微型板块" for s in with_filter),
         f"sectors={[s['name'] for s in with_filter]}")

    # No filter
    no_filter = rank_hot_sectors(MOCK_RANKINGS, top_n=10, min_stocks=0, min_up_ratio=0)
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
    from fetchers.sector_data import filter_leaders

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
    from fetchers.sector_data import filter_core_stocks

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
    from fetchers.longhubang import _parse_amount

    test("PA-01: None值返回0", _parse_amount(None) == 0.0)
    test("PA-02: 数字字符串转换", _parse_amount("100") == 100.0)
    test("PA-03: 小数字符串转换", _parse_amount("3.14") == 3.14)
    test("PA-04: 万单位转换", _parse_amount("123.45万") == 1234500.0)
    test("PA-05: 零值", _parse_amount("0") == 0.0)
























def test_get_sector_list(tmpdir):
    """Test get_sector_list() via direct call (uses network)."""
    from fetchers.sector_data import get_sector_list

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
    from fetchers.sector_data import get_sector_rankings

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
    from fetchers.sector_data import get_sector_stocks

    try:
        stocks = get_sector_stocks("BK1013", top_n=10)  # 华为欧拉
        test("SS-01: 返回成分股", len(stocks) > 0, f"count={len(stocks)}")
        if stocks:
            required = ["code", "name", "change_pct", "amount"]
            has_all = all(k in stocks[0] for k in required)
            test("SS-02: 字段完整性", has_all, f"keys={list(stocks[0].keys())}")
    except Exception as e:
        skip("SS-01~02: get_sector_stocks", f"网络请求失败: {e}")






def test_fetch_sector_data_cli(tmpdir):
    """Test fetch_sector_data.py CLI."""
    # --list
    rc, stdout, stderr = _run_script("fetchers/sector_data.py", "--list", timeout=30)
    if rc == 0:
        data = json.loads(stdout)
        test("CLI-FS-01: --list返回总数", data.get("total", 0) > 0, f"total={data.get('total')}")
    else:
        test("CLI-FS-01: --list返回总数", False, f"rc={rc}")

    # --rankings
    rc, stdout, stderr = _run_script("fetchers/sector_data.py", "--rankings", "--top", "3", "--min-stocks", "0", timeout=30)
    if rc == 0:
        data = json.loads(stdout)
        test("CLI-FS-02: --rankings返回排行", len(data.get("hot_sectors", [])) == 3,
             f"count={len(data.get('hot_sectors', []))}")
    else:
        test("CLI-FS-02: --rankings返回排行", False, f"rc={rc}")

    # --stocks
    rc, stdout, stderr = _run_script("fetchers/sector_data.py", "--stocks", "BK1013", "--top", "5", timeout=30)
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


# ──────────────────────── DDX Score Tests ────────────────────────


def test_ddx_score_computation():
    """Test compute_ddx_score() and compute_super_order_score()."""
    from fetchers.ddx import compute_ddx_score, compute_super_order_score

    test("DDX-01: ddx>=0.5 + ddx_days>=3 -> 100",
         compute_ddx_score({"ddx": 0.6, "ddx_days": 5}) == 100)
    test("DDX-02: ddx>=0.5 alone -> 90",
         compute_ddx_score({"ddx": 0.5, "ddx_days": 1}) == 90)
    test("DDX-03: ddx=0.2 -> 80",
         compute_ddx_score({"ddx": 0.2, "ddx_days": 0}) == 80)
    score = compute_ddx_score({"ddx": 0.1, "ddx_days": 0})
    test("DDX-04: ddx=0.1 interpolated 50-80",
         50 < score < 80, f"score={score}")
    test("DDX-05: ddx=0 -> 50",
         compute_ddx_score({"ddx": 0, "ddx_days": 0}) == 50)
    test("DDX-06: ddx=-0.3 -> max(0,20)=20",
         compute_ddx_score({"ddx": -0.3, "ddx_days": 0}) == 20)
    test("DDX-07: ddx=-0.6 -> clamped to 0",
         compute_ddx_score({"ddx": -0.6, "ddx_days": 0}) == 0)
    test("DDX-08: empty dict -> 50",
         compute_ddx_score({}) == 50)

    test("DSO-01: ratio>=15% -> 100",
         compute_super_order_score({"super_order_ratio": 0.15}) == 100)
    test("DSO-02: ratio>=8% -> 80",
         compute_super_order_score({"super_order_ratio": 0.08}) == 80)
    test("DSO-03: ratio=6% -> 60",
         compute_super_order_score({"super_order_ratio": 0.06}) == 60)
    test("DSO-04: ratio=3% -> 50",
         compute_super_order_score({"super_order_ratio": 0.03}) == 50)
    test("DSO-05: ratio=25% -> 100",
         compute_super_order_score({"super_order_ratio": 0.25}) == 100)
    test("DSO-06: empty -> 50",
         compute_super_order_score({}) == 50)


def test_rescore_leaders_with_ddx():
    """Test rescore_leaders_with_ddx() DDX-enhanced leader scoring."""
    from fetchers.sector_data import rescore_leaders_with_ddx

    stocks = [
        {"code": "600001", "name": "高DDX龙头", "change_pct": 9.5, "amount": 5e8},
        {"code": "600002", "name": "低DDX龙头", "change_pct": 7.2, "amount": 3e8},
        {"code": "600003", "name": "负DDX跟风", "change_pct": 6.0, "amount": 2e8},
    ]
    ddx_data = {
        "600001": {"ddx": 0.8, "ddx_days": 5, "super_order_ratio": 0.18},
        "600002": {"ddx": 0.1, "ddx_days": 1, "super_order_ratio": 0.04},
        "600003": {"ddx": -0.4, "ddx_days": 0, "super_order_ratio": 0.02},
    }

    rescored = rescore_leaders_with_ddx(stocks, ddx_data)
    test("RS-01: 高DDX股排首位", rescored[0]["code"] == "600001",
         f"top={rescored[0]['name']} score={rescored[0]['leader_score']}")
    test("RS-02: 负DDX排最后", rescored[-1]["code"] == "600003",
         f"last={rescored[-1]['name']} score={rescored[-1]['leader_score']}")

    no_ddx = rescore_leaders_with_ddx(stocks, {})
    test("RS-03: 无DDX数据保持排序", no_ddx[0]["code"] == "600001")

    empty = rescore_leaders_with_ddx([], {"600001": {}})
    test("RS-04: 空列表不抛异常", len(empty) == 0)

    partial = rescore_leaders_with_ddx(stocks[:2], {"600001": ddx_data["600001"]})
    test("RS-05: 部分DDX覆盖正常工作", len(partial) == 2, f"count={len(partial)}")


def test_longhubang_risk_analysis():
    """Test 龙虎榜 risk level classification."""
    from fetchers.longhubang import _classify_risk_level

    inst_buy = {
        "is_on_board": True, "has_institution_buy": True,
        "has_institution_sell": False, "retail_dominated": False,
        "has_floating_capital": False,
    }
    test("LHB-01: 机构净买入->low",
         _classify_risk_level(inst_buy) == "low",
         f"risk={_classify_risk_level(inst_buy)}")

    retail = {
        "is_on_board": True, "has_institution_buy": False,
        "has_institution_sell": False, "retail_dominated": True,
        "has_floating_capital": False,
    }
    test("LHB-02: 散户主导->high",
         _classify_risk_level(retail) == "high")

    mixed = {
        "is_on_board": True, "has_institution_buy": True,
        "has_institution_sell": True, "retail_dominated": False,
        "has_floating_capital": True, "floating_capital_net_buy": False,
    }
    test("LHB-03: 机构+游资分歧->medium",
         _classify_risk_level(mixed) == "medium")

    youzi = {
        "is_on_board": True, "has_institution_buy": False,
        "has_institution_sell": False, "retail_dominated": False,
        "has_floating_capital": True, "floating_capital_net_buy": True,
    }
    test("LHB-04: 纯游资->medium",
         _classify_risk_level(youzi) == "medium")

    test("LHB-05: 未上榜->low",
         _classify_risk_level({"is_on_board": False}) == "low")
    test("LHB-06: 空数据->low",
         _classify_risk_level({}) == "low")


def test_ddx_degradation():
    """Test graceful degradation when DDX fetch fails."""
    from fetchers.ddx import fetch_ddx_data, compute_ddx_score, compute_super_order_score

    empty = fetch_ddx_data([])
    test("DG-01: DDX空列表返回空", len(empty) == 0)

    test("DG-02: DDX空数据分=50", compute_ddx_score({}) == 50)
    test("DG-03: 超级资金空数据分=50", compute_super_order_score({}) == 50)


def test_longhubang_degradation():
    """Test graceful degradation when 龙虎榜 fetch fails."""
    from fetchers.longhubang import fetch_longhubang_data, _classify_risk_level

    empty = fetch_longhubang_data([])
    test("LHG-01: 龙虎榜空列表返回空", len(empty) == 0)

    test("LHG-02: 龙虎榜空数据风险=low", _classify_risk_level({}) == "low")


def test_sector_dedup():
    """Sectors with identical constituent stats should be deduplicated."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from fetchers.sector_data import rank_hot_sectors

    rankings = {
        "meta": {"total_sectors": 4},
        "sectors": [
            {"code": "BK0001", "name": "工程咨询服务Ⅱ", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            {"code": "BK0002", "name": "工程咨询服务Ⅲ", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            {"code": "BK0003", "name": "半导体", "type": "industry",
             "change_pct": 2.0, "up_count": 30, "down_count": 10,
             "main_force_net": 5e8},
            {"code": "BK0004", "name": "新能源", "type": "industry",
             "change_pct": 1.5, "up_count": 20, "down_count": 15,
             "main_force_net": 3e8},
        ],
    }
    result = rank_hot_sectors(rankings, top_n=10, min_stocks=0, min_up_ratio=0)
    eng_sectors = [s for s in result if "工程咨询" in s["name"]]
    test("sector_dedup_removes_duplicates", len(eng_sectors) <= 1,
         f"Expected ≤1 工程咨询 sector, got {len(eng_sectors)}")


def test_sector_up_ratio_filter():
    """Sectors with <15% up ratio should be excluded."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from fetchers.sector_data import rank_hot_sectors

    rankings = {
        "meta": {"total_sectors": 3},
        "sectors": [
            {"code": "BK0010", "name": "公交", "type": "concept",
             "change_pct": -3.6, "up_count": 0, "down_count": 8,
             "main_force_net": -0.5e8},
            {"code": "BK0011", "name": "弱势板块", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            {"code": "BK0012", "name": "强势板块", "type": "industry",
             "change_pct": 2.0, "up_count": 20, "down_count": 10,
             "main_force_net": 3e8},
        ],
    }
    result = rank_hot_sectors(rankings, top_n=10, min_stocks=0, min_up_ratio=0.15)
    names = [s["name"] for s in result]
    test("up_ratio_filter_excludes_weak",
         "公交" not in names and "弱势板块" not in names,
         f"Got: {names}")
    test("up_ratio_filter_keeps_strong",
         "强势板块" in names,
         f"Got: {names}")


def main():
    parser = argparse.ArgumentParser(description="Sector data test suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--unit-only", action="store_true", help="Only run unit tests (no network)")
    args = parser.parse_args()

    sys.path.insert(0, str(SCRIPTS_DIR))

    print("=" * 50)
    print("行业数据组件测试套件")
    print("=" * 50)

    tmpdir = tempfile.mkdtemp()

    print("\n📐 板块数据单元测试")
    print("=" * 40)
    test_compute_hot_score()
    test_rank_hot_sectors()
    test_filter_leaders()
    test_filter_core_stocks()
    test_parse_amount()
    test_ddx_score_computation()
    test_rescore_leaders_with_ddx()
    test_longhubang_risk_analysis()
    test_ddx_degradation()
    test_longhubang_degradation()
    test_sector_dedup()
    test_sector_up_ratio_filter()

    if not args.unit_only:
        print("\n📡 板块数据源测试")
        print("=" * 40)
        test_get_sector_list(tmpdir)
        test_get_sector_rankings(tmpdir)
        test_get_sector_stocks(tmpdir)
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
    results_path = results_dir / "test_sector_data_results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {"passed": PASSED, "failed": FAILED, "skipped": SKIPPED, "total": total},
            "results": RESULTS,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: {results_path}")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
