#!/usr/bin/env python3
"""Fetch DDX/DDY/DDZ and super-large order ratio from 同花顺 DDE ranking data.

Public functions:
    fetch_ddx_data(codes: list[str]) -> dict[str, dict]
    compute_ddx_score(ddx: dict) -> float
    compute_super_order_score(ddx: dict) -> float

Usage:
    ddx_map = fetch_ddx_data(["002415", "600519"])
    score = compute_ddx_score(ddx_map.get("002415", {}))
"""

import random
import re
import time
import urllib.request
from datetime import datetime

# 同花顺 DDE 排行页面 — shows DDX/DDY/DDZ for actively traded stocks
THS_DDX_URL = "http://data.10jqka.com.cn/financial/ddx/opendata/"

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://data.10jqka.com.cn/financial/ddx/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FETCH_TIMEOUT = 10  # seconds per request


def _fetch_page(url: str, timeout: int = FETCH_TIMEOUT,
                retries: int = 2) -> str | None:
    """Fetch HTML page with retry and exponential backoff."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=THS_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            # Sanity check: confirm page looks like DDE ranking data
            if "DDX" not in html and "DDE" not in html:
                return None
            return html
        except Exception:
            if attempt < retries:
                time.sleep(1.5 ** attempt + random.uniform(0.3, 1.0))
    return None


def _parse_ddx_table(html: str) -> dict[str, dict]:
    """Parse 同花顺 DDE ranking HTML table.

    Looks for a table containing DDX data rows with columns:
    代码, 名称, DDX, DDY, DDZ, 连续红柱, 超级资金占比.

    Returns dict mapping 6-digit code -> ddx data dict.
    """
    records = {}
    row_pattern = re.compile(
        r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL
    )
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
    code_pattern = re.compile(r'^\d{6}$')

    def _clean(v: str) -> str:
        return re.sub(r'<[^>]+>', '', v).strip().replace(',', '')

    for tr_match in row_pattern.finditer(html):
        cells = cell_pattern.findall(tr_match.group(1))
        # Need at least 8 cells for full DDX data
        if len(cells) < 8:
            continue
        code = _clean(cells[1])
        if not code_pattern.match(code):
            continue

        try:
            ddx_str = _clean(cells[3]) if len(cells) > 3 else "0"
            ddy_str = _clean(cells[4]) if len(cells) > 4 else "0"
            ddz_str = _clean(cells[5]) if len(cells) > 5 else "0"
            days_str = _clean(cells[6]) if len(cells) > 6 else "0"
            super_str = _clean(cells[7]) if len(cells) > 7 else "0%"

            ddx = float(ddx_str) if ddx_str and ddx_str != "--" else 0.0
            ddy = float(ddy_str) if ddy_str and ddy_str != "--" else 0.0
            ddz = float(ddz_str) if ddz_str and ddz_str != "--" else 0.0
            days_match = re.search(r'\d+', days_str)
            ddx_days = int(days_match.group()) if days_match else 0
            # super_order_ratio: "12.34%" or "12.34" -> 0.1234
            super_match = re.search(r'([\d.]+)', super_str)
            super_ratio = float(super_match.group(1)) / 100 if super_match else 0.0

            records[code] = {
                "ddx": round(ddx, 4),
                "ddy": round(ddy, 4),
                "ddz": round(ddz, 2),
                "ddx_days": ddx_days,
                "super_order_ratio": round(super_ratio, 4),
                "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            }
        except (ValueError, IndexError):
            continue

    return records


def fetch_ddx_data(codes: list[str]) -> dict[str, dict]:
    """Fetch DDX/DDY/DDZ/super_order_ratio for given stock codes from 同花顺.

    Since the ranking page shows top stocks by DDX, we fetch it and
    extract data only for codes in our candidate list.

    Args:
        codes: list of 6-digit A-share stock codes.

    Returns:
        Dict mapping code -> {ddx, ddx_days, ddy, ddz, super_order_ratio, fetch_time}.
        Only codes found in the DDE ranking page are included.
        Returns empty dict on any fetch/parse failure (graceful degradation).
    """
    if not codes:
        return {}
    html = _fetch_page(THS_DDX_URL)
    if html is None:
        return {}
    all_records = _parse_ddx_table(html)
    # Filter to only our target codes
    code_set = set(codes)
    return {k: v for k, v in all_records.items() if k in code_set}


def compute_ddx_score(ddx_data: dict) -> float:
    """Compute DDX score (0-100) for leader scoring weight.

    Anchors:
        ddx >= 0.5 + ddx_days >= 3  -> 100  (持续资金布局)
        ddx >= 0.5                   ->  90
        ddx >= 0.2                   ->  80
        0 < ddx < 0.2                ->  interpolated 50-80
        ddx <= 0                     ->  max(0, 50 + ddx * 100)

    Args:
        ddx_data: dict with ddx (float), ddx_days (int).

    Returns:
        Score 0-100.
    """
    ddx = ddx_data.get("ddx")
    if ddx is None:
        return 50.0  # neutral default

    ddx_days = ddx_data.get("ddx_days", 0) or 0

    if ddx >= 0.5 and ddx_days >= 3:
        return 100.0
    if ddx >= 0.5:
        return 90.0
    if ddx >= 0.2:
        return 80.0
    if ddx >= 0:
        # Linear interpolation: 0->50, 0.2->80
        return round(50.0 + (ddx / 0.2) * 30, 1)
    # ddx < 0
    return max(0.0, round(50.0 + ddx * 100, 1))


def compute_super_order_score(ddx_data: dict) -> float:
    """Compute super-large order ratio score (0-100).

    Anchors:
        ratio >= 15%  -> 100  (机构主导)
        ratio >= 8%   ->  80
        ratio >= 5%   ->  60
        ratio < 5%    ->  50  (散户特征)

    Args:
        ddx_data: dict with super_order_ratio (float, 0-1 or 0-100).

    Returns:
        Score 0-100.
    """
    ratio = ddx_data.get("super_order_ratio")
    if ratio is None:
        return 50.0  # neutral default

    # Normalize: if ratio is already 0-100 (percentage), convert to 0-1
    if ratio > 1:
        ratio = ratio / 100.0

    if ratio >= 0.15:
        return 100.0
    if ratio >= 0.08:
        return 80.0
    if ratio >= 0.05:
        return 60.0
    return 50.0
