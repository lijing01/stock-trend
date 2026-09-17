"""Test sector_feeder module — qualified_sectors read/write + mapping lookup."""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import json
import pytest
from bridge.sector_feeder import (
    export_qualified_sectors,
    load_qualified_sectors,
    map_ths_sector_to_em,
    SectorsFile,
)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".cache" / "stock-trend"


# ── export / load ──

def test_export_and_load(tmp_path):
    """Round-trip: export then load returns same data."""
    sectors = [
        {"name": "半导体", "heat_score": 78,
         "lhb_score": 45, "lhb_direction": "净买"},
        {"name": "人形机器人", "heat_score": 65,
         "lhb_score": 60, "lhb_direction": "净买"},
    ]
    out_path = tmp_path / "qualified_sectors.json"
    export_qualified_sectors(sectors, path=out_path, heat_min=50)
    assert out_path.exists()

    loaded = load_qualified_sectors(path=out_path)
    assert re.match(r"\d{4}-\d{2}-\d{2}$", loaded.date), f"bad date: {loaded.date}"
    assert loaded.threshold == {"heat_min": 50}
    assert len(loaded.sectors) == 2
    assert loaded.sectors[0]["name"] == "半导体"


def test_load_non_existent_file(tmp_path):
    """Load missing file returns empty SectorsFile."""
    missing = tmp_path / "nope.json"
    loaded = load_qualified_sectors(path=missing)
    assert loaded.sectors == []


def test_load_corrupted_file(tmp_path):
    """Load corrupted JSON returns empty SectorsFile."""
    bad = tmp_path / "bad.json"
    bad.write_text("{{{garbage}}", encoding="utf-8")
    loaded = load_qualified_sectors(path=bad)
    assert loaded.sectors == []


# ── mapping lookup ──

def test_map_ths_to_em_exists():
    """Known sector maps correctly to EM names."""
    em_names = map_ths_sector_to_em("半导体")
    assert isinstance(em_names, list)
    assert len(em_names) > 0
    assert "半导体及元件" in em_names


def test_map_ths_to_em_not_exists():
    """Unknown sector returns empty list."""
    em_names = map_ths_sector_to_em("不存在的板块12345")
    assert em_names == []


def test_map_ths_to_em_empty_name():
    """Empty string returns empty list."""
    assert map_ths_sector_to_em("") == []
