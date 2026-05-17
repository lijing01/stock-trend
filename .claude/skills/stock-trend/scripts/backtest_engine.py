#!/usr/bin/env python3
"""Backtest engine — validate ETF Phase 1 scoring model against historical data.

Simulates what etf-scan Phase 1 would have scored on past dates, then measures
actual subsequent returns. Reports IC (Information Coefficient), hit rate, and
return distribution per scoring dimension.

Usage:
    python3 backtest_engine.py [--lookback-days 120] [--eval-windows 5,10,20]
        [--top-n 10] [--sample-interval 5] [--focus <板块>]
        [--etf <code>] [--output <path>] [--output-html]

Outputs JSON to stdout.

Data limits:
- Only momentum + volume dimensions have full historical data.
- capital_flow, shares_trend, iopv return None for historical dates.
- compute_quick_score redistributes weight from None dimensions automatically.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
DEFAULT_WATCHLIST = SCRIPT_DIR / "watchlist.yaml"
ASSETS_DIR = SKILL_DIR / "assets"

# Import scoring functions from etf_scanner
sys.path.insert(0, str(SCRIPT_DIR))
from etf_scanner import (
    score_momentum, score_volume, score_capital_flow,
    score_shares_trend, score_iopv, compute_quick_score,
    normalize_scores_by_cohort, _piecewise_linear,
    detect_contradictions, detect_trend_stage,
)
from cache_utils import load_cache, save_cache, get_market_day_ttl


# ── Helpers ───────────────────────────────────────────────────────


def spearman_rank_correlation(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation coefficient. Manual impl, no scipy."""
    n = len(x)
    if n < 3:
        return 0.0

    def rank(vals):
        sorted_pairs = sorted([(v, i) for i, v in enumerate(vals)])
        ranks = [0] * n
        for pos, (_, idx) in enumerate(sorted_pairs):
            ranks[idx] = pos + 1
        # Tie handling: avg ranks for ties
        i = 0
        while i < n:
            j = i
            while j < n and sorted_pairs[j][0] == sorted_pairs[i][0]:
                j += 1
            if j > i + 1:
                avg = (i + 1 + j) / 2.0
                for k in range(i, j):
                    ranks[sorted_pairs[k][1]] = avg
            i = j
        return ranks

    rx = rank(x)
    ry = rank(y)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1.0 - (6.0 * d2) / (n * (n * n - 1))


def fetch_kline_for_etf(ts_code: str) -> Optional[list]:
    """Fetch K-line for an ETF, return records list or None."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            out_path = f.name
        cmd = [sys.executable, str(SCRIPT_DIR / "fetch_kline_eastmoney.py"), ts_code, "-o", out_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        with open(out_path, "r") as f:
            raw = json.load(f)
        os.unlink(out_path)
        # fetch_kline_eastmoney.py -o wraps in {"meta":..., "data":[...]}
        records = raw if isinstance(raw, list) else raw.get("data", [])
        if records and isinstance(records, list):
            return records
        return None
    except Exception:
        return None


def load_watchlist(focus: Optional[str] = None, etf_code: Optional[str] = None) -> list[dict]:
    """Load ETF watchlist, optionally filtered by focus category or single ETF."""
    with open(DEFAULT_WATCHLIST, "r", encoding="utf-8") as f:
        wl = yaml.safe_load(f)

    etfs = []
    for cat in wl.get("categories", []):
        name = cat.get("name", "")
        if focus and focus not in name:
            continue
        for etf in cat.get("etfs", []):
            code = str(etf.get("code", ""))
            if etf_code and code != etf_code:
                continue
            # Determine ts_code
            if code.startswith("159"):
                ts_code = f"{code}.SZ"
            elif code.startswith(("5", "15")):
                ts_code = f"{code}.SH"
            else:
                ts_code = f"{code}.SH"
            etfs.append({"code": code, "ts_code": ts_code, "category": name})
    return etfs


def slice_kline_to_date(kline: list, target_date: str) -> list:
    """Slice K-line up to and including target_date (format: YYYYMMDD or YYYY-MM-DD)."""
    target = target_date.replace("-", "")
    result = []
    for r in kline:
        td = str(r.get("trade_date", "")).replace("-", "")
        if td <= target:
            result.append(r)
        else:
            break
    return result


def find_kline_date_index(kline: list, target_date: str) -> int:
    """Find index of first record on or after target_date."""
    target = target_date.replace("-", "")
    for i, r in enumerate(kline):
        td = str(r.get("trade_date", "")).replace("-", "")
        if td >= target:
            return i
    return len(kline)


# ── Core backtest logic ───────────────────────────────────────────


def run_backtest(
    etfs: list[dict],
    lookback_days: int = 120,
    eval_windows: list[int] = None,
    top_n: int = 10,
    sample_interval: int = 5,
) -> dict:
    """Run full backtest. Returns result dict."""
    if eval_windows is None:
        eval_windows = [5, 10, 20]

    # Step 1: Fetch K-line for all ETFs (parallel)
    print(f"Fetching K-line for {len(etfs)} ETFs...", file=sys.stderr)
    etf_kline_map: dict[str, Optional[list]] = {}

    def _fetch_one(etf):
        k = fetch_kline_for_etf(etf["ts_code"])
        return etf["code"], k

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_one, e): e for e in etfs}
        for f in as_completed(futures):
            code, kline = f.result()
            etf_kline_map[code] = kline

    valid_etfs = [e for e in etfs if etf_kline_map.get(e["code"]) and len(etf_kline_map[e["code"]]) >= 30]
    print(f"Valid ETFs with data: {len(valid_etfs)}/{len(etfs)}", file=sys.stderr)

    if not valid_etfs:
        return {"meta": {"error": "no valid ETF data"}, "summary": {}}

    # Determine sample dates from the longest K-line
    all_klines = [etf_kline_map[e["code"]] for e in valid_etfs]
    max_kline = max(all_klines, key=len)
    all_dates = [r["trade_date"].replace("-", "") for r in max_kline]

    # Need at least lookback_days + max(eval_windows) of history
    min_needed = lookback_days + max(eval_windows)
    if len(all_dates) < min_needed:
        # Use what we have
        start_idx = 0
    else:
        start_idx = len(all_dates) - min_needed

    sample_indices = list(range(start_idx, len(all_dates) - max(eval_windows), sample_interval))
    sample_dates = [all_dates[i] for i in sample_indices]
    print(f"Sample dates: {len(sample_dates)} (from {sample_dates[0]} to {sample_dates[-1]})", file=sys.stderr)

    # Step 2: Per-date scoring + forward returns
    per_date_results = []
    dimension_ics = {dim: [] for dim in ["momentum", "volume", "capital_flow", "shares_trend", "iopv", "quick_score"]}

    for sidx in sample_indices:
        date = all_dates[sidx]
        scored = []

        for etf in valid_etfs:
            kline = etf_kline_map[etf["code"]]
            sliced = slice_kline_to_date(kline, date)

            if len(sliced) < 20:
                continue

            # Simulate Phase 1 scoring (capital_flow, shares_trend, iopv not available historically)
            dims = {
                "momentum": score_momentum(sliced),
                "volume": score_volume(sliced),
                "capital_flow": None,
                "shares_trend": None,
                "iopv": None,
            }

            # compute_quick_score-like calculation
            weights = {"momentum": 30, "volume": 20, "capital_flow": 20, "shares_trend": 15, "iopv": 15}
            total_w = 0
            weighted = 0.0
            for dim_key, w in weights.items():
                val = dims[dim_key]
                if val is not None:
                    weighted += val * w
                    total_w += w
            quick_score = round(weighted / total_w, 1) if total_w > 0 else None

            # Forward returns
            end_idx = sidx + max(eval_windows)
            future_slice = all_dates[sidx + 1:end_idx + 1]
            fwd_returns = {}
            for w in eval_windows:
                if sidx + w < len(all_dates):
                    # Find actual close at offset in this ETF's K-line
                    target_date = all_dates[sidx + w]
                    idx = find_kline_date_index(kline, target_date)
                    if idx > 0 and idx < len(kline):
                        close_now = sliced[-1].get("close", 0)
                        close_future = kline[idx - 1].get("close", 0) if idx > 0 else kline[-1].get("close", 0)
                        if close_now > 0 and close_future > 0:
                            fwd_returns[str(w)] = round((close_future - close_now) / close_now, 6)

            scored.append({
                "code": etf["code"],
                "ts_code": etf["ts_code"],
                "category": etf["category"],
                "quick_score": quick_score,
                "dimensions": dims,
                "returns": fwd_returns,
            })

        if len(scored) < 3:
            continue

        # Sort by quick_score descending
        scored.sort(key=lambda x: x["quick_score"] or 0, reverse=True)

        # Compute IC for this date
        scores_dim = [s["quick_score"] or 0 for s in scored]
        for w in eval_windows:
            rets_w = [s["returns"].get(str(w), None) for s in scored]
            valid_pairs = [(s, r) for s, r in zip(scores_dim, rets_w) if r is not None]
            if len(valid_pairs) >= 5:
                ic = spearman_rank_correlation([p[0] for p in valid_pairs], [p[1] for p in valid_pairs])
                dimension_ics["quick_score"].append({"date": date, "window": w, "ic": round(ic, 4)})
                # If we had capital_flow etc data, we'd compute per-dimension IC here

        per_date_results.append({
            "date": date,
            "total_scored": len(scored),
            "top_n": [{"code": s["code"], "quick_score": s["quick_score"],
                        "returns": s["returns"]} for s in scored[:top_n]],
            "bottom_n": [{"code": s["code"], "quick_score": s["quick_score"],
                           "returns": s["returns"]} for s in scored[-top_n:]],
        })

    # Step 3: Aggregate results
    summary = _compute_summary(dimension_ics, per_date_results, eval_windows, top_n)

    return {
        "meta": {
            "command": "backtest",
            "timestamp": datetime.now().isoformat(),
            "lookback_days": lookback_days,
            "eval_windows": eval_windows,
            "sample_interval": sample_interval,
            "total_dates": len(sample_dates),
            "total_etfs_tested": len(valid_etfs),
            "data_notes": {
                "capital_flow_available": False,
                "etf_data_available": False,
                "degraded_dims": ["capital_flow", "shares_trend", "iopv"],
                "note": "Only momentum + volume dimensions have historical data. Other dimensions return None and weights are redistributed."
            },
        },
        "summary": summary,
        "per_date": per_date_results,
        "per_etf": _compute_per_etf_stats(valid_etfs, etf_kline_map, sample_indices, all_dates, eval_windows),
    }


def _compute_summary(
    dimension_ics: dict,
    per_date_results: list,
    eval_windows: list[int],
    top_n: int,
) -> dict:
    """Aggregate per-date results into summary statistics."""
    # IC summary (for quick_score only since other dims have no historical data)
    ic_summary = {}
    for w in eval_windows:
        ics = [item["ic"] for item in dimension_ics["quick_score"] if item["window"] == w]
        if len(ics) >= 3:
            mean_ic = sum(ics) / len(ics)
            std_ic = math.sqrt(sum((x - mean_ic) ** 2 for x in ics) / len(ics))
            t_stat = mean_ic / (std_ic / math.sqrt(len(ics))) if std_ic > 0 else 0
            ic_summary[f"window_{w}"] = {
                "mean": round(mean_ic, 4),
                "std": round(std_ic, 4),
                "t_stat": round(t_stat, 4),
                "count": len(ics),
                "positive_ratio": round(sum(1 for x in ics if x > 0) / len(ics), 4),
            }

    dim_ic_result = {
        "momentum": None,
        "volume": None,
        "capital_flow": None,
        "shares_trend": None,
        "iopv": None,
        "quick_score": ic_summary,
    }

    # Hit rate: fraction of top-N with positive returns
    hit_rate = {}
    for w in eval_windows:
        total_top = 0
        positive_top = 0
        for pd in per_date_results:
            for item in pd["top_n"]:
                ret = item["returns"].get(str(w))
                if ret is not None:
                    total_top += 1
                    if ret > 0:
                        positive_top += 1
        hit_rate[f"top_{top_n}"] = hit_rate.get(f"top_{top_n}", {})
        hr = round(positive_top / total_top, 4) if total_top > 0 else 0
        hit_rate[f"top_{top_n}"][f"window_{w}"] = {"hit": positive_top, "total": total_top, "ratio": hr}

    # Return distribution
    ret_dist = {}
    for w in eval_windows:
        top_rets = []
        bot_rets = []
        for pd in per_date_results:
            for item in pd["top_n"]:
                r = item["returns"].get(str(w))
                if r is not None:
                    top_rets.append(r)
            for item in pd["bottom_n"]:
                r = item["returns"].get(str(w))
                if r is not None:
                    bot_rets.append(r)

        try:
            def _stat(vals):
                if not vals:
                    return None
                mean = sum(vals) / len(vals)
                sorted_v = sorted(vals)
                median = sorted_v[len(sorted_v) // 2]
                std = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
                return {"mean": round(mean, 6), "median": round(median, 6),
                        "std": round(std, 6), "min": round(min(vals), 6), "max": round(max(vals), 6),
                        "count": len(vals)}
            ret_dist[f"window_{w}"] = {
                "top_n": _stat(top_rets),
                "bottom_n": _stat(bot_rets),
            }
        except Exception:
            pass

    # Top vs bottom spread
    spread = {}
    for w in eval_windows:
        rw = ret_dist.get(f"window_{w}", {})
        if rw:
            top_mean = rw.get("top_n", {}).get("mean") if rw.get("top_n") else None
            bot_mean = rw.get("bottom_n", {}).get("mean") if rw.get("bottom_n") else None
            if top_mean is not None and bot_mean is not None:
                spread[f"window_{w}"] = round(top_mean - bot_mean, 6)
            else:
                spread[f"window_{w}"] = None
        else:
            spread[f"window_{w}"] = None

    return {
        "ic_by_dimension": dim_ic_result,
        "hit_rate": hit_rate,
        "return_distribution": ret_dist,
        "top_vs_bottom_spread": spread,
    }


def _compute_per_etf_stats(
    valid_etfs: list,
    kline_map: dict,
    sample_indices: list,
    all_dates: list,
    eval_windows: list[int],
) -> list[dict]:
    """Per-ETF summary statistics."""
    per_etf = []
    for etf in valid_etfs:
        code = etf["code"]
        kline = kline_map[code]
        if not kline:
            continue

        # Count how many sample dates this ETF was score-able
        scored_count = 0
        avg_rets = {}
        for w in eval_windows:
            avg_rets[str(w)] = []

        for sidx in sample_indices:
            date = all_dates[sidx]
            sliced = slice_kline_to_date(kline, date)
            if len(sliced) >= 20:
                scored_count += 1
                for w in eval_windows:
                    if sidx + w < len(all_dates):
                        target_date = all_dates[sidx + w]
                        idx = find_kline_date_index(kline, target_date)
                        if idx > 0 and idx < len(kline):
                            close_now = sliced[-1].get("close", 0)
                            close_future = kline[idx - 1].get("close", 0) if idx > 0 else 0
                            if close_now > 0 and close_future > 0:
                                avg_rets[str(w)].append((close_future - close_now) / close_now)

        if scored_count == 0:
            continue

        avg_returns = {}
        for w in eval_windows:
            vals = avg_rets[str(w)]
            if vals:
                avg_returns[str(w)] = round(sum(vals) / len(vals), 6)
            else:
                avg_returns[str(w)] = None

        per_etf.append({
            "code": code,
            "name": etf.get("ts_code", ""),
            "dates_scored": scored_count,
            "avg_returns": avg_returns,
        })
    return per_etf


# ── CLI ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="ETF 速评分回测验证")
    parser.add_argument("--lookback-days", type=int, default=120, help="回测天数（默认120）")
    parser.add_argument("--eval-windows", default="5,10,20", help="评估窗口（天），逗号分隔")
    parser.add_argument("--top-n", type=int, default=10, help="每日取 top N ETF")
    parser.add_argument("--sample-interval", type=int, default=5, help="采样间隔（天）")
    parser.add_argument("--focus", help="只回测指定板块")
    parser.add_argument("--etf", help="只回测单只 ETF")
    parser.add_argument("--output", help="输出文件路径")
    args = parser.parse_args()

    eval_windows = [int(w) for w in args.eval_windows.split(",")]

    etfs = load_watchlist(focus=args.focus, etf_code=args.etf)
    if not etfs:
        print(json.dumps({"error": "No ETFs matched"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    result = run_backtest(
        etfs=etfs,
        lookback_days=args.lookback_days,
        eval_windows=eval_windows,
        top_n=args.top_n,
        sample_interval=args.sample_interval,
    )

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Output written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
