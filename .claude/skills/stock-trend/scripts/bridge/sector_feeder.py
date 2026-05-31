#!/usr/bin/env python3
"""Bridge module — relays ths-theme output to longtou input.

Functions:
    export_qualified_sectors: write qualified_sectors.json
    load_qualified_sectors: read qualified_sectors.json as SectorsFile
    map_ths_sector_to_em: look up 同花顺→东方财富 mapping
    build_signal_label: classify sector+stock into signal tag
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
CONFIG_DIR = SKILL_DIR / "config"

# Default qualified sectors path
QUALIFIED_PATH = CACHE_DIR / "qualified_sectors.json"


@dataclass
class SectorsFile:
    """Container for qualified_sectors.json contents."""
    date: str = ""
    threshold: dict = field(default_factory=lambda: {"heat_min": 50, "zt_min": 50})
    sectors: list = field(default_factory=list)


def export_qualified_sectors(
    sectors: list[dict],
    path: Optional[Path] = None,
    heat_min: int = 50,
    zt_min: int = 50,
) -> None:
    """Write qualified_sectors.json.

    Args:
        sectors: list of dicts with name, heat_score, zt_score, lhb_score, lhb_direction.
        path: output path, defaults to CACHE_DIR/qualified_sectors.json.
        heat_min: minimum heat_score threshold (informational).
        zt_min: minimum zt_score threshold (informational).
    """
    path = path or QUALIFIED_PATH
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "threshold": {"heat_min": heat_min, "zt_min": zt_min},
        "sectors": sectors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_qualified_sectors(
    path: Optional[Path] = None,
) -> SectorsFile:
    """Load qualified_sectors.json.

    Returns empty SectorsFile on file-not-found or parse error (never raises).
    """
    path = path or QUALIFIED_PATH
    if not path.exists():
        return SectorsFile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SectorsFile(
            date=raw.get("date", ""),
            threshold=raw.get("threshold", {}),
            sectors=raw.get("sectors", []),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return SectorsFile()


# ── Mapping lookup ──

_MAPPING_CACHE: dict[str, list[str]] | None = None


def _load_mapping() -> dict[str, list[str]]:
    """Load sector_mapping.yaml, cached after first call.

    Returns empty dict on any failure.
    """
    global _MAPPING_CACHE
    if _MAPPING_CACHE is not None:
        return _MAPPING_CACHE
    path = CONFIG_DIR / "sector_mapping.yaml"
    if not path.exists():
        _MAPPING_CACHE = {}
        return _MAPPING_CACHE
    try:
        import yaml
        _MAPPING_CACHE = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        _MAPPING_CACHE = {}
    return _MAPPING_CACHE


def map_ths_sector_to_em(ths_name: str) -> list[str]:
    """Look up 同花顺 sector name → 东方财富 sector name(s).

    Args:
        ths_name: sector name from ths-theme (e.g. "半导体").

    Returns:
        List of 东方财富 sector names, or empty list if not found.
    """
    if not ths_name:
        return []
    mapping = _load_mapping()
    return mapping.get(ths_name, [])


# ── Signal labels ──


def build_signal_label(
    heat: float,
    zt_score: float,
    final_score: float,
) -> str:
    """Build combined signal label string.

    Labels:
        heat≥50 & zt≥50 + final_score≥1.0 → "双强·龙头确认 🟢"
        heat≥50 & zt≥50 + final_score≥0   → "双强·关注中 🔵"
        heat≥50 & zt≥50 + final_score<0    → "双强·无龙头 ⚪"
        heat<50 or zt<50 + final_score≥1.0 → "龙头·板块待确认 🟡"
        else                                → "弱势区 ⚫"
    """
    dual_confirmed = heat >= 50 and zt_score >= 50

    if dual_confirmed and final_score >= 1.0:
        return "双强·龙头确认 🟢"
    if dual_confirmed and final_score >= 0:
        return "双强·关注中 🔵"
    if dual_confirmed:
        return "双强·无龙头 ⚪"
    if final_score >= 1.0:
        return "龙头·板块待确认 🟡"
    return "弱势区 ⚫"
