#!/usr/bin/env python3
"""同花顺涨停复盘数据获取器 (zt_replay).

爬取同花顺每日涨停板数据，提取涨停股票的概念归因（同花顺独有）。

核心数据：
  - 涨停股票列表（代码、名称）
  - 涨停原因/概念标签 ← 同花顺独有，东方财富没有
  - 首次涨停时间（早盘/午后/尾盘）
  - 连板高度
  - 封单金额

Usage:
    python3 zt_replay.py                          # 今日涨停榜
    python3 zt_replay.py --date 2026-05-29        # 历史某日
    python3 zt_replay.py --top-concepts 10         # 今日概念涨停排行
    python3 zt_replay.py --aggregate concepts      # 按概念聚合输出
    python3 zt_replay.py --json                    # JSON 输出
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ths_utils import fetch_page, extract_table_rows, parse_amount

# ──────────────── 常量 ────────────────

# 今日涨停首页
THS_ZT_URL = "https://data.10jqka.com.cn/financial/zt/"
# 按日期查询: https://data.10jqka.com.cn/financial/zt/date/2026-05-30/
THS_ZT_DATE_URL = "https://data.10jqka.com.cn/financial/zt/date/{}/"

# ──────────────── 涨停板数据结构 ────────────────

LIMIT_UP_SCHEMA = [
    "code",         # 股票代码
    "name",         # 股票名称
    "concepts",     # 涨停原因/概念标签 (list[str])
    "first_limit_time",  # 首次涨停时间 "HH:MM"
    "last_limit_time",   # 最后涨停时间 "HH:MM" (可能有)
    "limit_streak",      # 连板数 (int)
    "seal_amount",       # 封单金额 (float, 元)
    "seal_ratio",        # 封板率/封成比 (float, 可选)
    "limit_type",        # 涨停类型: firm(封板) / blown(炸板) / retest(回封)
    "board",             # 交易所板块: sh/sz/bj
]


# ──────────────── 核心：爬取 + 解析 ────────────────


def _url(date_str: Optional[str] = None) -> str:
    """Build target URL for given date (None = today)."""
    if date_str:
        return THS_ZT_DATE_URL.format(date_str)
    return THS_ZT_URL


def _classify_limit_time(t: Optional[str]) -> str:
    """Classify limit-up timing bucket."""
    if not t:
        return "unknown"
    try:
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        minutes = h * 60 + m
        if minutes < 9 * 60 + 30:
            return "pre_open"       # 集合竞价涨停
        elif minutes < 11 * 60 + 30:
            return "morning_early"  # 早盘
        elif minutes < 13 * 60:
            return "morning_late"   # 午前
        elif minutes < 14 * 60:
            return "afternoon"      # 午后
        elif minutes < 15 * 60:
            return "afternoon_late" # 尾盘
        return "close"
    except (ValueError, IndexError):
        return "unknown"


def _classify_limit_type(row_cells: list[str],
                         limit_streak: int) -> str:
    """Infer limit type from cell text."""
    joined = " ".join(row_cells).lower()
    if any(kw in joined for kw in ("炸板", "开板", "打开")):
        return "blown"
    if any(kw in joined for kw in ("回封", "回板")):
        return "retest"
    if any(kw in joined for kw in ("涨停", "封板", "一字板")):
        return "firm"
    # if limit_streak >= 1, it's a firm limit-up
    if limit_streak >= 1:
        return "firm"
    return "firm"


def _detect_board(code: str) -> str:
    """Detect exchange board from 6-digit code."""
    if code.startswith("6") or code.startswith("9"):
        return "sh"
    if code.startswith("0") or code.startswith("3"):
        return "sz"
    if code.startswith("8") or code.startswith("4"):
        return "bj"
    return "unknown"


def _parse_limit_streak(raw: str) -> int:
    """Parse连板数 from strings like '3连板', '首板', '2板'."""
    raw = raw.strip()
    if not raw or raw in ("-", "--", "—"):
        return 1  # 默认至少是首板
    if "首板" in raw or "首" in raw:
        return 1
    m = re.search(r'(\d+)', raw)
    if m:
        return int(m.group(1))
    return 1


def _parse_concepts(cell_text: str) -> list[str]:
    """Parse concept tags from 涨停原因 cell.

    Cell may contain: "DeepSeek概念,AI芯片,国产替代"
    Or: "人工智能; 大数据; 云计算"
    Returns cleaned list of concept names.
    """
    if not cell_text or cell_text in ("-", "--", "—"):
        return []
    # Split by common delimiters
    raw = cell_text.strip()
    parts = re.split(r'[,，;；/、\s]{1,3}', raw)
    concepts = []
    for p in parts:
        p = p.strip().strip('"\'「」【】')
        if p and len(p) >= 2 and p not in ("-", "--", "—"):
            concepts.append(p)
    return concepts


def fetch_limitup_stocks(date_str: Optional[str] = None) -> list[dict]:
    """Fetch today's (or specified date's) limit-up stock list from 同花顺.

    Args:
        date_str: "YYYY-MM-DD" or None for today.

    Returns:
        List of dicts:
            code, name, concepts, first_limit_time, last_limit_time,
            limit_streak, seal_amount, seal_ratio, limit_type, board
        Returns empty list on any failure (graceful degradation).
    """
    url = _url(date_str)
    html = fetch_page(url, referer="https://data.10jqka.com.cn/financial/zt/")
    if html is None:
        return []

    rows = extract_table_rows(html)
    if not rows:
        return []

    results = []
    seen_codes = set()

    for cells in rows:
        if len(cells) < 4:
            continue

        # Extract 6-digit stock code
        code = None
        for c in cells[:3]:
            m = re.search(r'\b(\d{6})\b', c)
            if m:
                code = m.group(1)
                break
        if not code or code in seen_codes:
            continue

        # Determine column layout (flexible for page structure changes)
        name = ""
        concepts_raw = ""
        first_time = None
        last_time = None
        streak_raw = "1"
        seal_raw = ""
        limit_type_raw = ""

        # Scan remaining cells for typed content
        # Order matters: check streak/amount/time FIRST to exclude them from
        # concept detection (concept cells may also contain digits/letters).
        for cell in cells[1:]:
            lower = cell.lower()
            stripped = cell.strip()

            # Limit streak: "3连板", "首板", "2连板", "N板"
            if re.search(r'[连板首板]', cell):
                streak_raw = cell
                continue
            # Amount with 亿/万
            if re.search(r'[亿万]', cell) and not seal_raw:
                seal_raw = cell
                continue
            # Limit type keywords
            if any(kw in lower for kw in ("炸板", "回封", "一字", "涨停")):
                limit_type_raw = cell
                continue
            # Time pattern "HH:MM" or "HH:MM-HH:MM"
            times = re.findall(r'\b(\d{1,2}):(\d{2})\b', cell)
            if times:
                ts = ":".join(times[0])
                if first_time is None:
                    first_time = ts
                elif len(times) > 1:
                    last_time = ":".join(times[1])
                elif last_time is None:
                    last_time = ts
                continue
            # Concept tags — mixed Chinese/English, comma-separated
            if (re.match(r'^[一-鿿,，、;；\s]{2,60}$', stripped) or
                re.match(r'^[一-鿿A-Za-z0-9,，、;；/\s]{2,80}$', stripped)):
                if len(stripped) > len(name) + 2:
                    concepts_raw = cell

        # If concepts_raw still empty, check first few cells for concept tags
        if not concepts_raw:
            for cell in cells[2:5]:
                stripped = cell.strip()
                if (re.match(r'^[一-鿿，,、\s]{2,50}$', stripped) or
                    re.match(r'^[一-鿿A-Za-z0-9，,、\s]{2,60}$', stripped)):
                    if len(stripped) > 2:
                        concepts_raw = cell
                        break

        name = cells[2] if len(cells) > 2 else ""
        concepts = _parse_concepts(concepts_raw)
        limit_streak = _parse_limit_streak(streak_raw)
        seal_amount = parse_amount(seal_raw)
        limit_type = _classify_limit_type(cells, limit_streak)
        timing_bucket = _classify_limit_time(first_time)
        board = _detect_board(code)

        seen_codes.add(code)
        results.append({
            "code": code,
            "name": name.strip(),
            "concepts": concepts,
            "first_limit_time": first_time,
            "last_limit_time": last_time,
            "limit_streak": limit_streak,
            "seal_amount": seal_amount,
            "limit_type": limit_type,
            "timing_bucket": timing_bucket,
            "board": board,
        })

    # Sort: higher streak first, then earlier limit time
    results.sort(key=lambda r: (-r["limit_streak"],
                                r["first_limit_time"] or "99:99"))
    return results


# ──────────────── 按概念聚合 ────────────────


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
            "continuous_count": len(streaks),  # 连板股数
            "max_streak": max(streaks) if streaks else 1,
            "early_morning_count": sum(1 for m in members
                                       if m.get("timing_bucket") in ("pre_open", "morning_early")),
            "seal_amount_total": sum(m["seal_amount"] for m in members),
            "members": [{"code": m["code"], "name": m["name"],
                         "limit_streak": m["limit_streak"],
                         "first_limit_time": m["first_limit_time"]}
                        for m in members[:5]],  # top 5 members
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


# ──────────────── 输出 ────────────────


def format_report(stocks: list[dict],
                  concepts: list[dict],
                  streak_dist: dict[int, int],
                  date_str: str) -> str:
    """Generate Markdown report."""
    lines = []
    lines.append(f"## 涨停复盘 {date_str}")
    lines.append("")

    # Summary stats
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

    # Streak distribution
    if streak_dist:
        bar = " ".join(f"{k}连板:{v}" for k, v in streak_dist.items())
        lines.append(f"**连板分布**: {bar}")
        lines.append("")

    # Concept ranking
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

    # All stocks
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
    lines.append(f"> 数据来源: 同花顺涨停复盘 | {date_str}")
    return "\n".join(lines)


# ──────────────── CLI ────────────────


def main():
    parser = argparse.ArgumentParser(description="同花顺涨停复盘数据获取")
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
