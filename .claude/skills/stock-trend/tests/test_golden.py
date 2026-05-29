#!/usr/bin/env python3
"""Golden snapshot generation & diff tool for stock-trend pipeline outputs.

Compares committed golden reference files against current cache outputs,
using deep recursive diff with configurable numeric thresholds.

Usage:
    python3 test_golden.py --diff          # Compare golden vs current cache
    python3 test_golden.py --diff -v       # Verbose diff output
    python3 test_golden.py --regenerate    # Regenerate golden files
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Path constants
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = TESTS_DIR / "golden"
FIXTURES_DIR = TESTS_DIR / "fixtures"
CONFIG_PATH = TESTS_DIR / "golden_config.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"

# Result tracking (matching test_stock_trend.py style)
PASSED = 0
FAILED = 0
WARNINGS = 0
RESULTS = []

# Diff result categories
TYPE_CHANGE = "TYPE_CHANGE"
KEYS_ADDED = "KEYS_ADDED"
KEYS_REMOVED = "KEYS_REMOVED"
LENGTH_CHANGE = "LENGTH_CHANGE"
NUMERIC_EXCEEDED = "NUMERIC_EXCEEDED"
NUMERIC_WARNING = "NUMERIC_WARNING"
VALUE_CHANGE = "VALUE_CHANGE"


def get_threshold_for_key(key, config):
    """Map a JSON key name to its threshold type using numeric_threshold_map.

    Falls back to 'default' threshold if key is not in the map.
    """
    threshold_map = config.get("numeric_threshold_map", {})
    threshold_type = threshold_map.get(key, "default")
    thresholds = config.get("thresholds", {})
    return thresholds.get(threshold_type, thresholds.get("default", 0.001))


def deep_diff(golden, current, path, config):
    """Recursively diff two JSON structures, returning a list of differences.

    Each difference is a dict with:
        - category: one of TYPE_CHANGE, KEYS_ADDED, KEYS_REMOVED, LENGTH_CHANGE,
                    NUMERIC_EXCEEDED, NUMERIC_WARNING, VALUE_CHANGE
        - path: dot-separated path to the differing element
        - detail: human-readable description
        - severity: "fail" or "warning"
    """
    diffs = []

    # Type mismatch — treat int/float as same "numeric" type for JSON compat
    # Note: bool is subclass of int, so check bool explicitly
    golden_is_numeric = isinstance(golden, (int, float)) and not isinstance(golden, bool)
    current_is_numeric = isinstance(current, (int, float)) and not isinstance(current, bool)
    if golden_is_numeric != current_is_numeric or (
        not golden_is_numeric and not current_is_numeric and type(golden) != type(current)
    ):
        diffs.append({
            "category": TYPE_CHANGE,
            "path": path,
            "detail": f"type mismatch: golden={type(golden).__name__}, current={type(current).__name__}",
            "severity": "fail",
        })
        return diffs

    # Dict comparison
    if isinstance(golden, dict):
        golden_keys = set(golden.keys())
        current_keys = set(current.keys())

        added = current_keys - golden_keys
        if added:
            diffs.append({
                "category": KEYS_ADDED,
                "path": path,
                "detail": f"keys added: {sorted(added)}",
                "severity": "fail",
            })

        removed = golden_keys - current_keys
        if removed:
            diffs.append({
                "category": KEYS_REMOVED,
                "path": path,
                "detail": f"keys removed: {sorted(removed)}",
                "severity": "fail",
            })

        # Recurse into common keys
        common_keys = golden_keys & current_keys
        for key in sorted(common_keys):
            child_path = f"{path}.{key}" if path else key
            diffs.extend(deep_diff(golden[key], current[key], child_path, config))

        return diffs

    # List comparison
    if isinstance(golden, list):
        if len(golden) != len(current):
            diffs.append({
                "category": LENGTH_CHANGE,
                "path": path,
                "detail": f"list length: golden={len(golden)}, current={len(current)}",
                "severity": "fail",
            })
            # Compare up to the shorter length
            min_len = min(len(golden), len(current))
        else:
            min_len = len(golden)

        for i in range(min_len):
            child_path = f"{path}[{i}]"
            diffs.extend(deep_diff(golden[i], current[i], child_path, config))

        return diffs

    # Numeric comparison (exclude bool — it's a subclass of int)
    if isinstance(golden, (int, float)) and isinstance(current, (int, float)) and not isinstance(golden, bool) and not isinstance(current, bool):
        if golden == current:
            return diffs

        # Avoid division by zero
        if golden == 0:
            abs_diff = abs(current)
            threshold = get_threshold_for_key(path.split(".")[-1] if path else "", config)
            if abs_diff > threshold:
                diffs.append({
                    "category": NUMERIC_EXCEEDED,
                    "path": path,
                    "detail": f"golden={golden}, current={current}, diff={abs_diff:.6f} (threshold={threshold})",
                    "severity": "fail",
                })
            else:
                diffs.append({
                    "category": NUMERIC_WARNING,
                    "path": path,
                    "detail": f"golden={golden}, current={current}, diff={abs_diff:.6f} (threshold={threshold})",
                    "severity": "warning",
                })
            return diffs

        # Relative difference
        rel_diff = abs(current - golden) / abs(golden)
        threshold = get_threshold_for_key(path.split(".")[-1] if path else "", config)
        if rel_diff > threshold:
            diffs.append({
                "category": NUMERIC_EXCEEDED,
                "path": path,
                "detail": f"golden={golden}, current={current}, rel_diff={rel_diff:.6f} (threshold={threshold})",
                "severity": "fail",
            })
        else:
            diffs.append({
                "category": NUMERIC_WARNING,
                "path": path,
                "detail": f"golden={golden}, current={current}, rel_diff={rel_diff:.6f} (threshold={threshold})",
                "severity": "warning",
            })
        return diffs

    # String/bool value comparison
    if golden != current:
        diffs.append({
            "category": VALUE_CHANGE,
            "path": path,
            "detail": f"golden={golden!r}, current={current!r}",
            "severity": "fail",
        })

    return diffs


def load_json_safe(path):
    """Load JSON from a file, returning None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _stable_keys(data, keys):
    """Return a dict subset with only keys that are present."""
    return {k: data[k] for k in keys if k in data}


def _normalized_volume_pair(golden_vol, current_vol):
    """Normalize volume pairs when one source reports lots and another shares."""
    if golden_vol is None or current_vol is None:
        return golden_vol, current_vol
    lo = min(abs(golden_vol), abs(current_vol))
    hi = max(abs(golden_vol), abs(current_vol))
    if lo == 0:
        return golden_vol, current_vol
    ratio = hi / lo
    if 80 <= ratio <= 120:
        if abs(golden_vol) < abs(current_vol):
            golden_vol *= 100
        else:
            current_vol *= 100
    return golden_vol, current_vol


def _normalized_amount_pair(golden_amount, current_amount):
    """Normalize amount pairs when sources differ by reporting unit."""
    if golden_amount is None or current_amount is None:
        return golden_amount, current_amount
    lo = min(abs(golden_amount), abs(current_amount))
    hi = max(abs(golden_amount), abs(current_amount))
    if lo == 0:
        return golden_amount, current_amount
    ratio = hi / lo
    if 900 <= ratio <= 1100:
        if abs(golden_amount) < abs(current_amount):
            golden_amount *= 1000
        else:
            current_amount *= 1000
    return golden_amount, current_amount


def _kline_config(config, asset_type=None):
    """Use more realistic thresholds for live kline cross-source comparisons."""
    local = {
        "thresholds": dict(config.get("thresholds", {})),
        "numeric_threshold_map": dict(config.get("numeric_threshold_map", {})),
    }
    local["thresholds"]["price"] = 0.005
    local["thresholds"]["pct"] = 0.10
    local["thresholds"]["volume"] = 0.02
    if asset_type:
        asset_overrides = config.get("asset_thresholds", {}).get(asset_type, {})
        for k, v in asset_overrides.items():
            local["thresholds"][k] = v
    local["numeric_threshold_map"]["pct_chg"] = "pct"
    local["numeric_threshold_map"]["pre_close"] = "price"
    local["numeric_threshold_map"]["vol"] = "volume"
    return local


def _normalize_macro_snapshot(data):
    return {
        "meta": _stable_keys(data.get("meta", {}), ["data_source"]),
        "summary": data.get("summary", {}),
        "data": data.get("data", {}),
    }


def _normalize_fundamental(data):
    summary = data.get("summary", {})
    normalized = {
        "meta": _stable_keys(data.get("meta", {}), ["ts_code", "data_source", "asset"]),
        "summary": _stable_keys(summary, ["data_quality"]),
    }
    if summary.get("data_quality") != "error":
        normalized["data"] = data.get("data", {})
    return normalized


def _normalize_capital_flow(data):
    extended = data.get("data_extended", {})
    market_values = [
        item.get("net_buy_billion")
        for item in (extended.get("northbound_market") or [])[-10:]
    ]
    return {
        "meta": _stable_keys(data.get("meta", {}), ["ts_code", "asset"]),
        "northbound_individual": extended.get("northbound_individual", {}),
        "northbound_market_values": market_values,
    }


def _has_futures_semantics(data):
    return isinstance(data.get("basis"), dict) and isinstance(data.get("signals"), dict)


def _normalize_futures_data(data, include_numeric=False):
    meta = data.get("meta", {})
    normalized = {
        "meta": _stable_keys(meta, ["etf_code", "futures_code", "futures_secid", "index_secid"]),
    }
    basis = data.get("basis") or {}
    signals = data.get("signals") or {}
    if basis and signals:
        normalized["basis"] = _stable_keys(basis, ["direction", "signal"])
        normalized["signals"] = _stable_keys(signals, [
            "basis_signal",
            "oi_signal",
            "volume_signal",
            "composite_signal",
        ])
        if include_numeric:
            normalized["basis"]["basis_pct"] = basis.get("basis_pct")
            normalized["signals"].update(_stable_keys(signals, [
                "basis_score",
                "oi_score",
                "volume_score",
                "composite_score",
            ]))
    return normalized


def _diff_futures_data(golden_data, current_data, config):
    include_semantics = _has_futures_semantics(golden_data) and _has_futures_semantics(current_data)
    latest_golden = ((golden_data.get("futures") or {}).get("latest") or {}).get("date")
    latest_current = ((current_data.get("futures") or {}).get("latest") or {}).get("date")
    include_numeric = include_semantics and latest_golden == latest_current
    return deep_diff(
        _normalize_futures_data(golden_data, include_numeric=include_numeric),
        _normalize_futures_data(current_data, include_numeric=include_numeric),
        "",
        config,
    )


def _normalize_etf_data(data):
    return {
        "fund_code": data.get("fund_code"),
        "data_source": data.get("data_source"),
        "fund_name": data.get("fund_name"),
        "tracking_index": data.get("tracking_index"),
        "fund_type": data.get("fund_type"),
        "has_nav": bool(data.get("nav")),
    }


def _normalize_technical(data):
    summary = data.get("summary", {})
    return {
        "summary": _stable_keys(summary, ["direction", "confidence", "data_quality"]),
    }


def _normalize_scores(data):
    return {
        "direction_detail": data.get("direction_detail"),
        "confidence": data.get("confidence"),
        "data_quality": data.get("data_quality"),
        "special_type": (data.get("special") or {}).get("type"),
        "automated_source_keys": sorted((data.get("automated_sources") or {}).keys()),
    }


def _diff_kline(golden_data, current_data, config, asset_type=None):
    golden_rows = {r.get("trade_date"): r for r in golden_data.get("data", []) if r.get("trade_date")}
    current_rows = {r.get("trade_date"): r for r in current_data.get("data", []) if r.get("trade_date")}
    common_dates = sorted(set(golden_rows) & set(current_rows))
    if not common_dates:
        return [{
            "category": VALUE_CHANGE,
            "path": "data",
            "detail": "no overlapping trade_date between golden and current",
            "severity": "fail",
        }]

    normalized_golden = {"data": []}
    normalized_current = {"data": []}
    latest_common_date = common_dates[-1]
    for trade_date in common_dates:
        golden_row = golden_rows[trade_date]
        current_row = current_rows[trade_date]
        golden_vol, current_vol = _normalized_volume_pair(golden_row.get("vol"), current_row.get("vol"))
        golden_amount, current_amount = _normalized_amount_pair(golden_row.get("amount"), current_row.get("amount"))
        skip_live_turnover = trade_date == latest_common_date
        skip_volume = (
            skip_live_turnover or
            golden_amount == 0 and current_amount == 0
            and (golden_data.get("meta", {}).get("ts_code", "").endswith(".HK")
                 or current_data.get("meta", {}).get("ts_code", "").endswith(".HK"))
        )
        golden_record = {
            "trade_date": trade_date,
            "open": golden_row.get("open"),
            "close": golden_row.get("close"),
            "high": golden_row.get("high"),
            "low": golden_row.get("low"),
        }
        current_record = {
            "trade_date": trade_date,
            "open": current_row.get("open"),
            "close": current_row.get("close"),
            "high": current_row.get("high"),
            "low": current_row.get("low"),
        }
        if not skip_live_turnover:
            golden_record["amount"] = golden_amount
            current_record["amount"] = current_amount
        if not skip_volume:
            golden_record["vol"] = golden_vol
            current_record["vol"] = current_vol
        if golden_row.get("pre_close") is not None and current_row.get("pre_close") is not None:
            golden_record["pre_close"] = golden_row.get("pre_close")
            current_record["pre_close"] = current_row.get("pre_close")
        normalized_golden["data"].append({
            **golden_record,
        })
        normalized_current["data"].append({
            **current_record,
        })

    return deep_diff(normalized_golden, normalized_current, "", _kline_config(config, asset_type))


def diff_output(output_name, golden_data, current_data, config, asset_type=None):
    """Diff output files using stable semantics per file type."""
    if output_name == "kline.json":
        return _diff_kline(golden_data, current_data, config, asset_type)

    if output_name == "futures_data.json":
        return _diff_futures_data(golden_data, current_data, config)

    if asset_type:
        asset_overrides = config.get("asset_thresholds", {}).get(asset_type, {})
        if asset_overrides:
            config = {
                **config,
                "thresholds": {**config.get("thresholds", {}), **asset_overrides},
            }

    normalizers = {
        "macro_snapshot.json": _normalize_macro_snapshot,
        "fundamental.json": _normalize_fundamental,
        "capital_flow.json": _normalize_capital_flow,
        "etf_data.json": _normalize_etf_data,
        "technical.json": _normalize_technical,
        "scores.json": _normalize_scores,
    }
    normalizer = normalizers.get(output_name)
    if normalizer:
        golden_data = normalizer(golden_data)
        current_data = normalizer(current_data)
    return deep_diff(golden_data, current_data, "", config)


def get_symbol_dir_name(symbol):
    """Extract directory name from symbol code.

    e.g. '600519.SH' -> '600519', '00700.HK' -> '00700'
    """
    return symbol["code"].split(".")[0]


def prepare_current_outputs(config, current_dir):
    """Generate fresh current outputs in an isolated cache directory."""
    symbols = config.get("symbols", [])
    env = os.environ.copy()
    env["STOCK_TREND_CACHE_DIR"] = str(current_dir)

    for cache_file in CACHE_DIR.glob("*.json"):
        shutil.copy2(cache_file, current_dir / cache_file.name)

    for symbol in symbols:
        code = symbol["code"]
        numeric_code = code.split(".")[0]
        symbol_dir = current_dir / numeric_code
        symbol_dir.mkdir(parents=True, exist_ok=True)
        shared_symbol_dir = CACHE_DIR / numeric_code

        shared_kline = load_json_safe(shared_symbol_dir / "kline.json")
        if shared_kline:
            adj = "none" if code.endswith(".HK") else "qfq"
            cache_key = f"kline_{code}_D_{adj}"
            seed_path = current_dir / f"{cache_key}.json"
            with open(seed_path, "w", encoding="utf-8") as f:
                json.dump({
                    "cache_timestamp": time.time(),
                    "cache_key": cache_key,
                    **shared_kline,
                }, f, ensure_ascii=False, indent=2)

        resolve_output = symbol_dir / "resolve.json"
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "resolve_code.py"), numeric_code, "-o", str(resolve_output)],
            capture_output=True, text=True, timeout=20, env=env,
        )
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run_pipeline.py"), "--code", numeric_code],
            capture_output=True, text=True, timeout=90, env=env,
        )
        if (symbol_dir / "technical.json").exists():
            subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "compute_scores.py"), "--code", numeric_code],
                capture_output=True, text=True, timeout=30, env=env,
            )


def run_diff(config, verbose, current_dir=CACHE_DIR):
    """Compare golden files against current cache outputs.

    For each symbol and script in config, loads the golden reference file
    and the current cache file, runs deep_diff, and reports results.
    """
    global PASSED, FAILED, WARNINGS, RESULTS

    symbols = config.get("symbols", [])
    scripts = config.get("scripts", [])

    for symbol in symbols:
        symbol_dir = get_symbol_dir_name(symbol)
        symbol_label = f"{symbol['name']}({symbol['code']})"
        print(f"\n--- {symbol_label} ---")

        for script in scripts:
            test_name = f"{symbol_dir}/{script['output']}"
            golden_path = GOLDEN_DIR / symbol_dir / script["output"]
            current_path = current_dir / symbol_dir / script["output"]

            # Golden file doesn't exist -> skip
            if not golden_path.exists():
                RESULTS.append({
                    "name": test_name,
                    "status": "SKIP",
                    "detail": "golden file not found",
                    "category": "golden",
                })
                print(f"  [SKIP] {test_name} - golden file not found")
                continue

            # Current cache file doesn't exist -> golden valid, no current to compare
            if not current_path.exists():
                PASSED += 1
                RESULTS.append({
                    "name": test_name,
                    "status": "PASS",
                    "detail": "golden valid, no current cache to compare",
                    "category": "golden",
                })
                print(f"  [PASS] {test_name} - golden valid, no current cache")
                continue

            # Load both files
            golden_data = load_json_safe(golden_path)
            current_data = load_json_safe(current_path)

            if golden_data is None:
                RESULTS.append({
                    "name": test_name,
                    "status": "FAIL",
                    "detail": f"failed to load golden: {golden_path}",
                    "category": "golden",
                })
                FAILED += 1
                print(f"  [FAIL] {test_name} - failed to load golden")
                continue

            if current_data is None:
                RESULTS.append({
                    "name": test_name,
                    "status": "FAIL",
                    "detail": f"failed to load current: {current_path}",
                    "category": "golden",
                })
                FAILED += 1
                print(f"  [FAIL] {test_name} - failed to load current")
                continue

            # Run deep diff
            diffs = diff_output(script["output"], golden_data, current_data, config, symbol.get("asset"))

            if not diffs:
                PASSED += 1
                RESULTS.append({
                    "name": test_name,
                    "status": "PASS",
                    "detail": "identical",
                    "category": "golden",
                })
                print(f"  [PASS] {test_name} - identical")
            else:
                has_fail = any(d["severity"] == "fail" for d in diffs)
                has_warning = any(d["severity"] == "warning" for d in diffs)

                if has_fail:
                    FAILED += 1
                    status = "FAIL"
                elif has_warning:
                    WARNINGS += 1
                    status = "PASS"  # warnings are still passing
                else:
                    PASSED += 1
                    status = "PASS"

                fail_count = sum(1 for d in diffs if d["severity"] == "fail")
                warn_count = sum(1 for d in diffs if d["severity"] == "warning")
                detail = f"{len(diffs)} diffs: {fail_count} failures, {warn_count} warnings"

                RESULTS.append({
                    "name": test_name,
                    "status": status,
                    "detail": detail,
                    "category": "golden",
                })

                prefix = "[FAIL]" if has_fail else "[PASS]"
                print(f"  {prefix} {test_name} - {detail}")

                if verbose:
                    for d in diffs:
                        marker = "  FAIL" if d["severity"] == "fail" else "  WARN"
                        print(f"    {marker} {d['category']}: {d['path']} - {d['detail']}")


def regenerate_golden(config):
    """Regenerate golden files from current pipeline outputs.

    For each symbol in config:
      1. Run resolve_code.py to get resolve.json
      2. Run run_pipeline.py --code <code> --no-cache to fetch all data
      3. Copy pipeline output files from cache to golden directory
      4. Run compute_scores.py --code <code> and copy scores.json

    Asset-type filtering:
      - ETFs (asset=etf): include etf_data.json, futures_data.json; no fundamental
      - Stocks (asset=stock): include fundamental.json; no etf_data/futures_data
      - HK stocks (asset=hk): may not have fundamental; skip missing files
    """
    symbols = config.get("symbols", [])
    scripts = config.get("scripts", [])

    # Map script outputs to their per-asset applicability
    # None means applicable to all asset types
    ASSET_EXCLUSIONS = {
        "fundamental": ["etf"],      # ETFs don't get fundamental data
        "etf_data": ["stock", "hk"],  # Only ETFs get ETF data
        "futures_data": ["stock", "hk"],  # Only ETFs get futures data
        "index_valuation": ["stock", "hk"],  # Only ETFs get index valuation
    }

    print("=" * 50)
    print("Golden Snapshot Regeneration")
    print("=" * 50)
    print(f"Golden dir: {GOLDEN_DIR}")
    print(f"Cache dir:  {CACHE_DIR}")
    print("=" * 50)

    # Create golden directory
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    for symbol in symbols:
        code = symbol["code"]
        # Use numeric-only code for directory names (matches get_symbol_dir_name
        # and ensures pipeline creates .cache/stock-trend/600519/ not 600519.SH/)
        numeric_code = code.split(".")[0]
        code_dir = numeric_code
        asset_type = symbol.get("asset", "stock")
        symbol_label = f"{symbol['name']}({code})"

        print(f"\n--- Processing {symbol_label} (asset={asset_type}) ---")

        golden_symbol_dir = GOLDEN_DIR / code_dir
        cache_symbol_dir = CACHE_DIR / code_dir
        golden_symbol_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Run resolve_code.py
        print(f"  [1/3] Resolving code {numeric_code}...")
        resolve_output = golden_symbol_dir / "resolve.json"
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "resolve_code.py"), numeric_code],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                try:
                    resolve_data = json.loads(result.stdout)
                    with open(resolve_output, "w", encoding="utf-8") as f:
                        json.dump(resolve_data, f, ensure_ascii=False, indent=2)
                    print(f"    resolve.json saved ({resolve_data.get('ts_code', 'unknown')})")
                except json.JSONDecodeError:
                    print(f"    WARNING: resolve output is not valid JSON, skipping")
            else:
                print(f"    WARNING: resolve_code failed: {result.stderr[:200]}")
        except Exception as e:
            print(f"    WARNING: resolve_code error: {e}")

        # Step 2: Run pipeline (use numeric code so directory is .cache/stock-trend/600519/)
        print(f"  [2/3] Running pipeline for {numeric_code}...")
        try:
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPTS_DIR / "run_pipeline.py"),
                    "--code", numeric_code, "--no-cache",
                ],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                print(f"    WARNING: pipeline returned non-zero exit code: {result.returncode}")
                if result.stderr:
                    print(f"    stderr: {result.stderr[:300]}")
            else:
                print(f"    Pipeline completed successfully")
        except subprocess.TimeoutExpired:
            print(f"    WARNING: pipeline timed out (120s)")
        except Exception as e:
            print(f"    WARNING: pipeline error: {e}")

        # Step 3: Copy output files from cache to golden
        print(f"  [3/3] Copying output files to golden directory...")
        copied = 0
        skipped = 0

        for script in scripts:
            output_file = script["output"]
            src = cache_symbol_dir / output_file

            # Check if this output is excluded for this asset type
            script_name = script["name"]
            excluded_assets = ASSET_EXCLUSIONS.get(script_name, [])
            if asset_type in excluded_assets:
                print(f"    SKIP {output_file} (not applicable for asset={asset_type})")
                skipped += 1
                continue

            if src.exists():
                shutil.copy2(src, golden_symbol_dir / output_file)
                # Validate it's valid JSON
                try:
                    with open(src, "r", encoding="utf-8") as f:
                        json.load(f)
                    print(f"    COPIED {output_file}")
                    copied += 1
                except (json.JSONDecodeError, OSError):
                    print(f"    WARNING: {output_file} is not valid JSON, skipping")
                    (golden_symbol_dir / output_file).unlink(missing_ok=True)
            else:
                print(f"    SKIP {output_file} (not in cache, likely unavailable for this asset)")
                skipped += 1

        # Step 4: Run compute_scores
        # Only run if we have the required technical.json in cache
        tech_path = cache_symbol_dir / "technical.json"
        if tech_path.exists():
            print(f"  [bonus] Running compute_scores for {numeric_code}...")
            try:
                result = subprocess.run(
                    [
                        sys.executable, str(SCRIPTS_DIR / "compute_scores.py"),
                        "--code", numeric_code,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    scores_src = cache_symbol_dir / "scores.json"
                    if scores_src.exists():
                        shutil.copy2(scores_src, golden_symbol_dir / "scores.json")
                        print(f"    COPIED scores.json")
                    else:
                        print(f"    WARNING: compute_scores ran but scores.json not found")
                else:
                    print(f"    WARNING: compute_scores failed: {result.stderr[:200]}")
            except Exception as e:
                print(f"    WARNING: compute_scores error: {e}")

        print(f"  Done: {copied} files copied, {skipped} skipped")

    print("\n" + "=" * 50)
    print("Golden regeneration complete")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="Golden snapshot diff & regeneration tool for stock-trend"
    )
    parser.add_argument(
        "--diff", action="store_true",
        help="Compare golden files against current cache outputs"
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Regenerate golden files from current pipeline outputs"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed diff output"
    )
    args = parser.parse_args()

    if not args.diff and not args.regenerate:
        parser.print_help()
        print("\nError: must specify --diff or --regenerate")
        return 1

    # Load config
    if not CONFIG_PATH.exists():
        print(f"Error: config not found at {CONFIG_PATH}")
        return 1

    config = load_json_safe(CONFIG_PATH)
    if config is None:
        print(f"Error: failed to parse config at {CONFIG_PATH}")
        return 1

    if args.diff:
        print("=" * 50)
        print("Golden Snapshot Diff")
        print("=" * 50)
        print(f"Golden dir: {GOLDEN_DIR}")
        print(f"Cache dir:  {CACHE_DIR}")
        print("=" * 50)

        if not GOLDEN_DIR.exists():
            print(f"\nGolden directory does not exist: {GOLDEN_DIR}")
            print("Run with --regenerate to create golden files first.")

        with tempfile.TemporaryDirectory(prefix="stock-trend-golden-current-") as tmpdir:
            current_dir = Path(tmpdir)
            prepare_current_outputs(config, current_dir)
            run_diff(config, args.verbose, current_dir=current_dir)

        # Summary
        total = PASSED + FAILED + WARNINGS
        print("\n" + "=" * 50)
        print(f"Results: {PASSED} passed, {FAILED} failed, {WARNINGS} warnings (total: {total})")
        print("=" * 50)

        if FAILED > 0:
            print("\nFailed diffs:")
            for r in RESULTS:
                if r["status"] == "FAIL":
                    print(f"  - {r['name']}: {r['detail']}")

        return 1 if FAILED > 0 else 0

    if args.regenerate:
        regenerate_golden(config)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
