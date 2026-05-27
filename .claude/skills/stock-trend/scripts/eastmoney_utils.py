#!/usr/bin/env python3
"""Shared East Money (东方财富) API utilities.

Consolidates headers, secid mapping, and node rotation logic
that was duplicated across fetch_kline_eastmoney.py and fetch_capital_flow.py.
"""

import time

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