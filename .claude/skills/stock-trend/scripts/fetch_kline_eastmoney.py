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
import sys
import urllib.request
from datetime import datetime, timedelta


# --- EastMoney API node rotation ---

EM_API_HOSTS = [
    "push2his.eastmoney.com",
    "38.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
]

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# --- secid mapping ---

# Market prefix: .SH -> 1 (Shanghai), .SZ -> 0 (Shenzhen)
MARKET_PREFIX = {
    ".SH": "1",
    ".SZ": "0",
}


def resolve_secid(ts_code):
    """Convert ts_code to East Money secid format.

    Returns None for unsupported markets (e.g. .HK).
    """
    if "." not in ts_code:
        return None

    code, suffix = ts_code.rsplit(".", 1)
    suffix = f".{suffix}"

    prefix = MARKET_PREFIX.get(suffix)
    if prefix is None:
        return None

    return f"{prefix}.{code}"


def detect_asset(ts_code):
    """Auto-detect asset type from ts_code pattern."""
    code = ts_code.split(".")[0]
    if code.startswith(("5", "15")):
        return "FD"
    return "E"


def detect_adj(ts_code):
    """Auto-detect adjustment type from ts_code."""
    if ts_code.endswith(".HK"):
        return "none"
    return "qfq"


def freq_to_klt(freq):
    """Convert frequency code to East Money klt parameter."""
    return "102" if freq == "W" else "101"


def calc_beg_date(freq):
    """Calculate start date string for East Money API based on frequency."""
    end = datetime.now()
    if freq == "W":
        start = end - timedelta(days=730)  # ~2 years for weekly
    else:
        start = end - timedelta(days=365)  # ~1 year for daily
    return start.strftime("%Y%m%d")


def fetch_eastmoney(secid, freq, lmt=250, host="push2his.eastmoney.com"):
    """Fetch K-line data from East Money API.

    Returns a list of parsed records or raises on error.
    """
    klt = freq_to_klt(freq)
    beg = calc_beg_date(freq)
    end = datetime.now().strftime("%Y%m%d")
    # fqt=1: forward adjustment (前复权)
    fields1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    url = (
        f"https://{host}/api/qt/stock/kline/get"
        f"?secid={secid}"
        f"&fields1={fields1}"
        f"&fields2={fields2}"
        f"&klt={klt}"
        f"&fqt=1"
        f"&beg={beg}"
        f"&end={end}"
        f"&lmt={lmt}"
    )

    req = urllib.request.Request(url, headers=EM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
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
        parts = line.split(",")
        if len(parts) < 11:
            continue
        # f51=日期, f52=开, f53=收, f54=高, f55=低,
        # f56=成交量, f57=成交额, f58=振幅, f59=涨跌幅, f60=涨跌额, f61=换手率
        try:
            trade_date = parts[0].replace("-", "")
            open_p = float(parts[1])
            close_p = float(parts[2])
            high_p = float(parts[3])
            low_p = float(parts[4])
            vol = float(parts[5])
            amount = float(parts[6])
            pct_chg = float(parts[8])
            change = float(parts[9])
            turnover_rate = float(parts[10]) if len(parts) > 10 and parts[10] else None

            pre_close = round(close_p - change, 4) if change != 0 else close_p

            record = {
                "trade_date": trade_date,
                "open": open_p,
                "close": close_p,
                "high": high_p,
                "low": low_p,
                "pre_close": pre_close,
                "change": change,
                "pct_chg": pct_chg,
                "vol": vol,
                "amount": amount,
            }
            if turnover_rate is not None:
                record["turnover_rate"] = turnover_rate

            records.append(record)
        except (ValueError, IndexError):
            continue

    return records, name


def main():
    parser = argparse.ArgumentParser(
        description="Fetch K-line data from East Money (东方财富) API"
    )
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 513180.SH")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("--freq", choices=["D", "W"], default="D", help="Frequency: D=daily, W=weekly")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], help="Adjustment type (auto-detected if omitted)")
    parser.add_argument("--lmt", type=int, default=250, help="Number of records to fetch (default: 250)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")

    args = parser.parse_args()

    # Check if market is supported
    secid = resolve_secid(args.ts_code)
    if secid is None:
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "data_source": "error",
                "error": (
                    f"东方财富不支持 {args.ts_code} 所属市场。"
                    "仅支持上交所(.SH)和深交所(.SZ)的A股及ETF。"
                    "港股请使用 Tushare 数据源。"
                ),
            },
            "data": [],
        }
        _output(result, args.output)
        return

    # Resolve parameters
    asset = args.asset or detect_asset(args.ts_code)
    adj = args.adj or detect_adj(args.ts_code)

    # Fetch data with host rotation
    records = None
    name = ""
    error_msg = None
    used_host = None

    for host in EM_API_HOSTS:
        try:
            records, name = fetch_eastmoney(secid, args.freq, args.lmt, host=host)
            used_host = host
            break
        except Exception as e:
            error_msg = str(e)
            import time
            time.sleep(1)

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
        _output(result, args.output)
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

    _output(result, args.output)


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
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock登录失败: {lg.error_msg}")

    try:
        # adjustflag: 2=前复权(qfq)
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount,pctChg,preclose",
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
                    "pre_close": float(row[8]) if len(row) > 8 and row[8] else None,
                    "pct_chg": float(row[7]) if row[7] else None,
                    "vol": float(row[5]) if row[5] else None,
                    "amount": float(row[6]) if row[6] else None,
                }
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


def _output(result, output_path=None):
    """Write JSON result to file or stdout."""
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Data written to {output_path}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()