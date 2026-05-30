#!/usr/bin/env python3
"""龙虎榜机构买卖板块聚合 (P4).

基于 AKShare 东方财富龙虎榜机构买卖明细，按板块聚合机构资金倾向。

数据源:
  stock_lhb_jgmmtj_em() — 机构买卖统计（买方/卖方机构数、机构净买额）

聚合流程:
  1. fetch_lhb_jgmmtj(date) → 当日上榜股票 + 机构买卖数据
  2. 加载 sector_mapper 板块映射
  3. aggregate_lhb_by_sector() → 按板块聚合机构净买额
  4. score_lhb_sectors() → 龙虎榜板块评分 (0-100)

评分公式:
  龙虎榜板块分 = 机构净买额分(40%) + 上榜家数(25%) + 机构参与度(20%) + 净买一致性(15%)

Usage:
    python3 fetchers/longhubang_agg.py                         # 今日龙虎榜板块聚合
    python3 fetchers/longhubang_agg.py --date 20260529         # 指定日期
    python3 fetchers/longhubang_agg.py --json                  # JSON 输出
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

from fetchers.sector_mapper import get_mapping
from analysis.ths_theme import _safe_float, _safe_int


# ──────────────── 数据获取 ────────────────


def fetch_lhb_jgmmtj(date_str: Optional[str] = None) -> list[dict]:
    """Fetch 龙虎榜 institutional buy/sell data via AKShare.

    Args:
        date_str: YYYYMMDD date string. Defaults to last trading day.

    Returns:
        List of dicts with keys:
            code, name, close, change_pct, inst_buy_count, inst_sell_count,
            inst_buy_total, inst_sell_total, inst_net_amount, total_amount,
            net_ratio, turnover_rate, market_cap, reason, date
        Empty list on failure.
    """
    if not HAS_AKSHARE:
        return []

    if date_str is None:
        # Default to today, fallback to yesterday
        date_str = datetime.now().strftime("%Y%m%d")

    try:
        df = ak.stock_lhb_jgmmtj_em(start_date=date_str, end_date=date_str)
        if df is None or df.empty:
            # Try yesterday
            dt = datetime.strptime(date_str, "%Y%m%d")
            prev = (dt - timedelta(days=1)).strftime("%Y%m%d")
            df = ak.stock_lhb_jgmmtj_em(start_date=prev, end_date=prev)
            if df is None or df.empty:
                return []
            date_str = prev
    except Exception:
        return []

    results = []
    for _, row in df.iterrows():
        inst_net = _safe_float(row.get("机构买入净额", 0))
        results.append({
            "code": str(row.get("代码", "")),
            "name": str(row.get("名称", "")),
            "close": _safe_float(row.get("收盘价")),
            "change_pct": _safe_float(row.get("涨跌幅")),
            "inst_buy_count": _safe_int(row.get("买方机构数")),
            "inst_sell_count": _safe_int(row.get("卖方机构数")),
            "inst_buy_total": _safe_float(row.get("机构买入总额")),
            "inst_sell_total": _safe_float(row.get("机构卖出总额")),
            "inst_net_amount": inst_net,
            "total_amount": _safe_float(row.get("市场总成交额")),
            "net_ratio": _safe_float(row.get("机构净买额占总成交额比")),
            "turnover_rate": _safe_float(row.get("换手率")),
            "market_cap": _safe_float(row.get("流通市值")),
            "reason": str(row.get("上榜原因", "")),
            "date": str(row.get("上榜日期", date_str)),
        })
    return results


# ──────────────── 板块聚合 ────────────────


def aggregate_lhb_by_sector(lhb_records: list[dict],
                             mapping: dict) -> list[dict]:
    """Aggregate 龙虎榜 institutional data by sector.

    Args:
        lhb_records: list from fetch_lhb_jgmmtj().
        mapping: stock_sector_map dict with "mapping" key.

    Returns:
        List of sector-level aggregates sorted by composite score:
            sector_code, sector_name, sector_type,
            stock_count, inst_buy_count, inst_sell_count, pure_inst_stocks,
            total_inst_net, avg_inst_net_per_stock,
            positive_net_count, positive_net_ratio,
            total_inst_buy, total_inst_sell,
            member_codes, member_names, lhb_score
    """
    if not lhb_records or not mapping:
        return []

    stock_map = mapping.get("mapping", {})
    if not stock_map:
        return []

    # Group: sector → list of LHB records for stocks in that sector
    sector_lhb = defaultdict(list)
    seen_sector_stocks = defaultdict(set)  # track unique stocks per sector
    for rec in lhb_records:
        code = rec["code"]
        sectors = stock_map.get(code, [])
        if not sectors:
            continue

        # Prefer industry sectors: if stock maps to any industry, skip concept-only
        has_industry = any(s["type"] == "industry" for s in sectors)
        for sec in sectors:
            if has_industry and sec["type"] != "industry":
                continue  # skip concept noise when industry available
            key = sec["code"]
            if code in seen_sector_stocks[key]:
                continue  # already counted this stock for this sector
            seen_sector_stocks[key].add(code)
            sector_lhb[key].append({**rec, "sector_name": sec["name"],
                                    "sector_type": sec["type"]})

    if not sector_lhb:
        return []

    results = []
    for sec_code, members in sector_lhb.items():
        total = len(members)
        # Dedup by stock code (same stock can appear multiple times per sector
        # e.g. same stock on different reasons)
        seen_codes = set()
        unique_members = []
        for m in members:
            if m["code"] not in seen_codes:
                seen_codes.add(m["code"])
                unique_members.append(m)
        unique_total = len(unique_members)

        # Stocks with institutional buying
        inst_buy_stocks = [m for m in unique_members
                          if m.get("inst_buy_count", 0) > 0]
        inst_sell_stocks = [m for m in unique_members
                           if m.get("inst_sell_count", 0) > 0]
        pure_inst_buy = [m for m in unique_members
                        if m.get("inst_buy_count", 0) > 0
                        and m.get("inst_sell_count", 0) == 0]

        # Net amounts
        total_inst_net = sum(m.get("inst_net_amount", 0) or 0
                            for m in unique_members)
        total_inst_buy = sum(m.get("inst_buy_total", 0) or 0
                            for m in unique_members)
        total_inst_sell = sum(m.get("inst_sell_total", 0) or 0
                             for m in unique_members)
        positive_net = [m for m in unique_members
                       if m.get("inst_net_amount", 0) or 0 > 0]
        negative_net = [m for m in unique_members
                       if m.get("inst_net_amount", 0) or 0 < 0]

        avg_net = (total_inst_net / unique_total) if unique_total else 0
        positive_ratio = len(positive_net) / unique_total if unique_total else 0

        member_codes = [m["code"] for m in unique_members[:5]]
        member_names = [m["name"] for m in unique_members[:5]]

        results.append({
            "sector_code": sec_code,
            "sector_name": unique_members[0]["sector_name"],
            "sector_type": unique_members[0]["sector_type"],
            "stock_count": unique_total,
            "inst_buy_count": len(inst_buy_stocks),
            "inst_sell_count": len(inst_sell_stocks),
            "pure_inst_buy_count": len(pure_inst_buy),
            "total_inst_net_yi": round(total_inst_net / 1e8, 2),
            "avg_inst_net_yi": round(avg_net / 1e8, 2),
            "total_inst_buy_yi": round(total_inst_buy / 1e8, 2),
            "total_inst_sell_yi": round(total_inst_sell / 1e8, 2),
            "positive_net_count": len(positive_net),
            "negative_net_count": len(negative_net),
            "positive_net_ratio": round(positive_ratio, 3),
            "member_codes": member_codes,
            "member_names": member_names,
            "lhb_score": 0.0,  # placeholder, computed below
        })

    # Score each sector
    results = score_lhb_sectors(results)
    results.sort(key=lambda r: r["lhb_score"], reverse=True)
    return results


# ──────────────── 评分 ────────────────


def score_lhb_sectors(sectors: list[dict]) -> list[dict]:
    """Score sectors on LHB institutional activity (0-100).

    评分维度:
      - 机构净买额分 (40%): 总机构净买额归一化 0-100
      - 上榜家数分 (25%): 上榜股票数归一化 0-100
      - 机构参与度 (20%): 有机构买入的股票占比
      - 净买一致性 (15%): 机构净买入为正的股票占比
    """
    if not sectors:
        return []

    # Extract raw values for normalization
    net_amounts = [abs(s["total_inst_net_yi"]) for s in sectors]
    stock_counts = [s["stock_count"] for s in sectors]

    max_net = max(net_amounts) if net_amounts else 1
    max_count = max(stock_counts) if stock_counts else 1

    for s in sectors:
        # Net amount score: 0亿→0, max→100
        net_abs = abs(s["total_inst_net_yi"])
        net_score = min(100, net_abs / max_net * 100) if max_net > 0 else 0

        # Stock count score: 0→0, max→100
        count_score = s["stock_count"] / max_count * 100 if max_count > 0 else 0

        # Institutional participation (inst_buy_count / stock_count)
        inst_participation = (s["inst_buy_count"] / s["stock_count"]
                             if s["stock_count"] > 0 else 0)
        participation_score = inst_participation * 100

        # Net buy consistency (positive_net_ratio)
        consistency_score = s["positive_net_ratio"] * 100

        # Direction bonus: if net is positive, boost; if negative, penalize
        dir_sign = 1.0 if s["total_inst_net_yi"] >= 0 else 0.3

        composite = (
            net_score * 0.40 * dir_sign
            + count_score * 0.25
            + participation_score * 0.20
            + consistency_score * 0.15
        )

        s["lhb_score"] = round(max(0, min(100, composite)), 1)
        s["net_score"] = round(net_score, 1)
        s["count_score"] = round(count_score, 1)
        s["participation_score"] = round(participation_score, 1)
        s["consistency_score"] = round(consistency_score, 1)
        s["direction"] = "净买" if s["total_inst_net_yi"] >= 0 else "净卖"

    return sectors


# ──────────────── 报告生成 ────────────────


def generate_lhb_md_section(scored: list[dict], top_n: int = 10) -> str:
    """Generate MD section for 龙虎榜 sector aggregation."""
    if not scored:
        return ""

    lines = []
    lines.append("\n### 龙虎榜机构板块聚合")
    lines.append("")
    lines.append("| 排名 | 板块 | 类型 | 评分 | 上榜股数 | 机构净买额(亿) | 机构买入家数 | 方向 |")
    lines.append("|------|------|------|------|---------|---------------|-------------|------|")

    for s in scored[:top_n]:
        net = s["total_inst_net_yi"]
        net_str = f"+{net:.2f}" if net >= 0 else f"{net:.2f}"
        lines.append(
            f"| {scored.index(s)+1} | {s['sector_name']} | "
            f"{'行业' if s['sector_type']=='industry' else '概念'} | "
            f"{s['lhb_score']:.0f} | {s['stock_count']} | "
            f"{net_str} | {s['inst_buy_count']} | {s['direction']} |"
        )

    # Highlight top institutional net buy sectors
    net_buy = [s for s in scored if s["total_inst_net_yi"] > 0]
    net_sell = [s for s in scored if s["total_inst_net_yi"] < 0]
    if net_buy:
        lines.append("")
        lines.append("**机构净买入 Top 3:**")
        for s in net_buy[:3]:
            names = "、".join(s["member_names"][:3])
            lines.append(
                f"- {s['sector_name']} 净买+{s['total_inst_net_yi']:.2f}亿 "
                f"(上榜{s['stock_count']}只, 机构买入{s['inst_buy_count']}家)"
                f" → {names}"
            )
    if net_sell:
        lines.append("")
        lines.append("**机构净卖出 Top 3:**")
        for s in net_sell[:3]:
            names = "、".join(s["member_names"][:3])
            lines.append(
                f"- {s['sector_name']} 净卖{s['total_inst_net_yi']:.2f}亿 "
                f"(上榜{s['stock_count']}只, 机构卖出{s['inst_sell_count']}家)"
                f" → {names}"
            )

    lines.append("")
    return "\n".join(lines)


def generate_lhb_html_section(scored: list[dict], top_n: int = 10) -> str:
    """Generate HTML snippet for 龙虎榜 section."""
    if not scored:
        return ""

    rows = ""
    for s in scored[:top_n]:
        net = s["total_inst_net_yi"]
        net_cls = "sp" if net >= 0 else "sn"
        net_str = f"+{net:.2f}" if net >= 0 else f"{net:.2f}"
        rows += (
            f"<tr>"
            f"<td>{scored.index(s)+1}</td>"
            f"<td><strong>{s['sector_name']}</strong></td>"
            f"<td>{'行业' if s['sector_type']=='industry' else '概念'}</td>"
            f"<td>{s['lhb_score']:.0f}</td>"
            f"<td>{s['stock_count']}</td>"
            f'<td class="{net_cls}">{net_str}</td>'
            f"<td>{s['inst_buy_count']}/{s['inst_sell_count']}</td>"
            f"<td>{s['direction']}</td>"
            f"</tr>"
        )

    # Top buy/sell details
    net_buy = [s for s in scored if s["total_inst_net_yi"] > 0]
    net_sell = [s for s in scored if s["total_inst_net_yi"] < 0]

    buy_details = ""
    if net_buy:
        buy_details += '<div style="margin-top:12px"><strong style="color:#dc2626">机构净买入 Top 3:</strong><ul>'
        for s in net_buy[:3]:
            names = "、".join(s["member_names"][:3])
            buy_details += (
                f'<li style="font-size:13px;margin:4px 0">'
                f'{s["sector_name"]} 净买+{s["total_inst_net_yi"]:.2f}亿 '
                f'(上榜{s["stock_count"]}只, 机构买入{s["inst_buy_count"]}家) → {names}</li>'
            )
        buy_details += "</ul></div>"

    sell_details = ""
    if net_sell:
        sell_details += '<div style="margin-top:8px"><strong style="color:#16a34a">机构净卖出 Top 3:</strong><ul>'
        for s in net_sell[:3]:
            names = "、".join(s["member_names"][:3])
            sell_details += (
                f'<li style="font-size:13px;margin:4px 0">'
                f'{s["sector_name"]} 净卖{s["total_inst_net_yi"]:.2f}亿 '
                f'(上榜{s["stock_count"]}只, 机构卖出{s["inst_sell_count"]}家) → {names}</li>'
            )
        sell_details += "</ul></div>"

    return f"""<div class="sec" style="border-left:4px solid #7c3aed">
<h2 style="color:#7c3aed">🏛️ 龙虎榜机构板块聚合</h2>
<table>
<thead><tr><th>#</th><th>板块</th><th>类型</th><th>评分</th><th>上榜数</th><th>机构净买</th><th>买方/卖方</th><th>方向</th></tr></thead>
<tbody>{rows}</tbody>
</table>
{buy_details}
{sell_details}
</div>"""


# ──────────────── 主入口 ────────────────


def run_lhb_analysis(date_str: Optional[str] = None,
                      top_n: int = 10) -> dict:
    """Complete 龙虎榜 analysis pipeline.

    Args:
        date_str: YYYYMMDD date.
        top_n: Top sectors to score.

    Returns:
        dict with:
            meta: {date, total_lhb_stocks, total_sectors, note}
            sectors: scored sector list
    """
    start = time.time()
    result = {
        "meta": {"date": date_str or datetime.now().strftime("%Y%m%d"),
                 "total_lhb_stocks": 0, "total_sectors": 0, "note": "",
                 "elapsed_seconds": 0},
        "sectors": [],
    }

    if not HAS_AKSHARE:
        result["meta"]["note"] = "AKShare 未安装"
        return result

    # Step 1: Fetch LHB data
    records = fetch_lhb_jgmmtj(date_str)
    if not records:
        result["meta"]["note"] = "今日无龙虎榜数据（非交易日或尚无数据）"
        return result

    result["meta"]["total_lhb_stocks"] = len(records)

    # Step 2: Load sector mapping
    mapping = get_mapping()
    if not mapping:
        result["meta"]["note"] = "无板块映射表（需先运行 sector_mapper）"
        return result

    # Step 3: Aggregate by sector
    sectors = aggregate_lhb_by_sector(records, mapping)
    result["meta"]["total_sectors"] = len(sectors)
    result["sectors"] = sectors[:top_n]

    elapsed = time.time() - start
    result["meta"]["elapsed_seconds"] = round(elapsed, 1)

    return result


def main():
    parser = argparse.ArgumentParser(description="龙虎榜板块聚合")
    parser.add_argument("--date", type=str, help="日期 YYYYMMDD")
    parser.add_argument("--top", type=int, default=10, help="显示个数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--md", action="store_true", help="MD 格式输出")
    args = parser.parse_args()

    result = run_lhb_analysis(args.date, args.top)
    sectors = result["sectors"]

    if not sectors:
        print(f"⚠️ {result['meta']['note']}")
        return

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.md:
        md = generate_lhb_md_section(sectors, args.top)
        print(md)
    else:
        print(f"\n🏛️ 龙虎榜机构板块聚合 ({result['meta']['date']})")
        print(f"   上榜股票: {result['meta']['total_lhb_stocks']} 只")
        print(f"   覆盖板块: {result['meta']['total_sectors']} 个")
        print(f"   耗时: {result['meta']['elapsed_seconds']}s")
        print()
        print(f"{'排名':<4} {'板块':<12} {'类型':<6} {'评分':<6} {'上榜':<6} {'机构净买(亿)':<14} {'买方':<6} {'方向':<4}")
        print("-" * 70)
        for i, s in enumerate(sectors[:args.top], 1):
            net = s["total_inst_net_yi"]
            net_str = f"+{net:.2f}" if net >= 0 else f"{net:.2f}"
            print(f"{i:<4} {s['sector_name']:<12} {'行业' if s['sector_type']=='industry' else '概念':<6} "
                  f"{s['lhb_score']:<6} {s['stock_count']:<6} {net_str:<14} "
                  f"{s['inst_buy_count']:<6} {s['direction']:<4}")

        net_buy = [s for s in sectors if s["total_inst_net_yi"] > 0]
        net_sell = [s for s in sectors if s["total_inst_net_yi"] < 0]
        if net_buy:
            print("\n机构净买入 Top 3:")
            for s in net_buy[:3]:
                names = "、".join(s["member_names"][:3])
                print(f"  +{s['sector_name']} +{s['total_inst_net_yi']:.2f}亿 → {names}")
        if net_sell:
            print("\n机构净卖出 Top 3:")
            for s in net_sell[:3]:
                names = "、".join(s["member_names"][:3])
                print(f"  -{s['sector_name']} {s['total_inst_net_yi']:.2f}亿 → {names}")


if __name__ == "__main__":
    main()
