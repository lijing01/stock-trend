#!/usr/bin/env python3
"""Fetch 龙虎榜 (Dragon & Tiger Board) data from 同花顺.

Public functions:
    fetch_longhubang_data(codes: list[str]) -> dict[str, dict]
"""

import random
import re
import time
import urllib.request

THS_LHB_URL = "http://data.10jqka.com.cn/financial/longhubang/"

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://data.10jqka.com.cn/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FETCH_TIMEOUT = 15


def _fetch_page(url: str, timeout: int = FETCH_TIMEOUT,
                retries: int = 2) -> str | None:
    """Fetch HTML page with retry and exponential backoff."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=THS_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            if "龙虎榜" not in html:
                return None
            return html
        except Exception:
            if attempt < retries:
                time.sleep(1.5 ** attempt + random.uniform(0.3, 1.0))
    return None


def _parse_amount(val: str) -> float:
    """Parse amount string to yuan. Handles 万/亿 suffixes."""
    if not val:
        return 0.0
    val = val.strip().replace(',', '')
    if '亿' in val:
        m = re.search(r'([\d.]+)', val)
        return float(m.group(1)) * 1e8 if m else 0.0
    if '万' in val:
        m = re.search(r'([\d.]+)', val)
        return float(m.group(1)) * 1e4 if m else 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0


def _parse_lhb_table(html: str) -> dict[str, dict]:
    """Parse 同花顺 龙虎榜 HTML table.

    Expected columns: 日期, 代码, 名称, 买入额, 卖出额, 净额, 详情.
    """
    from datetime import datetime

    records = {}
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)

    def _clean(v: str) -> str:
        return re.sub(r'<[^>]+>', '', v).strip().replace(',', '')

    now = datetime.now().strftime("%Y%m%d-%H%M%S")

    for tr_match in row_pattern.finditer(html):
        cells = cell_pattern.findall(tr_match.group(1))
        # Need 7 cells: 日期, 代码, 名称, 买入额, 卖出额, 净额, 详情
        if len(cells) < 7:
            continue
        code_html = cells[1]
        code_match = re.search(r'(\d{6})', code_html)
        if not code_match:
            continue
        code = code_match.group(1)

        # 净额 at index 5 (0:日期 1:代码 2:名称 3:买入额 4:卖出额 5:净额 6:详情)
        net_raw = _clean(cells[5])
        net_total = _parse_amount(net_raw)

        detail_html = cells[6]
        detail_text = _clean(detail_html)

        has_inst_buy = "机构买入" in detail_html or "机构专用" in detail_html
        has_inst_sell = "机构卖出" in detail_html or "机构专用" in detail_html
        has_youzi = any(kw in detail_text for kw in ["游资", "拉萨", "宁波"])
        retail = any(kw in detail_text for kw in ["散户", "接盘"])

        records[code] = {
            "is_on_board": True,
            "net_buy_total": net_total,
            "buy_seats": [],   # reserved for future seat-level extraction
            "sell_seats": [],  # reserved for future seat-level extraction
            "has_institution_buy": has_inst_buy,
            "has_institution_sell": has_inst_sell,
            "has_floating_capital": has_youzi,
            "floating_capital_net_buy": has_youzi and net_total > 0,
            "retail_dominated": retail,
            "risk_level": "low",
            "fetch_time": now,
        }

    return records


def _classify_risk_level(lhb_data: dict) -> str:
    """Classify risk level based on 龙虎榜 seat composition."""
    if not lhb_data.get("is_on_board"):
        return "low"
    if lhb_data.get("retail_dominated"):
        return "high"
    has_inst_buy = lhb_data.get("has_institution_buy", False)
    has_inst_sell = lhb_data.get("has_institution_sell", False)
    youzi = lhb_data.get("has_floating_capital", False)

    if has_inst_sell and not has_inst_buy:
        return "high"
    if has_inst_buy and not has_inst_sell and not youzi:
        return "low"
    if has_inst_buy and has_inst_sell:
        return "medium"
    if has_inst_buy and youzi:
        return "medium"
    if youzi:
        return "medium"
    return "low"


def fetch_longhubang_data(codes: list[str]) -> dict[str, dict]:
    """Fetch 龙虎榜 data for given stock codes.

    Args:
        codes: list of 6-digit A-share stock codes.

    Returns:
        Dict mapping code -> {is_on_board, net_buy_total, ...}.
        Only codes found on the 龙虎榜 page are included.
        Returns empty dict on any fetch/parse failure.
    """
    if not codes:
        return {}
    html = _fetch_page(THS_LHB_URL)
    if html is None:
        return {}
    records = _parse_lhb_table(html)
    code_set = set(codes)
    result = {}
    for code, data in records.items():
        if code in code_set:
            data["risk_level"] = _classify_risk_level(data)
            result[code] = data
    return result
