#!/usr/bin/env python3
"""Shared East Money (东方财富) API utilities.

Consolidates headers, secid mapping, and node rotation logic
that was duplicated across fetch_kline_eastmoney.py and fetch_capital_flow.py.
"""

import time
import urllib.request
from datetime import datetime, timedelta

EM_API_HOSTS = [
    "push2his.eastmoney.com",
    "38.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
]

EM_PUSH2_HOSTS = [
    "push2.eastmoney.com",
    "38.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "push2test.eastmoney.com",    # CDN fallback - 部分网络环境下主节点限流时可用
    "60.push2.eastmoney.com",      # CDN fallback
    "95.push2.eastmoney.com",      # CDN fallback
]

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# Market prefix: .SH -> 1 (Shanghai), .SZ -> 0 (Shenzhen)
MARKET_PREFIX = {
    ".SH": "1",
    ".SZ": "0",
}

# ETF code -> corresponding index futures code
ETF_FUTURES_MAP = {
    # 沪深300
    "510300": "IF",   # 沪深300ETF华泰柏瑞
    "510310": "IF",   # 沪深300ETF易方达
    "159919": "IF",   # 沪深300ETF嘉实
    # 上证50
    "510050": "IH",   # 上证50ETF
    "510800": "IH",   # 上证50ETF易方达
    # 中证500
    "510500": "IC",   # 中证500ETF
    "159915": "IC",   # 创业板ETF (approximate, mid-cap proxy)
    # 中证1000
    "560010": "IM",   # 中证1000ETF
    "560011": "IM",   # 中证1000ETF易方达
    # 恒生科技
    "513180": "HTI_M",  # 恒生科技ETF华夏
    "513130": "HTI_M",  # 恒生科技ETF华泰柏瑞
    "513010": "HTI_M",  # 恒生科技ETF易方达
    "520920": "HTI_M",  # 恒生科技ETF天弘
    "159740": "HTI_M",  # 恒生科技ETF大成
    "159741": "HTI_M",  # 恒生科技ETF嘉实
    "159742": "HTI_M",  # 恒生科技ETF博时
    # 恒生指数
    "159920": "HSI_M",  # 恒生ETF
    "513010": "HTI_M",  # 恒生科技ETF易方达
}

# Futures code -> East Money secid
FUTURES_SECID_MAP = {
    "IF":     "8.IF",       # 沪深300股指期货主连
    "IH":     "8.IH",       # 上证50股指期货主连
    "IC":     "8.IC",       # 中证500股指期货主连
    "IM":     "8.IM",       # 中证1000股指期货主连
    "HTI_M":  "134.HTI_M",  # 恒生科技指数期货主连
    "HSI_M":  "134.HSI_M",  # 恒生指数期货主连
}

# Futures code -> underlying spot index secid (for basis calculation)
INDEX_SECID_MAP = {
    "IF":     "1.000300",   # 沪深300
    "IH":     "1.000016",   # 上证50
    "IC":     "0.399905",   # 中证500
    "IM":     "0.399852",   # 中证1000
    "HTI_M":  "100.HSTECH", # 恒生科技指数
    "HSI_M":  "100.HSI",   # 恒生指数
}


def get_futures_secid(etf_code):
    """Map ETF code to its corresponding index futures secid.

    Returns (futures_code, secid) tuple or (None, None) if no mapping exists.
    """
    futures_code = ETF_FUTURES_MAP.get(etf_code)
    if futures_code:
        return futures_code, FUTURES_SECID_MAP[futures_code]
    return None, None


def build_secid(ts_code):
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


def rotate_push2_host(fetch_fn, max_retries=3):
    """Try fetch_fn with each EM_PUSH2_HOSTS node until success.

    Same pattern as rotate_em_host but for EM_PUSH2_HOSTS (push2 endpoints
    for capital flow, sector data, etc.).
    """
    last_error = None
    for attempt in range(max_retries):
        host = EM_PUSH2_HOSTS[attempt % len(EM_PUSH2_HOSTS)]
        try:
            data = fetch_fn(host)
            return data, host
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1)
    raise RuntimeError(f"East Money push2全节点失败: {last_error}")


def rotate_em_host(fetch_fn, max_retries=3):
    """Try fetch_fn with each EM_API_HOSTS node until success.

    Args:
        fetch_fn: callable(host) -> data, raises on failure.
        max_retries: max attempts (cycles through hosts).

    Returns:
        (data, used_host) tuple on success.

    Raises:
        RuntimeError: if all hosts fail.
    """
    last_error = None
    for attempt in range(max_retries):
        host = EM_API_HOSTS[attempt % len(EM_API_HOSTS)]
        try:
            data = fetch_fn(host)
            return data, host
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1)
    raise RuntimeError(f"East Money全节点失败: {last_error}")


def fetch_url(url, headers=None, timeout=15):
    """Fetch URL via HTTP GET. Falls back to proxyless if proxy fails.

    Args:
        url: Full URL string.
        headers: Request headers dict (default EM_HEADERS).
        timeout: Request timeout in seconds.

    Returns:
        Response body as UTF-8 string.

    Raises:
        Exception from first attempt if both proxy and proxyless fail.
    """
    req = urllib.request.Request(url, headers=headers or EM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as first:
        try:
            proxyless = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(proxyless)
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            raise first


def build_em_kline_url(host, secid, freq='D', lmt=250, beg=None, end=None):
    """Build East Money K-line API URL.

    Args:
        host: API hostname (e.g. push2his.eastmoney.com).
        secid: Market-prefixed code (e.g. 1.600519, 90.BK0477).
        freq: 'D' for daily or 'W' for weekly.
        lmt: Max records to fetch.
        beg/end: Date range YYYYMMDD (auto-calculated if omitted).

    Returns:
        Full URL string.
    """
    f1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    f2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    klt = "102" if freq == "W" else "101"
    if not end:
        end = datetime.now().strftime("%Y%m%d")
    if not beg:
        delta = 730 if freq == "W" else 365
        beg = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
    return (f"https://{host}/api/qt/stock/kline/get"
            f"?secid={secid}&fields1={f1}&fields2={f2}"
            f"&klt={klt}&fqt=1&beg={beg}&end={end}&lmt={lmt}")


def parse_em_kline_line(line):
    """Parse single East Money K-line CSV string -> record dict.

    Fields: trade_date, open, close, high, low, pre_close, change,
            pct_chg, vol, amount, turnover_rate (if available).

    Returns None if line is malformed.
    """
    parts = line.split(",")
    if len(parts) < 11:
        return None
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
        return record
    except (ValueError, IndexError):
        return None
