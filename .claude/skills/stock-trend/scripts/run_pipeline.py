#!/usr/bin/env python3
"""Pipeline orchestration script for stock-trend skill.

Runs all data fetching and analysis steps in sequence, with automatic
Tushare fallback and parallel execution where possible.

Usage:
    python3 run_pipeline.py <ts_code> [options]

Examples:
    python3 run_pipeline.py 159740.SZ --asset FD --adj qfq -o /tmp
    python3 run_pipeline.py 600519.SH -o /tmp
    python3 run_pipeline.py 00700.HK -o /tmp
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def run_script(cmd, label=""):
    """Run a Python script and return (success, output_path, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
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
            "stdout": "",
            "stderr": "Timeout (120s)",
        }
    except Exception as e:
        return {
            "success": False,
            "label": label,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        }


def read_json(path):
    """Read JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Run full stock-trend data pipeline"
    )
    parser.add_argument("ts_code", help="Tushare-format code (e.g. 600519.SH, 159740.SZ)")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], help="Adjustment type (auto-detected if omitted)")
    parser.add_argument("--freq", choices=["D", "W"], default="D", help="K-line frequency (default: D)")
    parser.add_argument("--no-etf", action="store_true", help="Skip ETF data fetch")
    parser.add_argument("--no-capital", action="store_true", help="Skip capital flow fetch")
    parser.add_argument("--no-fundamental", action="store_true", help="Skip fundamental data fetch")
    parser.add_argument("--no-macro", action="store_true", help="Skip macro snapshot fetch")
    parser.add_argument("-o", "--output-dir", default="/tmp", help="Output directory (default: /tmp)")
    args = parser.parse_args()

    ts_code = args.ts_code
    code = ts_code.split(".")[0]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect asset and adj if not specified
    asset = args.asset
    if not asset:
        if code.startswith(("5", "15")):
            asset = "FD"
        else:
            asset = "E"

    adj = args.adj
    if not adj:
        if ts_code.endswith(".HK"):
            adj = "none"
        else:
            adj = "qfq"

    is_etf = asset == "FD"
    is_hk = ts_code.endswith(".HK")

    pipeline_start = time.time()
    errors = []
    results = {}

    # --- Step 1: Quick diagnostic ---
    print(f"[1/5] Running diagnostic...")
    diag_result = run_script(
        [sys.executable, str(SCRIPT_DIR / "diagnose.py"), "--quick"],
        label="diagnostic",
    )
    tushare_available = True  # Assume available, will check via kline fetch

    # --- Step 2: Fetch K-line data ---
    print(f"[2/5] Fetching K-line data for {ts_code}...")
    kline_path = str(output_dir / "kline.json")

    # Try Tushare first
    kline_result = run_script(
        [
            sys.executable, str(SCRIPT_DIR / "fetch_kline.py"),
            ts_code, "--asset", asset, "--freq", args.freq,
            "--adj", adj, "-o", kline_path,
        ],
        label="fetch_kline_tushare",
    )

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
        fallback_result = run_script(
            [
                sys.executable, str(SCRIPT_DIR / "fetch_kline_eastmoney.py"),
                ts_code, "--asset", asset, "--freq", args.freq,
                "-o", kline_path,
            ],
            label="fetch_kline_eastmoney",
        )
        if not fallback_result["success"]:
            errors.append(f"K-line fetch failed: {fallback_result['stderr']}")
        kline_data = read_json(kline_path)

    if kline_data:
        data_source = kline_data.get("meta", {}).get("data_source", "unknown")
        record_count = kline_data.get("meta", {}).get("record_count", 0)
        print(f"  K-line data: {data_source}, {record_count} records")
        results["kline"] = {
            "data_source": data_source,
            "record_count": record_count,
        }
    else:
        errors.append("K-line data unavailable")
        results["kline"] = {"data_source": "error", "record_count": 0}

    # --- Step 3: Technical analysis (depends on kline) ---
    technical_path = str(output_dir / "technical.json")
    if kline_data and kline_data.get("meta", {}).get("data_source") != "error":
        print(f"[3/5] Running technical analysis...")
        tech_result = run_script(
            [
                sys.executable, str(SCRIPT_DIR / "analyze_technical.py"),
                kline_path, "-o", technical_path,
            ],
            label="analyze_technical",
        )
        if not tech_result["success"]:
            errors.append(f"Technical analysis failed: {tech_result['stderr']}")

        tech_data = read_json(technical_path)
        if tech_data:
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
        print(f"[3/5] Skipping technical analysis (no K-line data)")

    # --- Step 4: ETF data and capital flow (parallel, independent) ---
    print(f"[4/5] Fetching supplementary data...")

    parallel_tasks = []

    # ETF data
    if is_etf and not args.no_etf:
        etf_path = str(output_dir / "etf_data.json")
        parallel_tasks.append((
            [
                sys.executable, str(SCRIPT_DIR / "fetch_etf_data.py"),
                code, "-o", etf_path,
            ],
            "fetch_etf_data",
            etf_path,
        ))

    # Capital flow
    if not args.no_capital:
        capital_path = str(output_dir / "capital_flow.json")
        parallel_tasks.append((
            [
                sys.executable, str(SCRIPT_DIR / "fetch_capital_flow.py"),
                ts_code, "--asset", asset, "-o", capital_path,
            ],
            "fetch_capital_flow",
            capital_path,
        ))

    # Fundamental data (skip for ETFs)
    if not args.no_fundamental and asset != "FD":
        fundamental_path = str(output_dir / "fundamental.json")
        parallel_tasks.append((
            [
                sys.executable, str(SCRIPT_DIR / "fetch_fundamental.py"),
                ts_code, "--asset", asset, "-o", fundamental_path,
            ],
            "fetch_fundamental",
            fundamental_path,
        ))

    # Macro snapshot (market-level, always)
    if not args.no_macro:
        macro_path = str(output_dir / "macro_snapshot.json")
        parallel_tasks.append((
            [
                sys.executable, str(SCRIPT_DIR / "fetch_macro_snapshot.py"),
                "-o", macro_path,
            ],
            "fetch_macro_snapshot",
            macro_path,
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
        "output_files": {
            "kline": kline_path,
            "technical": technical_path if kline_data else None,
            "etf_data": str(output_dir / "etf_data.json") if is_etf and not args.no_etf else None,
            "capital_flow": str(output_dir / "capital_flow.json") if not args.no_capital else None,
            "fundamental": str(output_dir / "fundamental.json") if not args.no_fundamental and asset != "FD" else None,
            "macro_snapshot": str(output_dir / "macro_snapshot.json") if not args.no_macro else None,
        },
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


if __name__ == "__main__":
    main()