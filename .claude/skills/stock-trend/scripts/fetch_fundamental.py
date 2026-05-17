#!/usr/bin/env python3
"""Fundamental data fetcher for stock-trend skill.

Fetches PE/PB valuation, financial indicators, revenue/growth data
using AKShare. Supports A-shares and HK stocks. ETFs are skipped.

Usage:
    python3 fetch_fundamental.py <ts_code> [--asset E|FD] [-o output.json]

Examples:
    python3 fetch_fundamental.py 600519.SH
    python3 fetch_fundamental.py 00700.HK -o /tmp/fundamental.json
"""

import argparse
import json
import os
import sys
import time
import logging
from cache_utils import load_cache, safe_float, save_cache, retry, get_market_day_ttl
from datetime import datetime

logging.getLogger("akshare").setLevel(logging.ERROR)

# All fundamental values need 2-decimal rounding
_sf = lambda v: safe_float(v, round_to=2)


def fetch_a_share_fundamentals(code):
    """Fetch fundamental data for A-share stocks using AKShare with retries."""
    import akshare as ak
    result = {"data_quality": "error"}
    errors = []

    # 1. Basic info: PE, PB, market cap, industry (with retry)
    info = None
    df_info, err = retry(lambda: ak.stock_individual_info_em(symbol=code), max_attempts=2, delay=3)
    if err:
        errors.append(f"stock_individual_info_em: {err}")
    elif df_info is not None and not df_info.empty:
        info = {}
        for _, row in df_info.iterrows():
            info[str(row.iloc[0])] = row.iloc[1]
    else:
        errors.append("stock_individual_info_em: empty result")

    if info:
        result["pe_ttm"] = _sf(info.get("市盈率-动态"))
        result["pb"] = _sf(info.get("市净率"))
        mc = _sf(info.get("总市值"))
        if mc is not None:
            result["market_cap_billion"] = round(mc / 1e8, 2)
        result["industry"] = info.get("行业")

    # 2. Financial analysis indicators (ROE, EPS, debt ratio)
    start_yr = str(datetime.now().year - 1)
    df_fin, err = retry(lambda: ak.stock_financial_analysis_indicator(symbol=code, start_year=start_yr),
                         max_attempts=2, delay=2)
    if err:
        errors.append(f"stock_financial_analysis_indicator: {err}")
    elif df_fin is not None and not df_fin.empty:
        latest = df_fin.iloc[-1]
        if result.get("roe") is None:
            result["roe"] = _sf(latest.get("净资产收益率"))
        if result.get("eps") is None:
            result["eps"] = _sf(latest.get("每股收益"))
        if result.get("debt_ratio") is None:
            result["debt_ratio"] = _sf(latest.get("资产负债率"))

    # 3. Revenue/profit growth from earnings report (confirmed working)
    today = datetime.now()
    quarter = f"{today.year}0331" if today.month < 5 else \
              f"{today.year}0630" if today.month < 9 else \
              f"{today.year}0930" if today.month < 11 else \
              f"{today.year}1231"
    df_yjbb, err = retry(lambda: ak.stock_yjbb_em(date=quarter), max_attempts=2, delay=2)
    if err:
        errors.append(f"stock_yjbb_em: {err}")
    elif df_yjbb is not None and not df_yjbb.empty:
        match = df_yjbb[df_yjbb["股票代码"] == code]
        if match.empty:
            match = df_yjbb[df_yjbb["股票代码"] == code.lstrip("0")]
        if not match.empty:
            row = match.iloc[0]
            result["revenue_growth_pct"] = _sf(row.get("营业收入同比增长率"))
            result["profit_growth_pct"] = _sf(row.get("净利润同比增长率"))

    # 4. PE/PB percentile (3-year) - try valuation API (may fail, non-critical)
    df_val_pe, err_pe = retry(
        lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="市盈率-动态", period="近三年"),
        max_attempts=1, delay=2
    )
    if err_pe:
        errors.append(f"stock_zh_valuation_baidu(pe): {err_pe}")
    elif df_val_pe is not None and not df_val_pe.empty:
        try:
            # Find the numeric value column (skip date column)
            val_col = df_val_pe.select_dtypes(include=["float64", "int64"]).columns
            if len(val_col) > 0:
                pe_values = df_val_pe[val_col[0]].dropna().astype(float)
                if not pe_values.empty and result.get("pe_ttm"):
                    below = (pe_values < result["pe_ttm"]).sum()
                    result["pe_percentile_3y"] = round(float(below) / len(pe_values) * 100, 1)
        except Exception as e:
            errors.append(f"pe_percentile_calc: {e}")

    df_val_pb, err_pb = retry(
        lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator="市净率", period="近三年"),
        max_attempts=1, delay=2
    )
    if err_pb:
        errors.append(f"stock_zh_valuation_baidu(pb): {err_pb}")
    elif df_val_pb is not None and not df_val_pb.empty:
        try:
            val_col = df_val_pb.select_dtypes(include=["float64", "int64"]).columns
            if len(val_col) > 0:
                pb_values = df_val_pb[val_col[0]].dropna().astype(float)
                if not pb_values.empty and result.get("pb"):
                    below = (pb_values < result["pb"]).sum()
                    result["pb_percentile_3y"] = round(float(below) / len(pb_values) * 100, 1)
        except Exception as e:
            errors.append(f"pb_percentile_calc: {e}")

    # 5. Estimate dividend yield from PE
    if result.get("pe_ttm") and result["pe_ttm"] > 0:
        est_div_yield = (0.30 / result["pe_ttm"]) * 100
        result["dividend_yield_pct"] = round(est_div_yield, 2)

    # Determine data quality
    filled = sum(1 for k in ["pe_ttm", "pb", "roe", "eps", "revenue_growth_pct"] if result.get(k) is not None)
    if filled >= 3:
        result["data_quality"] = "good"
    elif filled >= 1:
        result["data_quality"] = "partial"
    else:
        result["data_quality"] = "error"

    if errors:
        result["_errors"] = errors

    return result


def fetch_hk_fundamentals(code):
    """Fetch fundamental data for HK stocks using AKShare."""
    import akshare as ak
    result = {"data_quality": "error"}
    errors = []

    # HK financial indicators
    try:
        # AKShare uses format like "00700" for HK
        hk_code = code
        df_hk = ak.stock_hk_financial_indicator_em(symbol=hk_code)
        if df_hk is not None and not df_hk.empty:
            latest = df_hk.iloc[-1]
            result["pe_ttm"] = _sf(latest.get("市盈率"))
            result["pb"] = _sf(latest.get("市净率"))
            result["roe"] = _sf(latest.get("净资产收益率"))
            result["eps"] = _sf(latest.get("每股收益"))
            result["market_cap_billion"] = _sf(latest.get("总市值"))
            mc = result["market_cap_billion"]
            if mc is not None and mc > 1e6:
                result["market_cap_billion"] = round(mc / 1e8, 2)
    except Exception as e:
        errors.append(f"stock_hk_financial_indicator_em: {e}")

    # HK valuation percentile
    try:
        import akshare as ak
        df_hk_val = ak.stock_hk_valuation_baidu(symbol=hk_code, indicator="市盈率", period="近三年")
        if df_hk_val is not None and not df_hk_val.empty:
            pe_values = df_hk_val.iloc[:, 0].dropna().astype(float)
            if not pe_values.empty and result.get("pe_ttm"):
                below = (pe_values < result["pe_ttm"]).sum()
                result["pe_percentile_3y"] = round(float(below) / len(pe_values) * 100, 1)
    except Exception as e:
        errors.append(f"stock_hk_valuation_baidu(pe): {e}")

    # Estimate dividend yield for HK stocks
    if result.get("pe_ttm") and result["pe_ttm"] > 0:
        est_div_yield = (0.35 / result["pe_ttm"]) * 100
        result["dividend_yield_pct"] = round(est_div_yield, 2)

    filled = sum(1 for k in ["pe_ttm", "pb", "roe", "eps"] if result.get(k) is not None)
    if filled >= 3:
        result["data_quality"] = "good"
    elif filled >= 1:
        result["data_quality"] = "partial"
    else:
        result["data_quality"] = "error"

    if errors:
        result["_errors"] = errors

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch fundamental data")
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 00700.HK")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")
    args = parser.parse_args()

    # Check cache
    cache_key = f"fundamental_{args.ts_code}"
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl(trading_ttl=1800, after_hours_ttl=57600))
        if cached:
            text = json.dumps(cached, ensure_ascii=False, indent=2)
            if args.output:
                os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"Fundamental data (cached) written to {args.output}", file=sys.stderr)
            else:
                print(text)
            return

    code = args.ts_code.split(".")[0]
    suffix = "." + args.ts_code.split(".")[1] if "." in args.ts_code else ""
    asset = args.asset or ("FD" if code.startswith(("5", "15")) else "E")
    is_hk = suffix == ".HK" or args.ts_code.endswith(".HK")

    errors = []

    # ETFs have no meaningful fundamental analysis
    if asset == "FD":
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "asset": "FD",
                "data_source": "skip",
                "note": "ETF 不进行基本面分析",
            },
            "summary": {"data_quality": "skip"},
            "data": {},
            "errors": [],
        }
    elif is_hk:
        hk_code = code.lstrip("0")
        fund_data = fetch_hk_fundamentals(hk_code)
        errors = fund_data.pop("_errors", [])
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "data_source": "akshare_hk",
                "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "asset": "E",
            },
            "summary": fund_data,
            "data": {},
            "errors": errors,
        }
    else:
        fund_data = fetch_a_share_fundamentals(code)
        errors = fund_data.pop("_errors", [])
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "data_source": "akshare",
                "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "asset": "E",
            },
            "summary": fund_data,
            "data": {},
            "errors": errors,
        }

    # Cache successful result (skip ETF placeholder)
    if result.get("meta", {}).get("data_source") not in ("error", "skip", None):
        save_cache(cache_key, result)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Fundamental data written to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
