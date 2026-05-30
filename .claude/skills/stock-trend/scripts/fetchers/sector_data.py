#!/usr/bin/env python3
"""East Money sector data fetcher for /longtou skill.

Fetches A-share sector/concept rankings and constituent stock data.

Usage:
    python3 fetch_sector_data.py --rankings        # Get hot sector rankings
    python3 fetch_sector_data.py --stocks BKxxx     # Get sector constituents
    python3 fetch_sector_data.py --list             # List all sectors
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.eastmoney_utils import EM_HEADERS, EM_PUSH2_HOSTS, rotate_em_host

SCRIPT_DIR = Path(__file__).resolve().parent.parent


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


def _up_ratio(sector: dict) -> float:
    """Calculate up/(up+down) ratio for a sector."""
    up = sector.get("up_count", 0) or 0
    down = sector.get("down_count", 0) or 0
    total = up + down
    return up / total if total > 0 else 0


def rank_hot_sectors(rankings: dict, top_n: int = 10,
                     min_stocks: int = 8,
                     min_up_ratio: float = 0.15) -> list[dict]:
    """Rank sectors by composite hot score.

    Filters:
      - Tiny sectors (fewer than min_stocks constituents)
      - Weak sectors (up_count / total < min_up_ratio)
      - Duplicate child-level sectors (same base name + identical stats)

    Args:
        rankings: output from get_sector_rankings().
        top_n: number of top sectors to return.
        min_stocks: minimum constituent stocks. 0 disables.
        min_up_ratio: minimum up/(up+down) ratio. 0 disables.

    Returns:
        Sorted list with score added to each sector dict.
    """
    import re

    sectors = rankings.get("sectors", [])

    if min_stocks > 0:
        sectors = [
            s for s in sectors
            if (s.get("up_count", 0) + s.get("down_count", 0)) >= min_stocks
        ]

    # Filter by up/down ratio — exclude boards that are overwhelmingly red
    if min_up_ratio > 0:
        sectors = [s for s in sectors if _up_ratio(s) >= min_up_ratio]

    # Dedup: sectors with same base name (stripping Ⅰ/Ⅱ/Ⅲ/Ⅳ) and identical
    # (up_count, down_count, change_pct) are parent/child duplicates; keep first.
    seen_signatures = set()
    deduped = []
    for s in sectors:
        base_name = re.sub(r'[ⅠⅡⅢⅣ\u2160-\u2163]$', '', s.get("name", ""))
        sig = (base_name,
               s.get("up_count", 0),
               s.get("down_count", 0),
               round(s.get("change_pct", 0) or 0, 2))
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            deduped.append(s)
    sectors = deduped

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


def _to_float(val: Any) -> float:
    """Parse amount/成交额 value, return in yuan."""
    if val is None:
        return 0.0
    return float(val)


def filter_leaders(stocks: list[dict], top_n: int = 3,
                   min_market_cap: float = 5e9,
                   max_market_cap: float = 5e11) -> list[dict]:
    """Filter leader stocks (龙头) from sector constituents.

    Criteria: phase return(50%) + turnover/amount(30%) + volume breakout(20%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of leaders to return.
        min_market_cap: minimum market cap in yuan (default 50亿).
        max_market_cap: maximum market cap in yuan (default 5000亿).

    Returns:
        Top N leaders sorted by composite leader score.
    """
    # Pre-filter: ST removal + market cap bounds
    filtered = []
    for s in stocks:
        name = s.get("name", "")
        if any(kw in name for kw in ("ST", "*ST", "退")):
            continue
        mcap = _to_float(s.get("market_cap"))
        if mcap < min_market_cap or mcap > max_market_cap:
            continue
        filtered.append(s)

    # Compute median amount for breakout detection
    amounts = [_to_float(s.get("amount")) for s in filtered if _to_float(s.get("amount")) > 0]
    median_amount = sorted(amounts)[len(amounts) // 2] if amounts else 1e8

    scored = []
    for s in filtered:
        change = s.get("change_pct") or 0
        amount = _to_float(s.get("amount"))

        # Leader score: today's change proxies phase return + volume
        change_score = min(100, max(0, 50 + change * 5))    # 0%→50, +10%→100
        amount_score = min(100, amount / 1e7)                # scaled

        # Volume breakout bonus: change > 3% AND amount > 1.5x sector median
        breakout_bonus = 0
        if change > 3.0 and median_amount > 0 and amount > median_amount * 1.5:
            breakout_bonus = min(100, max(0, 50 + (amount / median_amount - 1) * 50))

        leader_score = change_score * 0.50 + amount_score * 0.30 + breakout_bonus * 0.20

        s["leader_score"] = round(leader_score, 1)
        s["_volume_breakout"] = breakout_bonus > 0
        scored.append(s)

    scored.sort(key=lambda x: x.get("leader_score", 0), reverse=True)
    return scored[:top_n]


def filter_core_stocks(stocks: list[dict], top_n: int = 3,
                       min_market_cap: float = 5e9,
                       max_market_cap: float = 5e11) -> list[dict]:
    """Filter core stocks (中军) from sector constituents.

    Criteria: market cap(35%) + fundamentals/pe(35%) + stability(15%) + laggard bonus(15%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of core stocks to return.
        min_market_cap: minimum market cap in yuan (default 50亿).
        max_market_cap: maximum market cap in yuan (default 5000亿).

    Returns:
        Top N core stocks sorted by composite core score.
    """
    # Pre-filter: ST removal + market cap bounds
    filtered = []
    for s in stocks:
        name = s.get("name", "")
        if any(kw in name for kw in ("ST", "*ST", "退")):
            continue
        mcap = _to_float(s.get("market_cap"))
        if mcap < min_market_cap or mcap > max_market_cap:
            continue
        filtered.append(s)

    # Precompute change percentiles for laggard detection
    changes = sorted([s.get("change_pct") or 0 for s in filtered])

    def _pct_rank(val, sorted_vals):
        """Percentile rank of val in sorted_vals (0-100)."""
        if not sorted_vals:
            return 50
        n = len(sorted_vals)
        rank = sum(1 for v in sorted_vals if v < val)
        return rank / n * 100

    scored = []
    for s in filtered:
        mcap = s.get("market_cap") or 0
        pe = s.get("pe") or 0
        change = s.get("change_pct") or 0

        # Market cap: >1000亿 → 100, >500亿 → 80, >100亿 → 60
        cap_score = min(100, max(0, _to_float(mcap) / 1e8 * 5))
        # PE reasonableness: 10-30 ideal, very high or negative penalized
        if pe is not None and pe > 0:
            pe_score = max(0, 100 - abs(pe - 20) * 1.5)
        else:
            pe_score = 30  # negative PE = unclear
        # Stability: moderate change (0~5%) preferred over extreme
        stability_score = max(0, 100 - abs(change) * 10)

        # Laggard bonus: underperformed within sector but has reasonable PE
        pct_rank = _pct_rank(change, changes)
        laggard_bonus = 0
        if pct_rank < 50 and pe and 0 < pe < 30:
            laggard_bonus = min(20, (50 - pct_rank) * 0.4)

        core_score = (cap_score * 0.35 + pe_score * 0.35
                      + stability_score * 0.15 + laggard_bonus * 0.15)
        s["core_score"] = round(core_score, 1)
        s["_is_laggard"] = laggard_bonus > 0
        scored.append(s)

    scored.sort(key=lambda x: x.get("core_score", 0), reverse=True)
    return scored[:top_n]


def rescore_leaders_with_ddx(leaders: list[dict],
                              ddx_data: dict[str, dict]) -> list[dict]:
    """Re-score leader stocks with DDX data enhancement.

    Uses new formula: change*30% + amount*20% + ddx_score*30% + super_order_score*20%.
    Stocks without DDX data keep their existing leader_score.

    Args:
        leaders: list of stock dicts with leader_score.
        ddx_data: dict mapping code -> {ddx, ddx_days, super_order_ratio, ...}.

    Returns:
        Re-sorted leaders list with updated leader_score.
    """
    if not leaders:
        return []

    from fetchers.ddx import compute_ddx_score, compute_super_order_score

    for s in leaders:
        ddx = ddx_data.get(s["code"])
        if ddx:
            change_score = min(100, max(0, 50 + (s.get("change_pct") or 0) * 5))
            amount_score = min(100, _to_float(s.get("amount")) / 1e7)
            ddx_s = compute_ddx_score(ddx)
            super_s = compute_super_order_score(ddx)

            s["leader_score"] = round(
                change_score * 0.30 + amount_score * 0.20
                + ddx_s * 0.30 + super_s * 0.20,
                1,
            )
            s["ddx_data"] = ddx

    leaders.sort(key=lambda x: x.get("leader_score", 0), reverse=True)
    return leaders


# ──────────────────────── Rankings Cache ────────────────────────

CACHE_DIR = SCRIPT_DIR.parent.parent.parent.parent / ".cache" / "stock-trend"
CACHE_FILE = CACHE_DIR / "sector_rankings_cache.json"
MAX_CACHE_AGE_HOURS = 96  # 4 days, covers long weekends

# A-share market hours: 9:30-11:30, 13:00-15:00 CST
_MARKET_OPEN_MINUTES = (9 * 60 + 30, 15 * 60)  # 570, 900


def _is_outside_market_hours(dt: datetime) -> bool:
    """Check if datetime is outside A-share trading hours.

    Cache written during market hours (9:30-15:00) may contain
    incomplete mid-session data — reject for weekend fallback.
    """
    t = dt.hour * 60 + dt.minute
    return t < _MARKET_OPEN_MINUTES[0] or t >= _MARKET_OPEN_MINUTES[1]


def save_rankings_cache(rankings: dict, hot_sectors: Optional[list] = None) -> None:
    """Save sector rankings snapshot for non-trading-day fallback.

    Also saves pre-computed hot_sectors if provided, so non-trading day
    can return yesterday's actual hot sector rankings rather than
    regenerating from stale data.

    Does NOT overwrite existing cache if today's data has no real sector
    activity (non-trading day). This preserves the last trading day's cache.
    """
    sectors = rankings.get("sectors", [])
    active = sum(
        1 for s in sectors
        if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
    )
    # Never cache zero-activity data (non-trading day).
    # Without this guard, first run on a weekend creates a zero cache
    # that drowns the last trading day's data.
    if active == 0:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": datetime.now().isoformat(),
        "rankings": rankings,
    }
    if hot_sectors:
        payload["hot_sectors"] = hot_sectors
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_rankings_cache() -> Optional[dict]:
    """Load cached sector rankings if fresh and has any sector data.

    Returns the rankings dict or None if expired / corrupted.
    Accepts caches from any time of day (including mid-session).
    Even a sparse intraday cache is more useful than multi-week stale
    BK K-line fallback.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(payload["cached_at"])
        age = datetime.now() - cached_at
        if age.total_seconds() > MAX_CACHE_AGE_HOURS * 3600:
            return None
        # Must have at least some sectors (not an empty cache)
        rankings = payload.get("rankings", {})
        sectors = rankings.get("sectors", [])
        if len(sectors) < 5:
            return None
        return rankings
    except Exception:
        return None


def load_rankings_cache_full() -> Optional[dict]:
    """Load full cached payload including hot_sectors if present.

    Returns the raw payload dict, or None if expired / corrupted.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(payload["cached_at"])
        age = datetime.now() - cached_at
        if age.total_seconds() > MAX_CACHE_AGE_HOURS * 3600:
            return None
        return payload
    except Exception:
        return None


# ──────────────────────── Snapshot History ────────────────────────
# Daily sector snapshot history replaces BK K-line dependence for
# market-theme persistence analysis. Every successful realtime fetch
# appends a snapshot.  market_theme.py reads last N snapshot days
# to compute trend persistence.

SNAPSHOT_FILE = CACHE_DIR / "sector_snapshot_history.json"
SNAPSHOT_MAX_DAYS = 30  # auto-prune older snapshots


def _hot_ranked_sectors(rankings: dict, top_n: int = 30) -> list[dict]:
    """Extract top N sectors sorted by composite hot score.

    Same filtering as rank_hot_sectors but lighter — always applies
    min_stocks=4, min_up_ratio=0.05 to avoid degenerate data.
    Used for snapshot history (archival quality, not display quality).
    """
    sectors = rankings.get("sectors", [])
    # Filter tiny / dead sectors
    sectors = [
        s for s in sectors
        if (s.get("up_count", 0) or 0) + (s.get("down_count", 0) or 0) >= 4
        and _up_ratio(s) >= 0.05
    ]
    for s in sectors:
        s["hot_score"] = compute_hot_score(s)
    sectors.sort(key=lambda x: x.get("hot_score", 0), reverse=True)
    # Min-max normalize
    if sectors:
        scores = [s["hot_score"] for s in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for s in sectors:
                s["hot_score"] = round(
                    (s["hot_score"] - lo) / (hi - lo) * 100, 1
                )
    return sectors[:top_n]


def append_daily_snapshot(rankings: dict, override_date: str = "") -> None:
    """Append today's sector snapshot to history file.

    Called after successful realtime ranking fetch (NOT on non-trading
    days).  Stores compact sector summaries keyed by date for fast
    persistence loading.

    Args:
        rankings: output from get_sector_rankings().
        override_date: force date string YYYY-MM-DD (for testing).
    """
    sectors = rankings.get("sectors", [])
    active = sum(
        1 for s in sectors
        if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
    )
    if active == 0:
        return  # non-trading day, skip

    date_key = override_date or datetime.now().strftime("%Y-%m-%d")

    # Compute top 30 summaries
    top = _hot_ranked_sectors(rankings, top_n=30)
    summary = []
    for s in top:
        up = s.get("up_count", 0) or 0
        down = s.get("down_count", 0) or 0
        total = up + down
        summary.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "hot_score": s.get("hot_score", 0),
            "change_pct": s.get("change_pct"),
            "up_ratio": round(up / total, 3) if total > 0 else 0,
            "rank": len(summary) + 1,
        })

    # Load existing history, update, save
    history = {}
    if SNAPSHOT_FILE.exists():
        try:
            history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            pass
    history[date_key] = summary

    # Auto-prune: keep last SNAPSHOT_MAX_DAYS
    dates = sorted(history.keys())
    if len(dates) > SNAPSHOT_MAX_DAYS:
        keep = set(dates[-SNAPSHOT_MAX_DAYS:])
        history = {k: v for k, v in history.items() if k in keep}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")


def load_snapshot_history(days: int = 10) -> dict[str, list[dict]]:
    """Load snapshot history for the last N trading days.

    Returns dict mapping date YYYY-MM-DD -> list of sector summaries.
    Each summary has: code, name, hot_score, change_pct, up_ratio, rank.
    Returns empty dict if no history available.
    """
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return {}
    dates = sorted(history.keys())
    recent = dates[-days:] if len(dates) > days else dates
    return {d: history[d] for d in recent}


def get_last_trading_day() -> tuple[Optional[str], str]:
    """Determine last trading day from best available source.

    Three-tier lookup:
      1. Snapshot history latest date (exact, set by append_daily_snapshot)
      2. Rankings cache cached_at (exact, set by save_rankings_cache)
      3. Calendar fallback: today - 1 weekday (approximate)

    Returns:
        (date_str YYYY-MM-DD or None, source_label)
        source_label: "snapshot" | "cache" | "calendar" | ""
    """
    # Tier 1: snapshot history latest date
    if SNAPSHOT_FILE.exists():
        try:
            history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
            if history and isinstance(history, dict):
                dates = sorted(history.keys())
                if dates:
                    return dates[-1], "snapshot"
        except Exception:
            pass

    # Tier 2: rankings cache timestamp
    if CACHE_FILE.exists():
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_at_str = payload.get("cached_at", "")
            if cached_at_str:
                cached_at = datetime.fromisoformat(cached_at_str)
                age = datetime.now() - cached_at
                if age.total_seconds() <= MAX_CACHE_AGE_HOURS * 3600:
                    return cached_at.strftime("%Y-%m-%d"), "cache"
        except Exception:
            pass

    # Tier 3: calendar fallback (weekend regression)
    today = datetime.now()
    if today.weekday() == 5:   # Saturday → Friday
        prev = today.replace(hour=0, minute=0, second=0, microsecond=0)
        prev = prev.replace(day=prev.day - 1)
        return prev.strftime("%Y-%m-%d"), "calendar"
    elif today.weekday() == 6:  # Sunday → Friday
        prev = today.replace(hour=0, minute=0, second=0, microsecond=0)
        prev = prev.replace(day=prev.day - 2)
        return prev.strftime("%Y-%m-%d"), "calendar"
    # Weekday but might be holiday — can't detect without calendar API
    return None, ""


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
