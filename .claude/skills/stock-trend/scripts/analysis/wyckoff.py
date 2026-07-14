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

# Maximum lookback for finding breakout
FIND_BREAKOUT_MAX_BARS = 60


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


def classify_accumulation(swings: list, closes: list, volumes: list, lows: list,
                           highs: list, trading_range: dict | None, atr_values: list,
                           latest_idx: int) -> tuple | None:
    """Detect accumulation phase and sub-phase.

    Returns (sub_phase, confidence) or None if not in accumulation.
    """
    if trading_range is None:
        return None
    range_support = trading_range["support"]
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0
    latest_vol = volumes[latest_idx]

    if latest_close > range_resistance + latest_atr * 1.0:
        return None
    if latest_close < range_support - latest_atr * 1.0:
        return None

    recent_swing_lows = [s for s in swings if s["type"] == "low"
                         and s["index"] > trading_range["resistance_idx"] * 0.7
                         and s["price"] >= range_support - latest_atr * 2
                         and s["price"] <= range_resistance + latest_atr * 2]
    if not recent_swing_lows:
        near_support = abs(latest_close - range_support) <= latest_atr * 0.5
        if near_support and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.7:
            return (SUB_LPS, 0.6)
        return None

    latest_swing_low = recent_swing_lows[-1]
    if latest_swing_low["is_climax"] and latest_swing_low.get("climax_type") == "selling":
        return (SUB_SC, 0.8)

    if len(recent_swing_lows) >= 1:
        sc_swings = [s for s in recent_swing_lows if s.get("is_climax")]
        if sc_swings:
            sc_idx = sc_swings[-1]["index"]
            bars_since_sc = latest_idx - sc_idx
            if bars_since_sc <= 10 and latest_close > range_support + (range_resistance - range_support) * 0.3:
                return (SUB_AR, 0.7)

    if len(recent_swing_lows) >= 2:
        prev_low = recent_swing_lows[-2]
        curr_low = latest_swing_low
        if (curr_low["volume_ratio"] < prev_low["volume_ratio"] * 0.7
                and abs(curr_low["price"] - prev_low["price"]) <= latest_atr * 2):
            return (SUB_ST, 0.8)

    spring_candidates = [s for s in recent_swing_lows
                         if s["price"] < range_support - latest_atr * 0.3
                         and s["price"] >= range_support - latest_atr * 2.0]
    if spring_candidates and any(s["volume_ratio"] > 1.5 for s in spring_candidates):
        return (SUB_SPRING, 0.7)

    near_support = abs(latest_close - range_support) <= latest_atr * 0.5
    if near_support and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPS, 0.7)

    return (SUB_LPS, 0.5)


def classify_markup(swings: list, closes: list, volumes: list, highs: list,
                     trading_range: dict | None, atr_values: list,
                     latest_idx: int) -> tuple | None:
    """Detect markup phase after breakout from accumulation range."""
    if trading_range is None:
        return None
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    if latest_close <= range_resistance:
        return None

    trend_high = max(closes[max(0, latest_idx - 20) : latest_idx + 1])
    retrace_from_high = (trend_high - latest_close) / latest_atr if latest_atr > 0 else 0
    bars_since_breakout = _find_first_breakout_bar(closes, trading_range, latest_idx)

    if bars_since_breakout is not None and bars_since_breakout <= 5:
        breakout_volumes = volumes[latest_idx - bars_since_breakout : latest_idx + 1]
        avg_vol = sum(breakout_volumes) / len(breakout_volumes) if breakout_volumes else 0
        baseline_vol = _ma_of_last_n(volumes, latest_idx - bars_since_breakout, 50) if latest_idx - bars_since_breakout >= 50 else 1
        if avg_vol > baseline_vol * 1.3:
            return (SUB_JAC, 0.8)
        return (SUB_JAC, 0.5)

    if retrace_from_high <= 2.0 and retrace_from_high >= 0.5:
        pullback_vol = volumes[latest_idx]
        if pullback_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.8:
            return (SUB_BU, 0.7)
        return (SUB_BU, 0.5)

    return (SUB_CONTINUATION, 0.6)


def _find_first_breakout_bar(closes: list, trading_range: dict, latest_idx: int) -> int | None:
    """Find how many bars ago the price first closed above resistance.

    Returns bar offset (0 = today, 1 = yesterday, etc.) or None if price
    has never been above resistance in the lookback window.
    """
    resistance = trading_range["resistance"]
    lookback = min(latest_idx, FIND_BREAKOUT_MAX_BARS)
    for offset in range(lookback):
        idx = latest_idx - offset
        if idx < 0:
            return None
        if closes[idx] <= resistance:
            return offset - 1 if offset > 0 else None
    return FIND_BREAKOUT_MAX_BARS  # Sentinal: breakout happened > N bars ago


def classify_distribution(swings: list, closes: list, volumes: list,
                            lows: list, highs: list, trading_range: dict | None,
                            atr_values: list, latest_idx: int) -> tuple | None:
    """Detect distribution phase."""
    if trading_range is None:
        return None
    range_support = trading_range["support"]
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    if latest_close < range_support - latest_atr * 1.0:
        return None
    if latest_close < range_support - latest_atr * 0.5:
        return (SUB_SOW, 0.6)

    recent_swing_highs = [s for s in swings if s["type"] == "high"
                          and s["index"] > trading_range["resistance_idx"] * 0.7
                          and s["price"] >= range_support - latest_atr * 2
                          and s["price"] <= range_resistance + latest_atr * 2]
    if not recent_swing_highs:
        return None

    latest_swing_high = recent_swing_highs[-1]

    if latest_swing_high["is_climax"] and latest_swing_high.get("climax_type") == "buying":
        return (SUB_BC, 0.8)

    utad_candidates = [s for s in recent_swing_highs
                       if s["price"] > range_resistance + latest_atr * 0.3
                       and latest_close < range_resistance + latest_atr * 0.3]
    if utad_candidates:
        has_climax = any(s.get("is_climax") for s in utad_candidates)
        return (SUB_UTAD, 0.75 if has_climax else 0.55)

    near_resistance = abs(latest_close - range_resistance) <= latest_atr * 0.5
    if near_resistance and volumes[latest_idx] < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPSY, 0.65)

    near_support = abs(latest_close - range_support) <= latest_atr * 0.5
    if near_support:
        return (SUB_SOW, 0.5)

    return (SUB_LPSY, 0.4)


def classify_markdown(swings: list, closes: list, volumes: list, lows: list,
                       highs: list, trading_range: dict | None, atr_values: list,
                       latest_idx: int) -> tuple | None:
    """Detect markdown phase."""
    if trading_range is None:
        return None
    range_support = trading_range["support"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    if latest_close >= range_support:
        return None

    range_under = range_support - latest_close
    if range_under <= latest_atr * 2.0 and range_under > latest_atr * 0.5:
        return (SUB_BREAKDOWN, 0.7)

    if len(closes) >= 10:
        recent_returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
                          for i in range(max(latest_idx - 10, 1), latest_idx + 1)]
        avg_return = sum(recent_returns) / len(recent_returns)
        if avg_return < -0.02 and volumes[latest_idx] > _ma_of_last_n(volumes, latest_idx, 50) * 1.5:
            return (SUB_PANIC, 0.75)

    if volumes[latest_idx] > _ma_of_last_n(volumes, latest_idx, 50) * 1.5:
        daily_range = highs[latest_idx] - lows[latest_idx]
        if daily_range < latest_atr * 0.7:
            return (SUB_STOPPING_VOL, 0.6)

    return (SUB_BREAKDOWN, 0.4)
