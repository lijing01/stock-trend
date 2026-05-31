"""Test integrated report generation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bridge.integrated_report import (
    build_sector_overview,
    generate_integrated_md,
    generate_integrated_html,
)

# Sample data
SAMPLE_THS = """
热力板块数: 2
强势板块(≥70): 半导体 78, 人形机器人 65
"""

SAMPLE_MARKET_LEADER = """
### 半导体 (热度:78 涨幅:3.2%)
> 行业热力:78/100 涨停概念:82/100

**龙头**
- 北方华创(002371) +10.0% ↑偏多 ★★★
  板块加成:+0.72
"""

SAMPLE_OVERVIEW = {
    "total_hot_sectors": 2,
    "total_leaders": 3,
    "dual_confirmed": 2,
    "lhb_strong": 1,
    "top_sectors": ["半导体", "人形机器人"],
}


def test_build_sector_overview():
    """Overview dict has correct structure."""
    overview = build_sector_overview(
        total_hot=2, leaders=3, dual=2, lhb=1, top=["A", "B"]
    )
    assert overview["total_hot_sectors"] == 2
    assert overview["total_leaders"] == 3
    assert overview["dual_confirmed"] == 2
    assert overview["top_sectors"] == ["A", "B"]


def test_generate_integrated_md_contains_sections():
    """MD report has expected section headers."""
    md = generate_integrated_md(
        date="2026-05-31",
        ths_report="THS REPORT",
        leader_report=SAMPLE_MARKET_LEADER,
        overview=SAMPLE_OVERVIEW,
    )
    assert "市场热力 · 龙头整合报告" in md
    assert "一、市场总览" in md
    assert "二、热力板块 · 龙头扫描" in md


def test_generate_integrated_md_empty_leader():
    """When no leaders, report still works."""
    md = generate_integrated_md(
        date="2026-05-31",
        ths_report="仅热力报告",
        leader_report="",
        overview={"total_hot_sectors": 0, "total_leaders": 0,
                  "dual_confirmed": 0, "lhb_strong": 0, "top_sectors": []},
    )
    assert "市场热力 · 龙头整合报告" in md
    assert "无满足双强条件" in md


def test_generate_integrated_html_basic():
    """HTML report renders without error."""
    html = generate_integrated_html(
        date="2026-05-31",
        ths_report="THS REPORT",
        leader_report=SAMPLE_MARKET_LEADER,
        overview=SAMPLE_OVERVIEW,
    )
    assert "<html" in html
    assert "市场热力" in html
    assert "双强热力板块" in html
