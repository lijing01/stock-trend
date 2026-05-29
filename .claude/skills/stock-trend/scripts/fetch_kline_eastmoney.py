#!/usr/bin/env python3
"""East Money (东方财富) K-line data fetcher for stock-trend skill.

Free data source, no token required. Supports A-shares and ETFs.
Used as fallback when Tushare is unavailable.

Usage:
    python3 fetch_kline_eastmoney.py <ts_code> [options]

Examples:
    python3 fetch_kline_eastmoney.py 600519.SH
    python3 fetch_kline_eastmoney.py 513180.SH --asset FD -o /tmp/kline.json
    python3 fetch_kline_eastmoney.py 000001.SZ --freq W
"""

import argparse
import json
import os
import sys
from cache_utils import load_cache, output_json, save_cache, get_market_day_ttl
from resolve_code import detect_asset, detect_adj
from datetime import datetime, timedelta
from eastmoney_utils import (
    EM_HEADERS, EM_API_HOSTS, build_secid,
    fetch_url, build_em_kline_url, parse_em_kline_line,
)


def fetch_eastmoney(secid, freq, lmt=250, host="push2his.eastmoney.com"):
    """Fetch K-line data from East Money API.

    Returns a list of parsed records or raises on error.
    """
    url = build_em_kline_url(host, secid, freq=freq, lmt=lmt)
    try:
        raw = fetch_url(url, headers=EM_HEADERS, timeout=15)
        result = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"东方财富API请求失败({host}): {e}")

    if not result or result.get("rc") != 0 or not result.get("data"):
        error_msg = result.get("message", "未知错误") if result else "无响应"
        raise RuntimeError(f"东方财富API返回错误: {error_msg}")

    data = result["data"]
    klines = data.get("klines", [])
    name = data.get("name", "")
    code = data.get("code", "")

    if not klines:
        raise RuntimeError(f"未获取到数据（可能代码无效或已停牌）")

    records = []
    for line in klines:
        record = parse_em_kline_line(line)
        if record:
            records.append(record)

    return records, name


def fetch_hk_stock(ts_code, freq, lmt=250):
    """Fetch HK stock K-line data via Tencent Finance API.

    Tencent Finance provides free HK stock data without authentication.
    Used as fallback when EastMoney and Tushare don't support .HK codes.

    Args:
        ts_code: Tushare-style code, e.g. 00700.HK
        freq: 'D' for daily, 'W' for weekly
        lmt: Number of records to fetch (default: 250)

    Returns:
        Tuple of (records_list, name_string)

    Raises:
        RuntimeError: If Tencent API fails to return data
    """
    import urllib.request

    code = ts_code.split(".")[0]  # e.g. "00700"
    klt = "week" if freq == "W" else "day"
    # Tencent Finance HK stock API
    # Returns qfq (forward-adjusted) data by default
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param=hk{code},{klt},,,{lmt},qfq"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.qq.com/",
        "Accept": "*/*",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"腾讯港股API请求失败: {e}")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(f"腾讯港股API返回非JSON数据")

    if data.get("code") != 0:
        raise RuntimeError(f"腾讯港股API返回错误: code={data.get('code')}")

    # Data structure: {"code":0, "data": {"hk00700": {"day": [[date, open, close, high, low, vol], ...]}}}
    stock_key = f"hk{code}"
    stock_data = data.get("data", {}).get(stock_key, {})
    klines = stock_data.get(klt, stock_data.get("day", []))

    if not klines:
        # Try alternative key format
        for key in stock_data:
            if isinstance(stock_data[key], list):
                klines = stock_data[key]
                break

    if not klines:
        raise RuntimeError(f"腾讯港股API未返回数据: {ts_code}")

    records = []
    for item in klines:
        try:
            # Tencent format: ["2026-01-02", "474.000", "467.800", "474.800", "463.200", "38406905.000"]
            # Fields: date, open, close, high, low, volume
            if len(item) < 6:
                continue

            trade_date = str(item[0]).replace("-", "")
            open_p = float(item[1])
            close_p = float(item[2])
            high_p = float(item[3])
            low_p = float(item[4])
            vol = float(item[5]) if len(item) > 5 else 0

            if trade_date and close_p > 0:
                record = {
                    "trade_date": trade_date,
                    "open": open_p,
                    "close": close_p,
                    "high": high_p,
                    "low": low_p,
                    "vol": vol,
                    "amount": 0,  # Tencent HK doesn't provide amount in this API
                }
                # Calculate pct_chg and pre_close from previous record
                if len(records) > 0:
                    prev_close = records[-1]["close"]
                    record["pre_close"] = round(prev_close, 4)
                    if prev_close > 0:
                        record["pct_chg"] = round((close_p - prev_close) / prev_close * 100, 4)

                records.append(record)
        except (ValueError, IndexError):
            continue

    if not records:
        raise RuntimeError(f"腾讯港股API未返回有效数据: {ts_code}")

    # Sort by date ascending (API may return in various orders)
    records.sort(key=lambda x: x["trade_date"])

    name = ts_code  # Tencent doesn't provide name in this API
    return records, name


def main():
    parser = argparse.ArgumentParser(
        description="Fetch K-line data from East Money (东方财富) API"
    )
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 513180.SH, 00700.HK")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("--freq", choices=["D", "W"], default="D", help="Frequency: D=daily, W=weekly")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], help="Adjustment type (auto-detected if omitted)")
    parser.add_argument("--lmt", type=int, default=250, help="Number of records to fetch (default: 250)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")

    args = parser.parse_args()

    # Check cache (shared key with fetch_kline.py)
    adj = args.adj or detect_adj(args.ts_code)
    cache_key = f"kline_{args.ts_code}_{args.freq}_{adj}"
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached:
            output_json(cached, output_path=args.output)
            return

    # Check if market is supported by EastMoney
    secid = build_secid(args.ts_code)
    asset = args.asset or detect_asset(args.ts_code)
    adj = args.adj or detect_adj(args.ts_code)

    # For HK stocks, use Sina Finance API directly
    if args.ts_code.endswith(".HK"):
        records = None
        name = ""
        error_msg = None
        try:
            records, name = fetch_hk_stock(args.ts_code, args.freq, args.lmt)
        except Exception as e:
            error_msg = f"腾讯港股API失败: {e}"

        if records is None:
            result = {
                "meta": {
                    "ts_code": args.ts_code,
                    "asset": asset,
                    "freq": args.freq,
                    "data_source": "error",
                    "error": error_msg,
                },
                "data": [],
            }
            output_json(result, output_path=args.output)
            return

        record_count = len(records)
        warnings = []
        if record_count < 60:
            warnings.append(f"数据记录不足60条（仅{record_count}条），部分指标可能无法准确计算")

        for r in records:
            r["ts_code"] = args.ts_code

        result = {
            "meta": {
                "ts_code": args.ts_code,
                "asset": asset,
                "freq": args.freq,
                "adj": "none",
                "record_count": record_count,
                "data_points": record_count,
                "data_source": "tencent_hk",
                "warnings": warnings,
            },
            "data": records,
        }
        output_json(result, output_path=args.output)
        return

    if secid is None:
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "data_source": "error",
                "error": (
                    f"东方财富不支持 {args.ts_code} 所属市场。"
                    "仅支持上交所(.SH)和深交所(.SZ)的A股及ETF。"
                ),
            },
            "data": [],
        }
        output_json(result, output_path=args.output)
        return

    # Fetch data with host rotation
    records = None
    name = ""
    error_msg = None
    used_host = None

    from eastmoney_utils import rotate_em_host
    try:
        (records, name), used_host = rotate_em_host(lambda h: fetch_eastmoney(secid, args.freq, args.lmt, host=h))
    except RuntimeError as e:
        error_msg = str(e)

    # Fallback to BaoStock if all EastMoney hosts failed
    if records is None and not args.ts_code.endswith(".HK"):
        try:
            records, name = fetch_baostock(args.ts_code, args.freq)
            used_host = "baostock"
        except Exception as e:
            error_msg = f"东方财富全节点失败 + BaoStock降级失败: {error_msg}; {e}"

    if records is None:
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "asset": asset,
                "freq": args.freq,
                "data_source": "error",
                "error": error_msg,
            },
            "data": [],
        }
        output_json(result, output_path=args.output)
        return

    data_source = "eastmoney" if used_host in EM_API_HOSTS else "baostock"

    record_count = len(records)
    warnings = []

    if record_count < 60:
        warnings.append(f"数据记录不足60条（仅{record_count}条），部分指标可能无法准确计算")

    # Add ts_code to each record
    for r in records:
        r["ts_code"] = args.ts_code

    result = {
        "meta": {
            "ts_code": args.ts_code,
            "asset": asset,
            "freq": args.freq,
            "adj": adj,
            "record_count": record_count,
            "data_points": record_count,
            "data_source": data_source,
            "em_host": used_host if used_host in EM_API_HOSTS else None,
            "warnings": warnings,
        },
        "data": records,
    }

    # Cache successful result
    if result.get("meta", {}).get("data_source") not in ("error", None):
        save_cache(cache_key, result)

    output_json(result, output_path=args.output)


def fetch_baostock(ts_code, freq):
    """Fetch K-line data from BaoStock as a Level-3 fallback.

    BaoStock is an independent data source (not EastMoney) that covers
    A-shares (including STAR board) and ETFs. It does NOT support HK stocks.

    Args:
        ts_code: Tushare-style code, e.g. 600519.SH, 159919.SZ
        freq: 'D' for daily, 'W' for weekly

    Returns:
        Tuple of (records_list, name_string)

    Raises:
        RuntimeError: If BaoStock fails to return data
        ImportError: If baostock package is not installed
    """
    import baostock as bs

    # Convert ts_code to BaoStock code format
    # 600519.SH -> sh.600519, 000001.SZ -> sz.000001
    code, suffix = ts_code.rsplit(".", 1)
    if suffix == "SH":
        bs_code = f"sh.{code}"
    elif suffix == "SZ":
        bs_code = f"sz.{code}"
    else:
        raise RuntimeError(f"BaoStock不支持港股代码: {ts_code}")

    frequency = "d" if freq == "D" else "w"
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d") if freq == "D" else (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock登录失败: {lg.error_msg}")

    try:
        # adjustflag: 2=前复权(qfq)
        # Note: weekly frequency does not support 'preclose' field
        if frequency == "w":
            fields = "date,open,high,low,close,volume,amount,pctChg"
        else:
            fields = "date,open,high,low,close,volume,amount,pctChg,preclose"

        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag="2",
        )

        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock查询失败: {rs.error_msg}")

        records = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            try:
                record = {
                    "trade_date": row[0].replace("-", ""),
                    "open": float(row[1]) if row[1] else None,
                    "high": float(row[2]) if row[2] else None,
                    "low": float(row[3]) if row[3] else None,
                    "close": float(row[4]) if row[4] else None,
                    "pct_chg": float(row[7]) if row[7] else None,
                    "vol": float(row[5]) if row[5] else None,
                    "amount": float(row[6]) if row[6] else None,
                }
                # pre_close may not be available for weekly data
                pre_close_idx = 8
                if len(row) > pre_close_idx and row[pre_close_idx]:
                    record["pre_close"] = float(row[pre_close_idx])
                # Skip records with None close price
                if record["close"] is not None:
                    records.append(record)
            except (ValueError, IndexError):
                continue

        if not records:
            raise RuntimeError(f"BaoStock未返回数据: {bs_code}")

        # BaoStock doesn't return stock name, use ts_code as placeholder
        name = ts_code
        return records, name

    finally:
        bs.logout()



if __name__ == "__main__":
    main()
