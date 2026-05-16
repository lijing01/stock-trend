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
import sys
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


def get_symbol_dir_name(symbol):
    """Extract directory name from symbol code.

    e.g. '600519.SH' -> '600519', '00700.HK' -> '00700'
    """
    return symbol["code"].split(".")[0]


def run_diff(config, verbose):
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
            current_path = CACHE_DIR / symbol_dir / script["output"]

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
            diffs = deep_diff(golden_data, current_data, "", config)

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

    For now, prints a placeholder message. Full implementation will
    be added in a follow-up task that runs each pipeline script
    against fixtures and saves the outputs as golden references.
    """
    print("Golden regeneration not yet implemented.")
    print("This will be added in a future task that runs the pipeline")
    print("scripts against fixture data and saves the outputs as golden references.")
    print("\nTo manually create golden files, run the pipeline for each symbol")
    print("and copy the cache outputs to the golden directory:")
    print(f"  Golden dir: {GOLDEN_DIR}")
    print(f"  Cache dir:  {CACHE_DIR}")


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

        run_diff(config, args.verbose)

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