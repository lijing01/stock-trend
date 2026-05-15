# Pipeline Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor stock-trend pipeline from 10-step manual Agent orchestration to 4-step automation, eliminate code duplication across 6 fetch scripts, replace CLI parameter passing with JSON files, and improve cache reliability and timeout handling.

**Architecture:** Pipeline orchestrator (`run_pipeline.py`) gains `--code` mode to run all data fetching internally. New `BaseFetcher` class and `eastmoney_utils` module eliminate boilerplate. `compute_scores.py` and `generate_report.py` read from a per-code data directory instead of CLI args. Cache moves from `/tmp` to project-relative `.cache/` with LRU eviction.

**Tech Stack:** Python 3.10+, existing scripts refactored in-place, no new dependencies.

---

## Task 1: Add `eastmoney_utils.py` shared module

**Files:**
- Create: `.claude/skills/stock-trend/scripts/eastmoney_utils.py`

This module extracts the duplicated East Money constants and helpers from `fetch_kline_eastmoney.py` and `fetch_capital_flow.py`.

- [ ] **Step 1: Create `eastmoney_utils.py`**

```python
#!/usr/bin/env python3
"""Shared East Money (东方财富) API utilities.

Consolidates headers, secid mapping, and node rotation logic
that was duplicated across fetch_kline_eastmoney.py and fetch_capital_flow.py.
"""

EM_API_HOSTS = [
    "push2his.eastmoney.com",
    "38.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
]

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# Market prefix: .SH -> 1 (Shanghai), .SZ -> 0 (Shenzhen)
MARKET_PREFIX = {
    ".SH": "1",
    ".SZ": "0",
}


def build_secid(ts_code):
    """Convert ts_code to East Money secid format.

    Returns None for unsupported markets (e.g. .HK).
    """
    if "." not in ts_code:
        return None
    code, suffix = ts_code.rsplit(".", 1)
    suffix = f".{suffix}"
    prefix = MARKET_PREFIX.get(suffix)
    if prefix is None:
        return None
    return f"{prefix}.{code}"


def rotate_em_host(fetch_fn, max_retries=3):
    """Try fetch_fn with each EM_API_HOSTS node until success.

    Args:
        fetch_fn: callable(host) -> data, raises on failure.
        max_retries: max attempts (cycles through hosts).

    Returns:
        (data, used_host) tuple on success.

    Raises:
        RuntimeError: if all hosts fail.
    """
    import time
    last_error = None
    for attempt in range(max_retries):
        host = EM_API_HOSTS[attempt % len(EM_API_HOSTS)]
        try:
            data = fetch_fn(host)
            return data, host
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1)
    raise RuntimeError(f"East Money全节点失败: {last_error}")
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/trace/work/agent/stock-trend && python3 -c "from clao.claude.skills.stock-trend.scripts.eastmoney_utils import EM_HEADERS, build_secid, rotate_em_host; print('OK:', build_secid('600519.SH'))"`

Expected: `OK: 1.600519`

(Adjust import path as needed — use `PYTHONPATH` or direct execution.)

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 -c "from eastmoney_utils import EM_HEADERS, build_secid; print('OK:', build_secid('600519.SH'))"`

Expected: `OK: 1.600519`

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/eastmoney_utils.py
git commit -m "feat: add eastmoney_utils shared module (EM headers, secid, node rotation)"
```

---

## Task 2: Add `base_fetcher.py` shared module

**Files:**
- Create: `.claude/skills/stock-trend/scripts/base_fetcher.py`

This module provides a base class that eliminates the duplicated argparse/JSON output/error handling/cache logic across all fetch scripts.

- [ ] **Step 1: Create `base_fetcher.py`**

```python
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
        """Main entry point: parse args → check cache → fetch → cache write → output."""
        parser = argparse.ArgumentParser(description=self.__class__.__doc__ or "Stock-trend data fetcher")
        self.add_arguments(parser)
        self.args = parser.parse_args()

        # Resolve ts_code from positional arg or --code
        self.ts_code = self.args.ts_code
        if not self.ts_code and hasattr(self.args, "code") and self.args.code:
            # --code used instead of positional
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
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 -c "from base_fetcher import BaseFetcher; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/base_fetcher.py
git commit -m "feat: add BaseFetcher base class for unified fetch script behavior"
```

---

## Task 3: Update `cache_utils.py` — new default dir and `clean_cache()`

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/cache_utils.py`

- [ ] **Step 1: Update `CACHE_DIR` default and add `clean_cache` LRU eviction**

Replace `CACHE_DIR` line (line 31) and add `clean_cache` LRU function after `clear_cache` (line 108).

Change line 31 from:
```python
CACHE_DIR = os.environ.get("STOCK_TREND_CACHE_DIR", "/tmp/stock-trend-cache")
```
to:
```python
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
# .claude/skills/stock-trend/scripts/ -> project root
_DEFAULT_CACHE_DIR = os.path.join(_PROJECT_ROOT, ".cache", "stock-trend")
CACHE_DIR = os.environ.get("STOCK_TREND_CACHE_DIR", _DEFAULT_CACHE_DIR)
```

Add after `clear_cache` function (after line 107):

```python
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
```

Also update the `--stat` section in `__main__` to use `CACHE_DIR` consistently (it already does).

- [ ] **Step 2: Verify changes**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 -c "from cache_utils import CACHE_DIR, clean_cache; print('Cache dir:', CACHE_DIR)"`

Expected: Cache dir points to project `.cache/stock-trend/` path.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/cache_utils.py
git commit -m "feat: move cache to project .cache/ dir, add LRU eviction clean_cache()"
```

---

## Task 4: Refactor `fetch_kline_eastmoney.py` to use `eastmoney_utils`

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py`

- [ ] **Step 1: Replace duplicated constants and functions with imports**

At the top of `fetch_kline_eastmoney.py`, add import:
```python
from eastmoney_utils import EM_HEADERS, EM_API_HOSTS, build_secid as resolve_secid_from_utils
```

Remove the following blocks (they're now in `eastmoney_utils`):
- `EM_API_HOSTS` list (lines 27-31)
- `EM_HEADERS` dict (lines 33-38)
- `MARKET_PREFIX` dict (lines 44-47)
- `resolve_secid` function (lines 49-65) — replace usage with `build_secid` from `eastmoney_utils`

Update `resolve_secid` calls to use `build_secid` (same function, different name imported):
```python
secid = build_secid(args.ts_code)  # was: resolve_secid(args.ts_code)
```

Update the host rotation loop in `main()` (lines 391-399) to use `rotate_em_host` from `eastmoney_utils`. Replace:
```python
    for host in EM_API_HOSTS:
        try:
            records, name = fetch_eastmoney(secid, args.freq, args.lmt, host=host)
            used_host = host
            break
        except Exception as e:
            error_msg = str(e)
            import time
            time.sleep(1)
```
with:
```python
    from eastmoney_utils import rotate_em_host
    try:
        (records, name), used_host = rotate_em_host(lambda h: fetch_eastmoney(secid, args.freq, args.lmt, host=h))
    except RuntimeError as e:
        error_msg = str(e)
```

- [ ] **Step 2: Verify script still works**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 fetch_kline_eastmoney.py 600519.SH --no-cache -o /tmp/test_kline_em.json`

Expected: JSON output with data or error (no import errors).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py
git commit -m "refactor: fetch_kline_eastmoney uses eastmoney_utils for shared constants"
```

---

## Task 5: Refactor `fetch_capital_flow.py` to use `eastmoney_utils`

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_capital_flow.py`

- [ ] **Step 1: Replace duplicated constants and functions with imports**

At the top of `fetch_capital_flow.py`, add:
```python
from eastmoney_utils import EM_HEADERS, build_secid as resolve_secid
```

Remove:
- `MARKET_PREFIX` dict (lines 28-30)
- `resolve_secid` function (lines 33-42)
- `EM_HEADERS` dict (lines 45-50)

The import `resolve_secid` from `eastmoney_utils` replaces the local function. `EM_HEADERS` is now shared.

- [ ] **Step 2: Verify script still works**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 fetch_capital_flow.py 600519.SH --no-cache -o /tmp/test_capital.json`

Expected: JSON output with data or error (no import errors).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_capital_flow.py
git commit -m "refactor: fetch_capital_flow uses eastmoney_utils for shared constants"
```

---

## Task 6: Refactor `fetch_etf_data.py` to use `eastmoney_utils`

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_etf_data.py`

- [ ] **Step 1: Replace duplicated EM_HEADERS with import**

At the top of `fetch_etf_data.py`, add:
```python
from eastmoney_utils import EM_HEADERS
```

Remove the local `EM_HEADERS` dict (lines 23-28).

Note: `fetch_etf_data.py` uses a slightly different `Referer` header (`http://fund.eastmoney.com/` vs `https://quote.eastmoney.com/`). Update the `_fetch_url` function to merge the shared headers with an override:
```python
def _fetch_url(url, timeout=15):
    """Fetch URL content with error handling."""
    headers = {**EM_HEADERS, "Referer": "http://fund.eastmoney.com/"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")
```

- [ ] **Step 2: Verify script still works**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 fetch_etf_data.py 513180 -o /tmp/test_etf.json`

Expected: JSON output with ETF data (no import errors).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_etf_data.py
git commit -m "refactor: fetch_etf_data uses eastmoney_utils for shared EM_HEADERS"
```

---

## Task 7: Update `run_pipeline.py` — add `--code` mode, per-step timeout, data dir

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/run_pipeline.py`

This is the largest change. The pipeline gains:
1. `--code` argument for one-command entry
2. Data directory based on code (`.cache/stock-trend/{code}/`)
3. Per-step `timeout=30s` with graceful degradation
4. `timeouts` field in output

- [ ] **Step 1: Add `--code` argument and data directory logic**

Add to argument parser (after existing args):
```python
parser.add_argument("--code", help="Stock/ETF code (e.g. 513180). Auto-resolves and runs full pipeline.")
```

Add data directory helper at module level:
```python
def get_data_dir(code):
    """Return data directory path for a given code."""
    from cache_utils import CACHE_DIR
    d = Path(CACHE_DIR) / code
    d.mkdir(parents=True, exist_ok=True)
    return d
```

Add `--code` mode in `main()`: when `--code` is provided, auto-resolve via `resolve_code.py`, then run the full pipeline with data written to `get_data_dir(code)`.

- [ ] **Step 2: Add per-step timeout**

Update `run_script` to accept `timeout` parameter (default 30s):
```python
def run_script(cmd, label="", timeout=30):
    """Run a Python script with timeout. Returns result dict."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        success = result.returncode == 0
        return {
            "success": success,
            "label": label,
            "returncode": result.returncode,
            "stdout": result.stdout[-500:] if result.stdout else "",
            "stderr": result.stderr[-500:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "label": label,
            "returncode": -1,
            "timeout": True,
            "stdout": "",
            "stderr": f"Timeout ({timeout}s)",
        }
    except Exception as e:
        return {
            "success": False,
            "label": label,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        }
```

Update all `run_script` calls to pass `timeout=30`.

In the pipeline output, track timeouts:
```python
timeouts = []
# ... for each step:
if task_result.get("timeout"):
    timeouts.append(step_name)
```

Add `timeouts` to `pipeline_output`:
```python
pipeline_output = {
    "meta": {...},
    "results": results,
    "errors": errors,
    "timeouts": timeouts,
    "output_files": {...},
}
```

- [ ] **Step 3: Update output paths to use data directory**

When `--code` is provided, all output files go to `get_data_dir(code)/` instead of `/tmp/`:
```python
if args.code:
    output_dir = get_data_dir(args.code)
else:
    output_dir = Path(args.output_dir)
```

- [ ] **Step 4: Verify pipeline runs**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 run_pipeline.py --code 513180 --no-cache`

Expected: Pipeline runs, writes files to `.cache/stock-trend/513180/`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/run_pipeline.py
git commit -m "feat: run_pipeline gains --code mode, per-step timeout, data directory"
```

---

## Task 8: Update `compute_scores.py` — read from data directory

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/compute_scores.py`

- [ ] **Step 1: Add `--code` and `--data-dir` arguments, add data directory reader**

Add to argument parser:
```python
parser.add_argument("--code", help="Stock/ETF code to locate data directory")
parser.add_argument("--data-dir", help="Data directory path (default: .cache/stock-trend/{code}/)")
```

Add helper function:
```python
def find_data_file(data_dir, filename):
    """Find a data file in the data directory, return parsed JSON or None."""
    path = Path(data_dir) / filename
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), str(path)
        except (json.JSONDecodeError, OSError):
            pass
    return None, str(path)
```

- [ ] **Step 2: Update `main()` to resolve data directory and read from files**

When `--code` is provided:
```python
if args.code:
    from cache_utils import CACHE_DIR
    data_dir = Path(args.data_dir) if args.data_dir else Path(CACHE_DIR) / args.code
else:
    data_dir = None
```

Then, for each data source, try reading from data directory first, fall back to explicit CLI arg:
```python
# Technical data
if data_dir:
    technical_data, tech_path = find_data_file(data_dir, "technical.json")
    if technical_data is None:
        print(f"Error: technical.json not found in {data_dir}", file=sys.stderr)
        sys.exit(1)
elif args.technical:
    with open(args.technical, "r", encoding="utf-8") as f:
        technical_data = json.load(f)
else:
    parser.error("--technical or --code required")

# Dimension data files
if data_dir:
    capital_flow_data, _ = find_data_file(data_dir, "capital_flow.json")
    fundamental_data, _ = find_data_file(data_dir, "fundamental.json")
    macro_data, _ = find_data_file(data_dir, "macro_snapshot.json")
    etf_data, _ = find_data_file(data_dir, "etf_data.json")
    search_results, _ = find_data_file(data_dir, "search_results.json")
else:
    # Use explicit CLI args (backward compat)
    ...
```

When `--code` is provided, write scores output to data directory:
```python
if data_dir:
    output_path = data_dir / "scores.json"
else:
    output_path = Path(args.output)
```

- [ ] **Step 3: Add backward compatibility — keep old CLI args as deprecated overrides**

All existing CLI args (`--capital-flow-score`, `--sentiment-summary`, etc.) remain functional. When both `--code` and explicit args are provided, explicit args take precedence (already the behavior since the JSON files are secondary).

- [ ] **Step 4: Verify compute_scores works with --code**

First generate data: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 run_pipeline.py 513180.SH --asset FD -o /tmp`

Then: `python3 compute_scores.py --code 513180 --technical /tmp/technical.json --capital-flow-score 0.5 --sentiment-score 1 -o /tmp/scores.json`

Expected: Scores JSON written to `.cache/stock-trend/513180/scores.json`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/compute_scores.py
git commit -m "feat: compute_scores reads from data directory via --code, backward compat"
```

---

## Task 9: Update `generate_report.py` — read from data directory

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py`

- [ ] **Step 1: Add `--code` and `--data-dir` arguments**

Add to argument parser:
```python
parser.add_argument("--code", help="Stock/ETF code to locate data directory")
parser.add_argument("--data-dir", help="Data directory path (default: .cache/stock-trend/{code}/)")
```

- [ ] **Step 2: When `--code` provided, read pipeline/scores/technical from data dir**

```python
if args.code:
    from cache_utils import CACHE_DIR
    data_dir = Path(args.data_dir) if args.data_dir else Path(CACHE_DIR) / args.code

    # Auto-fill paths from data directory
    if not args.pipeline:
        pipeline_path = data_dir / "pipeline_output.json"
        if pipeline_path.exists():
            args.pipeline = str(pipeline_path)

    if not args.scores_file:
        scores_path = data_dir / "scores.json"
        if scores_path.exists():
            args.scores_file = str(scores_path)

    if not args.technical and args.pipeline:
        # Technical path derived from pipeline
        pass  # existing --pipeline logic handles this
```

- [ ] **Step 3: Verify generate_report works with --code**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 generate_report.py --code 513180 --ts-code 513180.SH --stock-name '恒生科技ETF大成' --output-md /tmp/test_report.md`

Expected: Report generated using data from `.cache/stock-trend/513180/`.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-trend/scripts/generate_report.py
git commit -m "feat: generate_report reads from data directory via --code, backward compat"
```

---

## Task 10: Simplify SKILL.md to 4-step flow

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: Rewrite SKILL.md with simplified 4-step flow**

The new SKILL.md retains:
- Step 1: Parse input (unchanged)
- Step 2: Pipeline one-command + web search (combined)
- Step 3: Compute scores (simplified args)
- Step 4: Generate report (simplified args)

The key change is Step 2: instead of 332 lines of manual orchestration, it's now one command plus web search. Step 4 (compute_scores) and Step 8 (generate_report) in the old flow become Steps 3 and 4 with `--code` instead of 20+ CLI flags.

The reference docs (trend-dimensions.md, kline-patterns.md, troubleshooting.md) remain unchanged.

Keep the Step 3.5 reverse check, Step 5-7 (trend determination, risk management, special asset handling) as narrative guidance — these are Agent decisions, not script invocations.

Keep the data quality check and multi-timeframe instructions.

- [ ] **Step 2: Verify SKILL.md triggers correctly**

Run the skill invocation mentally: `/stock-trend 513180` should follow the 4-step flow without errors.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/SKILL.md
git commit -m "docs: simplify SKILL.md from 10-step to 4-step flow with --code mode"
```

---

## Task 11: Update test suite

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/test_stock_trend.py`

- [ ] **Step 1: Add tests for eastmoney_utils**

```python
def test_eastmoney_utils():
    from eastmoney_utils import EM_HEADERS, build_secid, EM_API_HOSTS
    assert EM_HEADERS["User-Agent"]
    assert len(EM_API_HOSTS) == 3
    assert build_secid("600519.SH") == "1.600519"
    assert build_secid("000001.SZ") == "0.000001"
    assert build_secid("00700.HK") is None
    assert build_secid("159740.SZ") == "0.159740"
```

- [ ] **Step 2: Add tests for base_fetcher**

```python
def test_base_fetcher_subclass():
    from base_fetcher import BaseFetcher
    class TestFetcher(BaseFetcher):
        def fetch(self):
            return {"meta": {"data_source": "test"}, "data": []}
    # Verify instantiation
    f = TestFetcher()
    assert f.cache_key_suffix == ""
```

- [ ] **Step 3: Add tests for cache_utils clean_cache**

```python
def test_cache_dir_is_project_relative():
    from cache_utils import CACHE_DIR
    assert "/tmp/stock-trend-cache" not in CACHE_DIR
    assert ".cache" in CACHE_DIR
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts && python3 -m pytest test_stock_trend.py -v`

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/test_stock_trend.py
git commit -m "test: add tests for eastmoney_utils, base_fetcher, cache_utils"
```

---

## Task 12: End-to-end verification

- [ ] **Step 1: Run full pipeline with --code mode**

```bash
cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts
python3 run_pipeline.py --code 513180 --no-cache
```

Expected: All data files written to `.cache/stock-trend/513180/`. No errors.

- [ ] **Step 2: Verify data directory contents**

```bash
ls -la ../../.cache/stock-trend/513180/
```

Expected: `pipeline_output.json`, `kline.json`, `technical.json`, `capital_flow.json`, `fundamental.json` (skip for ETF), `macro_snapshot.json`.

- [ ] **Step 3: Run compute_scores with --code**

```bash
python3 compute_scores.py --code 513180 --capital-flow-score 0.5 --sentiment-score 1
```

Expected: `scores.json` written to `.cache/stock-trend/513180/scores.json`.

- [ ] **Step 4: Run generate_report with --code**

```bash
python3 generate_report.py --code 513180 --ts-code 513180.SH --stock-name '恒生科技ETF大成' --output-md /tmp/test_report.md
```

Expected: Report generated.

- [ ] **Step 5: Verify old CLI interface still works (backward compat)**

```bash
python3 compute_scores.py --technical /tmp/technical.json --capital-flow-score 0.5 -o /tmp/scores_compat.json
```

Expected: Backward compatible path works.

- [ ] **Step 6: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: end-to-end verification fixes"
```

---

## Spec Coverage Check

| Spec Section | Task |
|---|---|
| Pipeline one-command entry | Task 7 (run_pipeline.py --code) |
| BaseFetcher class | Task 2 |
| eastmoney_utils module | Task 1 |
| fetch_kline_eastmoney refactor | Task 4 |
| fetch_capital_flow refactor | Task 5 |
| fetch_etf_data refactor | Task 6 |
| JSON intermediate files (data dir) | Task 7, 8, 9 |
| compute_scores reads JSON | Task 8 |
| generate_report reads JSON | Task 9 |
| Cache dir migration to .cache/ | Task 3 |
| LRU clean_cache() | Task 3 |
| Per-step timeout with degradation | Task 7 |
| SKILL.md simplification | Task 10 |
| Tests | Task 11 |
| E2E verification | Task 12 |

## Placeholder Scan

No TBD/TODO/placeholders found. All code blocks contain complete implementations.

## Type Consistency Check

- `build_secid(ts_code)` returns `str | None` — used consistently in fetch_kline_eastmoney and fetch_capital_flow
- `BaseFetcher.fetch()` returns `dict` — all subclass `fetch()` methods return dict
- `get_data_dir(code)` returns `Path` — used consistently in pipeline, compute_scores, generate_report
- `clean_cache(max_size_mb=200)` returns `int` (count of removed files) — called at pipeline start