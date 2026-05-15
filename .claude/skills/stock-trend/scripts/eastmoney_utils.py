#!/usr/bin/env python3
"""Shared East Money (东方财富) API utilities.

Consolidates headers, secid mapping, and node rotation logic
that was duplicated across fetch_kline_eastmoney.py and fetch_capital_flow.py.
"""

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

# Market prefix: .SH -> 1 (Shanghai), .SZ -> 0 (Shenzhen)
MARKET_PREFIX = {
    ".SH": "1",
    ".SZ": "0",
}


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
    import time
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