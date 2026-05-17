#!/usr/bin/env python3
"""Index PE percentile fetcher for ETF index valuation.

Fetches PE percentile data for the tracking index of ETFs.
Tiered data sources:
  Tier 1 — legulegu (stock_index_pe_lg): historical PE series, 3-yr percentile
  Tier 2 — CSIndex (stock_zh_index_value_csindex): current PE, 20-day percentile
  Tier 3 — HK indices: skip (no reliable automated API)

Usage:
    python3 fetch_index_valuation.py --code <etf_code> [-o output.json]

Examples:
    python3 fetch_index_valuation.py --code 510300 -o /tmp/index_valuation.json
    python3 fetch_index_valuation.py --code 510050
"""

import argparse
import json
import os
import sys
import logging
from cache_utils import load_cache, save_cache, get_market_day_ttl
from datetime import datetime
from contextlib import contextmanager

logging.getLogger("akshare").setLevel(logging.ERROR)

# ETF code → tracking index mapping
# lg_name: index name for stock_index_pe_lg (Tier 1, best — historical PE)
# csindex_code: code for stock_zh_index_value_csindex (Tier 2, current PE only)
# If both are None, the ETF's index is not supported (e.g. HK indices)
ETF_INDEX_MAP = {
    "510300": {"index_name": "沪深300", "lg_name": "沪深300", "csindex_code": "000300"},
    "510310": {"index_name": "沪深300", "lg_name": "沪深300", "csindex_code": "000300"},
    "510050": {"index_name": "上证50", "lg_name": "上证50", "csindex_code": "000016"},
    "510500": {"index_name": "中证500", "lg_name": None, "csindex_code": "000905"},
    "510580": {"index_name": "中证500", "lg_name": None, "csindex_code": "000905"},
    "512500": {"index_name": "中证500", "lg_name": None, "csindex_code": "000905"},
    "510210": {"index_name": "上证380", "lg_name": "上证380", "csindex_code": "000009"},
    "159845": {"index_name": "中证1000", "lg_name": None, "csindex_code": "000852"},
    "512100": {"index_name": "中证1000", "lg_name": None, "csindex_code": "000852"},
    "588000": {"index_name": "科创50", "lg_name": None, "csindex_code": None},
    "588050": {"index_name": "科创50", "lg_name": None, "csindex_code": None},
    "159915": {"index_name": "创业板指", "lg_name": None, "csindex_code": None},
    "159949": {"index_name": "创业板50", "lg_name": "创业板50", "csindex_code": None},
    "159901": {"index_name": "深证100", "lg_name": "深证100", "csindex_code": "399330"},
    "159905": {"index_name": "深证红利", "lg_name": None, "csindex_code": "399324"},
    "510880": {"index_name": "上证红利", "lg_name": None, "csindex_code": "000015"},
    "510180": {"index_name": "上证180", "lg_name": None, "csindex_code": "000010"},
    "512010": {"index_name": "中证医药", "lg_name": None, "csindex_code": "000933"},
    "515000": {"index_name": "科技龙头", "lg_name": None, "csindex_code": "931087"},
    # HK indices — no automated API
    "513180": {"index_name": "恒生科技", "lg_name": None, "csindex_code": None},
    "513130": {"index_name": "恒生科技", "lg_name": None, "csindex_code": None},
    "159740": {"index_name": "恒生科技", "lg_name": None, "csindex_code": None},
    "513330": {"index_name": "恒生互联网", "lg_name": None, "csindex_code": None},
    "513060": {"index_name": "恒生医疗", "lg_name": None, "csindex_code": None},
    "510900": {"index_name": "恒生国企", "lg_name": None, "csindex_code": None},
}


def _safe_float(val):
    if val is None:
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


@contextmanager
def _suppress_stderr():
    """Temporarily suppress stderr to hide AKShare tqdm progress bars."""
    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        yield
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr


def _retry(func, max_attempts=2, delay=2):
    """Call func with retry. Suppresses stderr during call."""
    import time
    last_err = None
    for attempt in range(max_attempts):
        try:
            with _suppress_stderr():
                return func(), None
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay)
    return None, str(last_err)


def _calc_percentile(values, current_val):
    """Calculate what percent of values are below current_val (0-100)."""
    if values.empty or current_val is None:
        return None
    below = (values < current_val).sum()
    return round(float(below) / len(values) * 100, 1)


def fetch_index_valuation_tier1_lg(etf_code, lg_name):
    """Tier 1: Use stock_index_pe_lg for historical PE and 3-yr percentile.

    Returns dict with summary fields or None on failure.
    """
    import akshare as ak
    import pandas as pd

    df, err = _retry(lambda: ak.stock_index_pe_lg(symbol=lg_name), max_attempts=2, delay=3)
    if err:
        return None, f"stock_index_pe_lg({lg_name}): {err}"
    if df is None or df.empty:
        return None, f"stock_index_pe_lg({lg_name}): empty result"

    # Use 滚动市盈率 (TTM PE) column
    pe_col = "滚动市盈率"
    if pe_col not in df.columns:
        return None, f"stock_index_pe_lg({lg_name}): no {pe_col} column"

    pe_values = df[pe_col].dropna().astype(float)
    if pe_values.empty:
        return None, f"stock_index_pe_lg({lg_name}): no valid PE values"

    current_pe = _safe_float(pe_values.iloc[-1])
    pe_percentile_3y = _calc_percentile(pe_values, current_pe)

    # Get date range
    date_col = "日期"
    latest_date = str(df[date_col].iloc[-1]) if date_col in df.columns else ""

    # Also get the index close price
    index_close = _safe_float(df["指数"].iloc[-1]) if "指数" in df.columns else None

    result = {
        "pe_ttm": current_pe,
        "pe_percentile_3y": pe_percentile_3y,
        "pe_percentile_20d": None,
        "dividend_yield_pct": None,
        "data_quality": "good",
        "index_close": index_close,
        "latest_date": latest_date,
        "record_count": len(pe_values),
        "data_source": "legulegu",
    }
    return result, None


def fetch_index_valuation_tier2_csindex(etf_code, csindex_code):
    """Tier 2: Use stock_zh_index_value_csindex for current PE.

    CSIndex only provides ~20 data points, not enough for 3-yr percentile.
    We compute a short-term percentile as a rough signal.
    """
    import akshare as ak

    df, err = _retry(lambda: ak.stock_zh_index_value_csindex(symbol=csindex_code), max_attempts=2, delay=3)
    if err:
        return None, f"stock_zh_index_value_csindex({csindex_code}): {err}"
    if df is None or df.empty:
        return None, f"stock_zh_index_value_csindex({csindex_code}): empty result"

    # 市盈率2 is typically TTM PE, 股息率1 is dividend yield
    pe_col = "市盈率2"
    dy_col = "股息率1"

    if pe_col not in df.columns:
        return None, f"stock_zh_index_value_csindex({csindex_code}): no {pe_col} column"

    pe_values = df[pe_col].dropna().astype(float)
    if pe_values.empty:
        return None, f"stock_zh_index_value_csindex({csindex_code}): no valid PE values"

    current_pe = _safe_float(pe_values.iloc[0])  # Most recent (sorted desc)
    pe_percentile_20d = _calc_percentile(pe_values, current_pe)

    # Dividend yield
    div_yield = None
    if dy_col in df.columns:
        dy_values = df[dy_col].dropna().astype(float)
        if not dy_values.empty:
            div_yield = _safe_float(dy_values.iloc[0])

    latest_date = str(df["日期"].iloc[0]) if "日期" in df.columns else ""

    result = {
        "pe_ttm": current_pe,
        "pe_percentile_3y": None,
        "pe_percentile_20d": pe_percentile_20d,
        "dividend_yield_pct": div_yield,
        "data_quality": "partial",
        "index_close": None,
        "latest_date": latest_date,
        "record_count": len(pe_values),
        "data_source": "csindex",
    }
    return result, None


def main():
    parser = argparse.ArgumentParser(description="Fetch index PE percentile for ETF")
    parser.add_argument("--code", required=True, help="ETF code (e.g. 510300)")
    parser.add_argument("-o", "--output", help="Output JSON file path")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")
    args = parser.parse_args()

    etf_code = args.code
    cache_key = f"index_valuation_{etf_code}"

    # Check cache
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached is not None:
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(cached, f, ensure_ascii=False, indent=2)
                print(f"Index valuation (cached) → {args.output}")
            else:
                print(json.dumps(cached, ensure_ascii=False, indent=2))
            return

    # Look up ETF → Index mapping
    index_info = ETF_INDEX_MAP.get(etf_code)
    if index_info is None:
        result = {
            "meta": {
                "etf_code": etf_code,
                "index_name": None,
                "data_source": "skip",
                "data_quality": "skip",
                "note": f"ETF {etf_code} 未配置跟踪指数映射",
            },
            "summary": {"data_quality": "skip"},
            "data": {},
            "errors": ["no_index_mapping"],
        }
        _output(result, args)
        save_cache(cache_key, result)
        return

    index_name = index_info["index_name"]
    lg_name = index_info.get("lg_name")
    csindex_code = index_info.get("csindex_code")

    # Try Tier 1: legulegu (historical PE, best quality)
    summary = None
    errors = []
    source = ""
    if lg_name:
        summary, err = fetch_index_valuation_tier1_lg(etf_code, lg_name)
        if summary:
            source = summary.pop("data_source", "legulegu")
        if err:
            errors.append(err)

    # Fallback to Tier 2: CSIndex (current PE only)
    if summary is None and csindex_code:
        summary, err = fetch_index_valuation_tier2_csindex(etf_code, csindex_code)
        if summary:
            source = summary.pop("data_source", "csindex")
        if err:
            errors.append(err)

    # Tier 3: skip (HK indices, no API)
    if summary is None:
        result = {
            "meta": {
                "etf_code": etf_code,
                "index_name": index_name,
                "data_source": "skip",
                "data_quality": "skip",
                "note": f"{index_name} 指数PE数据暂无可用的自动数据源",
            },
            "summary": {"data_quality": "skip"},
            "data": {},
            "errors": errors,
        }
        _output(result, args)
        save_cache(cache_key, result)
        return

    result = {
        "meta": {
            "etf_code": etf_code,
            "index_name": index_name,
            "data_source": source,
            "data_quality": summary["data_quality"],
            "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
        },
        "summary": summary,
        "data": {
            "latest_date": summary.get("latest_date", ""),
            "record_count": summary.get("record_count", 0),
        },
        "errors": errors,
    }

    # Clean transient fields from summary
    for field in ("latest_date", "record_count", "data_source"):
        summary.pop(field, None)

    _output(result, args)
    save_cache(cache_key, result)


def _output(data, args):
    """Write output to file or stdout."""
    out = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"Index valuation → {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
