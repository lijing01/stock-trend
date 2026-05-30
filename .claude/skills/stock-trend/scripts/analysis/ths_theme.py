#!/usr/bin/env python3
"""同花顺涨停热力主题分析 (ths-theme).

从涨停复盘数据出发，按概念板块聚合计算热力评分，识别短线热点方向。
可选整合 DDX 资金流向数据做交叉验证。

评分公式（纯涨停版）：
  概念热度 = 涨停数×30% + 连板强度×25% + 早盘强度×20% + 封单强度×15% + 炸板惩罚×(-10%)

评分公式（含 DDX）：
  综合热度 = 涨停热力×70% + DDX资金力×30%

Usage:
    python3 ths_theme.py                           # 今日涨停热力报告
    python3 ths_theme.py --date 2026-05-29         # 历史日期
    python3 ths_theme.py --top 15                  # Top 15 概念
    python3 ths_theme.py --min-score 30            # 最低热力分过滤
    python3 ths_theme.py --ddx                     # 整合 DDX 资金流向
    python3 ths_theme.py --json                    # JSON 输出
    python3 ths_theme.py --no-html                 # 跳过 HTML
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))
from fetchers.zt_replay import (
    fetch_limitup_stocks,
    aggregate_by_concept,
    aggregate_by_limit_streak,
)
from fetchers.ddx import fetch_ddx_ranking

# ──────────────── 评分权重 ────────────────

WEIGHTS = {
    "stock_count": 0.30,        # 涨停家数
    "continuous": 0.25,         # 连板强度（连板股占比 × 最高连板因子）
    "morning": 0.20,            # 早盘强度（集合竞价 + 早盘涨停占比）
    "seal_strength": 0.15,      # 封单强度
    "blown_penalty": -0.10,     # 炸板惩罚
}

# ──────────────── 评分引擎 ────────────────


def compute_concept_scores(stocks: list[dict]) -> list[dict]:
    """Compute hot scores for each concept from limit-up stock list.

    Each concept gets a multi-dimensional score (0-100).
    Returns sorted list of concept dicts.
    """
    if not stocks:
        return []

    # Group stocks by concept
    concept_stocks = defaultdict(list)
    for s in stocks:
        if s["concepts"]:
            for c in s["concepts"]:
                concept_stocks[c].append(s)
        else:
            concept_stocks["其他"].append(s)

    # Compute raw metrics
    scores = []
    for concept, members in concept_stocks.items():
        total = len(members)
        continuous = [m for m in members if m["limit_streak"] >= 2]
        high_streak = max((m["limit_streak"] for m in members), default=1)
        early = [m for m in members
                 if m.get("timing_bucket") in ("pre_open", "morning_early")]
        blown = [m for m in members if m.get("limit_type") == "blown"]
        retest = [m for m in members if m.get("limit_type") == "retest"]
        seal_vals = [m["seal_amount"] for m in members if m.get("seal_amount")]

        scores.append({
            "concept": concept,
            "stock_count": total,
            "continuous_count": len(continuous),
            "continuous_ratio": round(len(continuous) / total, 3) if total else 0,
            "max_streak": high_streak,
            "morning_count": len(early),
            "morning_ratio": round(len(early) / total, 3) if total else 0,
            "blown_count": len(blown),
            "blown_ratio": round(len(blown) / total, 3) if total else 0,
            "retest_count": len(retest),
            "avg_seal": round(mean(seal_vals), 2) if seal_vals else 0,
            "members": sorted(members, key=lambda m: (-m["limit_streak"],
                                                      m.get("first_limit_time") or "")),
        })

    # Normalize each dimension to 0-100, then apply weights
    for dim in ["stock_count", "continuous_count", "morning_count", "avg_seal"]:
        vals = [s[dim] for s in scores]
        lo, hi = min(vals), max(vals)
        if hi > lo:
            for s in scores:
                s[f"{dim}_norm"] = round((s[dim] - lo) / (hi - lo) * 100, 1)
        else:
            for s in scores:
                s[f"{dim}_norm"] = 50.0

    # Composite hot score
    for s in scores:
        sc = s["stock_count_norm"]
        # Continuous factor: ratio × max_streak bonus
        cont_factor = s["continuous_ratio"] * 100
        streak_bonus = min(20, (s["max_streak"] - 1) * 10)  # each连板 above 1 → +10
        cont_score = cont_factor * 0.6 + streak_bonus * 0.4

        morning_score = s["morning_ratio"] * 100
        seal_score = s["avg_seal_norm"]

        # Blown penalty: 0% → 0 penalty, 100% → -100
        blown_penalty = s["blown_ratio"] * 100

        s["cont_score"] = round(cont_score, 1)
        s["morning_score"] = round(morning_score, 1)
        s["seal_score"] = round(seal_score, 1)
        s["blown_penalty"] = round(blown_penalty, 1)

        raw = (sc * WEIGHTS["stock_count"]
               + cont_score * WEIGHTS["continuous"]
               + morning_score * WEIGHTS["morning"]
               + seal_score * WEIGHTS["seal_strength"]
               - blown_penalty * abs(WEIGHTS["blown_penalty"]))
        s["hot_score"] = round(max(0, min(100, raw)), 1)

    # Sort by hot_score descending
    scores.sort(key=lambda s: s["hot_score"], reverse=True)

    # Re-rank
    for i, s in enumerate(scores, 1):
        s["rank"] = i

    return scores


# ──────────────── DDX 资金整合 ────────────────

DDX_WEIGHT = 0.30   # DDX 资金分在综合评分中的权重


def _build_sector_name_mapping(ddx_sectors: list[dict]) -> dict[str, list[dict]]:
    """Build name→DDX mapping for cross-referencing with concept names.

    Uses substring matching for best-effort cross-reference.
    Returns dict mapping lowercased sector_name → [ddx_sector_entry, ...].
    """
    mapping = defaultdict(list)
    for s in ddx_sectors:
        name = s.get("sector_name", "").lower().replace(" ", "")
        mapping[name].append(s)
        # Also index by first 4 chars for partial matching
        if len(name) >= 4:
            mapping[name[:4]].append(s)
    return dict(mapping)


def cross_reference_with_ddx(concept_scores: list[dict],
                              ddx_sector_data: list[dict]) -> list[dict]:
    """Merge DDX sector data into concept scores for cross-validation.

    Uses best-effort name matching between 同花顺 concept tags and
    East Money BK sector names. Adds ddx fields where matched.

    Args:
        concept_scores: output from compute_concept_scores().
        ddx_sector_data: output from sector_mapper.aggregate_ddx_by_sector().

    Returns:
        concept_scores with added ddx_score, ddx_inflow_count, ddx_cross fields.
    """
    if not ddx_sector_data:
        return concept_scores

    name_map = _build_sector_name_mapping(ddx_sector_data)
    matched_count = 0

    for cs in concept_scores:
        concept_lower = cs["concept"].lower().replace(" ", "")
        matched_entry = None

        # Try exact match first
        if concept_lower in name_map:
            matched_entry = name_map[concept_lower][0]
        else:
            # Try partial: does any BK sector name contain this concept?
            for bk_name, entries in name_map.items():
                if len(concept_lower) >= 3 and concept_lower in bk_name:
                    matched_entry = entries[0]
                    break
            # Try reverse: does this concept contain a BK sector name?
            if not matched_entry and len(concept_lower) >= 4:
                for bk_name, entries in name_map.items():
                    if len(bk_name) >= 3 and bk_name in concept_lower:
                        matched_entry = entries[0]
                        break

        if matched_entry:
            cs["ddx_score"] = matched_entry["ddx_score"]
            cs["ddx_inflow_count"] = matched_entry["ddx_inflow_count"]
            cs["ddx_inflow_ratio"] = matched_entry["ddx_inflow_ratio"]
            cs["ddx_continuous_count"] = matched_entry["continuous_count"]
            cs["ddx_cross"] = True
            matched_count += 1
        else:
            cs["ddx_score"] = 0
            cs["ddx_inflow_count"] = 0
            cs["ddx_cross"] = False

    if matched_count:
        print(f"    DDX cross-reference: {matched_count}/{len(concept_scores)} concepts matched")
    return concept_scores


def compute_combined_score(concept_scores: list[dict]) -> list[dict]:
    """Compute combined hot_score = 涨停分 × (1-DDX_WEIGHT) + DDX资金分 × DDX_WEIGHT.

    Only affects entries with DDX data. Existing hot_score is treated as
    the 'limit-up score' when DDX weight is applied.
    """
    for cs in concept_scores:
        if cs.get("ddx_cross") and cs.get("ddx_score") is not None:
            limit_score = cs["hot_score"]
            ddx_s = cs["ddx_score"]
            cs["hot_score"] = round(
                limit_score * (1 - DDX_WEIGHT) + ddx_s * DDX_WEIGHT, 1
            )
            cs["hot_score_limit"] = round(limit_score, 1)  # preserve original
            cs["hot_score_ddx"] = round(ddx_s, 1)
    return concept_scores


def get_ddx_sector_data(rebuild_mapping: bool = False) -> list[dict]:
    """Fetch DDX ranking + build sector aggregation. Returns empty list if unavailable."""
    ddx_list = fetch_ddx_ranking(top_n=100)
    if not ddx_list:
        return []

    from fetchers.sector_mapper import get_mapping, aggregate_ddx_by_sector
    mapping = get_mapping(rebuild=rebuild_mapping)
    if not mapping:
        return []

    return aggregate_ddx_by_sector(ddx_list, mapping)


# ──────────────── 主题分类 ────────────────


def classify_concepts(scores: list[dict]) -> dict:
    """Classify concepts into tiers by hot_score."""
    strong = [s for s in scores if s["hot_score"] >= 70]
    active = [s for s in scores if 50 <= s["hot_score"] < 70]
    warm = [s for s in scores if 30 <= s["hot_score"] < 50]
    cold = [s for s in scores if s["hot_score"] < 30]
    return {"strong": strong, "active": active, "warm": warm, "cold": cold}


# ──────────────── 统计摘要 ────────────────


def market_summary(stocks: list[dict]) -> dict:
    """Compute market-level summary stats."""
    total = len(stocks)
    firm = sum(1 for s in stocks if s["limit_type"] == "firm")
    blown = sum(1 for s in stocks if s["limit_type"] == "blown")
    retest = sum(1 for s in stocks if s["limit_type"] == "retest")
    continuous = sum(1 for s in stocks if s["limit_streak"] >= 2)
    high_streak = sum(1 for s in stocks if s["limit_streak"] >= 3)
    early = sum(1 for s in stocks
                if s.get("timing_bucket") in ("pre_open", "morning_early"))
    afternoon = sum(1 for s in stocks
                    if s.get("timing_bucket") in ("afternoon", "afternoon_late"))

    max_streak = max((s["limit_streak"] for s in stocks), default=1)
    top_streak_stocks = [s for s in stocks if s["limit_streak"] >= max_streak]

    return {
        "total": total,
        "firm": firm,
        "blown": blown,
        "retest": retest,
        "continuous": continuous,
        "high_streak": high_streak,
        "early": early,
        "afternoon": afternoon,
        "max_streak": max_streak,
        "top_streak_stocks": [s["name"] for s in top_streak_stocks[:3]],
        "streak_dist": aggregate_by_limit_streak(stocks),
    }


# ──────────────── MD 报告 ────────────────


def generate_report(scores: list[dict], classified: dict,
                    summary: dict, meta: dict,
                    ddx_sectors: Optional[list] = None) -> str:
    """Generate Markdown report."""
    date_str = meta.get("date_str", "")
    scan_time = meta.get("scan_time", "")
    has_ddx = bool(ddx_sectors)
    lines = []
    lines.append(f"## 涨停热力主题报告  {date_str}")
    lines.append("")
    lines.append(f"▸ 扫描时间: {scan_time}")
    lines.append(f"▸ 涨停总数: {summary['total']} 只 "
                 f"(封板 {summary['firm']} | 炸板 {summary['blown']} | 回封 {summary['retest']})")
    lines.append(f"▸ 连板: {summary['continuous']} 只 | "
                 f"高标≥3板: {summary['high_streak']} 只 | "
                 f"最高板: {summary['max_streak']} 板")
    lines.append(f"▸ 早盘涨停: {summary['early']} 只 | "
                 f"午后涨停: {summary['afternoon']} 只")
    if summary["top_streak_stocks"]:
        lines.append(f"▸ 高标股: {'、'.join(summary['top_streak_stocks'])}")
    if has_ddx:
        lines.append(f"▸ DDX 资金扫描: {len(ddx_sectors)} 个板块有资金异动")
    lines.append("")

    # Streak distribution
    sd = summary.get("streak_dist", {})
    if sd:
        bar = " ".join(f"{k}板:{v}" for k, v in sorted(sd.items(), reverse=True))
        lines.append(f"**连板分布**: {bar}")
        lines.append("")

    # Strong concepts
    strong = classified["strong"]
    if strong:
        lines.append("### 核心热点（高持续性）")
        lines.append("")
        lines.append("| 排名 | 概念 | 热度 | 涨停数 | 连板数 | 最高板 | 早盘比 | 封单均值 |")
        lines.append("|------|------|------|--------|--------|--------|--------|---------|")
        for s in strong:
            seal = f"{s['avg_seal']/1e8:.1f}亿" if s['avg_seal'] else "-"
            lines.append(
                f"| **{s['rank']}** | {s['concept']} | **{s['hot_score']:.0f}** | "
                f"{s['stock_count']} | {s['continuous_count']} | {s['max_streak']} | "
                f"{s['morning_ratio']*100:.0f}% | {seal} |"
            )
        lines.append("")

    # Active concepts
    active = classified["active"]
    if active:
        lines.append("### 活跃方向")
        lines.append("")
        lines.append("| 排名 | 概念 | 热度 | 涨停数 | 最高板 | 早盘比 | 代表股 |")
        lines.append("|------|------|------|--------|--------|--------|--------|")
        for s in active[:8]:
            rep = s["members"][0] if s["members"] else {}
            rep_str = f"{rep.get('name','')}({rep.get('limit_streak','')}板)"
            lines.append(
                f"| {s['rank']} | {s['concept']} | {s['hot_score']:.0f} | "
                f"{s['stock_count']} | {s['max_streak']} | "
                f"{s['morning_ratio']*100:.0f}% | {rep_str} |"
            )
        lines.append("")

    # Warm (newly forming)
    warm = classified["warm"]
    if warm:
        lines.append("### 初现方向")
        lines.append("")
        for s in warm[:5]:
            lines.append(
                f"- {s['concept']} — 热度 {s['hot_score']:.0f}, "
                f"涨停 {s['stock_count']} 只, 最高 {s['max_streak']} 板"
            )
        lines.append("")

    # DDX cross-reference section (optional)
    if has_ddx:
        # Filter concepts with DDX data
        ddx_matched = [s for s in scores if s.get("ddx_cross")]
        if ddx_matched:
            # Top DDX concepts sorted by DDX score
            ddx_top = sorted(ddx_matched, key=lambda x: x.get("ddx_score", 0), reverse=True)[:8]
            lines.append("### 📊 DDX 资金交叉验证")
            lines.append("")
            lines.append("| 概念 | DDX资金分 | 涨停热度 | 综合热度 | DDX流入比 | 连续红柱 |")
            lines.append("|------|----------|---------|---------|----------|---------|")
            for s in ddx_top:
                combined = s.get("hot_score", 0)
                lines.append(
                    f"| {s['concept']} | {s.get('ddx_score', 0):.0f} | "
                    f"{s.get('hot_score_limit', s['hot_score']):.0f} | "
                    f"{combined:.0f} | "
                    f"{s.get('ddx_inflow_ratio', 0)*100:.0f}% | "
                    f"{s.get('ddx_continuous_count', 0)} |"
                )
            lines.append("")

        # Pure DDX sectors not matched to any concept (资金潜伏)
        if ddx_sectors:
            ddx_unmatched = [s for s in ddx_sectors
                             if s["sector_name"] not in
                             [c["concept"] for c in scores]
                             and s["ddx_score"] >= 60][:5]
            if ddx_unmatched:
                lines.append("### ⚡ 资金潜伏（DDX流入但无涨停）")
                lines.append("")
                for s in ddx_unmatched:
                    lines.append(
                        f"- {s['sector_name']} — DDX流入 {s['ddx_inflow_ratio']*100:.0f}%, "
                        f"连续红柱 {s['continuous_count']} 只, "
                        f"超级大单占比 {s['avg_super_order_ratio']*100:.1f}%"
                    )
                lines.append("")

    # Blown weakness
    blown_stocks = [s for s in stock_list if s.get("limit_type") == "blown"]
    if blown_stocks:
        lines.append("### 炸板统计（负面信号）")
        lines.append("")
        lines.append("| 代码 | 名称 | 概念 | 炸板时间 |")
        lines.append("|------|------|------|---------|")
        for s in blown_stocks[:8]:
            concepts_str = ",".join(s["concepts"][:2]) if s["concepts"] else "-"
            lines.append(
                f"| {s['code']} | {s['name']} | {concepts_str} | "
                f"{s.get('first_limit_time','-')} |"
            )
        lines.append("")

    lines.append("---")
    lines.append(f"**热点概况**: 核心 {len(strong)} 个 | "
                 f"活跃 {len(active)} 个 | 初现 {len(warm)} 个")
    lines.append("")
    lines.append("> *数据来源: 同花顺涨停复盘 | 仅供学习参考，不构成投资建议*")
    return "\n".join(lines)


# ──────────────── HTML 报告 ────────────────


def _css_hot(score: float) -> str:
    if score >= 70:
        return "strong"
    if score >= 50:
        return "active"
    if score >= 30:
        return "warm"
    return "cold"


def _generate_html_report(scores: list[dict], classified: dict,
                          summary: dict, meta: dict,
                          ddx_sectors: Optional[list] = None) -> str:
    """Generate HTML report matching project style."""
    date_str = meta.get("date_str", "")
    scan_time = meta.get("scan_time", "")
    strong = classified["strong"]
    active = classified["active"]
    warm = classified["warm"]
    sd = summary.get("streak_dist", {})

    # Build streak bar
    streak_bar = " ".join(
        f"{k}板:{v}" for k, v in sorted(sd.items(), reverse=True)
    ) if sd else ""

    # Strong table rows
    def _rank_rows(items, cols, show_idx=False):
        """Render table rows."""
        rows = ""
        for i, s in enumerate(items, 1):
            cells = f"<td>{i}</td>" if show_idx else ""
            for c in cols:
                if c == "concept":
                    cells += f"<td>{s['concept']}</td>"
                elif c == "hot":
                    cells += f'<td class="{_css_hot(s["hot_score"])}">{s["hot_score"]:.0f}</td>'
                elif c == "count":
                    cells += f"<td>{s['stock_count']}</td>"
                elif c == "continuous":
                    cells += f"<td>{s['continuous_count']}</td>"
                elif c == "max_streak":
                    cells += f"<td>{s['max_streak']}</td>"
                elif c == "morning":
                    cells += f"<td>{s['morning_ratio']*100:.0f}%</td>"
                elif c == "seal":
                    v = f"{s['avg_seal']/1e8:.1f}亿" if s.get('avg_seal') else "-"
                    cells += f"<td>{v}</td>"
                elif c == "rep":
                    rep = s["members"][0] if s["members"] else {}
                    cells += f"<td>{rep.get('name','-')}({rep.get('limit_streak','')}板)</td>"
            rows += f"<tr>{cells}</tr>"
        return rows

    rank_cols = ["concept", "hot", "count", "continuous", "max_streak", "morning", "seal"]
    rank_rows = _rank_rows(scores, rank_cols, show_idx=True)
    strong_rows = _rank_rows(strong, rank_cols)

    # Active rows
    active_rows = _rank_rows(active[:8], ["concept", "hot", "count", "max_streak", "morning", "rep"])

    # Blown
    blown_html = ""
    blown_stocks = [s for s in stock_list if s.get("limit_type") == "blown"]
    if blown_stocks:
        blown_rows = ""
        for s in blown_stocks[:8]:
            c = ",".join(s["concepts"][:2]) if s["concepts"] else "-"
            blown_rows += f"<tr><td>{s['code']}</td><td>{s['name']}</td><td>{c}</td><td>{s.get('first_limit_time','-')}</td></tr>"
        blown_html = f"""<div class="sec sec-blown">
        <h2>炸板统计</h2>
        <table><thead><tr><th>代码</th><th>名称</th><th>概念</th><th>时间</th></tr></thead>
        <tbody>{blown_rows}</tbody></table></div>"""

    # Warm list
    warm_html = ""
    if warm:
        warm_items = "".join(
            f"<li>{s['concept']} — 热度 {s['hot_score']:.0f}, 涨停 {s['stock_count']} 只</li>"
            for s in warm[:5]
        )
        warm_html = f"""<div class="sec sec-warm">
        <h2>初现方向</h2>
        <ul>{warm_items}</ul></div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>涨停热力主题 {date_str}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6;padding:20px}}
.w{{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
header{{border-bottom:2px solid #f0f0f0;padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:24px;color:#1a1a1a;margin-bottom:4px}}
.dt{{color:#86868b;font-size:14px;margin-bottom:8px}}
.meta{{color:#86868b;font-size:14px}}
.summary-card{{background:#f3f4f6;border-radius:8px;padding:16px 20px;margin:20px 0;display:flex;gap:16px;flex-wrap:wrap}}
.summary-card .item{{text-align:center;flex:1;min-width:80px}}
.summary-card .num{{font-size:24px;font-weight:700;color:#1d4ed8}}
.summary-card .label{{font-size:12px;color:#86868b}}
h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #f0f0f0}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
tr:nth-child(even) td{{background:#f9fafb}}
.strong{{color:#16a34a;font-weight:700}}
.active{{color:#1d4ed8;font-weight:600}}
.warm{{color:#d97706;font-weight:600}}
.cold{{color:#9ca3af}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.sec h2{{margin-top:0}}
.sec.sec-strong{{border-left:4px solid #16a34a}}
.sec.sec-blown{{border-left:4px solid #dc2626;background:#fef2f2}}
.sec.sec-warm{{border-left:4px solid #d97706}}
.sec ul{{list-style:none;padding:0}}
.sec ul li{{padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px}}
.streak-bar{{background:#f0fdf4;border-radius:8px;padding:12px 16px;margin:12px 0;font-size:14px;text-align:center}}
.disc{{color:#a1a1a6;font-size:12px;font-style:italic;text-align:center;margin-top:32px}}
</style>
</head>
<body>
<div class="w">

<header>
<h1>涨停热力主题分析</h1>
<p class="dt">{date_str} | {scan_time}</p>
<p class="meta">涨停 {summary['total']} 只 | 连板 {summary['continuous']} 只 | 最高 {summary['max_streak']} 板</p>
</header>

<div class="summary-card">
<div class="item"><div class="num">{summary['total']}</div><div class="label">涨停</div></div>
<div class="item"><div class="num">{summary['firm']}</div><div class="label">封板</div></div>
<div class="item"><div class="num">{summary['blown']}</div><div class="label">炸板</div></div>
<div class="item"><div class="num">{summary['continuous']}</div><div class="label">连板</div></div>
<div class="item"><div class="num">{summary['early']}</div><div class="label">早盘</div></div>
<div class="item"><div class="num">{summary['max_streak']}</div><div class="label">最高板</div></div>
</div>

{'<div class="streak-bar">📊 ' + streak_bar + '</div>' if streak_bar else ''}

<div class="sec sec-strong">
<h2>概念热度排行</h2>
<table>
<thead><tr><th>#</th><th>概念</th><th>热度</th><th>涨停</th><th>连板</th><th>最高</th><th>早盘</th><th>封单</th></tr></thead>
<tbody>{rank_rows}</tbody>
</table>
</div>

<div class="sec sec-strong">
<h2>核心热点（热度≥70）</h2>
<table>
<thead><tr><th>概念</th><th>热度</th><th>涨停</th><th>连板</th><th>最高板</th><th>早盘比</th><th>封单均值</th></tr></thead>
<tbody>{strong_rows}</tbody>
</table>
</div>

<div class="sec">
<h2>活跃方向（50-69）</h2>
<table>
<thead><tr><th>概念</th><th>热度</th><th>涨停</th><th>最高板</th><th>早盘比</th><th>代表股</th></tr></thead>
<tbody>{active_rows}</tbody>
</table>
</div>

{warm_html}

{blown_html}

<footer>
<p class="disc">数据来源: 同花顺涨停复盘 | 仅供学习参考，不构成投资建议</p>
</footer>
</div>
</body>
</html>"""


# ──────────────── Main ────────────────


def main():
    parser = argparse.ArgumentParser(
        description="同花顺涨停热力主题分析 (/ths-theme)")
    parser.add_argument("--date", type=str, help="日期 YYYY-MM-DD, 默认今日")
    parser.add_argument("--top", type=int, default=10, help="显示概念数量, 默认10")
    parser.add_argument("--min-score", type=float, default=0, help="最低热力分过滤")
    parser.add_argument("--ddx", action="store_true", help="整合 DDX 资金流向数据")
    parser.add_argument("--rebuild-mapping", action="store_true",
                        help="强制重建股票→板块映射表（配合 --ddx）")
    parser.add_argument("-j", "--json", action="store_true", help="JSON 输出")
    parser.add_argument("--no-html", action="store_true", help="跳过 HTML 报告")
    args = parser.parse_args()

    start = time.time()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    # Fetch limit-up stocks
    global stock_list  # for HTML report access
    stock_list = fetch_limitup_stocks(args.date)

    if not stock_list:
        print(f"⚠️ {date_str} 无涨停数据（非交易日或数据不可用）")
        return

    # Compute scores
    scores = compute_concept_scores(stock_list)

    # ── DDX integration (optional) ──
    ddx_sectors = None
    if args.ddx:
        print("[DDX] Fetching DDX ranking + sector mapping...")
        ddx_sectors = get_ddx_sector_data(rebuild_mapping=args.rebuild_mapping)
        if ddx_sectors:
            print(f"[DDX] Got {len(ddx_sectors)} sectors with DDX data")
            scores = cross_reference_with_ddx(scores, ddx_sectors)
            scores = compute_combined_score(scores)
            # Re-sort after combined scoring
            scores.sort(key=lambda s: s["hot_score"], reverse=True)
            for i, s in enumerate(scores, 1):
                s["rank"] = i
        else:
            print("[DDX] No DDX data available")

    scores = [s for s in scores if s["hot_score"] >= args.min_score]

    if not scores:
        print(f"⚠️ 无概念分数 >= {args.min_score}")
        return

    classified = classify_concepts(scores)
    summary = market_summary(stock_list)
    elapsed = time.time() - start

    meta = {
        "date_str": date_str,
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 1),
        "total_stocks": len(stock_list),
        "total_concepts": len(scores),
        "has_ddx": bool(ddx_sectors),
    }

    if args.json:
        output = {
            "meta": meta,
            "summary": {k: v for k, v in summary.items()
                        if k != "streak_dist"},
            "scores": scores[:args.top],
            "strong": [s for s in classified["strong"]],
            "active": [s for s in classified["active"]],
        }
        if ddx_sectors:
            output["ddx_sectors"] = ddx_sectors[:10]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report = generate_report(scores, classified, summary, meta,
                                 ddx_sectors=ddx_sectors)
        print(report)

    # HTML report
    if not args.no_html and not args.json:
        html = _generate_html_report(scores, classified, summary, meta,
                                     ddx_sectors=ddx_sectors)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = REPORTS_DIR / f"ths-theme-{ts}.html"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"\nHTML report: {html_path}")

    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
