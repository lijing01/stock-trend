"""Test market_leader.py with --sectors-from flag."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Simulate a qualified_sectors.json
SAMPLE_QUALIFIED = {
    "date": "2026-05-31",
    "threshold": {"heat_min": 50, "zt_min": 50},
    "sectors": [
        {"name": "半导体", "heat_score": 78, "zt_score": 82,
         "lhb_score": 45, "lhb_direction": "净买"},
    ],
}


@pytest.fixture
def qualified_file(tmp_path):
    path = tmp_path / "qualified_sectors.json"
    path.write_text(json.dumps(SAMPLE_QUALIFIED, ensure_ascii=False), encoding="utf-8")
    return path


def test_sectors_from_loads_qualified(qualified_file):
    """--sectors-from correctly reads sector list."""
    from scans.market_leader import load_sectors_from_file
    sectors = load_sectors_from_file(str(qualified_file))
    assert len(sectors) == 1
    assert sectors[0]["name"] == "半导体"
    assert sectors[0]["heat_score"] == 78
    assert sectors[0]["zt_score"] == 82


def test_sectors_from_empty_file(tmp_path):
    """Empty qualified file returns empty list."""
    from scans.market_leader import load_sectors_from_file
    empty = tmp_path / "empty.json"
    empty.write_text('{"sectors": []}', encoding="utf-8")
    assert load_sectors_from_file(str(empty)) == []


def test_sectors_from_missing_file(tmp_path):
    """Missing file returns empty list."""
    from scans.market_leader import load_sectors_from_file
    missing = tmp_path / "nope.json"
    assert load_sectors_from_file(str(missing)) == []


def test_sector_boost_formula():
    """Verify sector_boost calculation."""
    from scans.market_leader import compute_sector_boost
    # heat=78, zt=82 → boost = (78/33.3)*0.15 + (82/33.3)*0.15
    # boost ≈ 0.351 + 0.369 = 0.72
    boost = compute_sector_boost(heat=78, zt_score=82)
    assert 0.70 <= boost <= 0.74, f"boost={boost} out of range"


def test_sector_boost_zero():
    """Zero heat/zt → zero boost."""
    from scans.market_leader import compute_sector_boost
    assert compute_sector_boost(0, 0) == 0.0


def test_sector_boost_full():
    """Max heat/zt → ~0.9 boost."""
    from scans.market_leader import compute_sector_boost
    boost = compute_sector_boost(100, 100)
    assert 0.85 <= boost <= 0.95, f"boost={boost} out of range"
