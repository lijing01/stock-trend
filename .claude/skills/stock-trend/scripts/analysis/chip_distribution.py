#!/usr/bin/env python3
"""Approximate chip distribution (筹码分布) from daily OHLCV data.

Uses triangular weight distribution per bar (peak near close) to estimate
where volume accumulated across price levels. No Level-2 data required.

Output:
  - distribution: [{price, volume, vol_ratio}] sorted by price, 50 buckets
  - avg_cost: volume-weighted average cost
  - profit_ratio: % of chips below current price
  - concentration: % of chips within current_price ± ATR
  - high_volume_nodes: Top 5 highest-volume price levels

Usage:
    python3 compute_chip_distribution.py /path/to/kline.json -o /path/output.json
"""

import argparse
import json
import sys
from pathlib import Path


def compute_atr(records, period=14):
    """Simple ATR from kline records list."""
    if len(records) < period + 1:
        return None
    tr_sum = 0.0
    for i in range(-period, 0):
        r = records[i]
        prev = records[i - 1]
        hl = r["high"] - r["low"]
        hc = abs(r["high"] - prev["close"])
        lc = abs(r["low"] - prev["close"])
        tr_sum += max(hl, hc, lc)
    return tr_sum / period


def compute_chip_distribution(kline_data, lookback=120, num_buckets=50):
    """Compute approximate chip distribution from daily OHLCV.

    Args:
        kline_data: dict with "data" list of OHLCV records
        lookback: number of recent bars to use (default 120, ~half year)
        num_buckets: price granularity (default 50)

    Returns:
        dict with distribution, avg_cost, profit_ratio, concentration, high_volume_nodes
    """
    records = kline_data.get("data", [])
    if len(records) < 20:
        return {"error": "insufficient data", "detail": f"Need >=20 bars, got {len(records)}"}

    recent = records[-min(lookback, len(records)):]

    # Overall price range for bucket definitions
    price_min = min(r["low"] for r in recent)
    price_max = max(r["high"] for r in recent)
    price_span = price_max - price_min
    if price_span <= 0:
        return {"error": "zero price span", "detail": "price_min == price_max"}

    bucket_width = price_span / num_buckets

    # Precompute bucket mid-prices
    bucket_prices = [price_min + (i + 0.5) * bucket_width for i in range(num_buckets)]

    # Accumulate volume per bucket
    buckets = [0.0] * num_buckets

    for r in recent:
        low = r["low"]
        high = r["high"]
        close = r["close"]
        open_p = r["open"]
        vol = r.get("vol", 0)
        if vol <= 0 or high <= low:
            continue

        bar_span = high - low

        # Determine peak (center of triangular distribution)
        # Bullish bars: nudge peak upward; bearish bars: nudge downward
        is_bullish = close > open_p
        if is_bullish:
            center = close + (high - close) * 0.2
        else:
            center = close - (close - low) * 0.2
        # Clamp center within [low, high]
        center = max(low, min(high, center))

        # Max distance from center to edge (for normalization)
        max_dist = max(center - low, high - center)
        if max_dist <= 0:
            max_dist = bar_span

        # Compute weights for in-range buckets
        first_bucket = max(0, int((low - price_min) / bucket_width))
        last_bucket = min(num_buckets - 1, int((high - price_min) / bucket_width))

        raw_weights = []
        for i in range(first_bucket, last_bucket + 1):
            dist = abs(bucket_prices[i] - center) / max_dist
            w = max(0.0, 1.0 - dist)
            raw_weights.append(w)

        weight_sum = sum(raw_weights)
        if weight_sum <= 0:
            continue

        # Distribute volume across buckets
        for idx_offset, w in enumerate(raw_weights):
            buckets[first_bucket + idx_offset] += (w / weight_sum) * vol

    total_vol = sum(buckets)
    if total_vol <= 0:
        return {"error": "zero total volume", "detail": "no volume accumulated"}

    # Build distribution array
    max_bucket_vol = max(buckets)
    distribution = []
    for i in range(num_buckets):
        v = buckets[i]
        distribution.append({
            "price": round(bucket_prices[i], 4),
            "volume": round(v, 2),
            "vol_ratio": round(v / total_vol, 6) if total_vol > 0 else 0,
        })

    # Volume-weighted average cost
    avg_cost = sum(buckets[i] * bucket_prices[i] for i in range(num_buckets)) / total_vol

    current_price = recent[-1]["close"]

    # Profit ratio: % of chips at prices < current price
    profit_vol = sum(
        v for i, v in enumerate(buckets) if bucket_prices[i] < current_price
    )
    profit_ratio = profit_vol / total_vol

    # Concentration: % of chips within current_price ± ATR
    atr_val = compute_atr(recent)
    if atr_val is not None and atr_val > 0:
        conc_low = current_price - atr_val
        conc_high = current_price + atr_val
        conc_vol = sum(
            v for i, v in enumerate(buckets)
            if conc_low <= bucket_prices[i] <= conc_high
        )
        concentration = conc_vol / total_vol
    else:
        concentration = 0.0

    # High-volume nodes: Top 5
    indexed = [(i, v) for i, v in enumerate(buckets) if v > 0]
    indexed.sort(key=lambda x: x[1], reverse=True)
    high_volume_nodes = []
    for i, v in indexed[:5]:
        high_volume_nodes.append({
            "price": round(bucket_prices[i], 4),
            "volume": round(v, 2),
            "vol_ratio": round(v / total_vol, 6),
        })

    # Annotate each distribution entry with whether it's a high-volume node
    hv_node_prices = {n["price"] for n in high_volume_nodes}
    for d in distribution:
        d["is_peak"] = d["price"] in hv_node_prices

    return {
        "meta": {
            "lookback": lookback,
            "num_buckets": num_buckets,
            "price_min": round(price_min, 4),
            "price_max": round(price_max, 4),
            "total_volume": round(total_vol, 2),
            "atr": round(atr_val, 4) if atr_val is not None else None,
        },
        "distribution": distribution,
        "avg_cost": round(avg_cost, 4),
        "current_price": current_price,
        "profit_ratio": round(profit_ratio, 4),
        "concentration": round(concentration, 4),
        "high_volume_nodes": high_volume_nodes,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compute approximate chip distribution from daily OHLCV data"
    )
    parser.add_argument("kline_file", help="Path to kline.json (OHLCV data)")
    parser.add_argument("--lookback", type=int, default=120, help="Number of bars to analyze (default: 120)")
    parser.add_argument("--buckets", type=int, default=50, help="Number of price buckets (default: 50)")
    parser.add_argument("-o", "--output", help="Output JSON file path (default: stdout)")

    args = parser.parse_args()

    try:
        with open(args.kline_file, "r", encoding="utf-8") as f:
            kline_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading kline file: {e}", file=sys.stderr)
        sys.exit(1)

    result = compute_chip_distribution(kline_data, lookback=args.lookback, num_buckets=args.buckets)

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Chip distribution written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
