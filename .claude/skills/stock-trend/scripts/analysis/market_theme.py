#!/usr/bin/env python3
"""Market main line (市场主线) analyzer.

Identifies persistent market themes from daily sector ranking snapshots.
Goes beyond single-day hot sector scanning to find sustained trends.

Three-phase architecture:
  Phase 1: Get today's sector rankings (realtime / cache / snapshot fallback)
  Phase 2: Load snapshot history
  Phase 3: Compute persistence scores from snapshots + classify themes

No BK K-line dependency — all data from East Money sector rankings API.

Usage:
    python3 analyze_market_theme.py [--top 15] [--days 10] [--min-score 30]

Examples:
    python3 analyze_market_theme.py
    python3 analyze_market_theme.py --top 20 --days 15
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
REPORTS_LISTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))
from fetchers.sector_data import (
    get_sector_rankings, rank_hot_sectors,
    save_rankings_cache, load_rankings_cache_full,
    append_daily_snapshot, load_snapshot_history,
    get_last_trading_day,
)


# ──────────────────────── Phase 1: Sector Scan ────────────────────────


def _check_non_trading(all_sectors: list[dict]) -> bool:
    """Detect non-trading day by checking if API returned all-zero data."""
    sample = all_sectors[:20]
    return not any(
        (s.get("up_count", 0) or 0) != 0 or (s.get("down_count", 0) or 0) != 0
        for s in sample
    )


def get_top_sectors(top_n: int = 15) -> tuple[list[dict], str, str]:
    """Phase 1: Get today's top hot sectors.

    Two-tier fallback on non-trading days:
      1. Real-time snapshot API (normal path, appends snapshot)
      2. Cached rankings from last trading day (< 96h)

    Returns:
        (sectors, data_date, source)
        - sectors: ranked sector dicts
        - data_date: date the ranking data represents
        - source: "realtime" | "cache"
    """
    print(f"[Phase 1/3] Scanning top {top_n} sectors...")
    rankings = get_sector_rankings()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Always save rankings for future non-trading-day cache fallback
    save_rankings_cache(rankings)

    hot = rank_hot_sectors(rankings, top_n=top_n, min_stocks=8, min_up_ratio=0.15)

    if hot:
        # ── Tier 1: real-time data ──
        print(f"  Got {len(hot)} hot sectors from {rankings['meta']['total_sectors']} total")
        save_rankings_cache(rankings, hot_sectors=hot)
        # Append daily snapshot for future persistence analysis
        append_daily_snapshot(rankings)
        return hot, today_str, "realtime"

    # ── Zero hot sectors: detect non-trading day vs weak market ──
    all_sectors = rankings.get("sectors", [])
    if not _check_non_trading(all_sectors):
        print(f"  Got 0 hot sectors from {rankings['meta']['total_sectors']} total (weak market)")
        return [], today_str, "realtime"

    # ── Tier 2: cached rankings ──
    print(f"  ⚠️ 0 hot sectors — non-trading day detected, trying cached rankings...")
    cache_payload = load_rankings_cache_full()
    if cache_payload:
        cached_at_str = cache_payload.get("cached_at", "")
        try:
            cached_at = datetime.fromisoformat(cached_at_str)
            cache_date_str = cached_at.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            cache_date_str = today_str

        cached_rankings = cache_payload.get("rankings", {})
        cached_sectors = cached_rankings.get("sectors", [])
        cache_active = sum(
            1 for s in cached_sectors
            if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
        )
        if cache_active == 0:
            print(f"  ⚠️ Cached data from {cache_date_str} has 0 active sectors — skipping")
        else:
            cached_hot = cache_payload.get("hot_sectors")
            if cached_hot:
                print(f"  ✓ Cache hit — {len(cached_hot)} sectors from {cache_date_str} "
                      f"(active sectors: {cache_active})")
                data_date = cache_date_str if cache_date_str != today_str else today_str
                return cached_hot, data_date, "cache"
            print(f"  ⚠️ Cached data from {cache_date_str} has no hot_sectors")

    # ── Tier 3: last-trading-day fallback ──
    last_date, last_source = get_last_trading_day()
    if last_date:
        print(f"  Last trading day: {last_date} (source: {last_source})")

        # 3a: Try snapshot history first
        history = load_snapshot_history(days=10)
        if history:
            sectors_from_hist = _top_sectors_from_history(history, top_n=top_n)
            if sectors_from_hist:
                print(f"  ✓ Extracted {len(sectors_from_hist)} sectors from snapshot history")
                return sectors_from_hist, last_date, "cache"

        # 3b: rank by change_pct directly from fresh API data
        # On non-trading days, API still returns last close's change_pct
        # but up/down counts are 0 — rank_hot_scores rejects those.
        # Fall back to simple change_pct ranking which works fine.
        raw_sectors = rankings.get("sectors", [])
        candidates = [s for s in raw_sectors
                      if s.get("change_pct") is not None]
        candidates.sort(key=lambda x: x.get("change_pct", 0) or 0, reverse=True)
        # Filter out concept-level tiny sectors, prefer industry
        filtered = []
        seen = set()
        for s in candidates:
            base = s.get("code", "")
            if base not in seen:
                seen.add(base)
                s["hot_score"] = max(0, min(100, 50 + (s["change_pct"] or 0) * 10))
                filtered.append(s)
                if len(filtered) >= top_n:
                    break

        if filtered:
            print(f"  ✓ Ranked {len(filtered)} sectors by change_pct from fresh API data")
            return filtered, last_date, "cache"
        else:
            print(f"  ⚠️ No sectors with change_pct in API data")
    else:
        print(f"  ⚠️ Cannot determine last trading day — fall through")

    # Nothing available at all
    print(f"  ⚠️ No cached data or change_pct from API — returning empty")
    return [], today_str, "cache"


def _top_sectors_from_history(history: dict[str, list[dict]],
                               top_n: int = 15) -> list[dict]:
    """Extract top N sectors from snapshot history by on-list frequency.

    For non-trading days when no realtime/cache data exists but snapshot
    history is available.  Returns sectors with synthetic hot_score based
    on frequency and recency weighting.

    Args:
        history: snapshot history dict date -> [sector_summary, ...].
        top_n: number of sectors to return.

    Returns:
        list of {code, name, hot_score, change_pct, ...} sorted by
        composite frequency score descending.
    """
    # Count appearances and avg rank per sector
    freq: dict[str, dict] = {}
    dates = sorted(history.keys())
    n_dates = len(dates)

    for i, date_str in enumerate(dates):
        recency = (i + 1) / n_dates  # 0→1, recent dates weighted higher
        for entry in history[date_str]:
            code = entry.get("code", "")
            if not code:
                continue
            if code not in freq:
                freq[code] = {
                    "code": code,
                    "name": entry.get("name", ""),
                    "count": 0,
                    "total_hot": 0.0,
                    "best_rank": 999,
                    "weighted_score": 0.0,
                }
            f = freq[code]
            f["count"] += 1
            f["total_hot"] += entry.get("hot_score", 0) or 0
            f["best_rank"] = min(f["best_rank"], entry.get("rank", 999))
            # Recency-weighted hot score
            f["weighted_score"] += (entry.get("hot_score", 0) or 0) * recency

    if not freq:
        return []

    # Composite: frequency ratio * 40 + weighted avg hot * 40 + rank bonus * 20
    scored = []
    for f in freq.values():
        freq_ratio = f["count"] / n_dates
        avg_hot = f["total_hot"] / f["count"] if f["count"] else 0
        rank_bonus = max(0, min(20, (30 - f["best_rank"]) * 0.7))
        composite = freq_ratio * 40 + avg_hot * 0.40 + rank_bonus
        scored.append({
            "code": f["code"],
            "name": f["name"],
            "hot_score": round(composite, 1),
            "change_pct": None,
            "up_count": 0,
            "down_count": 0,
            "total_count": 0,
        })

    scored.sort(key=lambda x: x["hot_score"], reverse=True)
    return scored[:top_n]


# ──────────────────────── Phase 2: Load Snapshot History ────────────


def get_snapshot_history(lookback_days: int) -> dict[str, list[dict]]:
    """Load snapshot history for persistence analysis.

    Returns dict date -> list of sector summaries sorted by rank.
    """
    print(f"[Phase 2/3] Loading snapshot history ({lookback_days} days)...")
    history = load_snapshot_history(days=lookback_days)
    if history:
        print(f"  Loaded {len(history)} snapshot days ({sum(len(v) for v in history.values())} records)")
    else:
        print(f"  ⚠️ No snapshot history — all persistence scores will be 0")
    return history


# ──────────────────────── Phase 3: Persistence from Snapshots ───────


def _sector_snapshots(sector_code: str,
                       history: dict[str, list[dict]]) -> list[dict]:
    """Extract this sector's snapshot entries across all dates, sorted."""
    entries = []
    for date_str in sorted(history.keys()):
        for entry in history[date_str]:
            if entry.get("code") == sector_code:
                e = dict(entry)
                e["date"] = date_str
                entries.append(e)
                break
    return entries


def compute_persistence(sector: dict, snapshots: list[dict],
                        lookback_days: int, history: dict):
    """Compute persistence score from snapshot history.

    Score components (0-100 each, weighted):
      - on_list_rate (30%): days sector appears in top N / total snapshot days
      - avg_hot (20%): mean hot_score over all snapshot entries
      - rank_trend (20%): rank improvement, recent half vs early half
      - today_hot (15%): today's hot_score (from realtime or cache)
      - up_ratio_trend (15%): mean up_ratio over snapshot entries

    Args:
        sector: current sector dict with hot_score/change_pct/up_count/down_count.
        snapshots: this sector's snapshot entries across dates.
        lookback_days: analysis window.
        history: full snapshot history dict date->list (for total_days).

    Returns:
        Dict with persistence metrics, or None if sector has no data at all.
    """
    # Apply lookback window truncation
    if lookback_days and len(snapshots) > lookback_days:
        snapshots = snapshots[-lookback_days:]

    if not snapshots:
        return None

    total_days = len(history) if history else 1
    n = len(snapshots)
    hot_score = sector.get("hot_score", 0) or 0
    today_change = sector.get("change_pct")

    # Component 1: on_list_rate (30%) — how often sector appears in top N
    on_list_rate = n / total_days if total_days > 0 else 0

    # Component 2: avg_hot (20%)
    avg_hot = mean(s["hot_score"] for s in snapshots) if snapshots else 0

    # Component 3: rank_trend (20%) — recent half vs early half improvement
    rank_trend = 0  # 0 = no trend, positive = improving
    if len(snapshots) >= 4:
        half = len(snapshots) // 2
        early_avg = mean(s["rank"] for s in snapshots[:half])
        recent_avg = mean(s["rank"] for s in snapshots[-half:])
        # Negative delta means rank number decreasing = improving
        delta = early_avg - recent_avg
        # Normalize: delta > 3 ranks improvement → 100, no change → 50, worsening → 0
        rank_trend = max(0, min(100, 50 + delta * 15))
    elif snapshots:
        rank_trend = 50  # neutral with sparse data

    # Component 4: today_hot (15%)
    today_hot = hot_score

    # Component 5: up_ratio_trend (15%)
    up_vals = [s.get("up_ratio", 0.5) for s in snapshots]
    avg_up_ratio = mean(up_vals) if up_vals else 0.5

    # Composite score (0-100)
    composite = (
        (on_list_rate * 100) * 0.30
        + (avg_hot) * 0.20
        + rank_trend * 0.20
        + today_hot * 0.15
        + (avg_up_ratio * 100) * 0.15
    )

    # Trend direction label
    if rank_trend > 65:
        trend_label = "↑ (加速)"
    elif rank_trend > 50:
        trend_label = "↗ (延续)"
    elif on_list_rate > 0.3 and avg_hot > 40:
        trend_label = "→ (走平)"
    else:
        trend_label = "↘ (减弱)"

    # Derived metrics for report compatibility
    # momentum_5d = avg hot score improvement over last 3 vs prior (proxy)
    mom5 = avg_hot - (mean(s["hot_score"] for s in snapshots[-3:]) if len(snapshots) >= 3 else avg_hot * 0.9)
    mom10 = avg_hot
    up_days = sum(1 for s in snapshots if s.get("up_ratio", 0) > 0.5)

    return {
        "code": sector["code"],
        "name": sector.get("name", ""),
        "hot_score": round(hot_score, 1),
        "persistence": round(composite, 1),
        "momentum_5d": round(mom5, 2),
        "momentum_10d": round(mom10, 2),
        "up_days": up_days,
        "up_days_ratio": round(avg_up_ratio, 2),
        "trend_label": trend_label,
        "today_change": today_change,
        "stocks_count": sector.get("total_count", 0),
    }


def classify_themes(results: list[dict]) -> dict:
    """Classify sectors into theme categories.

    Categories:
      - strong: persistence >= 70 (main line)
      - moderate: persistence >= 50 (candidate)
      - emerging: persistence < 50 but >= 40 (newly forming)
      - fading: persistence < 40 (in decline)

    Also flags sectors that spike today but lack persistence (one-day wonder).
    """
    strong = [r for r in results if r["persistence"] >= 70]
    moderate = [r for r in results if 50 <= r["persistence"] < 70]
    emerging = [r for r in results if 40 <= r["persistence"] < 50]
    fading = [r for r in results if r["persistence"] < 40]

    # One-day wonder: hot today but low persistence
    one_day = []
    if results:
        max_hot = max(r["hot_score"] for r in results)
        hot_threshold = max(60, max_hot * 0.6)
        for r in results:
            if r["hot_score"] >= hot_threshold and r["persistence"] < 50:
                one_day.append(r)

    return {
        "strong": strong,
        "moderate": moderate,
        "emerging": emerging,
        "fading": fading,
        "one_day_wonders": one_day,
    }


# ──────────────────────── Report ────────────────────────


def _fmt_direction(val: float) -> str:
    """Format a momentum-like value for display (positive with +)."""
    if val is None or val == 0:
        return "-"
    return f"{val:+.1f}"


def generate_report(classified: dict, meta: dict, lookback_days: int) -> str:
    """Generate Markdown report."""
    scan_time = meta.get("scan_time", "")
    data_date = meta.get("data_date", "")
    data_source = meta.get("data_source", "realtime")
    snapshot_days = meta.get("snapshot_days", 0)
    lines = []

    lines.append(f"## 市场主线分析报告  {scan_time}")
    lines.append(f"")
    lines.append(f"▸ 分析周期: 最近 {lookback_days} 个数据点（{snapshot_days} 天快照历史）")
    lines.append(f"▸ 持续性阈值: 强≥70 | 中≥50 | 弱<50")

    # Data date annotation
    today_str = datetime.now().strftime("%Y-%m-%d")
    is_non_trading = data_date and data_date != today_str
    if is_non_trading:
        lines.append(f"▸ 数据日期: {data_date}（最近交易日）")
    if snapshot_days == 0:
        lines.append(f"▸ ⚠️ 无历史快照 — 下一个交易日运行 /market-theme 建立快照后可算持续性")
    elif is_non_trading:
        lines.append(f"▸ 当前非交易日，基于快照历史分析")
    lines.append(f"")

    strong = classified["strong"]
    moderate = classified["moderate"]
    emerging = classified["emerging"]
    fading = classified["fading"]

    # Strong main lines
    if strong:
        lines.append(f"### 阶段强势（主线确认）")
        lines.append(f"")
        lines.append(f"| 板块 | 今日热度 | 热度趋势 | 上榜率 | 持续性分 | 趋势 |")
        lines.append(f"|------|---------|---------|-------|---------|------|")
        for r in strong:
            lines.append(
                f"| {r['name']} | {r['hot_score']:.0f} | "
                f"{_fmt_direction(r['momentum_5d'])} | "
                f"{r['up_days_ratio']*100:.0f}% | "
                f"**{r['persistence']:.1f}** | {r['trend_label']} |"
            )
        lines.append(f"")

    # Moderate candidates
    if moderate:
        lines.append(f"### 稳步上行（候选主线）")
        lines.append(f"")
        lines.append(f"| 板块 | 今日热度 | 热度趋势 | 上榜率 | 持续性分 | 趋势 |")
        lines.append(f"|------|---------|---------|-------|---------|------|")
        for r in moderate:
            lines.append(
                f"| {r['name']} | {r['hot_score']:.0f} | "
                f"{_fmt_direction(r['momentum_5d'])} | "
                f"{r['up_days_ratio']*100:.0f}% | "
                f"{r['persistence']:.1f} | {r['trend_label']} |"
            )
        lines.append(f"")

    # Emerging
    if emerging:
        lines.append(f"### 新兴主题")
        lines.append(f"")
        for r in emerging:
            lines.append(
                f"- {r['name']} — 持续性分 {r['persistence']:.1f}, "
                f"上榜率 {r['up_days_ratio']*100:.0f}%, "
                f"趋势 {r['trend_label']}"
            )
        lines.append(f"")

    # One-day wonders
    if classified.get("one_day_wonders"):
        lines.append(f"### ⚠️ 脉冲热点（单日热度高但持续性不足）")
        lines.append(f"")
        for r in classified["one_day_wonders"]:
            lines.append(
                f"- {r['name']} — 今日热度 {r['hot_score']:.0f}, "
                f"持续性分 {r['persistence']:.1f}, "
                f"上榜率 {r['up_days_ratio']*100:.0f}%"
            )
        lines.append(f"")

    # Fading
    if fading:
        lines.append(f"### 退潮板块")
        lines.append(f"")
        for r in fading[:5]:
            lines.append(
                f"- {r['name']} — 持续性分 {r['persistence']:.1f}, "
                f"上榜率 {r['up_days_ratio']*100:.0f}%, "
                f"趋势 {r['trend_label']}"
            )
        lines.append(f"")

    # Summary
    lines.append(f"---")
    lines.append(f"**主线概况**: {len(strong)} 条确认主线, {len(moderate)} 条候选, "
                 f"{len(emerging)} 个新兴, {len(fading)} 个退潮")
    lines.append(f"")
    lines.append(f"> *本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。*")
    lines.append(f"> *报告时间: {scan_time}*")

    return "\n".join(lines)


# ──────────────────────── HTML Report ────────────────────────


def _css_cls(score: float) -> str:
    if score >= 70:
        return "strong"
    if score >= 50:
        return "moderate"
    if score >= 40:
        return "emerging"
    return "fading"


def _pct_cls(val: float) -> str:
    if val is None:
        return "neut"
    return "sp" if val > 0 else "sn" if val < 0 else "neut"


# ── Column renderers for HTML table cells ──
_COL_RENDERERS = {
    "name":         lambda r: f"<td>{r['name']}</td>",
    "today_change": lambda r: f'<td class="{_pct_cls(r.get("today_change"))}">{r["today_change"]:+.1f}%</td>',
    "momentum_5d":  lambda r: f"<td>{r['momentum_5d']:+.1f}%</td>",
    "momentum_10d": lambda r: f"<td>{r['momentum_10d']:+.1f}%</td>",
    "up_days_ratio":lambda r: f"<td>{r['up_days_ratio']*100:.0f}%</td>",
    "persistence":  lambda r: f'<td class="ml-{_css_cls(r["persistence"])}">{r["persistence"]:.1f}</td>',
    "trend_label":  lambda r: f"<td>{r['trend_label']}</td>",
    "hot_score":    lambda r: f"<td>{r['hot_score']:.0f}</td>",
}


def _html_rows(items: list[dict], col_keys: list[str], *, show_idx: bool = False) -> str:
    """Build HTML table rows from items given ordered column keys."""
    rows = ""
    for i, r in enumerate(items, 1):
        cells = f"<td>{i}</td>" if show_idx else ""
        cells += "".join(_COL_RENDERERS[k](r) for k in col_keys)
        rows += f"<tr>{cells}</tr>"
    return rows


def _sec_table(title: str, desc: str, css_cls: str, items: list[dict],
               col_keys: list[str], thead_html: str) -> str:
    if not items:
        return ""
    return f"""<div class="sec sec-{css_cls}">
    <h2>{title}</h2>
    <p class="sec-desc">{desc}</p>
    <table>
        <thead>{thead_html}</thead>
        <tbody>{_html_rows(items, col_keys)}</tbody>
    </table>
    </div>"""


def _sec_list(title: str, desc: str, css_cls: str, items: list[dict],
              item_fn) -> str:
    if not items:
        return ""
    items_html = "".join(item_fn(r) for r in items)
    return f"""<div class="sec sec-{css_cls}">
    <h2>{title}</h2>
    <p class="sec-desc">{desc}</p>
    <ul>{items_html}</ul>
    </div>"""


def _generate_html_report(classified: dict, meta: dict, results: list[dict],
                          lookback_days: int) -> str:
    """Generate HTML report matching stock-trend report-template style."""
    scan_time = meta.get("scan_time", "")
    data_date = meta.get("data_date", "")
    snapshot_days = meta.get("snapshot_days", 0)
    strong = classified["strong"]
    moderate = classified["moderate"]
    emerging = classified["emerging"]
    fading = classified["fading"]
    one_day = classified.get("one_day_wonders", [])

    # Build data-date annotation
    today_str_html = datetime.now().strftime("%Y-%m-%d")
    date_note = ""
    is_non_trading = data_date and data_date != today_str_html
    if is_non_trading:
        date_note = f" | 数据日期: {data_date}（最近交易日）"
    if snapshot_days == 0:
        date_note += " | ⚠️ 无历史快照"
    elif is_non_trading:
        date_note += " | 非交易日，基于快照分析"

    rank_cols = ["name", "hot_score", "persistence", "trend_label"]
    rank_rows = _html_rows(results, rank_cols, show_idx=True)
    rank_table = f"""<div class="rank">
    <h2>板块持续性排名</h2>
    <table>
        <thead><tr><th>#</th><th>板块</th><th>今日热度</th><th>持续性</th><th>趋势</th></tr></thead>
        <tbody>{rank_rows}</tbody>
    </table>
    </div>"""

    sections_html = (
        _sec_table("阶段强势（主线确认）", "持续性分 ≥ 70，趋势明确且持续", "strong", strong,
                   ["name", "hot_score", "persistence", "trend_label"],
                   "<tr><th>板块</th><th>今日热度</th><th>持续性</th><th>趋势</th></tr>")
        + _sec_table("稳步上行（候选主线）", "持续性分 50-70，具备主线潜力", "moderate", moderate,
                     ["name", "hot_score", "persistence", "trend_label"],
                     "<tr><th>板块</th><th>今日热度</th><th>持续性</th><th>趋势</th></tr>")
        + _sec_list("新兴主题", "持续性分 40-50，新冒头方向，需验证", "emerging", emerging,
                    lambda r: f"<li><strong>{r['name']}</strong> — 持续性 {r['persistence']:.1f}, 今日热度 {r['hot_score']:.0f}, {r['trend_label']}</li>")
        + _sec_list("⚠️ 脉冲热点", "今日热度高但持续性不足 50，警惕追高", "warn", one_day,
                    lambda r: f"<li><strong>{r['name']}</strong> — 今日热度 {r['hot_score']:.0f}, 持续性 {r['persistence']:.1f}</li>")
        + _sec_list("退潮板块", "持续性分 < 40，趋势走弱", "fading", fading[:5],
                    lambda r: f"<li><strong>{r['name']}</strong> — 持续性 {r['persistence']:.1f}, {r['trend_label']}</li>")
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场主线分析报告 {scan_time}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6;padding:20px}}
.w{{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
header{{border-bottom:2px solid #f0f0f0;padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:24px;color:#1a1a1a;margin-bottom:4px}}
.dt{{color:#86868b;font-size:14px;margin-bottom:8px}}
.meta{{color:#86868b;font-size:14px}}
h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #f0f0f0;color:#1d4ed8}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
tr:nth-child(even) td{{background:#f9fafb}}
.sp{{color:#dc2626;font-weight:600}}
.sn{{color:#16a34a;font-weight:600}}
.neut{{color:#6b7280}}
.ml-strong{{color:#16a34a;font-weight:700;font-size:15px}}
.ml-moderate{{color:#1d4ed8;font-weight:600}}
.ml-emerging{{color:#d97706;font-weight:600}}
.ml-fading{{color:#9ca3af}}
.rank,.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.rank h2,.sec h2{{margin-top:0}}
.sec-desc{{color:#86868b;font-size:13px;margin-bottom:12px}}
.sec-strong{{border-left:4px solid #16a34a}}
.sec-moderate{{border-left:4px solid #1d4ed8}}
.sec-emerging{{border-left:4px solid #d97706}}
.sec-warn{{border-left:4px solid #dc2626;background:#fef2f2}}
.sec-fading{{border-left:4px solid #d1d5db}}
.sec ul{{list-style:none;padding:0}}
.sec ul li{{padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px}}
.sec ul li:last-child{{border-bottom:none}}
.summary{{background:#f3f4f6;border-radius:8px;padding:16px 20px;margin:20px 0;font-size:14px;line-height:1.8;text-align:center;color:#374151}}
.summary strong{{color:#1d4ed8}}
footer{{margin-top:32px;padding-top:16px;border-top:1px solid #f0f0f0}}
.disc{{color:#a1a1a6;font-size:12px;font-style:italic;text-align:center}}
</style>
</head>
<body>
<div class="w">

<header>
<h1>市场主线分析报告</h1>
<p class="dt">分析时间: {scan_time} | 耗时: {meta.get('elapsed_seconds','?')}s</p>
<p class="meta">扫描板块: {meta.get('total_sectors',0)} 个 | 分析周期: 最近 {lookback_days} 个数据点{date_note}</p>
</header>

{rank_table}

{sections_html}

<div class="summary">
<strong>主线概况:</strong> {len(strong)} 条确认主线 &nbsp;|&nbsp; {len(moderate)} 条候选 &nbsp;|&nbsp; {len(emerging)} 个新兴 &nbsp;|&nbsp; {len(fading)} 个退潮
</div>

<footer>
<p class="disc">本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。</p>
</footer>
</div>
</body>
</html>"""


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(description="市场主线分析 (/market-theme)")
    parser.add_argument("--top", type=int, default=15, help="扫描板块数量, 默认15")
    parser.add_argument("--days", type=int, default=10, help="快照回溯天数, 默认10")
    parser.add_argument("--min-score", type=float, default=0, help="最低持续性分(过滤噪声), 默认0")
    parser.add_argument("--output-html", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--no-html", action="store_true", help="跳过HTML报告生成")
    args = parser.parse_args()

    start = time.time()

    # Phase 1: Get today's hot sectors (realtime / cache fallback)
    sectors, data_date, data_source = get_top_sectors(top_n=args.top)

    # Phase 2: Load snapshot history
    history = get_snapshot_history(lookback_days=args.days)

    # Phase 3: Compute persistence from snapshots
    print(f"[Phase 3/3] Computing persistence scores...")
    results = []
    for s in sectors:
        snapshots = _sector_snapshots(s.get("code", ""), history)
        score = compute_persistence(s, snapshots, args.days, history)
        if score and score["persistence"] >= args.min_score:
            results.append(score)

    results.sort(key=lambda x: x["persistence"], reverse=True)
    print(f"  Analyzed {len(results)} sectors above min-score={args.min_score}")

    # Classify themes
    classified = classify_themes(results)

    elapsed = time.time() - start
    snapshot_days = len(history)
    meta = {
        "scan_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "elapsed_seconds": round(elapsed, 1),
        "lookback_days": args.days,
        "snapshot_days": snapshot_days,
        "total_sectors": len(sectors),
        "data_date": data_date,
        "data_source": data_source,
        "strong": len(classified["strong"]),
        "moderate": len(classified["moderate"]),
        "emerging": len(classified["emerging"]),
        "fading": len(classified["fading"]),
    }

    # Generate Markdown report
    report = generate_report(classified, meta, args.days)
    print(report)

    # HTML report (default on, skip with --no-html)
    if not args.no_html:
        html = _generate_html_report(classified, meta, results, args.days)
        ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = REPORTS_LISTS_DIR / f"market-theme-{ts_str}.html"
        REPORTS_LISTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML report: {html_path}")

    # JSON output for agent consumption
    output = {
        "meta": meta,
        "strong": classified["strong"],
        "moderate": classified["moderate"],
        "emerging": classified["emerging"],
        "fading": classified["fading"],
        "one_day_wonders": classified["one_day_wonders"],
    }
    print(f"\n<!--JSON_OUTPUT-->\n{json.dumps(output, ensure_ascii=False, indent=2)}\n<!--END_JSON_OUTPUT-->")

    print(f"\nMarket theme analysis complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
