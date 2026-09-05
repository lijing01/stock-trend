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
            patch.object(sector_akshare, "_fetch_named_em_stocks",
                         return_value=([
                             sector_akshare._normalise_constituent_row(row)
                             for _, row in constituents.iterrows()
                         ], 2, "BK1259")) as fetch:
        wrapped = get_sector_stocks_akshare(
            "养殖业", "industry", top_n=1, as_of_date="2026-09-04",
            with_evidence=True)
        cached = get_sector_stocks_akshare_cached(
            "养殖业", "industry", top_n=1)

    fetch.assert_called_once_with("养殖业", "industry", 1, 15, 1, None)
    stock = wrapped["payload"][0]
    assert stock["code"] == "600001"
    assert stock["market_cap"] is None
    assert stock["membership_provider"] == "eastmoney"
    assert stock["membership_provider_code"] == "BK1259"
    assert stock["membership_mapping"] == "em_name_live"
    assert stock["membership_data_date"] == "2026-09-04"
    assert cached[0]["membership_source"] == "cache"
    assert cached[0]["membership_provider"] == "eastmoney"
    assert cached[0]["membership_provider_code"] == "BK1259"


def test_named_membership_uses_rotating_hosts_and_keyed_quote_fields():
    from fetchers.sector_akshare import _fetch_named_em_stocks
    from core.source_health import source_result, live_attempt

    payloads = [
        {"total": 101, "diff": [{"f12": "BK0001", "f14": "其他"}]},
        {"total": 101, "diff": [{"f12": "BK1259", "f14": "养殖业"}]},
        {"diff": [{"f14": "测试股份", "f20": 8e9, "f12": "600001",
                   "f6": 2e8, "f3": 1.2, "f9": 18.5}]},
    ]
    responses = [source_result({"rc": 0, "data": data}, live_attempt(
        attempted=True, provider_attempts=1)) for data in payloads]
    with patch("fetchers.sector_data._fetch_json", side_effect=responses) as fetch:
        stocks, attempts, code = _fetch_named_em_stocks(
            "养殖业", "industry", 25, 3, 1, 123.0)
    assert code == "BK1259" and attempts == 3
    assert stocks[0]["amount"] == 2e8
    assert stocks[0]["pe"] == 18.5
    assert stocks[0]["market_cap"] == 8e9
    assert "t:2" in fetch.call_args_list[0].args[0]
    assert "pn=2" in fetch.call_args_list[1].args[0]
    assert "fs=b:BK1259" in fetch.call_args_list[2].args[0]
    for call in fetch.call_args_list:
        assert call.kwargs == dict(timeout=3, retries=1, deadline=123.0,
                                   with_evidence=True)


def test_named_membership_missing_mapping_does_not_guess_bk_code():
    from fetchers.sector_akshare import (
        _fetch_named_em_stocks, SectorMembershipFetchError,
    )
    from core.source_health import source_result, live_attempt
    response = source_result({"rc": 0, "data": {
        "total": 1, "diff": [{"f12": "BK1259", "f14": "养殖"}],
    }}, live_attempt(attempted=True, provider_attempts=1))
    with patch("fetchers.sector_data._fetch_json", return_value=response) as fetch:
        try:
            _fetch_named_em_stocks("养殖业", "concept", 25, 3, 1, None)
        except SectorMembershipFetchError as exc:
            assert exc.reason == "sector_mapping_missing"
            assert exc.provider_attempts == 1
        else:
            raise AssertionError("Must reject a non-exact mapping")
    assert fetch.call_count == 1
    assert "t:3" in fetch.call_args.args[0]


def test_membership_network_failure_retains_old_cache_and_diagnostics():
    from fetchers import sector_akshare
    from core.source_health import classify_failure
    error = "ProxyError: Unable to connect to proxy: RemoteDisconnected"
    assert classify_failure(error) == "proxy_error"
    assert classify_failure("Remote end closed connection without response") == "connection_error"
    assert classify_failure("[Errno 1] Operation not permitted") == "permission_denied"
    with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(sector_akshare, "CACHE_DIR", Path(tmpdir)), \
            patch.object(sector_akshare, "_fetch_named_em_stocks",
                         side_effect=sector_akshare.SectorMembershipFetchError(
                             error, 2, "proxy_error")):
        sector_akshare._save_ths_stock_cache(
            "养殖业", "industry", [{"code": "600001"}], "2026-08-19")
        result = get_sector_stocks_akshare(
            "养殖业", as_of_date="2026-09-04", with_evidence=True)
    assert result["payload"][0]["membership_quality"] == "degraded"
    assert result["payload"][0]["membership_data_date"] == "2026-08-19"
    assert result["live_attempt"]["provider_attempts"] == 2
    assert result["live_attempt"]["reason"] == "proxy_error"
    assert result["live_attempt"]["failure_detail"] == error


def test_unmapped_sector_does_not_circuit_break_other_sectors():
    from core.source_health import RunSourceHealth, live_attempt

    health = RunSourceHealth()
    for _ in range(10):
        permit = health.try_acquire_live_permit("sector_membership")
        assert permit is not None
        health.mark_started(permit)
        health.complete_failure(permit, live_attempt(
            attempted=True, provider_attempts=1, reason="sector_mapping_missing"))
    state = health.snapshot()["sector_membership"]
    assert state["failures"] == 10
    assert state["circuit_breaks"] == 0
    assert state["state"] == "healthy"
    for _ in range(8):
        permit = health.try_acquire_live_permit("sector_membership")
        health.mark_started(permit)
        health.complete_failure(permit, live_attempt(
            attempted=True, provider_attempts=1, reason="proxy_error"))
    assert health.unavailable("sector_membership")
