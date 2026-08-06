#!/usr/bin/env python3
"""每日候选股 /candidates — 每天自动筛出 20~30 只维科夫买点候选.

自动选热点板块(板块排行 hot_score)→ stock_scanner 维科夫漏斗扫成分股 →
批量扩展直到 ≥ min-candidates → 按综合分排序取 top ~30 → 每日候选报告。

Usage:
    python3 daily_candidates.py [--top 30] [--min-candidates 20] [--min-score 50] [--json]
    python3 daily_candidates.py --sectors BK0420,BK0897   # 手动指定板块(覆盖自动选)
"""

import os
import sys
import json
import argparse
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = Path(os.environ.get("STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

from scans.stock_scanner import gather_candidates, run_phase2
from core.cache_utils import is_trading_hours

SIGNAL_LABELS = {
    "volume_breakout": "放量突破",
    "northbound_adding": "北向增持",
}


def pick_hot_sectors(top_n=20, min_hot=45, min_stocks=10):
    """Pick hot sectors from live rankings (hot_score min-max 0-100)."""
    from fetchers.sector_data import get_sector_rankings, rank_hot_sectors
    rankings = get_sector_rankings()
    hot = rank_hot_sectors(rankings, top_n=top_n, min_stocks=min_stocks)
    return [(s["code"], s["name"], s.get("hot_score", 0)) for s in hot]


def scan_sectors(sector_codes, batch_size=4, per_sector=25, min_candidates=20):
    """Run Wyckoff funnel across sectors in batches; expand until enough candidates."""
    all_scored = {}
    for i in range(0, len(sector_codes), batch_size):
        batch = sector_codes[i:i + batch_size]
        try:
            phase1 = gather_candidates(batch, top_n_per_sector=per_sector)
        except Exception as e:
            print(f"  ⚠️ 板块 {batch} 汇聚失败: {e}", file=sys.stderr)
            continue
        if not phase1["candidates"]:
            continue
        scored = run_phase2(phase1["candidates"], enable_wyckoff=True)
        for s in scored:
            all_scored[s["code"]] = s
        print(f"  批次完成,当前候选 {len(all_scored)} 只", file=sys.stderr)
        if len(all_scored) >= min_candidates:
            break
    return list(all_scored.values())


def _signal_text(signals):
    """Render signals dict to Chinese-readable string (bools → labels)."""
    parts = []
    for k, v in signals.items():
        if k in SIGNAL_LABELS:
            parts.append(SIGNAL_LABELS[k])
        elif isinstance(v, bool):
            parts.append(k)
        else:
            parts.append(str(v))
    return "、".join(parts) or "-"


def _append_candidate_table(lines, title, items, empty_text):
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append(f"> {empty_text}")
        return
    lines.extend([
        "| # | 名称(代码) | 板块 | 买点 | 置信度 | 综合分 | 覆盖率 | 信号 |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        lines.append(
            f"| {index} | {item['name']}({item['code']}) | "
            f"{item['sector_name']} | {wyckoff.get('sub_phase', '-')} | "
            f"{wyckoff.get('confidence', 0):.0%} | "
            f"{item['composite_score']:.1f} | "
            f"{quality.get('coverage', 0):.0%} | "
            f"{_signal_text(item.get('signals', {}))} |"
        )


def load_regime_context():
    """Return market-regime summary line if today's context exists."""
    try:
        p = CACHE_DIR / "market_regime.json"
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        r = d.get("regime", {})
        return {
            "score": r.get("score"),
            "label": r.get("label", ""),
            "data_date": d.get("data_date", ""),
            "advice": r.get("advice", ""),
        }
    except Exception:
        return None


def build_recommendation_policy(regime, expected_date, market_open=False):
    if market_open:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["intraday_provisional"],
        }
    if not regime or regime.get("score") is None:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_missing"],
        }
    if regime.get("data_date") != expected_date:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_stale"],
        }
    score = float(regime["score"])
    if score < 60:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_weak"],
        }
    if score < 80:
        return {
            "mode": "waiting_trigger", "max_recommendations": 2,
            "max_portfolio_pct": 30, "reasons": [],
        }
    return {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
    }


def classify_candidates(candidates, policy):
    eligible = [
        item for item in candidates
        if item.get("data_quality", {}).get("eligible", False)
    ]
    limit = policy.get("max_recommendations", 0)
    actionable = eligible[:limit] if policy.get("mode") == "actionable" else []
    waiting = eligible[:limit] if policy.get("mode") == "waiting_trigger" else []
    promoted = {item["code"] for item in actionable + waiting}
    return {
        "actionable": actionable,
        "waiting_trigger": waiting,
        "observation": [
            item for item in candidates if item.get("code") not in promoted
        ],
    }


def generate_report(candidates, sector_codes, elapsed, policy, buckets):
    lines = [
        "# 每日候选股",
        "",
        f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | "
        f"耗时 {elapsed:.0f}s",
        "",
        f"**推荐模式**: {policy['mode']} | "
        f"推荐上限 {policy['max_recommendations']} 只 | "
        f"组合仓位上限 {policy['max_portfolio_pct']}%",
    ]
    regime = load_regime_context()
    if regime and regime.get("score") is not None:
        lines.extend([
            "",
            f"**市场环境**: {regime['score']} {regime['label']} "
            f"(数据 {regime['data_date']}) — {regime.get('advice', '')}",
        ])
    if policy.get("reasons"):
        lines.extend(["", f"> ⚠️ 推荐降级: {', '.join(policy['reasons'])}"])
    _append_candidate_table(
        lines, "今日可执行", buckets["actionable"], "今日无可执行推荐。")
    _append_candidate_table(
        lines, "等待触发", buckets["waiting_trigger"], "暂无等待触发标的。")
    _append_candidate_table(
        lines, "观察池", buckets["observation"], "观察池为空。")
    lines.extend([
        "", "---", "",
        "*候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。*",
        "",
        "**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**",
    ])
    return "\n".join(lines)


def _html_candidate_rows(items):
    if not items:
        return '<tr><td colspan="8">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        rows.append(
            f"<tr><td>{index}</td><td><strong>{item['name']}</strong><br>"
            f"<span style='color:#86868b;font-size:12px'>{item['code']}</span></td>"
            f"<td>{item['sector_name']}</td>"
            f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
            f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
            f"<td><strong>{item['composite_score']:.1f}</strong></td>"
            f"<td>{quality.get('coverage', 0):.0%}</td>"
            f"<td>{_signal_text(item.get('signals', {}))}</td></tr>"
        )
    return "".join(rows)


def _generate_html(candidates, sector_codes, elapsed, ts, policy, buckets):
    """Lightweight HTML mirror of the MD report."""
    regime = load_regime_context()
    weak = bool(regime and regime["score"] is not None and regime["score"] < 60)
    actionable_rows = _html_candidate_rows(buckets["actionable"])
    waiting_rows = _html_candidate_rows(buckets["waiting_trigger"])
    observation_rows = _html_candidate_rows(buckets["observation"])
    policy_note = (
        f"推荐模式 {policy['mode']} | 推荐上限 "
        f"{policy['max_recommendations']}只 | 组合仓位上限 "
        f"{policy['max_portfolio_pct']}%"
    )

    regime_html = ""
    if regime and regime["score"] is not None:
        color = {"强势": "#dc2626", "中性": "#d97706", "弱势": "#16a34a"}.get(regime["label"], "#86868b")
        weak_note = ('<p style="color:#b45309;font-weight:600">⚠️ 弱势市:候选仅作观察,'
                     '不宜建仓,等大盘站回 MA20。</p>' if weak else "")
        regime_html = (
            f"<div class='score' style='color:{color}'>市场环境 {regime['score']} {regime['label']}"
            f"<span style='font-size:14px;color:#86868b'> (数据 {regime['data_date']})</span></div>"
            f"{weak_note}"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日候选股 {ts}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1000px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:32px 36px}}
h1{{font-size:24px}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.score{{font-size:26px;font-weight:800;margin:14px 0}}
table{{width:100%;border-collapse:collapse;margin:12px 0;border-radius:8px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-size:13px}}
.buy{{color:#dc2626;font-weight:600}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:28px}}
</style></head><body><div class="w">
<h1>📋 每日候选股 {datetime.now().strftime('%Y-%m-%d')}</h1>
<p class="dt">生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | 耗时 {elapsed:.0f}s</p>
{regime_html}

<p class="dt">{policy_note}</p>
<h2 style="font-size:18px;margin:18px 0 8px">今日可执行</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{actionable_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">等待触发</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{waiting_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{observation_rows}</tbody></table>

<footer><p class="disc">候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。<br><strong>本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。</strong></p></footer>
</div></body></html>"""


def build_json_output(candidates, sector_codes, elapsed, policy, buckets):
    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "sector_count": len(sector_codes),
            "candidate_count": len(candidates),
            "elapsed_seconds": round(elapsed, 1),
        },
        "policy": policy,
        "candidates": candidates,
        "recommendations": buckets["actionable"],
        "waiting_trigger": buckets["waiting_trigger"],
        "observation": buckets["observation"],
    }


def main():
    parser = argparse.ArgumentParser(description="每日候选股 — 自动筛出维科夫买点候选")
    parser.add_argument("--top", type=int, default=30, help="输出上限(默认30)")
    parser.add_argument("--min-candidates", type=int, default=20,
                        help="候选数量下限,不足则扩展板块(默认20)")
    parser.add_argument("--min-score", type=float, default=50, help="最低综合分(默认50)")
    parser.add_argument("--sectors", type=str,
                        help="手动指定板块,逗号分隔(覆盖自动选板块)")
    parser.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    parser.add_argument("--html", dest="html", action="store_true", default=True,
                        help="(默认) 生成 HTML 报告")
    parser.add_argument("--no-html", dest="html", action="store_false",
                        help="不生成 HTML(仅 MD)")
    args = parser.parse_args()

    start = time.time()
    regime = load_regime_context()
    expected_date = datetime.now().strftime("%Y-%m-%d")
    policy = build_recommendation_policy(
        regime, expected_date, market_open=is_trading_hours())

    # 板块来源
    if args.sectors:
        sector_codes = [(c.strip(), c.strip(), 0)
                        for c in args.sectors.split(",") if c.strip()]
        print(f"手动板块 {len(sector_codes)} 个: {[c[0] for c in sector_codes]}",
              file=sys.stderr)
    else:
        print("[1/3] 拉取热点板块...", file=sys.stderr)
        sector_codes = pick_hot_sectors()
        if not sector_codes:
            print("⚠️ 无热点板块,候选为空", file=sys.stderr)
            sys.exit(1)
        print(f"  热点板块 {len(sector_codes)} 个: "
              f"{[f'{n}({h:.0f})' for _, n, h in sector_codes[:5]]}...",
              file=sys.stderr)

    # 漏斗扫描
    print("[2/3] 维科夫漏斗扫描成分股...", file=sys.stderr)
    scored = scan_sectors([c[0] for c in sector_codes],
                          min_candidates=args.min_candidates)

    # 过滤 + 排序 + 归一化到 top
    scored = [s for s in scored if s["composite_score"] >= args.min_score]
    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    candidates = scored[:args.top]
    buckets = classify_candidates(candidates, policy)

    elapsed = time.time() - start

    if args.json:
        out = build_json_output(candidates, sector_codes, elapsed, policy, buckets)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 报告
    report = generate_report(candidates, sector_codes, elapsed, policy, buckets)
    print(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"candidates-{ts}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n候选报告: {path}")

    if args.html:
        try:
            html = _generate_html(
                candidates, sector_codes, elapsed, ts, policy, buckets)
            html_path = REPORTS_DIR / f"candidates-{ts}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"HTML: {html_path}")
        except Exception as e:
            print(f"⚠️ HTML 生成失败: {e}")

    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
