#!/usr/bin/env python3
"""Capital flow data fetcher for stock-trend skill.

Fetches money flow, northbound capital, margin trading, and
dragon & tiger list data. Supports both individual stocks and ETFs.

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
import logging
from cache_utils import load_cache, save_cache, get_market_day_ttl
from datetime import datetime

logging.getLogger("akshare").setLevel(logging.ERROR)

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


def _safe_float(val):
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ──────────────────────────── Existing Functions ────────────────────────────


def fetch_stock_capital_flow(secid, days=5):
    """Fetch capital flow data for individual stocks from East Money."""
    url = (
        f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        f"?secid={secid}"
        f"&fields1=f1,f2,f3,f7"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        f"&klt=101"
        f"&lmt={days}"
    )
    req = urllib.request.Request(url, headers=EM_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if not result or result.get("rc") != 0 or not result.get("data"):
        raise RuntimeError("东方财富资金流向API返回错误")

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
                "main_net_inflow": _safe_float(parts[1]),
                "main_inflow": _safe_float(parts[2]),
                "main_outflow": _safe_float(parts[3]),
                "retail_net_inflow": _safe_float(parts[4]),
                "retail_inflow": _safe_float(parts[5]),
                "retail_outflow": _safe_float(parts[6]),
                "total_net_inflow": _safe_float(parts[7]) if len(parts) > 7 else None,
            }
            records.append(record)
        except (ValueError, IndexError):
            continue
    return records


def fetch_etf_capital_flow(fund_code, days=5):
    """Fetch ETF subscription/redemption flow from East Money pingzhongdata."""
    import re
    url = f"http://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    req = urllib.request.Request(url, headers={
        "User-Agent": EM_HEADERS["User-Agent"],
        "Referer": "http://fund.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read().decode("utf-8")

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


# ──────────────────────────── New: Northbound Capital ────────────────────────────


def fetch_northbound_flow():
    """Fetch northbound (沪深股通) capital flow history using AKShare."""
    import akshare as ak
    df = ak.stock_hsgt_hist_em("北向资金")
    if df is not None and not df.empty:
        records = []
        for _, row in df.iterrows():
            records.append({
                "date": str(row.iloc[0]).replace("-", ""),
                "net_buy_billion": _safe_float(row.get("沪股通净流入") or row.get("深股通净流入")),
            })
        # Aggregate daily totals
        daily = {}
        for r in records:
            d = r["date"]
            if d not in daily:
                daily[d] = 0
            if r["net_buy_billion"]:
                daily[d] += r["net_buy_billion"]
        result = [{"date": d, "net_buy_billion": round(v, 2)} for d, v in sorted(daily.items())]
        return result[-10:]  # last 10 days
    return None


def fetch_individual_northbound(code):
    """Fetch individual stock northbound holding data."""
    import akshare as ak
    try:
        df = ak.stock_hsgt_individual_em(code)
        if df is not None and not df.empty:
            latest = df.iloc[-1] if len(df) > 1 else df.iloc[0]
            return {
                "hold_shares": _safe_float(latest.get("持股股数")),
                "hold_value_billion": _safe_float(latest.get("持股数")),
                "change_shares": _safe_float(latest.get("股数变动")),
            }
    except Exception:
        pass
    return None


# ──────────────────────────── New: Margin Trading ────────────────────────────


def fetch_margin_detail(code, exchange="SH"):
    """Fetch margin trading detail for a given stock."""
    import akshare as ak
    today = datetime.now().strftime("%Y%m%d")
    try:
        if exchange == "SH":
            df = ak.stock_margin_detail_sse(date=today)
        else:
            df = ak.stock_margin_detail_szse(date=today)
        if df is not None and not df.empty:
            match = df[df["证券代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                return {
                    "margin_balance_billion": _safe_float(row.get("融资余额")),
                    "margin_buy_billion": _safe_float(row.get("融资买入额")),
                    "net_margin_billion": _safe_float(row.get("融资余额")),
                }
    except Exception:
        pass
    return None


# ──────────────────────────── New: Dragon & Tiger List ────────────────────────────


def fetch_longhubang(code, days=5):
    """Fetch dragon & tiger list data for a given stock."""
    import akshare as ak
    from datetime import timedelta
    end = datetime.now()
    start = end - timedelta(days=20)
    try:
        df = ak.stock_lhb_detail_em(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            match = df[df["代码"] == code]
            if not match.empty:
                records = []
                for _, row in match.iterrows():
                    records.append({
                        "date": str(row.get("日期", "")).replace("-", ""),
                        "reason": row.get("上榜原因", ""),
                        "total_net_buy_billion": _safe_float(row.get("龙虎榜净买入额")),
                        "institution_net_buy_billion": _safe_float(row.get("机构净买入额")),
                    })
                return records[-days:]
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Fetch capital flow data")
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 159740.SZ")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")
    args = parser.parse_args()

    # Check cache
    cache_key = f"capital_flow_{args.ts_code}"
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached:
            text = json.dumps(cached, ensure_ascii=False, indent=2)
            if args.output:
                os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"Capital flow data (cached) written to {args.output}", file=sys.stderr)
            else:
                print(text)
            return

    code = args.ts_code.split(".")[0]
    suffix = "." + args.ts_code.split(".")[1] if "." in args.ts_code else ""
    asset = args.asset or ("FD" if code.startswith(("5", "15")) else "E")

    errors = []

    # ── Primary flow data (existing) ──
    result = {"meta": {}, "data": [], "data_extended": {}}

    if asset == "FD":
        fund_code = code
        try:
            flows = fetch_etf_capital_flow(fund_code)
            result["meta"] = {
                "ts_code": args.ts_code, "asset": "FD", "fund_code": fund_code,
                "data_source": "eastmoney_etf", "record_count": len(flows),
            }
            result["data"] = flows
        except Exception as e:
            result["meta"] = {
                "ts_code": args.ts_code, "asset": "FD",
                "data_source": "error", "error": f"ETF资金流向获取失败: {e}",
            }
            result["data"] = []
    else:
        secid = resolve_secid(args.ts_code)
        if secid is None:
            result["meta"] = {
                "ts_code": args.ts_code, "data_source": "error",
                "error": f"不支持的市场代码: {args.ts_code}",
            }
            result["data"] = []
        else:
            try:
                flows = fetch_stock_capital_flow(secid)
                result["meta"] = {
                    "ts_code": args.ts_code, "asset": "E", "secid": secid,
                    "data_source": "eastmoney", "record_count": len(flows),
                }
                result["data"] = flows
            except Exception as e:
                result["meta"] = {
                    "ts_code": args.ts_code, "asset": "E",
                    "data_source": "error", "error": f"资金流向获取失败: {e}",
                }
                result["data"] = []

    # ── Enhanced: Northbound flow (A-shares only) ──
    is_hk = suffix == ".HK" or args.ts_code.endswith(".HK")
    if asset == "E" and not is_hk:
        try:
            nb_market = fetch_northbound_flow()
            if nb_market:
                result["data_extended"]["northbound_market"] = nb_market
        except Exception as e:
            errors.append(f"北向资金: {e}")

        try:
            nb_individual = fetch_individual_northbound(code)
            if nb_individual:
                result["data_extended"]["northbound_individual"] = nb_individual
        except Exception as e:
            errors.append(f"个股北向: {e}")

        # ── Enhanced: Margin trading ──
        try:
            exchange = "SH" if suffix == ".SH" else "SZ"
            margin = fetch_margin_detail(code, exchange)
            if margin:
                result["data_extended"]["margin"] = margin
        except Exception as e:
            errors.append(f"融资融券: {e}")

        # ── Enhanced: Dragon & Tiger list ──
        try:
            lhb = fetch_longhubang(code)
            if lhb:
                result["data_extended"]["longhubang"] = lhb
        except Exception as e:
            errors.append(f"龙虎榜: {e}")

    if errors:
        result["warnings"] = errors

    # Cache successful result
    if result.get("meta", {}).get("data_source") not in ("error", None):
        save_cache(cache_key, result)

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
