#!/usr/bin/env python3
"""周主线报告 — 中期板块方向聚合分析.

聚合一周数据（行业热力 + 持续性 + 龙虎榜机构信号），
识别适合中线持仓（1-6个月）的主线方向。

数据源:
  - ths_theme: 行业板块实时热力评分
  - market_theme snapshots: 板块持续性记录
  - LHB snapshots: 机构资金信号

评分公式:
  周主线分 = 周均热度(30%) + 上榜频率(25%) + 最新热度(25%)
             + 趋势方向(10%) + LHB验证(10%)

Usage:
    python3 analysis/weekly_report.py                     # 本周报告
    python3 analysis/weekly_report.py --weeks 2           # 回溯2周
    python3 analysis/weekly_report.py --html              # HTML 报告
"""

import argparse
import json
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
LHB_SNAPSHOT_DIR = CACHE_DIR / "lhb_snapshots"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


def _safe_float(v) -> float:
    if v is None: return 0.0
    try: return float(v)
    except: return 0.0


# ──────────────── 数据收集 ────────────────


def load_market_snapshots(days: int = 10) -> dict[str, list[dict]]:
    """Load market-theme sector snapshots."""
    try:
        from fetchers.sector_data import load_snapshot_history
        return load_snapshot_history(days=days)
    except Exception:
        return {}


def load_lhb_snapshots(days: int = 10) -> list[dict]:
    """Load LHB institutional snapshots."""
    snapshots = []
    today = date.today()
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        fp = LHB_SNAPSHOT_DIR / f"{d}.json"
        if fp.exists():
            try:
                snapshots.append(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
    return sorted(snapshots, key=lambda s: s["date"])


def fetch_industry_data() -> list[dict]:
    """Fetch today's industry heat data via ths-theme."""
    try:
        from analysis.ths_theme import fetch_industry_data, score_industries
        industries = fetch_industry_data()
        if industries:
            return score_industries(industries)
    except Exception:
        pass
    return []


# ──────────────── 聚合评分 ────────────────


def aggregate_sectors(market_snapshots: dict[str, list[dict]],
                       lhb_snapshots: list[dict],
                       today_industries: list[dict]) -> list[dict]:
    """Aggregate sector data across all sources.

    Returns scored sectors with weekly_score (0-100).
    """
    # Track all sectors and their daily data
    sector_days = defaultdict(list)  # sector_name → list of {date, hot_score, ...}
    sector_codes = {}

    # From market-theme snapshots
    for date_str, sectors in market_snapshots.items():
        for s in sectors:
            name = s.get("name", "")
            if not name:
                continue
            sector_days[name].append({
                "date": date_str,
                "hot_score": _safe_float(s.get("hot_score", 0)),
                "change_pct": _safe_float(s.get("change_pct", 0)),
                "up_ratio": _safe_float(s.get("up_ratio", 0)),
                "source": "market_theme",
            })
            # Store code from first encounter
            if name not in sector_codes:
                sector_codes[name] = s.get("code", "")

    # From today's industry data
    for s in today_industries:
        name = s.get("name", "")
        if not name:
            continue
        sector_days[name].append({
            "date": "today",
            "hot_score": s.get("hot_score", 0),
            "change_pct": _safe_float(s.get("change_pct", 0)),
            "up_ratio": _safe_float(s.get("up_ratio", 0)),
            "net_flow": s.get("net_flow", 0),
            "leader": s.get("leader_name", ""),
            "source": "ths_theme",
        })
        if name not in sector_codes:
            sector_codes[name] = ""

    if not sector_days:
        return []

    # Count how many distinct dates we have
    all_dates = set()
    for entries in sector_days.values():
        for e in entries:
            if e["date"] != "today":
                all_dates.add(e["date"])
    total_dates = len(all_dates) + (1 if any(
        any(e["date"] == "today" for e in entries)
        for entries in sector_days.values()) else 0)

    # Score each sector
    results = []
    for name, days in sector_days.items():
        hot_scores = [d["hot_score"] for d in days if d["hot_score"] > 0]
        appearance_days = len(set(d["date"] for d in days if d["hot_score"] > 0))

        # Weekly avg hot_score
        avg_hot = mean(hot_scores) if hot_scores else 0

        # Frequency
        freq = appearance_days / max(total_dates, 1)

        # Latest hot_score
        latest_entry = max(days, key=lambda d: (
            9999 if d["date"] == "today" else 0,
            d["date"]
        ))
        latest_hot = latest_entry.get("hot_score", 0)

        # Trend: compare first half vs second half
        sorted_days = sorted([d for d in days if d["date"] != "today"],
                             key=lambda d: d["date"])
        mid = len(sorted_days) // 2
        if mid > 0 and len(sorted_days) >= 2:
            first_avg = mean(d["hot_score"] for d in sorted_days[:mid] if d["hot_score"] > 0) if mid else 0
            second_avg = mean(d["hot_score"] for d in sorted_days[mid:] if d["hot_score"] > 0) if len(sorted_days) > mid else 0
            trend = "up" if second_avg > first_avg else "down" if second_avg < first_avg else "flat"
            trend_score = 80 if trend == "up" else 40 if trend == "down" else 60
        else:
            trend = "flat"
            trend_score = 50

        # LHB cross-ref
        lhb_net = 0
        lhb_direction = ""
        for snap in lhb_snapshots:
            for sec in snap.get("sectors", []):
                if sec["sector_name"] == name or sec.get("matched_industry") == name:
                    lhb_net += sec.get("inst_net_yi", 0)
        if lhb_net > 0.5:
            lhb_direction = "净买"
            lhb_score = 80
        elif lhb_net < -0.5:
            lhb_direction = "净卖"
            lhb_score = 20
        else:
            lhb_score = 50

        # Composite weekly score
        weekly = (avg_hot * 0.30 + freq * 100 * 0.25
                  + latest_hot * 0.25 + trend_score * 0.10
                  + lhb_score * 0.10)
        weekly = round(max(0, min(100, weekly)), 1)

        net_flow = latest_entry.get("net_flow", 0) or 0
        leader = latest_entry.get("leader", "")
        change = latest_entry.get("change_pct", 0) or 0

        results.append({
            "name": name,
            "code": sector_codes.get(name, ""),
            "weekly_score": weekly,
            "avg_hot": round(avg_hot, 1),
            "appearance_days": appearance_days,
            "total_dates": total_dates,
            "frequency": round(freq, 2),
            "latest_hot": round(latest_hot, 1),
            "latest_change": round(change, 2),
            "net_flow": net_flow,
            "trend": trend,
            "trend_score": trend_score,
            "lhb_net_yi": round(lhb_net, 2),
            "lhb_direction": lhb_direction,
            "leader": leader,
        })

    results.sort(key=lambda r: r["weekly_score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def classify_weekly(scored: list[dict]) -> dict:
    """Classify into weekly tiers."""
    strong = [s for s in scored if s["weekly_score"] >= 65]
    active = [s for s in scored if 45 <= s["weekly_score"] < 65]
    normal = [s for s in scored if 30 <= s["weekly_score"] < 45]
    weak = [s for s in scored if s["weekly_score"] < 30]
    return {"strong": strong, "active": active, "normal": normal, "weak": weak}


# ──────────────── 报告 ────────────────


def generate_report(scored: list[dict], classified: dict,
                    meta: dict) -> str:
    """Generate MD report."""
    lines = []
    lines.append("## 📅 周主线报告")
    lines.append("")
    lines.append(f"▸ 生成时间: {meta.get('time', '')}")
    lines.append(f"▸ 数据区间: {meta.get('period', '')}")
    lines.append(f"▸ 覆盖板块: {meta.get('total_sectors', 0)} 个")
    lines.append(f"▸ 数据天数: {meta.get('total_dates', 0)} 天")
    lines.append("")

    strong = classified["strong"]
    active = classified["active"]
    weak = classified["weak"]

    if strong:
        lines.append("### 🔥 中期主线（周分≥65）")
        lines.append("")
        lines.append("| 排名 | 板块 | 周分 | 均热度 | 上榜天数 | 趋势 | LHB验证 | 领涨股 |")
        lines.append("|------|------|------|--------|---------|------|---------|--------|")
        for s in strong[:10]:
            lhb = f"{s['lhb_direction']}({s['lhb_net_yi']:+.1f}亿)" if s['lhb_direction'] else "-"
            lines.append(
                f"| {s['rank']} | {s['name']} | **{s['weekly_score']:.0f}** | "
                f"{s['avg_hot']:.0f} | {s['appearance_days']}/{s['total_dates']} | "
                f"{'↑' if s['trend']=='up' else '↓' if s['trend']=='down' else '→'} | "
                f"{lhb} | {s['leader'] or '-'} |")
        lines.append("")

    if active:
        lines.append("### 👀 关注方向（45-64）")
        lines.append("")
        lines.append("| 排名 | 板块 | 周分 | 均热度 | 趋势 | LHB |")
        lines.append("|------|------|------|--------|------|-----|")
        for s in active[:8]:
            lines.append(
                f"| {s['rank']} | {s['name']} | {s['weekly_score']:.0f} | "
                f"{s['avg_hot']:.0f} | "
                f"{'↑' if s['trend']=='up' else '↓' if s['trend']=='down' else '→'} | "
                f"{s['lhb_direction'] or '-'} |")
        lines.append("")

    if weak:
        lines.append("### ❄️ 退潮方向（<30）")
        lines.append("")
        for s in weak[:5]:
            lines.append(f"- {s['name']} 周分{s['weekly_score']:.0f} 均热度{s['avg_hot']:.0f}")
        lines.append("")

    # LHB highlights
    lhb_buy = [s for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1]
    if lhb_buy:
        lines.append("### 🏛️ 机构资金动向")
        lines.append("")
        lines.append("**机构本周净买入板块**:")
        for s in lhb_buy[:5]:
            lines.append(f"- {s['name']} 净买{s['lhb_net_yi']:+.1f}亿 周分{s['weekly_score']:.0f}")
        lines.append("")

    lines.append("---")
    lines.append("> *数据来源: 同花顺热力 + 东方财富持续性 + 龙虎榜 (AKShare)*")
    return "\n".join(lines)


# ──────────────── HTML 报告 ────────────────


def _generate_html_report(scored: list[dict], classified: dict,
                          meta: dict) -> str:
    """Generate HTML report."""
    strong = classified["strong"][:10]
    active = classified["active"][:8]
    weak = classified["weak"][:5]
    lhb_buy = [s for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1][:5]

    def _rows(items, cols):
        rows = ""
        for s in items:
            cells = ""
            for c in cols:
                if c == "rank":
                    cells += f"<td>{s['rank']}</td>"
                elif c == "name":
                    cells += f"<td><strong>{s['name']}</strong></td>"
                elif c == "score":
                    cls = "s-strong" if s['weekly_score'] >= 65 else "s-active" if s['weekly_score'] >= 45 else ""
                    cells += f'<td class="{cls}">{s["weekly_score"]:.0f}</td>'
                elif c == "avg":
                    cells += f"<td>{s['avg_hot']:.0f}</td>"
                elif c == "days":
                    cells += f"<td>{s['appearance_days']}/{s['total_dates']}</td>"
                elif c == "trend":
                    arr = "↑" if s['trend']=='up' else "↓" if s['trend']=='down' else "→"
                    cls_t = "sp" if s['trend']=='up' else "sn" if s['trend']=='down' else ""
                    cells += f'<td class="{cls_t}">{arr}</td>'
                elif c == "lhb":
                    lhb_str = f"{s['lhb_direction']}({s['lhb_net_yi']:+.1f}亿)" if s['lhb_direction'] else "-"
                    cells += f"<td>{lhb_str}</td>"
                elif c == "leader":
                    cells += f"<td>{s.get('leader','-')}</td>"
                elif c == "hot":
                    cells += f"<td>{s['latest_hot']:.0f}</td>"
            rows += f"<tr>{cells}</tr>"
        return rows

    strong_cols = ["rank", "name", "score", "avg", "days", "trend", "lhb", "leader"]
    active_cols = ["rank", "name", "score", "avg", "trend", "lhb"]

    lhb_rows = ""
    for s in lhb_buy:
        lhb_rows += f"<tr><td>{s['name']}</td><td class='sp'>+{s['lhb_net_yi']:.1f}亿</td><td>{s['weekly_score']:.0f}</td><td>{s['latest_hot']:.0f}</td><td>{s.get('leader','-')}</td></tr>"

    weak_list = "".join(f"<li>{s['name']} 周分{s['weekly_score']:.0f}</li>" for s in weak)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>周主线报告 {meta.get('time','')}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1100px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
h1{{font-size:24px;color:#1a1a1a}} h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.sec-strong{{border-left:4px solid #dc2626}}
.sec-active{{border-left:4px solid #1d4ed8}}
.sec-lhb{{border-left:4px solid #7c3aed}}
h2{{margin-top:0}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
.s-strong{{color:#dc2626;font-weight:700}} .s-active{{color:#1d4ed8;font-weight:600}}
.sp{{color:#dc2626;font-weight:600}} .sn{{color:#16a34a;font-weight:600}}
.summary-cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#f9fafb;border-radius:8px;padding:12px 16px;flex:1;min-width:100px;text-align:center}}
.card .num{{font-size:22px;font-weight:700;color:#1d4ed8}}
.card .lbl{{font-size:12px;color:#86868b;margin-top:2px}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:32px}}
</style></head><body><div class="w">
<h1>📅 周主线报告</h1>
<p class="dt">{meta.get('time','')} | {meta.get('period','')} | {meta.get('total_sectors',0)} 板块 | {meta.get('total_dates',0)} 天数据</p>

<div class="summary-cards">
<div class="card"><div class="num" style="color:#dc2626">{len(strong)}</div><div class="lbl">中期主线</div></div>
<div class="card"><div class="num" style="color:#1d4ed8">{len(active)}</div><div class="lbl">关注方向</div></div>
<div class="card"><div class="num" style="color:#a1a1a6">{len(weak)}</div><div class="lbl">退潮方向</div></div>
<div class="card"><div class="num">{len(lhb_buy)}</div><div class="lbl">机构净买板块</div></div>
</div>

{'<div class="sec sec-strong"><h2>🔥 中期主线（周分≥65）</h2><table><thead><tr><th>#</th><th>板块</th><th>周分</th><th>均热度</th><th>上榜</th><th>趋势</th><th>LHB</th><th>领涨</th></tr></thead><tbody>' + _rows(strong[:10], strong_cols) + '</tbody></table></div>' if strong else ''}

{'<div class="sec sec-active"><h2>👀 关注方向（45-64）</h2><table><thead><tr><th>#</th><th>板块</th><th>周分</th><th>均热度</th><th>趋势</th><th>LHB</th></tr></thead><tbody>' + _rows(active[:8], active_cols) + '</tbody></table></div>' if active else ''}

{'<div class="sec sec-lhb"><h2>🏛️ 机构资金动向</h2><table><thead><tr><th>板块</th><th>净买额</th><th>周分</th><th>最新热度</th><th>领涨</th></tr></thead><tbody>' + lhb_rows + '</tbody></table></div>' if lhb_buy else ''}

{'<div class="sec"><h2>❄️ 退潮方向</h2><ul>' + weak_list + '</ul></div>' if weak else ''}

<footer><p class="disc">数据来源: 同花顺热力 + 东方财富持续性 + 龙虎榜 (AKShare) | 仅供学习参考</p></footer>
</div></body></html>"""


# ──────────────── 主流程 ────────────────


def main():
    parser = argparse.ArgumentParser(description="周主线报告")
    parser.add_argument("--weeks", type=int, default=1, help="回溯周数, 默认1周")
    parser.add_argument("--html", action="store_true", help="生成 HTML 报告")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if not HAS_AKSHARE:
        print("⚠️ AKShare 未安装")
        return

    days = args.weeks * 7
    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    period_start = (date.today() - timedelta(days=days)).isoformat()
    period_end = date.today().isoformat()

    print(f"[1/4] 加载市场持续性快照 ({days}天)...")
    market_snapshots = load_market_snapshots(days=days)
    print(f"  {len(market_snapshots)} 天快照")

    print(f"[2/4] 加载龙虎榜快照 ({days}天)...")
    lhb_snapshots = load_lhb_snapshots(days=days)
    print(f"  {len(lhb_snapshots)} 天快照")

    print("[3/4] 获取今日行业热力...")
    today_industries = fetch_industry_data()
    if today_industries:
        print(f"  {len(today_industries)} 个行业")

    print("[4/4] 聚合评分...")
    scored = aggregate_sectors(market_snapshots, lhb_snapshots, today_industries)
    if not scored:
        print("⚠️ 无数据")
        return

    classified = classify_weekly(scored)
    meta = {
        "time": now,
        "period": f"{period_start} ~ {period_end}",
        "total_sectors": len(scored),
        "total_dates": len(market_snapshots) + (1 if today_industries else 0),
    }

    if args.json:
        output = {
            "meta": meta,
            "strong": [{"name": s["name"], "weekly_score": s["weekly_score"],
                        "avg_hot": s["avg_hot"], "trend": s["trend"],
                        "lhb_direction": s["lhb_direction"]}
                       for s in classified["strong"]],
            "active": [{"name": s["name"], "weekly_score": s["weekly_score"]}
                       for s in classified["active"]],
            "lhb_buy": [{"name": s["name"], "net_yi": s["lhb_net_yi"],
                         "weekly_score": s["weekly_score"]}
                        for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report = generate_report(scored, classified, meta)
        print(report)

    if args.html:
        html = _generate_html_report(scored, classified, meta)
        html_path = REPORTS_DIR / f"weekly-{now_ts}.html"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"\nHTML: {html_path}")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
