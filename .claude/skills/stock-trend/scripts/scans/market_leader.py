#!/usr/bin/env python3
"""Market leader & core stock scanner (/longtou).

Three-phase architecture:
  Phase 1: Scan A-share market for hot sectors (板块热点扫描)
  Phase 2: Filter leaders (龙头) and core stocks (中军) per sector
  Phase 3: Deep analysis via existing pipeline + scoring

Usage:
    python3 market_leader.py [--top N] [--sector <板块名>] [--compact] [--output-html]

Examples:
    python3 market_leader.py
    python3 market_leader.py --top 5 --compact
    python3 market_leader.py --sector 半导体
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_LISTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))
from fetchers.sector_data import (
    get_sector_list,
    get_sector_rankings,
    get_sector_stocks,
    rank_hot_sectors,
    filter_leaders,
    filter_core_stocks,
)
from core.resolve_code import code_to_ts_code
from analysis.quality_gate import check_signal_consistency

# Optional bridge import (for --sectors-from integration with ths-theme)
try:
    from bridge.sector_feeder import map_ths_sector_to_em
    _HAS_BRIDGE = True
except ImportError:
    map_ths_sector_to_em = None  # type: ignore
    _HAS_BRIDGE = False


# ──────────────────────── Phase 1: Sector Scanning ────────────────────────


def scan_hot_sectors(top_n: int = 10) -> list[dict]:
    """Phase 1: Scan and rank hot sectors.

    Args:
        top_n: number of hot sectors to return.

    Returns:
        List of hot sector dicts with hot_score.
    """
    print(f"[Phase 1/3] Scanning top {top_n} hot sectors...")
    rankings = get_sector_rankings()
    total = rankings["meta"]["total_sectors"]
    hot = rank_hot_sectors(rankings, top_n, min_stocks=8)
    print(f"  Scanned {total} sectors, top {len(hot)} hot sectors identified")
    return hot


# ── Integration: load qualified sectors from ths-theme output ──


def load_sectors_from_file(path: str) -> list[dict]:
    """Load qualified sectors from ths-theme output JSON.

    Returns list of sector dicts with heat_score, zt_score.
    Returns empty list on any failure.
    """
    p = Path(path)
    if not p.exists():
        print(f"  ⚠️ File not found: {path}")
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sectors = data.get("sectors", [])
        if not sectors:
            print("  ⚠️ No sectors in qualified file")
        return sectors
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"  ⚠️ Failed to parse {path}: {e}")
        return []


def compute_sector_boost(heat: float, zt_score: float) -> float:
    """Compute sector boost for leader scoring.

    boost = (heat/33.3) × 0.15 + (zt_score/33.3) × 0.15
    Range: 0 ~ +0.9

    Args:
        heat: ths-theme industry heat score (0-100).
        zt_score: ths-theme zt concept score (0-100).

    Returns:
        Boost value in [0, ~0.9].
    """
    if heat <= 0 and zt_score <= 0:
        return 0.0
    return round(
        (max(0, min(100, heat)) / 33.3) * 0.15
        + (max(0, min(100, zt_score)) / 33.3) * 0.15,
        4,
    )


def find_sector_by_name(name: str) -> Optional[dict]:
    """Find a sector by exact or partial name match."""
    sectors = get_sector_list()
    for s in sectors:
        if s["name"] == name or name in s["name"]:
            return s
    return None


# ──────────────────────── Phase 2: Leader/Core Filtering ────────────────────────


def analyze_sector(sector: dict, leader_n: int = 3, core_n: int = 3) -> dict:
    """Phase 2: Filter leaders and core stocks for one sector.

    Args:
        sector: sector dict with code, name.
        leader_n: number of leaders to return.
        core_n: number of core stocks to return.

    Returns:
        Sector dict with leaders, core_stocks, and filter stats added.
    """
    code = sector["code"]
    name = sector["name"]
    print(f"  Analyzing {name} ({code})...")

    stocks = get_sector_stocks(code, top_n=50)
    if not stocks:
        print(f"    No stocks found")
        return {**sector, "stocks_count": 0, "leaders": [], "core_stocks": []}

    # Hard filter: ST removal + market cap bounds
    filtered_stocks = []
    excluded = []
    for s in stocks:
        s_name = s.get("name", "")
        mcap = s.get("market_cap") or 0

        if any(kw in s_name for kw in ("ST", "*ST", "退")):
            excluded.append({"code": s["code"], "name": s_name, "reason": "ST/退市风险"})
            continue
        if mcap < 5e9:
            excluded.append({"code": s["code"], "name": s_name, "reason": "市值过小(<50亿)"})
            continue

        filtered_stocks.append(s)

    leaders = filter_leaders(filtered_stocks, top_n=leader_n)
    cores = filter_core_stocks(filtered_stocks, top_n=core_n)

    # Dedup: remove from cores any stock already in leaders
    leader_codes = {s["code"] for s in leaders}
    cores = [s for s in cores if s["code"] not in leader_codes]

    # Collect flags for each selected stock
    all_selected = leaders + cores
    for s in all_selected:
        s.setdefault("flags", {})
        s["flags"]["volume_breakout"] = s.pop("_volume_breakout", False)
        s["flags"]["is_laggard"] = s.pop("_is_laggard", False)

    print(f"    {len(stocks)} stocks → {len(filtered_stocks)} after filter"
          f" ({len(excluded)} excluded), {len(leaders)} leaders, {len(cores)} core")
    return {
        **sector,
        "stocks_count": len(stocks),
        "stocks_after_filter": len(filtered_stocks),
        "excluded": excluded[:10],
        "leaders": leaders,
        "core_stocks": cores,
    }


def run_phase2(hot_sectors: list[dict], leader_n: int = 3, core_n: int = 3,
               max_workers: int = 4) -> list[dict]:
    """Phase 2: Analyze all hot sectors in parallel."""
    print(f"[Phase 2/3] Filtering leaders + core stocks per sector...")
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(analyze_sector, s, leader_n, core_n): s
            for s in hot_sectors
        }
        for fut in as_completed(fut_map):
            try:
                results.append(fut.result())
            except Exception as e:
                s = fut_map[fut]
                results.append({**s, "error": str(e), "leaders": [], "core_stocks": []})

    results.sort(key=lambda x: x.get("hot_score", 0), reverse=True)
    return results


# ──────────────────────── Phase 3: Deep Analysis ────────────────────────


def run_deep_analysis(code: str, timeout: int = 60, max_retries: int = 1) -> dict:
    """Run full pipeline + scoring for one stock code.

    Retries once on non-timeout failures. Logs failure reasons for diagnosis.

    Returns analysis result dict.
    """
    result = {"code": code}
    ts_code = code_to_ts_code(code)
    result["ts_code"] = ts_code

    # Run pipeline with retry
    pipeline_cmd = [sys.executable, str(SCRIPT_DIR / "pipeline/runner.py"),
                    "--code", code]
    pipeline_ok = False
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(pipeline_cmd, capture_output=True, text=True,
                                  timeout=timeout)
            if proc.returncode == 0:
                pipeline_ok = True
                break
            result["pipeline_stderr"] = proc.stderr[-200:] if proc.stderr else ""
            if attempt < max_retries:
                time.sleep(1)
        except subprocess.TimeoutExpired:
            result["error"] = "pipeline_timeout"
            return result
        except Exception as e:
            result["error"] = str(e)
            if attempt >= max_retries:
                return result

    if not pipeline_ok:
        result["error"] = f"pipeline_failed_after_{max_retries + 1}_attempts"
        return result

    # Run scoring (no retry — local computation)
    scores_cmd = [sys.executable, str(SCRIPT_DIR / "analysis/scores.py"),
                  "--code", code]
    try:
        proc = subprocess.run(scores_cmd, capture_output=True, text=True,
                              timeout=30)
        if proc.returncode != 0:
            stderr_tail = proc.stderr[-100:] if proc.stderr else "unknown"
            result["error"] = f"scoring_failed: {stderr_tail}"
            return result
    except subprocess.TimeoutExpired:
        result["error"] = "scores_timeout"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    # Read scores output
    scores_path = CACHE_DIR / code / "scores.json"
    pipeline_path = CACHE_DIR / code / "pipeline_output.json"
    technical_path = CACHE_DIR / code / "technical.json"

    try:
        scores_data = json.loads(scores_path.read_bytes())
        result["composite_score"] = scores_data.get("composite_score")
        result["direction"] = scores_data.get("direction")
        result["confidence"] = scores_data.get("confidence")
        dims = scores_data.get("scores", {}) or {}
        result["dimension_scores"] = {
            "technical": dims.get("technical"),
            "capital_flow": dims.get("capital_flow"),
            "fundamental": dims.get("fundamental"),
            "sentiment": dims.get("sentiment"),
            "macro": dims.get("macro"),
        }
        result["risks"] = scores_data.get("risks", [])
        rp = scores_data.get("report_params", {}) or {}
        result["stop_loss"] = rp.get("stop_loss")
        result["targets"] = {
            "conservative": rp.get("target_conservative"),
            "moderate": rp.get("target_moderate"),
        }
    except Exception:
        pass

    try:
        tech_data = json.loads(technical_path.read_bytes())
        result["trend_stage"] = tech_data.get("summary", {}).get("trend_stage")
    except Exception:
        pass

    try:
        pipe_data = json.loads(pipeline_path.read_bytes())
        result["pipeline_errors"] = pipe_data.get("errors", [])
    except Exception:
        pass

    return result


def run_phase3(candidates: list[dict], roles: list[str],
               max_workers: int = 4) -> dict:
    """Phase 3: Run deep analysis on unique candidate stocks.

    Args:
        candidates: list of {code, role, sector_name, ...}.
        roles: parallel list of role labels.
        max_workers: parallel pipeline workers.

    Returns:
        Dict mapping code → analysis result.
    """
    print(f"[Phase 3/3] Deep analysis via pipeline...")
    seen = set()
    unique = []
    for c, r in zip(candidates, roles):
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    print(f"  Analyzing {len(unique)} unique candidates (max {max_workers} parallel)...")
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(run_deep_analysis, c["code"]): c
            for c in unique
        }
        for fut in as_completed(fut_map):
            c = fut_map[fut]
            try:
                results[c["code"]] = fut.result()
            except Exception as e:
                results[c["code"]] = {"code": c["code"], "error": str(e)}

    return results


# ──────────────────────── Quality Gate: Penalties ────────────────────────

# Minimum useful stop-loss distance for mid-term holding (1-6 months)
MIN_STOP_LOSS_PCT = 0.02  # 2%


def _apply_quality_penalties(candidates: list[dict]) -> list[dict]:
    """Apply quality penalties to candidate scores for ranking.

    Penalties:
      - stop_loss too close (<2%): -0.15
      - stop_loss missing when direction is bullish: -0.05
      - deep analysis failed (fallback scoring): -0.10
      - signal consistency conflict: -0.10 to -0.20

    Args:
        candidates: list of dicts with composite_score, stop_loss, current_price,
                    direction, risks.

    Returns:
        Same list sorted by adjusted_score descending.
    """
    for c in candidates:
        penalty = 0.0
        score = c.get("composite_score") or 0

        # Stop-loss distance penalty
        stop = c.get("stop_loss")
        price = c.get("current_price") or 0
        if stop and price > 0:
            stop_pct = (price - stop) / price
            if 0 < stop_pct < MIN_STOP_LOSS_PCT:
                penalty += 0.15
        elif not stop and "偏多" in (c.get("direction") or ""):
            penalty += 0.05

        # Fallback scoring penalty
        risks = c.get("risks") or []
        if any("深度分析数据获取失败" in r for r in risks):
            penalty += 0.10

        # Signal consistency penalty
        direction = c.get("direction") or ""
        if check_signal_consistency and risks:
            consistency = check_signal_consistency(direction, risks)
            if consistency["has_conflict"]:
                penalty += consistency["penalty"]
                c["signal_conflict"] = consistency["conflict_detail"]

        c["quality_penalty"] = round(penalty, 3)
        c["adjusted_score"] = round(score - penalty, 3)

    candidates.sort(key=lambda x: x.get("adjusted_score", 0), reverse=True)
    return candidates


# ──────────────────────── Report Generation ────────────────────────


def _score_to_stars(score: float) -> str:
    if score is None:
        return "N/A"
    if score >= 0.7:
        return "★★★"
    if score >= 0.5:
        return "★★☆"
    if score >= 0.3:
        return "★☆☆"
    return "☆☆☆"


def _signal_str(score: float) -> str:
    if score is None:
        return "--"
    if score >= 2.0:
        return "↑"
    if score >= 0.5:
        return "↗"
    if score >= -0.5:
        return "→"
    if score >= -2.0:
        return "↘"
    return "↓"


def _fallback_score(stock: dict) -> dict:
    """Compute basic score from Phase 2 data when pipeline unavailable.

    Score range 0-1, matching pipeline composite_score convention.
    """
    change = stock.get("change_pct") or 0
    amount = stock.get("amount") or 0

    # change: -10% → 0, 0% → 0.5, +10% → 1.0
    change_norm = max(0, min(1, (change + 10) / 20))
    # amount: normalize relative to 10亿
    amount_norm = max(0, min(1, amount / 1e9))

    composite = round(change_norm * 0.6 + amount_norm * 0.4, 3)

    if composite > 0.5:
        direction = "偏多"
        confidence = "低"
    elif composite > 0.35:
        direction = "震荡偏多"
        confidence = "低"
    elif composite > 0.2:
        direction = "震荡偏空"
        confidence = "低"
    else:
        direction = "偏空"
        confidence = "低"

    return {
        "composite_score": composite,
        "direction": direction,
        "confidence": confidence,
        "dimension_scores": {
            "technical": round(change_norm, 2),
            "capital_flow": 0,
            "fundamental": 0,
            "sentiment": 0,
            "macro": 0,
        },
        "risks": ["深度分析数据获取失败, 使用基础评分"],
        "stop_loss": None,
        "targets": {},
        "trend_stage": None,
    }


def generate_report(output: dict, compact: bool = False) -> str:
    """Generate Markdown report from scan output."""
    meta = output.get("meta", {})
    sectors = output.get("sectors_analyzed", [])
    pipeline_summary = output.get("pipeline_summary", {})
    best_picks = output.get("best_picks", [])
    risk_tips = output.get("risk_tips", [])
    scan_time = meta.get("scan_time", "")
    elapsed = meta.get("elapsed_seconds", 0)

    lines = []
    lines.append(f"## 🔍 龙头中军扫描报告  {scan_time}")
    lines.append(f"")
    lines.append(f"▸ 扫描板块: {meta.get('total_sectors', 0)} 个")
    lines.append(f"▸ 热点板块: {len(sectors)} 个")
    lines.append(f"▸ 候选标的: {meta.get('total_candidates', 0)} 只")
    lines.append(f"▸ 耗时: {elapsed}s")
    lines.append(f"")

    for sec in sectors:
        name = sec.get("name", "?")
        hot = sec.get("hot_score", 0)
        change = sec.get("change_pct", 0)
        lines.append(f"### {name} (热度:{hot:.0f} 涨幅:{change:.1f}%)")
        ths_heat = sec.get("ths_heat_score")
        ths_zt = sec.get("ths_zt_score")
        if ths_heat is not None:
            lines.append(f"> 行业热力:{ths_heat:.0f}/100  涨停概念:{ths_zt:.0f}/100")
        lines.append(f"")

        leaders = sec.get("leaders", [])
        cores = sec.get("core_stocks", [])
        sector_analysis = sec.get("deep_analysis", {})

        if leaders:
            lines.append(f"**龙头**")
            for s in leaders:
                code = s["code"]
                da = pipeline_summary.get(code, {})
                score = da.get("composite_score")
                direction = da.get("direction", "?")
                stop_loss = da.get("stop_loss")
                stars = _score_to_stars(score)
                signal = _signal_str(score if score else 0)
                sl_str = f" 止损:{stop_loss}" if stop_loss else ""

                if compact:
                    line = f"- {s.get('name','?')}({code}) {signal}{direction} {stars}"
                else:
                    dims = da.get("dimension_scores", {})
                    dim_str = " ".join(
                        f"{k}:{v}" for k, v in dims.items() if v is not None
                    )
                    targets = da.get("targets", {})
                    t_str = (
                        f" 目标:{targets.get('conservative','?')}/"
                        f"{targets.get('moderate','?')}"
                        if targets.get("conservative")
                        else ""
                    )
                    boost = da.get("sector_boost")
                    boost_str = f" [板块加成:{boost:+.2f}]" if boost else ""
                    line = (
                        f"- {s.get('name','?')}({code}) "
                        f"涨跌幅:{s.get('change_pct','?')}%"
                        f" {signal}{direction} {stars}"
                        f"\n  {dim_str}"
                        f"\n  {sl_str} {t_str}"
                        f"{boost_str}"
                    )
                lines.append(line)

        if cores:
            lines.append(f"")
            lines.append(f"**中军**")
            for s in cores:
                code = s["code"]
                da = pipeline_summary.get(code, {})
                score = da.get("composite_score")
                direction = da.get("direction", "?")
                stop_loss = da.get("stop_loss")
                stars = _score_to_stars(score)
                signal = _signal_str(score if score else 0)
                sl_str = f" 止损:{stop_loss}" if stop_loss else ""

                if compact:
                    line = f"- {s.get('name','?')}({code}) {signal}{direction} {stars}"
                else:
                    line = (
                        f"- {s.get('name','?')}({code}) "
                        f"涨跌幅:{s.get('change_pct','?')}% "
                        f"市值:{s.get('market_cap','?')} "
                        f"PE:{s.get('pe','?')}"
                        f" {signal}{direction} {stars}"
                        f"\n  {sl_str}"
                    )
                lines.append(line)

        if not compact and sector_analysis:
            ver = sector_analysis.get("verdict", {})
            if ver:
                lines.append(f"")
                lines.append(f"  板块研判: {ver.get('logic','')}")
                lines.append(f"  持续性: {ver.get('sustainability','')}")
                lines.append(f"  风险: {ver.get('risk','')}")

        lines.append(f"")

    if not compact:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"### 🏆 综合推荐")
        if best_picks:
            for i, pick in enumerate(best_picks, 1):
                lines.append(f"{i}. {pick}")
        else:
            lines.append(f"暂无明确推荐")
        lines.append(f"")

        if risk_tips:
            lines.append(f"### ⚠️ 风险提示")
            for tip in risk_tips:
                lines.append(f"- {tip}")
            lines.append(f"")

    lines.append(f"---")
    lines.append(f"> *本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。*")
    lines.append(f"> *报告时间: {scan_time} | 耗时: {elapsed:.1f}s*")

    return "\n".join(lines)


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="市场龙头中军扫描 (/longtou)"
    )
    parser.add_argument("--top", type=int, default=10,
                        help="热点板块数量, 默认10")
    parser.add_argument("--sector", type=str,
                        help="指定板块名, 跳过板块扫描")
    parser.add_argument("--sectors-from", type=str,
                        help="从 qualified_sectors.json 读取热板块列表，跳过全市场扫描")
    parser.add_argument("--compact", action="store_true",
                        help="精简输出")
    parser.add_argument("--output-html", action="store_true",
                        help="生成HTML报告")
    args = parser.parse_args()

    start = time.time()
    output: dict[str, Any] = {
        "meta": {
            "scan_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "top_n": args.top,
        },
        "sectors_analyzed": [],
        "pipeline_summary": {},
        "best_picks": [],
        "risk_tips": [],
    }

    # ── Phase 1: Sector scan, single sector, or sectors-from ──
    qualified = []
    sector_heat_map: dict[str, dict] = {}  # name → {heat_score, zt_score}

    if args.sectors_from:
        print(f"[Phase 1/3] Loading sectors from: {args.sectors_from}")
        qualified = load_sectors_from_file(args.sectors_from)
        if not qualified:
            print("  ⚠️ 无有效热板块列表，降级为全市场扫描")
            hot_sectors = scan_hot_sectors(args.top)
        else:
            print(f"  Loaded {len(qualified)} qualified sectors")
            hot_sectors = []
            for qs in qualified:
                name = qs["name"]
                sector_heat_map[name] = {
                    "heat_score": qs.get("heat_score", 0),
                    "zt_score": qs.get("zt_score", 0),
                    "lhb_score": qs.get("lhb_score", 0),
                    "lhb_direction": qs.get("lhb_direction", ""),
                }
                # Try to find matching 东方财富 sector via bridge mapping
                em_names = map_ths_sector_to_em(name) if _HAS_BRIDGE and map_ths_sector_to_em else []
                found = False
                if em_names:
                    for em_name in em_names:
                        sector = find_sector_by_name(em_name)
                        if sector:
                            sector["heat_score"] = qs.get("heat_score", 0)
                            hot_sectors.append(sector)
                            found = True
                            break
                if not found:
                    # Use original name as fallback — will be marked in report
                    sector = find_sector_by_name(name)
                    if sector:
                        sector["heat_score"] = qs.get("heat_score", 0)
                        hot_sectors.append(sector)
                        found = True
                if not found:
                    print(f"  ⚠️ 板块 '{name}' 在东方财富中未找到对应")
            print(f"  Matched {len(hot_sectors)}/{len(qualified)} sectors")
    elif args.sector:
        print(f"[Phase 1/3] Looking up sector: {args.sector}")
        sector = find_sector_by_name(args.sector)
        if not sector:
            print(f"Error: 未找到板块 '{args.sector}'", file=sys.stderr)
            sys.exit(1)
        hot_sectors = [sector]
        # Need to get ranking data for this sector
        rankings = get_sector_rankings()
        matched = [s for s in rankings["sectors"] if s["code"] == sector["code"]]
        if matched:
            hot_sectors = rank_hot_sectors(
                {"meta": rankings["meta"], "sectors": matched}, 1,
                min_stocks=0  # skip filter, user explicitly requested
            )
        else:
            hot_sectors[0]["hot_score"] = 50
        print(f"  Found: {sector['name']} ({sector['code']})")
    else:
        hot_sectors = scan_hot_sectors(args.top)

    output["meta"]["total_sectors"] = len(hot_sectors)

    # ── Phase 2: Filter leaders + core stocks ──
    sectors_analyzed = run_phase2(hot_sectors, leader_n=3, core_n=3)

    # ── DDX Enhancement: fetch DDX data and rescore leaders ──
    try:
        from fetchers.ddx import fetch_ddx_data
        from fetchers.sector_data import rescore_leaders_with_ddx

        all_codes = list(dict.fromkeys(
            s["code"]
            for sec in sectors_analyzed
            for s in sec.get("leaders", []) + sec.get("core_stocks", [])
        ))

        if all_codes:
            print(f"[DDX] Fetching DDX data for {len(all_codes)} candidates...")
            ddx_data = fetch_ddx_data(all_codes)
            if ddx_data:
                print(f"  Found DDX data for {len(ddx_data)} stocks")
                for sec in sectors_analyzed:
                    leaders = sec.get("leaders", [])
                    if leaders:
                        rescored = rescore_leaders_with_ddx(leaders, ddx_data)
                        sec["leaders"] = rescored
                        sec["has_ddx_enhanced"] = True
            else:
                print("  No DDX data available (degraded)")
    except Exception as e:
        print(f"  [DDX] Enhancement skipped: {e}")

    # ── Phase 3: Deep analysis ──
    candidates = []
    roles = []
    for sec in sectors_analyzed:
        for s in sec.get("leaders", []):
            candidates.append({"code": s["code"], "name": s.get("name",""),
                               "sector": sec.get("name","")})
            roles.append("leader")
        for s in sec.get("core_stocks", []):
            candidates.append({"code": s["code"], "name": s.get("name",""),
                               "sector": sec.get("name","")})
            roles.append("core")

    # ── 龙虎榜 Enhancement: fetch and cache per-stock ──
    try:
        from fetchers.longhubang import fetch_longhubang_data

        all_codes = [c["code"] for c in candidates]
        if all_codes:
            print(f"[LHB] Fetching 龙虎榜 data for {len(all_codes)} candidates...")
            lhb_data = fetch_longhubang_data(all_codes)
            if lhb_data:
                print(f"  Found 龙虎榜 data for {len(lhb_data)} stocks")
                for code, lhb in lhb_data.items():
                    code_cache = CACHE_DIR / code
                    code_cache.mkdir(parents=True, exist_ok=True)
                    (code_cache / "longhubang.json").write_text(
                        json.dumps(lhb, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                for code, lhb in lhb_data.items():
                    if lhb.get("risk_level") == "high":
                        name = ""
                        for sec in sectors_analyzed:
                            for s in sec.get("leaders", []) + sec.get("core_stocks", []):
                                if s["code"] == code:
                                    name = s.get("name", "")
                                    break
                        tip = f"{name}({code}): 龙虎榜风险 — 散户主导买入"
                        if tip not in output["risk_tips"]:
                            output["risk_tips"].append(tip)
            else:
                print("  No 龙虎榜 data available (degraded)")
    except Exception as e:
        print(f"  [LHB] Enhancement skipped: {e}")

    output["meta"]["total_candidates"] = len(candidates)

    pipeline_results = {}
    if candidates:
        pipeline_results = run_phase3(candidates, roles, max_workers=4)

    # Fallback: Phase 2 data when pipeline yields no score
    for sec in sectors_analyzed:
        for s in sec.get("leaders", []) + sec.get("core_stocks", []):
            da = pipeline_results.get(s["code"], {})
            if da.get("composite_score") is None:
                pipeline_results[s["code"]] = _fallback_score(s)

    output["pipeline_summary"] = pipeline_results

    # Attach deep analysis to each sector
    for sec in sectors_analyzed:
        leaders_deep = []
        for s in sec.get("leaders", []):
            da = pipeline_results.get(s["code"], {})
            s["deep_score"] = da.get("composite_score")
            s["deep_direction"] = da.get("direction")
            s["deep_confidence"] = da.get("confidence")
            leaders_deep.append(s)

        cores_deep = []
        for s in sec.get("core_stocks", []):
            da = pipeline_results.get(s["code"], {})
            s["deep_score"] = da.get("composite_score")
            s["deep_direction"] = da.get("direction")
            s["deep_confidence"] = da.get("confidence")
            cores_deep.append(s)

        sec["leaders"] = leaders_deep
        sec["core_stocks"] = cores_deep

    output["sectors_analyzed"] = sectors_analyzed

    # ── Apply sector_boost to leader scores ──
    if args.sectors_from and qualified:
        print("[Sector Boost] Applying sector heat boost...")
        for sec in sectors_analyzed:
            sec_name = sec.get("name", "")
            # Find matching heat data
            s_heat = None
            for qs in qualified:
                em_names = map_ths_sector_to_em(qs["name"]) if _HAS_BRIDGE and map_ths_sector_to_em else []
                if sec_name == qs["name"] or sec_name in em_names:
                    s_heat = qs
                    break
            if not s_heat:
                continue
            heat = s_heat.get("heat_score", 0)
            zt = s_heat.get("zt_score", 0)
            boost = compute_sector_boost(heat, zt)
            if boost <= 0:
                continue
            for stock_list_name in ("leaders", "core_stocks"):
                for s in sec.get(stock_list_name, []):
                    da = pipeline_results.get(s["code"], {})
                    orig = da.get("composite_score")
                    if orig is not None:
                        new_score = round(orig + boost, 3)
                        da["composite_score"] = new_score
                        da["sector_boost"] = boost
                        da["original_score"] = orig
                        da["sector_heat"] = heat
                        da["sector_zt"] = zt
            # Also store sector heat data on the sector itself for report
            sec["ths_heat_score"] = heat
            sec["ths_zt_score"] = zt
            sec["ths_lhb_score"] = s_heat.get("lhb_score", 0)
            sec["ths_lhb_direction"] = s_heat.get("lhb_direction", "")
        print(f"  Sector boost applied")

    # ── Best picks & risk tips (with quality penalties) ──
    all_rated = []
    for sec in sectors_analyzed:
        for s in sec.get("leaders", []) + sec.get("core_stocks", []):
            da = pipeline_results.get(s["code"], {})
            score = da.get("composite_score")
            if score is not None:
                all_rated.append({
                    "code": s["code"],
                    "name": s.get("name", ""),
                    "sector": sec.get("name", ""),
                    "direction": da.get("direction", ""),
                    "composite_score": score,
                    "stop_loss": da.get("stop_loss"),
                    "current_price": s.get("current_price") or s.get("close"),
                    "risks": da.get("risks", []),
                })

    all_rated = _apply_quality_penalties(all_rated)

    # Only recommend bullish-direction stocks for mid-term holding
    all_rated = [c for c in all_rated if "偏多" in c.get("direction", "")]

    for item in all_rated[:5]:
        penalty_note = ""
        if item.get("quality_penalty", 0) > 0:
            penalty_note = f" (质量惩罚:-{item['quality_penalty']})"
        output["best_picks"].append(
            f"{item['name']}({item['code']}) [{item['sector']}] "
            f"{item['direction']} 综合分:{item['adjusted_score']}{penalty_note}"
        )

    for sec in sectors_analyzed:
        for s in sec.get("leaders", []):
            da = pipeline_results.get(s["code"], {})
            for risk in da.get("risks", []):
                tip = f"{s.get('name','?')}({s['code']}): {risk}"
                if tip not in output["risk_tips"]:
                    output["risk_tips"].append(tip)

    # ── Timing ──
    elapsed = time.time() - start
    output["meta"]["elapsed_seconds"] = round(elapsed, 1)

    # ── Output ──
    report = generate_report(output, compact=args.compact)

    if args.output_html:
        html = _generate_html_report(output, report)
        ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = REPORTS_LISTS_DIR / f"longtou-{ts_str}.html"
        REPORTS_LISTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML report: {html_path}")

    print(report)

    # Also output JSON to stdout for agent consumption
    json_output = json.dumps(output, ensure_ascii=False, indent=2)
    print(f"\n<!--JSON_OUTPUT-->\n{json_output}\n<!--END_JSON_OUTPUT-->")

    print(f"\nLongtou scan complete in {elapsed:.1f}s")


def _generate_html_report(output: dict, markdown: str) -> str:
    """Generate HTML report matching stock-trend report-template style."""
    scan_time = output["meta"].get("scan_time", "")
    sectors = output.get("sectors_analyzed", [])
    best_picks = output.get("best_picks", [])
    risk_tips = output.get("risk_tips", [])
    elapsed = output["meta"].get("elapsed_seconds", 0)

    def dir_css(direction):
        if not direction:
            return ""
        if "多" in str(direction):
            return "bull"
        if "空" in str(direction):
            return "bear"
        return "neut"

    def score_color(score):
        if score is None:
            return ""
        if score >= 2.0:
            return "bull"
        if score >= 0.5:
            return "sn"
        if score <= -2.0:
            return "bear"
        if score <= -0.5:
            return "sp"
        return "neut"

    sections_html = ""
    for sec in sectors:
        name = sec.get("name", "?")
        hot = sec.get("hot_score", 0)
        change = sec.get("change_pct", 0)

        leaders_rows = ""
        for s in sec.get("leaders", []):
            da = output.get("pipeline_summary", {}).get(s["code"], {})
            score = da.get("composite_score")
            direction = da.get("direction", "?")
            score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "N/A"
            sl = da.get("stop_loss")
            sl_str = f" 止损:{sl}" if sl else ""
            sc = score_color(score)
            dc = dir_css(direction)
            leaders_rows += (
                f"<tr>"
                f"<td>{s.get('name','?')}</td>"
                f"<td class=\"{dc}\">{s['code']}</td>"
                f"<td class=\"{'sp' if (s.get('change_pct') or 0) > 0 else 'sn' if (s.get('change_pct') or 0) < 0 else 'neut'}\">{s.get('change_pct','?')}%</td>"
                f"<td class=\"{sc}\">{score_str}</td>"
                f"<td class=\"{dc}\">{direction}</td>"
                f"<td>{sl_str}</td>"
                f"</tr>"
            )

        cores_rows = ""
        for s in sec.get("core_stocks", []):
            da = output.get("pipeline_summary", {}).get(s["code"], {})
            score = da.get("composite_score")
            direction = da.get("direction", "?")
            score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "N/A"
            mcap = s.get("market_cap", 0)
            mcap_str = f"{mcap/1e8:.0f}亿" if mcap and mcap > 1e8 else str(mcap or "?")
            pe = s.get("pe")
            pe_str = f"{pe:.1f}" if isinstance(pe, (int, float)) and pe > 0 else "N/A"
            sc = score_color(score)
            dc = dir_css(direction)
            cp = s.get('change_pct')
            cp_str = f"{cp:+.1f}%" if isinstance(cp, (int, float)) else "?"
            cp_cls = 'sp' if isinstance(cp, (int, float)) and cp > 0 else 'sn' if isinstance(cp, (int, float)) and cp < 0 else 'neut'
            cores_rows += (
                f"<tr>"
                f"<td>{s.get('name','?')}</td>"
                f"<td class=\"{dc}\">{s['code']}</td>"
                f"<td class=\"{cp_cls}\">{cp_str}</td>"
                f"<td>{mcap_str}</td>"
                f"<td>{pe_str}</td>"
                f"<td class=\"{sc}\">{score_str}</td>"
                f"<td class=\"{dc}\">{direction}</td>"
                f"</tr>"
            )

        sections_html += f"""
        <div class="sec">
            <h2 class="sec-title">{name}</h2>
            <div class="sec-meta">热度: {hot:.0f} | 涨幅: {change:+.1f}% | 上涨: {sec.get('up_count','?')} | 下跌: {sec.get('down_count','?')}</div>
            <h3>龙头股</h3>
            <table>
                <thead><tr><th>名称</th><th>代码</th><th>涨跌幅</th><th>评分</th><th>方向</th><th>止损</th></tr></thead>
                <tbody>{leaders_rows}</tbody>
            </table>
            <h3>中军股</h3>
            <table>
                <thead><tr><th>名称</th><th>代码</th><th>涨跌幅</th><th>市值</th><th>PE</th><th>评分</th><th>方向</th></tr></thead>
                <tbody>{cores_rows}</tbody>
            </table>
        </div>"""

    picks_html = ""
    for p in best_picks:
        picks_html += f"<li>{p}</li>\n"

    risks_html = ""
    for r in risk_tips:
        risks_html += f"<li>{r}</li>\n"

    # Sector ranking table
    rank_rows = ""
    for i, sec in enumerate(sectors, 1):
        name = sec.get("name", "?")
        hot = sec.get("hot_score", 0)
        change = sec.get("change_pct", 0)
        mf = sec.get("main_force_net", 0)
        mf_str = f"{mf/1e8:.1f}亿" if mf else "N/A"
        up = sec.get("up_count", "?")
        dn = sec.get("down_count", "?")
        total = sec.get("total_count", "?")
        rank_rows += (
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{name}</td>"
            f"<td>{hot:.0f}</td>"
            f"<td class=\"{'sp' if change > 0 else 'sn' if change < 0 else 'neut'}\">{change:+.1f}%</td>"
            f"<td>{mf_str}</td>"
            f"<td>{up}/{dn}/{total}</td>"
            f"</tr>"
        )

    rank_table = f"""<div class="rank">
    <h2>板块热度排名</h2>
    <table>
        <thead><tr><th>#</th><th>板块</th><th>热度</th><th>涨幅</th><th>主力净流入</th><th>涨/跌/总</th></tr></thead>
        <tbody>{rank_rows}</tbody>
    </table>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>龙头中军扫描报告 {scan_time}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6;padding:20px}}
.w{{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
header{{border-bottom:2px solid #f0f0f0;padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:24px;color:#1a1a1a;margin-bottom:4px}}
.dt{{color:#86868b;font-size:14px;margin-bottom:8px}}
.meta{{color:#86868b;font-size:14px}}
h2{{font-size:18px;margin:28px 0 14px;padding-bottom:6px;border-bottom:1px solid #f0f0f0;color:#1d4ed8}}
h2.sec-title{{font-size:16px;margin:0 0 6px;padding:0;border:none;color:#1d4ed8}}
.sec-meta{{color:#86868b;font-size:13px;margin-bottom:12px}}
h3{{font-size:15px;margin:16px 0 8px;color:#374151}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
tr:nth-child(even) td{{background:#f9fafb}}
.bull{{color:#dc2626;font-weight:600}}
.bear{{color:#16a34a;font-weight:600}}
.neut{{color:#6b7280}}
.sp{{color:#dc2626;font-weight:600}}
.sn{{color:#16a34a;font-weight:600}}
.sec,.rank,.bp{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.rank h2,.bp h2{{margin-top:0}}
.rank table{{margin-bottom:0}}
ul.risks{{list-style:none;margin-bottom:16px}}
ul.risks li{{padding:8px 0 8px 20px;position:relative;border-bottom:1px solid #f5f5f5;font-size:14px}}
ul.risks li::before{{content:"⚠";color:#dc2626;position:absolute;left:0;font-size:13px}}
.bp ol{{padding-left:20px;font-size:14px;line-height:1.8}}
footer{{margin-top:32px;padding-top:16px;border-top:1px solid #f0f0f0}}
.disc{{color:#a1a1a6;font-size:12px;font-style:italic;text-align:center}}
</style>
</head>
<body>
<div class="w">
<header>
<h1>🔍 龙头中军扫描报告</h1>
<p class="dt">扫描时间: {scan_time} | 耗时: {elapsed}s</p>
<p class="meta">扫描板块: {output['meta'].get('total_sectors',0)} 个 | 候选标的: {output['meta'].get('total_candidates',0)} 只</p>
</header>

<div class="bp">
<h2>🏆 综合推荐</h2>
<ol>{picks_html}</ol>
</div>

{rank_table}

{sections_html}

<h2>⚠️ 风险提示</h2>
<ul class="risks">{risks_html}</ul>

<footer>
<p class="disc">本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。</p>
</footer>
</div>
</body>
</html>"""


if __name__ == "__main__":
    main()
