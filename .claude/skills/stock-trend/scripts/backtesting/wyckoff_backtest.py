#!/usr/bin/env python3
"""维科夫买点回测 — 验证 /candidates 买点信号的历史胜率.

历史重放: 在每个采样日, 用截至该日的 K 线切片跑维科夫分析
(analyze_kline_dict), 检测买点 (吸筹/拉升阶段 + Spring/LPS/ST/PRE_MARKUP/JAC/BU
子阶段 + 置信度≥阈值), 随后测量 5/10/20 日前向收益。对照全样本基线, 回答:

  1. 买点信号是否真的带来正收益? (胜率 / 均收益 vs 基线)
  2. 哪个子阶段 / 置信度档位 / 阶段 / 100分档位胜率最高? (用于调漏斗参数)

与 lhb_tracker 每日快照不同, 买点可由历史 K 线重现, 无需快照积累 ——
直接重放历史即得胜率, 当天出结论。

已知局限 (meta.notes 标注):
  - close-to-close 前向收益, 不含手续费/印花税/滑点
  - 未模拟止损触发 (跳空打穿)
  - 宇宙 = 当前成分股, 历史退市股不在列 (幸存者偏差)
  - 同一买点连续多日出现 → 按 min-gap 去重计为一次信号

Usage:
    python3 backtesting/wyckoff_backtest.py --codes 600519,000001
    python3 backtesting/wyckoff_backtest.py --sectors BK0477,BK0897
    python3 backtesting/wyckoff_backtest.py --from-candidates <candidates.json>
    python3 backtesting/wyckoff_backtest.py --codes 600519 --output-html
"""

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

from scans.stock_scanner import _resolve_ts_code, _fetch_kline, gather_candidates, _is_a_share
from analysis.wyckoff import (
    analyze_kline_dict,
    classify_buy_point_level,
    is_buy_signal,
    normalize_score_100,
)

# 与 stock_scanner wyckoff 漏斗保持一致
MIN_KLINES = 60                      # 不足 60 根 K 线丢弃
DEFAULT_MIN_CONFIDENCE = 0.3         # WYCKOFF_MIN_CONFIDENCE

WINDOW_KEY = lambda w: str(w)  # "5" / "10" / "20"


def _safe_close(row):
    """Parse close price safely."""
    if not isinstance(row, dict):
        return None
    try:
        return float(row.get("close"))
    except (TypeError, ValueError):
        return None


def _kline_date(row):
    """Extract normalized YYYYMMDD date from a kline row."""
    return str(row.get("date") or row.get("trade_date") or "").replace("-", "")


def slice_kline(kline, target_date):
    """Rows up to and including target_date (ascending kline)."""
    target = str(target_date).replace("-", "")
    return [r for r in kline if _kline_date(r) <= target]


def _forward_return(kline, now_idx, target_date):
    """Return close-to-close performance at first bar on/after target_date."""
    c_now = _safe_close(kline[now_idx])
    if not c_now or c_now <= 0:
        return None
    target = str(target_date).replace("-", "")
    for i in range(now_idx + 1, len(kline)):
        if _kline_date(kline[i]) >= target:
            c_fut = _safe_close(kline[i])
            if c_fut and c_fut > 0:
                return round((c_fut - c_now) / c_now, 6)
            return None
    return None


def _forward_excursion(kline, now_idx, target_date):
    """Measure max adverse/favorable excursion until the target date."""
    entry = _safe_close(kline[now_idx])
    if not entry or entry <= 0:
        return None
    target = str(target_date).replace("-", "")
    path = []
    for row in kline[now_idx + 1:]:
        path.append(row)
        if _kline_date(row) >= target:
            break
    if not path or _kline_date(path[-1]) < target:
        return None

    lows = []
    highs = []
    for row in path:
        try:
            low = float(row.get("low"))
        except (TypeError, ValueError):
            low = _safe_close(row)
        try:
            high = float(row.get("high"))
        except (TypeError, ValueError):
            high = _safe_close(row)
        if low is not None:
            lows.append(low)
        if high is not None:
            highs.append(high)
    if not lows or not highs:
        return None
    return {
        "mae": round(min(lows) / entry - 1, 6),
        "mfe": round(max(highs) / entry - 1, 6),
    }


def _classify_signal(analysis, min_confidence):
    """Extract (phase, sub, confidence, score_100) from wyckoff analysis.

    Returns dict or None if the analysis is malformed/errored.
    """
    if not analysis or "error" in analysis.get("meta", {}):
        return None
    phase_info = analysis.get("phase", {})
    phase = phase_info.get("primary", "")
    sub = phase_info.get("primary_sub_phase", "")
    conf = phase_info.get("confidence", 0.0) or 0.0
    if not phase or not sub:
        return None
    level = classify_buy_point_level(analysis)
    return {
        "phase": phase,
        "sub_phase": sub,
        "confidence": round(float(conf), 3),
        "score_100": round(normalize_score_100(float(analysis.get("wyckoff_score", 0))), 1),
        "buy_point_level": level["number"] if level else None,
    }


def _select_signals(obs_list, min_gap):
    """Dedup consecutive buy-point observations per stock.

    obs_list: sorted-by-sidx [(sidx, ...), ...]. After a counted signal at sidx,
    suppress until a later observation at least min_gap bars away. Since
    observations only exist while the stock stays a buy point, this collapses a
    persistent buy phase into one episode signal.
    """
    selected = []
    last = -10 ** 9
    for o in obs_list:
        if o[0] - last >= min_gap:
            selected.append(o)
            last = o[0]
    return selected


# ── 统计 ────────────────────────────────────────────────────────────────


def _stats(returns):
    """Basic return statistics. Returns None if empty."""
    if not returns:
        return None
    n = len(returns)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    mean = sum(returns) / n
    sorted_r = sorted(returns)
    std = math.sqrt(sum((x - mean) ** 2 for x in returns) / n)
    return {
        "count": n,
        "win_rate": round(len(wins) / n, 4),
        "avg": round(mean, 6),
        "median": round(sorted_r[n // 2], 6),
        "std": round(std, 6),
        "max": round(max(returns), 6),
        "min": round(min(returns), 6),
        "avg_win": round(sum(wins) / len(wins), 6) if wins else None,
        "avg_loss": round(abs(sum(losses) / len(losses)), 6) if losses else None,
    }


def _spearman(x, y):
    """Spearman rank correlation (same manual impl as backtest engine)."""
    n = len(x)
    if n < 3:
        return None

    def rank(vals):
        pairs = sorted([(v, i) for i, v in enumerate(vals)])
        ranks = [0] * n
        for pos, (_, idx) in enumerate(pairs):
            ranks[idx] = pos + 1
        i = 0
        while i < n:
            j = i
            while j < n and pairs[j][0] == pairs[i][0]:
                j += 1
            if j > i + 1:
                avg = (i + 1 + j) / 2.0
                for k in range(i, j):
                    ranks[pairs[k][1]] = avg
            i = j
        return ranks

    rx = rank(x)
    ry = rank(y)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return round(1.0 - 6.0 * d2 / (n * (n * n - 1)), 4)


def _conf_band(sig):
    conf = sig["confidence"]
    if conf >= 0.7:
        return "置信≥0.7"
    if conf >= 0.5:
        return "0.5≤置信<0.7"
    return "0.3≤置信<0.5"


def _score_band(sig):
    score_100 = sig["score_100"]
    if score_100 >= 70:
        return "100分≥70(强势)"
    if score_100 >= 55:
        return "55≤100分<70(候选)"
    return "100分<55(弱)"


# ── 聚合 ────────────────────────────────────────────────────────────────


def _bucket_stats(signal_rets_by_window, key_fn):
    """Group signal returns by bucket key per window.

    signal_rets_by_window: {window: [(ret, raw_signal_dict), ...]}
    key_fn: callable(raw_signal_dict) -> bucket label
    Returns {window: {bucket: stats}}
    """
    out = {}
    for w, pairs in signal_rets_by_window.items():
        buckets = defaultdict(list)
        for ret, sig in pairs:
            buckets[key_fn(sig)].append(ret)
        out[w] = {k: _stats(v) for k, v in sorted(buckets.items())}
    return out


def _level_key(signal):
    level = signal.get("buy_point_level")
    return f"level_{level}" if level in (1, 2, 3) else "ungraded"


def _risk_bucket_stats(signal_pairs_by_window, key_fn):
    """Aggregate max adverse/favorable excursions by signal bucket."""
    out = {}
    for window, pairs in signal_pairs_by_window.items():
        buckets = defaultdict(list)
        for _, signal in pairs:
            excursion = (signal.get("excursions") or {}).get(str(window))
            if not isinstance(excursion, dict):
                continue
            try:
                mae = float(excursion["mae"])
                mfe = float(excursion["mfe"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(mae) and math.isfinite(mfe):
                buckets[key_fn(signal)].append((mae, mfe))
        out[window] = {
            key: {
                "count": len(paths),
                "avg_mae": round(sum(path[0] for path in paths) / len(paths), 6),
                "avg_mfe": round(sum(path[1] for path in paths) / len(paths), 6),
            }
            for key, paths in sorted(buckets.items())
            if paths
        }
    return out


# ── 核心回测 ────────────────────────────────────────────────────────────


def run_backtest(stocks, kline_map, lookback_days=120, eval_windows=(5, 10, 20),
                 sample_interval=5, min_confidence=0.3, min_gap=10) -> dict:
    """Replay wyckoff buy points across history and measure forward returns.

    Args:
        stocks: list of {code, ts_code, name, ...}
        kline_map: {ts_code: {"data": [rows]}}
        eval_windows: forward return windows in trading days

    Returns result dict (same shape as CLI stdout).
    """
    valid = []
    for s in stocks:
        rows = (kline_map.get(s["ts_code"]) or {}).get("data") or []
        if len(rows) >= MIN_KLINES:
            valid.append(s)
    if len(valid) < 2:
        return {
            "meta": {"error": f"valid stocks < 2 (need {MIN_KLINES}+ bars each)"},
            "summary": {},
        }

    rows_by_code = {s["ts_code"]: kline_map[s["ts_code"]]["data"] for s in valid}
    all_dates = sorted({_kline_date(r) for rows in rows_by_code.values() for r in rows})

    min_needed = lookback_days + max(eval_windows)
    start_idx = 0 if len(all_dates) < min_needed else len(all_dates) - min_needed
    sample_indices = list(range(start_idx, len(all_dates) - max(eval_windows), sample_interval))
    sample_indices = [i for i in sample_indices if 0 <= i < len(all_dates) - max(eval_windows)]

    baseline = defaultdict(list)          # {w: [ret, ...]} all stock-days
    per_stock_raw = defaultdict(list)     # {ts_code: [(sidx, date, sig_dict, fwd), ...]}

    for sidx in sample_indices:
        date = all_dates[sidx]
        for s in valid:
            rows = rows_by_code[s["ts_code"]]
            sliced = slice_kline(rows, date)
            if len(sliced) < MIN_KLINES:
                continue

            now_idx = len(sliced) - 1  # same row index in full `rows`
            fwd = {}
            for w in eval_windows:
                if sidx + w >= len(all_dates):
                    continue
                r_ = _forward_return(rows, now_idx, all_dates[sidx + w])
                if r_ is not None:
                    fwd[str(w)] = r_
                    baseline[str(w)].append(r_)
            if not fwd:
                continue

            analysis = analyze_kline_dict({"meta": {}, "data": sliced})
            sig = _classify_signal(analysis, min_confidence)
            if sig is None:
                continue
            if is_buy_signal(analysis) and sig["confidence"] >= min_confidence:
                excursions = {}
                for w in eval_windows:
                    wk = str(w)
                    if wk not in fwd or sidx + w >= len(all_dates):
                        continue
                    path = _forward_excursion(rows, now_idx, all_dates[sidx + w])
                    if path is not None:
                        excursions[wk] = path
                per_stock_raw[s["ts_code"]].append(
                    (sidx, date, sig, fwd, excursions))

    # Episode dedup per stock, flatten
    signals = []
    for code, obs in per_stock_raw.items():
        obs.sort(key=lambda x: x[0])
        for sidx, date, sig, fwd, excursions in _select_signals(obs, min_gap):
            signals.append({
                "code": code.split(".")[0],
                "ts_code": code,
                "name": next((s.get("name", "") for s in valid if s["ts_code"] == code), ""),
                "date": date,
                "phase": sig["phase"],
                "sub_phase": sig["sub_phase"],
                "confidence": sig["confidence"],
                "score_100": sig["score_100"],
                "buy_point_level": sig["buy_point_level"],
                "returns": fwd,
                "excursions": excursions,
            })

    return _build_result(valid, signals, baseline, eval_windows,
                         {"lookback_days": lookback_days, "sample_interval": sample_interval,
                          "min_confidence": min_confidence, "min_gap": min_gap,
                          "sample_dates": len(sample_indices)})


def _build_result(valid, signals, baseline, eval_windows, params) -> dict:
    """Aggregate signals + baseline into the final result dict."""
    signal_rets = {str(w): [s["returns"][str(w)] for s in signals if str(w) in s["returns"]]
                   for w in eval_windows}
    signal_pairs = {str(w): [(s["returns"][str(w)], s) for s in signals if str(w) in s["returns"]]
                    for w in eval_windows}

    summary = {}
    for w in eval_windows:
        wk = str(w)
        sig_stats = _stats(signal_rets[wk])
        base_stats = _stats(baseline[wk])
        summary[wk] = {
            "signals": sig_stats,
            "baseline": base_stats,
            "alpha": {
                "win_rate": round((sig_stats["win_rate"] - base_stats["win_rate"]), 4)
                             if sig_stats and base_stats else None,
                "avg": round((sig_stats["avg"] - base_stats["avg"]), 6)
                       if sig_stats and base_stats else None,
            },
        }

    by_sub_phase = _bucket_stats(signal_pairs, lambda s: s["sub_phase"])
    by_buy_level = _bucket_stats(signal_pairs, _level_key)
    risk_by_buy_level = _risk_bucket_stats(signal_pairs, _level_key)
    by_conf = _bucket_stats(signal_pairs, _conf_band)
    by_phase = _bucket_stats(signal_pairs, lambda s: "吸筹" if s["phase"] == "accumulation"
                             else ("拉升" if s["phase"] == "markup" else s["phase"]))
    by_score = _bucket_stats(signal_pairs, _score_band)

    level_counts = {
        level: sum(1 for signal in signals if _level_key(signal) == level)
        for level in ("level_1", "level_2", "level_3")
    }
    evidence = {
        "minimum_signals_per_level": 100,
        "counts": level_counts,
        "status": (
            "ready" if all(count >= 100 for count in level_counts.values())
            else "evidence_insufficient"
        ),
    }

    # IC: does confidence / 100分 rank predict forward return?
    ic = {}
    for w in eval_windows:
        wk = str(w)
        pts = [(s["confidence"], s["returns"][wk]) for s in signals if wk in s["returns"]]
        ic[wk] = {
            "confidence": _spearman([p[0] for p in pts], [p[1] for p in pts]) if len(pts) >= 5 else None,
        }
        pts100 = [(s["score_100"], s["returns"][wk]) for s in signals if wk in s["returns"]]
        ic[wk]["score_100"] = _spearman([p[0] for p in pts100], [p[1] for p in pts100]) if len(pts100) >= 5 else None

    # Strategy stats for primary window (feeds Kelly)
    primary = str(eval_windows[0])
    strategy_stats = {"sample_count": len(signal_rets[primary])}
    if len(signal_rets[primary]) >= 30:
        wins = [r for r in signal_rets[primary] if r > 0]
        losses = [r for r in signal_rets[primary] if r < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        strategy_stats = {
            "win_rate": round(len(wins) / len(signal_rets[primary]), 4),
            "avg_win": round(avg_win, 6),
            "avg_loss": round(avg_loss, 6),
            "avg_win_loss_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else 1.5,
            "sample_count": len(signal_rets[primary]),
            "eval_window": primary,
        }

    # Per-stock signal counts + win rate
    per_stock = []
    by_code = defaultdict(list)
    for s in signals:
        if primary in s["returns"]:
            by_code[s["ts_code"]].append(s["returns"][primary])
    for code, rets in sorted(by_code.items()):
        per_stock.append({
            "ts_code": code,
            "signals": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4) if rets else None,
        })

    return {
        "meta": {
            "command": "wyckoff-backtest",
            "timestamp": datetime.now().isoformat(),
            "eval_windows": list(eval_windows),
            **params,
            "stocks_tested": len(valid),
            "signal_count": len(signals),
            "notes": [
                "close-to-close 前向收益, 不含手续费/印花税/滑点",
                "未模拟止损触发(跳空打穿)",
                "宇宙=当前成分股, 历史退市股不在列(幸存者偏差)",
                "同一买点连续多日→按 min_gap 去重计为一次信号",
            ],
        },
        "summary": summary,
        "by_sub_phase": by_sub_phase,
        "by_buy_level": by_buy_level,
        "risk_by_buy_level": risk_by_buy_level,
        "evidence": evidence,
        "by_confidence": by_conf,
        "by_phase": by_phase,
        "by_score_100": by_score,
        "ic": ic,
        "strategy_stats": strategy_stats,
        "signals": signals[:200],
        "per_stock": per_stock,
    }


# ── 宇宙 + 抓取 ─────────────────────────────────────────────────────────


def build_stocks(codes=None, sectors=None, from_candidates=None) -> list[dict]:
    """Resolve the test universe from CLI sources."""
    stocks = []
    if from_candidates:
        with open(from_candidates, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in data.get("candidates", []):
            code = str(c.get("code", ""))
            if _is_a_share(code):
                stocks.append({"code": code, "ts_code": _resolve_ts_code(code),
                               "name": c.get("name", code)})
    elif sectors:
        sector_codes = [c.strip() for c in sectors.split(",") if c.strip()]
        phase1 = gather_candidates(sector_codes, top_n_per_sector=25)
        for s in phase1["candidates"]:
            stocks.append({"code": s["code"], "ts_code": s["ts_code"],
                           "name": s["name"], "sector_name": s["sector_name"]})
    elif codes:
        for raw in codes.split(","):
            code = raw.strip()
            if not _is_a_share(code):
                print(f"  ⚠️ 跳过非A股代码: {raw}", file=sys.stderr)
                continue
            stocks.append({"code": code, "ts_code": _resolve_ts_code(code), "name": code})
    else:
        raise ValueError("need one of --codes / --sectors / --from-candidates")
    return stocks


def fetch_klines(stocks, max_workers=4) -> dict:
    """Fetch full K-line history for each stock (shared cache with stock_scanner)."""
    kline_map = {}

    def _fetch_one(s):
        return s["ts_code"], _fetch_kline(s["ts_code"])

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in stocks}
        for fut in as_completed(futures):
            ts_code, kline = fut.result()
            if kline and kline.get("data"):
                kline_map[ts_code] = kline
    return kline_map


# ── HTML 报告 ───────────────────────────────────────────────────────────


def _render_md(result) -> str:
    """Markdown report mirror of the JSON summary."""
    meta = result.get("meta", {})
    summary = result.get("summary", {})
    if meta.get("error"):
        return f"# 维科夫买点回测\n\n⚠️ {meta['error']}"
    lines = []
    lines.append("# 维科夫买点回测")
    lines.append("")
    lines.append(f"> 生成 {meta.get('timestamp', '')} | 标的 {meta.get('stocks_tested', 0)} 只 | "
                 f"信号 {meta.get('signal_count', 0)} 次 | 采样日 {meta.get('sample_dates', 0)} | "
                 f"置信度≥{meta.get('min_confidence', 0.3)} | 去重间隔 {meta.get('min_gap', 10)}d")
    lines.append("")
    lines.append("## 信号 vs 全样本基线")
    lines.append("")
    lines.append("| 窗口 | 信号数 | 信号胜率 | 信号均收益 | 基线胜率 | 基线均收益 | 胜率α | 收益α |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for w in meta.get("eval_windows", []):
        wk = str(w)
        sw = summary.get(wk, {})
        s = sw.get("signals") or {}
        b = sw.get("baseline") or {}
        a = sw.get("alpha") or {}
        lines.append(
            f"| {w}日 | {s.get('count', 0)} | {s.get('win_rate', 0)*100:.1f}% | "
            f"{s.get('avg', 0)*100:+.2f}% | {b.get('win_rate', 0)*100:.1f}% | "
            f"{b.get('avg', 0)*100:+.2f}% | {a.get('win_rate', 0)*100:+.1f}% | "
            f"{a.get('avg', 0)*100:+.2f}% |"
        )
    lines.append("")
    lines.append("## 按置信度档位胜率 (5日)")
    lines.append("")
    lines.append("| 档位 | 信号数 | 胜率 | 均收益 |")
    lines.append("|---|---|---|---|")
    for band, st in (result.get("by_confidence") or {}).get("5", {}).items():
        if st:
            lines.append(f"| {band} | {st['count']} | {st['win_rate']*100:.1f}% | {st['avg']*100:+.2f}% |")
    lines.append("")
    lines.append("## 按子阶段胜率 (5日)")
    lines.append("")
    lines.append("| 子阶段 | 信号数 | 胜率 | 均收益 |")
    lines.append("|---|---|---|---|")
    for sub, st in (result.get("by_sub_phase") or {}).get("5", {}).items():
        if st:
            lines.append(f"| {sub} | {st['count']} | {st['win_rate']*100:.1f}% | {st['avg']*100:+.2f}% |")
    lines.append("")
    lines.append("## 按买点等级表现与风险 (5日)")
    lines.append("")
    lines.append("| 买点 | 信号数 | 胜率 | 均收益 | 平均MAE | 平均MFE |")
    lines.append("|---|---|---|---|---|---|")
    level_labels = {
        "level_1": "一级（Spring/Test）",
        "level_2": "二级（SOS后LPS）",
        "level_3": "三级（JAC/BU后再确认）",
        "ungraded": "未分级",
    }
    level_stats = (result.get("by_buy_level") or {}).get("5", {})
    level_risk = (result.get("risk_by_buy_level") or {}).get("5", {})
    for level in ("level_1", "level_2", "level_3", "ungraded"):
        st = level_stats.get(level) or {}
        risk = level_risk.get(level) or {}
        if not st and not risk:
            continue
        lines.append(
            f"| {level_labels[level]} | {st.get('count', risk.get('count', 0))} | "
            f"{st.get('win_rate', 0)*100:.1f}% | {st.get('avg', 0)*100:+.2f}% | "
            f"{risk.get('avg_mae', 0)*100:+.2f}% | {risk.get('avg_mfe', 0)*100:+.2f}% |"
        )
    evidence = result.get("evidence") or {}
    counts = evidence.get("counts") or {}
    lines.append("")
    lines.append(
        f"> 证据状态: {evidence.get('status', '-')}；每级最低样本 "
        f"{evidence.get('minimum_signals_per_level', '-')}；"
        f"一级 {counts.get('level_1', 0)} / 二级 {counts.get('level_2', 0)} / "
        f"三级 {counts.get('level_3', 0)}。"
    )
    lines.append("")
    lines.append("## IC (置信度/100分 对前向收益)")
    lines.append("")
    lines.append("| 窗口 | IC(置信度) | IC(100分) |")
    lines.append("|---|---|---|")
    for w in meta.get("eval_windows", []):
        wk = str(w)
        ic = (result.get("ic") or {}).get(wk, {})
        c = ic.get("confidence")
        sc = ic.get("score_100")
        lines.append(f"| {w}日 | {f'{c:+.3f}' if c is not None else '-'} | "
                     f"{f'{sc:+.3f}' if sc is not None else '-'} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*仅供学习参考,不构成投资建议。*")
    return "\n".join(lines)


def _generate_html(result, ts) -> str:
    """HTML report mirror."""
    meta = result.get("meta", {})
    summary = result.get("summary", {})

    def _win(ret):
        return f"{ret*100:+.2f}%"

    # win rate by window: signals vs baseline
    windows = meta.get("eval_windows", [])
    sw_rates = [(summary.get(str(w), {}).get("signals") or {}).get("win_rate", 0) * 100 for w in windows]
    bs_rates = [(summary.get(str(w), {}).get("baseline") or {}).get("win_rate", 0) * 100 for w in windows]
    sg_avgs = [(summary.get(str(w), {}).get("signals") or {}).get("avg", 0) * 100 for w in windows]

    # confidence bands (5d)
    conf_rows = ""
    for band, st in (result.get("by_confidence") or {}).get("5", {}).items():
        if st:
            conf_rows += (f"<tr><td>{band}</td><td>{st['count']}</td>"
                          f"<td class='{'sp' if st['win_rate']>=0.5 else 'sn'}'>{st['win_rate']*100:.1f}%</td>"
                          f"<td class='{'sp' if st['avg']>0 else 'sn'}'>{_win(st['avg'])}</td></tr>")

    sub_rows = ""
    for sub, st in (result.get("by_sub_phase") or {}).get("5", {}).items():
        if st:
            sub_rows += (f"<tr><td>{sub}</td><td>{st['count']}</td>"
                         f"<td class='{'sp' if st['win_rate']>=0.5 else 'sn'}'>{st['win_rate']*100:.1f}%</td>"
                         f"<td class='{'sp' if st['avg']>0 else 'sn'}'>{_win(st['avg'])}</td></tr>")

    level_labels = {
        "level_1": "一级（Spring/Test）",
        "level_2": "二级（SOS后LPS）",
        "level_3": "三级（JAC/BU后再确认）",
        "ungraded": "未分级",
    }
    level_stats = (result.get("by_buy_level") or {}).get("5", {})
    level_risk = (result.get("risk_by_buy_level") or {}).get("5", {})
    level_rows = ""
    for level in ("level_1", "level_2", "level_3", "ungraded"):
        st = level_stats.get(level) or {}
        risk = level_risk.get(level) or {}
        if not st and not risk:
            continue
        win_rate = st.get("win_rate", 0)
        avg = st.get("avg", 0)
        level_rows += (
            f"<tr><td>{level_labels[level]}</td>"
            f"<td>{st.get('count', risk.get('count', 0))}</td>"
            f"<td class='{('sp' if win_rate >= 0.5 else 'sn')}'>{win_rate*100:.1f}%</td>"
            f"<td class='{('sp' if avg > 0 else 'sn')}'>{_win(avg)}</td>"
            f"<td>{_win(risk.get('avg_mae', 0))}</td>"
            f"<td>{_win(risk.get('avg_mfe', 0))}</td></tr>"
        )
    evidence = result.get("evidence") or {}
    evidence_counts = evidence.get("counts") or {}
    evidence_note = (
        f"证据状态: {evidence.get('status', '-')}；每级最低样本 "
        f"{evidence.get('minimum_signals_per_level', '-')}；"
        f"一级 {evidence_counts.get('level_1', 0)} / 二级 {evidence_counts.get('level_2', 0)} / "
        f"三级 {evidence_counts.get('level_3', 0)}"
    )

    ic_rows = ""
    for w in windows:
        wk = str(w)
        ic = (result.get("ic") or {}).get(wk, {})
        c = ic.get("confidence")
        sc = ic.get("score_100")
        ic_rows += (f"<tr><td>{w}日</td>"
                    f"<td class='{'sp' if (c or 0)>=0 else 'sn'}'>{f'{c:+.3f}' if c is not None else '-'}</td>"
                    f"<td class='{'sp' if (sc or 0)>=0 else 'sn'}'>{f'{sc:+.3f}' if sc is not None else '-'}</td></tr>")

    sig_rows = ""
    for s in (result.get("signals") or [])[:50]:
        r5 = s["returns"].get("5")
        dc = "sp" if (r5 or 0) > 0 else "sn"
        sig_rows += (f"<tr><td>{s['date']}</td><td><strong>{s['name']}</strong><br>"
                     f"<span style='color:#86868b;font-size:12px'>{s['code']}</span></td>"
                     f"<td>{s['phase']}</td><td>{s['sub_phase']}</td>"
                     f"<td>{s['confidence']:.2f}</td><td>{s['score_100']:.0f}</td>"
                     f"<td class='{dc}'>{_win(r5) if r5 is not None else '-'}</td></tr>")

    chart_data = json.dumps({
        "windows": [f"{w}日" for w in windows],
        "sig_win": sw_rates,
        "base_win": bs_rates,
        "sig_avg": sg_avgs,
    })

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>维科夫买点回测 {ts}</title>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1100px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:32px 36px}}
h1{{font-size:24px}} h2{{font-size:18px;margin:22px 0 10px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:16px 0;border:1px solid #e5e7eb;border-left:4px solid #1d4ed8}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.card{{background:#f9fafb;border-radius:8px;padding:12px 16px;text-align:center;flex:1;min-width:100px}}
.card .num{{font-size:22px;font-weight:700;color:#1d4ed8}} .card .lbl{{font-size:12px;color:#86868b;margin-top:2px}}
table{{width:100%;border-collapse:collapse;margin-bottom:8px;border-radius:8px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px}}
th{{background:#1d4ed8;color:#fff;font-size:12px}}
.sp{{color:#dc2626;font-weight:600}} .sn{{color:#16a34a;font-weight:600}}
.chart-box{{width:100%;height:320px;margin:10px 0}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:28px}}
</style></head><body><div class="w">
<header><h1>🏛 维科夫买点回测</h1>
<p class="dt">生成 {meta.get('timestamp','')} | 标的 {meta.get('stocks_tested',0)} 只 | 信号 {meta.get('signal_count',0)} 次 |
置信度≥{meta.get('min_confidence',0.3)} | 去重间隔 {meta.get('min_gap',10)}d</p></header>

<div class="summary">
<div class="card"><div class="num">{meta.get('stocks_tested',0)}</div><div class="lbl">标的数</div></div>
<div class="card"><div class="num">{meta.get('signal_count',0)}</div><div class="lbl">信号次数</div></div>
<div class="card"><div class="num">{(summary.get('5',{}).get('signals') or {}).get('win_rate',0)*100:.0f}%</div><div class="lbl">5日信号胜率</div></div>
<div class="card"><div class="num">{(summary.get('5',{}).get('baseline') or {}).get('win_rate',0)*100:.0f}%</div><div class="lbl">5日基线胜率</div></div>
</div>

<div class="sec"><h2>📊 胜率 vs 基线</h2><div id="winChart" class="chart-box"></div></div>
<div class="sec"><h2>📊 信号均收益</h2><div id="avgChart" class="chart-box"></div></div>
<div class="sec"><h2>🏷 按置信度档位 (5日)</h2>
<table><thead><tr><th>档位</th><th>信号数</th><th>胜率</th><th>均收益</th></tr></thead><tbody>{conf_rows or '<tr><td colspan="4">无信号</td></tr>'}</tbody></table></div>
<div class="sec"><h2>🏷 按子阶段 (5日)</h2>
<table><thead><tr><th>子阶段</th><th>信号数</th><th>胜率</th><th>均收益</th></tr></thead><tbody>{sub_rows or '<tr><td colspan="4">无信号</td></tr>'}</tbody></table></div>
<div class="sec"><h2>🏷 按买点等级表现与风险 (5日)</h2>
<table><thead><tr><th>买点</th><th>信号数</th><th>胜率</th><th>均收益</th><th>平均MAE</th><th>平均MFE</th></tr></thead><tbody>{level_rows or '<tr><td colspan="6">无信号</td></tr>'}</tbody></table>
<p class="dt">{evidence_note}</p></div>
<div class="sec"><h2>📐 IC (置信度/100分 → 前向收益)</h2>
<table><thead><tr><th>窗口</th><th>IC置信度</th><th>IC100分</th></tr></thead><tbody>{ic_rows or '<tr><td colspan="3">样本不足</td></tr>'}</tbody></table></div>
<div class="sec"><h2>📋 信号明细 (Top 50)</h2>
<table><thead><tr><th>日期</th><th>标的</th><th>阶段</th><th>子阶段</th><th>置信</th><th>100分</th><th>5日收益</th></tr></thead><tbody>{sig_rows or '<tr><td colspan="7">无信号</td></tr>'}</tbody></table></div>
<footer><p class="disc">仅供学习参考,不构成投资建议。数据: 东方财富K线。</p></footer></div>
<script>
var cd = {chart_data};
if (cd.windows && cd.windows.length) {{
  Plotly.newPlot('winChart', [
    {{x:cd.windows, y:cd.sig_win, type:'bar', name:'信号胜率', marker:{{color:'#dc2626'}}}},
    {{x:cd.windows, y:cd.base_win, type:'bar', name:'基线胜率', marker:{{color:'#9ca3af'}}}}
  ], {{barmode:'group', margin:{{l:50,r:20,t:10,b:40}}, yaxis:{{title:'胜率(%)',range:[0,100]}}, plot_bgcolor:'#fafafa', paper_bgcolor:'#fafafa', font:{{family:'PingFang SC',size:13}}}}, {{responsive:true,displayModeBar:false}});
  Plotly.newPlot('avgChart', [
    {{x:cd.windows, y:cd.sig_avg, type:'bar', name:'信号均收益', marker:{{color:'#1d4ed8'}}}}
  ], {{margin:{{l:50,r:20,t:10,b:40}}, yaxis:{{title:'收益(%)'}}, plot_bgcolor:'#fafafa', paper_bgcolor:'#fafafa', font:{{family:'PingFang SC',size:13}}}}, {{responsive:true,displayModeBar:false}});
}}
</script></body></html>"""


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="维科夫买点回测 — 验证 /candidates 买点信号历史胜率")
    parser.add_argument("--codes", help="逗号分隔A股代码")
    parser.add_argument("--sectors", help="逗号分隔板块代码(BK...), 复用 /candidates 宇宙")
    parser.add_argument("--from-candidates", help="从 daily_candidates --json 输出读候选代码")
    parser.add_argument("--lookback-days", type=int, default=120, help="回测天数(默认120)")
    parser.add_argument("--eval-windows", default="5,10,20", help="前向窗口(天),逗号分隔")
    parser.add_argument("--sample-interval", type=int, default=5, help="采样间隔(天,默认5)")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                        help="买点置信度阈值(默认0.3,与漏斗一致)")
    parser.add_argument("--min-gap", type=int, default=10,
                        help="同标的两次信号的间隔(天,默认10)")
    parser.add_argument("--output", help="JSON输出路径(默认stdout)")
    parser.add_argument("--output-html", action="store_true", help="生成HTML报告")
    args = parser.parse_args()

    eval_windows = [int(w) for w in args.eval_windows.split(",") if w.strip()]

    print("[1/3] 解析标的宇宙...", file=sys.stderr)
    try:
        stocks = build_stocks(codes=args.codes, sectors=args.sectors,
                              from_candidates=args.from_candidates)
    except ValueError as e:
        parser.error(str(e))
    if not stocks:
        print("❌ 宇宙为空", file=sys.stderr)
        sys.exit(1)
    print(f"  标的 {len(stocks)} 只", file=sys.stderr)

    print("[2/3] 抓取K线...", file=sys.stderr)
    start = time.time()
    kline_map = fetch_klines(stocks)
    print(f"  K线可用 {len(kline_map)}/{len(stocks)} ({time.time()-start:.0f}s)", file=sys.stderr)
    if len(kline_map) < 2:
        print("❌ 可用K线不足2只", file=sys.stderr)
        sys.exit(1)

    print("[3/3] 历史重放回测...", file=sys.stderr)
    result = run_backtest(stocks, kline_map,
                          lookback_days=args.lookback_days,
                          eval_windows=tuple(eval_windows),
                          sample_interval=args.sample_interval,
                          min_confidence=args.min_confidence,
                          min_gap=args.min_gap)

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"JSON: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")

    if args.output_html:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = REPORTS_DIR / f"wyckoff-backtest-{ts}.html"
        html_path.write_text(_generate_html(result, ts), encoding="utf-8")
        print(f"HTML: {html_path}", file=sys.stderr)

    # Console summary
    meta = result.get("meta", {})
    if meta.get("error"):
        print(f"❌ {meta['error']}", file=sys.stderr)
        sys.exit(1)
    summary = result.get("summary", {})
    print(f"\n信号 {meta.get('signal_count', 0)} 次 | 标的 {meta.get('stocks_tested', 0)} 只", file=sys.stderr)
    for w in eval_windows:
        sw = summary.get(str(w), {})
        s = sw.get("signals") or {}
        b = sw.get("baseline") or {}
        print(f"  {w}日: 信号胜率 {s.get('win_rate', 0)*100:.1f}% "
              f"(基线 {b.get('win_rate', 0)*100:.1f}%) | "
              f"均收益 {s.get('avg', 0)*100:+.2f}% (基线 {b.get('avg', 0)*100:+.2f}%) "
              f"| n={s.get('count', 0)}", file=sys.stderr)


if __name__ == "__main__":
    main()
