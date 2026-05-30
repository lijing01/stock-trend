#!/usr/bin/env python3
"""A股个股筛选器 — Scan A-stock constituents of hot sectors after market_theme/market_leader.

Three-phase architecture:
  Phase 1: Gather + hard-filter A-stocks from hot sector constituents
  Phase 2: Quick multi-dimension scoring (momentum/volume/capital/fundamental/sector)
  Phase 3: Rank, assign stars, output JSON

Usage:
    python3 stock_scanner.py --sectors BK0477,BK0897 --top 10
    python3 stock_scanner.py --from-leader /path/to/leader_output.json --top 10
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.cache_utils import run_script, CACHE_DIR
from core.eastmoney_utils import ma, rsi, macd_direction, volume_ma

SCRIPT_DIR = Path(__file__).resolve().parent.parent

# ──────────────────────── Helpers ────────────────────────


def _read_json(path):
    """Read JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_float(val, default=0.0):
    """Parse float safely."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _is_a_share(code):
    """Return True if code looks like an A-share (not ETF, not HK)."""
    if not code or not isinstance(code, str):
        return False
    if len(code) == 6 and code.isdigit():
        if code.startswith(("6", "0", "3")):
            return code[:2] not in ("50", "51", "55", "56", "58", "15", "16", "18")
        return False
    return False


def _is_st(name):
    """Return True if stock name indicates ST / delisting risk."""
    if not name:
        return False
    return any(kw in str(name) for kw in ("ST", "*ST", "退"))


def _compute_pct_rank(val, sorted_vals):
    """Percentile rank of val in sorted_vals (0-100)."""
    if not sorted_vals:
        return 50
    n = len(sorted_vals)
    rank = sum(1 for v in sorted_vals if v < val)
    return rank / n * 100


def _resolve_ts_code(code):
    """Resolve 6-digit code to ts_code suffix."""
    if len(code) != 6 or not code.isdigit():
        return code
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _piecewise_linear(val, anchors):
    """Piecewise linear map from anchors [(x0,y0), (x1,y1), ...]."""
    if not anchors:
        return 0
    if val <= anchors[0][0]:
        return anchors[0][1]
    if val >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= val <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (val - x0) / (x1 - x0)
    return 0


# ──────────────────────── Phase 1: Gather + Filter ────────────────────────


def gather_candidates(sector_codes: list[str], top_n_per_sector: int = 30,
                      max_workers: int = 4) -> dict:
    """Phase 1: Gather constituent A-stocks from hot sectors, dedup, hard filter.

    Returns dict with:
        stocks: list of candidate stock dicts
        excluded: list of excluded stocks with reasons
        sector_map: {code: {name, hot_score}} for later reference
    """
    # Import sector_data inline to avoid circular imports
    from fetchers.sector_data import get_sector_rankings, rank_hot_sectors, get_sector_stocks

    # Get sector rankings to enrich with hot scores
    sector_scores = {}
    try:
        rankings = get_sector_rankings()
        hot_sectors = rank_hot_sectors(rankings, top_n=len(rankings.get("sectors", [])))
        for s in hot_sectors:
            sector_scores[s["code"]] = s.get("hot_score", 50)
    except Exception:
        pass

    # Parallel fetch constituent stocks per sector
    sector_map = {}
    all_stocks = []  # list of (stock_dict, sector_info)

    def _fetch_one_sector(code):
        try:
            stocks = get_sector_stocks(code, top_n=top_n_per_sector)
            hot_score = sector_scores.get(code, 50)
            # Try to get sector name from the first stock or rankings
            name = code  # fallback
            for s in hot_sectors if 'hot_sectors' in dir() else []:
                if s.get("code") == code:
                    name = s.get("name", code)
                    break
            return {"code": code, "name": name, "hot_score": hot_score,
                    "stocks": stocks, "error": None}
        except Exception as e:
            return {"code": code, "name": code, "hot_score": 50,
                    "stocks": [], "error": str(e)}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one_sector, c): c for c in sector_codes}
        for fut in as_completed(futures):
            result = fut.result()
            sector_code = result["code"]
            sector_map[sector_code] = {
                "name": result["name"],
                "hot_score": result["hot_score"],
            }
            for s in result["stocks"]:
                all_stocks.append((s, sector_code))

    # Dedup + filter
    seen_codes = set()
    candidates = []
    excluded = []

    for s, sector_code in all_stocks:
        code = s.get("code", "")
        name = s.get("name", "")

        if code in seen_codes:
            continue
        seen_codes.add(code)

        # A-share filter
        if not _is_a_share(code):
            excluded.append({"code": code, "name": name, "reason": "非A股(ETF/港股)"})
            continue

        # ST filter
        if _is_st(name):
            excluded.append({"code": code, "name": name, "reason": "ST/退市风险"})
            continue

        # Market cap filter: 50-500亿
        mcap = _safe_float(s.get("market_cap"))
        if mcap < 5e9:
            excluded.append({"code": code, "name": name, "reason": "市值过小(<50亿)"})
            continue
        if mcap > 5e11:
            excluded.append({"code": code, "name": name, "reason": "市值过大(>5000亿)"})
            continue

        candidates.append({
            "code": code,
            "ts_code": _resolve_ts_code(code),
            "name": name,
            "sector_code": sector_code,
            "sector_name": sector_map.get(sector_code, {}).get("name", sector_code),
            "sector_hot_score": sector_map.get(sector_code, {}).get("hot_score", 50),
            "change_pct": _safe_float(s.get("change_pct")),
            "amount": _safe_float(s.get("amount")),
            "market_cap": mcap,
            "pe": _safe_float(s.get("pe")),
        })

    return {
        "candidates": candidates,
        "excluded": excluded,
        "sector_map": sector_map,
    }


# ──────────────────────── Phase 2: Scoring ────────────────────────


def _fetch_kline(ts_code):
    """Fetch 60-day K-line for a stock via eastmoney CLI."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "kline.json"

    # Check cache first
    cached = _read_json(str(cache_path))
    if cached and cached.get("data") and len(cached["data"]) >= 30:
        return cached

    # Fetch via subprocess
    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/kline_eastmoney.py"),
        ts_code, "--asset", "E", "--freq", "D",
        "-o", str(cache_path),
    ]
    result = run_script(cmd, label=f"kline_{ts_code}")
    if result["success"]:
        return _read_json(str(cache_path))
    return None


def _fetch_capital_flow(ts_code):
    """Fetch capital flow for a stock via CLI."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "capital_flow.json"

    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/capital_flow.py"),
        ts_code, "--asset", "E", "-o", str(cache_path),
    ]
    result = run_script(cmd, label=f"cap_{ts_code}")
    if result["success"]:
        return _read_json(str(cache_path))
    return None


def _fetch_fundamental(ts_code):
    """Fetch fundamental data, prefer cache."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "fundamental.json"

    cached = _read_json(str(cache_path))
    if cached and cached.get("summary"):
        return cached

    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/fundamental.py"),
        ts_code, "--asset", "E", "-o", str(cache_path),
    ]
    result = run_script(cmd, label=f"fund_{ts_code}")
    if result["success"]:
        return _read_json(str(cache_path))
    return None


def _compute_close_prices(kline_data):
    """Extract close price series from kline data."""
    records = kline_data.get("data", []) if kline_data else []
    return [r["close"] for r in records if r.get("close") is not None]


def score_momentum(candidate, kline_data):
    """Score momentum dimension (0-100).

    Components: MA alignment + RSI position + MACD direction + 20d return.
    """
    if not kline_data:
        return 50.0

    records = kline_data.get("data", [])
    if len(records) < 20:
        return 50.0

    closes = _compute_close_prices(kline_data)
    if len(closes) < 20:
        return 50.0

    score = 50.0

    # MA alignment (contributes ±25)
    ma5_val = ma(closes, 5)
    ma20_val = ma(closes, 20)
    ma60_val = ma(closes, 60) if len(closes) >= 60 else ma20_val
    if ma5_val and ma20_val and ma60_val:
        if ma5_val > ma20_val > ma60_val:
            score += 25
        elif ma5_val > ma20_val:
            score += 10
        elif ma5_val < ma20_val < ma60_val:
            score -= 25
        elif ma5_val < ma20_val:
            score -= 10

    # RSI position (contributes ±15)
    latest_rsi = rsi(closes, 14)
    if latest_rsi is not None:
        if 40 <= latest_rsi <= 70:
            score += 15
        elif 30 <= latest_rsi < 40:
            score += 5
        elif 70 < latest_rsi <= 80:
            score += 0
        elif latest_rsi > 80:
            score -= 10
        elif latest_rsi < 30:
            score -= 10

    # MACD direction (contributes ±10)
    macd_dir = macd_direction(closes)
    if macd_dir == "golden_cross":
        score += 10
    elif macd_dir == "death_cross":
        score -= 10

    # 20-day return (contributes ±10)
    if len(closes) >= 20:
        ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
        if ret_20d > 10:
            score += 10
        elif ret_20d > 5:
            score += 5
        elif ret_20d < -10:
            score -= 10
        elif ret_20d < -5:
            score -= 5

    return max(0.0, min(100.0, score))


def score_volume_price(candidate, kline_data):
    """Score volume-price dimension (0-100).

    Components: volume ratio + volume-price coordination + divergence detection.
    """
    if not kline_data:
        return 50.0

    records = kline_data.get("data", [])
    if len(records) < 20:
        return 50.0

    score = 50.0

    # Volume ratio (vol_ma5 / vol_ma20)
    ma5_vol = volume_ma(records, period=5)
    ma20_vol = volume_ma(records, period=20)
    if ma5_vol and ma20_vol and ma20_vol > 0:
        vol_ratio = ma5_vol / ma20_vol
        if vol_ratio > 1.5:
            score += 20
        elif vol_ratio > 1.2:
            score += 10
        elif vol_ratio < 0.5:
            score -= 10

    # Volume-price coordination (latest day)
    if len(records) >= 1:
        latest = records[-1]
        close = latest.get("close", 0)
        pre_close = latest.get("pre_close", close)
        pct_chg = (close - pre_close) / pre_close * 100 if pre_close else 0
        vol = latest.get("vol", 0)

        # 5-day average vol
        recent_vols = [r.get("vol", 0) for r in records[-5:]]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else vol

        vol_ratio_day = vol / avg_vol if avg_vol > 0 else 1.0

        if pct_chg > 1 and vol_ratio_day > 1.3:
            score += 15  # 放量上涨
        elif pct_chg > 0 and vol_ratio_day < 0.7:
            score -= 5   # 缩量上涨 (weak)
        elif pct_chg < -1 and vol_ratio_day > 1.5:
            score -= 15  # 放量下跌
        elif pct_chg < 0 and vol_ratio_day < 0.7:
            score += 5   # 缩量下跌 (selling exhausted)

    # Volume-price divergence over last 5 days
    if len(records) >= 5:
        recent = records[-5:]
        up_days_vol = sum(1 for r in recent if r.get("close", 0) > r.get("open", 0)
                          and r.get("vol", 0) > avg_vol)
        if up_days_vol >= 4:
            score += 5  # consistent volume-supported rise

    return max(0.0, min(100.0, score))


def score_capital(candidate, capital_data):
    """Score capital flow dimension (0-100).

    Components: main force net direction + northbound change + flow streak.
    """
    if not capital_data:
        return 50.0

    score = 50.0

    # Main force net inflow direction (5-day sum)
    records = capital_data.get("data", [])
    if records:
        total_main = sum(_safe_float(r.get("main_net_inflow")) for r in records[:5])
        if total_main > 1e8:
            score += 25
        elif total_main > 0:
            score += 15
        elif total_main < -1e8:
            score -= 15
        elif total_main < 0:
            score -= 10

    # Northbound individual change
    ext = capital_data.get("data_extended", {})
    nb = ext.get("northbound_individual", {})
    if nb and isinstance(nb, dict):
        chg = nb.get("change_shares")
        if chg is not None and chg > 0:
            score += 10

    # Capital flow streak
    istreak = ext.get("individual_streak", {})
    if istreak and isinstance(istreak, dict):
        ms = istreak.get("main_streak", 0)
        if ms >= 3:
            score += 10

    return max(0.0, min(100.0, score))


def score_fundamental_quick(candidate, fundamental_data):
    """Score fundamental dimension (0-100).

    Components: PE percentile + ROE + profit growth + revenue growth.
    """
    if not fundamental_data:
        return 50.0

    summary = fundamental_data.get("summary", {})
    if summary.get("data_quality") in ("error", None):
        return 50.0

    score = 50.0

    # PE percentile 3-year
    pe_pct = summary.get("pe_percentile_3y")
    if pe_pct is not None:
        if pe_pct < 30:
            score += 20
        elif pe_pct < 50:
            score += 10
        elif pe_pct > 80:
            score -= 15

    # ROE
    roe = _safe_float(summary.get("roe"))
    if roe > 15:
        score += 15
    elif roe > 10:
        score += 10
    elif roe < 0:
        score -= 10

    # Profit growth
    profit_g = _safe_float(summary.get("profit_growth_pct"))
    if profit_g > 20:
        score += 15
    elif profit_g > 10:
        score += 8
    elif profit_g < 0:
        score -= 10

    # Revenue growth
    revenue_g = _safe_float(summary.get("revenue_growth_pct"))
    if revenue_g > 15:
        score += 10
    elif revenue_g > 5:
        score += 5

    return max(0.0, min(100.0, score))


def score_sector_strength(candidate, sector_scores, sector_ranks):
    """Score sector strength dimension (0-100).

    Components: sector hot_score + relative rank within sector.
    """
    score = 50.0

    sector_code = candidate.get("sector_code", "")
    hot = sector_scores.get(sector_code, 50)
    # Map hot_score (0-100) to contribution
    score += (hot - 50) * 0.3  # ±15 from sector hot score

    # Within-sector relative rank: laggards in hot sectors get bonus
    change_pct = candidate.get("change_pct", 0)
    all_changes = sector_ranks.get(sector_code, [change_pct])
    pct_rank = _compute_pct_rank(change_pct, sorted(all_changes))

    if pct_rank < 50:
        # Laggard in a hot sector → rotation candidate bonus
        laggard_bonus = min(10, (50 - pct_rank) * 0.2)
        score += laggard_bonus

    return max(0.0, min(100.0, score))


def run_phase2(candidates, max_workers=4):
    """Phase 2: Fetch data and compute multi-dimension scores for all candidates."""
    print(f"[Phase 2/3] Scoring {len(candidates)} candidates...", file=sys.stderr)
    if not candidates:
        return []

    # Pre-fetch all K-line data in parallel
    print(f"  Fetching K-line data...", file=sys.stderr)
    kline_data = {}

    def _fetch_one_kline(c):
        ts_code = c["ts_code"]
        return ts_code, _fetch_kline(ts_code)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one_kline, c) for c in candidates]
        for fut in as_completed(futures):
            ts_code, kline = fut.result()
            kline_data[ts_code] = kline

    # Fetch capital flow in parallel (only for stocks with K-line data)
    print(f"  Fetching capital flow data...", file=sys.stderr)
    capital_data = {}

    def _fetch_one_cap(c):
        ts_code = c["ts_code"]
        return ts_code, _fetch_capital_flow(ts_code)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one_cap, c) for c in candidates]
        for fut in as_completed(futures):
            ts_code, cap = fut.result()
            capital_data[ts_code] = cap

    # Fetch fundamental data (prefer cache, parallel for misses)
    print(f"  Fetching fundamental data...", file=sys.stderr)
    fundamental_data = {}

    def _fetch_one_fund(c):
        ts_code = c["ts_code"]
        return ts_code, _fetch_fundamental(ts_code)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one_fund, c) for c in candidates]
        for fut in as_completed(futures):
            ts_code, fund = fut.result()
            fundamental_data[ts_code] = fund

    # Pre-compute sector change ranks
    sector_changes = {}
    for c in candidates:
        sc = c.get("sector_code", "")
        cp = c.get("change_pct", 0)
        sector_changes.setdefault(sc, []).append(cp)

    # Build sector scores map
    sector_scores = {}
    for c in candidates:
        sc = c.get("sector_code", "")
        if sc not in sector_scores:
            sector_scores[sc] = c.get("sector_hot_score", 50)

    # Compute scores
    scored = []
    for c in candidates:
        ts = c["ts_code"]
        kline = kline_data.get(ts)
        cap = capital_data.get(ts)
        fund = fundamental_data.get(ts)

        # Skip stocks with no K-line data (can't score)
        if not kline:
            continue

        # Verify K-line data has enough records
        records = kline.get("data", [])
        if len(records) < 20:
            continue

        dim_momentum = score_momentum(c, kline)
        dim_volume = score_volume_price(c, kline)
        dim_capital = score_capital(c, cap)
        dim_fundamental = score_fundamental_quick(c, fund)
        dim_sector = score_sector_strength(c, sector_scores, sector_changes)

        composite = (dim_momentum * 0.30 + dim_volume * 0.20
                     + dim_capital * 0.20 + dim_fundamental * 0.15
                     + dim_sector * 0.15)

        # Detect signals
        signals = _detect_signals(c, kline, cap, fund)

        # Detect warnings
        warnings = _detect_warnings(c, kline, cap, fund, dim_momentum, dim_volume,
                                     dim_fundamental)

        # Sector-relative rank (within sector, by change_pct)
        sector_code = c.get("sector_code", "")
        changes_in_sector = sorted(sector_changes.get(sector_code, [c["change_pct"]]),
                                   reverse=True)
        sector_rank = changes_in_sector.index(c["change_pct"]) + 1 if c["change_pct"] in changes_in_sector else len(changes_in_sector)

        scored.append({
            "code": c["code"],
            "ts_code": ts,
            "name": c["name"],
            "sector_code": sector_code,
            "sector_name": c["sector_name"],
            "sector_hot_score": c["sector_hot_score"],
            "composite_score": round(composite, 1),
            "dimensions": {
                "momentum": round(dim_momentum, 1),
                "volume_price": round(dim_volume, 1),
                "capital": round(dim_capital, 1),
                "fundamental": round(dim_fundamental, 1),
                "sector_strength": round(dim_sector, 1),
            },
            "signals": signals,
            "warnings": warnings,
            "sector_relative_rank": sector_rank,
            "sector_total": len(changes_in_sector),
        })

    return scored


# ──────────────────────── Signal & Warning Detection ────────────────────────


def _detect_signals(candidate, kline_data, capital_data, fundamental_data):
    """Detect positive signals for a stock."""
    signals = {}

    if not kline_data:
        return signals

    closes = _compute_close_prices(kline_data)

    # MA alignment
    ma5_val = ma(closes, 5)
    ma20_val = ma(closes, 20)
    ma60_val = ma(closes, 60) if len(closes) >= 60 else ma20_val
    if ma5_val and ma20_val and ma60_val:
        if ma5_val > ma20_val > ma60_val:
            signals["ma_alignment"] = "多头排列"
        elif ma5_val > ma20_val:
            signals["ma_alignment"] = "短期偏多"
        elif ma5_val < ma20_val < ma60_val:
            signals["ma_alignment"] = "空头排列"

    # Volume breakout
    records = kline_data.get("data", [])
    if len(records) >= 5:
        recent = records[-5:]
        avg_vol = sum(r.get("vol", 0) for r in recent) / len(recent)
        latest = records[-1]
        vol_ratio = latest.get("vol", 0) / avg_vol if avg_vol > 0 else 1.0
        close = latest.get("close", 0)
        if vol_ratio > 1.3 and ma20_val and close > ma20_val:
            signals["volume_breakout"] = True

    # Capital streak
    if capital_data:
        ext = capital_data.get("data_extended", {})
        istreak = ext.get("individual_streak", {})
        if istreak:
            ms = istreak.get("main_streak", 0)
            if ms > 0:
                signals["capital_streak"] = ms

        nb = ext.get("northbound_individual", {})
        if nb and isinstance(nb, dict):
            chg = nb.get("change_shares")
            if chg is not None and chg > 0:
                signals["northbound_adding"] = True

    # Fundamental signals
    if fundamental_data:
        summary = fundamental_data.get("summary", {})
        pe_pct = summary.get("pe_percentile_3y")
        if pe_pct is not None:
            signals["pe_percentile_3y"] = pe_pct
        roe = summary.get("roe")
        if roe is not None:
            signals["roe"] = roe

    return signals


def _detect_warnings(candidate, kline_data, capital_data, fundamental_data,
                     dim_momentum, dim_volume, dim_fundamental):
    """Detect warning signals."""
    warnings = []

    # High momentum but low volume support → divergence
    if dim_momentum > 70 and dim_volume < 40:
        warnings.append("量价背离：动量强但量能不足")

    # Low fundamental but high momentum → speculation risk
    if dim_momentum > 70 and dim_fundamental < 40:
        warnings.append("短期炒作风险：基本面弱但动量强")

    return warnings


# ──────────────────────── Phase 3: Rank + Output ────────────────────────


def assign_stars(composite_score):
    """Assign star rating based on composite score."""
    if composite_score >= 80:
        return 3
    elif composite_score >= 65:
        return 2
    elif composite_score >= 50:
        return 1
    return 0


def build_output(scored, candidates, excluded, sector_map, elapsed, source="market_theme"):
    """Build final output JSON."""
    # Sort by composite_score descending
    scored.sort(key=lambda x: x["composite_score"], reverse=True)

    # Assign stars
    for s in scored:
        s["stars"] = assign_stars(s["composite_score"])

    # Build sector summary
    sector_summary = {}
    for sc in sector_map:
        sector_stocks = [s for s in scored if s["sector_code"] == sc]
        if sector_stocks:
            avg_score = sum(s["composite_score"] for s in sector_stocks) / len(sector_stocks)
            sector_summary[sc] = {
                "name": sector_map[sc]["name"],
                "hot_score": sector_map[sc]["hot_score"],
                "stock_count": len(sector_stocks),
                "avg_score": round(avg_score, 1),
            }

    output = {
        "meta": {
            "scan_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "source": source,
            "input_sectors": list(sector_map.keys()),
            "candidate_count": len(candidates),
            "scored_count": len(scored),
            "elapsed_seconds": round(elapsed, 1),
        },
        "rankings": scored,
        "sector_summary": sector_summary,
        "excluded": excluded[:30],  # cap at 30 for readability
    }

    return output


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="A股个股筛选器 — Scan A-stocks in hot sectors"
    )
    parser.add_argument("--sectors", type=str,
                        help="板块代码列表, 逗号分隔 (e.g. BK0477,BK0897)")
    parser.add_argument("--from-leader", type=str,
                        help="从 market_leader JSON 输出文件读取板块")
    parser.add_argument("--top", type=int, default=10,
                        help="输出前N只股票 (默认10)")
    parser.add_argument("--min-score", type=float, default=50,
                        help="最低综合分阈值 (默认50)")
    args = parser.parse_args()

    # Determine sector codes
    sector_codes = []
    source = "manual"

    if args.from_leader:
        leader_data = _read_json(args.from_leader)
        if leader_data:
            source = "market_leader"
            for sec in leader_data.get("sectors_analyzed", []):
                sector_codes.append(sec.get("code", ""))
            # Also enrich sector_map from leader data
            if not sector_codes:
                print("Error: no sectors found in leader output", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"Error: cannot read leader file: {args.from_leader}", file=sys.stderr)
            sys.exit(1)

    if args.sectors:
        source = "market_theme" if not args.from_leader else source
        sector_codes.extend([s.strip() for s in args.sectors.split(",") if s.strip()])

    if not sector_codes:
        parser.error("Provide --sectors or --from-leader")

    # Remove duplicates
    sector_codes = list(dict.fromkeys(sector_codes))

    start = time.time()

    # Phase 1
    print(f"[Phase 1/3] Gathering A-stocks from {len(sector_codes)} sectors...", file=sys.stderr)
    phase1 = gather_candidates(sector_codes, top_n_per_sector=30)
    candidates = phase1["candidates"]
    excluded = phase1["excluded"]
    sector_map = phase1["sector_map"]
    print(f"  {len(candidates)} candidates, {len(excluded)} excluded", file=sys.stderr)

    if not candidates:
        elapsed = time.time() - start
        output = build_output([], candidates, excluded, sector_map, elapsed, source)
        print("<!--JSON_OUTPUT-->")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        print("<!--END_JSON_OUTPUT-->")
        return

    # Phase 2
    scored = run_phase2(candidates)

    # Phase 3
    print(f"[Phase 3/3] Building output...", file=sys.stderr)
    elapsed = time.time() - start

    # Filter by min score
    scored = [s for s in scored if s["composite_score"] >= args.min_score]

    output = build_output(scored, candidates, excluded, sector_map, elapsed, source)

    print(f"\nDone in {elapsed:.1f}s. Top {min(args.top, len(scored))} stocks:", file=sys.stderr)
    for i, s in enumerate(scored[:args.top]):
        dims = s["dimensions"]
        print(f"  {i+1}. {s['name']}({s['code']}) [{s['sector_name']}] "
              f"综合={s['composite_score']:.0f} ★{s['stars']} "
              f"动={dims['momentum']:.0f} 量={dims['volume_price']:.0f} "
              f"资={dims['capital']:.0f} 基={dims['fundamental']:.0f}",
              file=sys.stderr)

    print("<!--JSON_OUTPUT-->")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print("<!--END_JSON_OUTPUT-->")


if __name__ == "__main__":
    main()
