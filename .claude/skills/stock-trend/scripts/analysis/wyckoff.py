#!/usr/bin/env python3
"""Wyckoff Method analysis module.

Provides phase detection (accumulation/markup/distribution/markdown),
Volume Spread Analysis (VSA), and cause-effect quantification using
the Wyckoff framework.

Usage:
    python3 wyckoff.py <kline_json> [-o <output_path>]
"""

import json
import sys
from pathlib import Path
from typing import Any

# Phase enums
PHASE_ACCUMULATION = "accumulation"
PHASE_MARKUP = "markup"
PHASE_DISTRIBUTION = "distribution"
PHASE_MARKDOWN = "markdown"
PHASE_UNKNOWN = "phase_unknown"

PHASE_NAMES = {
    PHASE_ACCUMULATION: "吸筹阶段",
    PHASE_MARKUP: "拉升阶段",
    PHASE_DISTRIBUTION: "派发阶段",
    PHASE_MARKDOWN: "砸盘阶段",
    PHASE_UNKNOWN: "无法判定",
}

# Sub-phase enums
SUB_SC = "selling_climax"
SUB_AR = "automatic_rally"
SUB_ST = "secondary_test"
SUB_SPRING = "spring"
SUB_LPS = "lps"
SUB_PRE_MARKUP = "pre_markup"
SUB_JAC = "jac"
SUB_BU = "backup"
SUB_CONTINUATION = "continuation"
SUB_BC = "buying_climax"
SUB_UTAD = "utad"
SUB_LPSY = "lpsy"
SUB_SOW = "sign_of_weakness"
SUB_PRE_MARKDOWN = "pre_markdown"
SUB_BREAKDOWN = "breakdown"
SUB_PANIC = "panic_selling"
SUB_STOPPING_VOL = "stopping_volume"

SUB_PHASE_NAMES = {
    SUB_SC: "抛售高潮（SC）",
    SUB_AR: "自动反弹（AR）",
    SUB_ST: "二次测试（ST）",
    SUB_SPRING: "初支（Spring）",
    SUB_LPS: "最后支撑点（LPS）",
    SUB_PRE_MARKUP: "拉升前准备",
    SUB_JAC: "跃过小溪（JAC）",
    SUB_BU: "回踩（BU）",
    SUB_CONTINUATION: "持续拉升",
    SUB_BC: "买入高潮（BC）",
    SUB_UTAD: "上冲回落（UTAD）",
    SUB_LPSY: "最后供应点（LPSY）",
    SUB_SOW: "弱势信号（SOW）",
    SUB_PRE_MARKDOWN: "砸盘前兆",
    SUB_BREAKDOWN: "破位下跌",
    SUB_PANIC: "恐慌抛售",
    SUB_STOPPING_VOL: "止跌量",
}

# Phase → score mapping
PHASE_SCORES = {
    (PHASE_ACCUMULATION, SUB_SC): 0.5,
    (PHASE_ACCUMULATION, SUB_AR): 0.5,
    (PHASE_ACCUMULATION, SUB_ST): 1.0,
    (PHASE_ACCUMULATION, SUB_SPRING): 1.5,
    (PHASE_ACCUMULATION, SUB_LPS): 2.0,
    (PHASE_ACCUMULATION, SUB_PRE_MARKUP): 2.0,
    (PHASE_MARKUP, SUB_JAC): 2.0,
    (PHASE_MARKUP, SUB_BU): 1.5,
    (PHASE_MARKUP, SUB_CONTINUATION): 1.0,
    (PHASE_DISTRIBUTION, SUB_BC): -1.0,
    (PHASE_DISTRIBUTION, SUB_UTAD): -1.5,
    (PHASE_DISTRIBUTION, SUB_LPSY): -2.0,
    (PHASE_DISTRIBUTION, SUB_SOW): -2.0,
    (PHASE_DISTRIBUTION, SUB_PRE_MARKDOWN): -2.5,
    (PHASE_MARKDOWN, SUB_BREAKDOWN): -2.5,
    (PHASE_MARKDOWN, SUB_PANIC): -2.0,
    (PHASE_MARKDOWN, SUB_STOPPING_VOL): -1.5,
}
DEFAULT_PHASE_SCORE = 0.0

# Swing detection constants
SWING_MIN_HEIGHT_ATR_RATIO = 0.5
VOLUME_RATIO_LOOKBACK = 50

# Climax detection constants
CLIMAX_VOL_RATIO_THRESHOLD = 2.0
CLIMAX_SHADOW_RATIO_THRESHOLD = 0.5

# Trading range constants
RANGE_CLUSTER_TOLERANCE_ATR = 1.0
RANGE_MIN_HEIGHT_ATRS = 3
RANGE_MIN_TOUCHES = 3
RANGE_MIN_BARS = 20


def load_kline(path: str) -> dict | None:
    """Load K-line JSON, validate structure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading K-line: {e}", file=sys.stderr)
        return None
    rows = data.get("data", [])
    if not isinstance(rows, list) or len(rows) < 30:
        print(f"Warning: insufficient K-line data ({len(rows) if isinstance(rows, list) else 0} rows)", file=sys.stderr)
    return data


def _safe_float(v: Any) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def extract_ohlcv(rows: list) -> dict:
    """Extract OHLCV arrays from K-line data rows."""
    opens, highs, lows, closes, volumes = [], [], [], [], []
    dates = []
    for r in rows:
        o = _safe_float(r.get("open"))
        h = _safe_float(r.get("high"))
        l = _safe_float(r.get("low"))
        c = _safe_float(r.get("close"))
        v = _safe_float(r.get("vol") or r.get("volume"))
        if None in (o, h, l, c, v):
            continue
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v)
        dates.append(str(r.get("date") or r.get("trade_date") or ""))
    return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes, "date": dates}


def compute_ma(values: list, period: int) -> list[float | None]:
    """Simple moving average. Pads with None for first period-1 entries."""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(round(sum(values[i - period + 1 : i + 1]) / period, 4))
    return result


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> list[float | None]:
    """Average True Range."""
    trs = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return compute_ma(trs, period)


def detect_swing_points(closes: list, highs: list, lows: list, volumes: list,
                         atr_values: list, lookback: int = 2) -> list[dict]:
    """Detect pivot highs and lows using N-bar lookback."""
    if len(closes) < lookback * 2 + 1:
        return []
    swings = []
    for i in range(lookback, len(closes) - lookback):
        atr = atr_values[i]
        if atr is None or atr == 0:
            continue
        min_height = atr * SWING_MIN_HEIGHT_ATR_RATIO
        # Pivot high
        is_pivot_high = True
        for offset in range(1, lookback + 1):
            if highs[i] <= highs[i - offset] or highs[i] <= highs[i + offset]:
                is_pivot_high = False
                break
        if is_pivot_high and (highs[i] - lows[i]) > min_height:
            swings.append({
                "index": i, "date": "", "type": "high", "price": highs[i],
                "volume_ratio": volumes[i] / _ma_of_last_n(volumes, i, VOLUME_RATIO_LOOKBACK) if i >= VOLUME_RATIO_LOOKBACK else 1.0,
                "is_climax": False,
            })
        # Pivot low
        is_pivot_low = True
        for offset in range(1, lookback + 1):
            if lows[i] >= lows[i - offset] or lows[i] >= lows[i + offset]:
                is_pivot_low = False
                break
        if is_pivot_low and (highs[i] - lows[i]) > min_height:
            swings.append({
                "index": i, "date": "", "type": "low", "price": lows[i],
                "volume_ratio": volumes[i] / _ma_of_last_n(volumes, i, VOLUME_RATIO_LOOKBACK) if i >= VOLUME_RATIO_LOOKBACK else 1.0,
                "is_climax": False,
            })
    return sorted(swings, key=lambda s: s["index"])


def _ma_of_last_n(values: list, idx: int, n: int) -> float:
    start = max(0, idx - n + 1)
    segment = values[start : idx + 1]
    return sum(segment) / len(segment) if segment else 0.0


def mark_climaxes(swings: list, highs: list, lows: list, closes: list,
                  volumes: list, atr_values: list, opens: list | None = None) -> list[dict]:
    """Classify swing points as climaxes where volume spikes + extreme spread."""
    for s in swings:
        i = s["index"]
        if i >= len(volumes) or i >= len(highs) or i >= len(lows) or i >= len(closes):
            continue
        vol_ratio = s["volume_ratio"]
        total_range = highs[i] - lows[i]
        if total_range == 0:
            continue
        # Compute shadow ratios using open for bearish candles
        if opens and i < len(opens):
            o = opens[i]
            lower_shadow = min(closes[i], o) - lows[i]
            upper_shadow = highs[i] - max(closes[i], o)
        else:
            lower_shadow = min(closes[i], highs[i]) - lows[i]
            upper_shadow = highs[i] - max(closes[i], lows[i])
        shadow_ratio_lower = lower_shadow / total_range if total_range > 0 else 0
        shadow_ratio_upper = upper_shadow / total_range if total_range > 0 else 0

        if s["type"] == "low" and vol_ratio > CLIMAX_VOL_RATIO_THRESHOLD and shadow_ratio_lower > CLIMAX_SHADOW_RATIO_THRESHOLD:
            s["is_climax"] = True
            s["climax_type"] = "selling"
        elif s["type"] == "high" and vol_ratio > CLIMAX_VOL_RATIO_THRESHOLD and shadow_ratio_upper > CLIMAX_SHADOW_RATIO_THRESHOLD:
            s["is_climax"] = True
            s["climax_type"] = "buying"
    return swings


def detect_trading_range(swings: list, closes: list, atr_values: list,
                          min_touches: int = RANGE_MIN_TOUCHES, min_bars: int = RANGE_MIN_BARS) -> dict | None:
    """Aggregate swing high/low points into a trading range."""
    if len(swings) < min_touches:
        return None
    highs_sorted = sorted(set(s["price"] for s in swings if s["type"] == "high"))
    lows_sorted = sorted(set(s["price"] for s in swings if s["type"] == "low"))
    if not highs_sorted or not lows_sorted:
        return None
    median_atr = _median([a for a in atr_values if a is not None]) or 0
    tolerance = median_atr * RANGE_CLUSTER_TOLERANCE_ATR
    resistance = _cluster_peak(highs_sorted, tolerance)
    support = _cluster_peak(lows_sorted, tolerance)
    if resistance is None or support is None or resistance <= support:
        return None
    range_height = resistance - support
    if range_height < median_atr * RANGE_MIN_HEIGHT_ATRS:
        return None
    touch_count = 0
    for s in swings:
        if abs(s["price"] - resistance) <= tolerance or abs(s["price"] - support) <= tolerance:
            touch_count += 1
    if touch_count < min_touches:
        return None
    range_indices = [s["index"] for s in swings if abs(s["price"] - resistance) <= tolerance
                     or abs(s["price"] - support) <= tolerance]
    if not range_indices:
        return None
    first_idx = min(range_indices)
    last_idx = max(range_indices)
    duration = last_idx - first_idx
    if duration < min_bars:
        return None
    return {
        "support": round(support, 2), "resistance": round(resistance, 2),
        "range_height": round(range_height, 2),
        "range_height_pct": round(range_height / ((support + resistance) / 2) * 100, 2),
        "duration_bars": duration, "touch_count": touch_count, "is_clear_range": True,
        "support_idx": first_idx, "resistance_idx": last_idx,
    }


def _cluster_peak(prices: list, tolerance: float) -> float | None:
    """Find most dense price cluster within tolerance, return its center."""
    if not prices:
        return None
    best_count = 0
    best_price = prices[0]
    for p in prices:
        count = sum(1 for x in prices if abs(x - p) <= tolerance)
        if count > best_count:
            best_count = count
            best_price = p
    return best_price


def _median(values: list) -> float | None:
    """Compute median of non-None values."""
    vals = sorted([v for v in values if v is not None])
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
