#!/usr/bin/env python3
"""Technical indicator calculator and K-line pattern recognizer for stock-trend skill.

Usage:
    python3 analyze_technical.py [input_file] [options]
    python3 fetch_kline.py 600519.SH | python3 analyze_technical.py

Examples:
    python3 analyze_technical.py /tmp/kline.json
    python3 analyze_technical.py /tmp/kline.json -o /tmp/technical.json
    python3 analyze_technical.py /tmp/kline.json --compact
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from core.cache_utils import output_json


# --- MA signal analysis ---


def calc_ma_signals(df, periods=None):
    """Analyze MA alignment, crossovers, and proximity."""
    if periods is None:
        periods = [5, 10, 20, 60]

    ma_cols = {}
    for p in periods:
        col = f"ma{p}"
        if col in df.columns:
            ma_cols[p] = df[col].iloc[-1]
        elif len(df) >= p:
            ma_cols[p] = df["close"].rolling(p).mean().iloc[-1]
        else:
            ma_cols[p] = None

    # Filter out None values
    available = {k: v for k, v in ma_cols.items() if v is not None and not (isinstance(v, float) and v != v)}
    if len(available) < 2:
        return {"values": ma_cols, "signal": {"type": "insufficient_data", "description": "均线数据不足", "score": 0}}

    # Check alignment
    sorted_vals = [available[k] for k in sorted(available.keys())]
    keys_sorted = sorted(available.keys())

    bullish_align = all(sorted_vals[i] > sorted_vals[i + 1] for i in range(len(sorted_vals) - 1))
    bearish_align = all(sorted_vals[i] < sorted_vals[i + 1] for i in range(len(sorted_vals) - 1))

    score = 0
    desc_parts = []
    alignment = "mixed"

    if bullish_align:
        alignment = "bullish"
        score = 2 if len(available) >= 4 else 1
        desc_parts.append("多头排列")
    elif bearish_align:
        alignment = "bearish"
        score = -2 if len(available) >= 4 else -1
        desc_parts.append("空头排列")

    # Check crossovers (look at last 2 data points for MA5 vs MA10)
    if 5 in available and 10 in available and len(df) >= 2:
        ma5_vals = df["ma5"] if "ma5" in df.columns else df["close"].rolling(5).mean()
        ma10_vals = df["ma10"] if "ma10" in df.columns else df["close"].rolling(10).mean()

        if len(ma5_vals) >= 2 and len(ma10_vals) >= 2:
            prev_ma5, curr_ma5 = ma5_vals.iloc[-2], ma5_vals.iloc[-1]
            prev_ma10, curr_ma10 = ma10_vals.iloc[-2], ma10_vals.iloc[-1]

            if not (pd.isna(prev_ma5) or pd.isna(prev_ma10)):
                if prev_ma5 <= prev_ma10 and curr_ma5 > curr_ma10:
                    score += 1
                    desc_parts.append("MA5上穿MA10金叉")
                elif prev_ma5 >= prev_ma10 and curr_ma5 < curr_ma10:
                    score -= 1
                    desc_parts.append("MA5下穿MA10死叉")

    score = max(-3, min(3, score))
    if not desc_parts:
        desc_parts.append("均线缠绕，方向不明")

    return {
        "values": {f"ma{k}": round(v, 4) if v is not None else None for k, v in ma_cols.items()},
        "alignment": alignment,
        "signal": {
            "type": f"{alignment}_align" if alignment != "mixed" else "mixed",
            "description": "；".join(desc_parts),
            "score": score,
        },
    }


# --- MACD ---


def calc_macd(df, fast=12, slow=26, signal=9):
    """Compute MACD (DIF, DEA, histogram) and detect signals."""
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    histogram = (dif - dea) * 2

    curr_dif = dif.iloc[-1]
    curr_dea = dea.iloc[-1]
    curr_hist = histogram.iloc[-1]

    score = 0
    desc_parts = []

    # Cross detection
    if len(dif) >= 2 and len(dea) >= 2:
        prev_dif, prev_dea = dif.iloc[-2], dea.iloc[-2]
        if prev_dif <= prev_dea and curr_dif > curr_dea:
            desc_parts.append("MACD金叉")
            score += 1
        elif prev_dif >= prev_dea and curr_dif < curr_dea:
            desc_parts.append("MACD死叉")
            score -= 1

    # Histogram direction
    if curr_hist > 0:
        if len(histogram) >= 2 and curr_hist > histogram.iloc[-2]:
            desc_parts.append("红柱放大")
            score += 1
        else:
            desc_parts.append("红柱缩窄")
    elif curr_hist < 0:
        if len(histogram) >= 2 and curr_hist < histogram.iloc[-2]:
            desc_parts.append("绿柱放大")
            score -= 1
        else:
            desc_parts.append("绿柱缩窄")

    # Divergence detection using peak/valley matching
    if len(df) >= 30:
        div_type, div_score = _detect_divergence(df["close"], dif, lookback=60, min_distance=5)
        if div_type == "bearish":
            desc_parts.append("顶背离")
            score += div_score
        elif div_type == "bullish":
            desc_parts.append("底背离")
            score += div_score

    score = max(-3, min(3, score))
    if not desc_parts:
        desc_parts.append("MACD中性")

    return {
        "dif": round(curr_dif, 4),
        "dea": round(curr_dea, 4),
        "histogram": round(curr_hist, 4),
        "signal": {
            "type": "golden_cross" if "金叉" in "；".join(desc_parts) else ("death_cross" if "死叉" in "；".join(desc_parts) else "neutral"),
            "description": "；".join(desc_parts),
            "score": score,
        },
    }


# --- RSI ---


def calc_rsi(df, period=14):
    """Compute RSI and classify signal."""
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.inf)
    rsi = 100 - (100 / (1 + rs))

    curr_rsi = rsi.iloc[-1]
    if pd.isna(curr_rsi):
        return {"rsi14": None, "signal": {"type": "insufficient_data", "description": "RSI数据不足", "score": 0}}

    score = 0
    desc = ""

    if curr_rsi > 70:
        score = -1 if curr_rsi < 80 else -2
        desc = f"RSI={curr_rsi:.1f}，超买"
    elif curr_rsi < 30:
        score = 1 if curr_rsi > 20 else 2
        desc = f"RSI={curr_rsi:.1f}，超卖"
    else:
        desc = f"RSI={curr_rsi:.1f}，中性区间"

    # Divergence using peak/valley matching
    if len(df) >= 30:
        div_type, div_score = _detect_divergence(df["close"], rsi, lookback=40, min_distance=5)
        if div_type == "bearish":
            desc += "；顶背离"
            score += div_score
        elif div_type == "bullish":
            desc += "；底背离"
            score += div_score

    score = max(-3, min(3, score))

    return {
        "rsi14": round(curr_rsi, 2),
        "signal": {
            "type": "overbought" if curr_rsi > 70 else ("oversold" if curr_rsi < 30 else "neutral"),
            "description": desc,
            "score": score,
        },
    }


# --- KDJ ---


def calc_kdj(df, n=9, m1=3, m2=3):
    """Compute KDJ and detect signals."""
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100

    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d

    curr_k = k.iloc[-1]
    curr_d = d.iloc[-1]
    curr_j = j.iloc[-1]

    if pd.isna(curr_k) or pd.isna(curr_d):
        return {"k": None, "d": None, "j": None, "signal": {"type": "insufficient_data", "description": "KDJ数据不足", "score": 0}}

    score = 0
    desc_parts = []

    # Cross detection
    if len(k) >= 2 and len(d) >= 2:
        prev_k, prev_d = k.iloc[-2], d.iloc[-2]
        if prev_k <= prev_d and curr_k > curr_d:
            if curr_k < 20:
                score = 2
                desc_parts.append(f"低位金叉(K={curr_k:.1f})")
            else:
                score = 1
                desc_parts.append(f"金叉(K={curr_k:.1f})")
        elif prev_k >= prev_d and curr_k < curr_d:
            if curr_k > 80:
                score = -2
                desc_parts.append(f"高位死叉(K={curr_k:.1f})")
            else:
                score = -1
                desc_parts.append(f"死叉(K={curr_k:.1f})")

    # Zone classification
    if curr_k > 80 and "死叉" not in "；".join(desc_parts):
        score -= 1
        desc_parts.append("超买区")
    elif curr_k < 20 and "金叉" not in "；".join(desc_parts):
        score += 1
        desc_parts.append("超卖区")

    score = max(-3, min(3, score))
    if not desc_parts:
        desc_parts.append(f"KDJ中性区域(K={curr_k:.1f},D={curr_d:.1f},J={curr_j:.1f})")

    return {
        "k": round(curr_k, 2),
        "d": round(curr_d, 2),
        "j": round(curr_j, 2),
        "signal": {
            "type": "golden_cross" if "金叉" in "；".join(desc_parts) else ("death_cross" if "死叉" in "；".join(desc_parts) else "neutral"),
            "description": "；".join(desc_parts),
            "score": score,
        },
    }


# --- Peak/Valley detection for divergence ---


def _find_peaks(series, min_distance=5):
    """Find local peaks in a series. Returns list of (index, value) tuples."""
    peaks = []
    for i in range(min_distance, len(series) - min_distance):
        window = series.iloc[i - min_distance:i + min_distance + 1]
        if series.iloc[i] == window.max() and series.iloc[i] > series.iloc[i - 1]:
            peaks.append((i, series.iloc[i]))
    return peaks


def _find_valleys(series, min_distance=5):
    """Find local valleys in a series. Returns list of (index, value) tuples."""
    valleys = []
    for i in range(min_distance, len(series) - min_distance):
        window = series.iloc[i - min_distance:i + min_distance + 1]
        if series.iloc[i] == window.min() and series.iloc[i] < series.iloc[i - 1]:
            valleys.append((i, series.iloc[i]))
    return valleys


def _detect_divergence(price_series, indicator_series, lookback=60, min_distance=5):
    """Detect divergence between price and indicator using peak/valley matching.

    Returns: (divergence_type, score) or (None, 0)
    - "bullish" = price making lower lows while indicator making higher lows
    - "bearish" = price making higher highs while indicator making lower highs
    """
    if len(price_series) < lookback:
        return None, 0

    price_recent = price_series.iloc[-lookback:]
    ind_recent = indicator_series.iloc[-lookback:]

    # Bearish divergence: price higher highs + indicator lower highs
    price_peaks = _find_peaks(price_recent, min_distance)
    ind_peaks = _find_peaks(ind_recent, min_distance)

    if len(price_peaks) >= 2 and len(ind_peaks) >= 2:
        last_two_pp = price_peaks[-2:]
        last_two_ip = ind_peaks[-2:]
        if last_two_pp[1][1] > last_two_pp[0][1] and last_two_ip[1][1] < last_two_ip[0][1]:
            return "bearish", -2

    # Bullish divergence: price lower lows + indicator higher lows
    price_valleys = _find_valleys(price_recent, min_distance)
    ind_valleys = _find_valleys(ind_recent, min_distance)

    if len(price_valleys) >= 2 and len(ind_valleys) >= 2:
        last_two_pv = price_valleys[-2:]
        last_two_iv = ind_valleys[-2:]
        if last_two_pv[1][1] < last_two_pv[0][1] and last_two_iv[1][1] > last_two_iv[0][1]:
            return "bullish", 2

    return None, 0


# --- ADX ---


def calc_adx(df, period=14):
    """Compute ADX (Average Directional Index) for trend strength assessment."""
    if len(df) < period * 2:
        return {"adx": None, "plus_di": None, "minus_di": None, "signal": {"type": "insufficient_data", "description": "ADX数据不足", "score": 0}}

    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    # Directional Movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0)

    # Smoothed averages
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(period).mean()

    curr_adx = adx.iloc[-1]
    curr_plus_di = plus_di.iloc[-1]
    curr_minus_di = minus_di.iloc[-1]

    if pd.isna(curr_adx):
        return {"adx": None, "plus_di": None, "minus_di": None, "signal": {"type": "insufficient_data", "description": "ADX数据不足", "score": 0}}

    score = 0
    desc = ""

    # Trend strength classification
    if curr_adx > 40:
        desc = f"ADX={curr_adx:.1f}，强趋势"
    elif curr_adx > 25:
        desc = f"ADX={curr_adx:.1f}，趋势有效"
    elif curr_adx > 20:
        desc = f"ADX={curr_adx:.1f}，趋势弱"
    else:
        desc = f"ADX={curr_adx:.1f}，震荡市"
        score = 0  # Neutral - no trend to follow

    # DI direction for trend direction confirmation
    if not pd.isna(curr_plus_di) and not pd.isna(curr_minus_di):
        if curr_plus_di > curr_minus_di and curr_adx > 25:
            score = 1  # Confirms bullish trend
            desc += "；+DI>-DI确认多头"
        elif curr_minus_di > curr_plus_di and curr_adx > 25:
            score = -1  # Confirms bearish trend
            desc += "；-DI>+DI确认空头"

    return {
        "adx": round(curr_adx, 2),
        "plus_di": round(curr_plus_di, 2) if not pd.isna(curr_plus_di) else None,
        "minus_di": round(curr_minus_di, 2) if not pd.isna(curr_minus_di) else None,
        "signal": {"type": "trend_strength", "description": desc, "score": score},
    }


# --- OBV ---


def calc_obv(df):
    """Compute On Balance Volume to detect volume-based money flow trends."""
    if "vol" not in df.columns or len(df) < 20:
        return {"obv": None, "signal": {"type": "insufficient_data", "description": "OBV数据不足", "score": 0}}

    close = df["close"]
    vol = df["vol"]

    direction = (close > close.shift(1)).astype(float) - (close < close.shift(1)).astype(float)
    obv = (vol * direction).cumsum()

    curr_obv = obv.iloc[-1]
    if pd.isna(curr_obv):
        return {"obv": None, "signal": {"type": "insufficient_data", "description": "OBV数据不足", "score": 0}}

    # OBV trend: compare 20-period MA
    obv_ma20 = obv.rolling(20).mean()
    score = 0
    desc_parts = []

    if not pd.isna(obv_ma20.iloc[-1]):
        if curr_obv > obv_ma20.iloc[-1]:
            score = 1
            desc_parts.append("OBV在20日均线上方，资金净流入")
        else:
            score = -1
            desc_parts.append("OBV在20日均线下方，资金净流出")

    # OBV divergence with price
    div_type, div_score = _detect_divergence(close, obv, lookback=40, min_distance=5)
    if div_type == "bullish":
        desc_parts.append("OBV底背离（量先价行，可能反转向上）")
        score += 1
    elif div_type == "bearish":
        desc_parts.append("OBV顶背离（量先价行，可能反转向下）")
        score -= 1

    score = max(-3, min(3, score))
    if not desc_parts:
        desc_parts.append("OBV中性")

    return {
        "obv": round(curr_obv, 0),
        "signal": {"type": "money_flow", "description": "；".join(desc_parts), "score": score},
    }


# --- Bollinger Bands ---


def calc_bollinger(df, period=20, std_dev=2):
    """Compute Bollinger Bands and detect signals."""
    close = df["close"]
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std

    curr_close = close.iloc[-1]
    curr_upper = upper.iloc[-1]
    curr_middle = middle.iloc[-1]
    curr_lower = lower.iloc[-1]

    if pd.isna(curr_middle):
        return {"upper": None, "middle": None, "lower": None, "signal": {"type": "insufficient_data", "description": "布林带数据不足", "score": 0}}

    score = 0
    desc_parts = []

    # Position relative to bands
    if curr_close > curr_upper:
        desc_parts.append("突破上轨")
        # Check volume for confirmation
        if "vol" in df.columns and len(df) >= 2:
            if df["vol"].iloc[-1] > df["vol"].iloc[-2] * 1.5:
                score = 2
                desc_parts.append("放量确认")
            else:
                score = 1
        else:
            score = 1
    elif curr_close < curr_lower:
        score = -1
        desc_parts.append("跌破下轨")
    elif curr_close > curr_middle:
        score = 1
        desc_parts.append("中轨上方运行")
    else:
        desc_parts.append("中轨下方运行")

    # Squeeze detection using bandwidth percentile
    if len(df) >= period * 2:
        all_widths = ((upper - lower) / middle).dropna()
        band_width = (curr_upper - curr_lower) / curr_middle if curr_middle > 0 else 0
        if len(all_widths) >= 20 and not pd.isna(band_width):
            bandwidth_percentile = (all_widths < band_width).sum() / len(all_widths) * 100
            if bandwidth_percentile < 20:
                desc_parts.append(f"布林带极度收口({bandwidth_percentile:.0f}%分位，即将变盘)")
                score = 0  # Squeeze = neutral, watch for direction
            elif bandwidth_percentile < 40:
                desc_parts.append(f"布林带收口({bandwidth_percentile:.0f}%分位)")
                score = 0

    score = max(-3, min(3, score))

    return {
        "upper": round(curr_upper, 4),
        "middle": round(curr_middle, 4),
        "lower": round(curr_lower, 4),
        "signal": {
            "type": "breakout_upper" if curr_close > curr_upper else ("breakout_lower" if curr_close < curr_lower else "middle_support"),
            "description": "；".join(desc_parts),
            "score": score,
        },
    }


# --- Volume analysis ---


def analyze_volume(df):
    """Analyze volume-price relationship."""
    if "vol" not in df.columns or len(df) < 2:
        return {"vol": None, "signal": {"type": "insufficient_data", "description": "成交量数据不足", "score": 0}}

    vol = df["vol"]
    close = df["close"]
    curr_vol = vol.iloc[-1]

    vol_ma20 = vol.rolling(20).mean().iloc[-1] if len(vol) >= 20 else vol.mean()

    if pd.isna(curr_vol) or pd.isna(vol_ma20) or vol_ma20 == 0:
        return {"vol": None, "signal": {"type": "insufficient_data", "description": "成交量数据不足", "score": 0}}

    vol_ratio = curr_vol / vol_ma20
    price_chg = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] if len(close) >= 2 else 0

    score = 0
    desc = ""

    if price_chg > 0 and vol_ratio > 1.3:
        score = 2
        desc = f"放量上涨(量比{vol_ratio:.2f})"
    elif price_chg > 0 and vol_ratio < 0.7:
        score = -1
        desc = f"缩量上涨(量比{vol_ratio:.2f})，趋势弱化"
    elif price_chg < 0 and vol_ratio < 0.7:
        score = -1
        desc = f"缩量下跌(量比{vol_ratio:.2f})"
    elif price_chg < 0 and vol_ratio > 1.5:
        score = -2
        desc = f"放量下跌(量比{vol_ratio:.2f})，恐慌信号"
    else:
        desc = f"量价中性(量比{vol_ratio:.2f})"

    # Extreme volume detection
    if len(df) >= 60:
        vol_max = vol.rolling(60).max()
        if curr_vol >= vol_max.iloc[-1] * 0.95 and price_chg > 0:
            desc += "；天量天价风险"
            score = min(score, -2)
        elif curr_vol <= vol.rolling(60).min().iloc[-1] * 1.1 and price_chg < 0:
            desc += "；地量地价可能"
            score = max(score, 1)

    score = max(-3, min(3, score))

    return {
        "vol": round(curr_vol, 0),
        "vol_ma20": round(vol_ma20, 0),
        "vol_ratio": round(vol_ratio, 2),
        "signal": {"type": "volume_confirms" if score > 0 else ("volume_diverges" if score < 0 else "neutral"), "description": desc, "score": score},
    }


# --- K-line pattern recognition ---


def identify_trend(df, lookback=10):
    """Identify recent trend direction using MA5 vs MA20 and recent candles."""
    if len(df) < lookback:
        return "neutral"

    # Method 1: MA comparison
    if "ma5" in df.columns and "ma20" in df.columns:
        ma5 = df["ma5"].iloc[-1]
        ma20 = df["ma20"].iloc[-1]
        if not (pd.isna(ma5) or pd.isna(ma20)):
            if ma5 > ma20 * 1.005:
                return "uptrend"
            elif ma5 < ma20 * 0.995:
                return "downtrend"
            return "neutral"

    # Method 2: Count recent up/down candles
    recent = df.tail(lookback)
    up = sum(recent["close"] > recent["open"])
    down = sum(recent["close"] < recent["open"])
    if up >= lookback * 0.7:
        return "uptrend"
    elif down >= lookback * 0.7:
        return "downtrend"
    return "neutral"


def _candle_features(row):
    """Compute features for a single candle."""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    full_range = h - l if h != l else 1e-10
    is_yang = c > o

    return {
        "body": body,
        "upper_shadow": upper_shadow,
        "lower_shadow": lower_shadow,
        "full_range": full_range,
        "is_yang": is_yang,
        "body_ratio": body / full_range,
        "upper_ratio": upper_shadow / body if body > 0 else 10,
        "lower_ratio": lower_shadow / body if body > 0 else 10,
        "is_doji": body <= 0.05 * full_range,
        "is_small_body": body < 0.2 * full_range,
        "is_large_body": body > 0.6 * full_range,
    }


def scan_patterns(df, lookback=10):
    """Scan for K-line patterns in the last N candles."""
    n = min(lookback, len(df))
    if n < 3:
        return []

    tail = df.tail(n).reset_index(drop=True)
    trend = identify_trend(df)
    patterns = []

    # Scan from newest backwards
    i = n - 1

    # 3-candle patterns (highest priority)
    if i >= 2:
        f1 = _candle_features(tail.iloc[i - 2])
        f2 = _candle_features(tail.iloc[i - 1])
        f3 = _candle_features(tail.iloc[i])

        # Morning Star
        if trend in ("downtrend", "neutral") and not f1["is_yang"] and f1["is_large_body"] and f2["is_small_body"] and f3["is_yang"] and f3["is_large_body"]:
            c1_mid = (tail.iloc[i - 2]["open"] + tail.iloc[i - 2]["close"]) / 2
            if tail.iloc[i]["close"] > c1_mid:
                patterns.append({"name": "早晨之星", "name_en": "Morning Star", "direction": "bullish", "position": "downtrend_end", "score": 2, "candles": 3})

        # Evening Star
        if trend in ("uptrend", "neutral") and f1["is_yang"] and f1["is_large_body"] and f2["is_small_body"] and not f3["is_yang"] and f3["is_large_body"]:
            c1_mid = (tail.iloc[i - 2]["open"] + tail.iloc[i - 2]["close"]) / 2
            if tail.iloc[i]["close"] < c1_mid:
                patterns.append({"name": "黄昏之星", "name_en": "Evening Star", "direction": "bearish", "position": "uptrend_end", "score": -2, "candles": 3})

        # Three White Soldiers
        if trend in ("downtrend", "neutral"):
            c1_yang = tail.iloc[i - 2]["close"] > tail.iloc[i - 2]["open"]
            c2_yang = tail.iloc[i - 1]["close"] > tail.iloc[i - 1]["open"]
            c3_yang = tail.iloc[i]["close"] > tail.iloc[i]["open"]
            if c1_yang and c2_yang and c3_yang:
                if (tail.iloc[i - 1]["open"] >= tail.iloc[i - 2]["open"] and
                    tail.iloc[i - 1]["open"] <= tail.iloc[i - 2]["close"] and
                    tail.iloc[i]["open"] >= tail.iloc[i - 1]["open"] and
                    tail.iloc[i]["open"] <= tail.iloc[i - 1]["close"] and
                    tail.iloc[i]["close"] > tail.iloc[i - 1]["close"] > tail.iloc[i - 2]["close"]):
                    patterns.append({"name": "三白兵", "name_en": "Three White Soldiers", "direction": "bullish", "position": "downtrend_reversal", "score": 2, "candles": 3})

        # Three Black Crows
        if trend in ("uptrend", "neutral"):
            c1_yin = tail.iloc[i - 2]["close"] < tail.iloc[i - 2]["open"]
            c2_yin = tail.iloc[i - 1]["close"] < tail.iloc[i - 1]["open"]
            c3_yin = tail.iloc[i]["close"] < tail.iloc[i]["open"]
            if c1_yin and c2_yin and c3_yin:
                if (tail.iloc[i - 1]["open"] <= tail.iloc[i - 2]["open"] and
                    tail.iloc[i - 1]["open"] >= tail.iloc[i - 2]["close"] and
                    tail.iloc[i]["open"] <= tail.iloc[i - 1]["open"] and
                    tail.iloc[i]["open"] >= tail.iloc[i - 1]["close"] and
                    tail.iloc[i]["close"] < tail.iloc[i - 1]["close"] < tail.iloc[i - 2]["close"]):
                    patterns.append({"name": "三只乌鸦", "name_en": "Three Black Crows", "direction": "bearish", "position": "uptrend_reversal", "score": -2, "candles": 3})

    # 2-candle patterns
    if i >= 1:
        f_prev = _candle_features(tail.iloc[i - 1])
        f_curr = _candle_features(tail.iloc[i])

        # Bullish Engulfing
        if trend in ("downtrend", "neutral") and not f_prev["is_yang"] and f_curr["is_yang"]:
            if tail.iloc[i]["open"] <= tail.iloc[i - 1]["close"] and tail.iloc[i]["close"] >= tail.iloc[i - 1]["open"]:
                patterns.append({"name": "看涨吞没", "name_en": "Bullish Engulfing", "direction": "bullish", "position": "downtrend", "score": 2, "candles": 2})

        # Bearish Engulfing
        if trend in ("uptrend", "neutral") and f_prev["is_yang"] and not f_curr["is_yang"]:
            if tail.iloc[i]["open"] >= tail.iloc[i - 1]["close"] and tail.iloc[i]["close"] <= tail.iloc[i - 1]["open"]:
                patterns.append({"name": "看跌吞没", "name_en": "Bearish Engulfing", "direction": "bearish", "position": "uptrend", "score": -2, "candles": 2})

        # Piercing Line
        if trend in ("downtrend", "neutral") and not f_prev["is_yang"] and f_curr["is_yang"]:
            prev_mid = (tail.iloc[i - 1]["open"] + tail.iloc[i - 1]["close"]) / 2
            if tail.iloc[i]["open"] < tail.iloc[i - 1]["low"] and tail.iloc[i]["close"] > prev_mid:
                patterns.append({"name": "刺透形态", "name_en": "Piercing Line", "direction": "bullish", "position": "downtrend", "score": 1, "candles": 2})

        # Dark Cloud Cover
        if trend in ("uptrend", "neutral") and f_prev["is_yang"] and not f_curr["is_yang"]:
            prev_mid = (tail.iloc[i - 1]["open"] + tail.iloc[i - 1]["close"]) / 2
            if tail.iloc[i]["open"] > tail.iloc[i - 1]["high"] and tail.iloc[i]["close"] < prev_mid:
                patterns.append({"name": "乌云盖顶", "name_en": "Dark Cloud Cover", "direction": "bearish", "position": "uptrend", "score": -1, "candles": 2})

        # Bullish Gap Up
        if tail.iloc[i]["low"] > tail.iloc[i - 1]["high"]:
            direction = "bullish"
            s = 1
            if trend == "downtrend":
                s = 2
            patterns.append({"name": "跳空向上", "name_en": "Bullish Gap Up", "direction": direction, "position": "breakout", "score": s, "candles": 2})

        # Bearish Gap Down
        if tail.iloc[i]["high"] < tail.iloc[i - 1]["low"]:
            direction = "bearish"
            s = -1
            if trend == "uptrend":
                s = -2
            patterns.append({"name": "跳空向下", "name_en": "Bearish Gap Down", "direction": direction, "position": "breakdown", "score": s, "candles": 2})

        # Harami (containment)
        if f_prev["is_large_body"] and f_curr["is_small_body"]:
            if (tail.iloc[i]["high"] <= tail.iloc[i - 1]["high"] and
                tail.iloc[i]["low"] >= tail.iloc[i - 1]["low"]):
                if f_curr["is_doji"]:
                    patterns.append({"name": "十字胎", "name_en": "Harami Cross", "direction": "neutral", "position": "reversal_possible", "score": 0, "candles": 2})
                else:
                    patterns.append({"name": "孕线", "name_en": "Harami", "direction": "neutral", "position": "trend_weakening", "score": 0, "candles": 2})

    # 1-candle patterns
    f = _candle_features(tail.iloc[i])

    # Hammer
    if f["lower_ratio"] >= 2 and f["upper_ratio"] <= 0.3 and trend in ("downtrend", "neutral"):
        s = 1
        if "vol" in tail.columns and tail.iloc[i]["vol"] > tail["vol"].mean() * 1.5:
            s = 2
        patterns.append({"name": "锤子线", "name_en": "Hammer", "direction": "bullish", "position": "downtrend_end", "score": s, "candles": 1})

    # Hanging Man (same shape as hammer, but in uptrend)
    if f["lower_ratio"] >= 2 and f["upper_ratio"] <= 0.3 and trend == "uptrend":
        patterns.append({"name": "上吊线", "name_en": "Hanging Man", "direction": "bearish", "position": "uptrend_end", "score": -1, "candles": 1})

    # Shooting Star
    if f["upper_ratio"] >= 2 and f["lower_ratio"] <= 0.3 and not f["is_yang"] and trend == "uptrend":
        patterns.append({"name": "射击之星", "name_en": "Shooting Star", "direction": "bearish", "position": "uptrend_end", "score": -1, "candles": 1})

    # Inverted Hammer
    if f["upper_ratio"] >= 2 and f["lower_ratio"] <= 0.3 and f["is_yang"] and trend in ("downtrend", "neutral"):
        patterns.append({"name": "倒锤子", "name_en": "Inverted Hammer", "direction": "bullish", "position": "downtrend_end", "score": 1, "candles": 1})

    # Doji
    if f["is_doji"]:
        pos = "trend_end" if trend != "neutral" else "mid_trend"
        patterns.append({"name": "十字星", "name_en": "Doji", "direction": "neutral", "position": pos, "score": 0, "candles": 1})

    # Spinning Top
    if f["is_small_body"] and not f["is_doji"] and f["upper_shadow"] > 0 and f["lower_shadow"] > 0:
        patterns.append({"name": "纺锤线", "name_en": "Spinning Top", "direction": "neutral", "position": "indecision", "score": 0, "candles": 1})

    # Deduplicate: keep highest absolute score per direction
    seen = {}
    for p in patterns:
        key = p["name"]
        if key not in seen or abs(p["score"]) > abs(seen[key]["score"]):
            seen[key] = p

    return list(seen.values())


# --- Support/Resistance levels ---


def calc_vwap(df, period=20):
    """Approximate VWAP using daily OHLCV data."""
    if len(df) < period:
        return None
    recent = df.tail(period)
    typ_price = (recent["high"] + recent["low"] + recent["close"]) / 3
    if recent["vol"].sum() == 0:
        return None
    vwap = (typ_price * recent["vol"]).sum() / recent["vol"].sum()
    return round(vwap, 4)


def calc_quantile_levels(df, lookback=60):
    """Calculate price quantile levels for S/R."""
    if len(df) < 20:
        return {}
    prices = df["close"].tail(min(lookback, len(df)))
    return {
        "q05": round(prices.quantile(0.05), 4),
        "q10": round(prices.quantile(0.10), 4),
        "q90": round(prices.quantile(0.90), 4),
        "q95": round(prices.quantile(0.95), 4),
    }


def calc_pivot_points(df):
    """Classic pivot points from previous complete bar."""
    if len(df) < 2:
        return {"support": [], "resistance": []}
    prev = df.iloc[-2]
    H, L, C = prev["high"], prev["low"], prev["close"]
    P = (H + L + C) / 3
    return {
        "support": [round(2 * P - H, 4), round(P - (H - L), 4)],
        "resistance": [round(2 * P - L, 4), round(P + (H - L), 4)],
    }


def calc_volume_profile(df, lookback=60, num_buckets=20):
    """Approximate volume profile from daily data: high-volume nodes as S/R."""
    if len(df) < 20:
        return {"support": [], "resistance": []}
    recent = df.tail(min(lookback, len(df)))
    price_min, price_max = recent["low"].min(), recent["high"].max()
    bucket_width = (price_max - price_min) / num_buckets
    if bucket_width <= 0:
        return {"support": [], "resistance": []}

    buckets = {i: 0.0 for i in range(num_buckets)}
    for _, row in recent.iterrows():
        low_b = int((row["low"] - price_min) / bucket_width)
        high_b = int((row["high"] - price_min) / bucket_width)
        low_b = max(0, min(low_b, num_buckets - 1))
        high_b = max(0, min(high_b, num_buckets - 1))
        vol = row["vol"]
        if high_b == low_b:
            buckets[low_b] += vol
        else:
            mid_b = (low_b + high_b) // 2
            buckets[low_b] += vol * 0.3
            buckets[high_b] += vol * 0.3
            buckets[mid_b] += vol * 0.4

    avg_vol = sum(buckets.values()) / num_buckets
    if avg_vol == 0:
        return {"support": [], "resistance": []}
    threshold = avg_vol * 1.5

    curr_close = df["close"].iloc[-1]
    support, resistance = [], []
    for i, total_vol in buckets.items():
        if total_vol > threshold:
            price = price_min + (i + 0.5) * bucket_width
            if price < curr_close:
                support.append({"price": round(price, 4), "source": "volume_profile",
                                "strength": "medium", "vol_ratio": round(total_vol / avg_vol, 1)})
            elif price > curr_close:
                resistance.append({"price": round(price, 4), "source": "volume_profile",
                                   "strength": "medium", "vol_ratio": round(total_vol / avg_vol, 1)})
    return {"support": support, "resistance": resistance}


def calc_support_resistance(df, ma_result, bollinger_result, atr_pct=None, adx_value=None, atr_absolute=None, chip_peaks=None):
    """Calculate key support and resistance levels with strength ranking.

    Args:
        df: DataFrame with OHLCV data
        ma_result: Result from calc_ma_signals()
        bollinger_result: Result from calc_bollinger()
        atr_pct: ATR as percentage of price (for convergence check).
                 If None, falls back to 0.5% fixed threshold.
        atr_absolute: Absolute ATR value (for clustering distance threshold).
                      If None, falls back to percentage-based estimate.
        chip_peaks: List of {price, vol_ratio} from chip distribution high_volume_nodes.
                    Added as S/R levels with "chip_peak" source, strength="high".
    """
    close = df["close"]
    curr_close = close.iloc[-1]
    levels = {"support": [], "resistance": []}

    # Recency tiers for sort tiebreaking: lower = more recent = higher priority.
    # 0=today, 1=yesterday/pivot, 2=this week, 3=older swing points, 4=timeless
    def _recency(source):
        if source in ("today_low", "today_high", "today_open"):
            return 0
        if source == "pivot":
            return 1
        if source in ("recent_low", "recent_high"):
            return 2
        if source in ("swing_low", "swing_high"):
            return 3
        return 4  # MA, bollinger, fib, round_number, vwap, quantile, volume_profile, chip_peak

    # From MA values
    ma_vals = ma_result.get("values", {})
    for key, val in ma_vals.items():
        if val is not None and not pd.isna(val):
            strength = "high" if "ma60" in key else ("medium" if "ma20" in key else "low")
            if val < curr_close:
                levels["support"].append({"price": round(val, 4), "source": key, "strength": strength})
            elif val > curr_close:
                levels["resistance"].append({"price": round(val, 4), "source": key, "strength": strength})

    # From Bollinger Bands
    boll = bollinger_result
    for key in ["lower", "middle", "upper"]:
        val = boll.get(key)
        if val is not None and not pd.isna(val):
            if val < curr_close:
                levels["support"].append({"price": round(val, 4), "source": f"boll_{key}", "strength": "medium"})
            elif val > curr_close:
                levels["resistance"].append({"price": round(val, 4), "source": f"boll_{key}", "strength": "medium"})

    # From recent price action (local min/max in last 20 bars)
    if len(df) >= 5:
        recent = df.tail(20)
        for _, row in recent.iterrows():
            low, high = row["low"], row["high"]
            if low < curr_close:
                levels["support"].append({"price": round(low, 4), "source": "recent_low", "strength": "low"})
            if high > curr_close:
                levels["resistance"].append({"price": round(high, 4), "source": "recent_high", "strength": "low"})

    # Today's OHLC — the most immediate and actionable reference levels.
    # Force high strength so they survive clustering regardless of other sources.
    today = df.iloc[-1]
    today_low = round(today["low"], 4)
    today_high = round(today["high"], 4)
    today_open = round(today["open"], 4)
    if today_low < curr_close:
        levels["support"].append({"price": today_low, "source": "today_low", "strength": "high"})
    if today_high > curr_close:
        levels["resistance"].append({"price": today_high, "source": "today_high", "strength": "high"})
    # Today's open as medium reference — often acts as intraday support/resistance flip
    if today_open < curr_close:
        levels["support"].append({"price": today_open, "source": "today_open", "strength": "medium"})
    elif today_open > curr_close:
        levels["resistance"].append({"price": today_open, "source": "today_open", "strength": "medium"})

    # From structural highs/lows (previous swing points)
    if len(df) >= 20:
        swing_lookback = min(60, len(df))
        swing_df = df.tail(swing_lookback)
        # Find swing highs (local max with 3 bars on each side)
        for i in range(3, len(swing_df) - 3):
            if swing_df["high"].iloc[i] == swing_df["high"].iloc[i - 3:i + 4].max():
                val = round(swing_df["high"].iloc[i], 4)
                if val > curr_close:
                    levels["resistance"].append({"price": val, "source": "swing_high", "strength": "medium"})
            if swing_df["low"].iloc[i] == swing_df["low"].iloc[i - 3:i + 4].min():
                val = round(swing_df["low"].iloc[i], 4)
                if val < curr_close:
                    levels["support"].append({"price": val, "source": "swing_low", "strength": "medium"})

    # Fibonacci retracement levels
    if len(df) >= 30:
        lookback = min(120, len(df))
        recent_data = df.tail(lookback)
        high_price = recent_data["high"].max()
        low_price = recent_data["low"].min()

        fib_ratios = {"0.382": 0.382, "0.5": 0.5, "0.618": 0.618}
        for name, ratio in fib_ratios.items():
            fib_level = high_price - (high_price - low_price) * ratio
            fib_rounded = round(fib_level, 4)
            if fib_level > curr_close:
                levels["resistance"].append({"price": fib_rounded, "source": f"fib_{name}", "strength": "medium"})
            elif fib_level < curr_close:
                levels["support"].append({"price": fib_rounded, "source": f"fib_{name}", "strength": "medium"})

    # Round number levels (psychological levels)
    if curr_close > 0:
        magnitude = 10 ** max(0, int(np.log10(curr_close)) - 1)
        for mult in range(1, 20):
            round_level = magnitude * mult
            if round_level > curr_close * 1.5:
                break
            if round_level > curr_close:
                levels["resistance"].append({"price": round_level, "source": "round_number", "strength": "low"})
            elif round_level < curr_close:
                levels["support"].append({"price": round_level, "source": "round_number", "strength": "low"})

    # VWAP level
    vwap = calc_vwap(df)
    if vwap is not None:
        if vwap < curr_close:
            levels["support"].append({"price": vwap, "source": "vwap", "strength": "medium"})
        elif vwap > curr_close:
            levels["resistance"].append({"price": vwap, "source": "vwap", "strength": "medium"})

    # Quantile levels
    quantiles = calc_quantile_levels(df)
    if quantiles:
        if "q05" in quantiles and quantiles["q05"] < curr_close:
            levels["support"].append({"price": quantiles["q05"], "source": "quantile_q05", "strength": "medium"})
        if "q10" in quantiles and quantiles["q10"] < curr_close:
            levels["support"].append({"price": quantiles["q10"], "source": "quantile_q10", "strength": "medium"})
        if "q90" in quantiles and quantiles["q90"] > curr_close:
            levels["resistance"].append({"price": quantiles["q90"], "source": "quantile_q90", "strength": "medium"})
        if "q95" in quantiles and quantiles["q95"] > curr_close:
            levels["resistance"].append({"price": quantiles["q95"], "source": "quantile_q95", "strength": "medium"})

    # Pivot points
    pivots = calc_pivot_points(df)
    for p in pivots["support"]:
        if p < curr_close:
            levels["support"].append({"price": p, "source": "pivot", "strength": "medium"})
    for p in pivots["resistance"]:
        if p > curr_close:
            levels["resistance"].append({"price": p, "source": "pivot", "strength": "medium"})

    # Volume profile (high-volume nodes)
    vp = calc_volume_profile(df)
    for item in vp["support"]:
        levels["support"].append(item)
    for item in vp["resistance"]:
        levels["resistance"].append(item)

    # Chip distribution peaks (high-volume price nodes from longer lookback)
    # These represent real traded volume zones — strong S/R levels.
    if chip_peaks:
        for peak in chip_peaks:
            price = peak.get("price")
            if price is None:
                continue
            if price < curr_close:
                levels["support"].append({"price": round(price, 4), "source": "chip_peak", "strength": "high"})
            elif price > curr_close:
                levels["resistance"].append({"price": round(price, 4), "source": "chip_peak", "strength": "high"})

    # Stamp recency on every level for tiebreaking in sort.
    # Lower recency = more recent = higher priority when strength is equal.
    for direction in ["support", "resistance"]:
        for item in levels[direction]:
            item["recency"] = _recency(item.get("source", ""))

    # Round number filter: drop round_number levels without nearby confirmation
    # A round number is "anchored" if at least one non-round-number level
    # from another source exists within 1.0×ATR distance.
    if atr_absolute is not None:
        round_validation_range = 1.0 * atr_absolute
        all_non_round_prices = [item["price"] for item in levels["support"] + levels["resistance"]
                                if item.get("source") != "round_number"]
        for direction in ["support", "resistance"]:
            filtered = []
            for item in levels[direction]:
                if item.get("source") == "round_number":
                    anchored = any(abs(item["price"] - p) < round_validation_range
                                   for p in all_non_round_prices)
                    if not anchored:
                        continue
                filtered.append(item)
            levels[direction] = filtered

    # Cluster nearby levels and sort by strength
    for direction in ["support", "resistance"]:
        if not levels[direction]:
            continue
        # Clustering threshold: use absolute ATR-based distance instead of percentage.
        # Semi-ATR adapts to market regime via ADX:
        #   trending (ADX>=25): 0.5×ATR  — wider zones in strong trends
        #   transition (20-25): 0.35×ATR
        #   ranging (ADX<20):   0.2×ATR  — tight zones, every level matters
        # Falls back to 0.5% of price if ATR unavailable.
        if atr_absolute is not None:
            if adx_value is not None and adx_value >= 25:
                cluster_dist = 0.5 * atr_absolute
            elif adx_value is not None and adx_value >= 20:
                cluster_dist = 0.35 * atr_absolute
            else:
                cluster_dist = 0.2 * atr_absolute
        else:
            cluster_dist = curr_close * 0.005  # fallback: 0.5% of price

        sorted_items = sorted(levels[direction], key=lambda x: x["price"], reverse=(direction == "support"))
        clustered = []
        for item in sorted_items:
            merged = False
            for cluster in clustered:
                if abs(item["price"] - cluster["price"]) < cluster_dist:
                    # Merge: keep stronger strength, minimum recency, combine sources
                    strength_rank = {"high": 3, "medium": 2, "low": 1}
                    if strength_rank.get(item.get("strength", "low"), 0) > strength_rank.get(cluster.get("strength", "low"), 0):
                        cluster["strength"] = item["strength"]
                    cluster["recency"] = min(cluster.get("recency", 4), item.get("recency", 4))
                    cluster["sources"] = cluster.get("sources", [cluster["source"]]) + [item["source"]]
                    cluster["test_count"] = cluster.get("test_count", 1) + 1
                    merged = True
                    break
            if not merged:
                clustered.append({**item, "sources": [item["source"]], "test_count": 1})

        # Upgrade strength based on test count (multiple sources = stronger)
        strength_rank = {"high": 3, "medium": 2, "low": 1}
        for item in clustered:
            if item.get("test_count", 1) >= 3 and strength_rank.get(item.get("strength", "low"), 0) < 3:
                item["strength"] = "high"
            elif item.get("test_count", 1) >= 2 and strength_rank.get(item.get("strength", "low"), 0) < 2:
                item["strength"] = "medium"

        # Sort: strength (high→low), recency (recent→old), distance (close→far)
        # Recency tiebreaker: when two levels have equal strength, prefer the one
        # anchored by more recent price action over a stale historical swing point.
        clustered.sort(key=lambda x: (
            -strength_rank.get(x.get("strength", "low"), 0),
            x.get("recency", 4),
            abs(x["price"] - curr_close)
        ))

        levels[direction] = clustered[:5]  # Top 5 per side

    # S/R convergence check: if nearest support and resistance are within 0.5×ATR,
    # they form a convergence zone — a battleground where price is deciding direction.
    # Tag the return value instead of deleting either level.
    levels["convergence_zone"] = None
    atr_ratio = atr_pct / 100 if atr_pct else 0.005
    min_sr_gap = curr_close * atr_ratio * 0.5
    if levels["support"] and levels["resistance"]:
        s, r = levels["support"][0], levels["resistance"][0]
        if abs(r["price"] - s["price"]) < min_sr_gap:
            levels["convergence_zone"] = {
                "support": s["price"],
                "resistance": r["price"],
                "gap_pct": round(abs(r["price"] - s["price"]) / curr_close * 100, 2),
            }

    return levels


# --- ATR ---


def calc_atr(df, period=14):
    """Compute Average True Range for stop-loss and volatility assessment."""
    if len(df) < period + 1:
        return {"atr": None, "atr_pct": None, "signal": {"type": "insufficient_data", "description": "ATR数据不足", "score": 0}}

    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    curr_atr = atr.iloc[-1]

    if pd.isna(curr_atr):
        return {"atr": None, "atr_pct": None, "signal": {"type": "insufficient_data", "description": "ATR数据不足", "score": 0}}

    curr_close = df["close"].iloc[-1]
    atr_pct = (curr_atr / curr_close * 100) if curr_close > 0 else 0

    # Volatility assessment for position sizing
    if atr_pct > 4:
        vol_level = "extreme"
        desc = f"ATR={curr_atr:.2f}({atr_pct:.1f}%)，极端波动"
    elif atr_pct > 2.5:
        vol_level = "high"
        desc = f"ATR={curr_atr:.2f}({atr_pct:.1f}%)，高波动"
    elif atr_pct > 1.5:
        vol_level = "normal"
        desc = f"ATR={curr_atr:.2f}({atr_pct:.1f}%)，正常波动"
    else:
        vol_level = "low"
        desc = f"ATR={curr_atr:.2f}({atr_pct:.1f}%)，低波动"

    return {
        "atr": round(curr_atr, 4),
        "atr_pct": round(atr_pct, 2),
        "volatility_level": vol_level,
        "signal": {"type": "volatility", "description": desc, "score": 0},
    }


# --- Volatility regime detection ---


def _volatility_regime_multiplier(df, atr_pct=None):
    """Detect volatility clustering for adaptive stop-loss ATR multiplier.

    Returns adjustment to atr_mult:
      expanding vol (ratio>1.2) → +0.5~+1.0  (avoid whipsaw)
      contracting vol (ratio<0.8) → -0.3      (tighter stop)
    """
    if len(df) < 25:
        return 0.0

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)

    recent_atr = tr.tail(5).mean()
    medium_atr = tr.tail(20).mean()

    if not medium_atr or medium_atr == 0:
        return 0.0

    vol_ratio = recent_atr / medium_atr
    adj = 0.0

    if vol_ratio > 1.2:
        adj = min(1.0, (vol_ratio - 1.2) * 3.0)
    elif vol_ratio < 0.8:
        adj = -0.3

    return round(adj, 1)


# --- Max Drawdown ---


def calc_max_drawdown(df):
    """Calculate maximum drawdown over the data period."""
    if len(df) < 2:
        return {"max_drawdown_pct": None, "drawdown_days": None}

    close = df["close"]
    cummax = close.cummax()
    drawdown = (close - cummax) / cummax
    max_dd = drawdown.min()

    if pd.isna(max_dd):
        return {"max_drawdown_pct": None, "drawdown_days": None}

    # Find duration of max drawdown
    dd_end = drawdown.idxmin()
    dd_start = close.loc[:dd_end].idxmax()
    dd_days = (dd_end - dd_start) if dd_end > dd_start else None

    return {
        "max_drawdown_pct": round(max_dd * 100, 2),
        "drawdown_days": int(dd_days) if dd_days is not None and not pd.isna(dd_days) else None,
    }


# --- Stop-loss and Risk:Reward ---


def calc_risk_reward(df, atr_result, levels, direction="neutral", is_etf=False):
    """Calculate stop-loss price and risk:reward ratio with three-tier targets.

    Stop-loss uses adaptive ATR multiplier based on trend direction and volatility.
    Position sizing factors in R:R quality and trend direction.
    ETF vs stock: ETFs get tighter ATR multipliers (smoother action) + higher R:R threshold.
    """
    curr_close = df["close"].iloc[-1]
    atr = atr_result.get("atr")
    atr_pct = atr_result.get("atr_pct", 0)

    # Adaptive price rounding: low-price assets (<1) need 4 decimals,
    # mid-price (<10) use 3, normal stocks use 2.
    def _round_price(p):
        if p is None:
            return None
        if curr_close < 1:
            return round(p, 4)
        elif curr_close < 10:
            return round(p, 3)
        return round(p, 2)

    # Exclude boll_upper from stop-loss support: upper band is volatility ceiling, not floor
    support_items = [item for item in levels.get("support", []) if item["price"] and item.get("source") != "boll_upper"]
    support_prices = [item["price"] for item in support_items]
    resistance_prices = sorted([item["price"] for item in levels.get("resistance", []) if item["price"]])

    # --- Adaptive stop-loss with volatility regime awareness ---
    # ATR multiplier: wider in bearish (avoid whipsaw), tighter in bullish low-vol
    # ETF: tighter stops (smoother price action, less gap risk)
    if is_etf:
        if direction == "bearish":
            atr_mult = 1.8
        elif direction == "bullish" and atr_pct < 2.0:
            atr_mult = 1.5
        else:
            atr_mult = 1.5
    else:
        if direction == "bearish":
            atr_mult = 2.5
        elif direction == "bullish" and atr_pct < 2.0:
            atr_mult = 2.0
        else:
            atr_mult = 2.0
    # Volatility regime adjustment (expanding → wider, contracting → tighter)
    regime_adj = _volatility_regime_multiplier(df, atr_pct)
    atr_mult += regime_adj
    # Extra width for extreme volatility
    if atr_pct > 4.0 and regime_adj < 0.5:
        atr_mult += 0.5

    if support_prices and atr:
        nearest_support = max(support_prices)
        atr_stop = curr_close - atr_mult * atr
        # Take the higher of support level and ATR-based stop (don't go below support)
        stop_loss = _round_price(max(nearest_support, atr_stop))
    elif support_prices:
        nearest_support = max(support_prices)
        stop_loss = _round_price(nearest_support)
    elif atr:
        stop_loss = _round_price(curr_close - atr_mult * atr)
    else:
        stop_loss = None

    # Guard: stop_loss must stay below current price
    # (support level rounding can push it above for low-price ETFs)
    if stop_loss and stop_loss >= curr_close:
        if atr:
            stop_loss = _round_price(curr_close - atr_mult * atr)
        else:
            stop_loss = _round_price(curr_close * 0.99)

    # Safety net: if stop-loss too close (< 0.5x ATR%), prefer ATR-based distance
    # NOTE: atr_pct is percentage (4.07 = 4.07%), stop_pct is decimal (0.0646 = 6.46%)
    if stop_loss and atr and atr_pct > 0:
        stop_pct = (curr_close - stop_loss) / curr_close
        if stop_pct < (atr_pct / 100.0) * 0.5:
            atr_stop = _round_price(curr_close - atr_mult * atr)
            if atr_stop < stop_loss:
                stop_loss = atr_stop

    # Stop-loss too close warning
    stop_loss_warning = None
    if stop_loss and curr_close > 0:
        stop_pct = (curr_close - stop_loss) / curr_close
        if stop_pct < 0.02:
            stop_loss_warning = f"止损距现价仅{round(stop_pct*100, 1)}%，易被正常波动扫掉，建议放宽止损或等待更确认信号"

    # --- Three-tier target system ---
    target_conservative = None
    target_moderate = None
    target_aggressive = None
    warning = None

    risk = (curr_close - stop_loss) if stop_loss else None

    if resistance_prices:
        # Conservative: nearest resistance
        target_conservative = _round_price(resistance_prices[0])

        if risk and risk > 0:
            # R:R threshold for target selection: higher for ETFs (tighter stop → need wider target)
            target_rr_threshold = 2.0 if is_etf else 1.5
            # Moderate: first resistance where R:R >= target_rr_threshold
            for rp in resistance_prices:
                if (rp - curr_close) / risk >= target_rr_threshold:
                    target_moderate = _round_price(rp)
                    break
            if target_moderate is None and atr:
                # ETF tight stop means 2*atr gives ~1.33R, need more — use 3*atr for ~2.0R
                atr_target_mult = 3 if is_etf else 2
                target_moderate = _round_price(curr_close + atr_target_mult * atr)
            elif target_moderate is None:
                target_moderate = target_conservative

            # Aggressive: next resistance after moderate
            moderate_idx = None
            for i, rp in enumerate(resistance_prices):
                if _round_price(rp) == target_moderate:
                    moderate_idx = i
                    break
            if moderate_idx is not None and moderate_idx + 1 < len(resistance_prices):
                target_aggressive = _round_price(resistance_prices[moderate_idx + 1])
            elif atr:
                etf_agg_mult = 4 if is_etf else 3
                target_aggressive = _round_price(curr_close + etf_agg_mult * atr)
            else:
                target_aggressive = target_moderate
        else:
            target_moderate = target_conservative
            target_aggressive = target_conservative
    elif atr:
        target_conservative = _round_price(curr_close + 1 * atr)
        # ETF: wider ATR multipliers to compensate for tighter stop
        mod_mult = 3 if is_etf else 2
        agg_mult = 4 if is_etf else 3
        target_moderate = _round_price(curr_close + mod_mult * atr)
        target_aggressive = _round_price(curr_close + agg_mult * atr)
        warning = "支撑/压力位数据不足，止损/目标价仅供参考"
    else:
        warning = "ATR数据不足，无法计算止损/目标价"

    # Primary target = moderate
    target = target_moderate

    # R:R favorable threshold: higher for ETFs (smoother → require more edge)
    rr_favorable_threshold = 2.0 if is_etf else 1.5

    # --- R:R ratios for all three targets ---
    reward = (target - curr_close) if target else None

    rr_ratio = None
    rr_conservative = None
    rr_moderate = None
    rr_aggressive = None
    favorable_rr = None

    if risk and risk > 0:
        if target:
            reward = target - curr_close
            rr_ratio = round(reward / risk, 2)
            favorable_rr = rr_ratio >= rr_favorable_threshold
        if target_conservative:
            rr_conservative = round((target_conservative - curr_close) / risk, 2)
        if target_moderate:
            rr_moderate = round((target_moderate - curr_close) / risk, 2)
        if target_aggressive:
            rr_aggressive = round((target_aggressive - curr_close) / risk, 2)

    # Combine warnings
    if not warning:
        if rr_ratio is not None and rr_ratio < 0.5:
            warning = f"风险收益比过低(R:R={rr_ratio})，支撑/压力位过近，参考价值有限"
        elif stop_loss_warning:
            warning = stop_loss_warning

    # --- Position sizing: ATR% + R:R quality + trend direction ---
    POS_TIERS = ["轻仓(20-30%)", "半仓(40-50%)", "标准仓位(50-70%)", "可重仓(70-80%)"]
    if atr_pct > 4:
        base_tier = 0
    elif atr_pct > 2.5:
        base_tier = 1
    elif atr_pct > 1.5:
        base_tier = 2
    else:
        base_tier = 3

    # R:R adjustment (ETF thresholds shifted +0.5: smoother price needs more edge)
    rr_adj = 0
    if rr_ratio is not None:
        if is_etf:
            if rr_ratio < 1.5:
                rr_adj = -2
            elif rr_ratio < 2.0:
                rr_adj = -1
            elif rr_ratio > 3.0:
                rr_adj = 1
        else:
            if rr_ratio < 1.0:
                rr_adj = -2
            elif rr_ratio < 1.5:
                rr_adj = -1
            elif rr_ratio > 2.5:
                rr_adj = 1

    # Trend adjustment: bearish → reduce one tier
    trend_adj = -1 if direction == "bearish" else 0

    final_tier = max(0, min(3, base_tier + rr_adj + trend_adj))
    position = POS_TIERS[final_tier]

    return {
        "stop_loss": stop_loss,
        "target_conservative": target_conservative,
        "target_moderate": target_moderate,
        "target_aggressive": target_aggressive,
        "target": target,
        "risk_reward_ratio": rr_ratio,
        "rr_conservative": rr_conservative,
        "rr_moderate": rr_moderate,
        "rr_aggressive": rr_aggressive,
        "favorable_rr": favorable_rr,
        "risk": round(risk, 2) if risk else 0,
        "reward": round(reward, 2) if reward else 0,
        "position_sizing": position,
        "position_tier": final_tier,
        "warning": warning,
    }


# --- Entry signal fusion ---


def _calc_rsi_at(df, offset, period=14):
    """Compute RSI at a specific offset from end of df."""
    if len(df) < abs(offset) + period + 1:
        return None
    segment = df.iloc[offset - period:offset] if offset < 0 else df.iloc[offset:offset + period]
    if len(segment) < period:
        return None
    closes = segment["close"].values
    gains = 0
    losses = 0
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def calc_entry_signals(df, indicator_results, rr_ratio=None, is_etf=False) -> dict:
    """Fuse multi-indicator signals into entry timing advice.

    Checks 4 confirmation signals:
    1. Volume breakout: MA5 volume / MA20 volume > 1.3 + close > MA20
    2. RSI inflection: RSI was < 30 5-10d ago, now > 35
    3. Bollinger squeeze breakout: bandwidth < 5% + close breaks band
    4. Volume-shrinking pullback: vol_ratio < 0.8 + close near MA20 (±1.5%)

    Also considers R:R quality: R:R < threshold demotes verdict priority.
    ETF threshold higher (1.5R) than stock (1.0R).
    """
    close_price = float(df["close"].iloc[-1])

    # Volume column: accept "vol" or "volume"
    vol_col = "vol" if "vol" in df.columns else ("volume" if "volume" in df.columns else None)
    if vol_col is None:
        return {"signal_count": 0, "signals": [], "verdict": "wait"}
    vol_ma5 = float(df[vol_col].tail(5).mean())
    vol_ma20 = float(df[vol_col].tail(20).mean())
    vol_ratio = vol_ma5 / vol_ma20 if vol_ma20 > 0 else 1.0

    # MA20 value
    ma20_val = None
    ma_result = indicator_results.get("ma", {})
    if isinstance(ma_result, dict):
        ma20_val = ma_result.get("values", {}).get("ma20")
    if ma20_val is None and "ma20" in df.columns:
        ma20_val = float(df["ma20"].iloc[-1])
    if ma20_val is None and len(df) >= 20:
        ma20_val = float(df["close"].tail(20).mean())

    signals_found = []

    # 1) Volume breakout confirmation
    if vol_ratio > 1.3 and ma20_val and close_price > ma20_val:
        signals_found.append("放量突破")

    # 2) RSI inflection from oversold
    rsi_now = None
    rsi_result = indicator_results.get("rsi", {})
    if isinstance(rsi_result, dict):
        rsi_now = rsi_result.get("rsi")
    if rsi_now is None:
        # Calc from df
        rsi_now = _calc_rsi_at(df, -1, 14)
    if rsi_now is not None:
        rsi_10d = _calc_rsi_at(df, -10, 14) if len(df) >= 25 else None
        rsi_5d = _calc_rsi_at(df, -5, 14) if len(df) >= 20 else None
        ref_rsi = rsi_10d or rsi_5d
        if ref_rsi and ref_rsi < 30 and rsi_now > ref_rsi and rsi_now > 35:
            signals_found.append("RSI拐头")

    # 3) Bollinger squeeze + breakout
    bb_result = indicator_results.get("bollinger", {})
    if isinstance(bb_result, dict):
        bandwidth = bb_result.get("bandwidth_pct")
        upper = bb_result.get("upper")
        lower = bb_result.get("lower")
        if bandwidth is not None and bandwidth < 5 and upper and lower:
            if close_price >= float(upper) or close_price <= float(lower):
                signals_found.append("布林变盘")

    # 4) Volume-shrinking pullback to MA20
    if vol_ratio < 0.8 and ma20_val and abs(close_price - ma20_val) / ma20_val < 0.015:
        signals_found.append("缩量回踩")

    # Verdict
    count = len(signals_found)
    summary = indicator_results.get("summary", {}) if "summary" in indicator_results else {}
    direction = summary.get("direction", "") if isinstance(summary, dict) else ""

    if count >= 2 and direction == "bullish":
        verdict = "ready"
    elif count >= 1 and direction == "bullish":
        verdict = "watch"
    elif count >= 2:
        verdict = "watch"
    elif direction == "bearish":
        verdict = "avoid"
    else:
        verdict = "wait"

    # R:R filter: poor R:R demotes entry priority
    # ETF: stricter threshold (1.5R vs 1.0R for stocks)
    rr_entry_threshold = 1.5 if is_etf else 1.0
    if rr_ratio is not None and rr_ratio < rr_entry_threshold:
        if verdict == "ready":
            verdict = "watch"
            signals_found.append("R:R偏低(建议观望)")
        elif verdict == "watch":
            verdict = "wait"

    return {
        "signal_count": count,
        "signals": signals_found,
        "verdict": verdict,
    }


# --- Aggregate summary ---


def build_summary(indicator_results, patterns, data_points=None):
    """Build overall technical summary with weighted scores and consistency factor."""
    # Sub-weights: trend indicators (MA+MACD) heavier, oscillators (RSI+KDJ) lighter
    SUB_WEIGHTS = {
        "ma": 1.5,
        "macd": 1.5,
        "rsi": 0.8,
        "kdj": 0.8,
        "bollinger": 1.0,
        "volume": 1.0,
        "adx": 1.2,
        "obv": 1.0,
    }

    weighted_sum = 0
    weight_total = 0
    scores = []
    valid_scores = []  # Only non-insufficient_data scores for consistency
    key_signals = []

    for name, result in indicator_results.items():
        signal = result.get("signal", {})
        s = signal.get("score", 0)
        w = SUB_WEIGHTS.get(name, 1.0)
        weighted_sum += s * w
        weight_total += w
        scores.append(s)
        # Track only indicators with actual data for consistency calculation
        if signal.get("type") != "insufficient_data":
            valid_scores.append(s)
        desc = signal.get("description", "")
        if desc:
            key_signals.append(desc)

    # Pattern scores (aggregate, cap at ±3)
    pattern_score = sum(p["score"] for p in patterns) if patterns else 0
    pattern_score = max(-3, min(3, pattern_score))
    weighted_sum += pattern_score * 0.5  # Weight patterns at 50%
    weight_total += 0.5

    for p in patterns:
        if p["score"] != 0:
            key_signals.append(f"K线形态: {p['name']}({p['direction']})")

    # Weighted average, then scale back to [-3, +3]
    total = weighted_sum / weight_total if weight_total > 0 else 0
    total = max(-3, min(3, round(total, 2)))

    # Consistency factor: same-direction indicators increase confidence
    # Use only valid (non-insufficient_data) scores for consistency
    bull_count = sum(1 for s in valid_scores if s > 0)
    bear_count = sum(1 for s in valid_scores if s < 0)
    total_count = len(valid_scores) if valid_scores else 1
    consistency = max(bull_count, bear_count) / total_count

    if total >= 2:
        direction = "bullish"
    elif total <= -2:
        direction = "bearish"
    else:
        direction = "neutral"

    abs_total = abs(total)
    # Consistency boosts confidence
    if abs_total >= 2.5 and consistency >= 0.7:
        confidence = "high"
    elif abs_total >= 2.0 and consistency >= 0.5:
        confidence = "medium"
    elif abs_total >= 2.5 and consistency < 0.5:
        confidence = "medium"  # High score but low consistency → downgrade
    else:
        confidence = "low"

    result = {
        "total_score": total,
        "direction": direction,
        "confidence": confidence,
        "consistency": round(consistency, 2),
        "key_signals": key_signals[:8],
    }

    # Data quality assessment
    if data_points is not None and data_points < 30:
        result["data_quality"] = "insufficient"
        result["key_signals"].insert(0, f"⚠️ 数据仅{data_points}条，分析结果可靠性极低")
    elif data_points is not None and data_points < 60:
        result["data_quality"] = "limited"
        result["key_signals"].insert(0, f"⚠️ 数据仅{data_points}条，部分指标可能不准确")
    else:
        result["data_quality"] = "good"

    # Build dimension scores dict for downstream consumers
    dimension_scores = {}
    for ind_name, ind_result in indicator_results.items():
        signal = ind_result.get("signal", {})
        dimension_scores[ind_name] = signal.get("score", 0)
    if patterns:
        pattern_score = sum(p["score"] for p in patterns)
        dimension_scores["patterns"] = max(-3, min(3, pattern_score))
    result["dimension_scores"] = dimension_scores

    return result


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="Technical analysis for stock-trend skill")
    parser.add_argument("input_file", nargs="?", help="K-line JSON file from fetch_kline.py (reads stdin if omitted)")
    parser.add_argument("--indicators", default="ma,macd,rsi,kdj,bollinger,volume,adx,obv,patterns",
                        help="Comma-separated indicators to compute (default: all)")
    parser.add_argument("--ma-periods", default="5,10,20,60", help="MA periods (default: 5,10,20,60)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--compact", action="store_true", help="Output only summary section")
    parser.add_argument("--etf", action="store_true", help="Treat as ETF (tighter stop, higher R:R threshold)")
    parser.add_argument("--chip-distribution", help="Path to chip_distribution.json for chip peak S/R levels")

    args = parser.parse_args()

    # Read input
    if args.input_file and args.input_file != "-":
        with open(args.input_file, "r", encoding="utf-8") as f:
            input_data = json.load(f)
    else:
        input_data = json.load(sys.stdin)

    # Check for error input
    if input_data.get("meta", {}).get("data_source") == "error":
        result = {
            "meta": {
                "ts_code": input_data.get("meta", {}).get("ts_code", "unknown"),
                "analysis_date": datetime.now().strftime("%Y%m%d"),
                "data_points": 0,
                "indicators_computed": [],
                "error": input_data["meta"].get("error", "Unknown error"),
            },
            "latest": {},
            "patterns": [],
            "summary": {
                "total_score": 0,
                "direction": "neutral",
                "confidence": "low",
                "key_signals": ["无K线数据，技术面无法分析"],
                "support_levels": [],
                "resistance_levels": [],
            },
        }
        _output(result, args.output, args.compact)
        return

    # Parse data into DataFrame
    records = input_data.get("data", [])
    if not records:
        result = {"meta": {"error": "No data records in input"}, "summary": {"total_score": 0, "direction": "neutral", "confidence": "low", "key_signals": ["无数据"]}}
        _output(result, args.output, args.compact)
        return

    df = pd.DataFrame(records)
    for col in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Add MA columns from data if available
    for p in args.ma_periods.split(","):
        col = f"ma{p.strip()}"
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    ts_code = input_data.get("meta", {}).get("ts_code", "unknown")
    indicators = [x.strip() for x in args.indicators.split(",")]
    ma_periods = [int(x.strip()) for x in args.ma_periods.split(",")]

    # Compute indicators
    indicator_results = {}

    if "ma" in indicators:
        indicator_results["ma"] = calc_ma_signals(df, ma_periods)
    if "macd" in indicators:
        indicator_results["macd"] = calc_macd(df)
    if "rsi" in indicators:
        indicator_results["rsi"] = calc_rsi(df)
    if "kdj" in indicators:
        indicator_results["kdj"] = calc_kdj(df)
    if "bollinger" in indicators:
        indicator_results["bollinger"] = calc_bollinger(df)
    if "volume" in indicators:
        indicator_results["volume"] = analyze_volume(df)
    if "adx" in indicators:
        indicator_results["adx"] = calc_adx(df)
    if "obv" in indicators:
        indicator_results["obv"] = calc_obv(df)

    # Pattern recognition
    patterns = scan_patterns(df) if "patterns" in indicators else []

    # ATR
    atr_result = calc_atr(df)

    # Max drawdown
    drawdown_result = calc_max_drawdown(df)

    # Support/Resistance
    chip_peaks = None
    if getattr(args, "chip_distribution", None):
        try:
            with open(args.chip_distribution, "r", encoding="utf-8") as f:
                chip_data = json.load(f)
            chip_peaks = chip_data.get("high_volume_nodes", [])
        except (OSError, json.JSONDecodeError):
            pass

    levels = calc_support_resistance(
        df,
        indicator_results.get("ma", {}),
        indicator_results.get("bollinger", {}),
        atr_pct=atr_result.get("atr_pct"),
        adx_value=indicator_results.get("adx", {}).get("adx"),
        atr_absolute=atr_result.get("atr"),
        chip_peaks=chip_peaks,
    )

    # Summary (need direction before risk_reward)
    summary = build_summary(indicator_results, patterns, data_points=len(df))
    direction = summary.get("direction", "neutral")

    # Risk:Reward and stop-loss (uses direction for adaptive stop)
    risk_reward = calc_risk_reward(df, atr_result, levels, direction=direction, is_etf=args.etf)

    summary["support_levels"] = sorted([item["price"] for item in levels["support"]])
    summary["resistance_levels"] = sorted([item["price"] for item in levels["resistance"]])
    summary["stop_loss"] = risk_reward.get("stop_loss")
    summary["target_conservative"] = risk_reward.get("target_conservative")
    summary["target_moderate"] = risk_reward.get("target_moderate")
    summary["target_aggressive"] = risk_reward.get("target_aggressive")
    summary["target"] = risk_reward.get("target")
    summary["risk_reward_ratio"] = risk_reward.get("risk_reward_ratio")
    summary["rr_conservative"] = risk_reward.get("rr_conservative")
    summary["rr_moderate"] = risk_reward.get("rr_moderate")
    summary["rr_aggressive"] = risk_reward.get("rr_aggressive")
    summary["favorable_rr"] = risk_reward.get("favorable_rr")
    summary["position_sizing"] = risk_reward.get("position_sizing")
    summary["position_tier"] = risk_reward.get("position_tier")
    summary["risk_reward_warning"] = risk_reward.get("warning")
    summary["max_drawdown_pct"] = drawdown_result.get("max_drawdown_pct")

    # Entry signal fusion (with R:R filter)
    rr_ratio = risk_reward.get("risk_reward_ratio")
    summary["entry_signals"] = calc_entry_signals(df, indicator_results, rr_ratio=rr_ratio, is_etf=args.etf)

    # Latest data point
    last = df.iloc[-1]
    latest = {
        "date": str(last.get("trade_date", "")),
        "close": round(float(last["close"]), 4) if not pd.isna(last["close"]) else None,
    }
    for name, result in indicator_results.items():
        latest[name] = result
    latest["atr"] = atr_result
    latest["max_drawdown"] = drawdown_result

    result = {
        "meta": {
            "ts_code": ts_code,
            "is_etf": args.etf,
            "analysis_date": datetime.now().strftime("%Y%m%d"),
            "data_points": len(df),
            "indicators_computed": list(indicator_results.keys()) + (["patterns"] if patterns else []),
        },
        "latest": latest,
        "patterns": patterns,
        "summary": summary,
    }

    _output(result, args.output, args.compact)


def _convert_numpy(obj):
    """Recursively convert numpy types to Python native types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if pd.isna(obj) if isinstance(obj, (float, np.floating)) else False:
        return None
    return obj


def _output(result, output_path=None, compact=False):
    """Write JSON result to file or stdout."""
    output_json(_convert_numpy(result), output_path=output_path, compact=compact)


if __name__ == "__main__":
    main()