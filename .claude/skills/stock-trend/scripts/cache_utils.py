#!/usr/bin/env python3
"""Shared caching utilities for stock-trend scripts.

Provides file-based JSON caching with TTL support and market-hours-aware
TTL calculation. Cache files are stored in .cache/stock-trend/ by default.

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
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_WALK = _SCRIPT_DIR
_PROJECT_ROOT = _WALK
for _ in range(10):
    if (_WALK / ".claude").exists() or (_WALK / "CLAUDE.md").exists():
        _PROJECT_ROOT = _WALK
        break
    _WALK = _WALK.parent
_DEFAULT_CACHE_DIR = os.path.join(str(_PROJECT_ROOT), ".cache", "stock-trend")
CACHE_DIR = os.environ.get("STOCK_TREND_CACHE_DIR", _DEFAULT_CACHE_DIR)


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


def clean_cache(max_size_mb=200):
    """Remove oldest cache files when total size exceeds max_size_mb.

    Uses LRU eviction based on cache_timestamp metadata.
    Called at pipeline start to prevent unbounded cache growth.
    """
    if not os.path.exists(CACHE_DIR):
        return 0

    max_size_bytes = max_size_mb * 1024 * 1024
    files = []
    total_size = 0

    for f in os.listdir(CACHE_DIR):
        if not f.endswith(".json"):
            continue
        fp = os.path.join(CACHE_DIR, f)
        try:
            size = os.path.getsize(fp)
            with open(fp, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            ts = d.get("cache_timestamp", 0)
            files.append((fp, size, ts))
            total_size += size
        except (json.JSONDecodeError, OSError):
            try:
                os.remove(fp)
            except OSError:
                pass

    if total_size <= max_size_bytes:
        return 0

    # Sort oldest first for eviction
    files.sort(key=lambda x: x[2])
    removed = 0
    for fp, size, ts in files:
        try:
            os.remove(fp)
            total_size -= size
            removed += 1
            if total_size <= max_size_bytes * 0.8:  # Evict to 80% threshold
                break
        except OSError:
            pass

    return removed


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


# ─── Shared utility functions ────────────────────────────────────────


def safe_float(val, default=None, round_to=None):
    """Convert value to float, return default on failure. Strips %, commas."""
    if val is None or val == "" or val == "-" or val == "N/A":
        return default
    try:
        s = str(val).replace("%", "").replace(",", "").strip()
        result = float(s)
        return round(result, round_to) if round_to is not None else result
    except (ValueError, TypeError):
        return default


def output_json(data, output_path=None, compact=False):
    """Write JSON to file or stdout. Creates output dir if needed.

    Args:
        data: JSON-serializable dict/list.
        output_path: File path or None for stdout.
        compact: If True, use no indentation. For dict data, only
                 write data.get('summary', data).
    """
    out = data.get("summary", data) if compact and isinstance(data, dict) else data
    text = json.dumps(out, ensure_ascii=False, indent=2 if not compact else None)
    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


@contextmanager
def suppress_stderr():
    """Temporarily suppress stderr to hide progress bars."""
    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        yield
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr


def retry(func, max_attempts=2, delay=2):
    """Call func with retry and stderr suppression. Returns (result, error)."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            with suppress_stderr():
                return func(), None
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay)
    return None, str(last_err)


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