#!/usr/bin/env python3
"""AKShare 备选板块数据获取（当直连东方财富 API 不可用时）。

封装 AKShare 的同花顺/东方财富板块接口，输出格式与 sector_data.py 兼容。

目前可用数据源:
  - stock_board_industry_summary_ths()  — 同花顺行业实时排行 ✅
  - stock_board_concept_name_ths()      — 同花顺概念列表     ✅

Usage:
    from fetchers.sector_akshare import get_sector_rankings_akshare
    rankings = get_sector_rankings_akshare()
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


def get_sector_rankings_akshare() -> Optional[dict]:
    """Get sector rankings via AKShare (同花顺 data), compatible format.

    Returns:
        Same format as sector_data.get_sector_rankings():
        {
            "meta": {"fetch_time": ..., "total_sectors": ...},
            "sectors": [
                {
                    "code": str,          # 同花顺板块代码 (e.g. "881121")
                    "name": str,          # 板块名称
                    "type": "industry",   # industry / concept
                    "change_pct": float,  # 涨跌幅%
                    "amount": float,      # 成交额
                    "up_count": int,      # 上涨家数
                    "down_count": int,    # 下跌家数
                    "total_count": int,   # 总计家数
                    "main_force_net": float,  # 主力净流入
                }
            ]
        }
        Returns None if AKShare unavailable or all APIs fail.
    """
    if not HAS_AKSHARE:
        return None

    sectors = []
    now = datetime.now().strftime("%Y%m%d-%H%M%S")

    # ── 1. 同花顺行业板块实时排行 ──
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                change = _safe_float(row.get("涨跌幅"))
                amount = _safe_float(row.get("总成交额")) * 1e8  # 亿→元
                net = _safe_float(row.get("净流入")) * 1e8
                up = _safe_int(row.get("上涨家数"))
                down = _safe_int(row.get("下跌家数"))
                total = up + down

                sectors.append({
                    "code": str(row.get("序号", "")) or "",
                    "name": str(row.get("板块", "")),
                    "type": "industry",
                    "change_pct": change,
                    "amount": amount,
                    "up_count": up,
                    "down_count": down,
                    "total_count": total,
                    "main_force_net": net,
                })
    except Exception as e:
        print(f"  [AKShare] Warning: 行业板块排行失败: {e}", file=sys.stderr)

    # ── 2. 同花顺概念列表（名称+代码，无实时行情） ──
    # 概念板块没有实时排行 API，但把名称列表加进去
    # 这样 hot_score 逻辑至少能识别这些概念存在
    try:
        df = ak.stock_board_concept_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", ""))
                code = str(row.get("code", ""))
                if name and code:
                    sectors.append({
                        "code": code,
                        "name": name,
                        "type": "concept",
                        "change_pct": 0,
                        "amount": 0,
                        "up_count": 0,
                        "down_count": 0,
                        "total_count": 0,
                        "main_force_net": 0,
                    })
    except Exception as e:
        print(f"  [AKShare] Warning: 概念列表失败: {e}", file=sys.stderr)

    if not sectors:
        return None

    return {
        "meta": {
            "fetch_time": now,
            "total_sectors": len(sectors),
            "source": "akshare",
        },
        "sectors": sectors,
    }


def get_sector_list_akshare() -> list[dict]:
    """Get sector list via AKShare, compatible with sector_data.get_sector_list().

    Returns:
        [{code, name, type}, ...]
    """
    sectors = []
    try:
        df = ak.stock_board_industry_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sectors.append({
                    "code": str(row.get("code", "")),
                    "name": str(row.get("name", "")),
                    "type": "industry",
                })
    except Exception:
        pass

    try:
        df = ak.stock_board_concept_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sectors.append({
                    "code": str(row.get("code", "")),
                    "name": str(row.get("name", "")),
                    "type": "concept",
                })
    except Exception:
        pass

    return sectors


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
