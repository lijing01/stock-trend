#!/usr/bin/env python3
"""股票→板块映射表构建工具。

通过东方财富板块成分股 API 反向构建映射：给定股票代码，找出所属行业/概念板块。

用于 DDX 板块聚合：把个股 DDX 数据按所属板块汇总。

Usage:
    python3 sector_mapper.py                              # 构建映射并缓存
    python3 sector_mapper.py --rebuild                    # 强制重建
    python3 sector_mapper.py --lookup 600123              # 查个股所属板块
    python3 sector_mapper.py --json                       # JSON 输出
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from core.eastmoney_utils import rotate_em_host, EM_HEADERS
from fetchers.sector_data import get_sector_list

# ──────────────── 缓存 ────────────────

CACHE_DIR = SCRIPT_DIR.parent.parent.parent.parent / ".cache" / "stock-trend"
MAP_CACHE_FILE = CACHE_DIR / "stock_sector_map.json"
MAX_CACHE_AGE_HOURS = 168  # 7 days — sector membership changes slowly


# ──────────────── API：获取板块成分股 ────────────────


def _fetch_sector_stocks_raw(sector_code: str, max_stocks: int = 30) -> list[dict]:
    """Fetch constituent stocks for a sector code from East Money.

    Returns list of {code, name} dicts.
    """
    import random
    import time as _time
    import urllib.request

    url = (
        f"https://push2.eastmoney.com/api/qt/clist/get"
        f"?fs=b:{sector_code}"
        f"&fields=f12,f14"
        f"&pn=1&pz={max_stocks}&po=0&np=1&fltt=2"
        f"&fid=f3"
    )
    for attempt in range(3):
        host = rotate_em_host(attempt)
        actual_url = url.replace("https://push2.eastmoney.com", f"https://{host}", 1) if host != "push2.eastmoney.com" else url
        try:
            req = urllib.request.Request(actual_url, headers=EM_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("rc") == 0 and data.get("data", {}).get("diff"):
                items = data["data"]["diff"]
                return [{"code": i.get("f12", ""), "name": i.get("f14", "")}
                        for i in items if i.get("f12")]
        except Exception:
            if attempt < 2:
                _time.sleep(1.5 ** attempt + random.uniform(0.3, 0.8))
    return []


# ──────────────── 构建映射表 ────────────────


def build_stock_sector_map(max_stocks_per_sector: int = 30) -> dict:
    """Build reverse mapping: stock_code → [sector_info, ...].

    Iterates all industry + concept sectors, fetches constituent stocks,
    builds reverse index.

    Returns:
        dict with meta and mapping:
        {
            "meta": {"built_at": ..., "total_sectors": ..., "total_stocks": ...},
            "mapping": {
                "600519": [{"code": "BK0477", "name": "白酒", "type": "industry"}, ...],
                ...
            }
        }
    """
    print("Building stock→sector mapping...")
    start = time.time()

    # Get all sectors
    all_sectors = get_sector_list()
    if not all_sectors:
        print("  ⚠️ No sectors from API")
        return {"meta": {"built_at": datetime.now().isoformat(), "total_sectors": 0, "total_stocks": 0}, "mapping": {}}

    print(f"  Got {len(all_sectors)} total sectors")

    # Dedup: a sector may appear in both industry and concept (rare but possible)
    seen_sectors = set()
    deduped = []
    for s in all_sectors:
        key = s["code"]
        if key not in seen_sectors:
            seen_sectors.add(key)
            deduped.append(s)

    # Build: stock_code → set of (sector_code, sector_name, type)
    stock_sectors = defaultdict(set)
    processed = 0
    errors = 0

    for s in deduped:
        code = s["code"]
        name = s["name"]
        stype = s["type"]
        stocks = _fetch_sector_stocks_raw(code, max_stocks_per_sector)
        for stock in stocks:
            if stock["code"]:
                stock_sectors[stock["code"]].add((code, name, stype))
        processed += 1
        if processed % 50 == 0:
            print(f"  Processed {processed}/{len(deduped)} sectors ({len(stock_sectors)} unique stocks)")

    # Convert sets to lists for JSON serialization
    mapping = {}
    for stock_code, sectors in stock_sectors.items():
        mapping[stock_code] = [
            {"code": sc, "name": sn, "type": st}
            for sc, sn, st in sorted(sectors)
        ]

    elapsed = time.time() - start
    result = {
        "meta": {
            "built_at": datetime.now().isoformat(),
            "total_sectors": len(deduped),
            "total_stocks": len(mapping),
            "elapsed_seconds": round(elapsed, 1),
        },
        "mapping": mapping,
    }

    print(f"  ✓ Built {len(mapping)} stock entries from {len(deduped)} sectors in {elapsed:.0f}s")
    return result


# ──────────────── 缓存管理 ────────────────


def save_mapping(data: dict) -> None:
    """Save mapping to cache file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MAP_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"  Cached to {MAP_CACHE_FILE}")


def load_mapping() -> Optional[dict]:
    """Load cached mapping if fresh enough.

    Returns mapping dict or None if missing/expired.
    """
    if not MAP_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(MAP_CACHE_FILE.read_text(encoding="utf-8"))
        built_at = data.get("meta", {}).get("built_at", "")
        if built_at:
            cached_at = datetime.fromisoformat(built_at)
            age = datetime.now() - cached_at
            if age.total_seconds() > MAX_CACHE_AGE_HOURS * 3600:
                return None
        if data.get("mapping"):
            return data
        return None
    except Exception:
        return None


def get_stock_sectors(code: str) -> list[dict]:
    """Lookup sectors for a single stock code.

    Returns list of {code, name, type} or empty list.
    """
    data = get_mapping()
    if not data:
        return []
    return data.get("mapping", {}).get(code, [])


def get_mapping(rebuild: bool = False) -> Optional[dict]:
    """Get mapping (load cache or build if needed)."""
    if not rebuild:
        cached = load_mapping()
        if cached:
            return cached
    data = build_stock_sector_map()
    if data and data.get("mapping"):
        save_mapping(data)
    return data


# ──────────────── DDX 板块聚合 ────────────────


def aggregate_ddx_by_sector(ddx_list: list[dict],
                             mapping: dict) -> list[dict]:
    """Aggregate DDX data by sector.

    Args:
        ddx_list: list from fetch_ddx_ranking().
        mapping: stock_sector_map dict with "mapping" key.

    Returns:
        List of sector-level DDX aggregate dicts sorted by composite score:
            sector_code, sector_name, type, ddx_inflow_count, ddx_inflow_ratio,
            continuous_count, super_order_avg, composite_score
    """
    if not ddx_list or not mapping:
        return []

    stock_map = mapping.get("mapping", {})
    if not stock_map:
        return []

    # Group: sector → list of DDX entries for stocks in that sector
    sector_ddx = defaultdict(list)
    for stock in ddx_list:
        code = stock["code"]
        sectors = stock_map.get(code, [])
        if not sectors:
            # Some stocks have no sector mapping (delisted, new, etc.)
            continue
        for sec in sectors:
            key = sec["code"]
            sector_ddx[key].append({**stock, "sector_name": sec["name"], "sector_type": sec["type"]})

    if not sector_ddx:
        return []

    results = []
    for sec_code, members in sector_ddx.items():
        total = len(members)
        inflow = [m for m in members if m.get("ddx", 0) > 0]
        continuous = [m for m in members if m.get("ddx_days", 0) >= 3]
        high_super = [m for m in members if (m.get("super_order_ratio", 0) or 0) > 0.05]

        avg_ddx = sum(m.get("ddx", 0) for m in members) / total if total else 0
        avg_super = sum(m.get("super_order_ratio", 0) or 0 for m in members) / total if total else 0

        # Composite DDX sector score (0-100)
        inflow_ratio = len(inflow) / total if total else 0
        continuous_ratio = len(continuous) / total if total else 0
        super_ratio = len(high_super) / total if total else 0

        score = (inflow_ratio * 50 + continuous_ratio * 30 + super_ratio * 20) * 100
        score = round(max(0, min(100, score)), 1)

        member_codes = [m["code"] for m in members[:5]]

        results.append({
            "sector_code": sec_code,
            "sector_name": members[0]["sector_name"] if members else "",
            "sector_type": members[0]["sector_type"] if members else "",
            "total_ddx_stocks": total,
            "ddx_inflow_count": len(inflow),
            "ddx_inflow_ratio": round(inflow_ratio, 3),
            "continuous_count": len(continuous),
            "continuous_ratio": round(continuous_ratio, 3),
            "high_super_count": len(high_super),
            "avg_ddx": round(avg_ddx, 4),
            "avg_super_order_ratio": round(avg_super, 4),
            "ddx_score": score,
            "member_codes": member_codes,
        })

    results.sort(key=lambda r: r["ddx_score"], reverse=True)
    return results


# ──────────────── CLI ────────────────


def main():
    parser = argparse.ArgumentParser(description="股票→板块映射构建")
    parser.add_argument("--rebuild", action="store_true", help="强制重建缓存")
    parser.add_argument("--lookup", type=str, help="查询个股所属板块")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()

    if args.lookup:
        sectors = get_stock_sectors(args.lookup)
        if args.json:
            print(json.dumps(sectors, ensure_ascii=False, indent=2))
        else:
            if sectors:
                print(f"股票 {args.lookup} 所属板块:")
                for s in sectors:
                    print(f"  [{s['type']}] {s['name']} ({s['code']})")
            else:
                print(f"股票 {args.lookup}: 未找到板块信息")
        return

    data = get_mapping(rebuild=args.rebuild)
    if not data:
        print("⚠️ 构建映射失败")
        return

    meta = data["meta"]
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"\n股票→板块映射表:")
        print(f"  构建时间: {meta.get('built_at', '')}")
        print(f"  覆盖板块: {meta.get('total_sectors', 0)} 个")
        print(f"  覆盖股票: {meta.get('total_stocks', 0)} 只")
        print(f"  耗时: {meta.get('elapsed_seconds', 0)}s")
        print(f"  缓存: {MAP_CACHE_FILE}")


if __name__ == "__main__":
    main()
