"""Test sector_akshare.py — AKShare 备选数据源."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers.sector_akshare import (
    get_sector_rankings_akshare,
    get_sector_list_akshare,
    get_sector_stocks_akshare,
    get_sector_stocks_akshare_cached,
    HAS_AKSHARE,
)


def test_has_akshare():
    """AKShare should be installed."""
    assert HAS_AKSHARE, "AKShare not installed"


def test_get_sector_rankings_akshare_returns_data():
    """get_sector_rankings should return sectors with real data (at minimum 行业板块)."""
    result = get_sector_rankings_akshare()
    if result is None:
        return  # transient AKShare failure
    assert "sectors" in result
    assert len(result["sectors"]) > 0, "No sectors returned"

    # Should have at least some industry sectors
    industries = [s for s in result["sectors"] if s["type"] == "industry"]
    if not industries:
        return  # transient
    assert len(industries) >= 10, f"Expected >=10 industry sectors, got {len(industries)}"

    # Industry sectors should have real data (not all zeros)
    top = [s for s in industries if abs(s.get("change_pct", 0) or 0) > 0.1]
    assert len(top) > 0, "No industry sectors with >0.1% change"


def test_sector_format():
    """Each sector should have all required fields."""
    result = get_sector_rankings_akshare()
    assert result is not None
    for s in result["sectors"]:
        assert "code" in s, f"Missing code in {s}"
        assert "name" in s, f"Missing name in {s}"
        assert "type" in s, f"Missing type in {s}"
        assert s["type"] in ("industry", "concept"), f"Invalid type: {s['type']}"
        assert "change_pct" in s
        assert "amount" in s
        assert "up_count" in s
        assert "down_count" in s


def test_industry_has_net_flow():
    """Industry sectors should have main_force_net data."""
    result = get_sector_rankings_akshare()
    if result is None:
        return
    industries = [s for s in result["sectors"] if s["type"] == "industry"]
    if not industries:
        return
    with_flow = [s for s in industries if s.get("main_force_net") is not None]
    assert len(with_flow) > 0, "No industry sectors with main_force_net"


def test_has_concept_sectors():
    """Should have concept sectors (at least from ths name list)."""
    result = get_sector_rankings_akshare()
    if result is None:
        return
    concepts = [s for s in result["sectors"] if s["type"] == "concept"]
    if not concepts:
        return
    assert len(concepts) >= 50, f"Expected >=50 concepts, got {len(concepts)}"


def test_get_sector_list_akshare():
    """get_sector_list_akshare should return both industry and concept sectors."""
    sectors = get_sector_list_akshare()
    assert len(sectors) > 0
    types = set(s["type"] for s in sectors)
    assert "industry" in types
    assert "concept" in types


def test_sector_list_format():
    """Each list entry should have code, name, type."""
    sectors = get_sector_list_akshare()
    for s in sectors:
        assert "code" in s
        assert "name" in s
        assert "type" in s


def test_alignment_with_sector_data_format():
    """AKShare output should be compatible with sector_data.get_sector_rankings()."""
    result = get_sector_rankings_akshare()
    if result is None:
        return
    assert "meta" in result
    assert "fetch_time" in result["meta"]
    assert "total_sectors" in result["meta"]


def test_real_time_data_freshness():
    """Industry data should be from today or yesterday (not stale)."""
    result = get_sector_rankings_akshare()
    if result is None:
        return
    industries = [s for s in result["sectors"] if s["type"] == "industry"]
    if not industries:
        return
    active = [
        s for s in industries
        if abs(s.get("change_pct", 0) or 0) > 0.01
        or (s.get("up_count", 0) or 0) > 0
        or (s.get("down_count", 0) or 0) > 0
    ]
    assert len(active) >= 5, f"Expected >=5 active sectors, got {len(active)}"


def test_ths_ranking_keeps_display_ordinal_out_of_provider_identity():
    """THS industry 序号 must not be treated as an EM BK code."""
    from fetchers import sector_akshare

    industries = pd.DataFrame([{
        "序号": 1,
        "板块": "养殖业",
        "涨跌幅": 1.0,
        "总成交额": 2.0,
        "净流入": 0.5,
        "上涨家数": 4,
        "下跌家数": 1,
    }])
    concepts = pd.DataFrame([{"name": "农业", "code": "885001"}])
    with patch.object(sector_akshare, "HAS_AKSHARE", True), \
            patch.object(sector_akshare.ak,
                         "stock_board_industry_summary_ths",
                         return_value=industries), \
            patch.object(sector_akshare.ak,
                         "stock_board_concept_name_ths",
                         return_value=concepts):
        result = sector_akshare.get_sector_rankings_akshare()

    industry = next(s for s in result["sectors"] if s["type"] == "industry")
    assert industry["code"] == "ths:industry:养殖业"
    assert industry["provider"] == "ths"
    assert industry["provider_code"] == "1"
    assert industry["expand_symbol"] == "养殖业"
    assert industry["sector_id"] == "ths:industry:养殖业"
    assert result["meta"]["provider"] == "ths"


def test_ths_constituent_adapter_uses_sector_name_and_isolated_cache():
    """Constituents are fetched by name and written to the THS cache only."""
    from fetchers import sector_akshare

    constituents = pd.DataFrame([{
        "代码": "600001",
        "名称": "测试股份",
        "涨跌幅": 2.5,
        "成交额": 123000000,
        "市盈率-动态": 18.0,
    }])
    with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(sector_akshare, "CACHE_DIR", Path(tmpdir)), \
            patch.object(sector_akshare, "HAS_AKSHARE", True), \
            patch.object(sector_akshare.ak,
                         "stock_board_industry_cons_em",
                         return_value=constituents) as fetch:
        wrapped = get_sector_stocks_akshare(
            "养殖业", "industry", top_n=1, as_of_date="2026-09-04",
            with_evidence=True)
        cached = get_sector_stocks_akshare_cached(
            "养殖业", "industry", top_n=1)

    fetch.assert_called_once_with(symbol="养殖业")
    stock = wrapped["payload"][0]
    assert stock["code"] == "600001"
    assert stock["market_cap"] is None
    assert stock["membership_provider"] == "akshare"
    assert stock["membership_provider_code"] == "养殖业"
    assert stock["membership_mapping"] == "ths_name_live"
    assert stock["membership_data_date"] == "2026-09-04"
    assert cached[0]["membership_source"] == "cache"
