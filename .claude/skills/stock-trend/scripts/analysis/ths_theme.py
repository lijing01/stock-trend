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
    """Generate HTML report."""
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
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6;padding:20px}}
.w{{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
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
</body>
</html>"""


# ──────────────── Main ────────────────


def main():
    parser = argparse.ArgumentParser(description="板块热力主题分析 (/ths-theme)")
    parser.add_argument("--top", type=int, default=15, help="显示数量, 默认15")
    parser.add_argument("--min-score", type=float, default=0, help="最低热力分")
    parser.add_argument("-j", "--json", action="store_true", help="JSON 输出")
    parser.add_argument("--no-html", action="store_true", help="跳过 HTML")
    args = parser.parse_args()

    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not HAS_AK:
        print("⚠️ AKShare 未安装")
        return

    print("[Phase 1/3] 获取行业排行...")
    industries = fetch_industry_data()
    if not industries:
        print("⚠️ 无行业数据")
        return
    print(f"  Got {len(industries)} industry sectors")

    print("[Phase 2/3] 获取概念驱动事件...")
    concepts = fetch_concept_catalysts()
    if concepts:
        print(f"  Got {len(concepts)} concept catalysts")

    print("[Phase 3/3] 评分...")
    scored = score_industries(industries)
    scored = [s for s in scored if s["hot_score"] >= args.min_score]
    classified = classify_industries(scored)
    summary = market_summary(scored)

    elapsed = time.time() - start
    meta = {
        "scan_time": now,
        "elapsed_seconds": round(elapsed, 1),
        "total_sectors": len(scored),
        "source": "akshare",
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
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report = generate_report(scored, classified, summary,
                                 concepts or [], meta)
        print(report)

    if not args.no_html and not args.json:
        html = _generate_html_report(scored, classified, summary,
                                     concepts or [], meta)
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
