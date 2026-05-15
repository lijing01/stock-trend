#!/usr/bin/env python3
"""Capital flow data fetcher for stock-trend skill.

Fetches money flow (主力资金流向) data from East Money API.
Supports both individual stocks (E) and ETF/Fund (FD) flows.

Usage:
    python3 fetch_capital_flow.py <ts_code> [--asset E|FD] [-o output.json]

Examples:
    python3 fetch_capital_flow.py 600519.SH
    python3 fetch_capital_flow.py 159740.SZ --asset FD -o /tmp/capital_flow.json
"""

import argparse
import json
import os
import sys
import urllib.request

# Reuse secid mapping from fetch_kline_eastmoney
MARKET_PREFIX = {
    ".SH": "1",
    ".SZ": "0",
}


def resolve_secid(ts_code):
    """Convert ts_code to East Money secid format."""
    if "." not in ts_code:
        return None
    code, suffix = ts_code.rsplit(".", 1)
    suffix = f".{suffix}"
    prefix = MARKET_PREFIX.get(suffix)
    if prefix is None:
        return None
    return f"{prefix}.{code}"


EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch_stock_capital_flow(secid, days=5):
    """Fetch capital flow data for individual stocks from East Money.

    Returns list of daily flow records (most recent first).
    """
    url = (
        f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        f"?secid={secid}"
        f"&fields1=f1,f2,f3,f7"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        f"&klt=101"  # daily
        f"&lmt={days}"
    )

    req = urllib.request.Request(url, headers=EM_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if not result or result.get("rc") != 0 or not result.get("data"):
        raise RuntimeError(f"东方财富资金流向API返回错误")

    data = result["data"]
    klines = data.get("klines", [])

    records = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 10:
            continue
        try:
            record = {
                "date": parts[0].replace("-", ""),
                "main_net_inflow": _safe_float(parts[1]),      # 主力净流入
                "main_inflow": _safe_float(parts[2]),           # 主力流入
                "main_outflow": _safe_float(parts[3]),          # 主力流出
                "retail_net_inflow": _safe_float(parts[4]),    # 散户净流入
                "retail_inflow": _safe_float(parts[5]),          # 散户流入
                "retail_outflow": _safe_float(parts[6]),         # 散户流出
                "total_net_inflow": _safe_float(parts[7]) if len(parts) > 7 else None,
            }
            records.append(record)
        except (ValueError, IndexError):
            continue

    return records


def fetch_etf_capital_flow(fund_code, days=5):
    """Fetch ETF subscription/redemption flow from East Money pingzhongdata.

    Returns list of daily flow records.
    """
    url = f"http://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    req = urllib.request.Request(url, headers={
        "User-Agent": EM_HEADERS["User-Agent"],
        "Referer": "http://fund.eastmoney.com/",
    })

    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read().decode("utf-8")

    import re
    # Extract Data_flvol variable
    m = re.search(r'var\s+Data_flvol\s*=\s*(\[.+?\]);', content, re.DOTALL)
    if not m:
        return []

    try:
        flvol_data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []

    records = []
    for item in flvol_data[-days:]:
        if isinstance(item, list) and len(item) >= 2:
            records.append({
                "date": str(item[0]).replace("-", ""),
                "shares_billion": _safe_float(item[1]),
                "type": "etf_subscription_redemption",
            })

    return records


def _safe_float(val):
    """Safely convert value to float, returning None on failure."""
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch capital flow data")
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 159740.SZ")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")

    args = parser.parse_args()

    # Auto-detect asset type
    code = args.ts_code.split(".")[0]
    asset = args.asset or ("FD" if code.startswith(("5", "15")) else "E")

    errors = []

    if asset == "FD":
        # ETF: use pingzhongdata for subscription/redemption flows
        fund_code = code
        try:
            flows = fetch_etf_capital_flow(fund_code)
            result = {
                "meta": {
                    "ts_code": args.ts_code,
                    "asset": "FD",
                    "fund_code": fund_code,
                    "data_source": "eastmoney_etf",
                    "record_count": len(flows),
                },
                "data": flows,
            }
        except Exception as e:
            result = {
                "meta": {
                    "ts_code": args.ts_code,
                    "asset": "FD",
                    "data_source": "error",
                    "error": f"ETF资金流向获取失败: {e}",
                },
                "data": [],
            }
    else:
        # Stock: use fflow API
        secid = resolve_secid(args.ts_code)
        if secid is None:
            result = {
                "meta": {
                    "ts_code": args.ts_code,
                    "data_source": "error",
                    "error": f"不支持的市场代码: {args.ts_code}",
                },
                "data": [],
            }
        else:
            try:
                flows = fetch_stock_capital_flow(secid)
                result = {
                    "meta": {
                        "ts_code": args.ts_code,
                        "asset": "E",
                        "secid": secid,
                        "data_source": "eastmoney",
                        "record_count": len(flows),
                    },
                    "data": flows,
                }
            except Exception as e:
                result = {
                    "meta": {
                        "ts_code": args.ts_code,
                        "asset": "E",
                        "data_source": "error",
                        "error": f"资金流向获取失败: {e}",
                    },
                    "data": [],
                }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Capital flow data written to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()