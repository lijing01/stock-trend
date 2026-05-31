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
    build_signal_label,
    SectorsFile,
)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".cache" / "stock-trend"


# ── export / load ──

def test_export_and_load(tmp_path):
    """Round-trip: export then load returns same data."""
    sectors = [
        {"name": "半导体", "heat_score": 78, "zt_score": 82,
         "lhb_score": 45, "lhb_direction": "净买"},
        {"name": "人形机器人", "heat_score": 65, "zt_score": 71,
         "lhb_score": 60, "lhb_direction": "净买"},
    ]
    out_path = tmp_path / "qualified_sectors.json"
    export_qualified_sectors(sectors, path=out_path, heat_min=50, zt_min=50)
    assert out_path.exists()

    loaded = load_qualified_sectors(path=out_path)
    assert re.match(r"\d{4}-\d{2}-\d{2}$", loaded.date), f"bad date: {loaded.date}"
    assert loaded.threshold == {"heat_min": 50, "zt_min": 50}
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


# ── signal label ──

def test_signal_label_dual_strong():
    """Dual-engine confirmed + high score → 🟢."""
    label = build_signal_label(heat=65, zt_score=72, final_score=1.5)
    assert "🟢" in label
    assert "双强" in label


def test_signal_label_dual_watch():
    """Dual-engine confirmed + low score → 🔵."""
    label = build_signal_label(heat=55, zt_score=60, final_score=0.4)
    assert "🔵" in label


def test_signal_label_dual_no_leader():
    """Dual-engine confirmed + negative score → ⚪."""
    label = build_signal_label(heat=60, zt_score=55, final_score=-0.3)
    assert "⚪" in label


def test_signal_label_leader_no_dual():
    """Leader but no dual confirmation → 🟡."""
    label = build_signal_label(heat=30, zt_score=20, final_score=1.2)
    assert "🟡" in label


def test_signal_label_weak():
    """Neither hot nor leader → ⚫."""
    label = build_signal_label(heat=30, zt_score=20, final_score=-0.5)
    assert "⚫" in label
