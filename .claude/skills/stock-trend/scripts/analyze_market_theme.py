#!/usr/bin/env python3
"""Market main line (市场主线) analyzer.

Identifies persistent market themes by analyzing sector index K-lines
over the past N trading days. Goes beyond single-day hot sector scanning
to find sustained trends.

Three-phase architecture:
  Phase 1: Get today's sector rankings
  Phase 2: Fetch BK index K-lines for past N days
  Phase 3: Compute persistence scores + classify themes

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from statistics import stdev, mean

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
REPORTS_LISTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))
from fetch_sector_data import get_sector_rankings, rank_hot_sectors
from fetch_sector_kline import batch_fetch_kline


# ──────────────────────── Phase 1: Sector Scan ────────────────────────


def get_top_sectors(top_n: int = 15) -> list[dict]:
    """Phase 1: Get today's top hot sectors.

    Returns:
        List of sector dicts with code, name, hot_score, change_pct, etc.
    """
    print(f"[Phase 1/3] Scanning top {top_n} sectors...")
    rankings = get_sector_rankings()
    hot = rank_hot_sectors(rankings, top_n=top_n, min_stocks=8, min_up_ratio=0.15)
    print(f"  Got {len(hot)} hot sectors from {rankings['meta']['total_sectors']} total")
    return hot


# ──────────────────────── Phase 2: Fetch K-lines ────────────────────────


def fetch_kline_for_sectors(sectors: list[dict], max_workers: int = 4) -> dict[str, list[dict]]:
    """Phase 2: Fetch BK index K-lines for all candidate sectors.

    Requests max available records per sector (API returns ~28 sparse
    records across ~3mo). Enough for 10-15 point trend analysis.

    Args:
        sectors: list of sector dicts with 'code' key.
        max_workers: parallel fetch concurrency.

    Returns:
        Dict mapping sector_code -> list of daily K-line records.
    """
    codes = [s["code"] for s in sectors if s.get("code")]
    print(f"[Phase 2/3] Fetching BK K-lines for {len(codes)} sectors...")
    klines = batch_fetch_kline(codes, min_records=20, max_workers=max_workers)
    success = sum(1 for v in klines.values() if v)
    got = [len(v) for v in klines.values() if v]
    avg = sum(got) / len(got) if got else 0
    print(f"  Got data: {success}/{len(codes)} sectors (avg {avg:.0f} records each)")
    return klines


# ──────────────────────── Score Computation ────────────────────────


def _compute_momentum(records: list[dict], n_days: int) -> float:
    """Sum of pct_chg over last N days."""
    relevant = records[-n_days:] if len(records) >= n_days else records
    return sum(r.get("pct_chg", 0) for r in relevant)


def _up_days_ratio(records: list[dict]) -> float:
    """Fraction of up days (pct_chg > 0) in the period."""
    if not records:
        return 0.0
    up = sum(1 for r in records if (r.get("pct_chg") or 0) > 0)
    return up / len(records)


def _compute_volatility(records: list[dict]) -> float:
    """Std dev of daily returns."""
    if len(records) < 3:
        return 999.0
    returns = [r.get("pct_chg", 0) for r in records]
    return stdev(returns)


def _compute_acceleration(records: list[dict]) -> float:
    """Recent momentum vs prior: 3d avg vs prior 7d avg.

    Positive = accelerating, negative = decelerating.
    """
    if len(records) < 10:
        return 0.0
    recent = mean(r.get("pct_chg", 0) for r in records[-3:])
    prior = mean(r.get("pct_chg", 0) for r in records[-10:-3])
    return recent - prior


def compute_persistence(sector: dict, kline: list[dict], lookback_days: int = 10) -> Optional[dict]:
    """Compute persistence score for one sector.

    Score components:
      - momentum_5d (30%): 5-day cumulative return
      - up_days_ratio (25%): consistency of up days
      - momentum_10d (20%): 10-day cumulative return
      - acceleration (15%): recent trend acceleration
      - stability (10%): inverse of volatility

    Args:
        sector: sector dict with hot_score.
        kline: list of daily K-line records, sorted ascending.
        lookback_days: analysis window length.

    Returns:
        Dict with persistence metrics, or None if insufficient data.
    """
    if not kline or len(kline) < 3:
        return None

    # Truncate to analysis window so all metrics use same period
    window = kline[-lookback_days:] if len(kline) >= lookback_days else kline

    mom5 = _compute_momentum(window, 5)
    mom10 = _compute_momentum(window, 10) if len(window) >= 10 else mom5
    up_ratio = _up_days_ratio(window)
    vol = _compute_volatility(window)
    accel = _compute_acceleration(window) if len(window) >= 10 else 0.0
    hot_score = sector.get("hot_score", 0)

    # Normalize to 0-100 per component
    s_mom5 = max(0, min(100, mom5 * 6))           # e.g. 5%→30, 10%→60, 16.7%→100
    s_mom10 = max(0, min(100, mom10 * 3))          # e.g. 10%→30, 20%→60, 33%→100
    s_up = up_ratio * 100                           # 0-100
    s_accel = max(0, min(100, 50 + accel * 20))    # accel around 0 → 50, accel > 2.5 → 100
    s_stable = max(0, min(100, 100 - vol * 15))    # 0% vol→100, >6.7% vol→0

    composite = (
        s_mom5 * 0.30
        + s_up * 0.25
        + s_mom10 * 0.20
        + s_accel * 0.15
        + s_stable * 0.10
    )

    # Trend direction label
    if accel > 0.3:
        trend_label = "↑ (加速)"
    elif mom5 > 1:
        trend_label = "↗ (延续)"
    elif mom5 > -1:
        trend_label = "→ (走平)"
    else:
        trend_label = "↘ (减弱)"

    return {
        "code": sector["code"],
        "name": sector["name"],
        "hot_score": round(hot_score, 1),
        "persistence": round(composite, 1),
        "momentum_5d": round(mom5, 2),
        "momentum_10d": round(mom10, 2),
        "up_days_ratio": round(up_ratio, 2),
        "volatility": round(vol, 2),
        "acceleration": round(accel, 2),
        "trend_label": trend_label,
        "today_change": sector.get("change_pct"),
        "stocks_count": sector.get("total_count", 0),
    }


# ──────────────────────── Phase 3: Analysis ────────────────────────


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
    # Uses both relative (top 40% of hot scores) and absolute (≥ 60) thresholds
    # to avoid false flags in weak markets or single-outlier scenarios
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


def generate_report(classified: dict, meta: dict, lookback_days: int) -> str:
    """Generate Markdown report."""
    scan_time = meta.get("scan_time", "")
    lines = []

    lines.append(f"## 市场主线分析报告  {scan_time}")
    lines.append(f"")
    lines.append(f"▸ 分析周期: 最近 {lookback_days} 个交易日")
    lines.append(f"▸ 持续性阈值: 强≥70 | 中≥50 | 弱<50")
    lines.append(f"")

    strong = classified["strong"]
    moderate = classified["moderate"]
    emerging = classified["emerging"]
    fading = classified["fading"]

    # Strong main lines
    if strong:
        lines.append(f"### 阶段强势（主线确认）")
        lines.append(f"")
        lines.append(f"| 板块 | 今日热度 | 5日涨幅 | 10日涨幅 | 上涨天数 | 持续性分 | 趋势 |")
        lines.append(f"|------|---------|---------|----------|---------|---------|------|")
        for r in strong:
            up_str = f"{r['up_days_ratio']*10:.0f}/{lookback_days}"
            lines.append(
                f"| {r['name']} | {r['hot_score']:.0f} | "
                f"{r['momentum_5d']:+.1f}% | {r['momentum_10d']:+.1f}% | "
                f"{up_str} | **{r['persistence']:.1f}** | {r['trend_label']} |"
            )
        lines.append(f"")

    # Moderate candidates
    if moderate:
        lines.append(f"### 稳步上行（候选主线）")
        lines.append(f"")
        lines.append(f"| 板块 | 今日热度 | 5日涨幅 | 上涨天数 | 持续性分 | 趋势 |")
        lines.append(f"|------|---------|---------|---------|---------|------|")
        for r in moderate:
            up_str = f"{r['up_days_ratio']*10:.0f}/{lookback_days}"
            lines.append(
                f"| {r['name']} | {r['hot_score']:.0f} | "
                f"{r['momentum_5d']:+.1f}% | "
                f"{up_str} | {r['persistence']:.1f} | {r['trend_label']} |"
            )
        lines.append(f"")

    # Emerging
    if emerging:
        lines.append(f"### 新兴主题")
        lines.append(f"")
        for r in emerging:
            lines.append(
                f"- {r['name']} — 持续性分 {r['persistence']:.1f}, "
                f"5日涨幅 {r['momentum_5d']:+.1f}%, "
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
                f"5日涨幅 {r['momentum_5d']:+.1f}%"
            )
        lines.append(f"")

    # Fading
    if fading:
        lines.append(f"### 退潮板块")
        lines.append(f"")
        for r in fading[:5]:
            lines.append(
                f"- {r['name']} — 持续性分 {r['persistence']:.1f}, "
                f"10日涨幅 {r['momentum_10d']:+.1f}%, "
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


def _generate_html_report(classified: dict, meta: dict, results: list[dict],
                          lookback_days: int) -> str:
    """Generate HTML report matching stock-trend report-template style."""
    scan_time = meta.get("scan_time", "")
    strong = classified["strong"]
    moderate = classified["moderate"]
    emerging = classified["emerging"]
    fading = classified["fading"]
    one_day = classified.get("one_day_wonders", [])

    # Ranking table
    rank_rows = ""
    for i, r in enumerate(results, 1):
        pc = _pct_cls(r.get("today_change"))
        rank_rows += (
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{r['name']}</td>"
            f"<td class=\"{pc}\">{r['today_change']:+.1f}%</td>"
            f"<td>{r['momentum_5d']:+.1f}%</td>"
            f"<td>{r['momentum_10d']:+.1f}%</td>"
            f"<td>{r['up_days_ratio']*100:.0f}%</td>"
            f"<td class=\"ml-{_css_cls(r['persistence'])}\">{r['persistence']:.1f}</td>"
            f"<td>{r['trend_label']}</td>"
            f"</tr>"
        )
    rank_table = f"""<div class="rank">
    <h2>板块持续性排名</h2>
    <table>
        <thead><tr><th>#</th><th>板块</th><th>今日涨幅</th><th>5日涨幅</th><th>10日涨幅</th><th>上涨比</th><th>持续性</th><th>趋势</th></tr></thead>
        <tbody>{rank_rows}</tbody>
    </table>
    </div>"""

    # Sections: strong, moderate, emerging, one-day, fading
    sections_html = ""

    if strong:
        rows = ""
        for r in strong:
            pc = _pct_cls(r.get("today_change"))
            rows += (
                f"<tr>"
                f"<td>{r['name']}</td>"
                f"<td class=\"{pc}\">{r['today_change']:+.1f}%</td>"
                f"<td>{r['momentum_5d']:+.1f}%</td>"
                f"<td>{r['momentum_10d']:+.1f}%</td>"
                f"<td>{r['up_days_ratio']*100:.0f}%</td>"
                f"<td class=\"ml-strong\">{r['persistence']:.1f}</td>"
                f"<td>{r['trend_label']}</td>"
                f"</tr>"
            )
        sections_html += f"""<div class="sec sec-strong">
        <h2>阶段强势（主线确认）</h2>
        <p class="sec-desc">持续性分 ≥ 70，趋势明确且持续</p>
        <table>
            <thead><tr><th>板块</th><th>今日涨幅</th><th>5日涨幅</th><th>10日涨幅</th><th>上涨比</th><th>持续性</th><th>趋势</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        </div>"""

    if moderate:
        rows = ""
        for r in moderate:
            pc = _pct_cls(r.get("today_change"))
            rows += (
                f"<tr>"
                f"<td>{r['name']}</td>"
                f"<td class=\"{pc}\">{r['today_change']:+.1f}%</td>"
                f"<td>{r['momentum_5d']:+.1f}%</td>"
                f"<td>{r['up_days_ratio']*100:.0f}%</td>"
                f"<td class=\"ml-moderate\">{r['persistence']:.1f}</td>"
                f"<td>{r['trend_label']}</td>"
                f"</tr>"
            )
        sections_html += f"""<div class="sec sec-moderate">
        <h2>稳步上行（候选主线）</h2>
        <p class="sec-desc">持续性分 50-70，具备主线潜力</p>
        <table>
            <thead><tr><th>板块</th><th>今日涨幅</th><th>5日涨幅</th><th>上涨比</th><th>持续性</th><th>趋势</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        </div>"""

    if emerging:
        items = ""
        for r in emerging:
            items += (
                f"<li><strong>{r['name']}</strong> — "
                f"持续性 {r['persistence']:.1f}, "
                f"5日涨幅 {r['momentum_5d']:+.1f}%, "
                f"{r['trend_label']}</li>"
            )
        sections_html += f"""<div class="sec sec-emerging">
        <h2>新兴主题</h2>
        <p class="sec-desc">持续性分 40-50，新冒头方向，需验证</p>
        <ul>{items}</ul>
        </div>"""

    if one_day:
        items = ""
        for r in one_day:
            items += (
                f"<li><strong>{r['name']}</strong> — "
                f"今日热度 {r['hot_score']:.0f}, "
                f"持续性 {r['persistence']:.1f}, "
                f"5日涨幅 {r['momentum_5d']:+.1f}%</li>"
            )
        sections_html += f"""<div class="sec sec-warn">
        <h2>⚠️ 脉冲热点</h2>
        <p class="sec-desc">今日热度高但持续性不足 50，警惕追高</p>
        <ul>{items}</ul>
        </div>"""

    if fading:
        items = ""
        for r in fading[:5]:
            items += (
                f"<li><strong>{r['name']}</strong> — "
                f"持续性 {r['persistence']:.1f}, "
                f"10日涨幅 {r['momentum_10d']:+.1f}%, "
                f"{r['trend_label']}</li>"
            )
        sections_html += f"""<div class="sec sec-fading">
        <h2>退潮板块</h2>
        <p class="sec-desc">持续性分 &lt; 40，趋势走弱</p>
        <ul>{items}</ul>
        </div>"""

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
<p class="meta">扫描板块: {meta.get('total_sectors',0)} 个 | 分析周期: 最近 {lookback_days} 个数据点</p>
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
    parser.add_argument("--days", type=int, default=10, help="K线回溯天数, 默认10")
    parser.add_argument("--min-score", type=float, default=0, help="最低持续性分(过滤噪声), 默认0")
    parser.add_argument("--no-html", action="store_true", help="跳过HTML报告生成")
    args = parser.parse_args()

    start = time.time()

    # Phase 1: Get today's hot sectors
    sectors = get_top_sectors(top_n=args.top)

    # Phase 2: Fetch K-lines
    klines = fetch_kline_for_sectors(sectors, max_workers=4)

    # Phase 3: Compute persistence scores
    print(f"[Phase 3/3] Computing persistence scores...")
    results = []
    for s in sectors:
        k = klines.get(s["code"], [])
        score = compute_persistence(s, k, lookback_days=args.days)
        if score and score["persistence"] >= args.min_score:
            results.append(score)

    results.sort(key=lambda x: x["persistence"], reverse=True)
    print(f"  Analyzed {len(results)} sectors above min-score={args.min_score}")

    # Classify themes
    classified = classify_themes(results)

    elapsed = time.time() - start
    meta = {
        "scan_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "elapsed_seconds": round(elapsed, 1),
        "lookback_days": args.days,
        "total_sectors": len(sectors),
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
