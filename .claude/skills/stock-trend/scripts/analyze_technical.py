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

import argparse
import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd


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

    # Divergence detection (simplified: compare last 20 bars)
    if len(df) >= 20:
        recent_close = df["close"].iloc[-20:]
        recent_dif = dif.iloc[-20:]
        close_trend = recent_close.iloc[-1] - recent_close.iloc[0]
        dif_trend = recent_dif.iloc[-1] - recent_dif.iloc[0]

        if close_trend > 0 and dif_trend < 0:
            desc_parts.append("顶背离")
            score -= 2
        elif close_trend < 0 and dif_trend > 0:
            desc_parts.append("底背离")
            score += 2

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

    # Divergence
    if len(df) >= 20 and not pd.isna(rsi.iloc[-20]):
        close_trend = df["close"].iloc[-1] - df["close"].iloc[-20]
        rsi_trend = rsi.iloc[-1] - rsi.iloc[-20]
        if close_trend > 0 and rsi_trend < 0:
            desc += "；顶背离"
            score -= 2
        elif close_trend < 0 and rsi_trend > 0:
            desc += "；底背离"
            score += 2

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

    # Squeeze detection
    if len(df) >= period * 2:
        band_width = (curr_upper - curr_lower) / curr_middle if curr_middle > 0 else 0
        prev_width = (upper.iloc[-period] - lower.iloc[-period]) / middle.iloc[-period] if middle.iloc[-period] > 0 else 0
        if not (pd.isna(band_width) or pd.isna(prev_width)):
            if band_width < prev_width * 0.7:
                desc_parts.append("布林带收口")
                score = 0  # Squeeze = neutral, watch for direction

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


def calc_support_resistance(df, ma_result, bollinger_result):
    """Calculate key support and resistance levels."""
    close = df["close"]
    curr_close = close.iloc[-1]
    levels = {"support": [], "resistance": []}

    # From MA values
    ma_vals = ma_result.get("values", {})
    for key, val in ma_vals.items():
        if val is not None and not pd.isna(val):
            if val < curr_close:
                levels["support"].append({"price": round(val, 4), "source": key})
            elif val > curr_close:
                levels["resistance"].append({"price": round(val, 4), "source": key})

    # From Bollinger Bands
    boll = bollinger_result
    for key in ["lower", "middle", "upper"]:
        val = boll.get(key)
        if val is not None and not pd.isna(val):
            if val < curr_close:
                levels["support"].append({"price": round(val, 4), "source": f"boll_{key}"})
            elif val > curr_close:
                levels["resistance"].append({"price": round(val, 4), "source": f"boll_{key}"})

    # From recent price action (local min/max in last 20 bars)
    if len(df) >= 5:
        recent = df.tail(20)
        for _, row in recent.iterrows():
            low, high = row["low"], row["high"]
            if low < curr_close:
                levels["support"].append({"price": round(low, 4), "source": "recent_low"})
            if high > curr_close:
                levels["resistance"].append({"price": round(high, 4), "source": "recent_high"})

    # Deduplicate and sort
    for direction in ["support", "resistance"]:
        prices = {}
        for item in levels[direction]:
            p = item["price"]
            if p not in prices:
                prices[p] = item
        sorted_items = sorted(prices.values(), key=lambda x: x["price"], reverse=(direction == "support"))
        levels[direction] = sorted_items[:3]  # Top 3 per side

    return levels


# --- Aggregate summary ---


def build_summary(indicator_results, patterns):
    """Build overall technical summary."""
    scores = []
    key_signals = []

    for name, result in indicator_results.items():
        signal = result.get("signal", {})
        s = signal.get("score", 0)
        scores.append(s)
        desc = signal.get("description", "")
        if desc:
            key_signals.append(desc)

    # Pattern scores (aggregate, cap at ±3)
    pattern_score = sum(p["score"] for p in patterns) if patterns else 0
    pattern_score = max(-3, min(3, pattern_score))
    scores.append(pattern_score * 0.5)  # Weight patterns at 50% since indicators already include some

    for p in patterns:
        if p["score"] != 0:
            key_signals.append(f"K线形态: {p['name']}({p['direction']})")

    total = sum(scores)
    total = max(-3, min(3, round(total)))

    if total >= 2:
        direction = "bullish"
    elif total <= -2:
        direction = "bearish"
    else:
        direction = "neutral"

    abs_total = abs(total)
    if abs_total >= 2.5:
        confidence = "high"
    elif abs_total >= 2.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "total_score": total,
        "direction": direction,
        "confidence": confidence,
        "key_signals": key_signals[:8],
    }


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="Technical analysis for stock-trend skill")
    parser.add_argument("input_file", nargs="?", help="K-line JSON file from fetch_kline.py (reads stdin if omitted)")
    parser.add_argument("--indicators", default="ma,macd,rsi,kdj,bollinger,volume,patterns",
                        help="Comma-separated indicators to compute (default: all)")
    parser.add_argument("--ma-periods", default="5,10,20,60", help="MA periods (default: 5,10,20,60)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--compact", action="store_true", help="Output only summary section")

    args = parser.parse_args()

    # Read input
    if args.input_file:
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

    # Pattern recognition
    patterns = scan_patterns(df) if "patterns" in indicators else []

    # Support/Resistance
    levels = calc_support_resistance(
        df,
        indicator_results.get("ma", {}),
        indicator_results.get("bollinger", {}),
    )

    # Summary
    summary = build_summary(indicator_results, patterns)
    summary["support_levels"] = [item["price"] for item in levels["support"]]
    summary["resistance_levels"] = [item["price"] for item in levels["resistance"]]

    # Latest data point
    last = df.iloc[-1]
    latest = {
        "date": str(last.get("trade_date", "")),
        "close": round(float(last["close"]), 4) if not pd.isna(last["close"]) else None,
    }
    for name, result in indicator_results.items():
        latest[name] = result

    result = {
        "meta": {
            "ts_code": ts_code,
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
    if compact:
        output = result.get("summary", result)
    else:
        output = result

    output = _convert_numpy(output)
    text = json.dumps(output, ensure_ascii=False, indent=2 if not compact else None)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Analysis written to {output_path}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()