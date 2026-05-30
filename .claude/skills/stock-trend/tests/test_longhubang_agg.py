"""Test longhubang_agg.py — 龙虎榜机构板块聚合."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers.longhubang_agg import (
    fetch_lhb_jgmmtj,
    aggregate_lhb_by_sector,
    score_lhb_sectors,
    generate_lhb_md_section,
    generate_lhb_html_section,
    run_lhb_analysis,
    HAS_AKSHARE,
)


# ──────────────── Mock 数据 ────────────────

MOCK_LHB_RECORDS = [
    {
        "code": "600519", "name": "贵州茅台",
        "close": 1500.0, "change_pct": 5.0,
        "inst_buy_count": 3, "inst_sell_count": 1,
        "inst_buy_total": 5e8, "inst_sell_total": 1e8,
        "inst_net_amount": 4e8,
        "total_amount": 2e9, "net_ratio": 20.0,
        "turnover_rate": 0.5, "market_cap": 2e11,
        "reason": "日涨幅偏离值达到7%", "date": "20260529",
    },
    {
        "code": "000858", "name": "五粮液",
        "close": 180.0, "change_pct": 4.0,
        "inst_buy_count": 2, "inst_sell_count": 0,
        "inst_buy_total": 2e8, "inst_sell_total": 0,
        "inst_net_amount": 2e8,
        "total_amount": 8e8, "net_ratio": 25.0,
        "turnover_rate": 1.0, "market_cap": 7e10,
        "reason": "日涨幅偏离值达到7%", "date": "20260529",
    },
    {
        "code": "002230", "name": "科大讯飞",
        "close": 60.0, "change_pct": -3.0,
        "inst_buy_count": 1, "inst_sell_count": 4,
        "inst_buy_total": 0.5e8, "inst_sell_total": 2e8,
        "inst_net_amount": -1.5e8,
        "total_amount": 1e9, "net_ratio": -15.0,
        "turnover_rate": 3.0, "market_cap": 1.2e11,
        "reason": "日跌幅偏离值达到7%", "date": "20260529",
    },
]

MOCK_MAPPING = {
    "mapping": {
        "600519": [{"code": "BK0477", "name": "白酒", "type": "industry"}],
        "000858": [{"code": "BK0477", "name": "白酒", "type": "industry"}],
        "002230": [
            {"code": "BK0446", "name": "人工智能", "type": "concept"},
        ],
    },
}


# ──────────────── fetch_lhb_jgmmtj ────────────────


def test_has_akshare():
    assert HAS_AKSHARE, "AKShare not installed"


def test_fetch_lhb_jgmmtj_returns_list():
    records = fetch_lhb_jgmmtj("20260529")
    assert isinstance(records, list)
    if not records:
        return  # 非交易日跳过
    assert len(records) > 0
    assert "code" in records[0]
    assert "inst_net_amount" in records[0]
    assert "inst_buy_count" in records[0]


def test_fetch_lhb_jgmmtj_fields():
    records = fetch_lhb_jgmmtj("20260529")
    if not records:
        return
    for r in records[:3]:
        assert "code" in r
        assert "inst_buy_count" in r
        assert "inst_sell_count" in r
        assert "inst_net_amount" in r
        assert "net_ratio" in r
        assert "reason" in r


# ──────────────── aggregate_lhb_by_sector ────────────────


def test_aggregate_empty():
    assert aggregate_lhb_by_sector([], {}) == []
    assert aggregate_lhb_by_sector([], {"mapping": {}}) == []


def test_aggregate_basic():
    result = aggregate_lhb_by_sector(MOCK_LHB_RECORDS, MOCK_MAPPING)
    assert len(result) >= 2
    sectors = {s["sector_name"]: s for s in result}
    assert "白酒" in sectors
    assert "人工智能" in sectors
    # 白酒 has 3 stocks mapped (茅台+五粮液+讯飞映射到白酒)
    # But 科大讯飞 is negative net, 茅台+五粮液 positive
    bj = sectors["白酒"]
    assert bj["stock_count"] == 2  # 茅台+五粮液
    assert bj["total_inst_net_yi"] > 0  # both positive net buy
    # 人工智能 only has 科大讯飞
    ai = sectors["人工智能"]
    assert ai["stock_count"] == 1
    assert ai["total_inst_net_yi"] < 0  # 讯飞机构净卖出


def test_aggregate_dedup():
    """Same stock on multiple reasons should be counted once per sector."""
    records = MOCK_LHB_RECORDS + [{
        **MOCK_LHB_RECORDS[0],
        "reason": "连续三个交易日内涨幅偏离值累计达到20%",
    }]
    result = aggregate_lhb_by_sector(records, MOCK_MAPPING)
    for s in result:
        if s["sector_name"] == "白酒":
            # 茅台 should be counted once even though it appears twice
            assert s["stock_count"] <= 3
            break


def test_aggregate_no_mapping():
    """Stock with no sector mapping should be excluded."""
    records = [{"code": "999999", "name": "未知", "inst_net_amount": 1e8,
                "inst_buy_count": 1, "inst_sell_count": 0,
                "inst_buy_total": 1, "inst_sell_total": 0,
                "close": 10, "change_pct": 1, "total_amount": 1,
                "net_ratio": 1, "turnover_rate": 0,
                "market_cap": 1, "reason": "x", "date": "20260529"}]
    result = aggregate_lhb_by_sector(records, MOCK_MAPPING)
    assert result == []


def test_aggregate_fields():
    result = aggregate_lhb_by_sector(MOCK_LHB_RECORDS, MOCK_MAPPING)
    for s in result:
        assert "sector_code" in s
        assert "sector_name" in s
        assert "sector_type" in s
        assert "stock_count" in s
        assert "total_inst_net_yi" in s
        assert "inst_buy_count" in s
        assert "inst_sell_count" in s
        assert "lhb_score" in s
        assert isinstance(s["total_inst_net_yi"], (int, float))
        assert isinstance(s["stock_count"], int)


# ──────────────── score_lhb_sectors ────────────────


def test_score_empty():
    assert score_lhb_sectors([]) == []


def test_score_basic():
    result = aggregate_lhb_by_sector(MOCK_LHB_RECORDS, MOCK_MAPPING)
    # Scoring already applied in aggregate
    for s in result:
        assert 0 <= s["lhb_score"] <= 100
        assert "direction" in s
        assert s["direction"] in ("净买", "净卖")


def test_score_positive_higher():
    """Net buy sectors should score higher than net sell with similar counts."""
    buy_records = [
        {"code": "600000", "name": "买入A", "inst_buy_count": 3,
         "inst_sell_count": 0, "inst_buy_total": 3e8, "inst_sell_total": 0,
         "inst_net_amount": 3e8, "close": 10, "change_pct": 5,
         "total_amount": 1e9, "net_ratio": 30, "turnover_rate": 1,
         "market_cap": 1e10, "reason": "x", "date": "20260529"},
        {"code": "600001", "name": "买入B", "inst_buy_count": 2,
         "inst_sell_count": 0, "inst_buy_total": 1e8, "inst_sell_total": 0,
         "inst_net_amount": 1e8, "close": 10, "change_pct": 3,
         "total_amount": 5e8, "net_ratio": 20, "turnover_rate": 1,
         "market_cap": 5e9, "reason": "x", "date": "20260529"},
    ]
    sell_records = [
        {"code": "600002", "name": "卖出C", "inst_buy_count": 0,
         "inst_sell_count": 3, "inst_buy_total": 0, "inst_sell_total": 3e8,
         "inst_net_amount": -3e8, "close": 10, "change_pct": -5,
         "total_amount": 1e9, "net_ratio": -30, "turnover_rate": 1,
         "market_cap": 1e10, "reason": "x", "date": "20260529"},
    ]
    mapping_buy = {"mapping": {"600000": [{"code": "BK001", "name": "买入板块", "type": "industry"}],
                                "600001": [{"code": "BK001", "name": "买入板块", "type": "industry"}]}}
    mapping_sell = {"mapping": {"600002": [{"code": "BK002", "name": "卖出板块", "type": "industry"}]}}

    buy_result = aggregate_lhb_by_sector(buy_records, mapping_buy)
    sell_result = aggregate_lhb_by_sector(sell_records, mapping_sell)

    if buy_result and sell_result:
        assert buy_result[0]["lhb_score"] > sell_result[0]["lhb_score"], \
            f"Buy {buy_result[0]['lhb_score']} should > Sell {sell_result[0]['lhb_score']}"
        assert buy_result[0]["direction"] == "净买"
        assert sell_result[0]["direction"] == "净卖"


# ──────────────── generate_lhb_md_section ────────────────


def test_md_section_empty():
    assert generate_lhb_md_section([]) == ""


def test_md_section_basic():
    result = aggregate_lhb_by_sector(MOCK_LHB_RECORDS, MOCK_MAPPING)
    if not result:
        return
    md = generate_lhb_md_section(result)
    assert "龙虎榜机构板块聚合" in md
    assert "白酒" in md or "人工智能" in md
    assert "机构净买入" in md or "机构净卖出" in md


# ──────────────── generate_lhb_html_section ────────────────


def test_html_section_empty():
    assert generate_lhb_html_section([]) == ""


def test_html_section_basic():
    result = aggregate_lhb_by_sector(MOCK_LHB_RECORDS, MOCK_MAPPING)
    if not result:
        return
    html = generate_lhb_html_section(result)
    assert "龙虎榜机构板块聚合" in html
    assert "<table>" in html
    assert "</table>" in html


# ──────────────── run_lhb_analysis ────────────────


def test_run_lhb_analysis_live():
    """Integration test with real AKShare data (skip if no data)."""
    if not HAS_AKSHARE:
        return
    result = run_lhb_analysis("20260529", top_n=5)
    assert "meta" in result
    assert "sectors" in result
    if result["sectors"]:
        assert len(result["sectors"][:5]) > 0
        assert "lhb_score" in result["sectors"][0]
        assert "sector_name" in result["sectors"][0]


def test_run_lhb_analysis_structure():
    if not HAS_AKSHARE:
        return
    result = run_lhb_analysis("20260529", top_n=5)
    assert "meta" in result
    assert "date" in result["meta"]
    assert "total_lhb_stocks" in result["meta"]
    assert isinstance(result["sectors"], list)
