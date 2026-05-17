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
    --output-html  Write HTML report to reports/lists/YYYY-MM-DD-HH-mm.html

"""

import argparse
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from resolve_code import code_to_ts_code

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
DEFAULT_WATCHLIST = SCRIPT_DIR / "watchlist.yaml"
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_LISTS_DIR = PROJECT_ROOT / "reports" / "lists"
ASSETS_DIR = SKILL_DIR / "assets"


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


def _signal_direction(score: float) -> str:
    """Map combined score to signal direction CSS class suffix."""
    if score >= 80:
        return "up"
    if score >= 50:
        return "flat"
    return "down"


def build_report_context(output: dict) -> dict:
    """Build template context dict from scanner JSON output."""
    meta = output.get("meta", {})
    combined = output.get("combined_ranking", [])
    top_picks = output.get("top_picks", [])
    excluded = output.get("excluded", [])
    sector = output.get("sector_summary", {})

    # Ranking rows — top 50 detail, remainder summarized
    TOP_N = 50
    ranking_rows = []
    remainder_rows = []
    for i, c in enumerate(combined):
        ds = c.get("deep_score")
        cs = c.get("combined_score", 0)
        ts = c.get("trend_stage", "")
        stage_tag = {"early": "初期", "mid": "中期", "late": "末期"}.get(ts, "")
        sr = c.get("sector_rank", "")
        sc = c.get("sector_count", "")
        sector_str = f"{sr}/{sc}" if sr and sc else ""
        row = {
            "rank": c.get("rank", ""),
            "code": c.get("code", ""),
            "name": c.get("name", ""),
            "quick_score": c.get("quick_score", ""),
            "deep_score": str(ds) if ds is not None else "—",
            "signal": _signal_emoji(cs),
            "signal_dir": _signal_direction(cs),
            "stars": _stars_text(c.get("stars", 0)),
            "stars_num": c.get("stars", 0),
            "trend_stage": ts,
            "stage_tag": stage_tag,
            "sector_rank": sector_str,
            "risk_adjusted_score": c.get("risk_adjusted_score", ""),
        }
        if i < TOP_N:
            ranking_rows.append(row)
        else:
            remainder_rows.append(row)

    # Top picks
    pick_rows = []
    for i, p in enumerate(top_picks, 1):
        entry = {
            "pick_rank": i,
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "combined_score": p.get("combined_score", ""),
            "logic": p.get("logic", ""),
            "trend_stage": p.get("trend_stage", ""),
            "stage_tag": {"early": "初期", "mid": "中期", "late": "末期"}.get(p.get("trend_stage", ""), ""),
            "has_trading_plan": False,
            "entry_strategy": "",
            "entry_detail": "",
            "entry_low": "",
            "entry_high": "",
            "entry_current": "",
            "stop_loss_price": "",
            "stop_loss_pct": "",
            "tp1_price": "",
            "tp1_pct": "",
            "tp2_price": "",
            "tp2_pct": "",
            "position_pct": "",
            "timing_hint": "",
        }
        tp = p.get("trading_plan")
        if tp:
            entry["has_trading_plan"] = True
            entry["entry_strategy"] = tp.get("entry_strategy", "")
            entry["entry_detail"] = tp.get("entry_detail", "")
            ez = tp.get("entry_zone", {})
            entry["entry_low"] = ez.get("low", "")
            entry["entry_high"] = ez.get("high", "")
            entry["entry_current"] = ez.get("current", "")
            sl = tp.get("stop_loss", {})
            entry["stop_loss_price"] = sl.get("price", "")
            entry["stop_loss_pct"] = sl.get("risk_pct", "")
            tgs = tp.get("targets", {})
            tp1 = tgs.get("tp1", {})
            entry["tp1_price"] = tp1.get("price", "")
            entry["tp1_pct"] = tp1.get("pct", "")
            tp2 = tgs.get("tp2", {})
            entry["tp2_price"] = tp2.get("price", "")
            entry["tp2_pct"] = tp2.get("pct", "")
            pos = tp.get("position", {})
            entry["position_pct"] = pos.get("pct", "")
            entry["position_range"] = pos.get("range", [])
            entry["position_reason"] = pos.get("reason", "")
            timing = tp.get("timing", {})
            entry["timing_hint"] = timing.get("hint", "")
        pick_rows.append(entry)

    # Excluded
    excluded_summary = ", ".join(
        f"{e['code']}({e.get('name', '')} {e.get('reason', '')})"
        for e in excluded
    ) if excluded else ""

    # Sector summary — pass structured data for HTML template
    strong_list = sector.get("strong", [])
    neutral_list = sector.get("neutral", [])
    weak_list = sector.get("weak", [])

    # Remainder summary: count by signal direction
    remainder_count = len(remainder_rows)
    remainder_by_signal = {"up": 0, "flat": 0, "down": 0}
    for r in remainder_rows:
        d = r.get("signal_dir", "down")
        if d in remainder_by_signal:
            remainder_by_signal[d] += 1

    return {
        "scan_time": meta.get("scan_time", ""),
        "total_etfs": meta.get("total_etfs", ""),
        "valid_etfs": meta.get("valid_etfs", ""),
        "duration_seconds": meta.get("duration_seconds", ""),
        "has_ranking": bool(ranking_rows),
        "ranking_rows": ranking_rows if ranking_rows else None,
        "has_remainder": remainder_count > 0,
        "remainder_count": remainder_count,
        "remainder_up": remainder_by_signal["up"],
        "remainder_flat": remainder_by_signal["flat"],
        "remainder_down": remainder_by_signal["down"],
        "has_top_picks": bool(pick_rows),
        "top_picks": pick_rows if pick_rows else None,
        "has_excluded": bool(excluded),
        "excluded_summary": excluded_summary,
        "has_sector_summary": bool(strong_list or neutral_list or weak_list),
        "sector_strong": strong_list if strong_list else None,
        "sector_neutral": neutral_list if neutral_list else None,
        "sector_weak": weak_list if weak_list else None,
        # Keep text summaries for backward compat
        "sector_strong_summary": " | ".join(
            f"{s['name']}(+{s['avg_score']}↑)" for s in strong_list
        ) if strong_list else "",
        "sector_neutral_summary": " | ".join(
            f"{n['name']}({n['avg_score']}→)" for n in neutral_list
        ) if neutral_list else "",
        "sector_weak_summary": " | ".join(
            f"{w['name']}({w['avg_score']}↓)" for w in weak_list
        ) if weak_list else "",
    }


def generate_report(output: dict) -> tuple[Path, str]:
    """Render ETF scan HTML report and write to reports/lists/.

    Returns (output_path, report_content) so callers can embed in JSON.
    """
    from generate_report import render_template

    template_path = ASSETS_DIR / "etf-scan-report-template.html"
    if not template_path.exists():
        print(f"Warning: template not found at {template_path}", file=sys.stderr)
        return None, ""

    template = template_path.read_text(encoding="utf-8")
    context = build_report_context(output)
    report = render_template(template, context)

    now = datetime.now(timezone(timedelta(hours=8)))
    filename = now.strftime("%Y-%m-%d-%H-%M") + ".html"
    output_path = REPORTS_LISTS_DIR / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return output_path, report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    script_path = SCRIPT_DIR / script_name
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
# Trend stage detection
# ---------------------------------------------------------------------------


def detect_trend_stage(kline: list) -> dict:
    """Classify trend into early/mid/late stage.

    Returns dict with stage string and multiplier:
      - early (1.0): price just broke above MA20, RSI recovering
      - mid (1.0): MA5/20/60 aligned, RSI 55-70
      - late (0.5-0.7): price extended, RSI >70, divergence
    """
    closes = [r["close"] for r in kline]
    if len(closes) < 20:
        return {"stage": "mid", "multiplier": 1.0}

    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60) if len(closes) >= 60 else ma20
    rsi_val = _rsi(closes, 14)
    macd_val = _macd_direction(closes)
    price_ext_pct = (closes[-1] - ma20) / ma20 * 100 if ma20 > 0 else 0

    is_bullish = ma5 > ma20
    is_strong_bullish = ma5 > ma20 > ma60
    volumes = [r.get("vol", 0) or 0 for r in kline]
    if len(volumes) >= 10:
        avg_vol_recent = sum(volumes[-5:]) / 5
        avg_vol_older = sum(volumes[-10:-5]) / 5
        vol_ratio = avg_vol_recent / avg_vol_older if avg_vol_older > 0 else 1.0
        is_volume_shrinking = vol_ratio < 0.8
    else:
        vol_ratio = 1.0
        is_volume_shrinking = False

    late_signals = 0
    if rsi_val > 70:
        late_signals += 1
    if price_ext_pct > 5:
        late_signals += 1
    if is_volume_shrinking and is_bullish:
        late_signals += 1
    if macd_val < -0.5 and is_strong_bullish:
        late_signals += 1

    early_signals = 0
    if is_bullish and 35 <= rsi_val <= 55:
        early_signals += 1
    if 0 < price_ext_pct <= 4 and is_bullish:
        early_signals += 1
    if not is_volume_shrinking and is_bullish:
        early_signals += 1

    if late_signals >= 2:
        stage = "late"
        if price_ext_pct > 8:
            multiplier = 0.5
        elif price_ext_pct > 5:
            multiplier = 0.6
        else:
            multiplier = 0.7
    elif early_signals >= 2 and is_bullish and rsi_val < 60:
        stage = "early"
        multiplier = 1.0
    elif is_strong_bullish:
        stage = "mid"
        multiplier = 1.0
    else:
        stage = "mid"
        multiplier = 1.0

    return {"stage": stage, "multiplier": multiplier}


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


def compute_atr(kline: list, period: int = 14) -> float:
    """Average True Range."""
    if len(kline) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(kline)):
        high = kline[i].get("high", kline[i]["close"])
        low = kline[i].get("low", kline[i]["close"])
        prev_close = kline[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def compute_volatility(kline: list, period: int = 20) -> float:
    """Daily return volatility (std dev of returns)."""
    closes = [r["close"] for r in kline]
    if len(closes) < period + 1:
        return 0.0
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-period, 0)]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)


def compute_max_drawdown(kline: list, period: int = 20) -> float:
    """Maximum drawdown percentage in recent period."""
    closes = [r["close"] for r in kline]
    if len(closes) < period:
        return 0.0
    recent = closes[-period:]
    peak = recent[0]
    max_dd = 0.0
    for p in recent:
        if p > peak:
            peak = p
        dd = (peak - p) / peak
        max_dd = max(max_dd, dd)
    return max_dd * 100


def compute_risk_penalty(kline: list, risk_aversion: float = 5.0) -> dict:
    """Compute risk penalty coefficient [0, 1] and metrics."""
    volatility = compute_volatility(kline)
    max_dd = compute_max_drawdown(kline)
    atr = compute_atr(kline)
    penalty = 1.0 / (1.0 + volatility * risk_aversion)
    if max_dd > 15:
        penalty *= 0.7
    return {
        "penalty": round(penalty, 3),
        "atr": round(atr, 4),
        "volatility": round(volatility, 4),
        "max_drawdown": round(max_dd, 1),
    }


# ---------------------------------------------------------------------------
# Sector relative ranking
# ---------------------------------------------------------------------------


def compute_sector_ranking(scored: list[dict]) -> list[dict]:
    """Compute within-category percentile rank for each ETF.

    Adds sector_rank, sector_count, sector_percentile to scored entries.
    """
    from collections import defaultdict
    groups: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for i, s in enumerate(scored):
        cat = s.get("category", "其他")
        qs = s.get("quick_score") or 0
        groups[cat].append((i, qs))
    for _cat, members in groups.items():
        members.sort(key=lambda x: x[1], reverse=True)
        count = len(members)
        for rank, (idx, _qs) in enumerate(members, 1):
            pct = (count - rank) / count * 100 if count > 1 else 100.0
            scored[idx]["sector_rank"] = rank
            scored[idx]["sector_count"] = count
            scored[idx]["sector_percentile"] = round(pct, 1)
    return scored


# ---------------------------------------------------------------------------
# Trading plan
# ---------------------------------------------------------------------------


def build_trading_plan(code: str, name: str, kline: list, trend_stage: str,
                       combined_score: float, score_stars: int) -> dict:
    """Build actionable trading plan: entry zone, stop loss, targets, position."""
    closes = [r["close"] for r in kline]
    close_price = closes[-1]
    ma20 = _ma(closes, 20)
    atr_val = compute_atr(kline)
    risk = compute_risk_penalty(kline)
    volatility = risk["volatility"]

    # Support and resistance
    lows = [r.get("low", r["close"]) for r in kline[-20:]]
    highs = [r.get("high", r["close"]) for r in kline[-20:]]
    low_20 = min(lows) if len(kline) >= 20 else close_price * 0.95
    high_20 = max(highs) if len(kline) >= 20 else close_price * 1.05

    # Entry zone
    if trend_stage == "early":
        entry_strategy = "immediate"
        entry_low = round(max(low_20, close_price * 0.97), 3)
        entry_high = round(min(ma20, close_price), 3)
        entry_detail = "趋势初期，可立即入场"
    elif trend_stage == "mid":
        entry_strategy = "pullback"
        entry_low = round(ma20 * 0.98, 3)
        entry_high = round(min(ma20 * 1.02, close_price), 3)
        entry_detail = "趋势中期，等回踩均线入场"
    else:
        entry_strategy = "avoid"
        entry_low = round(low_20, 3)
        entry_high = round(close_price * 0.98, 3)
        entry_detail = "趋势末期，不建议新入场"

    # Stop loss
    atr_stop = close_price - 1.5 * (atr_val or close_price * 0.02)
    ma_stop = min(ma20 * 0.98, close_price * 0.95)
    struct_stop = low_20 * 0.995
    stop_price = max(atr_stop, ma_stop, struct_stop)
    risk_amount = max(close_price - stop_price, 0.01)
    risk_pct = round(risk_amount / close_price * 100, 1)
    stop_method = "atr" if stop_price == atr_stop else "ma" if stop_price == ma_stop else "structural"

    # Targets
    tp1 = min(close_price + 2 * risk_amount, high_20)
    tp2 = min(close_price + 3 * risk_amount, high_20 * 1.05)

    # Position sizing
    base_pct = 20.0
    cf = 1.2 if combined_score >= 80 else (1.0 if combined_score >= 65 else 0.8)
    vf = 1.1 if volatility < 0.1 else (1.0 if volatility < 0.2 else 0.8)
    position_pct = round(base_pct * cf * vf, 0)
    position_reason = "三星+低波动" if score_stars >= 3 else (
        "两星+中波动" if score_stars >= 2 else "一星+高波动"
    )

    # Timing hint
    rsi_val = _rsi(closes, 14)
    if trend_stage == "early" and rsi_val < 55:
        timing_action = "immediate"
        timing_hint = "可立即入场，趋势刚启动"
    elif trend_stage == "early":
        timing_action = "pullback"
        timing_hint = "关注回踩MA20入场机会"
    elif trend_stage == "mid" and rsi_val < 65:
        timing_action = "add"
        timing_hint = "持仓可加仓，注意仓位管理"
    elif trend_stage == "mid":
        timing_action = "hold"
        timing_hint = "持仓不动，不加仓"
    elif trend_stage == "late" and rsi_val >= 70:
        timing_action = "reduce"
        timing_hint = "减仓或设置跟踪止损"
    else:
        timing_action = "watch"
        timing_hint = "量价背离，考虑止盈"

    return {
        "entry_zone": {
            "low": entry_low, "high": entry_high, "current": round(close_price, 3)
        },
        "entry_strategy": entry_strategy,
        "entry_detail": entry_detail,
        "stop_loss": {"price": round(stop_price, 3), "method": stop_method, "risk_pct": risk_pct},
        "targets": {
            "tp1": {"price": round(tp1, 3), "method": "2R", "pct": round((tp1 - close_price) / close_price * 100, 1)},
            "tp2": {"price": round(tp2, 3), "method": "3R", "pct": round((tp2 - close_price) / close_price * 100, 1)},
        },
        "position": {
            "pct": int(position_pct),
            "range": [int(max(10, position_pct - 4)), int(min(30, position_pct + 4))],
            "reason": position_reason,
        },
        "timing": {"action": timing_action, "hint": timing_hint},
    }


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


def _piecewise_linear(x: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation between anchor points.

    Args:
        x: Input value to map.
        anchors: List of (input_val, output_score) tuples, sorted by input_val ascending.

    Returns:
        Interpolated output, clamped to [0, 100].
    """
    if len(anchors) < 2:
        raise ValueError("Need at least 2 anchors for piecewise linear")

    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x <= x0:
            if i == 0:
                result = y0
                return max(0.0, min(100.0, result))
            continue
        if x <= x1:
            if x1 == x0:
                return max(0.0, min(100.0, y0))
            slope = (y1 - y0) / (x1 - x0)
            result = y0 + slope * (x - x0)
            return max(0.0, min(100.0, result))

    # x > last anchor: use last segment
    x0, y0 = anchors[-2]
    x1, y1 = anchors[-1]
    if x1 == x0:
        return max(0.0, min(100.0, y1))
    slope = (y1 - y0) / (x1 - x0)
    result = y1 + slope * (x - x1)
    return max(0.0, min(100.0, result))


# --- Continuous anchor points for piecewise-linear scoring ---

CAPITAL_FLOW_ANCHORS = [
    (-1_000_000_000, 0.0),
    (-200_000_000, 10.0),
    (-60_000_000, 20.0),
    (0, 40.0),
    (60_000_000, 65.0),
    (200_000_000, 85.0),
    (1_000_000_000, 100.0),
]

SHARES_TREND_ANCHORS = [
    (-20, 0.0),
    (-10, 10.0),
    (-3, 20.0),
    (0, 40.0),
    (3, 65.0),
    (10, 85.0),
    (30, 100.0),
]

IOPV_ANCHORS = [
    (-2.0, 10.0),
    (-0.5, 40.0),
    (-0.3, 85.0),
    (-0.05, 65.0),
    (0.15, 30.0),
    (0.3, 15.0),
    (2.0, 0.0),
]

RSI_ANCHORS = [
    (0, -10.0),
    (20, -10.0),
    (30, 3.0),
    (40, 10.0),
    (50, 10.0),
    (60, 10.0),
    (70, 3.0),
    (80, -10.0),
    (100, -10.0),
]


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

    # --- RSI: continuous symmetric scoring ---
    score += _piecewise_linear(rsi_val, RSI_ANCHORS)

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

    # --- Price extension: penalize overextended moves ---
    # Price far above MA20 = high risk of mean reversion /追涨陷阱
    if ma20 > 0:
        deviation_pct = (closes[-1] - ma20) / ma20 * 100
        if deviation_pct > 12:
            score -= 20
        elif deviation_pct > 8:
            score -= 15
        elif deviation_pct > 5:
            score -= 8
        elif deviation_pct > 3:
            score -= 3
        elif deviation_pct < -3:
            score -= 5

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
    """Score capital flow using continuous piecewise-linear mapping.

    Returns 0-100 or None if no data.
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
    return round(_piecewise_linear(avg_net, CAPITAL_FLOW_ANCHORS), 1)


def score_shares_trend(etf_data: Optional[dict]) -> Optional[float]:
    """Score shares outstanding trend using continuous piecewise-linear mapping.

    Returns 0-100 or None if no data.
    """
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
    return round(_piecewise_linear(change_pct, SHARES_TREND_ANCHORS), 1)


def score_iopv(etf_data: Optional[dict]) -> Optional[float]:
    """Score IOPV discount/premium using continuous piecewise-linear mapping.

    Slight discount (-0.5% ~ -0.1%) is best signal.
    Returns 0-100 or None if no data.
    """
    if not etf_data:
        return None
    nav = etf_data.get("nav")
    if not isinstance(nav, dict):
        return None
    premium = nav.get("iopv_premium_pct")
    if premium is None:
        return None

    return round(_piecewise_linear(float(premium), IOPV_ANCHORS), 1)


# --- Contradiction detection ---

CONTRADICTION_RULES = [
    {
        "name": "shrink_up",
        "condition": lambda dims: (
            dims.get("momentum") is not None and dims.get("momentum") >= 70 and
            dims.get("volume") is not None and dims.get("volume") < 40
        ),
        "message": "缩量上涨，动能不可靠",
    },
    {
        "name": "momentum_flow_mismatch",
        "condition": lambda dims: (
            dims.get("momentum") is not None and dims.get("momentum") >= 70 and
            dims.get("capital_flow") is not None and dims.get("capital_flow") < 30
        ),
        "message": "动量与资金流向矛盾",
    },
    {
        "name": "flow_premium",
        "condition": lambda dims: (
            dims.get("capital_flow") is not None and dims.get("capital_flow") >= 70 and
            dims.get("iopv") is not None and dims.get("iopv") < 30
        ),
        "message": "资金流入但溢价偏高",
    },
    {
        "name": "dump",
        "condition": lambda dims: (
            dims.get("volume") is not None and dims.get("volume") >= 70 and
            dims.get("momentum") is not None and dims.get("momentum") < 30
        ),
        "message": "放量下跌",
    },
    {
        "name": "price_extension",
        "condition": lambda dims: (
            dims.get("price_ext_pct") is not None and dims.get("price_ext_pct", 0) > 5
        ),
        "message": "价格偏离均线过大，追涨风险高",
    },
]


def detect_contradictions(dimensions: dict) -> list[str]:
    """Detect contradictory signals across scoring dimensions.

    Args:
        dimensions: Dict of dimension_name -> score (0-100 scale, or None).

    Returns:
        List of warning message strings for detected contradictions.
    """
    warnings = []
    for rule in CONTRADICTION_RULES:
        if rule["condition"](dimensions):
            warnings.append(rule["message"])
    return warnings


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
    # Price extension % from MA20 for contradiction detection
    closes = [r["close"] for r in kline]
    ma20 = _ma(closes, 20)
    dims["price_ext_pct"] = round((closes[-1] - ma20) / ma20 * 100, 1) if ma20 > 0 and closes else 0.0

    # Detect contradictions across dimensions
    warnings = detect_contradictions(dims)

    # Trend stage and risk metrics
    trend_result = detect_trend_stage(kline)
    risk_result = compute_risk_penalty(kline)

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
        "warnings": warnings,
        "trend_stage": trend_result["stage"],
        "trend_stage_multiplier": trend_result["multiplier"],
        "risk_penalty": risk_result["penalty"],
        "close_price": closes[-1] if closes else 0,
        "kline": kline,  # for trading plan in Phase 3
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

    # Sector relative ranking
    valid = compute_sector_ranking(valid)

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
        pipeline_cmd = [sys.executable, str(SCRIPT_DIR / "run_pipeline.py"),
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
        scores_cmd = [sys.executable, str(SCRIPT_DIR / "compute_scores.py"),
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
            "warnings": p1.get("warnings", []),
            "trend_stage": p1.get("trend_stage", ""),
            "sector_rank": p1.get("sector_rank", ""),
            "sector_count": p1.get("sector_count", ""),
            "sector_percentile": p1.get("sector_percentile", 100),
        }

        trend_mult = p1.get("trend_stage_multiplier", 1.0)
        risk_pen = p1.get("risk_penalty", 1.0)

        if entry["deep_score"] is not None:
            # Normalize deep_score from [-3,+3] to [0,100] before combining
            deep_normalized = (entry["deep_score"] + 3) / 6 * 100
            # Phase 1 exclusive dimension bonus (shares_trend, iopv)
            p1_exclusive_weights = settings.get("p1_exclusive_bonus", {})
            bonus = 0.0
            p1_dims = entry.get("dimensions", {})
            for dim, w in p1_exclusive_weights.items():
                val = p1_dims.get(dim)
                if val is not None:
                    bonus += val * w
            entry["combined_score"] = round(
                (0.3 * entry["quick_score"] + 0.7 * deep_normalized + bonus) * trend_mult * risk_pen, 1
            )
            entry["p1_bonus"] = round(bonus, 1)
        else:
            entry["combined_score"] = round(
                entry["quick_score"] * trend_mult * risk_pen, 1
            ) if entry["quick_score"] is not None else None
            entry["p1_bonus"] = 0.0
        combined.append(entry)

    combined.sort(key=lambda x: x["combined_score"] or 0, reverse=True)
    for i, c in enumerate(combined):
        c["rank"] = i + 1
        cs = c["combined_score"] or 0

        # Sector-adjusted stars: only top 30% per category can get 3 stars
        sector_pct = c.get("sector_percentile", 100)
        if cs >= 80 and sector_pct >= 70:
            c["stars"] = 3
        elif cs >= 65 or (cs >= 55 and sector_pct >= 60):
            c["stars"] = 2
        elif cs >= 50:
            c["stars"] = 1
        else:
            c["stars"] = 0

        # Risk-adjusted score (raw * risk_penalty for display)
        risk_pen = c.get("risk_penalty", 1.0)
        c["risk_adjusted_score"] = round(cs * risk_pen, 1)

        # Build trading plan from stored kline
        kline = c.get("kline")
        if kline and cs > 0:
            c["trading_plan"] = build_trading_plan(
                c["code"], c.get("name", ""), kline,
                c.get("trend_stage", "mid"),
                cs, c["stars"],
            )

    return combined


def _cleanup_combined(combined: list[dict]) -> list[dict]:
    """Remove internal-only fields before output."""
    internal = {"kline", "trend_stage_multiplier", "close_price", "risk_penalty"}
    for c in combined:
        for key in internal:
            c.pop(key, None)
    return combined
def build_top_picks(combined: list[dict]) -> list[dict]:
    """Extract top picks with brief logic."""
    picks = combined[:5]
    result: list[dict] = []
    for p in picks:
        logic_parts: list[str] = []
        dims = p.get("dimensions", {})
        m = dims.get("momentum")
        if m is not None and m >= 70:
            logic_parts.append("动量强势")
        elif m is not None and m >= 55:
            logic_parts.append("动量偏强")
        cf = dims.get("capital_flow")
        if cf is not None and cf >= 65:
            logic_parts.append("主力资金流入")
        st = dims.get("shares_trend")
        if st is not None and st >= 65:
            logic_parts.append("份额持续增长")
        iv = dims.get("iopv")
        if iv is not None and iv >= 65:
            logic_parts.append("折价安全边际")
        warnings = p.get("warnings", [])
        if warnings:
            logic_parts.extend([f"⚠{w}" for w in warnings])
        if not logic_parts:
            logic_parts.append("综合评分居前")
        entry = {
            "code": p["code"],
            "name": p["name"],
            "combined_score": p["combined_score"],
            "logic": "，".join(logic_parts),
        }
        if "trading_plan" in p:
            entry["trading_plan"] = p["trading_plan"]
        if "trend_stage" in p:
            entry["trend_stage"] = p["trend_stage"]
        if "sector_percentile" in p:
            entry["sector_percentile"] = p["sector_percentile"]
        if "stars" in p:
            entry["stars"] = p["stars"]
        result.append(entry)
    return result


def build_excluded(scored_all: list[dict]) -> list[dict]:
    """Build list of low-score ETFs with reasons."""
    excluded: list[dict] = []
    for s in scored_all:
        if s["quick_score"] is not None and s["quick_score"] < 40:
            reasons: list[str] = []
            dims = s.get("dimensions", {})
            if (dims.get("momentum") or 50) < 40:
                reasons.append("动量弱")
            if (dims.get("capital_flow") or 50) < 30:
                reasons.append("资金流出")
            if (dims.get("shares_trend") or 50) < 30:
                reasons.append("份额缩水")
            if (dims.get("volume") or 50) < 30:
                reasons.append("量能不足")
            # Append contradiction warnings
            warnings = s.get("warnings", [])
            if warnings:
                reasons.extend([f"⚠{w}" for w in warnings])
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

    strong, neutral, weak = [], [], []
    for sector, scores in sector_scores.items():
        avg = sum(scores) / len(scores) if scores else 0
        if avg >= 60:
            strong.append({"name": sector, "avg_score": round(avg, 1)})
        elif avg >= 50:
            neutral.append({"name": sector, "avg_score": round(avg, 1)})
        else:
            weak.append({"name": sector, "avg_score": round(avg, 1)})

    return {
        "strong": sorted(strong, key=lambda x: x["avg_score"], reverse=True),
        "neutral": sorted(neutral, key=lambda x: x["avg_score"], reverse=True),
        "weak": sorted(weak, key=lambda x: x["avg_score"]),
    }


def build_output(watchlist: dict, phase1_ranked: list[dict], phase2_results: dict[str, dict],
                 settings: dict, args: argparse.Namespace, elapsed: float) -> dict:
    """Build final JSON output."""
    combined = build_combined_ranking(phase1_ranked, phase2_results, settings)
    combined = _cleanup_combined(combined)

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
    parser.add_argument("--output-html", action="store_true",
                        help="Write HTML report to reports/lists/")
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

    if args.output_html:
        report_path, report_html = generate_report(output)
        if report_path:
            output["report_path"] = str(report_path)
            output["report_html"] = report_html

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
