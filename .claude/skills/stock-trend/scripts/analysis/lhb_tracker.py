#!/usr/bin/env python3
"""龙虎榜机构信号跟踪系统 — 暗线追踪.

每日记录龙虎榜机构净买板块快照，跟踪后续 3/5/10 日表现。
验证机构资金信号是否有效预测板块未来走势。

Usage:
    python3 analysis/lhb_tracker.py                           # 今日快照 + 历史信号验证
    python3 analysis/lhb_tracker.py --history 30              # 回溯 30 天
    python3 analysis/lhb_tracker.py --report                  # 生成跟踪报告
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
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
SNAPSHOT_DIR = CACHE_DIR / "lhb_snapshots"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

from fetchers.longhubang_agg import run_lhb_analysis, fetch_lhb_jgmmtj
from fetchers.sector_mapper import get_mapping


def _safe_float(val) -> float:
    if val is None: return 0.0
    try: return float(val)
    except (ValueError, TypeError): return 0.0


# ──────────────── 快照 ────────────────


def save_daily_snapshot(date_str: Optional[str] = None) -> dict:
    """取今日龙虎榜数据 + 板块行情，保存快照.

    Returns snapshot dict or empty dict on failure.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 取龙虎榜板块数据
    result = run_lhb_analysis(date_str.replace("-", ""))
    sectors = result.get("sectors", [])
    if not sectors:
        return {}

    # 取板块行情（涨跌幅）
    sector_changes = {}
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sector_changes[str(row.get("板块", ""))] = _safe_float(row.get("涨跌幅"))
        df_c = ak.stock_board_concept_summary_ths()
        if df_c is not None and not df_c.empty:
            for _, row in df_c.iterrows():
                sector_changes[str(row.get("概念名称", ""))] = _safe_float(row.get("涨跌幅"))
    except Exception:
        pass

    # 构建快照
    snapshot = {
        "date": date_str,
        "save_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_lhb_stocks": result["meta"].get("total_lhb_stocks", 0),
        "sectors": [],
    }

    for s in sectors:
        name = s["sector_name"]
        snapshot["sectors"].append({
            "sector_code": s["sector_code"],
            "sector_name": name,
            "sector_type": s["sector_type"],
            "lhb_score": s["lhb_score"],
            "direction": s["direction"],
            "inst_net_yi": s["total_inst_net_yi"],
            "stock_count": s["stock_count"],
            "inst_buy_count": s["inst_buy_count"],
            "inst_sell_count": s["inst_sell_count"],
            "member_names": s["member_names"][:3],
            "change_pct": sector_changes.get(name, None),
            "return_3d": None,   # 待回填
            "return_5d": None,
            "return_10d": None,
        })

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = SNAPSHOT_DIR / f"{date_str}.json"
    filepath.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


# ──────────────── 历史加载 ────────────────


def load_history(days: int = 30) -> list[dict]:
    """加载最近 N 天快照."""
    snapshots = []
    today = date.today()
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        fp = SNAPSHOT_DIR / f"{d}.json"
        if fp.exists():
            try:
                snapshots.append(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
    return sorted(snapshots, key=lambda s: s["date"])


def _get_sector_change_pct(name: str, date_str: str) -> Optional[float]:
    """获取某板块在指定日期的涨跌幅."""
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                if str(row.get("板块", "")) == name:
                    return _safe_float(row.get("涨跌幅"))
        df_c = ak.stock_board_concept_summary_ths()
        if df_c is not None and not df_c.empty:
            for _, row in df_c.iterrows():
                if str(row.get("概念名称", "")) == name:
                    return _safe_float(row.get("涨跌幅"))
    except Exception:
        pass
    return None


# ──────────────── 收益回填 ────────────────


def backfill_returns(snapshots: list[dict]) -> list[dict]:
    """对已有快照回填后续 3/5/10 日收益.

    扫描快照中 change_pct 为 None 的板块，查当前涨跌幅填充.
    """
    if not snapshots or not HAS_AKSHARE:
        return snapshots

    now = datetime.now()
    for snap in snapshots:
        snap_date = datetime.strptime(snap["date"], "%Y-%m-%d")
        days_since = (now - snap_date).days

        for sec in snap["sectors"]:
            # 仅回填未填写的窗口
            if days_since >= 3 and sec.get("return_3d") is None:
                pct = _get_sector_change_pct(sec["sector_name"], "")
                if pct is not None:
                    sec["return_3d"] = round(pct, 2)
            if days_since >= 5 and sec.get("return_5d") is None:
                pct = _get_sector_change_pct(sec["sector_name"], "")
                if pct is not None:
                    sec["return_5d"] = round(pct, 2)
            if days_since >= 10 and sec.get("return_10d") is None:
                pct = _get_sector_change_pct(sec["sector_name"], "")
                if pct is not None:
                    sec["return_10d"] = round(pct, 2)

        # 保存回填后的快照
        fp = SNAPSHOT_DIR / f"{snap['date']}.json"
        fp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    return snapshots


# ──────────────── 信号验证 ────────────────


def verify_signals(snapshots: list[dict], min_snapshots: int = 3) -> dict:
    """验证机构信号有效性.

    Returns dict with:
        stats: 总体统计
        by_window: 各窗口表现 {3d: {hit_rate, avg_return_buy, avg_return_sell, ...}}
        top_signals: 最佳/最差信号
    """
    if len(snapshots) < min_snapshots:
        return {"stats": {"note": f"数据不足: {len(snapshots)}/{min_snapshots}"}}

    # 收集所有有收益数据的信号
    signals = {"3d": [], "5d": [], "10d": []}

    for snap in snapshots:
        for sec in snap["sectors"]:
            for w in ["3d", "5d", "10d"]:
                key = f"return_{w}"
                ret = sec.get(key)
                if ret is not None:
                    signals[w].append({
                        "date": snap["date"],
                        "sector": sec["sector_name"],
                        "direction": sec["direction"],
                        "lhb_score": sec["lhb_score"],
                        "inst_net_yi": sec["inst_net_yi"],
                        "return": ret,
                        "correct": (sec["direction"] == "净买" and ret > 0) or \
                                   (sec["direction"] == "净卖" and ret < 0),
                    })

    result = {}
    for w, sigs in signals.items():
        if not sigs:
            continue
        buy_signals = [s for s in sigs if s["direction"] == "净买"]
        sell_signals = [s for s in sigs if s["direction"] == "净卖"]

        buy_avg = sum(s["return"] for s in buy_signals) / len(buy_signals) if buy_signals else 0
        sell_avg = sum(s["return"] for s in sell_signals) / len(sell_signals) if sell_signals else 0
        buy_hit = sum(1 for s in buy_signals if s["correct"]) / len(buy_signals) * 100 if buy_signals else 0
        sell_hit = sum(1 for s in sell_signals if s["correct"]) / len(sell_signals) * 100 if sell_signals else 0
        all_hit = sum(1 for s in sigs if s["correct"]) / len(sigs) * 100

        result[w] = {
            "total_signals": len(sigs),
            "buy_count": len(buy_signals),
            "sell_count": len(sell_signals),
            "buy_avg_return": round(buy_avg, 2),
            "sell_avg_return": round(sell_avg, 2),
            "buy_hit_rate": round(buy_hit, 1),
            "sell_hit_rate": round(sell_hit, 1),
            "overall_hit_rate": round(all_hit, 1),
        }

    return {"signals": result}


# ──────────────── 报告 ────────────────


def generate_tracker_report(snapshots: list[dict], signal_analysis: dict) -> str:
    """生成跟踪报告."""
    lines = []
    lines.append("## 龙虎榜机构信号跟踪")
    lines.append("")
    lines.append(f"▸ 快照天数: {len(snapshots)}")
    if snapshots:
        lines.append(f"▸ 区间: {snapshots[0]['date']} ~ {snapshots[-1]['date']}")
    lines.append("")

    signals = signal_analysis.get("signals", {})
    if signals:
        lines.append("### 信号有效性")
        lines.append("")
        lines.append("| 窗口 | 信号数 | 买入 | 卖出 | 买入平均收益 | 卖出平均收益 | 买入胜率 | 卖出胜率 | 总胜率 |")
        lines.append("|------|--------|------|------|-------------|-------------|---------|---------|-------|")
        for w in ["3d", "5d", "10d"]:
            sw = signals.get(w)
            if not sw:
                continue
            lines.append(
                f"| {w} | {sw['total_signals']} | {sw['buy_count']} | {sw['sell_count']} | "
                f"{sw['buy_avg_return']:+.2f}% | {sw['sell_avg_return']:+.2f}% | "
                f"{sw['buy_hit_rate']}% | {sw['sell_hit_rate']}% | {sw['overall_hit_rate']}% |"
            )
        lines.append("")

    # 最近快照详情
    if snapshots:
        latest = snapshots[-1]
        lines.append(f"### 最近快照 ({latest['date']})")
        lines.append("")
        lines.append("| 板块 | 方向 | 评分 | 机构净买(亿) | 上榜股 | 今日涨跌 |")
        lines.append("|------|------|------|-------------|--------|---------|")
        for sec in latest["sectors"][:10]:
            chg = sec.get("change_pct")
            chg_str = f"{chg:+.2f}%" if chg is not None else "-"
            lines.append(
                f"| {sec['sector_name']} | {sec['direction']} | {sec['lhb_score']:.0f} | "
                f"{sec['inst_net_yi']:+.2f} | {sec['stock_count']} | {chg_str} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("> *数据来源: 东方财富龙虎榜 (AKShare) | 仅供学习参考*")
    return "\n".join(lines)


# ──────────────── HTML 报告 ────────────────


def _generate_lhb_html_report(snapshots: list[dict],
                               signal_analysis: dict,
                               date_str: str) -> str:
    """Generate HTML report with signal visualization."""
    signals = signal_analysis.get("signals", {})
    all_sigs = []
    for w in ["3d", "5d", "10d"]:
        for s in signals.get(w, []):
            all_sigs.append({**s, "window": w})

    sig_rows = ""
    for s in sorted(all_sigs, key=lambda x: abs(x.get("return", 0)), reverse=True)[:30]:
        cls = "sig-correct" if s["correct"] else "sig-wrong"
        mark = "✅" if s["correct"] else "❌"
        ret_cls = "sp" if s["return"] > 0 else "sn"
        sig_rows += (
            f"<tr class='{cls}'><td>{s['date']}</td><td>{s['window']}</td>"
            f"<td><strong>{s['sector']}</strong></td><td>{s['direction']}</td>"
            f"<td>{mark}</td><td class='{ret_cls}'>{s['return']:+.2f}%</td>"
            f"<td>{s.get('lhb_score',0):.0f}</td><td>{s.get('inst_net_yi',0):+.2f}</td></tr>"
        )

    chart_data = json.dumps({
        "windows": list(signals.keys()),
        "buy_returns": [signals[w]["buy_avg_return"] for w in signals],
        "sell_returns": [signals[w]["sell_avg_return"] for w in signals],
        "buy_hits": [signals[w]["buy_hit_rate"] for w in signals],
        "sell_hits": [signals[w]["sell_hit_rate"] for w in signals],
        "overall_hits": [signals[w]["overall_hit_rate"] for w in signals],
    }) if signals else "{}"

    latest = snapshots[-1] if snapshots else None
    latest_rows = ""
    if latest:
        for sec in latest["sectors"][:10]:
            chg = sec.get("change_pct")
            chg_str = f"{chg:+.2f}%" if chg is not None else "-"
            dc = "sp" if sec["direction"] == "净买" else "sn"
            latest_rows += (
                f"<tr><td>{sec['sector_name']}</td><td class='{dc}'>{sec['direction']}</td>"
                f"<td>{sec['lhb_score']:.0f}</td><td>{sec['inst_net_yi']:+.2f}亿</td>"
                f"<td>{sec['stock_count']}</td><td>{chg_str}</td></tr>"
            )

    now = date_str or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>龙虎榜信号跟踪 {now}</title>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1100px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
h1{{font-size:24px;color:#1a1a1a}} h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.card{{background:#f9fafb;border-radius:8px;padding:12px 16px;text-align:center}}
.card .num{{font-size:22px;font-weight:700;color:#1d4ed8}}
.card .lbl{{font-size:12px;color:#86868b;margin-top:2px}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb;border-left:4px solid #7c3aed}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px}}
th{{background:#7c3aed;color:#fff;font-weight:600;font-size:12px}}
tr.sig-correct td{{background:#f0fdf4}} tr.sig-wrong td{{background:#fef2f2}}
.sp{{color:#dc2626;font-weight:600}} .sn{{color:#16a34a;font-weight:600}}
.chart-box{{width:100%;height:350px;margin:12px 0}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}}
.summary .card{{flex:1;min-width:100px}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:32px}}
</style></head><body><div class="w">
<header><h1>🏛️ 龙虎榜机构信号跟踪</h1><p class="dt">{now}</p></header>
<div class="summary">
<div class="card"><div class="num">{len(snapshots)}</div><div class="lbl">快照天数</div></div>
<div class="card"><div class="num">{sum(len(s.get('sectors',[])) for s in snapshots) if snapshots else 0}</div><div class="lbl">信号总数</div></div>
<div class="card"><div class="num">{sum(1 for s in all_sigs if s['correct']) if all_sigs else 0}</div><div class="lbl">正确</div></div>
<div class="card"><div class="num">{sum(1 for s in all_sigs if not s['correct']) if all_sigs else 0}</div><div class="lbl">错误</div></div>
</div>
<div class="sec"><h2>📊 信号收益 (买入 vs 卖出)</h2><div id="returnChart" class="chart-box"></div></div>
<div class="sec"><h2>📊 信号胜率</h2><div id="hitChart" class="chart-box"></div></div>
<div class="sec"><h2>📋 信号明细 (Top 30)</h2><table><thead><tr><th>日期</th><th>窗口</th><th>板块</th><th>方向</th><th>结果</th><th>收益</th><th>评分</th><th>净额(亿)</th></tr></thead><tbody>{sig_rows}</tbody></table></div>
<div class="sec"><h2>📋 最近快照</h2><table><thead><tr><th>板块</th><th>方向</th><th>评分</th><th>净买(亿)</th><th>上榜</th><th>涨跌</th></tr></thead><tbody>{latest_rows}</tbody></table></div>
<footer><p class="disc">数据来源: 东方财富龙虎榜 (AKShare) | 仅供学习参考</p></footer></div>
<script>
var cd = {chart_data};
if (cd.windows && cd.windows.length > 0) {{
  Plotly.newPlot('returnChart', [
    {{x:cd.windows, y:cd.buy_returns, type:'bar', name:'买入均收益', marker:{{color:'#dc2626'}}}},
    {{x:cd.windows, y:cd.sell_returns, type:'bar', name:'卖出均收益', marker:{{color:'#16a34a'}}}}
  ], {{barmode:'group', margin:{{l:50,r:20,t:10,b:40}}, yaxis:{{title:'收益(%)'}}, plot_bgcolor:'#fafafa', paper_bgcolor:'#fafafa', font:{{family:'PingFang SC, Microsoft YaHei', size:13}}}}, {{responsive:true, displayModeBar:false}});
  Plotly.newPlot('hitChart', [
    {{x:cd.windows, y:cd.buy_hits, mode:'lines+markers', name:'买入胜率', line:{{color:'#dc2626',width:2}}, marker:{{size:8}}}},
    {{x:cd.windows, y:cd.sell_hits, mode:'lines+markers', name:'卖出胜率', line:{{color:'#16a34a',width:2}}, marker:{{size:8}}}},
    {{x:cd.windows, y:cd.overall_hits, mode:'lines+markers', name:'总胜率', line:{{color:'#7c3aed',width:3}}, marker:{{size:10}}}}
  ], {{margin:{{l:50,r:20,t:10,b:40}}, yaxis:{{title:'胜率(%)', range:[0,100]}}, plot_bgcolor:'#fafafa', paper_bgcolor:'#fafafa', font:{{family:'PingFang SC, Microsoft YaHei', size:13}}}}, {{responsive:true, displayModeBar:false}});
}}
</script></body></html>"""


# ──────────────── 主流程 ────────────────


def main():
    parser = argparse.ArgumentParser(description="龙虎榜机构信号跟踪")
    parser.add_argument("--history", type=int, default=30, help="回溯天数")
    parser.add_argument("--report", action="store_true", help="生成跟踪报告")
    parser.add_argument("--html", action="store_true", help="生成 HTML 报告")
    parser.add_argument("--snapshot-only", action="store_true", help="仅保存今日快照")
    args = parser.parse_args()

    if not HAS_AKSHARE:
        print("⚠️ AKShare 未安装")
        return

    # 1. 保存今日快照
    print("[Phase 1/3] 保存今日龙虎榜快照...")
    snap = save_daily_snapshot()
    if snap:
        print(f"  ✓ 快照已保存 ({snap['date']}, { snap['total_lhb_stocks']} 只股票, {len(snap['sectors'])} 板块)")
    else:
        print("  ⚠️ 今日无龙虎榜数据（非交易日）")

    if args.snapshot_only:
        return

    # 2. 加载历史 + 回填收益
    print(f"[Phase 2/3] 加载历史快照 ({args.history} 天)...")
    snapshots = load_history(args.history)
    if not snapshots:
        print("  ⚠️ 历史快照为空")
        return

    print(f"  ✓ 加载 {len(snapshots)} 天快照，回填收益...")
    snapshots = backfill_returns(snapshots)
    filled = sum(1 for s in snapshots for sec in s["sectors"]
                 if sec.get("return_3d") is not None)
    print(f"  ✓ 回填完成 ({filled} 条收益数据)")

    # 3. 信号验证
    print("[Phase 3/3] 信号验证...")
    analysis = verify_signals(snapshots)
    signals = analysis.get("signals", {})

    if signals:
        print()
        print("信号有效性:")
        print(f"{'窗口':<4} {'信号数':<8} {'买入':<6} {'卖出':<6} {'买均收益':<10} {'卖均收益':<10} {'买胜率':<8} {'卖胜率':<8} {'总胜率':<8}")
        print("-" * 70)
        for w in ["3d", "5d", "10d"]:
            sw = signals.get(w)
            if sw:
                print(f"{w:<4} {sw['total_signals']:<8} {sw['buy_count']:<6} {sw['sell_count']:<6} "
                      f"{sw['buy_avg_return']:+.2f}%{'':<5} {sw['sell_avg_return']:+.2f}%{'':<5} "
                      f"{sw['buy_hit_rate']}%{'':<5} {sw['sell_hit_rate']}%{'':<5} {sw['overall_hit_rate']}%")
        print()
    else:
        note = analysis.get("stats", {}).get("note", "")
        print(f"  ⚠️ {note}")

    # 4. 报告（可选）
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.report:
        report = generate_tracker_report(snapshots, analysis)
        report_path = REPORTS_DIR / f"lhb-tracker-{now_ts}.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"MD报告: {report_path}")

    if args.html:
        html = _generate_lhb_html_report(snapshots, analysis,
                                         datetime.now().strftime("%Y-%m-%d %H:%M"))
        html_path = REPORTS_DIR / f"lhb-tracker-{now_ts}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML报告: {html_path}")

    if not args.report and not args.html:
        print(generate_tracker_report(snapshots, analysis))

    # 5. 最近快照详情
    if snapshots:
        latest = snapshots[-1]
        if latest.get("sectors"):
            print(f"\n最近快照 ({latest['date']}) — 龙虎榜板块 Top 5:")
            for sec in latest["sectors"][:5]:
                chg = sec.get("change_pct")
                chg_str = f"{chg:+.2f}%" if chg is not None else "-"
                print(f"  {sec['direction']} {sec['sector_name']}: "
                      f"评分{sec['lhb_score']:.0f} 净额{sec['inst_net_yi']:+.2f}亿 "
                      f"今日{chg_str}")


if __name__ == "__main__":
    main()
