#!/usr/bin/env python3
"""East Money sector data fetcher for /longtou skill.

Fetches A-share sector/concept rankings and constituent stock data.

Usage:
    python3 fetch_sector_data.py --rankings        # Get hot sector rankings
    python3 fetch_sector_data.py --stocks BKxxx     # Get sector constituents
    python3 fetch_sector_data.py --list             # List all sectors
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from eastmoney_utils import EM_HEADERS, EM_PUSH2_HOSTS, rotate_em_host

SCRIPT_DIR = Path(__file__).resolve().parent


# ──────────────────────── East Money API Helpers ────────────────────────


import random as _random


def _fetch_json(url: str, timeout: int = 15, retries: int = 3) -> dict:
    """Fetch JSON from East Money API with host rotation and no-proxy fallback."""
    last_error = None
    for attempt in range(retries + 1):
        host = EM_PUSH2_HOSTS[attempt % len(EM_PUSH2_HOSTS)]
        actual_url = url
        if host != "push2.eastmoney.com":
            actual_url = url.replace("https://push2.eastmoney.com",
                                     f"https://{host}", 1)
        try:
            req = urllib.request.Request(actual_url, headers=EM_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_error = e
            # Try without proxy on first attempt (corporate proxy often blocks)
            if attempt == 0:
                try:
                    proxyless = urllib.request.ProxyHandler({})
                    opener = urllib.request.build_opener(proxyless)
                    req = urllib.request.Request(actual_url, headers=EM_HEADERS)
                    with opener.open(req, timeout=timeout) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception:
                    pass  # fall through to retry + host rotation
            if attempt < retries:
                # Exponential backoff with random jitter: 1.5^attempt * (1-2s)
                sleep_sec = 1.5 ** attempt + _random.uniform(0.5, 1.5)
                time.sleep(sleep_sec)
    raise RuntimeError(f"东方财富API请求失败(重试{retries}次): {last_error}")


def _check_result(result: dict) -> dict:
    """Validate API response, return data dict or raise."""
    if not result or result.get("rc") != 0 or not result.get("data"):
        msg = result.get("message", "未知错误") if result else "无响应"
        raise RuntimeError(f"API返回错误: {msg}")
    return result["data"]


# ──────────────────────── Sector List & Rankings ────────────────────────


def get_sector_list() -> list[dict]:
    """Fetch all A-share sector/concept lists from East Money.

    Returns:
        List of {code, name, type} dicts where type is "industry" or "concept".
    """
    sectors = []
    today = datetime.now().strftime("%Y%m%d")
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"

    for idx, (stype, sname) in enumerate([("2", "industry"), ("3", "concept")]):
        # Stagger concurrent requests to avoid rate limiting
        if idx > 0:
            time.sleep(_random.uniform(0.3, 0.8))
        url = (
            f"{base_url}?fs=m:90+t:{stype}&fields=f12,f14"
            f"&pn=1&pz=500&po=0&np=1&fltt=2"
            f"&fid=f3&_={today}"
        )
        try:
            data = _fetch_json(url)
            items = _check_result(data).get("diff", [])
            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                if code and name:
                    sectors.append({"code": code, "name": name, "type": sname})
        except Exception as e:
            print(f"  Warning: 无法获取{sname}板块列表: {e}", file=sys.stderr)

    return sectors


def get_sector_rankings() -> dict:
    """Fetch sector rankings with composite scoring data.

    Returns:
        dict with meta and sectors list (code, name, change_pct, amount,
        up_count, down_count, total_count, type).
    """
    result = {
        "meta": {"fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S")},
        "sectors": [],
    }
    today = datetime.now().strftime("%Y%m%d")
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"

    for idx, (stype, sname) in enumerate([("2", "industry"), ("3", "concept")]):
        # Stagger concurrent requests to avoid rate limiting
        if idx > 0:
            time.sleep(_random.uniform(0.3, 0.8))
        # f2=最新价, f3=涨跌幅, f4=涨跌额, f8=换手率/成交额,
        # f12=代码, f14=名称, f20=总市值,
        # f104=涨家数, f105=跌家数, f62=主力净流入
        fields = "f2,f3,f4,f8,f12,f14,f20,f62,f104,f105,f168,f170,f171"
        url = (
            f"{base_url}?fs=m:90+t:{stype}&fields={fields}"
            f"&pn=1&pz=500&po=0&np=1&fltt=2"
            f"&fid=f3&_={today}"
        )
        try:
            data = _fetch_json(url)
            items = _check_result(data).get("diff", [])
            for item in items:
                total = (item.get("f104", 0) or 0) + (item.get("f105", 0) or 0)
                sector = {
                    "code": item.get("f12", ""),
                    "name": item.get("f14", ""),
                    "type": sname,
                    "change_pct": item.get("f3"),       # 涨跌幅%
                    "amount": item.get("f8"),            # 成交额
                    "up_count": item.get("f104", 0) or 0,   # 涨家数
                    "down_count": item.get("f105", 0) or 0, # 跌家数
                    "total_count": total,
                    "main_force_net": item.get("f62"),   # 主力净流入
                }
                if sector["code"]:
                    result["sectors"].append(sector)
        except Exception as e:
            print(f"  Warning: 无法获取{sname}板块排行: {e}", file=sys.stderr)

    result["meta"]["total_sectors"] = len(result["sectors"])
    return result


def compute_hot_score(sector: dict) -> float:
    """Compute hot sector composite score.

    Weight: change_pct(40%) + main_force_net(30%) + up/down ratio(30%)

    Args:
        sector: dict with change_pct, main_force_net, up_count, down_count.

    Returns:
        Score 0-100.
    """
    # Normalize change_pct: 0% → 40, 5% → 100, < -5% → 0
    change = sector.get("change_pct") or 0
    change_score = min(100, max(0, 40 + change * 12))

    # Normalize capital flow (scaled by 1e8 for readability)
    # 0 → 50, +5亿 → 100, -5亿 → 0
    net = (sector.get("main_force_net") or 0) / 1e8
    capital_score = min(100, max(0, 50 + net * 10))

    # Up/down ratio: 1:1 → 50, all up → 100, all down → 0
    up = sector.get("up_count", 0) or 1
    down = sector.get("down_count", 0) or 1
    ratio = up / max(1, (up + down))
    ratio_score = ratio * 100

    return round(change_score * 0.40 + capital_score * 0.30 + ratio_score * 0.30, 1)


def rank_hot_sectors(rankings: dict, top_n: int = 10,
                     min_stocks: int = 8) -> list[dict]:
    """Rank sectors by composite hot score.

    Filters out tiny sectors (fewer than min_stocks constituents),
    then min-max normalizes scores to 0-100 range.

    Args:
        rankings: output from get_sector_rankings().
        top_n: number of top sectors to return.
        min_stocks: minimum constituent stocks. 0 disables.

    Returns:
        Sorted list with score added to each sector dict.
    """
    sectors = rankings.get("sectors", [])

    if min_stocks > 0:
        before = len(sectors)
        sectors = [
            s for s in sectors
            if (s.get("up_count", 0) + s.get("down_count", 0)) >= min_stocks
        ]
        dropped = before - len(sectors)

    for s in sectors:
        s["hot_score"] = compute_hot_score(s)

    sectors.sort(key=lambda x: x.get("hot_score", 0), reverse=True)

    # Min-max normalize to 0-100 for consistent differentiation
    if sectors:
        scores = [s["hot_score"] for s in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for s in sectors:
                s["hot_score"] = round(
                    (s["hot_score"] - lo) / (hi - lo) * 100, 1
                )

    return sectors[:top_n]


# ──────────────────────── Sector Constituent Stocks ────────────────────────


def get_sector_stocks(sector_code: str, top_n: int = 50) -> list[dict]:
    """Fetch constituent stocks for a sector.

    Args:
        sector_code: e.g. "BKxxx".
        top_n: max stocks to return.

    Returns:
        List of {code, name, change_pct, amount, market_cap, pe}.
    """
    today = datetime.now().strftime("%Y%m%d")
    # b:BKxxx filters stocks belonging to this sector
    url = (
        f"https://push2.eastmoney.com/api/qt/clist/get"
        f"?fs=b:{sector_code}"
        f"&fields=f2,f3,f4,f8,f12,f14,f20,f21,f23,f37,f62,f168,f170,f171"
        f"&pn=1&pz={top_n}&po=0&np=1&fltt=2"
        f"&fid=f3&_={today}"
    )
    try:
        data = _fetch_json(url)
        items = _check_result(data).get("diff", [])
    except Exception as e:
        raise RuntimeError(f"获取板块{sector_code}成分股失败: {e}")

    stocks = []
    for item in items:
        stock = {
            "code": item.get("f12", ""),
            "name": item.get("f14", ""),
            "change_pct": item.get("f3"),
            "amount": item.get("f8"),
            "market_cap": item.get("f20"),   # 总市值
            "pe": item.get("f37"),           # 动态市盈率
        }
        if stock["code"]:
            stocks.append(stock)
    return stocks


def _parse_amount(val: Any) -> float:
    """Parse amount/成交额 value, return in yuan."""
    if val is None:
        return 0.0
    return float(val)


def filter_leaders(stocks: list[dict], top_n: int = 3) -> list[dict]:
    """Filter leader stocks (龙头) from sector constituents.

    Criteria: phase return(50%) + turnover/amount(30%) + limit-up signal(20%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of leaders to return.

    Returns:
        Top N leaders sorted by composite leader score.
    """
    scored = []
    for s in stocks:
        change = s.get("change_pct") or 0
        amount = _parse_amount(s.get("amount"))

        # Leader score: today's change proxies phase return + volume
        change_score = min(100, max(0, 50 + change * 5))    # 0%→50, +10%→100
        amount_score = min(100, _parse_amount(amount) / 1e7) # scaled
        leader_score = change_score * 0.50 + amount_score * 0.30

        s["leader_score"] = round(leader_score, 1)
        scored.append(s)

    scored.sort(key=lambda x: x.get("leader_score", 0), reverse=True)
    return scored[:top_n]


def filter_core_stocks(stocks: list[dict], top_n: int = 3) -> list[dict]:
    """Filter core stocks (中军) from sector constituents.

    Criteria: market cap(40%) + fundamentals/pe(40%) + stability(20%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of core stocks to return.

    Returns:
        Top N core stocks sorted by composite core score.
    """
    scored = []
    for s in stocks:
        mcap = s.get("market_cap") or 0
        pe = s.get("pe") or 0
        change = s.get("change_pct") or 0

        # Market cap: >1000亿 → 100, >500亿 → 80, >100亿 → 60
        cap_score = min(100, max(0, _parse_amount(mcap) / 1e8 * 5))
        # PE reasonableness: 10-30 ideal, very high or negative penalized
        if pe is not None and pe > 0:
            pe_score = max(0, 100 - abs(pe - 20) * 1.5)
        else:
            pe_score = 30  # negative PE = unclear
        # Stability: moderate change (0~5%) preferred over extreme
        stability_score = max(0, 100 - abs(change) * 10)

        core_score = cap_score * 0.40 + pe_score * 0.40 + stability_score * 0.20
        s["core_score"] = round(core_score, 1)
        scored.append(s)

    scored.sort(key=lambda x: x.get("core_score", 0), reverse=True)
    return scored[:top_n]


# ──────────────────────── Main CLI ────────────────────────


def main():
    parser = argparse.ArgumentParser(description="东方财富板块数据获取")
    parser.add_argument("--rankings", action="store_true", help="获取板块排行")
    parser.add_argument("--stocks", type=str, help="获取板块成分股, 参数: BK代码")
    parser.add_argument("--list", action="store_true", help="列出所有板块")
    parser.add_argument("--top", type=int, default=10, help="排行数量")
    parser.add_argument("--min-stocks", type=int, default=8, help="最小成分股数")
    parser.add_argument("-o", "--output", type=str, help="输出JSON文件")

    args = parser.parse_args()

    if args.rankings:
        rankings = get_sector_rankings()
        hot = rank_hot_sectors(rankings, args.top, min_stocks=args.min_stocks)
        output = {
            "meta": rankings["meta"],
            "hot_sectors": hot,
            "total_sectors": rankings["meta"]["total_sectors"],
        }
    elif args.stocks:
        stocks = get_sector_stocks(args.stocks)
        leaders = filter_leaders(stocks)
        cores = filter_core_stocks(stocks)
        output = {
            "sector_code": args.stocks,
            "total_stocks": len(stocks),
            "leaders": leaders,
            "core_stocks": cores,
        }
    elif args.list:
        sectors = get_sector_list()
        output = {"total": len(sectors), "sectors": sectors}
    else:
        parser.print_help()
        return

    out_str = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(out_str, encoding="utf-8")
        print(f"Output: {args.output}")
    else:
        print(out_str)


if __name__ == "__main__":
    main()
