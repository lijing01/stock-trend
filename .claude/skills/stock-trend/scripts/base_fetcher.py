#!/usr/bin/env python3
"""Base class for stock-trend fetch scripts.

Provides unified argparse, JSON output, error handling, and cache integration.
Subclasses only need to implement fetch() -> dict.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from cache_utils import load_cache, save_cache, get_market_day_ttl


class BaseFetcher:
    """Base class for data fetch scripts.

    Subclass and implement:
        - fetch() -> dict: The core data fetching logic.
        - cache_key_suffix (str, optional): Appended to auto-generated cache key.
        - cache_ttl_seconds (int, optional): Override default market-aware TTL.

    Usage in subclass:
        class MyFetcher(BaseFetcher):
            def fetch(self):
                data = ...  # fetch from API
                return {"meta": {...}, "data": data}

            # Optional: override cache behavior
            cache_key_suffix = "_weekly"

        if __name__ == "__main__":
            MyFetcher().run()
    """

    # Subclass can override
    cache_key_suffix = ""
    cache_ttl_seconds = None  # None = use get_market_day_ttl()

    def __init__(self):
        self.args = None
        self.ts_code = None
        self.code = None

    def add_arguments(self, parser):
        """Override to add custom arguments. Subclasses call super().add_arguments(parser) first."""
        parser.add_argument("ts_code", nargs="?", help="Tushare-format code (e.g. 600519.SH)")
        parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
        parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")

    def fetch(self):
        """Subclass must implement. Returns dict of data."""
        raise NotImplementedError

    def build_cache_key(self):
        """Build cache key from ts_code + suffix. Subclass can override."""
        key = f"{self.__class__.__name__.lower()}_{self.ts_code}"
        if self.cache_key_suffix:
            key += self.cache_key_suffix
        return key

    def _output(self, result, output_path=None):
        """Write JSON result to file or stdout."""
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if output_path:
            os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"Data written to {output_path}", file=sys.stderr)
        else:
            print(text)

    def run(self):
        """Main entry point: parse args -> check cache -> fetch -> cache write -> output."""
        parser = argparse.ArgumentParser(description=self.__class__.__doc__ or "Stock-trend data fetcher")
        self.add_arguments(parser)
        self.args = parser.parse_args()

        # Resolve ts_code from positional arg
        self.ts_code = self.args.ts_code
        if not self.ts_code and hasattr(self.args, "code") and self.args.code:
            pass

        # Check cache
        cache_key = self.build_cache_key()
        ttl = self.cache_ttl_seconds or get_market_day_ttl()

        if not self.args.no_cache:
            cached = load_cache(cache_key, ttl_seconds=ttl)
            if cached:
                self._output(cached, self.args.output)
                return

        # Fetch data
        try:
            result = self.fetch()
        except Exception as e:
            result = {
                "meta": {
                    "data_source": "error",
                    "error": str(e),
                },
                "data": [],
            }

        # Cache successful result (skip errors)
        if result.get("meta", {}).get("data_source") not in ("error", None):
            save_cache(cache_key, result)

        self._output(result, self.args.output)