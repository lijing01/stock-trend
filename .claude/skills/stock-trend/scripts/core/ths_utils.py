"""同花顺 (10jqka) 通用请求工具。

提供 browser-like headers, retry + backoff, HTML parser helpers.
被 ddx.py, longhubang.py, zt_replay.py 共用。
"""

import random
import re
import time
import urllib.request
from typing import Optional

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

FETCH_TIMEOUT = 15
MAX_RETRIES = 2


def fetch_page(url: str,
               timeout: int = FETCH_TIMEOUT,
               retries: int = MAX_RETRIES,
               referer: Optional[str] = None) -> Optional[str]:
    """Fetch HTML page with browser-like headers + retry + backoff.

    Args:
        url: full URL to fetch.
        timeout: per-request timeout in seconds.
        retries: number of retries on failure.
        referer: custom Referer header (defaults to url's origin).

    Returns:
        HTML string on success, None on all failures.
    """
    headers = dict(THS_HEADERS)
    if referer:
        headers["Referer"] = referer

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            # Sanity: page must contain Chinese characters (anti-crawl guard)
            if not html or len(html) < 200:
                return None
            return html
        except Exception:
            if attempt < retries:
                sleep_sec = 1.5 ** attempt + random.uniform(0.3, 1.0)
                time.sleep(sleep_sec)
    return None


def fetch_page_with_retry(url: str, **kwargs) -> Optional[str]:
    """Alias — backwards compat."""
    return fetch_page(url, **kwargs)


# ──────────────── HTML Table Parsing Helpers ────────────────


def extract_table_rows(html: str) -> list[list[str]]:
    """Extract all <tr> rows from an HTML table as list of cell strings.

    Strips HTML tags inside each cell. Returns rows sorted by appearance.
    """
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)

    def _clean(v: str) -> str:
        return re.sub(r'<[^>]+>', '', v).strip().replace('\xa0', ' ')

    rows = []
    for tr_match in row_pattern.finditer(html):
        cells = cell_pattern.findall(tr_match.group(1))
        if cells:
            rows.append([_clean(c) for c in cells])
    return rows


def parse_amount(val: str) -> float:
    """Parse amount string to yuan. Handles 万/亿 suffixes."""
    if not val:
        return 0.0
    val = val.strip().replace(',', '').replace(' ', '')
    # Handle ranges like "1.2-2.3亿" → take mid point
    range_match = re.match(r'([\d.]+)\s*[-~]\s*([\d.]+)(亿|万)', val)
    if range_match:
        lo, hi, unit = float(range_match.group(1)), float(range_match.group(2)), range_match.group(3)
        mid = (lo + hi) / 2
        multiplier = 1e8 if unit == '亿' else 1e4 if unit == '万' else 1
        return mid * multiplier
    m = re.match(r'([\d.]+)(亿|万)', val)
    if m:
        num = float(m.group(1))
        return num * 1e8 if m.group(2) == '亿' else num * 1e4
    try:
        return float(val)
    except ValueError:
        return 0.0
