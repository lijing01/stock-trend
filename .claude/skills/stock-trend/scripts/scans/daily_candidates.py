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

SIGNAL_LABELS = {
    "volume_breakout": "放量突破",
    "northbound_adding": "北向增持",
}


def pick_hot_sectors(top_n=12, min_hot=45, min_stocks=10):
    """Pick hot sectors from live rankings (hot_score min-max 0-100)."""
    from fetchers.sector_data import get_sector_rankings, rank_hot_sectors
    rankings = get_sector_rankings()
    hot = rank_hot_sectors(rankings, top_n=top_n, min_stocks=min_stocks)
    return [(s["code"], s["name"], s.get("hot_score", 0)) for s in hot]


def scan_sectors(sector_codes, batch_size=4, per_sector=15, min_candidates=20):
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


def generate_report(candidates, sector_codes, elapsed):
    lines = []
    lines.append("# 每日候选股")
    lines.append("")
    lines.append(f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                 f"扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | "
                 f"耗时 {elapsed:.0f}s")

    regime = load_regime_context()
    if regime and regime["score"] is not None:
        lines.append("")
        lines.append(f"**市场环境**: {regime['score']} {regime['label']}"
                     f"(数据 {regime['data_date']}) — {regime.get('advice', '')}")
        if regime["score"] < 60:
            lines.append("")
            lines.append("> ⚠️ **弱势市**:候选仅作观察,不宜建仓,等大盘站回 MA20。")

    lines.append("")
    if len(candidates) < 20:
        lines.append("")
        lines.append(f"> ⚠️ 候选仅 {len(candidates)} 只(目标 20-30):买点稀缺或板块覆盖不足,"
                     f"可加 `--sectors` 或调低 `--min-score`。")

    lines.append("")
    lines.append("## 维科夫买点候选")
    lines.append("")
    lines.append("| # | 名称(代码) | 板块 | 买点 | 置信度 | 综合分 | 信号 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, s in enumerate(candidates, 1):
        wk = s.get("wyckoff", {})
        buy = wk.get("sub_phase", "-")
        conf = wk.get("confidence", 0)
        sig = _signal_text(s.get("signals", {}))
        lines.append(
            f"| {i} | {s['name']}({s['code']}) | {s['sector_name']} | "
            f"{buy} | {conf:.0%} | {s['composite_score']:.1f} | {sig} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*候选为维科夫吸筹/拉升买点 + 多维打分排序,需人工复核确认。仅供学习参考,不构成投资建议。*")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="每日候选股 — 自动筛出维科夫买点候选")
    parser.add_argument("--top", type=int, default=30, help="输出上限(默认30)")
    parser.add_argument("--min-candidates", type=int, default=20,
                        help="候选数量下限,不足则扩展板块(默认20)")
    parser.add_argument("--min-score", type=float, default=50, help="最低综合分(默认50)")
    parser.add_argument("--sectors", type=str,
                        help="手动指定板块,逗号分隔(覆盖自动选板块)")
    parser.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    args = parser.parse_args()

    start = time.time()

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

    elapsed = time.time() - start

    if args.json:
        out = {
            "meta": {
                "generated_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
                "sector_count": len(sector_codes),
                "candidate_count": len(candidates),
                "elapsed_seconds": round(elapsed, 1),
            },
            "candidates": candidates,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 报告
    report = generate_report(candidates, sector_codes, elapsed)
    print(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"candidates-{ts}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n候选报告: {path}")
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
