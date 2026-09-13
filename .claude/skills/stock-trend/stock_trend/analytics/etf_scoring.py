"""ETF replay scoring functions, isolated from scan orchestration."""


RSI_ANCHORS = [(0, -10.0), (20, -10.0), (30, 3.0), (40, 10.0),
               (50, 10.0), (60, 10.0), (70, 3.0), (80, -10.0), (100, -10.0)]
SHARES_TREND_ANCHORS = [(-20, 0.0), (-10, 10.0), (-3, 20.0), (0, 40.0),
                        (3, 65.0), (10, 85.0), (30, 100.0)]


def _ma(prices, period):
    return sum(prices[-period:]) / period if len(prices) >= period else (prices[-1] if prices else 0.0)


def _rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    gains, losses = [max(delta, 0) for delta in deltas], [max(-delta, 0) for delta in deltas]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    value = 50.0
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        value = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return value


def _macd_direction(prices):
    if len(prices) < 26:
        return 0.0
    ema12, ema26 = sum(prices[:12]) / 12, sum(prices[:26]) / 26
    for price in prices[12:]:
        ema12 = ema12 * (1 - 2 / 13) + price * 2 / 13
    for price in prices[26:]:
        ema26 = ema26 * (1 - 2 / 27) + price * 2 / 27
    return ema12 - ema26


def _piecewise_linear(value, anchors):
    if value <= anchors[0][0]:
        return max(0.0, min(100.0, anchors[0][1]))
    if value >= anchors[-1][0]:
        return max(0.0, min(100.0, anchors[-1][1]))
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= value <= x1:
            return max(0.0, min(100.0, y0 + (y1 - y0) * (value - x0) / (x1 - x0)))
    return 0.0


def _trend_strength(closes):
    if len(closes) < 20:
        return 0.0, 0.0
    roc5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0.0
    roc20 = (closes[-1] - closes[-21]) / closes[-21] if len(closes) >= 21 else 0.0
    direction = 1 if roc5 > .005 and roc20 > 0 else (-1 if roc5 < -.005 and roc20 < 0 else 0)
    return min(abs(roc5) + abs(roc20), 1.0) * 100, direction


def score_momentum(kline):
    closes = [row["close"] for row in kline]
    if len(closes) < 20:
        return 50.0
    ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
    rsi_value, macd_value = _rsi(closes, 14), _macd_direction(closes)
    strength, direction = _trend_strength(closes)
    score = 50.0
    score += 15 if ma5 > ma20 > ma60 else (-15 if ma5 < ma20 < ma60 else (5 if ma5 > ma20 else (-5 if ma5 < ma20 else 0)))
    score += _piecewise_linear(rsi_value, RSI_ANCHORS)
    score += 8 if macd_value > 0 else -8
    score += 8 if direction == 1 else (-8 if direction == -1 else 0)
    if ma20 > 0:
        deviation = (closes[-1] - ma20) / ma20 * 100
        trend_up, trend_down = strength > 25 and direction > 0, strength > 25 and direction < 0
        for threshold, regular, trending in ((12, 20, 5), (8, 15, 3), (5, 8, 2), (3, 3, 1)):
            if deviation > threshold:
                score -= trending if trend_up else regular
                break
            if deviation < -threshold:
                score -= trending if trend_down else regular
                break
    return max(0.0, min(100.0, score))


def score_volume(kline):
    if len(kline) < 10:
        return 50.0
    volumes, closes = [row.get("vol", 0) or 0 for row in kline], [row.get("close", 0) or 0 for row in kline]
    ratio = (sum(volumes[-5:]) / 5) / (sum(volumes) / len(volumes)) if sum(volumes) else 1.0
    size = min(5, len(closes))
    price_up = sum(closes[-size:]) / size > (sum(closes[-2 * size:-size]) / size if len(closes) >= 2 * size else closes[0])
    score = 50.0
    if ratio > 1.5: score += 30 if price_up else 5
    elif ratio > 1.2: score += 15 if price_up else 0
    elif ratio < .6: score -= 20
    elif ratio < .8: score -= 10
    if (kline[-1].get("amount", 0) or 0) > 1_000_000_000 and price_up: score += 10
    return max(0.0, min(100.0, score))


def score_shares_trend(etf_data):
    if not etf_data or not isinstance(etf_data.get("recent_flows"), list):
        return None
    valid = [row for row in etf_data["recent_flows"] if isinstance(row, dict) and row.get("shares_billion") is not None]
    if len(valid) < 2 or float(valid[0]["shares_billion"]) == 0:
        return None
    first, last = float(valid[0]["shares_billion"]), float(valid[-1]["shares_billion"])
    return round(_piecewise_linear((last - first) / abs(first) * 100, SHARES_TREND_ANCHORS), 1)
