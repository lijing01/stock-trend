#!/usr/bin/env python3
"""涨停复盘数据获取 (zt_replay) — AKShare 东方财富数据源.

替代原同花顺直爬方案（当前环境 403），改用 AKShare 东方财富涨停池 API.

数据源:
  - stock_zt_pool_em() — 今日涨停板池
  - stock_zt_pool_strong_em() — 强势涨停（60日新高等）
  - sector_mapper — 股票→概念板块映射（替代同花顺概念标签）

Usage:
    python3 zt_replay.py                          # 今日涨停榜
    python3 zt_replay.py --date 2026-05-29        # 历史某日
    python3 zt_replay.py --aggregate concepts      # 按概念聚合输出
    python3 zt_replay.py --json                    # JSON 输出
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
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

from fetchers.sector_mapper import get_mapping as get_sector_mapping


# ──────────────── 工具函数 ────────────────


def _safe_float(val) -> float:
    if val is None: return 0.0
    try: return float(val)
    except (ValueError, TypeError): return 0.0


def _safe_int(val) -> int:
    if val is None: return 0
    try: return int(val)
    except (ValueError, TypeError): return 0


def _detect_board(code: str) -> str:
    if code.startswith("6") or code.startswith("9"): return "sh"
    if code.startswith("0") or code.startswith("3"): return "sz"
    if code.startswith("8") or code.startswith("4"): return "bj"
    return "unknown"


def _classify_limit_time(t: Optional[str]) -> str:
    """Classify limit-up timing bucket from HHMMSS or HH:MM."""
    if not t:
        return "unknown"
    t = t.replace(":", "")
    if len(t) < 4:
        return "unknown"
    try:
        h, m = int(t[:2]), int(t[2:4])
        minutes = h * 60 + m
        if minutes < 9 * 60 + 30: return "pre_open"
        elif minutes < 11 * 60 + 30: return "morning_early"
        elif minutes < 13 * 60: return "morning_late"
        elif minutes < 14 * 60: return "afternoon"
        elif minutes < 15 * 60: return "afternoon_late"
        return "close"
    except (ValueError, IndexError):
        return "unknown"


def _classify_limit_type(blown_count: int,
                         first_time: Optional[str],
                         last_time: Optional[str]) -> str:
    """Determine limit type from blow-off count and times."""
    if blown_count is None:
        blown_count = 0
    if blown_count > 0:
        # If it re-sealed (different first/last time), it's a retest
        if first_time and last_time and first_time != last_time:
            return "retest"
        return "blown"
    return "firm"


def _timing_to_str(t) -> Optional[str]:
    """Convert AKShare time (int/str) to HH:MM string."""
    if t is None:
        return None
    s = str(t).strip()
    if len(s) == 6:
        return f"{s[:2]}:{s[2:4]}"
    if len(s) == 4:
        return f"{s[:2]}:{s[2:4]}"
    if ":" in s:
        return s[:5]
    return None


# ──────────────── 概念映射缓存 ────────────────

_concept_cache = None  # stock_code → [concept_names]


def _load_concept_map() -> dict[str, list[str]]:
    """Build stock→concept map from sector_mapper cache."""
    global _concept_cache
    if _concept_cache is not None:
        return _concept_cache

    mapping = get_sector_mapping()
    if not mapping:
        _concept_cache = {}
        return _concept_cache

    stock_map = mapping.get("mapping", {})
    result = {}
    for code, sectors in stock_map.items():
        concepts = [s["name"] for s in sectors if s["type"] == "concept"]
        if concepts:
            result[code] = concepts
    _concept_cache = result
    return result


def _lookup_concepts(code: str, industry: str = "") -> list[str]:
    """Look up concept tags for a stock code.

    Uses sector_mapper cache (BK concept sectors).
    Falls back to industry name if no concept mapping found.
    """
    cmap = _load_concept_map()
    concepts = cmap.get(code, [])
    if not concepts and industry:
        concepts = [industry]
    return concepts


# ──────────────── 核心：获取涨停数据 ────────────────


def fetch_limitup_stocks(date_str: Optional[str] = None) -> list[dict]:
    """Fetch limit-up stock list from AKShare 东方财富涨停池.

    Args:
        date_str: "YYYY-MM-DD" or None for today.

    Returns:
        List of dicts:
            code, name, concepts (list), first_limit_time, last_limit_time,
            limit_streak, seal_amount, limit_type, timing_bucket, board,
            blown_count, industry
        Empty list on failure (non-trading day / no data).
    """
    if not HAS_AKSHARE:
        return []

    if date_str is None:
        dt = datetime.now().strftime("%Y%m%d")
    else:
        dt = date_str.replace("-", "")

    try:
        df = ak.stock_zt_pool_em(date=dt)
        if df is None or df.empty:
            return []
    except Exception:
        return []

    # Preload concept mapping
    _load_concept_map()

    results = []
    seen_codes = set()

    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        name = str(row.get("名称", ""))
        industry = str(row.get("所属行业", ""))

        # Seal amount (封板资金) — yuan
        seal_amount = _safe_float(row.get("封板资金", 0))

        # Times
        first_time = _timing_to_str(row.get("首次封板时间"))
        last_time = _timing_to_str(row.get("最后封板时间"))

        # Limit streak
        limit_streak = _safe_int(row.get("连板数", 1))
        if limit_streak < 1:
            limit_streak = 1

        # Blown count
        blown_count = _safe_int(row.get("炸板次数", 0))

        # Limit type
        limit_type = _classify_limit_type(blown_count, first_time, last_time)

        # Timing bucket
        timing_bucket = _classify_limit_time(first_time)

        # Board
        board = _detect_board(code)

        # Concepts — from sector_mapper, fallback to industry
        concepts = _lookup_concepts(code, industry)

        results.append({
            "code": code,
            "name": name,
            "concepts": concepts,
            "first_limit_time": first_time,
            "last_limit_time": last_time,
            "limit_streak": limit_streak,
            "seal_amount": seal_amount,
            "limit_type": limit_type,
            "timing_bucket": timing_bucket,
            "board": board,
            "blown_count": blown_count,
            "industry": industry,
        })

    # Sort: higher streak first, earlier limit time
    results.sort(key=lambda r: (-r["limit_streak"],
                                r["first_limit_time"] or "99:99"))
    return results


# ──────────────── 聚合函数 ────────────────


def aggregate_by_concept(stocks: list[dict]) -> list[dict]:
    """Aggregate limit-up stocks by concept tag.

    Returns ranked list of concepts sorted by涨停家数 descending.
    """
    concept_map = defaultdict(list)
    for s in stocks:
        if s["concepts"]:
            for c in s["concepts"]:
                concept_map[c].append(s)
        else:
            concept_map["其他"].append(s)

    results = []
    for concept, members in concept_map.items():
        streaks = [m["limit_streak"] for m in members if m["limit_streak"] > 1]
        members.sort(key=lambda m: (-m["limit_streak"], m.get("first_limit_time") or "99:99"))
        results.append({
            "concept": concept,
            "stock_count": len(members),
            "continuous_count": len(streaks),
            "max_streak": max(streaks) if streaks else 1,
            "early_morning_count": sum(1 for m in members
                                       if m.get("timing_bucket") in ("pre_open", "morning_early")),
            "seal_amount_total": sum(m["seal_amount"] for m in members),
            "members": [{"code": m["code"], "name": m["name"],
                         "limit_streak": m["limit_streak"],
                         "first_limit_time": m["first_limit_time"]}
                        for m in members[:5]],
        })

    results.sort(key=lambda r: (-r["stock_count"], -r["max_streak"]))
    return results


def aggregate_by_limit_streak(stocks: list[dict]) -> dict[int, int]:
    """Aggregate limit-up stocks by连板高度.

    Returns {streak: count}, e.g. {1: 25, 2: 8, 3: 3, 4: 1}
    """
    counter = Counter()
    for s in stocks:
        counter[s["limit_streak"]] += 1
    return dict(sorted(counter.items(), reverse=True))


# ──────────────── 报告 ────────────────


def format_report(stocks: list[dict],
                  concepts: list[dict],
                  streak_dist: dict[int, int],
                  date_str: str) -> str:
    """Generate Markdown report."""
    lines = []
    lines.append(f"## 涨停复盘 {date_str}")
    lines.append("")

    total = len(stocks)
    firm = sum(1 for s in stocks if s["limit_type"] == "firm")
    blown = sum(1 for s in stocks if s["limit_type"] == "blown")
    retest = sum(1 for s in stocks if s["limit_type"] == "retest")
    continuous = sum(1 for s in stocks if s["limit_streak"] >= 2)
    high_streak = sum(1 for s in stocks if s["limit_streak"] >= 3)
    early = sum(1 for s in stocks
                if s.get("timing_bucket") in ("pre_open", "morning_early"))
    lines.append(f"▸ 涨停总数: **{total}** 只 "
                 f"(封板 {firm} | 炸板 {blown} | 回封 {retest})")
    lines.append(f"▸ 连板: {continuous} 只 | 高位连板(≥3板): {high_streak} 只 "
                 f"| 早盘涨停: {early} 只")
    lines.append("")

    if streak_dist:
        bar = " ".join(f"{k}连板:{v}" for k, v in streak_dist.items())
        lines.append(f"**连板分布**: {bar}")
        lines.append("")

    if concepts:
        lines.append("### 概念涨停排行")
        lines.append("")
        lines.append("| 概念 | 涨停数 | 连板数 | 最高连板 | 早盘涨停 | 代表股 |")
        lines.append("|------|--------|--------|---------|---------|--------|")
        for c in concepts[:10]:
            top = c["members"][0] if c["members"] else {}
            rep = f"{top.get('name','')}({top.get('limit_streak','')}板)" if top else "-"
            lines.append(
                f"| {c['concept']} | {c['stock_count']} | "
                f"{c['continuous_count']} | {c['max_streak']} | "
                f"{c['early_morning_count']} | {rep} |"
            )
        lines.append("")

    lines.append("### 全部涨停股")
    lines.append("")
    lines.append("| 代码 | 名称 | 概念 | 首次涨停 | 连板 | 封单(亿) |")
    lines.append("|------|------|------|---------|------|---------|")
    for s in stocks:
        concepts_str = ",".join(s["concepts"][:3]) if s["concepts"] else "-"
        seal = f"{s['seal_amount']/1e8:.1f}" if s["seal_amount"] else "-"
        lines.append(
            f"| {s['code']} | {s['name']} | {concepts_str} | "
            f"{s['first_limit_time'] or '-'} | {s['limit_streak']} | {seal} |"
        )
    lines.append("")
    lines.append(f"> 数据来源: 东方财富涨停池 (AKShare) | {date_str}")
    return "\n".join(lines)


# ──────────────── CLI ────────────────


def main():
    parser = argparse.ArgumentParser(description="涨停复盘数据获取")
    parser.add_argument("--date", type=str, help="日期 YYYY-MM-DD, 默认今日")
    parser.add_argument("-j", "--json", action="store_true", help="JSON 输出")
    parser.add_argument("--aggregate", choices=["concepts", "streak"],
                        help="聚合方式")
    parser.add_argument("--top-concepts", type=int, default=10,
                        help="概念排行数量, 默认10")
    parser.add_argument("-o", "--output", type=str, help="输出到文件")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    stocks = fetch_limitup_stocks(args.date)

    if not stocks:
        print(f"⚠️ {date_str} 无涨停数据（非交易日或数据不可用）")
        return

    if args.aggregate == "concepts":
        concepts = aggregate_by_concept(stocks)
        if args.json:
            out = json.dumps(concepts[:args.top_concepts],
                             ensure_ascii=False, indent=2)
        else:
            out = format_report(stocks, concepts[:args.top_concepts],
                                aggregate_by_limit_streak(stocks), date_str)

    elif args.aggregate == "streak":
        streak = aggregate_by_limit_streak(stocks)
        out = json.dumps(streak, ensure_ascii=False, indent=2)

    else:
        if args.json:
            out = json.dumps(stocks, ensure_ascii=False, indent=2)
        else:
            concepts = aggregate_by_concept(stocks)
            streak = aggregate_by_limit_streak(stocks)
            out = format_report(stocks, concepts, streak, date_str)

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Output: {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
