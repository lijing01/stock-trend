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

Options:
    --output-md    Write Markdown report to reports/lists/YYYY-MM-DD-HH-mm.md

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
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_LISTS_DIR = PROJECT_ROOT / "reports" / "lists"
ASSETS_DIR = SKILL_DIR.parent / "assets"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _signal_emoji(score: float) -> str:
    """Map combined score to signal emoji per SKILL.md spec."""
    if score >= 80:
        return "↑↑"
    if score >= 65:
        return "↑"
    if score >= 50:
        return "→"
    return "↓"


def _stars_text(stars: int) -> str:
    """Map star count to display string."""
    if stars >= 3:
        return "★★★"
    if stars == 2:
        return "★★☆"
    if stars == 1:
        return "★☆☆"
    return "☆☆☆"


def build_report_context(output: dict) -> dict:
    """Build template context dict from scanner JSON output."""
    meta = output.get("meta", {})
    combined = output.get("combined_ranking", [])
    top_picks = output.get("top_picks", [])
    excluded = output.get("excluded", [])
    sector = output.get("sector_summary", {})

    # Ranking rows
    ranking_rows = []
    for c in combined:
        ds = c.get("deep_score")
        ranking_rows.append({
            "rank": c.get("rank", ""),
            "code": c.get("code", ""),
            "name": c.get("name", ""),
            "quick_score": c.get("quick_score", ""),
            "deep_score": str(ds) if ds is not None else "—",
            "signal": _signal_emoji(c.get("combined_score", 0)),
            "stars": _stars_text(c.get("stars", 0)),
        })

    # Top picks
    pick_rows = []
    for i, p in enumerate(top_picks, 1):
        pick_rows.append({
            "pick_rank": i,
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "combined_score": p.get("combined_score", ""),
            "logic": p.get("logic", ""),
        })

    # Excluded
    excluded_summary = ", ".join(
        f"{e['code']}({e.get('name', '')} {e.get('reason', '')})"
        for e in excluded
    ) if excluded else ""

    # Sector summary
    strong_list = sector.get("strong", [])
    weak_list = sector.get("weak", [])
    strong_summary = " | ".join(
        f"{s['name']}(+{s['avg_score']}↑)" for s in strong_list
    ) if strong_list else ""
    weak_summary = " | ".join(
        f"{w['name']}({w['avg_score']}↓)" for w in weak_list
    ) if weak_list else ""

    return {
        "scan_time": meta.get("scan_time", ""),
        "total_etfs": meta.get("total_etfs", ""),
        "valid_etfs": meta.get("valid_etfs", ""),
        "duration_seconds": meta.get("duration_seconds", ""),
        "ranking_rows": ranking_rows if ranking_rows else None,
        "top_picks": pick_rows if pick_rows else None,
        "has_excluded": bool(excluded),
        "excluded_summary": excluded_summary,
        "has_sector_summary": bool(strong_list or weak_list),
        "sector_strong_summary": strong_summary,
        "sector_weak_summary": weak_summary,
    }


def generate_report(output: dict) -> Path:
    """Render ETF scan report template and write to reports/lists/."""
    from generate_report import render_template

    template_path = ASSETS_DIR / "etf-scan-report-template.md"
    if not template_path.exists():
        print(f"Warning: template not found at {template_path}", file=sys.stderr)
        return None

    template = template_path.read_text(encoding="utf-8")
    context = build_report_context(output)
    report = render_template(template, context)

    now = datetime.now(timezone(timedelta(hours=8)))
    filename = now.strftime("%Y-%m-%d-%H-%M") + ".md"
    output_path = REPORTS_LISTS_DIR / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"Report written to {output_path}", file=sys.stderr)
    return output_path


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


def _trend_strength(closes: list) -> tuple[float, float]:
    """Approximate directional movement: (trend_strength, direction_sign).

    Returns ADX-like magnitude (0-100) and sign: +1=bullish, -1=bearish.
    Uses rate of change over multiple lookbacks to gauge conviction.
    """
    if len(closes) < 20:
        return 0.0, 0.0

    # Rate of change over short and medium windows
    roc5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0.0
    roc20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0.0

    # Consistency: are both windows agreeing on direction?
    direction = 0
    if roc5 > 0.005 and roc20 > 0:
        direction = 1
    elif roc5 < -0.005 and roc20 < 0:
        direction = -1

    # Strength: how large is the move
    magnitude = min(abs(roc5) + abs(roc20), 1.0) * 100

    return magnitude, direction


def score_momentum(kline: list) -> float:
    """Score momentum: MA trend + RSI + MACD + trend strength. Returns 0-100.

    Symmetric scoring: bullish and bearish signals have equal magnitude.
    Base 50, each component ranges roughly -20 to +20 around base.
    """
    closes = [r["close"] for r in kline]
    if len(closes) < 20:
        return 50.0

    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    rsi_val = _rsi(closes, 14)
    macd_val = _macd_direction(closes)
    _, trend_dir = _trend_strength(closes)

    score = 50.0
    # --- MA alignment: symmetric ±15 ---
    if ma5 > ma20 > ma60:
        score += 15
    elif ma5 < ma20 and ma20 < ma60:
        score -= 15
    elif ma5 > ma20:
        score += 5
    elif ma5 < ma20:
        score -= 5

    # --- RSI: symmetric scoring ---
    if 40 <= rsi_val <= 60:
        score += 10
    elif 30 <= rsi_val < 40:
        score += 3
    elif 60 < rsi_val <= 70:
        score += 3
    elif rsi_val > 80:
        score -= 10
    elif rsi_val < 20:
        score -= 10

    # --- MACD direction: symmetric ±8 ---
    if macd_val > 0:
        score += 8
    else:
        score -= 8

    # --- Trend strength: symmetric ---
    if trend_dir == -1:
        score -= 8
    elif trend_dir == 1:
        score += 8

    return max(0.0, min(100.0, score))


def score_volume(kline: list) -> float:
    """Score volume activity dimension. Returns 0-100.

    High volume with rising prices is bullish; high volume with falling
    prices (恐慌性放量下跌) is bearish and should score lower.
    """
    if len(kline) < 10:
        return 50.0
    volumes = [r.get("vol", 0) or 0 for r in kline]
    closes = [r.get("close", 0) or 0 for r in kline]
    recent_avg = sum(volumes[-5:]) / 5
    long_avg = sum(volumes) / len(volumes)
    ratio = recent_avg / long_avg if long_avg > 0 else 1.0

    # Determine recent price direction
    n = min(5, len(closes))
    recent_close_avg = sum(closes[-n:]) / n
    older_close_avg = sum(closes[-2 * n:-n]) / n if len(closes) >= 2 * n else closes[0]
    price_up = recent_close_avg > older_close_avg

    score = 50.0
    if ratio > 1.5:
        score += 30 if price_up else 5   # 放量上涨 vs 恐慌放量下跌
    elif ratio > 1.2:
        score += 15 if price_up else 0
    elif ratio < 0.6:
        score -= 20
    elif ratio < 0.8:
        score -= 10

    # Bonus for high absolute turnover (only if price rising)
    amounts = [r.get("amount", 0) or 0 for r in kline[-1:]]
    if amounts and amounts[0] > 1_000_000_000 and price_up:
        score += 10

    return max(0.0, min(100.0, score))


def score_capital_flow(flow_data: Optional[dict]) -> Optional[float]:
    """Score capital flow from main force net flow. Returns 0-100 or None if no data.

    Reads 'main_net_inflow' (yuan) from E-type capital flow data.
    For FD-type ETFs the field is not present and the function returns
    None to signal missing data so it can be excluded from weighting.
    """
    if not flow_data:
        return None
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
        return None

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


def score_shares_trend(etf_data: Optional[dict]) -> Optional[float]:
    """Score shares outstanding trend from recent flows. Returns 0-100 or None if no data."""
    if not etf_data:
        return None
    recent_flows = etf_data.get("recent_flows")
    if not isinstance(recent_flows, list) or len(recent_flows) < 2:
        return None

    valid = [r for r in recent_flows if isinstance(r, dict) and r.get("shares_billion") is not None]
    if len(valid) < 2:
        return None

    first = float(valid[0]["shares_billion"])
    last = float(valid[-1]["shares_billion"])
    if first == 0:
        return None

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


def score_iopv(etf_data: Optional[dict]) -> Optional[float]:
    """Score IOPV discount/premium. Returns 0-100 or None if no data.

    A slight discount (-0.5% ~ -0.1%) is the best signal — it means the ETF
    trades below NAV, offering an entry at a small arbitrage discount.
    Deep discounts or premiums are scored lower.
    """
    if not etf_data:
        return None
    nav = etf_data.get("nav")
    if not isinstance(nav, dict):
        return None
    premium = nav.get("iopv_premium_pct")
    if premium is None:
        return None

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


def normalize_scores_by_cohort(scored: list[dict], weights: dict) -> list[dict]:
    """Rebase dimension scores to percentile ranks within this scan cohort.

    For each dimension, rank all ETFs and replace the raw score with its
    percentile (0-100). Then recompute quick_score using the same weights.
    This ensures differentiation even when absolute scores cluster (e.g.,
    all momentum scores near 100 in a bull market).
    Dimensions with None values are excluded from ranking for that dimension.
    """
    dim_keys = [k for k in weights if k != "quick_score"]
    n = len(scored)
    if n < 2:
        return scored

    for dim in dim_keys:
        # Collect pairs of (index, value) for non-None values
        pairs = []
        for i, s in enumerate(scored):
            val = s.get("dimensions", {}).get(dim)
            if val is not None:
                pairs.append((i, val))
        if not pairs:
            continue
        # Sort by value ascending
        pairs.sort(key=lambda x: x[1])
        # Assign percentile: rank / (n_with_data - 1) * 100
        count = len(pairs)
        for rank, (idx, _val) in enumerate(pairs):
            pct = rank / (count - 1) * 100 if count > 1 else 50.0
            scored[idx]["dimensions"][dim] = round(pct, 1)

    # Recompute quick_score from normalized dimensions
    for s in scored:
        total_weight = 0
        weighted_score = 0.0
        for dim, w in weights.items():
            val = s.get("dimensions", {}).get(dim)
            if val is not None:
                weighted_score += val * w
                total_weight += w
        s["quick_score"] = round(weighted_score / total_weight, 1) if total_weight > 0 else None

    return scored


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

    dims: dict[str, Optional[float]] = {}
    dims["momentum"] = score_momentum(kline)
    dims["volume"] = score_volume(kline)
    dims["capital_flow"] = score_capital_flow(cap_flow)
    dims["shares_trend"] = score_shares_trend(etf_data)
    dims["iopv"] = score_iopv(etf_data)

    # Weighted sum: skip None dimensions, redistribute their weight
    total_weight = 0
    weighted_score = 0.0
    for dim, w in weights.items():
        val = dims.get(dim)
        if val is not None:
            weighted_score += val * w
            total_weight += w

    quick_score = round(weighted_score / total_weight, 1) if total_weight > 0 else None

    name = ""
    etf_data = result.get("etf_data")
    if etf_data and isinstance(etf_data, dict):
        name = etf_data.get("fund_name", "")

    return {
        "code": result["code"],
        "ts_code": result["ts_code"],
        "name": name,
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
    max_workers: Optional[int] = None,
) -> tuple[dict, list[dict]]:
    """Run Phase 1 quick scan on all ETFs.

    Returns (raw_results_dict, ranked_list).
    """
    etf_list = build_phase1_etf_list(watchlist, focus)
    weights = settings.get("quick_score_weights", {})
    if max_workers is None:
        max_workers = settings.get("max_workers", 4)

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

    # Within-cohort percentile normalization: rebase each dimension
    # to its rank-percentile within this scan, then recompute quick_score.
    # This prevents bull-market clustering where all ETFs score 80-100.
    valid = normalize_scores_by_cohort(valid, weights)

    valid.sort(key=lambda x: x["quick_score"], reverse=True)

    for i, s in enumerate(valid):
        s["rank"] = i + 1

    return raw_results, valid


# --- Phase 2: Deep Analysis ---


def get_cached_pipeline_output(code: str) -> Optional[dict]:
    """Read existing pipeline_output.json from cache."""
    path = CACHE_DIR / code / "pipeline_output.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def get_cached_scores(code: str) -> Optional[dict]:
    """Read existing scores.json from cache."""
    path = CACHE_DIR / code / "scores.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def run_deep_analysis(code: str, settings: dict) -> dict:
    """Run full pipeline + scoring for one ETF. Returns deep score result."""
    result: dict[str, Any] = {"code": code, "ts_code": code_to_ts_code(code)}

    pipeline_result = get_cached_pipeline_output(code)
    if pipeline_result:
        result["pipeline_source"] = "cache"
    else:
        result["pipeline_source"] = "fresh"
        pipeline_cmd = [sys.executable, str(SKILL_DIR / "run_pipeline.py"),
                        "--code", code]
        try:
            subprocess.run(pipeline_cmd, capture_output=True, text=True,
                         timeout=settings.get("phase2_timeout", 45))
        except subprocess.TimeoutExpired:
            result["error"] = "pipeline_timeout"
            return result

    # Run scoring
    scores_result = get_cached_scores(code)
    if not scores_result:
        scores_cmd = [sys.executable, str(SKILL_DIR / "compute_scores.py"),
                      "--code", code]
        try:
            subprocess.run(scores_cmd, capture_output=True, text=True,
                         timeout=30)
            scores_result = get_cached_scores(code)
        except subprocess.TimeoutExpired:
            result["error"] = "scores_timeout"
            return result

    if scores_result:
        result["deep_score"] = scores_result.get("composite_score")
        result["verdict"] = scores_result.get("direction")
        result["confidence"] = scores_result.get("confidence")
        dims = scores_result.get("scores", {}) or {}
        result["dimension_scores"] = {
            "technical": dims.get("technical"),
            "capital_flow": dims.get("capital_flow"),
            "fundamental": dims.get("fundamental"),
            "sentiment": dims.get("sentiment"),
            "macro": dims.get("macro"),
        }
        result["risks"] = scores_result.get("risks", [])
        rp = scores_result.get("report_params", {}) or {}
        result["stop_loss"] = rp.get("stop_loss")
        result["targets"] = {
            "conservative": rp.get("target_conservative"),
            "moderate": rp.get("target_moderate"),
        }

    return result


def run_phase2(top_candidates: list[dict], settings: dict, max_workers: int = 4) -> dict[str, dict]:
    """Run deep analysis on top N ETF codes in parallel."""
    codes = [c["code"] for c in top_candidates]
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(run_deep_analysis, code, settings): code
                   for code in codes}
        for fut in as_completed(fut_map):
            code = fut_map[fut]
            try:
                results[code] = fut.result()
            except Exception as e:
                results[code] = {"code": code, "error": str(e)}
    return results


# --- Phase 3: Aggregate Output ---


def build_combined_ranking(phase1_ranked: list[dict], phase2_results: dict[str, dict],
                           settings: dict) -> list[dict]:
    """Merge Phase 1 and Phase 2 results into combined ranking."""
    combined: list[dict] = []
    for p1 in phase1_ranked:
        code = p1["code"]
        p2 = phase2_results.get(code, {})
        entry: dict[str, Any] = {
            "code": code,
            "ts_code": p1["ts_code"],
            "name": p1.get("name", "") or p2.get("name", ""),
            "category": p1.get("category", ""),
            "quick_score": p1["quick_score"],
            "deep_score": p2.get("deep_score"),
            "verdict": p2.get("verdict"),
            "confidence": p2.get("confidence"),
            "dimensions": p1.get("dimensions", {}),
            "deep_dimensions": p2.get("dimension_scores", {}),
            "risks": p2.get("risks", []),
            "stop_loss": p2.get("stop_loss"),
            "targets": p2.get("targets", {}),
        }

        if entry["deep_score"] is not None:
            # Normalize deep_score from [-3,+3] to [0,100] before combining
            deep_normalized = (entry["deep_score"] + 3) / 6 * 100
            entry["combined_score"] = round(
                0.3 * entry["quick_score"] + 0.7 * deep_normalized, 1
            )
        else:
            entry["combined_score"] = entry["quick_score"]
        combined.append(entry)

    combined.sort(key=lambda x: x["combined_score"] or 0, reverse=True)
    for i, c in enumerate(combined):
        c["rank"] = i + 1
        cs = c["combined_score"] or 0
        c["stars"] = 3 if cs >= 80 else 2 if cs >= 65 else 1 if cs >= 50 else 0

    return combined


def build_top_picks(combined: list[dict]) -> list[dict]:
    """Extract top picks with brief logic."""
    picks = combined[:5]
    result: list[dict] = []
    for p in picks:
        logic_parts: list[str] = []
        dims = p.get("dimensions", {})
        if dims.get("momentum", 0) >= 70:
            logic_parts.append("动量强势")
        elif dims.get("momentum", 0) >= 55:
            logic_parts.append("动量偏强")
        if dims.get("capital_flow", 0) >= 65:
            logic_parts.append("主力资金流入")
        if dims.get("shares_trend", 0) >= 65:
            logic_parts.append("份额持续增长")
        if dims.get("iopv", 0) >= 65:
            logic_parts.append("折价安全边际")
        if not logic_parts:
            logic_parts.append("综合评分居前")
        result.append({
            "code": p["code"],
            "name": p["name"],
            "combined_score": p["combined_score"],
            "logic": "，".join(logic_parts),
        })
    return result


def build_excluded(scored_all: list[dict]) -> list[dict]:
    """Build list of low-score ETFs with reasons."""
    excluded: list[dict] = []
    for s in scored_all:
        if s["quick_score"] is not None and s["quick_score"] < 40:
            reasons: list[str] = []
            dims = s.get("dimensions", {})
            if dims.get("momentum", 50) < 40:
                reasons.append("动量弱")
            if dims.get("capital_flow", 50) < 30:
                reasons.append("资金流出")
            if dims.get("shares_trend", 50) < 30:
                reasons.append("份额缩水")
            if dims.get("volume", 50) < 30:
                reasons.append("量能不足")
            excluded.append({
                "code": s["code"],
                "name": s.get("name", ""),
                "quick_score": s["quick_score"],
                "reason": " ".join(reasons) if reasons else "综合评分偏低",
            })
    return excluded


def build_sector_summary(combined: list[dict]) -> dict:
    """Build sector-level strength summary."""
    from collections import defaultdict
    sector_scores: dict[str, list[float]] = defaultdict(list)
    for c in combined:
        cat = c.get("category", "其他")
        sector_scores[cat].append(c.get("combined_score") or 0)

    strong, weak = [], []
    for sector, scores in sector_scores.items():
        avg = sum(scores) / len(scores) if scores else 0
        if avg >= 70:
            strong.append({"name": sector, "avg_score": round(avg, 1)})
        elif avg < 50:
            weak.append({"name": sector, "avg_score": round(avg, 1)})

    return {
        "strong": sorted(strong, key=lambda x: x["avg_score"], reverse=True),
        "weak": sorted(weak, key=lambda x: x["avg_score"]),
    }


def build_output(watchlist: dict, phase1_ranked: list[dict], phase2_results: dict[str, dict],
                 settings: dict, args: argparse.Namespace, elapsed: float) -> dict:
    """Build final JSON output."""
    combined = build_combined_ranking(phase1_ranked, phase2_results, settings)

    valid_count = len(phase1_ranked)
    total_count = sum(len(c["etfs"]) for c in watchlist["categories"])

    return {
        "meta": {
            "scan_time": datetime.now(timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%dT%H:%M:%S+08:00"
            ),
            "total_etfs": total_count,
            "valid_etfs": valid_count,
            "duration_seconds": round(elapsed, 1),
        },
        "combined_ranking": combined,
        "top_picks": build_top_picks(combined),
        "excluded": build_excluded(phase1_ranked),
        "sector_summary": build_sector_summary(combined),
    }


# --- CLI ---


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="ETF Scanner — scan watchlist and rank A-share ETFs")
    parser.add_argument("--top", type=int, default=None,
                        help="Number of ETFs for deep analysis (default: from config)")
    parser.add_argument("--focus", type=str, default=None,
                        help="Scan only specific category (e.g. 宽基指数, 科技)")
    parser.add_argument("--output", choices=["compact", "full"], default="full",
                        help="Output format")
    parser.add_argument("--watchlist", type=str, default=None,
                        help="Custom watchlist path")
    parser.add_argument("--no-deep", action="store_true",
                        help="Skip Phase 2 deep analysis")
    parser.add_argument("--output-md", action="store_true",
                        help="Write Markdown report to reports/lists/")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    """ETF Scanner main entry point."""
    args = parse_args(argv)
    start = time.time()

    # Load config
    watchlist = load_watchlist(Path(args.watchlist) if args.watchlist else None)
    settings = watchlist.get("settings", {})

    # Apply CLI overrides
    if args.top is not None:
        settings["top_n"] = args.top

    # Phase 1
    _, phase1_ranked = run_phase1(watchlist, settings, args.focus)

    # Phase 2
    phase2_results: dict[str, dict] = {}
    if not args.no_deep and phase1_ranked:
        top_n = settings.get("top_n", 10)
        top_candidates = phase1_ranked[:top_n]
        phase2_results = run_phase2(top_candidates, settings)

    # Phase 3
    elapsed = time.time() - start
    output = build_output(watchlist, phase1_ranked, phase2_results, settings, args, elapsed)

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()  # trailing newline

    if args.output_md:
        generate_report(output)


if __name__ == "__main__":
    main()
