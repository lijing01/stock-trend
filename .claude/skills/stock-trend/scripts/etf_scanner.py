#!/usr/bin/env python3
"""ETF Scanner — scan watchlist and rank A-share ETFs for daily trend analysis.

Phase 1: Quick scan — parallel data fetch + lightweight scoring across 5 dimensions.

Usage:
    python3 etf_scanner.py [--top N] [--focus <category>] [--output compact|full]

Options:
    --top N             Override number of top results (default: from watchlist settings)
    --focus <category>  Scan only a specific category (e.g., 科技)
    --output compact|full  compact=ranked list only, full=include raw data (default: compact)

Outputs JSON to stdout for Claude Code to render.
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
DEFAULT_WATCHLIST = SKILL_DIR / "watchlist.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def code_to_ts_code(code: str) -> str:
    """Convert raw ETF code to ts_code for existing scripts."""
    code = str(code).strip()
    if code.startswith("159"):
        return f"{code}.SZ"
    # All other ETF codes (5xx, 15xx, 51xx, 56xx, 58xx, 588xx) are SH
    return f"{code}.SH"


def load_watchlist(path: Optional[Path] = None) -> dict:
    """Load ETF watchlist from YAML config."""
    path = path or DEFAULT_WATCHLIST
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Data fetching — calls existing scripts via subprocess
# ---------------------------------------------------------------------------


def run_script(script_name: str, args: list[str], timeout: int = 30) -> Optional[dict]:
    """Run an existing stock-trend script and return parsed JSON output."""
    script_path = SKILL_DIR / script_name
    cmd = [sys.executable, str(script_path)] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def fetch_quick_kline(code: str, days: int = 60) -> Optional[list]:
    """Fetch K-line data for Phase 1 quick score via eastmoney."""
    ts_code = code_to_ts_code(code)
    raw = run_script("fetch_kline_eastmoney.py", [ts_code], timeout=20)
    if raw and raw.get("meta", {}).get("data_source") != "error":
        return raw.get("data", [])
    return None


def fetch_quick_capital_flow(code: str) -> Optional[dict]:
    """Fetch capital flow for Phase 1 (main force net flow)."""
    ts_code = code_to_ts_code(code)
    raw = run_script("fetch_capital_flow.py", [ts_code], timeout=20)
    if raw and raw.get("meta", {}).get("data_source") != "error":
        return raw
    return None


def fetch_quick_etf_data(code: str) -> Optional[dict]:
    """Fetch ETF-specific data for Phase 1 (scale, shares, IOPV).

    Note: fetch_etf_data.py outputs a flat dict (no meta/data wrapper),
    unlike the other scripts.
    """
    raw = run_script("fetch_etf_data.py", [code], timeout=20)
    if raw and isinstance(raw, dict) and raw.get("fund_code"):
        return raw
    return None


# ---------------------------------------------------------------------------
# Technical analysis helpers
# ---------------------------------------------------------------------------


def _ma(prices: list, period: int) -> float:
    """Simple moving average."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def _rsi(prices: list, period: int = 14) -> float:
    """Relative Strength Index (smoothed RSI)."""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_val = 50.0
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rsi_val = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return rsi_val


def _macd_direction(prices: list) -> float:
    """Return MACD histogram direction: positive=bullish, negative=bearish.

    Uses an approximate EMA calculation over the full price series.
    """
    if len(prices) < 26:
        return 0.0
    # Seed with SMA
    ema12 = sum(prices[:12]) / 12
    ema26 = sum(prices[:26]) / 26
    alpha12, alpha26 = 2 / 13, 2 / 27
    for p in prices[12:]:
        ema12 = ema12 * (1 - alpha12) + p * alpha12
    for p in prices[26:]:
        ema26 = ema26 * (1 - alpha26) + p * alpha26
    return ema12 - ema26


# ---------------------------------------------------------------------------
# Quick scoring functions (Phase 1)
# ---------------------------------------------------------------------------


def score_momentum(kline: list) -> float:
    """Score momentum: MA trend + RSI + MACD. Returns 0-100."""
    closes = [r["close"] for r in kline]
    if len(closes) < 20:
        return 50.0

    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    rsi_val = _rsi(closes, 14)
    macd_val = _macd_direction(closes)

    score = 50.0
    # --- MA alignment (0-40 points) ---
    if ma5 > ma20 > ma60:
        score += 30
    elif ma5 > ma20 and len(closes) >= 60 and ma20 > ma60:
        score += 20
    elif ma5 > ma20:
        score += 8
    elif ma5 < ma20 and ma20 < ma60:
        score -= 15
    elif ma5 < ma20:
        score -= 5

    # --- RSI (0-30 points) ---
    if 40 <= rsi_val <= 60:
        score += 15
    elif 30 <= rsi_val < 40 or 60 < rsi_val <= 70:
        score += 5
    elif rsi_val > 80 or rsi_val < 20:
        score -= 8

    # --- MACD direction (0-30 points) ---
    if macd_val > 0:
        score += 10
    else:
        score -= 5

    return max(0.0, min(100.0, score))


def score_volume(kline: list) -> float:
    """Score volume activity dimension. Returns 0-100."""
    if len(kline) < 10:
        return 50.0
    # Use 'vol' field from eastmoney kline data
    volumes = [r.get("vol", 0) or 0 for r in kline]
    recent_avg = sum(volumes[-5:]) / 5
    long_avg = sum(volumes) / len(volumes)
    ratio = recent_avg / long_avg if long_avg > 0 else 1.0

    score = 50.0
    if ratio > 1.5:
        score += 30
    elif ratio > 1.2:
        score += 15
    elif ratio < 0.6:
        score -= 20
    elif ratio < 0.8:
        score -= 10

    # Bonus for high absolute turnover
    amounts = [r.get("amount", 0) or 0 for r in kline[-1:]]
    if amounts and amounts[0] > 1_000_000_000:  # > 1B yuan
        score += 10

    return max(0.0, min(100.0, score))


def score_capital_flow(flow_data: Optional[dict]) -> float:
    """Score capital flow from main force net flow. Returns 0-100.

    Reads 'main_net_inflow' (yuan) from E-type capital flow data.
    For FD-type ETFs the field is not present and the function returns
    neutral (50).  The shares-based signal is captured by score_shares_trend.
    """
    if not flow_data:
        return 50.0
    data = flow_data.get("data", [])
    if not data:
        return 50.0

    net_flows = []
    for row in data:
        if isinstance(row, dict):
            net = row.get("main_net_inflow")
            if net is not None:
                net_flows.append(float(net))

    if not net_flows:
        return 50.0

    avg_net = sum(net_flows) / len(net_flows)

    if avg_net > 100_000_000:   # > 1亿 — strong inflow
        return 85.0
    elif avg_net > 20_000_000:  # > 2000万 — moderate inflow
        return 65.0
    elif avg_net > -20_000_000: # near neutral
        return 40.0
    elif avg_net > -100_000_000:# > -1亿 — moderate outflow
        return 20.0
    return 10.0                 # strong outflow


def score_shares_trend(etf_data: Optional[dict]) -> float:
    """Score shares outstanding trend from recent flows. Returns 0-100.

    Computes the percentage change in shares_billion over the available
    recent_flows period (from fetch_etf_data.py).
    """
    if not etf_data:
        return 50.0
    recent_flows = etf_data.get("recent_flows")
    if not isinstance(recent_flows, list) or len(recent_flows) < 2:
        return 50.0

    valid = [r for r in recent_flows if isinstance(r, dict) and r.get("shares_billion") is not None]
    if len(valid) < 2:
        return 50.0

    first = float(valid[0]["shares_billion"])
    last = float(valid[-1]["shares_billion"])
    if first == 0:
        return 50.0

    change_pct = (last - first) / abs(first) * 100

    if change_pct > 5:
        return 85.0
    elif change_pct > 1:
        return 65.0
    elif change_pct > -1:
        return 40.0
    elif change_pct > -5:
        return 20.0
    return 10.0


def score_iopv(etf_data: Optional[dict]) -> float:
    """Score IOPV discount/premium from fetch_etf_data.py output. Returns 0-100.

    A slight discount (-0.5% ~ -0.1%) is the best signal — it means the ETF
    trades below NAV, offering an entry at a small arbitrage discount.
    Deep discounts or premiums are scored lower.
    """
    if not etf_data:
        return 50.0
    nav = etf_data.get("nav")
    if not isinstance(nav, dict):
        return 50.0
    premium = nav.get("iopv_premium_pct")
    if premium is None:
        return 50.0

    premium = float(premium)
    if -0.5 < premium <= -0.1:
        return 85.0
    elif -0.1 < premium <= 0:
        return 65.0
    elif premium <= -0.5:
        return 40.0
    elif 0 < premium <= 0.3:
        return 30.0
    return 10.0  # premium > 0.3%


# ---------------------------------------------------------------------------
# Phase 1 orchestration
# ---------------------------------------------------------------------------


def scan_single_etf(code: str, settings: dict) -> dict:
    """Run Phase 1 scan for a single ETF. Returns result dict or error."""
    result: dict[str, Any] = {
        "code": code,
        "ts_code": code_to_ts_code(code),
        "error": None,
        "kline": None,
        "capital_flow": None,
        "etf_data": None,
    }
    try:
        kline = fetch_quick_kline(code, settings.get("quick_kline_days", 60))
        if not kline or len(kline) < 10:
            result["error"] = "kline_insufficient"
            return result
        result["kline"] = kline
        result["capital_flow"] = fetch_quick_capital_flow(code)
        result["etf_data"] = fetch_quick_etf_data(code)
    except Exception as e:
        result["error"] = str(e)
    return result


def compute_quick_score(result: dict, weights: dict) -> dict:
    """Compute quick score for a single ETF result. Returns scored result."""
    if result.get("error") or not result.get("kline"):
        return {
            "code": result["code"],
            "ts_code": result["ts_code"],
            "quick_score": None,
            "error": result.get("error", "no_data"),
        }

    kline = result["kline"]
    cap_flow = result.get("capital_flow")
    etf_data = result.get("etf_data")

    dims: dict[str, float] = {}
    dims["momentum"] = score_momentum(kline)
    dims["volume"] = score_volume(kline)
    dims["capital_flow"] = score_capital_flow(cap_flow)
    dims["shares_trend"] = score_shares_trend(etf_data)
    dims["iopv"] = score_iopv(etf_data)

    # Weighted sum with missing-dimension handling
    total_weight = 0
    weighted_score = 0.0
    for dim, w in weights.items():
        weighted_score += dims.get(dim, 50) * w
        total_weight += w

    quick_score = round(weighted_score / total_weight, 1) if total_weight > 0 else None

    return {
        "code": result["code"],
        "ts_code": result["ts_code"],
        "quick_score": quick_score,
        "dimensions": dims,
    }


def build_phase1_etf_list(watchlist: dict, focus: Optional[str] = None) -> list[dict]:
    """Build flat list of ETF codes from watchlist, optionally filtered by category."""
    etfs: list[dict] = []
    for cat in watchlist["categories"]:
        if focus and cat["name"] != focus:
            continue
        for etf in cat["etfs"]:
            etfs.append({"code": str(etf["code"]), "category": cat["name"]})
    return etfs


def apply_filters(etf_list: list[dict], raw_results: dict, settings: dict) -> list[dict]:
    """Filter out ETFs that don't meet minimum criteria."""
    filtered: list[dict] = []
    for e in etf_list:
        code = e["code"]
        raw = raw_results.get(code, {})
        kline = raw.get("kline")

        if raw.get("error") == "kline_insufficient":
            continue

        # Amount filter (成交额)
        if kline and len(kline) > 5:
            recent_amounts = [r.get("amount", 0) or 0 for r in kline[-5:]]
            avg_amount = sum(recent_amounts) / len(recent_amounts)
            if avg_amount < settings.get("min_amount", 10_000_000):
                continue

        filtered.append(e)
    return filtered


def run_phase1(
    watchlist: dict,
    settings: dict,
    focus: Optional[str] = None,
    max_workers: int = 4,
) -> tuple[dict, list[dict]]:
    """Run Phase 1 quick scan on all ETFs.

    Returns (raw_results_dict, ranked_list).
    """
    etf_list = build_phase1_etf_list(watchlist, focus)
    weights = settings.get("quick_score_weights", {})

    # Fetch data in parallel
    raw_results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(scan_single_etf, e["code"], settings): e
            for e in etf_list
        }
        for fut in as_completed(fut_map):
            e = fut_map[fut]
            try:
                raw_results[e["code"]] = fut.result()
            except Exception as ex:
                raw_results[e["code"]] = {
                    "code": e["code"],
                    "ts_code": code_to_ts_code(e["code"]),
                    "error": str(ex),
                    "kline": None,
                }

    # Filter by minimum criteria
    etf_list = apply_filters(etf_list, raw_results, settings)

    # Compute quick scores and rank
    scored: list[dict] = []
    for e in etf_list:
        res = raw_results.get(e["code"], {})
        score_result = compute_quick_score(res, weights)
        score_result["category"] = e["category"]
        scored.append(score_result)

    # Filter valid (non-error) and sort descending
    valid = [s for s in scored if s["quick_score"] is not None]
    valid.sort(key=lambda x: x["quick_score"], reverse=True)

    for i, s in enumerate(valid):
        s["rank"] = i + 1

    return raw_results, valid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETF Scanner — scan watchlist and rank A-share ETFs",
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="Number of top ETFs to show (default: from settings)",
    )
    parser.add_argument(
        "--focus", type=str, default=None,
        help="Focus on a specific category (e.g., 科技)",
    )
    parser.add_argument(
        "--output", choices=["compact", "full"], default="compact",
        help="Output format (default: compact)",
    )
    args = parser.parse_args()

    watchlist = load_watchlist()
    settings = watchlist["settings"]

    start = time.time()
    raw_results, ranked = run_phase1(watchlist, settings, focus=args.focus)
    elapsed = time.time() - start

    top_n = args.top or settings.get("top_n", 10)
    results = ranked[:top_n]

    output: dict[str, Any] = {
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "phase": 1,
        "total_scanned": len(build_phase1_etf_list(watchlist, args.focus)),
        "total_ranked": len(ranked),
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    }

    if args.output == "full":
        output["raw_results"] = raw_results

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
