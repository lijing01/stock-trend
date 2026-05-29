#!/usr/bin/env python3
"""Pipeline orchestration script for stock-trend skill.

Runs all data fetching and analysis steps in sequence, with automatic
Tushare fallback and parallel execution where possible.

Usage:
    python3 run_pipeline.py <ts_code> [options]
    python3 run_pipeline.py --code <code> [options]  (one-command entry, auto-resolve)

Examples:
    python3 run_pipeline.py --code 513180
    python3 run_pipeline.py 159740.SZ --asset FD --adj qfq -o /tmp
    python3 run_pipeline.py 600519.SH -o /tmp
    python3 run_pipeline.py 00700.HK -o /tmp
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.cache_utils import clean_cache, safe_float, run_script
from core.eastmoney_utils import latest_kline_record
from core.resolve_code import resolve_and_save

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def get_data_dir(code):
    """Return data directory path for a given code."""
    from core.cache_utils import CACHE_DIR
    d = Path(CACHE_DIR) / code
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path):
    """Read JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_successful_kline(kline_data):
    """Return True only when the current K-line payload has usable rows."""
    if not isinstance(kline_data, dict):
        return False
    if kline_data.get("meta", {}).get("data_source") == "error":
        return False
    rows = kline_data.get("data")
    if not isinstance(rows, list) or not rows:
        return False
    latest_row = latest_kline_record(rows)
    if latest_row is None:
        return False
    return all(safe_float(latest_row.get(key)) is not None for key in ("open", "high", "low", "close"))


def remove_stale_file(path, label, errors):
    """Delete stale downstream output so report generation cannot reuse it."""
    if not path or not Path(path).exists():
        return False
    try:
        Path(path).unlink()
        errors.append(f"Removed stale {label}: {path}")
        return True
    except OSError as exc:
        errors.append(f"Failed to remove stale {label}: {path}: {exc}")
        return False


def build_output_files(
    output_dir,
    kline_path,
    kline_available,
    technical_available,
    chip_available,
    is_etf,
    no_etf,
    no_capital,
    no_fundamental,
    no_macro,
    no_futures,
    no_index_valuation,
    asset,
):
    """Build pipeline output file map using freshness flags from this run."""
    return {
        "kline": kline_path if kline_available else None,
        "technical": str(output_dir / "technical.json") if technical_available else None,
        "etf_data": str(output_dir / "etf_data.json") if is_etf and not no_etf else None,
        "capital_flow": str(output_dir / "capital_flow.json") if not no_capital else None,
        "fundamental": str(output_dir / "fundamental.json") if not no_fundamental and asset != "FD" else None,
        "macro_snapshot": str(output_dir / "macro_snapshot.json") if not no_macro else None,
        "futures_data": str(output_dir / "futures_data.json") if is_etf and not no_futures else None,
        "index_valuation": str(output_dir / "index_valuation.json") if is_etf and not no_index_valuation else None,
        "chip_distribution": str(output_dir / "chip_distribution.json") if chip_available else None,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run full stock-trend data pipeline"
    )
    # --code mode: one-command entry, auto-resolves and writes to data dir
    parser.add_argument("--code", help="Stock/ETF code (e.g. 513180). Auto-resolves and runs full pipeline.")
    parser.add_argument("ts_code", nargs="?", help="Tushare-format code (e.g. 600519.SH, 159740.SZ). Not needed with --code.")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], help="Adjustment type (auto-detected if omitted)")
    parser.add_argument("--freq", choices=["D", "W"], default="D", help="K-line frequency (default: D)")
    parser.add_argument("--no-etf", action="store_true", help="Skip ETF data fetch")
    parser.add_argument("--no-capital", action="store_true", help="Skip capital flow fetch")
    parser.add_argument("--no-fundamental", action="store_true", help="Skip fundamental data fetch")
    parser.add_argument("--no-macro", action="store_true", help="Skip macro snapshot fetch")
    parser.add_argument("--no-futures", action="store_true", help="Skip futures data fetch (ETF only)")
    parser.add_argument("--no-index-valuation", action="store_true", help="Skip index PE valuation fetch (ETF only)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore all cache")
    parser.add_argument("-o", "--output-dir", default=None, help="Output directory (default: .cache/stock-trend/{code}/). Ignored when --code is used.")
    args = parser.parse_args()

    # Clean cache on pipeline start
    removed = clean_cache()
    if removed:
        print(f"Cache cleanup: removed {removed} stale files")

    asset = None
    adj = None

    # --code mode: auto-resolve
    if args.code and not args.ts_code:
        resolve_path = str(get_data_dir(args.code) / "resolve.json")
        resolve_data = resolve_and_save(args.code, output_path=resolve_path)
        if "error" in resolve_data:
            print(f"Error: could not resolve code {args.code}: {resolve_data['error']}", file=sys.stderr)
            sys.exit(1)
        if not resolve_data.get("ts_code"):
            print(f"Error: could not resolve code {args.code}", file=sys.stderr)
            sys.exit(1)
        ts_code = resolve_data["ts_code"]
        code = ts_code.split(".")[0]
        asset = resolve_data.get("asset")
        adj = resolve_data.get("adj")
        output_dir = get_data_dir(args.code)
    elif args.ts_code:
        ts_code = args.ts_code
        code = ts_code.split(".")[0]
        output_dir = Path(args.output_dir) if args.output_dir else get_data_dir(code)
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        parser.error("Provide either ts_code (positional) or --code")

    # Resolve asset/adj: CLI flag > resolve_code > auto-detect
    if args.asset:
        asset = args.asset
    elif not asset:
        asset = "FD" if code.startswith(("5", "15")) else "E"

    if args.adj:
        adj = args.adj
    elif not adj:
        adj = "none" if ts_code.endswith(".HK") else "qfq"

    is_etf = asset == "FD"
    is_hk = ts_code.endswith(".HK")

    pipeline_start = time.time()
    errors = []
    timeouts = []
    results = {}
    kline_available = False
    technical_available = False
    chip_available = False
    chip_result = {"success": False}

    # --- Step 1: Quick diagnostic ---
    print(f"[1/5] Running diagnostic...")
    diag_result = run_script(
        [sys.executable, str(SCRIPT_DIR / "diagnose.py"), "--quick"],
        label="diagnostic",
    )
    if diag_result.get("timeout"):
        timeouts.append("diagnostic")
    tushare_available = True  # Assume available, will check via kline fetch

    # --- Step 2: Fetch K-line data ---
    print(f"[2/5] Fetching K-line data for {ts_code}...")
    kline_path = str(output_dir / "kline.json")

    # Try Tushare first
    kline_cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/kline.py"),
        ts_code, "--asset", asset, "--freq", args.freq,
        "--adj", adj, "-o", kline_path,
    ]
    if args.no_cache:
        kline_cmd.append("--no-cache")
    kline_result = run_script(kline_cmd, label="fetch_kline_tushare")
    if kline_result.get("timeout"):
        timeouts.append("fetch_kline_tushare")

    kline_data = read_json(kline_path)
    need_fallback = False

    if not kline_result["success"] or kline_data is None:
        need_fallback = True
    elif kline_data.get("meta", {}).get("data_source") == "error":
        error_type = kline_data.get("meta", {}).get("error_type", "")
        if error_type == "permission":
            print(f"  Tushare permission denied, falling back to East Money...")
            need_fallback = True
        else:
            print(f"  Tushare error: {kline_data.get('meta', {}).get('error', 'unknown')}")
            need_fallback = True

    if need_fallback:
        print(f"  Falling back to East Money...")
        fallback_cmd = [
                sys.executable, str(SCRIPT_DIR / "fetchers/kline_eastmoney.py"),
                ts_code, "--asset", asset, "--freq", args.freq,
                "-o", kline_path,
            ]
        if args.no_cache:
            fallback_cmd.append("--no-cache")
        fallback_result = run_script(fallback_cmd, label="fetch_kline_eastmoney")
        if fallback_result.get("timeout"):
            timeouts.append("fetch_kline_eastmoney")
        if not fallback_result["success"]:
            errors.append(f"K-line fetch failed: {fallback_result['stderr']}")
        kline_data = read_json(kline_path)

    if kline_data:
        data_source = kline_data.get("meta", {}).get("data_source", "unknown")
        record_count = kline_data.get("meta", {}).get("record_count", 0)
        kline_available = is_successful_kline(kline_data)
        print(f"  K-line data: {data_source}, {record_count} records")
        results["kline"] = {
            "data_source": data_source,
            "record_count": record_count,
        }
        if not kline_available:
            errors.append("K-line data unavailable or empty")
    else:
        errors.append("K-line data unavailable")
        results["kline"] = {"data_source": "error", "record_count": 0}

    # --- Step 3: Chip distribution analysis (depends on kline, runs before technical) ---
    chip_distribution_path = str(output_dir / "chip_distribution.json")
    if kline_available:
        print(f"[3/5] Computing chip distribution...")
        chip_cmd = [
            sys.executable, str(SCRIPT_DIR / "analysis/chip_distribution.py"),
            kline_path, "-o", chip_distribution_path,
        ]
        chip_result = run_script(chip_cmd, label="compute_chip_distribution", timeout=15)
        if chip_result.get("timeout"):
            timeouts.append("compute_chip_distribution")
        if chip_result["success"]:
            chip_data = read_json(chip_distribution_path)
            if chip_data and "error" not in chip_data:
                chip_available = True
                avg_cost = chip_data.get("avg_cost")
                profit_ratio = chip_data.get("profit_ratio")
                concentration = chip_data.get("concentration")
                print(f"  Chip distribution: avg_cost={avg_cost}, "
                      f"profit_ratio={profit_ratio:.1%}, concentration={concentration:.1%}")
                results["chip_distribution"] = {
                    "avg_cost": avg_cost,
                    "profit_ratio": profit_ratio,
                    "concentration": concentration,
                }
            else:
                print(f"  Chip distribution skipped: {chip_data.get('detail', 'unknown') if chip_data else 'no output'}")
        else:
            print(f"  Chip distribution failed: {chip_result['stderr']}")
    else:
        print(f"[3/5] Skipping chip distribution (no K-line data)")

    # --- Step 3.5: Technical analysis (depends on kline, optionally chip distribution) ---
    technical_path = str(output_dir / "technical.json")
    if kline_available:
        print(f"[3.5/5] Running technical analysis...")
        tech_cmd = [
            sys.executable, str(SCRIPT_DIR / "analysis/technical.py"),
            kline_path, "-o", technical_path,
        ]
        if is_etf:
            tech_cmd.append("--etf")
        # Pass chip distribution for S/R enrichment
        if chip_result["success"] and read_json(chip_distribution_path):
            tech_cmd.extend(["--chip-distribution", chip_distribution_path])
        tech_result = run_script(tech_cmd,
            label="analyze_technical",
        )
        if tech_result.get("timeout"):
            timeouts.append("analyze_technical")
        if not tech_result["success"]:
            errors.append(f"Technical analysis failed: {tech_result['stderr']}")

        tech_data = read_json(technical_path)
        if tech_result["success"] and tech_data:
            technical_available = True
            summary = tech_data.get("summary", {})
            results["technical"] = {
                "total_score": summary.get("total_score"),
                "direction": summary.get("direction"),
                "confidence": summary.get("confidence"),
                "data_quality": summary.get("data_quality"),
            }
            print(f"  Technical: score={summary.get('total_score')}, "
                  f"direction={summary.get('direction')}, "
                  f"confidence={summary.get('confidence')}")
    else:
        print(f"[3.5/5] Skipping technical analysis (no K-line data)")
        remove_stale_file(technical_path, "technical analysis", errors)
        remove_stale_file(chip_distribution_path, "chip distribution", errors)
        remove_stale_file(kline_path, "K-line data", errors)

    # --- Step 4: ETF data and capital flow (parallel, independent) ---
    print(f"[4/5] Fetching supplementary data...")

    parallel_tasks = []

    # ETF data
    if is_etf and not args.no_etf:
        etf_path = str(output_dir / "etf_data.json")
        parallel_tasks.append((
            [
                sys.executable, str(SCRIPT_DIR / "fetchers/etf_data.py"),
                code, "-o", etf_path,
            ],
            "fetch_etf_data",
            etf_path,
        ))

    # Capital flow
    if not args.no_capital:
        capital_path = str(output_dir / "capital_flow.json")
        capital_cmd = [
                sys.executable, str(SCRIPT_DIR / "fetchers/capital_flow.py"),
                ts_code, "--asset", asset, "-o", capital_path,
            ]
        if args.no_cache:
            capital_cmd.append("--no-cache")
        parallel_tasks.append((
            capital_cmd,
            "fetch_capital_flow",
            capital_path,
        ))

    # Fundamental data (skip for ETFs)
    if not args.no_fundamental and asset != "FD":
        fundamental_path = str(output_dir / "fundamental.json")
        fundamental_cmd = [
                sys.executable, str(SCRIPT_DIR / "fetchers/fundamental.py"),
                ts_code, "--asset", asset, "-o", fundamental_path,
            ]
        if args.no_cache:
            fundamental_cmd.append("--no-cache")
        parallel_tasks.append((
            fundamental_cmd,
            "fetch_fundamental",
            fundamental_path,
        ))

    # Macro snapshot (market-level, always)
    if not args.no_macro:
        macro_path = str(output_dir / "macro_snapshot.json")
        macro_cmd = [
                sys.executable, str(SCRIPT_DIR / "fetchers/macro_snapshot.py"),
                "-o", macro_path,
            ]
        if args.no_cache:
            macro_cmd.append("--no-cache")
        parallel_tasks.append((
            macro_cmd,
            "fetch_macro_snapshot",
            macro_path,
        ))

    # Futures data (ETF only, requires ETF code mapping)
    if is_etf and not args.no_futures:
        futures_path = str(output_dir / "futures_data.json")
        futures_cmd = [
            sys.executable, str(SCRIPT_DIR / "fetchers/futures_data.py"),
            code, "-o", futures_path,
        ]
        if args.no_cache:
            futures_cmd.append("--no-cache")
        parallel_tasks.append((
            futures_cmd,
            "fetch_futures_data",
            futures_path,
        ))

    # Index valuation (ETF only)
    if is_etf and not args.no_index_valuation:
        index_valuation_path = str(output_dir / "index_valuation.json")
        index_valuation_cmd = [
            sys.executable, str(SCRIPT_DIR / "fetchers/index_valuation.py"),
            "--code", code, "-o", index_valuation_path,
        ]
        if args.no_cache:
            index_valuation_cmd.append("--no-cache")
        parallel_tasks.append((
            index_valuation_cmd,
            "fetch_index_valuation",
            index_valuation_path,
        ))

    if parallel_tasks:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(run_script, cmd, label): (label, path)
                for cmd, label, path in parallel_tasks
            }
            for future in as_completed(futures):
                label, path = futures[future]
                task_result = future.result()
                if task_result.get("timeout"):
                    timeouts.append(label)
                if not task_result["success"]:
                    errors.append(f"{label} failed: {task_result['stderr']}")
                    continue

                data = read_json(path)
                if label == "fetch_etf_data" and data:
                    nav_info = data.get("nav", {})
                    results["etf_data"] = {
                        "fund_name": data.get("fund_name"),
                        "nav": nav_info.get("nav"),
                        "iopv_premium_pct": nav_info.get("iopv_premium_pct"),
                    }
                    print(f"  ETF: {data.get('fund_name')}, "
                          f"NAV={nav_info.get('nav')}, "
                          f"IOPV溢价={nav_info.get('iopv_premium_pct')}%")
                elif label == "fetch_capital_flow" and data:
                    count = data.get("meta", {}).get("record_count", 0)
                    results["capital_flow"] = {"record_count": count}
                    print(f"  Capital flow: {count} records")
                elif label == "fetch_fundamental" and data:
                    dq = data.get("summary", {}).get("data_quality", "error")
                    has_pe = data.get("summary", {}).get("pe_ttm")
                    results["fundamental"] = {"data_quality": dq, "has_pe": has_pe is not None}
                    print(f"  Fundamental: {dq}{' (PE=' + str(has_pe) + ')' if has_pe else ''}")
                elif label == "fetch_macro_snapshot" and data:
                    dq = data.get("summary", {}).get("data_quality", "error")
                    results["macro_snapshot"] = {"data_quality": dq}
                    print(f"  Macro snapshot: {dq}")
                elif label == "fetch_futures_data" and data:
                    futures_meta = data.get("meta", {})
                    signals = data.get("signals", {})
                    results["futures_data"] = {
                        "futures_code": futures_meta.get("futures_code"),
                        "data_source": futures_meta.get("data_source"),
                        "composite_score": signals.get("composite_score") if signals else None,
                        "composite_signal": signals.get("composite_signal") if signals else None,
                    }
                    print(f"  Futures: code={futures_meta.get('futures_code')}, "
                          f"signal={signals.get('composite_signal') if signals else 'N/A'}, "
                          f"score={signals.get('composite_score') if signals else 'N/A'}")
                elif label == "fetch_index_valuation" and data:
                    dq = data.get("meta", {}).get("data_quality", "error")
                    pe = data.get("summary", {}).get("pe_ttm")
                    pct = data.get("summary", {}).get("pe_percentile_3y")
                    pct_suffix = f" (3-yr: {pct}%)" if pct is not None else ""
                    results["index_valuation"] = {
                        "data_quality": dq,
                        "pe_ttm": pe,
                        "pe_percentile_3y": pct,
                        "index_name": data.get("meta", {}).get("index_name"),
                    }
                    if dq == "good" and pe:
                        print(f"  Index valuation: {data['meta'].get('index_name')} PE={pe}{pct_suffix}")
                    elif dq == "partial" and pe:
                        pct_20d = data.get("summary", {}).get("pe_percentile_20d")
                        print(f"  Index valuation: {data['meta'].get('index_name')} PE={pe} (20d: {pct_20d}%)")
                    else:
                        print(f"  Index valuation: {data['meta'].get('index_name')} ({dq})")

    # --- Step 5: Write pipeline summary ---
    print(f"[5/5] Writing pipeline output...")
    elapsed = time.time() - pipeline_start

    pipeline_output = {
        "meta": {
            "ts_code": ts_code,
            "asset": asset,
            "adj": adj,
            "freq": args.freq,
            "code": code,
            "is_etf": is_etf,
            "is_hk": is_hk,
            "pipeline_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "elapsed_seconds": round(elapsed, 1),
        },
        "results": results,
        "errors": errors,
        "timeouts": timeouts,
        "output_files": build_output_files(
            output_dir=output_dir,
            kline_path=kline_path,
            kline_available=kline_available,
            technical_available=technical_available,
            chip_available=chip_available,
            is_etf=is_etf,
            no_etf=args.no_etf,
            no_capital=args.no_capital,
            no_fundamental=args.no_fundamental,
            no_macro=args.no_macro,
            no_futures=args.no_futures,
            no_index_valuation=args.no_index_valuation,
            asset=asset,
        ),
    }

    pipeline_path = str(output_dir / "pipeline_output.json")
    with open(pipeline_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_output, f, ensure_ascii=False, indent=2)

    print(f"\nPipeline complete in {elapsed:.1f}s")
    print(f"Output: {pipeline_path}")
    if errors:
        print(f"Errors: {len(errors)}")
        for err in errors:
            print(f"  - {err}")
    else:
        print("No errors")
    if timeouts:
        print(f"Timeouts: {len(timeouts)} ({', '.join(timeouts)})")


if __name__ == "__main__":
    main()