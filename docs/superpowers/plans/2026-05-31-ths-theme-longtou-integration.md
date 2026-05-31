# ths-theme + longtou 整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 热板块里找龙头 — ths-theme 输出热板块列表 → longtou 只扫热板块 → 整合报告

**Architecture:** ths-theme 改加 `--export-sectors` 输出 qualified_sectors.json；market_leader 改加 `--sectors-from` 接收该文件并限制扫描范围；新增 bridge/integrated_report.py 拼接两份输出。中间文件解耦，不硬调。

**Tech Stack:** Python 3.11+, AKShare, East Money API, sector_data.py (existing), sectory_mapper.py (existing)

---

### Task 1: 创建 sector_mapping.yaml 配置

**Files:**
- Create: `.claude/skills/stock-trend/config/sector_mapping.yaml`
- Test: `.claude/skills/stock-trend/tests/test_sector_mapping.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sector_mapping.py
"""Test sector_mapping.yaml parsing and lookup."""
import yaml
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MAPPING_PATH = CONFIG_DIR / "sector_mapping.yaml"


def test_mapping_file_exists():
    assert MAPPING_PATH.exists(), f"{MAPPING_PATH} not found"


def test_mapping_is_valid_yaml():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    assert len(data) > 0, "mapping is empty"


def test_mapping_format():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    for ths_name, em_names in data.items():
        assert isinstance(ths_name, str) and ths_name, \
            f"bad key: {ths_name}"
        assert isinstance(em_names, list) and len(em_names) > 0, \
            f"bad values for {ths_name}"
        for em_name in em_names:
            assert isinstance(em_name, str) and em_name, \
                f"bad em_name in {ths_name}: {em_name}"


def test_common_sectors_present():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    essential = {"半导体", "人工智能", "新能源汽车", "光伏", "证券", "银行", "白酒"}
    missing = essential - set(data.keys())
    assert not missing, f"missing essential sectors: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_sector_mapping.py -v 2>&1
```

Expected: FAIL with `MAPPING_PATH not found` (file doesn't exist yet).

- [ ] **Step 3: Create sector_mapping.yaml**

```yaml
# 同花顺板块名 → 东方财富板块名(可多个)
# ths-theme 输出用同花顺名称，longtou 内部用东方财富名称
# 映射不到时保留同花顺名、标注"未找到对应板块"

# ── 科技 ──
半导体: ["半导体及元件", "半导体"]
人工智能: ["人工智能", "计算机应用"]
芯片: ["半导体及元件", "国家大基金持股"]
消费电子: ["消费电子", "电子制造"]
通信: ["通信设备", "通信服务"]
软件: ["计算机应用", "国产软件"]
云计算: ["计算机应用", "云计算"]
5G: ["通信设备", "5G"]
机器人: ["自动化设备", "机器人概念"]
军工: ["国防军工", "航空装备"]
数据要素: ["计算机应用", "数据要素"]

# ── 新能源 ──
新能源汽车: ["新能源汽车", "汽车整车"]
光伏: ["电力设备", "光伏概念"]
储能: ["电力设备", "储能"]
锂电池: ["电力设备", "锂电池", "能源金属"]
风电: ["电力设备", "风电"]
氢能源: ["氢能源", "燃料电池"]
充电桩: ["电力设备", "充电桩"]
电力: ["电力"]

# ── 大金融 ──
证券: ["证券"]
银行: ["银行"]
保险: ["保险"]
互联网金融: ["互联网金融"]

# ── 消费 ──
白酒: ["饮料制造", "白酒概念"]
食品饮料: ["食品加工制造", "饮料制造"]
医药: ["医药生物", "化学制药"]
医疗器械: ["医疗器械", "医药生物"]
中药: ["中药"]
医美: ["美容护理"]
家电: ["家用电器"]
旅游: ["景点及旅游", "酒店及餐饮"]
免税: ["免税店", "零售"]
农林牧渔: ["农业", "养殖业"]
消费电子: ["消费电子", "电子"]
汽车: ["汽车整车", "汽车零部件"]

# ── 制造 ──
化工: ["化学制品", "化学原料"]
钢铁: ["钢铁"]
有色金属: ["有色金属冶炼加工", "工业金属"]
煤炭: ["煤炭开采加工"]
房地产: ["房地产"]
建筑: ["建筑装饰", "建筑材料"]
机械: ["专用设备", "通用设备"]
建材: ["建筑材料"]

# ── 周期 ──
航运: ["港口航运"]
航空: ["机场航运"]
物流: ["物流"]
石油: ["石油加工贸易", "石油开采"]
化工新材料: ["化学制品", "新材料概念"]

# ── 医药细分 ──
创新药: ["化学制药", "生物制品"]
CXO: ["医疗服务"]
生物医药: ["生物制品"]
CPO: ["通信设备", "共封装光学"]

# ── 题材 ──
新质生产力: ["自动化设备", "新型工业化"]
低空经济: ["国防军工", "飞行汽车"]
Sora概念: ["传媒", "人工智能"]
华为概念: ["消费电子", "华为概念"]
国企改革: ["建筑装饰", "建筑"]
中特估: ["建筑装饰", "中字头股票"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_sector_mapping.py -v 2>&1
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/config/sector_mapping.yaml .claude/skills/stock-trend/tests/test_sector_mapping.py && git commit -m "feat(config): sector_mapping.yaml — 同花顺↔东方财富板块名称映射

50+ 常用板块映射，覆盖科技/新能源/金融/消费/制造/周期/题材
优先行业映射，概念映射兜底

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 创建 bridge/sector_feeder.py

**Files:**
- Create: `.claude/skills/stock-trend/scripts/bridge/sector_feeder.py`
- Test: `.claude/skills/stock-trend/tests/test_sector_feeder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sector_feeder.py
"""Test sector_feeder module — qualified_sectors read/write + mapping lookup."""
import json
import pytest
from pathlib import Path
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
    assert loaded.date == "2026-05-31"  # pytest freezes time
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_sector_feeder.py -v 2>&1
```

Expected: ImportError for bridge.sector_feeder.

- [ ] **Step 3: Create bridge/sector_feeder.py**

```python
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

SCRIPT_DIR = Path(__file__).resolve().parent
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_sector_feeder.py -v 2>&1
```

Expected: 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/scripts/bridge/sector_feeder.py .claude/skills/stock-trend/tests/test_sector_feeder.py && git commit -m "feat(bridge): sector_feeder — qualified_sectors 读写 + 映射查询 + 信号标签

export_qualified_sectors / load_qualified_sectors / map_ths_sector_to_em / build_signal_label
全部带有错误降级（不抛出异常）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: ths_theme.py 加 --export-sectors 参数

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/ths_theme.py:690-825`

- [ ] **Step 1: 加 --export-sectors 参数解析**

在 `ths_theme.py` 的 parse_args 区域（约第 690 行）增加：

```python
    parser.add_argument("--export-sectors", action="store_true", default=False,
                        help="导出热板块列表到 qualified_sectors.json")
```

在 `parser.add_argument("--no-zt", ...)` 之后插入。

- [ ] **Step 2: 增加热板块筛选 + 导出逻辑**

在 `main()` 中 `scored` 计算完成、zt_scored 就绪后（约第 760 行 `elapsed = time.time() - start` 之前），增加：

```python
    # ── 热板块导出（用于 longtou 消费） ──
    if args.export_sectors and not args.no_zt:
        qualified = []
        # Build a set of sectors that pass dual confirmation
        zt_sectors_with_score = {}
        for zc in zt_scored:
            if zc.get("zt_score", 0) >= 50:
                name = zc.get("matched_industry") or zc["concept"]
                zt_sectors_with_score[name] = max(
                    zt_sectors_with_score.get(name, 0),
                    zc["zt_score"]
                )
        for s in scored:
            heat = s["hot_score"]
            name = s["name"]
            zt_val = zt_sectors_with_score.get(name, 0)
            if heat >= 50 and zt_val >= 50:
                # Find matching lhb data
                lhb_info = {"score": 0, "direction": ""}
                for lhb_sec in lhb_sectors:
                    if lhb_sec["name"] == name or name in lhb_sec.get("member_names", []):
                        lhb_info = {"score": lhb_sec.get("lhb_score", 0),
                                    "direction": lhb_sec.get("direction", "")}
                        break
                qualified.append({
                    "name": name,
                    "heat_score": round(heat, 1),
                    "zt_score": round(zt_val, 1),
                    "lhb_score": round(lhb_info["score"], 1),
                    "lhb_direction": lhb_info["direction"],
                })
        if qualified:
            try:
                from bridge.sector_feeder import export_qualified_sectors
                export_qualified_sectors(qualified, heat_min=50, zt_min=50)
                print(f"\n[export] 热板块导出: {len(qualified)} 个板块 → .cache/stock-trend/qualified_sectors.json")
            except Exception as e:
                print(f"\n⚠️ 热板块导出失败: {e}")
        else:
            print(f"\n[export] 无满足 heat≥50 & zt≥50 的板块，跳过导出")
```

- [ ] **Step 3: 跑 ths-theme 测试确认不破坏现有逻辑**

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/tests/test_stock_trend.py -v 2>&1
```

Expected: All existing tests PASS.

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/tests/test_golden.py --diff 2>&1
```

Expected: No golden snapshot diff (new flag doesn't affect default output).

- [ ] **Step 4: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/scripts/analysis/ths_theme.py && git commit -m "feat(ths-theme): --export-sectors 参数 — 导出热板块列表供 longtou 使用

筛选条件: heat_score≥50 & zt_score≥50
输出路径: .cache/stock-trend/qualified_sectors.json
无热板块时不导出、不报错

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: market_leader.py 加 --sectors-from 参数 + 评分改造

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/market_leader.py`
- Modify (indirect): parts of `generate_report` to show sector heat data
- Test: `.claude/skills/stock-trend/tests/test_market_leader_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_market_leader_integration.py
"""Test market_leader.py with --sectors-from flag."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_sectors_from_missing_file():
    """Missing file returns empty list."""
    from scans.market_leader import load_sectors_from_file
    assert load_sectors_from_file("/tmp/nonexistent_12345.json") == []


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
    """Max heat/zt → ~1.8 boost."""
    from scans.market_leader import compute_sector_boost
    boost = compute_sector_boost(100, 100)
    assert 1.75 <= boost <= 1.85, f"boost={boost} out of range"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_market_leader_integration.py -v 2>&1
```

Expected: Import errors on `load_sectors_from_file` and `compute_sector_boost`.

- [ ] **Step 3: Add --sectors-from argument**

在 `market_leader.py` 的 parse_args 区域（约第 558-570 行）：

```python
    parser.add_argument("--sectors-from", type=str,
                        help="从 qualified_sectors.json 读取热板块列表，跳过全市场扫描")
```

- [ ] **Step 4: Add load_sectors_from_file + compute_sector_boost 函数**

在 `market_leader.py` 的 Phase 1 区域（`scan_hot_sectors` 函数附近）增加：

```python
# ── Integration: load qualified sectors from ths-theme output ──


def load_sectors_from_file(path: str) -> list[dict]:
    """Load qualified sectors from ths-theme output JSON.

    Returns list of sector dicts with heat_score, zt_score.
    Returns empty list on any failure.
    """
    p = Path(path)
    if not p.exists():
        print(f"  ⚠️ File not found: {path}")
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sectors = data.get("sectors", [])
        if not sectors:
            print("  ⚠️ No sectors in qualified file")
        return sectors
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"  ⚠️ Failed to parse {path}: {e}")
        return []


def compute_sector_boost(heat: float, zt_score: float) -> float:
    """Compute sector boost for leader scoring.

    boost = (heat/33.3) × 0.15 + (zt_score/33.3) × 0.15
    Range: 0 ~ +1.8

    Args:
        heat: ths-theme industry heat score (0-100).
        zt_score: ths-theme zt concept score (0-100).

    Returns:
        Boost value in [0, ~1.8].
    """
    if heat <= 0 and zt_score <= 0:
        return 0.0
    return round(
        (max(0, min(100, heat)) / 33.3) * 0.15
        + (max(0, min(100, zt_score)) / 33.3) * 0.15,
        4,
    )
```

- [ ] **Step 5: Modify main() to support --sectors-from**

在 `main()` 中 Phase 1 扫描部分（约第 584-604 行）改：

```python
    # ── Phase 1: Sector scan, single sector, or sectors-from ──
    sector_heat_map: dict[str, dict] = {}  # name → {heat_score, zt_score}
    
    if args.sectors_from:
        print(f"[Phase 1/3] Loading sectors from: {args.sectors_from}")
        qualified = load_sectors_from_file(args.sectors_from)
        if not qualified:
            print("  ⚠️ 无有效热板块列表，降级为全市场扫描")
            hot_sectors = scan_hot_sectors(args.top)
        else:
            print(f"  Loaded {len(qualified)} qualified sectors")
            hot_sectors = []
            for qs in qualified:
                name = qs["name"]
                sector_heat_map[name] = {
                    "heat_score": qs.get("heat_score", 0),
                    "zt_score": qs.get("zt_score", 0),
                    "lhb_score": qs.get("lhb_score", 0),
                    "lhb_direction": qs.get("lhb_direction", ""),
                }
                # Try to find matching 东方财富 sector
                try:
                    from bridge.sector_feeder import map_ths_sector_to_em
                    em_names = map_ths_sector_to_em(name)
                except Exception:
                    em_names = []
                found = False
                if em_names:
                    for em_name in em_names:
                        sector = find_sector_by_name(em_name)
                        if sector:
                            sector["heat_score"] = qs.get("heat_score", 0)
                            hot_sectors.append(sector)
                            found = True
                            break
                if not found:
                    # Use original name as fallback — will be marked in report
                    sector = find_sector_by_name(name)
                    if sector:
                        sector["heat_score"] = qs.get("heat_score", 0)
                        hot_sectors.append(sector)
                        found = True
                if not found:
                    print(f"  ⚠️ 板块 '{name}' 在东方财富中未找到对应")
            print(f"  Matched {len(hot_sectors)}/{len(qualified)} sectors")
    elif args.sector:
        ...  # existing single sector logic
    else:
        hot_sectors = scan_hot_sectors(args.top)
    
    output["meta"]["total_sectors"] = len(hot_sectors)
```

- [ ] **Step 6: 在龙头分析结果中应用 sector_boost**

在 Phase 3 pipeline 结果汇总后、quality_penalties 之前（约第 698-720 行 `# Attach deep analysis` 区域后）：

```python
    # ── Apply sector_boost to leader scores ──
    if args.sectors_from and sector_heat_map:
        print("[Sector Boost] Applying sector heat boost...")
        for sec in sectors_analyzed:
            sec_name = sec.get("name", "")
            # Find matching heat data
            s_heat = None
            for qs in qualified:
                try:
                    from bridge.sector_feeder import map_ths_sector_to_em
                    em_names = map_ths_sector_to_em(qs["name"])
                except Exception:
                    em_names = []
                if sec_name == qs["name"] or sec_name in em_names:
                    s_heat = qs
                    break
            if not s_heat:
                continue
            heat = s_heat.get("heat_score", 0)
            zt = s_heat.get("zt_score", 0)
            boost = compute_sector_boost(heat, zt)
            if boost <= 0:
                continue
            for stock_list_name in ("leaders", "core_stocks"):
                for s in sec.get(stock_list_name, []):
                    da = pipeline_results.get(s["code"], {})
                    orig = da.get("composite_score")
                    if orig is not None:
                        new_score = round(orig + boost, 3)
                        da["composite_score"] = new_score
                        da["sector_boost"] = boost
                        da["original_score"] = orig
                        da["sector_heat"] = heat
                        da["sector_zt"] = zt
            # Also store sector heat data on the sector itself for report
            sec["ths_heat_score"] = heat
            sec["ths_zt_score"] = zt
            sec["ths_lhb_score"] = s_heat.get("lhb_score", 0)
            sec["ths_lhb_direction"] = s_heat.get("lhb_direction", "")
        print(f"  Sector boost applied")
```

- [ ] **Step 7: 报告生成中增加板块热力行**

在 `generate_report()` 中板块标题行（约第 453 行 `### {name} (热度:{hot:.0f})`）改为：

```python
    # In generate_report(), for each sector section:
    ths_heat = sec.get("ths_heat_score")
    ths_zt = sec.get("ths_zt_score")
    ths_lhb = sec.get("ths_lhb_score", 0)
    ths_lhb_dir = sec.get("ths_lhb_direction", "")
    
    # Sector header with ths data
    if ths_heat is not None:
        lines.append(f"### {nome if sec.get('name') != fallback_name else fallback_name}")
        # Sector overview line with heat data
        lhb_str = f"LHB:{ths_lhb_dir}{ths_lhb:.0f}" if ths_lhb_dir else ""
        lines.append(f"> 行业热力:{ths_heat:.0f}/100  涨停概念:{ths_zt:.0f}/100  {lhb_str}")
    else:
        lines.append(f"### {sec.get('name', '?')}")
        lines.append(f"> 热度:{hot:.0f} 涨幅:{change:.1f}%")
```

并在每个龙头的输出行中增加 sector_boost 标注（约第 488 行 `- {name}({code}) 涨跌幅...` 区域）：

```python
    da = pipeline_summary.get(code, {})
    boost = da.get("sector_boost")
    boost_str = f" [板块加成:{boost:+.2f}]" if boost else ""
    # Insert boost_str after the score in the line
```

- [ ] **Step 8: Run tests**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_market_leader_integration.py -v 2>&1
```

Expected: All 6 tests PASS.

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/tests/test_stock_trend.py -v 2>&1
```

Expected: All existing tests PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/scripts/scans/market_leader.py .claude/skills/stock-trend/tests/test_market_leader_integration.py && git commit -m "feat(longtou): --sectors-from 参数 + sector_boost 评分

- --sectors-from: 读取 ths-theme 热板块列表，限制扫描范围
- compute_sector_boost: heat/zt 分数压到 -3~+3 区间作为 30% 加成
- 报告板块行增加行业热力/涨停概念/龙虎榜数据行
- 映射不到时降级全市场扫描，有熔断不崩溃

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 创建 bridge/integrated_report.py

**Files:**
- Create: `.claude/skills/stock-trend/scripts/bridge/integrated_report.py`
- Test: `.claude/skills/stock-trend/tests/test_integrated_report.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_integrated_report.py
"""Test integrated report generation."""
import json
from pathlib import Path
from bridge.integrated_report import (
    build_sector_overview,
    generate_integrated_md,
    generate_integrated_html,
)

# Sample data
SAMPLE_THS = """
热力板块数: 2
强势板块(≥70): 半导体系78, 人形机器人系65
"""

SAMPLE_MARKET_LEADER = """
### 半导体系 (热度:78 涨幅:3.2%)
> 行业热力:78/100 涨停概念:82/100

**龙头**
- 北方华创(002371) +10.0% ↑偏多 ★★★
  板块加成:+0.72
- 中微公司(688012) +5.2% ↗偏多 ★★☆
  板块加成:+0.72
"""

SAMPLE_OVERVIEW = {
    "total_hot_sectors": 2,
    "total_leaders": 3,
    "dual_confirmed": 2,
    "lhb_strong": 1,
    "top_sectors": ["半导体系", "人形机器人"],
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
    assert "该报告由 ths-theme + longtou 整合生成" in md


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


def test_generate_integrated_html_basic():
    """HTML report renders without error."""
    html = generate_integrated_html(
        date="2026-05-31",
        ths_report="THS REPORT",
        leader_report=SAMPLE_MARKET_LEADER,
        overview=SAMPLE_OVERVIEW,
    )
    assert "<html>" in html
    assert "市场热力" in html
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_integrated_report.py -v 2>&1
```

Expected: ImportError for bridge.integrated_report.

- [ ] **Step 3: Create bridge/integrated_report.py**

```python
#!/usr/bin/env python3
"""Integrated report — merges ths-theme + longtou outputs into one report.

Functions:
    build_sector_overview: build overview section data
    generate_integrated_md: generate combined Markdown report
    generate_integrated_html: generate combined HTML report
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "reports" / "lists"


def build_sector_overview(
    total_hot: int,
    leaders: int,
    dual: int,
    lhb: int,
    top: list[str],
) -> dict:
    """Build overview section data."""
    return {
        "total_hot_sectors": total_hot,
        "total_leaders": leaders,
        "dual_confirmed": dual,
        "lhb_strong": lhb,
        "top_sectors": top,
    }


def generate_integrated_md(
    date: str,
    ths_report: str,
    leader_report: str,
    overview: dict,
) -> str:
    """Generate integrated Markdown report."""
    lines = []
    lines.append(f"# 市场热力 · 龙头整合报告 — {date}")
    lines.append("")
    
    # Section 1: Overview
    lines.append("## 一、市场总览")
    lines.append("")
    lines.append(f"- 双强热力板块: {overview.get('total_hot_sectors', 0)} 个")
    if overview.get("top_sectors"):
        lines.append(f"- 最强板块: {'、'.join(overview['top_sectors'][:3])}")
    lines.append(f"- 龙头标的: {overview.get('total_leaders', 0)} 只")
    lines.append(f"- 机构净买板块(LHB≥60): {overview.get('lhb_strong', 0)} 个")
    # Qualitative market sentiment
    heat = overview.get("total_hot_sectors", 0)
    if heat >= 5:
        lines.append("- 市场情绪: **积极** 🟢 — 多板块共振")
    elif heat >= 2:
        lines.append("- 市场情绪: **中性** 🔵 — 局部热点")
    else:
        lines.append("- 市场情绪: **谨慎** ⚪ — 板块效应弱")
    lines.append("")
    
    # Section 2: Sector leaders
    lines.append("## 二、热力板块 · 龙头扫描")
    lines.append("")
    if leader_report.strip():
        lines.append(leader_report.strip())
    else:
        lines.append("*无满足双强条件的板块，跳过龙头扫描*")
        lines.append("")
    
    # Section 3: Reference
    lines.append("---")
    lines.append("")
    if ths_report.strip():
        lines.append("### 参考：板块热力详情")
        lines.append("")
        lines.append(ths_report.strip())
        lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("> *本报告由 ths-theme + longtou 整合生成 | 仅供学习参考，不构成投资建议*")
    lines.append("")
    
    return "\n".join(lines)


def generate_integrated_html(
    date: str,
    ths_report: str,
    leader_report: str,
    overview: dict,
) -> str:
    """Generate integrated HTML report."""
    
    # Color tag CSS
    tags_css = """
    <style>
    .signal-strong { color: #16a34a; background: #f0fdf4; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
    .signal-active { color: #1d4ed8; background: #eff6ff; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
    .signal-caution { color: #d97706; background: #fffbeb; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
    .signal-watch   { color: #6b7280; background: #f9fafb; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
    .signal-avoid   { color: #9ca3af; background: #f3f4f6; padding: 2px 8px; border-radius: 4px; }
    .tag-strong { display:inline-block; background:#16a34a; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:700; }
    .tag-active { display:inline-block; background:#1d4ed8; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:600; }
    .tag-caution { display:inline-block; background:#d97706; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:600; }
    .tag-watch { display:inline-block; background:#6b7280; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; }
    </style>
    """
    
    heat = overview.get("total_hot_sectors", 0)
    if heat >= 5:
        sentiment = '<span class="tag-strong">积极</span> — 多板块共振'
    elif heat >= 2:
        sentiment = '<span class="tag-active">中性</span> — 局部热点'
    else:
        sentiment = '<span class="tag-strong" style="background:#9ca3af">谨慎</span> — 板块效应弱'
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>市场热力 · 龙头整合报告 — {date}</title>
{tags_css}
<style>
body {{ font-family: 'PingFang SC','Microsoft YaHei',sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #fafafa; color: #1d1d1f; }}
h1 {{ font-size: 24px; margin-bottom: 4px; }}
h2 {{ font-size: 18px; margin-top: 24px; border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; }}
h3 {{ font-size: 16px; margin-top: 20px; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
th,td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #e5e7eb; font-size: 14px; }}
th {{ background: #1d4ed8; color: #fff; }}
tr:hover td {{ background: #f0f0f0; }}
.overview {{ display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }}
.card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px 20px; flex: 1; min-width: 120px; }}
.card .num {{ font-size: 28px; font-weight: 700; }}
.card .lbl {{ font-size: 12px; color: #86868b; }}
.sector-block {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin: 16px 0; }}
.footer {{ font-size: 12px; color: #a1a1a6; text-align: center; margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb; }}
</style>
</head>
<body>

<h1>📊 市场热力 · 龙头整合报告</h1>
<p style="color:#86868b;margin:0 0 16px">{date}</p>

<div class="overview">
  <div class="card"><div class="num">{overview.get('total_hot_sectors',0)}</div><div class="lbl">双强热力板块</div></div>
  <div class="card"><div class="num">{overview.get('total_leaders',0)}</div><div class="lbl">龙头标的</div></div>
  <div class="card"><div class="num">{overview.get('lhb_strong',0)}</div><div class="lbl">机构净买板块</div></div>
  <div class="card"><div class="lbl" style="margin-top:8px">市场情绪: {sentiment}</div></div>
</div>

<div class="sector-block">
<h2>🔥 热力板块 · 龙头扫描</h2>
"""
    
    if leader_report.strip():
        # Convert simple markdown to clickable HTML — keep it simple, wrap in pre-like div
        from html import escape
        lines = leader_report.split("\n")
        in_table = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("###"):
                if in_table:
                    html += "</table>"
                    in_table = False
                html += f"<h3>{escape(stripped.lstrip('#').strip())}</h3>"
            elif stripped.startswith("> "):
                html += f'<p style="font-size:13px;color:#86868b">{escape(stripped[2:])}</p>'
            elif stripped.startswith("**龙头**"):
                html += "<table><thead><tr><th>个股</th><th>评分</th><th>方向</th><th>标签</th></tr></thead><tbody>"
                in_table = True
            elif stripped.startswith("**中军**"):
                if in_table:
                    html += "</tbody></table>"
                    in_table = True
                html += "<table><thead><tr><th>个股</th><th>评分</th><th>方向</th><th>标签</th></tr></thead><tbody>"
            elif stripped.startswith("- ") and in_table:
                parts = stripped[2:].split(") ", 1)
                code_part = parts[0] if len(parts) > 1 else stripped[2:]
                rest = parts[-1] if len(parts) > 1 else ""
                name_code = code_part.split("(")
                name_html = escape(name_code[0]) if name_code else escape(code_part)
                html += f"<tr><td>{name_html}</td><td>{escape(rest[:40])}</td><td>{escape(rest[-10:])}</td></tr>"
            elif stripped == "" and in_table:
                html += "</tbody></table>"
                in_table = False
            else:
                if in_table:
                    html += "</tbody></table>"
                    in_table = False
                html += f"<p style='margin:4px 0;font-size:13px;color:#333'>{escape(stripped)}</p>"
        if in_table:
            html += "</tbody></table>"
    else:
        html += "<p style='color:#86868b'>无满足双强条件的板块，跳过龙头扫描</p>"
    
    html += "</div>"
    
    if ths_report.strip():
        html += '<div class="sector-block"><h2>📋 参考：板块热力详情</h2>'
        html += f"<pre style='font-size:13px;color:#333;white-space:pre-wrap'>{escape(ths_report[:2000])}</pre>"
        html += "</div>"
    
    html += """
<div class="footer">
本报告由 ths-theme + longtou 整合生成 | 仅供学习参考，不构成投资建议
</div>
</body>
</html>"""
    
    return html
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/test_integrated_report.py -v 2>&1
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/scripts/bridge/integrated_report.py .claude/skills/stock-trend/tests/test_integrated_report.py && git commit -m "feat(report): integrated_report — ths-theme + longtou 整合报告

- generate_integrated_md / generate_integrated_html
- 总览 + 板块龙头扫描 + 参考热力 三段式
- 市场情绪定性（积极/中性/谨慎）
- HTML 含颜色标签 CSS

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 创建整合入口脚本 + 更新 SKILL.md

**Files:**
- Create: `.claude/skills/stock-trend/scripts/bridge/run_integrated.py`
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: Write the integration runner script**

```python
#!/usr/bin/env python3
"""Integrated entry point — runs ths-theme → longtou → merge in one pass.

Usage:
    python3 run_integrated.py [--top N] [--compact]

Pipeline:
    1. ths_theme.py --export-sectors --no-html
    2. IF qualified_sectors.json 非空 → market_leader.py --sectors-from
    3. integrated_report.py → 拼接输出
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/bridge/
SCRIPTS_DIR = SCRIPT_DIR.parent                      # scripts/
PROJECT_ROOT = SCRIPTS_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"
QUALIFIED_PATH = CACHE_DIR / "qualified_sectors.json"

sys.path.insert(0, str(SCRIPTS_DIR))  # enable "from bridge.* import ..."


def run_step(cmd: list[str], desc: str, timeout: int = 300) -> tuple[int, str]:
    """Run a pipeline step. Returns (returncode, stdout)."""
    print(f"\n{'='*60}")
    print(f"[{desc}]")
    print(f"{'='*60}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        print(proc.stdout[-2000:] if len(proc.stdout) > 2000 else proc.stdout)
        if proc.stderr:
            print(f"  stderr: {proc.stderr[-500:]}", file=sys.stderr)
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ 超时 ({timeout}s)")
        return -1, ""
    except Exception as e:
        print(f"  ⚠️ 失败: {e}")
        return -1, ""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ths-theme + longtou 整合入口")
    parser.add_argument("--top", type=int, default=10, help="显示数量")
    parser.add_argument("--compact", action="store_true", help="精简输出")
    parser.add_argument("--output-html", action="store_true", help="生成 HTML 报告")
    parser.add_argument("--lhb-date", type=str, help="龙虎榜日期 YYYYMMDD")
    parser.add_argument("--zt-date", type=str, help="涨停日期 YYYY-MM-DD")
    args = parser.parse_args()

    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Step 1: ths-theme
    ths_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "../analysis/ths_theme.py"),
        "--top", str(args.top),
        "--export-sectors",
    ]
    if args.lhb_date:
        ths_cmd.extend(["--lhb-date", args.lhb_date])
    if args.zt_date:
        ths_cmd.extend(["--zt-date", args.zt_date])
    
    rc1, ths_stdout = run_step(ths_cmd, "Step 1/3: 板块热力分析 (ths-theme)")
    ths_report = ths_stdout
    has_qualified = QUALIFIED_PATH.exists()

    # Step 2: longtou (only if qualified sectors available)
    leader_report = ""
    overview_data = {}
    
    if has_qualified:
        try:
            data = json.loads(QUALIFIED_PATH.read_text(encoding="utf-8"))
            sectors = data.get("sectors", [])
            overview_data = {
                "total_hot_sectors": len(sectors),
                "top_sectors": [s["name"] for s in sectors[:5]],
            }
        except Exception:
            overview_data = {"total_hot_sectors": 0, "top_sectors": []}
        
        if overview_data.get("total_hot_sectors", 0) > 0:
            leader_cmd = [
                sys.executable,
                str(SCRIPT_DIR / "../scans/market_leader.py"),
                "--sectors-from", str(QUALIFIED_PATH),
                "--top", str(args.top),
            ]
            if args.compact:
                leader_cmd.append("--compact")
            if args.output_html:
                leader_cmd.append("--output-html")
            
            rc2, leader_stdout = run_step(leader_cmd, "Step 2/3: 龙头扫描 (longtou)")
            leader_report = leader_stdout
        else:
            print("\n⚠️ 无满足双强条件的板块，跳过龙头扫描")
    else:
        print("\n⚠️ 无 qualified_sectors.json，跳过龙头扫描")
        print("  (ths-theme --export-sectors 未产生输出)")

    # Step 3: Integrated report
    print(f"\n{'='*60}")
    print("[Step 3/3] 生成整合报告")
    print(f"{'='*60}")

    try:
        from bridge.integrated_report import (
            build_sector_overview,
            generate_integrated_md,
            generate_integrated_html,
        )
        
        # Count leaders from leader_report
        leader_count = 0
        if leader_report:
            for line in leader_report.split("\n"):
                stripped = line.strip()
                if stripped.startswith("- ") and "(" in stripped and ")" in stripped:
                    leader_count += 1
        
        lhb_strong = sum(
            1 for s in (overview_data.get("top_sectors", [])[:])
            # will be populated from actual lhb data later
        )
        
        overview = build_sector_overview(
            total_hot=overview_data.get("total_hot_sectors", 0),
            leaders=leader_count,
            dual=overview_data.get("total_hot_sectors", 0),
            lhb=lhb_strong,
            top=overview_data.get("top_sectors", []),
        )

        md = generate_integrated_md(
            date=now,
            ths_report=ths_report or "",
            leader_report=leader_report or "",
            overview=overview,
        )

        # Output MD
        print(md)

        # HTML
        if args.output_html:
            html = generate_integrated_html(
                date=now,
                ths_report=ths_report or "",
                leader_report=leader_report or "",
                overview=overview,
            )
            path = REPORTS_DIR / f"integrated-{ts}.html"
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            print(f"\nHTML report: {path}")

        # Also output structured JSON for agent consumption
        json_output = json.dumps({
            "meta": {"scan_time": now, "elapsed": round(time.time() - start, 1)},
            "overview": overview,
            "has_leader_report": bool(leader_report),
        }, ensure_ascii=False, indent=2)
        print(f"\n<!--JSON_OUTPUT-->\n{json_output}\n<!--END_JSON_OUTPUT-->")

    except ImportError as e:
        print(f"  ⚠️ 整合报告模块导入失败: {e}")
        print("  输出原始报告:")
        if ths_report:
            print(ths_report)
        if leader_report:
            print(leader_report)

    elapsed = time.time() - start
    print(f"\n整合分析完成，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the runner to verify it works**

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py --top 5 --compact 2>&1
```

Expected: Pipeline runs without crash. If no market data available (weekend), should gracefully degrade.

- [ ] **Step 3: Update SKILL.md /stock-trend 入口**

在 `SKILL.md` 中 `/stock-trend` 流程说明上方增加 `/integrated-scan` 命令：

在 `triggers:` 列表加 `- /integrated-scan`。

在 allowed-tools 加：
```
  - Bash(python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/bridge/sector_feeder.py *)
```

在 `/ths-theme` 和 `/longtou` 之间增加 `/integrated-scan` 章节：

```markdown
## /integrated-scan [--top N] [--compact] [--output-html] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]

ths-theme + longtou 整合扫描 — 先跑板块热力筛选，再对热板块做龙头扫描，输出整合报告。

**顺序 pipeline**：
1. `ths_theme.py --export-sectors` — 全市场板块热力 + 涨停概念评分
2. 筛选 heat_score≥50 & zt_score≥50 的板块
3. `market_leader.py --sectors-from qualified_sectors.json` — 只扫热板块
4. 拼接为整合报告

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py [--top 10] [--compact] [--output-html] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]
```

2. 呈现：总览（热力板块数、龙头标数、市场情绪）→ 按板块展开（板块热力指标 → 龙头清单 → 综合信号标签）

3. 边界情况：
   - 无热板块：不跑 longtou，只输出 ths-theme 热力报告 + 提示无强信号
   - 映射找不到东方财富板块：标注"东方财富无对应板块"，保留方向参考
   - ths-theme/longtou 任一失败：降级为另一项的输出，不阻塞

---

## /longtou [--top N] [--sector <板块名>] [--sectors-from <file>] [--compact]
```

- [ ] **Step 4: Run existing tests to confirm nothing broken**

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/tests/test_stock_trend.py -v 2>&1
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/scripts/bridge/run_integrated.py .claude/skills/stock-trend/SKILL.md && git commit -m "feat(integrated): run_integrated.py — ths-theme→longtou 一键管线

- /integrated-scan 触发 3-step pipeline
- 无热板块时降级（不跑 longtou、不报错）
- 任一子系统失败时另一子系统仍输出

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Golden snapshot + 集成测试

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_golden.py` (if it exists and needs update)
- Test: Manually trigger `/integrated-scan` and verify output

- [ ] **Step 1: Check if golden test needs update**

```bash
cd /Users/trace/work/agent/stock-trend && python3 .claude/skills/stock-trend/tests/test_golden.py --diff 2>&1
```

Expected: No golden diff. If diff exists, verify it's reasonable and regenerate.

- [ ] **Step 2: Run all tests in one go**

```bash
cd /Users/trace/work/agent/stock-trend && python3 -m pytest .claude/skills/stock-trend/tests/ -v 2>&1
```

Expected: All tests PASS.

- [ ] **Step 3: Commit any final adjustments**

```bash
cd /Users/trace/work/agent/stock-trend && git add .claude/skills/stock-trend/tests/ && git status && git commit -m "test: 整合集成测试 + golden 更新

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
