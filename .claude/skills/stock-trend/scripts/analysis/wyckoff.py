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


DEFAULT_VOL_MA_PERIOD = 50


def analyze_vsa(ohlcv: dict, atr_values: list, ma50: list | None = None) -> list[dict]:
    """Volume Spread Analysis: detect effort-vs-result divergences.

    Analyzes each bar for VSA signals: absorption, no supply, no demand,
    stopping volume, and upthrust patterns.

    Returns list of signal dicts sorted by bar_index ascending.
    """
    closes = ohlcv["close"]
    highs = ohlcv["high"]
    lows = ohlcv["low"]
    opens = ohlcv["open"]
    volumes = ohlcv["volume"]

    if ma50 is None:
        ma50 = compute_ma(volumes, DEFAULT_VOL_MA_PERIOD)

    signals = []

    for i in range(1, len(closes)):
        atr = atr_values[i]
        if atr is None or atr == 0:
            continue
        vol = volumes[i]
        vol_ma_val = ma50[i] if ma50 and i < len(ma50) else _ma_of_last_n(volumes, i, 50)
        if vol_ma_val is None or vol_ma_val == 0:
            continue
        vol_ratio = vol / vol_ma_val
        spread = highs[i] - lows[i]
        spread_ratio = spread / atr
        if spread == 0:
            continue

        upper_shadow = highs[i] - max(closes[i], opens[i])
        lower_shadow = min(closes[i], opens[i]) - lows[i]
        shadow_upper_ratio = upper_shadow / spread
        shadow_lower_ratio = lower_shadow / spread
        close_position = (closes[i] - lows[i]) / spread

        # Absorption: wide spread, high volume, close mid-range
        if vol_ratio > 1.5 and spread_ratio > 0.8 and 0.3 <= close_position <= 0.7:
            strength = min(3, int(vol_ratio * 1.5))
            signals.append({
                "type": "absorption",
                "sub_type": "effort_no_result",
                "strength": strength,
                "bar_index": i,
                "description": f"放量震仓，主力吸筹特征 (vol={vol_ratio:.1f}x)",
            })

        # No Supply: narrow spread down, low volume
        if spread_ratio < 0.6 and closes[i] < opens[i] and vol_ratio < 0.7:
            strength = min(3, max(1, int((1 - vol_ratio) * 5)))
            signals.append({
                "type": "no_supply",
                "sub_type": "supply_exhaustion",
                "strength": strength,
                "bar_index": i,
                "description": f"缩量下跌，抛压枯竭 (vol={vol_ratio:.1f}x)",
            })

        # No Demand: narrow spread up, low volume
        if spread_ratio < 0.6 and closes[i] > opens[i] and vol_ratio < 0.7:
            strength = min(3, max(1, int((1 - vol_ratio) * 5)))
            signals.append({
                "type": "no_demand",
                "sub_type": "demand_exhaustion",
                "strength": strength,
                "bar_index": i,
                "description": f"缩量上涨，买盘不足 (vol={vol_ratio:.1f}x)",
            })

        # Stopping Volume: high vol, close high, lower shadow
        if vol_ratio > 1.8 and closes[i] < opens[i] and close_position > 0.6 and shadow_lower_ratio > 0.3:
            strength = min(3, int(vol_ratio * 1.2))
            signals.append({
                "type": "stopping_volume",
                "sub_type": "selling_climax",
                "strength": strength,
                "bar_index": i,
                "description": f"放量下跌+长下影，止跌量出现 (vol={vol_ratio:.1f}x)",
            })

        # Upthrust: narrow spread, high close, upper shadow, high vol
        if vol_ratio > 1.3 and spread_ratio < 0.7 and closes[i] > opens[i] and shadow_upper_ratio > 0.4:
            strength = min(3, max(1, int(vol_ratio)))
            signals.append({
                "type": "upthrust",
                "sub_type": "effort_no_result",
                "strength": strength,
                "bar_index": i,
                "description": f"放量窄幅+上影，上冲受阻 (vol={vol_ratio:.1f}x)",
            })

    return signals


def compute_cause_effect(trading_range: dict, current_price: float) -> dict:
    """Wyckoff 'Cause leads to Effect' quantification.

    Horizontal count: duration -> time projection.
    Vertical count: range height -> price targets.
    """
    support = trading_range["support"]
    resistance = trading_range["resistance"]
    height = resistance - support
    duration = trading_range["duration_bars"]

    time_projection = max(5, int(duration * 0.5))

    if current_price > resistance:
        target1 = current_price + height
        target2 = current_price + height * 1.5
        target3 = current_price + height * 2.0
        direction_label = "吸筹"
    elif current_price < support:
        target1 = current_price - height
        target2 = current_price - height * 1.5
        target3 = current_price - height * 2.0
        direction_label = "派发"
    else:
        return {
            "horizontal_count": duration,
            "vertical_height": round(height, 2),
            "targets": [],
            "time_projection_days": time_projection,
            "cause_description": f"箱体内震荡 {duration} 根 K 线，高度 {height:.2f}，等待突破确认",
        }

    return {
        "horizontal_count": duration,
        "vertical_height": round(height, 2),
        "targets": [
            {"level": 1, "price": round(target1, 2), "ratio": 1.0},
            {"level": 2, "price": round(target2, 2), "ratio": 1.5},
            {"level": 3, "price": round(target3, 2), "ratio": 2.0},
        ],
        "time_projection_days": time_projection,
        "cause_description": (
            f"{duration} 根 K 线横盘{direction_label}"
            f"，箱体高度 {height:.2f} ({height / ((support + resistance) / 2) * 100:.1f}%)"
        ),
    }


def wyckoff_score(phase: str, sub_phase: str) -> float:
    """Map phase/sub_phase to score in [-3, +3] range."""
    key = (phase, sub_phase)
    score = PHASE_SCORES.get(key, DEFAULT_PHASE_SCORE)
    return max(-3.0, min(3.0, score))


def generate_trading_implication(phase: str, sub_phase: str) -> str:
    """Generate human-readable trading implication based on Wyckoff signals."""
    if phase == PHASE_UNKNOWN:
        return "当前无明显维科夫阶段特征，暂无法提供操作参考。"

    if phase == PHASE_ACCUMULATION:
        implications = {
            SUB_SC: "抛售高潮出现，卖压集中释放，短期可能形成低点区域。不宜追空，等待二次测试确认。",
            SUB_AR: "自动反弹阶段，卖压暂缓。观察反弹量能，若缩量则可能再次测试支撑。",
            SUB_ST: "二次测试缩量确认支撑，吸筹信号增强。关注后续能否放量突破箱体。",
            SUB_SPRING: "初支（Spring）形态，短暂击穿支撑后快速收回，主力试盘特征。可考虑轻仓试多。",
            SUB_LPS: "最后支撑点附近缩量止跌，吸筹接近尾声。做好突破入场准备。",
            SUB_PRE_MARKUP: "拉升前准备阶段，震荡收窄、成交量极度萎缩。等待放量突破信号。",
        }
        return implications.get(sub_phase, "吸筹阶段运行中，以箱体上下沿作为关键边界。")

    if phase == PHASE_MARKUP:
        implications = {
            SUB_JAC: "JAC（跃过小溪）放量突破箱体，趋势确认。可顺势做多，以箱顶作为止损参考。",
            SUB_BU: "回踩箱顶获支撑，缩量整理。突破确认后的健康回调，可考虑加仓。",
            SUB_CONTINUATION: "持续拉升阶段。顺应趋势持有，跟踪止盈，不逆势猜顶。",
        }
        return implications.get(sub_phase, "拉升阶段，多头持仓为主，注意跟踪趋势力度变化。")

    if phase == PHASE_DISTRIBUTION:
        implications = {
            SUB_BC: "买入高潮（BC）出现，巨量长上影，主力派发特征。应逐步减仓。",
            SUB_UTAD: "UTAD（上冲回落）假突破，量价背离。派发确认信号，建议减仓或离场。",
            SUB_LPSY: "最后供应点（LPSY），反弹缩量无力。清仓为主，不再做多。",
            SUB_SOW: "弱势信号（SOW），跌破支撑或测试支撑时量大。离场观望。",
            SUB_PRE_MARKDOWN: "砸盘前兆。全面离场，准备做空或空仓。",
        }
        return implications.get(sub_phase, "派发阶段运行中，以减仓和控制风险为主。")

    if phase == PHASE_MARKDOWN:
        implications = {
            SUB_BREAKDOWN: "破位下跌，趋势转空。多单离场，不抄底。",
            SUB_PANIC: "恐慌抛售阶段，放量急跌。空仓等待，不接飞刀。",
            SUB_STOPPING_VOL: "止跌量出现，抛压衰竭信号。关注是否形成新的吸筹区间。",
        }
        return implications.get(sub_phase, "砸盘阶段，空仓观望，等待止跌企稳信号。")

    return ""


def analyze(kline_path: str, output_path: str | None = None) -> dict:
    """Run full Wyckoff analysis pipeline.

    Args:
        kline_path: Path to K-line JSON file.
        output_path: Optional path to write output JSON.

    Returns:
        wyckoff_analysis dict with phase, range, VSA, cause_effect, score.
    """
    kline_data = load_kline(kline_path)
    if kline_data is None:
        return {"meta": {"error": "failed to load K-line data"}, "phase": {"primary": PHASE_UNKNOWN}}

    data_rows = kline_data.get("data", [])
    if not data_rows:
        return {"meta": {"error": "empty K-line data"}, "phase": {"primary": PHASE_UNKNOWN}}

    kline_meta = kline_data.get("meta", {})
    ohlcv = extract_ohlcv(data_rows)
    closes = ohlcv["close"]
    highs = ohlcv["high"]
    lows = ohlcv["low"]
    volumes = ohlcv["volume"]

    if len(closes) < 30:
        return {"meta": {"error": f"insufficient data ({len(closes)} rows)"}, "phase": {"primary": PHASE_UNKNOWN}}

    atr_values = compute_atr(highs, lows, closes)

    swings = detect_swing_points(closes, highs, lows, volumes, atr_values)
    swings = mark_climaxes(swings, highs, lows, closes, volumes, atr_values)
    for s in swings:
        idx = s["index"]
        if idx < len(ohlcv["date"]):
            s["date"] = ohlcv["date"][idx]

    trading_range = detect_trading_range(swings, closes, atr_values)

    latest_idx = len(closes) - 1
    phase = PHASE_UNKNOWN
    sub_phase = ""
    confidence = 0.0

    if trading_range:
        result = classify_accumulation(swings, closes, volumes, lows, highs,
                                       trading_range, atr_values, latest_idx)
        if result:
            sub_phase, confidence = result
            phase = PHASE_ACCUMULATION

        if not result:
            result = classify_distribution(swings, closes, volumes, lows, highs,
                                           trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_DISTRIBUTION

        if not result:
            result = classify_markup(swings, closes, volumes, highs,
                                     trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_MARKUP

        if phase == PHASE_UNKNOWN:
            result = classify_markdown(swings, closes, volumes, lows, highs,
                                       trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_MARKDOWN
    else:
        if len(closes) >= 50:
            ma20 = compute_ma(closes, 20)
            ma60 = compute_ma(closes, 60)
            if ma20[-1] and ma60[-1] and closes[-1] > ma20[-1] and ma20[-1] > ma60[-1]:
                phase = PHASE_MARKUP
                sub_phase = SUB_CONTINUATION
                confidence = 0.3
            elif ma20[-1] and ma60[-1] and closes[-1] < ma20[-1] and ma20[-1] < ma60[-1]:
                phase = PHASE_MARKDOWN
                sub_phase = SUB_BREAKDOWN
                confidence = 0.3

    if phase == PHASE_UNKNOWN and trading_range:
        confidence = 0.3

    vsa_signals = analyze_vsa(ohlcv, atr_values)

    current_price = closes[-1]
    cause_effect = compute_cause_effect(trading_range, current_price) if trading_range else {}

    score = wyckoff_score(phase, sub_phase) if sub_phase else DEFAULT_PHASE_SCORE

    key_signals = []
    if trading_range:
        key_signals.append(f"箱体支撑 {trading_range['support']:.2f} / 阻力 {trading_range['resistance']:.2f}")
    if sub_phase:
        sub_name = SUB_PHASE_NAMES.get(sub_phase, sub_phase)
        key_signals.append(f"子阶段: {sub_name}")
    for vs in vsa_signals[-3:]:
        key_signals.append(vs["description"])

    vsa_signals_sorted = sorted(vsa_signals, key=lambda s: s["bar_index"], reverse=True)

    result = {
        "meta": {
            "ts_code": kline_meta.get("ts_code", ""),
            "name": kline_meta.get("name", ""),
            "calc_date": kline_meta.get("end_date", ""),
            "kline_days": len(closes),
            "data_quality": "good" if len(closes) >= 150 else ("limited" if len(closes) >= 60 else "insufficient"),
        },
        "phase": {
            "primary": phase,
            "primary_name": PHASE_NAMES.get(phase, "未知阶段"),
            "confidence": round(confidence, 2),
            "secondary_possibilities": [],
            "primary_sub_phase": sub_phase,
            "sub_phase_name": SUB_PHASE_NAMES.get(sub_phase, ""),
        },
        "range": trading_range or {"is_clear_range": False},
        "swing_points": swings[-20:],
        "vsa_signals": vsa_signals_sorted[:10],
        "cause_effect": cause_effect,
        "wyckoff_score": round(score, 2),
        "wyckoff_signals": {
            "verdict": (
                "bullish" if score > 1.0 else
                "cautiously_bullish" if score > 0 else
                "bearish" if score < -1.0 else
                "cautiously_bearish" if score < 0 else
                "neutral"
            ),
            "key_signals": key_signals[-5:],
            "trading_implication": generate_trading_implication(phase, sub_phase),
        },
    }

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def main():
    """CLI entry point: python3 wyckoff.py <kline_json> [-o <output_path>]"""
    if len(sys.argv) < 2:
        print("Usage: python3 wyckoff.py <kline_json> [-o <output_path>]", file=sys.stderr)
        sys.exit(1)
    kline_path = sys.argv[1]
    output_path = None
    if "-o" in sys.argv:
        idx = sys.argv.index("-o")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]
    result = analyze(kline_path, output_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
