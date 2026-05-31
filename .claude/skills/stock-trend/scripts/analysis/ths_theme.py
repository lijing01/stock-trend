#!/usr/bin/env python3
"""同花顺板块热力主题分析 (ths-theme).

基于 AKShare 同花顺数据（非直爬），对行业/概念板块做热力评分。
数据来源可靠（当前环境可用），无直连同花顺反爬问题。

数据源：
  - stock_board_industry_summary_ths() → 90 行业实时排行
  - stock_board_concept_summary_ths()  → 50 概念驱动事件

评分公式：
  行业热度 = 涨跌幅×35% + 主力净流入×35% + 上涨比率×30%

Usage:
    python3 ths_theme.py                           # 今日报告
    python3 ths_theme.py --top 20                  # Top 20 行业
    python3 ths_theme.py --json                    # JSON 输出
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))
from fetchers.sector_akshare import (
    get_sector_rankings_akshare,
    get_sector_list_akshare,
    HAS_AKSHARE as HAS_AK,
)

# ──────────────── 数据获取 ────────────────


def fetch_industry_data() -> list[dict]:
    """Fetch industry sector real-time rankings via AKShare.

    Returns list of dicts:
        name, change_pct, net_flow, total_amount, up_count, down_count,
        leader_name, leader_change
    Returns empty list on failure.
    """
    import akshare as ak
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            results.append({
                "name": str(row.get("板块", "")),
                "change_pct": _safe_float(row.get("涨跌幅")),
                "net_flow": _safe_float(row.get("净流入")) * 1e8,  # 亿→元
                "total_amount": _safe_float(row.get("总成交额")) * 1e8,
                "total_volume": _safe_float(row.get("总成交量")),
                "up_count": _safe_int(row.get("上涨家数")),
                "down_count": _safe_int(row.get("下跌家数")),
                "leader_name": str(row.get("领涨股", "")),
                "leader_change": _safe_float(row.get("领涨股-涨跌幅")),
            })
        return results
    except Exception as e:
        print(f"  [AKShare] 获取行业排行失败: {e}", file=sys.stderr)
        return []


def fetch_concept_catalysts() -> list[dict]:
    """Fetch concept summaries with catalyst events via AKShare.

    Returns list of dicts:
        name, date, catalyst (驱动事件), leader, stock_count
    """
    import akshare as ak
    try:
        df = ak.stock_board_concept_summary_ths()
        if df is None or df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            results.append({
                "name": str(row.get("概念名称", "")),
                "date": str(row.get("日期", "")),
                "catalyst": str(row.get("驱动事件", "")),
                "leader": str(row.get("龙头股", "")),
                "stock_count": _safe_int(row.get("成分股数量")),
            })
        return results
    except Exception as e:
        print(f"  [AKShare] 获取概念摘要失败: {e}", file=sys.stderr)
        return []


# ──────────────── 行业评分 ────────────────


INDUSTRY_WEIGHTS = {
    "change_pct": 0.35,
    "net_flow": 0.35,
    "up_ratio": 0.30,
}


def score_industries(industries: list[dict]) -> list[dict]:
    """Score industry sectors by change_pct, net_flow, up_ratio.

    Returns scored list sorted by hot_score descending.
    Each entry has hot_score (0-100).
    """
    if not industries:
        return []

    for s in industries:
        change = s.get("change_pct", 0) or 0
        net = s.get("net_flow", 0) or 0
        up = s.get("up_count", 0) or 0
        down = s.get("down_count", 0) or 0
        total = up + down

        # Normalize change_pct: 0%→50, >5%→100, <-5%→0
        change_score = max(0, min(100, 50 + change * 10))

        # Normalize net_flow: 0→50, >10亿→100, <-10亿→0
        net_yi = net / 1e8
        net_score = max(0, min(100, 50 + net_yi * 5))

        # Up ratio: 50%→50, 100%→100, 0%→0
        up_ratio = up / total if total > 0 else 0.5
        up_score = up_ratio * 100

        s["change_score"] = round(change_score, 1)
        s["net_score"] = round(net_score, 1)
        s["up_ratio"] = round(up_ratio, 3)
        s["up_score"] = round(up_score, 1)

        hot = (change_score * INDUSTRY_WEIGHTS["change_pct"]
               + net_score * INDUSTRY_WEIGHTS["net_flow"]
               + up_score * INDUSTRY_WEIGHTS["up_ratio"])
        s["hot_score"] = round(max(0, min(100, hot)), 1)

    # Sort by hot_score
    industries.sort(key=lambda s: s["hot_score"], reverse=True)
    for i, s in enumerate(industries, 1):
        s["rank"] = i

    return industries


def classify_industries(scored: list[dict]) -> dict:
    """Classify industries by hot_score tiers."""
    strong = [s for s in scored if s["hot_score"] >= 70]
    active = [s for s in scored if 50 <= s["hot_score"] < 70]
    normal = [s for s in scored if 30 <= s["hot_score"] < 50]
    weak = [s for s in scored if s["hot_score"] < 30]
    return {"strong": strong, "active": active, "normal": normal, "weak": weak}


# ──────────────── 市场摘要 ────────────────


def market_summary(industries: list[dict]) -> dict:
    """Compute market-level summary from industry data."""
    total = len(industries)
    up_sectors = sum(1 for s in industries if (s.get("change_pct", 0) or 0) > 0)
    down_sectors = sum(1 for s in industries if (s.get("change_pct", 0) or 0) < 0)
    flat = total - up_sectors - down_sectors
    avg_change = mean(s.get("change_pct", 0) or 0 for s in industries) if industries else 0
    total_net = sum(s.get("net_flow", 0) or 0 for s in industries)
    total_amt = sum(s.get("total_amount", 0) or 0 for s in industries)

    top_gainers = sorted(industries, key=lambda s: s.get("change_pct", 0) or 0, reverse=True)[:3]
    top_losers = sorted(industries, key=lambda s: s.get("change_pct", 0) or 0)[:3]

    max_net_in = sorted(industries, key=lambda s: s.get("net_flow", 0) or 0, reverse=True)[:3]
    max_net_out = sorted(industries, key=lambda s: s.get("net_flow", 0) or 0)[:3]

    return {
        "total_sectors": total,
        "up_sectors": up_sectors,
        "down_sectors": down_sectors,
        "flat": flat,
        "avg_change": round(avg_change, 2),
        "total_net_yi": round(total_net / 1e8, 1),
        "total_amount_yi": round(total_amt / 1e8, 1),
        "top_gainers": [s["name"] for s in top_gainers],
        "top_losers": [s["name"] for s in top_losers],
        "max_net_in": [(s["name"], round(s["net_flow"] / 1e8, 1)) for s in max_net_in],
        "max_net_out": [(s["name"], round(s["net_flow"] / 1e8, 1)) for s in max_net_out],
    }


# ──────────────── 涨停概念评分 ────────────────


def fetch_zt_limitup_data(date_str: Optional[str] = None) -> list[dict]:
    """Fetch limit-up stock data via zt_replay."""
    try:
        from fetchers.zt_replay import fetch_limitup_stocks
        stocks = fetch_limitup_stocks(date_str)
        return stocks or []
    except Exception:
        return []


def score_zt_concepts(stocks: list[dict]) -> list[dict]:
    """Score concepts by limit-up metrics.

    zt_score = stock_count×30% + cont_score×25% + morning_score×20%
               + seal_score×15% - blown_penalty×10%
    """
    if not stocks:
        return []
    from fetchers.zt_replay import aggregate_by_concept
    raw = aggregate_by_concept(stocks)
    if not raw:
        return []

    counts = [c["stock_count"] for c in raw]
    seal_amounts = [c["seal_amount_total"] for c in raw]
    max_count = max(counts) if counts else 1
    max_seal = max(seal_amounts) if seal_amounts else 1

    concept_blown = defaultdict(int)
    concept_total = defaultdict(int)
    for s in stocks:
        for c in s.get("concepts", []):
            concept_total[c] += 1
            if s.get("limit_type") in ("blown",):
                concept_blown[c] += 1

    results = []
    for c in raw:
        name = c["concept"]
        sc = c["stock_count"]
        cont_rate = c["continuous_count"] / max(sc, 1)
        morning_rate = c["early_morning_count"] / max(sc, 1)
        blown_rate = concept_blown.get(name, 0) / max(concept_total.get(name, 1), 1)
        count_score = sc / max_count * 100
        zt = (count_score * 0.30 + cont_rate * 100 * 0.25
              + morning_rate * 100 * 0.20
              + (c["seal_amount_total"] / max_seal * 100 if max_seal > 0 else 0) * 0.15
              - blown_rate * 100 * 0.10)
        results.append({
            "concept": name, "zt_score": round(max(0, min(100, zt)), 1),
            "stock_count": sc, "continuous_count": c["continuous_count"],
            "max_streak": c["max_streak"],
            "early_morning_count": c["early_morning_count"],
            "seal_amount_total": c["seal_amount_total"],
            "blown_rate": round(blown_rate, 2),
            "members": c["members"][:3],
        })
    results.sort(key=lambda r: r["zt_score"], reverse=True)
    return results


def match_zt_to_industries(zt_scored: list[dict],
                           industry_scored: list[dict]) -> list[dict]:
    """Cross-reference zt concepts with industry names."""
    ind_map = {s["name"]: s["hot_score"] for s in industry_scored}
    for zc in zt_scored:
        n = zc["concept"]
        if n in ind_map:
            zc["matched_industry"] = n; zc["industry_score"] = ind_map[n]
            continue
        matched = False
        for iname, iscore in ind_map.items():
            if len(n) >= 3 and (n in iname or iname in n):
                zc["matched_industry"] = iname; zc["industry_score"] = iscore
                matched = True; break
        if not matched:
            zc["matched_industry"] = None; zc["industry_score"] = None
    return zt_scored


def generate_zt_md_section(zt_scored: list[dict], top_n: int = 10) -> str:
    """Generate MD section for limit-up concept scoring."""
    if not zt_scored:
        return ""
    lines = ["\n### 🚀 涨停概念热度", "",
             "| 排名 | 概念 | 涨停分 | 涨停数 | 连板 | 最高板 | 早盘 | 封单(亿) | 关联行业 | 行业热度 |",
             "|------|------|--------|--------|------|-------|------|---------|---------|---------|"]
    for zc in zt_scored[:top_n]:
        ind = zc.get("matched_industry") or "-"
        isc = f"{zc['industry_score']:.0f}" if zc.get("industry_score") is not None else "-"
        seal = zc["seal_amount_total"] / 1e8
        lines.append(
            f"| {zt_scored.index(zc)+1} | {zc['concept']} | {zc['zt_score']:.0f} | "
            f"{zc['stock_count']} | {zc['continuous_count']} | {zc['max_streak']} | "
            f"{zc['early_morning_count']} | {seal:.1f} | {ind} | {isc} |")
    confirmed = [zc for zc in zt_scored
                 if zc.get("industry_score") and zc["industry_score"] >= 60 and zc["zt_score"] >= 50]
    if confirmed:
        lines += ["", "**🔥 双引擎确认**（涨停+行业共振）:"]
        for zc in confirmed[:5]:
            lines.append(f"- {zc['concept']} 涨停分{zc['zt_score']:.0f} 行业热{zc['industry_score']:.0f}")
    dark = [zc for zc in zt_scored
            if zc["zt_score"] >= 50
            and (zc.get("industry_score") is None or zc["industry_score"] < 50)]
    if dark:
        lines += ["", "**⚡ 涨停独立方向**（涨停强但行业尚未跟上）:"]
        for zc in dark[:3]:
            lines.append(f"- {zc['concept']} 涨停分{zc['zt_score']:.0f} 涨停{zc['stock_count']}只 最高{zc['max_streak']}连板")
    return "\n".join(lines)


def generate_zt_html_section(zt_scored: list[dict], top_n: int = 10) -> str:
    if not zt_scored:
        return ""
    rows = ""
    for zc in zt_scored[:top_n]:
        ind = zc.get("matched_industry") or "-"
        isc = f"{zc['industry_score']:.0f}" if zc.get("industry_score") is not None else "-"
        seal = zc["seal_amount_total"] / 1e8
        cls = "s-strong" if zc["zt_score"] >= 70 else "s-active" if zc["zt_score"] >= 50 else ""
        rows += f"<tr><td>{zt_scored.index(zc)+1}</td><td><strong>{zc['concept']}</strong></td><td class=\"{cls}\">{zc['zt_score']:.0f}</td><td>{zc['stock_count']}</td><td>{zc['continuous_count']}</td><td>{zc['max_streak']}</td><td>{zc['early_morning_count']}</td><td>{seal:.1f}</td><td>{ind}</td><td>{isc}</td></tr>"
    return f"""<div class="sec" style="border-left:4px solid #f59e0b"><h2 style="color:#f59e0b">🚀 涨停概念热度</h2><table><thead><tr><th>#</th><th>概念</th><th>涨停分</th><th>涨停数</th><th>连板</th><th>最高板</th><th>早盘</th><th>封单(亿)</th><th>关联行业</th><th>行业热</th></tr></thead><tbody>{rows}</tbody></table></div>"""


# ──────────────── MD 报告 ────────────────


def generate_report(scored: list[dict], classified: dict,
                    summary: dict, concepts: list[dict],
                    meta: dict) -> str:
    """Generate Markdown report."""
    now = meta.get("scan_time", "")
    lines = []
    lines.append(f"## 板块热力主题报告")
    lines.append("")
    lines.append(f"▸ 扫描时间: {now}")
    lines.append(f"▸ 行业板块: {summary['total_sectors']} 个 "
                 f"(涨 {summary['up_sectors']} | 跌 {summary['down_sectors']} | 平 {summary['flat']})")
    lines.append(f"▸ 平均涨跌幅: {summary['avg_change']:+.2f}%")
    lines.append(f"▸ 板块主力净流入: {summary['total_net_yi']:+.1f}亿")
    lines.append(f"▸ 领涨: {'、'.join(summary['top_gainers'][:3])}")
    lines.append(f"▸ 领跌: {'、'.join(summary['top_losers'][:3])}")
    if summary["max_net_in"]:
        net_str = " | ".join(f"{n}({v:+.1f}亿)" for n, v in summary["max_net_in"])
        lines.append(f"▸ 资金流入: {net_str}")
    if summary["max_net_out"]:
        net_str = " | ".join(f"{n}({v:+.1f}亿)" for n, v in summary["max_net_out"])
        lines.append(f"▸ 资金流出: {net_str}")
    lines.append("")

    # Strong
    strong = classified["strong"]
    if strong:
        lines.append("### 强势板块（热度≥70）")
        lines.append("")
        lines.append("| 排名 | 板块 | 热度 | 涨跌幅 | 净流入(亿) | 上涨/总数 | 领涨股 |")
        lines.append("|------|------|------|--------|-----------|----------|--------|")
        for s in strong[:10]:
            net = s.get("net_flow", 0) or 0
            up = s.get("up_count", 0) or 0
            down = s.get("down_count", 0) or 0
            total = up + down
            lines.append(
                f"| {s['rank']} | {s['name']} | **{s['hot_score']:.0f}** | "
                f"{s.get('change_pct',0):+.2f}% | {net/1e8:+.1f} | "
                f"{up}/{total} | {s.get('leader_name','-')} |"
            )
        lines.append("")

    # Active
    active = classified["active"]
    if active:
        lines.append("### 活跃板块（50-69）")
        lines.append("")
        lines.append("| 排名 | 板块 | 热度 | 涨跌幅 | 净流入(亿) |")
        lines.append("|------|------|------|--------|-----------|")
        for s in active[:8]:
            net = s.get("net_flow", 0) or 0
            lines.append(
                f"| {s['rank']} | {s['name']} | {s['hot_score']:.0f} | "
                f"{s.get('change_pct',0):+.2f}% | {net/1e8:+.1f} |"
            )
        lines.append("")

    # Concept catalysts
    if concepts:
        lines.append("### 概念驱动事件")
        lines.append("")
        lines.append("| 概念 | 驱动事件 | 成分股 | 龙头股 |")
        lines.append("|------|---------|--------|--------|")
        for c in concepts[:10]:
            # Clean catalyst: remove prefix like "概念新服|"
            cat = c["catalyst"]
            cat = cat.replace("概念新服|新建", "").replace("概念新服|", "")
            lines.append(
                f"| {c['name']} | {cat[:50]} | {c['stock_count']} | "
                f"{c['leader'] or '-'} |"
            )
        lines.append("")

    # Capital flow extremes
    if summary["total_net_yi"] != 0:
        lines.append("### 资金流向极端")
        lines.append("")
        if summary["max_net_in"]:
            lines.append("**大幅流入**:")
            for name, val in summary["max_net_in"]:
                lines.append(f"- {name} +{val:.1f}亿")
        if summary["max_net_out"]:
            lines.append("**大幅流出**:")
            for name, val in summary["max_net_out"]:
                lines.append(f"- {name} {val:.1f}亿")
        lines.append("")

    # Weak sectors
    weak = classified["weak"]
    if weak:
        lines.append("### 弱势板块（<30）")
        lines.append("")
        for s in weak[:5]:
            lines.append(
                f"- {s['name']} — 热度 {s['hot_score']:.0f}, "
                f"涨跌幅 {s.get('change_pct',0):+.2f}%"
            )
        lines.append("")

    lines.append("---")
    strong_c = len(classified["strong"])
    active_c = len(classified["active"])
    weak_c = len(classified["weak"])
    lines.append(f"**概况**: 强势 {strong_c} 个 | 活跃 {active_c} 个 | 弱势 {weak_c} 个")
    lines.append("")
    lines.append("> *数据来源: 同花顺 (AKShare) | 仅供学习参考*")
    return "\n".join(lines)


# ──────────────── HTML 报告 ────────────────


def _generate_html_report(scored: list[dict], classified: dict,
                          summary: dict, concepts: list[dict],
                          meta: dict) -> str:
    """Generate HTML report with interactive visualizations."""
    now = meta.get("scan_time", "")
    strong = classified["strong"]
    active = classified["active"]
    weak = classified["weak"]

    def _rows(items, cols):
        rows = ""
        for s in items:
            cells = ""
            for c in cols:
                if c == "rank":
                    cells += f"<td>{s['rank']}</td>"
                elif c == "name":
                    cells += f"<td><strong>{s['name']}</strong></td>"
                elif c == "hot":
                    cls = "s-strong" if s['hot_score'] >= 70 else "s-active" if s['hot_score'] >= 50 else ""
                    cells += f'<td class="{cls}">{s["hot_score"]:.0f}</td>'
                elif c == "change":
                    cls = "sp" if (s.get("change_pct", 0) or 0) > 0 else "sn"
                    cells += f'<td class="{cls}">{s.get("change_pct",0):+.2f}%</td>'
                elif c == "net":
                    net = s.get("net_flow", 0) or 0
                    cls = "sp" if net > 0 else "sn"
                    cells += f'<td class="{cls}">{net/1e8:+.1f}亿</td>'
                elif c == "ud":
                    up = s.get("up_count", 0) or 0
                    down = s.get("down_count", 0) or 0
                    cells += f"<td>{up}/{up+down}</td>"
                elif c == "leader":
                    cells += f"<td>{s.get('leader_name','-')}</td>"
            rows += f"<tr>{cells}</tr>"
        return rows

    ind_cols = ["rank", "name", "hot", "change", "net", "ud", "leader"]

    # ── Fund flow bar data (top 5 in / top 5 out) ──
    sorted_by_net = sorted(scored, key=lambda s: s.get("net_flow", 0) or 0, reverse=True)
    top_inflow = [s for s in sorted_by_net if (s.get("net_flow", 0) or 0) > 0][:5]
    top_outflow = [s for s in reversed(sorted_by_net) if (s.get("net_flow", 0) or 0) < 0][:5]
    # Max absolute net for scaling bars
    all_nets = [abs(s.get("net_flow", 0) or 0) for s in top_inflow + top_outflow]
    max_net = max(all_nets) / 1e8 if all_nets else 1

    def _fund_bar(sector, side):
        net_yi = (sector.get("net_flow", 0) or 0) / 1e8
        pct = min(100, abs(net_yi) / max_net * 100) if max_net else 0
        color = "#dc2626" if side == "in" else "#16a34a"
        bar_cls = "bar-in" if side == "in" else "bar-out"
        label = f"+{net_yi:.1f}" if net_yi > 0 else f"{net_yi:.1f}"
        return f'<div class="bar-row"><span class="bar-label">{sector["name"]}</span><div class="bar-track"><div class="{bar_cls}" style="width:{pct:.0f}%;background:{color}"></div></div><span class="bar-val">{label}亿</span></div>'

    fund_bars = '<div class="fund-flow"><div class="fund-col"><h4 style="color:#dc2626;margin:0 0 8px">📈 流入 Top 5</h4>'
    fund_bars += "".join(_fund_bar(s, "in") for s in top_inflow)
    fund_bars += '</div><div class="fund-col"><h4 style="color:#16a34a;margin:0 0 8px">📉 流出 Top 5</h4>'
    fund_bars += "".join(_fund_bar(s, "out") for s in top_outflow)
    fund_bars += "</div></div>"

    # ── Plotly scatter data ──
    scatter_data = []
    for s in scored:
        change = s.get("change_pct", 0) or 0
        net_yi = (s.get("net_flow", 0) or 0) / 1e8
        amount_yi = (s.get("total_amount", 0) or 0) / 1e8
        scatter_data.append({
            "name": s["name"],
            "change": round(change, 2),
            "net": round(net_yi, 2),
            "amount": round(amount_yi, 1),
            "score": round(s["hot_score"], 0),
            "up": (s.get("up_count", 0) or 0),
            "down": (s.get("down_count", 0) or 0),
        })

    # ── Score distribution histogram ──
    buckets = [0] * 10
    for s in scored:
        idx = min(9, int(s["hot_score"] // 10))
        buckets[idx] += 1
    max_bucket = max(buckets) if buckets else 1

    def _dist_bar(count, idx):
        pct = count / max_bucket * 100 if max_bucket else 0
        label = f"{idx*10}-{(idx+1)*10}"
        return f'<div class="dbar-row"><span class="dbar-label">{label}</span><div class="dbar-track"><div class="dbar-fill" style="width:{pct:.0f}%"></div></div><span class="dbar-val">{count}</span></div>'

    dist_bars = "".join(_dist_bar(buckets[i], i) for i in range(10))

    # Concept rows
    concept_rows = ""
    for c in concepts[:10]:
        cat = c["catalyst"].replace("概念新服|新建", "").replace("概念新服|", "")
        concept_rows += f"<tr><td>{c['name']}</td><td>{cat[:40]}</td><td>{c['stock_count']}</td><td>{c['leader'] or '-'}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>板块热力主题 {now}</title>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6;padding:20px}}
.w{{max-width:1100px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
header{{border-bottom:2px solid #f0f0f0;padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:24px;color:#1a1a1a}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.summary-cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#f9fafb;border-radius:8px;padding:12px 16px;flex:1;min-width:100px;text-align:center}}
.card .num{{font-size:22px;font-weight:700;color:#1d4ed8}}
.card .lbl{{font-size:12px;color:#86868b;margin-top:2px}}
.leader-bar{{background:#f0fdf4;border-radius:8px;padding:12px 16px;margin:12px 0;font-size:14px;text-align:center}}
h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
tr:nth-child(even) td{{background:#f9fafb}}
.sp{{color:#dc2626;font-weight:600}}
.sn{{color:#16a34a;font-weight:600}}
.s-strong{{color:#16a34a;font-weight:700;font-size:15px}}
.s-active{{color:#1d4ed8;font-weight:600}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.sec h2{{margin-top:0}}
.sec.sec-strong{{border-left:4px solid #16a34a}}
/* Fund flow bars */
.fund-flow{{display:flex;gap:24px;margin:20px 0 30px}}
.fund-col{{flex:1}}
.bar-row{{display:flex;align-items:center;margin:6px 0;font-size:13px;gap:8px}}
.bar-label{{width:80px;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bar-track{{flex:1;height:20px;background:#f0f0f0;border-radius:4px;overflow:hidden}}
.bar-in,.bar-out{{height:100%;border-radius:4px;min-width:2px;transition:width .3s}}
.bar-val{{width:72px;font-weight:600;font-size:12px}}
/* Distribution bars */
.dist-box{{display:flex;gap:20px;margin:12px 0}}
.dbar-row{{display:flex;align-items:center;margin:3px 0;font-size:12px;gap:8px}}
.dbar-label{{width:50px;text-align:right;font-size:11px;color:#86868b}}
.dbar-track{{flex:1;height:14px;background:#f0f0f0;border-radius:3px;overflow:hidden}}
.dbar-fill{{height:100%;background:#6366f1;border-radius:3px;min-width:2px}}
.dbar-val{{width:28px;font-weight:600;font-size:11px;color:#6366f1;text-align:right}}
.chart-box{{width:100%;height:500px;margin:16px 0}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:32px}}
</style>
</head>
<body>
<div class="w">

<header>
<h1>板块热力主题分析</h1>
<p class="dt">{now}</p>
</header>

<div class="summary-cards">
<div class="card"><div class="num">{summary['total_sectors']}</div><div class="lbl">行业总数</div></div>
<div class="card"><div class="num" style="color:#dc2626">{summary['up_sectors']}</div><div class="lbl">上涨</div></div>
<div class="card"><div class="num" style="color:#16a34a">{summary['down_sectors']}</div><div class="lbl">下跌</div></div>
<div class="card"><div class="num">{summary['avg_change']:+.1f}%</div><div class="lbl">平均涨跌</div></div>
<div class="card"><div class="num">{summary['total_net_yi']:+.0f}亿</div><div class="lbl">主力净流</div></div>
</div>

<div class="leader-bar">📈 领涨: {'、'.join(summary['top_gainers'][:5])}</div>

<div class="sec">
<h2>💰 资金流向</h2>
{fund_bars}
</div>

<div class="sec">
<h2>📊 板块分布图 — 涨跌幅 vs 主力净流入</h2>
<p style="font-size:13px;color:#86868b;margin:4px 0 8px">气泡大小 = 成交额，颜色 = 热度分 | 悬停查看详情</p>
<div id="scatterChart" class="chart-box"></div>
</div>

<div class="sec">
<h2>📋 热度分布</h2>
<div class="dist-box">
<div style="flex:1">
<p style="font-size:12px;color:#86868b;margin:0 0 6px">各分数段板块数量</p>
{dist_bars}
</div>
<div style="flex:0 0 auto;padding:8px 12px;background:#f0fdf4;border-radius:8px;font-size:13px;line-height:1.8">
<div><strong>强势 (≥70):</strong> <span style="color:#16a34a">{len(strong)}</span></div>
<div><strong>活跃 (50-69):</strong> <span style="color:#1d4ed8">{len(active)}</span></div>
<div><strong>弱势 (&lt;30):</strong> <span style="color:#a1a1a6">{len(weak)}</span></div>
</div>
</div>
</div>

{'<div class="sec sec-strong"><h2>强势板块（热度≥70）</h2><table><thead><tr><th>#</th><th>板块</th><th>热度</th><th>涨跌幅</th><th>净流入</th><th>涨跌</th><th>领涨</th></tr></thead><tbody>' + _rows(strong[:10], ind_cols) + '</tbody></table></div>' if strong else ''}

{'<div class="sec"><h2>活跃板块（50-69）</h2><table><thead><tr><th>#</th><th>板块</th><th>热度</th><th>涨跌幅</th><th>净流入</th><th>涨跌</th><th>领涨</th></tr></thead><tbody>' + _rows(active[:8], ind_cols) + '</tbody></table></div>' if active else ''}

<div class="sec">
<h2>概念驱动事件</h2>
<table><thead><tr><th>概念</th><th>驱动事件</th><th>成分股</th><th>龙头股</th></tr></thead>
<tbody>{concept_rows}</tbody></table>
</div>

<footer>
<p class="disc">数据来源: 同花顺 (AKShare) | 仅供学习参考</p>
</footer>
</div>

<script>
var scatterData = {json.dumps(scatter_data, ensure_ascii=False)};
var trace = {{
  x: scatterData.map(d => d.change),
  y: scatterData.map(d => d.net),
  mode: 'markers',
  type: 'scatter',
  marker: {{
    size: scatterData.map(d => Math.max(6, Math.sqrt(d.amount) * 1.5)),
    color: scatterData.map(d => d.score),
    colorscale: [['0','#e74c3c'],['0.3','#f39c12'],['0.5','#fdcb6e'],['0.7','#6ab04c'],['1','#2d7d46']],
    showscale: true,
    colorbar: {{title: '热度', titleside: 'right'}},
    line: {{color:'#fff', width: 0.5}},
    sizeref: 0.3,
    sizemode: 'area',
  }},
  text: scatterData.map(d => d.name),
  hovertemplate: '<b>%{{text}}</b><br>涨跌幅: %{{x:.2f}}%<br>净流入: %{{y:.1f}}亿<br>热度: %{{marker.color:.0f}}<extra></extra>',
}};

var layout = {{
  title: '',
  xaxis: {{title: '涨跌幅(%)', zerolinecolor: '#ddd', gridcolor: '#f0f0f0'}},
  yaxis: {{title: '主力净流入(亿)', zerolinecolor: '#ddd', gridcolor: '#f0f0f0'}},
  hovermode: 'closest',
  margin: {{l:60, r:30, t:10, b:50, pad:4}},
  plot_bgcolor: '#fafafa',
  paper_bgcolor: '#fafafa',
  font: {{family: 'PingFang SC, Microsoft YaHei, sans-serif', size: 13}},
}};

Plotly.newPlot('scatterChart', [trace], layout, {{responsive: true, displayModeBar: false}});
</script>
</body>
</html>"""


# ──────────────── Main ────────────────


def main():
    parser = argparse.ArgumentParser(description="板块热力主题分析 (/ths-theme)")
    parser.add_argument("--top", type=int, default=15, help="显示数量, 默认15")
    parser.add_argument("--min-score", type=float, default=0, help="最低热力分")
    parser.add_argument("-j", "--json", action="store_true", help="JSON 输出")
    parser.add_argument("--no-html", action="store_true", help="跳过 HTML")
    parser.add_argument("--no-zt", action="store_true", default=False,
                        help="跳过涨停概念热度评分")
    parser.add_argument("--no-longhubang", "--no-lhb", action="store_true", default=False,
                        help="跳过龙虎榜机构板块聚合分析")
    parser.add_argument("--lhb-date", type=str, help="龙虎榜日期 YYYYMMDD")
    parser.add_argument("--zt-date", type=str, help="涨停日期 YYYY-MM-DD")
    args = parser.parse_args()

    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not HAS_AK:
        print("⚠️ AKShare 未安装")
        return

    print("[Phase 1/4] 获取行业排行...")
    industries = fetch_industry_data()
    if not industries:
        print("⚠️ 无行业数据")
        return
    print(f"  Got {len(industries)} industry sectors")

    print("[Phase 2/4] 获取概念驱动事件...")
    concepts = fetch_concept_catalysts()
    if concepts:
        print(f"  Got {len(concepts)} concept catalysts")

    print("[Phase 3/4] 评分...")
    scored = score_industries(industries)
    scored = [s for s in scored if s["hot_score"] >= args.min_score]
    classified = classify_industries(scored)
    summary = market_summary(scored)

    # 涨停概念数据
    zt_scored = []
    if not args.no_zt:
        print("\n[Phase 4/4] 涨停概念热度评分...")
        zt_stocks = fetch_zt_limitup_data(args.zt_date)
        if zt_stocks:
            zt_raw = score_zt_concepts(zt_stocks)
            zt_scored = match_zt_to_industries(zt_raw, scored)
            print(f"  Got {len(zt_stocks)} limit-up stocks, {len(zt_scored)} concepts scored")
        else:
            print("  ⚠️ 无涨停数据")

    # 龙虎榜数据
    lhb_sectors = []
    if not args.no_longhubang:
        print("\n[Phase 4/4] 龙虎榜机构板块聚合...")
        try:
            from fetchers.longhubang_agg import (
                run_lhb_analysis,
                generate_lhb_md_section,
                generate_lhb_html_section,
            )
            lhb_result = run_lhb_analysis(args.lhb_date)
            lhb_sectors = lhb_result.get("sectors", [])
            if lhb_sectors:
                print(f"  Got {lhb_result['meta']['total_lhb_stocks']} LHB stocks "
                      f"across {len(lhb_sectors)} sectors")
            else:
                print(f"  ⚠️ {lhb_result['meta']['note']}")
        except Exception as e:
            print(f"  ⚠️ 龙虎榜分析失败: {e}")

    elapsed = time.time() - start
    meta = {
        "scan_time": now,
        "elapsed_seconds": round(elapsed, 1),
        "total_sectors": len(scored),
        "source": "akshare",
        "has_longhubang": bool(lhb_sectors),
    }

    if args.json:
        output = {
            "meta": meta,
            "summary": summary,
            "sectors": [{k: v for k, v in s.items()
                         if k in ("rank", "name", "hot_score", "change_pct",
                                  "net_flow", "up_count", "down_count",
                                  "leader_name")}
                        for s in scored[:args.top]],
            "strong": [s["name"] for s in classified["strong"]],
            "active": [s["name"] for s in classified["active"]],
            "concepts": concepts[:10] if concepts else [],
            "longhubang": lhb_sectors[:args.top] if lhb_sectors else [],
            "zt_concepts": [{"concept": z["concept"], "zt_score": z["zt_score"],
                             "stock_count": z["stock_count"],
                             "max_streak": z["max_streak"],
                             "matched_industry": z.get("matched_industry"),
                             "industry_score": z.get("industry_score")}
                            for z in zt_scored[:args.top]] if zt_scored else [],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report = generate_report(scored, classified, summary,
                                 concepts or [], meta)
        # 追加涨停概念章节
        zt_md = ""
        if zt_scored:
            zt_md = generate_zt_md_section(zt_scored, args.top)
        report += zt_md
        # 追加龙虎榜章节
        lhb_md = ""
        if lhb_sectors:
            from fetchers.longhubang_agg import generate_lhb_md_section
            lhb_md = generate_lhb_md_section(lhb_sectors, args.top)
        report += lhb_md
        print(report)

    if not args.no_html and not args.json:
        html = _generate_html_report(scored, classified, summary,
                                     concepts or [], meta)
        marker = "</div>\n\n<footer>"
        # 追加涨停概念 HTML 章节
        if zt_scored:
            zt_html = generate_zt_html_section(zt_scored, args.top)
            html = html.replace(marker, f"{zt_html}\n{marker}")
        # 追加龙虎榜 HTML 章节
        if lhb_sectors:
            from fetchers.longhubang_agg import generate_lhb_html_section
            lhb_html = generate_lhb_html_section(lhb_sectors, args.top)
            html = html.replace(marker, f"{lhb_html}\n{marker}")
        html_path = REPORTS_DIR / f"ths-theme-{now_ts}.html"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"\nHTML report: {html_path}")

    print(f"\nDone in {elapsed:.1f}s")


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


if __name__ == "__main__":
    main()
