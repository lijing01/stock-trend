#!/usr/bin/env python3
"""Shared caching utilities for stock-trend scripts.

Provides file-based JSON caching with TTL support and market-hours-aware
TTL calculation. Cache files are stored in /tmp/stock-trend-cache/ by default.

Usage in scripts:
    from cache_utils import load_cache, save_cache, get_market_day_ttl

    # Check cache before fetching
    cache_key = f"kline_{ts_code}_{freq}_{adj}"
    cached = load_cache(cache_key, ttl=get_market_day_ttl())
    if cached:
        _output(cached, args.output)
        sys.exit(0)

    # ... fetch data ...

    # Save to cache before output
    save_cache(cache_key, result)
    _output(result, args.output)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR = os.environ.get("STOCK_TREND_CACHE_DIR", "/tmp/stock-trend-cache")


def get_cache_path(cache_key: str) -> str:
    """Return the file path for a cache key."""
    # Sanitize key to be filesystem-safe
    safe_key = cache_key.replace("/", "_").replace(" ", "_")
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


def load_cache(cache_key: str, ttl_seconds: int) -> dict | None:
    """Load cached data if it exists and is within TTL.

    Args:
        cache_key: Unique identifier for the cached data.
        ttl_seconds: Maximum age in seconds for the cache to be valid.

    Returns:
        Cached dict if valid, None if expired or missing.
    """
    path = get_cache_path(cache_key)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        cache_ts = cached.get("cache_timestamp", 0)
        if time.time() - cache_ts < ttl_seconds:
            # Strip cache metadata before returning
            data = {k: v for k, v in cached.items() if k != "cache_timestamp" and k != "cache_key"}
            return data
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return None


def save_cache(cache_key: str, data: dict) -> None:
    """Save data to cache with timestamp metadata.

    Args:
        cache_key: Unique identifier for the cached data.
        data: The data dict to cache.
    """
    path = get_cache_path(cache_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cache_data = {
            "cache_timestamp": time.time(),
            "cache_key": cache_key,
            **data,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except OSError:
        # Cache write failure is non-fatal
        pass


def clear_cache(cache_key: str = None) -> None:
    """Clear cache for a specific key, or all cache if key is None."""
    if cache_key:
        path = get_cache_path(cache_key)
        try:
            os.remove(path)
        except OSError:
            pass
    else:
        try:
            for f in os.listdir(CACHE_DIR):
                fp = os.path.join(CACHE_DIR, f)
                if f.endswith(".json"):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
        except OSError:
            pass


def is_trading_hours() -> bool:
    """Check if current time is within A-share trading hours (9:30-15:00 CST, Mon-Fri)."""
    now = datetime.now()
    # Monday=0, Sunday=6; trading days are 0-4
    if now.weekday() >= 5:
        return False
    # Trading hours: 9:30-11:30, 13:00-15:00
    morning = now.hour == 9 and now.minute >= 30 or now.hour == 10 or now.hour == 11 and now.minute < 30
    afternoon = now.hour == 13 or now.hour == 14 or (now.hour == 15 and now.minute == 0)
    return morning or afternoon


def get_market_day_ttl(trading_ttl: int = 300, after_hours_ttl: int = 57600) -> int:
    """Return appropriate TTL based on whether market is currently open.

    Args:
        trading_ttl: Cache TTL during trading hours (default 5 min).
        after_hours_ttl: Cache TTL after hours (default 16 hours).

    Returns:
        TTL in seconds.
    """
    if is_trading_hours():
        return trading_ttl
    return after_hours_ttl


if __name__ == "__main__":
    # CLI for cache management
    import argparse
    parser = argparse.ArgumentParser(description="Stock-trend cache management")
    parser.add_argument("--list", action="store_true", help="List all cache entries")
    parser.add_argument("--clear", action="store_true", help="Clear all cache")
    parser.add_argument("--clear-key", help="Clear cache for a specific key")
    parser.add_argument("--stat", action="store_true", help="Show cache statistics")
    args = parser.parse_args()

    if args.clear:
        clear_cache()
        print("Cache cleared")
    elif args.clear_key:
        clear_cache(args.clear_key)
        print(f"Cache cleared for key: {args.clear_key}")
    elif args.list or args.stat:
        if not os.path.exists(CACHE_DIR):
            print("Cache directory does not exist")
        else:
            files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
            if args.stat:
                total_size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in files)
                print(f"Cache entries: {len(files)}")
                print(f"Total size: {total_size / 1024:.1f} KB")
                for f in sorted(files):
                    fp = os.path.join(CACHE_DIR, f)
                    try:
                        with open(fp, "r") as fh:
                            d = json.load(fh)
                        age = time.time() - d.get("cache_timestamp", 0)
                        print(f"  {f}: age={age:.0f}s, key={d.get('cache_key', '?')}")
                    except Exception:
                        print(f"  {f}: (error reading)")
            else:
                for f in sorted(files):
                    print(f.rstrip(".json"))