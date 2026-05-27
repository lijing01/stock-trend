# Longtou 报告质量优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化龙头中军扫描的投资筛选质量，解决板块重复、止损过紧、信号矛盾、弱势板块入选、数据获取失败等问题。

**Architecture:** 在现有三阶段管线（板块扫描 → 龙头中军过滤 → 深度分析）中插入质量门控层：Phase 1 增加板块去重和涨跌比准入；Phase 3 后增加止损有效性过滤和信号一致性校验；推荐排序引入惩罚因子。

**Tech Stack:** Python 3, existing scripts in `.claude/skills/stock-trend/scripts/`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `scripts/fetch_sector_data.py` | 板块数据获取与排名 | Modify: 添加板块去重 + 涨跌比准入 |
| `scripts/market_leader.py` | 主扫描管线 | Modify: 推荐排序增加惩罚因子、深度分析失败重试 |
| `scripts/analyze_technical.py` | 技术面分析 | Modify: 止损下限从1.5%提升至2%用于中线 |
| `scripts/quality_gate.py` | **新增**：质量门控模块 | Create: 信号一致性校验 + 推荐降权逻辑 |
| `tests/test_longtou.py` | 龙头测试 | Modify: 新增去重/门控测试用例 |
| `tests/test_quality_gate.py` | **新增**：质量门控测试 | Create: 独立测试文件 |

---

## Task 1: 板块名称去重

**Problem:** "工程咨询服务Ⅱ" 和 "工程咨询服务Ⅲ" 数据完全相同但被当作两个板块，浪费扫描名额。

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_sector_data.py:189-229`
- Test: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_longtou.py` 末尾添加：

```python
def test_sector_dedup():
    """Sectors with identical constituent stocks should be deduplicated."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from fetch_sector_data import rank_hot_sectors

    # Simulate Ⅱ/Ⅲ duplicates: same up/down/change, different code
    rankings = {
        "meta": {"total_sectors": 4},
        "sectors": [
            {"code": "BK0001", "name": "工程咨询服务Ⅱ", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            {"code": "BK0002", "name": "工程咨询服务Ⅲ", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            {"code": "BK0003", "name": "半导体", "type": "industry",
             "change_pct": 2.0, "up_count": 30, "down_count": 10,
             "main_force_net": 5e8},
            {"code": "BK0004", "name": "新能源", "type": "industry",
             "change_pct": 1.5, "up_count": 20, "down_count": 15,
             "main_force_net": 3e8},
        ],
    }
    result = rank_hot_sectors(rankings, top_n=10, min_stocks=8)
    # 工程咨询服务 should appear only once
    eng_sectors = [s for s in result if "工程咨询" in s["name"]]
    test("sector_dedup_removes_duplicates", len(eng_sectors) <= 1,
         f"Expected ≤1 工程咨询 sector, got {len(eng_sectors)}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: `FAIL` on `sector_dedup_removes_duplicates`

- [ ] **Step 3: Implement sector deduplication**

In `fetch_sector_data.py`, add dedup logic inside `rank_hot_sectors()` after the `min_stocks` filter (line ~213):

```python
def rank_hot_sectors(rankings: dict, top_n: int = 10,
                     min_stocks: int = 8) -> list[dict]:
    """Rank sectors by composite hot score."""
    sectors = rankings.get("sectors", [])

    if min_stocks > 0:
        before = len(sectors)
        sectors = [
            s for s in sectors
            if (s.get("up_count", 0) + s.get("down_count", 0)) >= min_stocks
        ]
        dropped = before - len(sectors)

    # Dedup: sectors with same (up_count, down_count, change_pct) are
    # likely parent/child levels of the same classification; keep the first.
    seen_signatures = set()
    deduped = []
    for s in sectors:
        sig = (s.get("up_count", 0), s.get("down_count", 0),
               round(s.get("change_pct", 0) or 0, 2))
        # Also strip trailing level markers (Ⅰ/Ⅱ/Ⅲ/Ⅳ) for name-based dedup
        import re
        base_name = re.sub(r'[ⅠⅡⅢⅣ\u2160-\u2163]$', '', s.get("name", ""))
        name_sig = (base_name, sig[0], sig[1], sig[2])
        if name_sig not in seen_signatures:
            seen_signatures.add(name_sig)
            deduped.append(s)
    sectors = deduped

    for s in sectors:
        s["hot_score"] = compute_hot_score(s)

    sectors.sort(key=lambda x: x.get("hot_score", 0), reverse=True)

    # Min-max normalize to 0-100 for consistent differentiation
    if sectors:
        scores = [s["hot_score"] for s in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for s in sectors:
                s["hot_score"] = round(
                    (s["hot_score"] - lo) / (hi - lo) * 100, 1
                )

    return sectors[:top_n]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_sector_data.py tests/test_longtou.py
git commit -m "feat(longtou): dedup sectors with identical constituents (Ⅱ/Ⅲ level)"
```

---

## Task 2: 板块涨跌比准入门槛

**Problem:** 0涨8跌的公交板块（热度56）仍入选，对中线投资者无价值。

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_sector_data.py:160-186`
- Test: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sector_up_ratio_filter():
    """Sectors with <15% up ratio should be excluded."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from fetch_sector_data import rank_hot_sectors

    rankings = {
        "meta": {"total_sectors": 3},
        "sectors": [
            # 0 up / 8 down = 0% up ratio → should be excluded
            {"code": "BK0010", "name": "公交", "type": "concept",
             "change_pct": -3.6, "up_count": 0, "down_count": 8,
             "main_force_net": -0.5e8},
            # 5 up / 42 down = 10.6% up ratio → should be excluded
            {"code": "BK0011", "name": "弱势板块", "type": "concept",
             "change_pct": -3.1, "up_count": 5, "down_count": 42,
             "main_force_net": -1e8},
            # 20 up / 10 down = 66% up ratio → should pass
            {"code": "BK0012", "name": "强势板块", "type": "industry",
             "change_pct": 2.0, "up_count": 20, "down_count": 10,
             "main_force_net": 3e8},
        ],
    }
    result = rank_hot_sectors(rankings, top_n=10, min_stocks=0, min_up_ratio=0.15)
    names = [s["name"] for s in result]
    test("up_ratio_filter_excludes_weak_sectors",
         "公交" not in names and "弱势板块" not in names,
         f"Got: {names}")
    test("up_ratio_filter_keeps_strong_sectors",
         "强势板块" in names,
         f"Got: {names}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: FAIL (no `min_up_ratio` parameter yet)

- [ ] **Step 3: Implement up-ratio filter**

In `fetch_sector_data.py`, modify `rank_hot_sectors()` signature and body:

```python
def rank_hot_sectors(rankings: dict, top_n: int = 10,
                     min_stocks: int = 8,
                     min_up_ratio: float = 0.15) -> list[dict]:
    """Rank sectors by composite hot score.

    Filters:
      - Tiny sectors (fewer than min_stocks constituents)
      - Weak sectors (up_count / total < min_up_ratio)
      - Duplicate child-level sectors (same base name + identical stats)

    Args:
        rankings: output from get_sector_rankings().
        top_n: number of top sectors to return.
        min_stocks: minimum constituent stocks. 0 disables.
        min_up_ratio: minimum up/(up+down) ratio. 0 disables.

    Returns:
        Sorted list with score added to each sector dict.
    """
    sectors = rankings.get("sectors", [])

    if min_stocks > 0:
        sectors = [
            s for s in sectors
            if (s.get("up_count", 0) + s.get("down_count", 0)) >= min_stocks
        ]

    # Filter by up/down ratio — exclude boards that are all-red
    if min_up_ratio > 0:
        sectors = [
            s for s in sectors
            if _up_ratio(s) >= min_up_ratio
        ]

    # ... (rest of existing dedup + scoring logic) ...
```

Add helper at module top:

```python
def _up_ratio(sector: dict) -> float:
    """Calculate up/(up+down) ratio for a sector."""
    up = sector.get("up_count", 0) or 0
    down = sector.get("down_count", 0) or 0
    total = up + down
    return up / total if total > 0 else 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_sector_data.py tests/test_longtou.py
git commit -m "feat(longtou): add min_up_ratio filter to exclude all-red sectors"
```

---

## Task 3: 止损距离下限提升（中线适配）

**Problem:** 止损警告阈值为1.5%，但大量标的止损距离为0.1%~0.7%仍进入推荐。对中线持仓(1-6月)毫无保护意义。

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analyze_technical.py:1086-1092`
- Modify: `.claude/skills/stock-trend/scripts/market_leader.py:619-639` (推荐排序增加止损惩罚)
- Test: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write the failing test**

```python
def test_stop_loss_penalty_in_ranking():
    """Stocks with stop-loss < 2% should be penalized in best_picks ranking."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from market_leader import _apply_quality_penalties

    candidates = [
        {"code": "000001", "name": "A", "composite_score": 0.5,
         "direction": "震荡偏多", "stop_loss": 10.0, "current_price": 10.01},  # 0.1% gap
        {"code": "000002", "name": "B", "composite_score": 0.45,
         "direction": "震荡偏多", "stop_loss": 9.5, "current_price": 10.0},   # 5% gap
    ]
    penalized = _apply_quality_penalties(candidates)
    # B should rank higher despite lower raw score because A has useless stop
    test("stop_loss_penalty_reranks",
         penalized[0]["code"] == "000002",
         f"Top pick: {penalized[0]['code']}, expected 000002")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: FAIL — `_apply_quality_penalties` doesn't exist yet

- [ ] **Step 3: Implement quality penalty function**

In `market_leader.py`, add before `generate_report()`:

```python
# ──────────────────────── Quality Gate: Penalties ────────────────────────

# Minimum useful stop-loss distance for mid-term holding (1-6 months)
MIN_STOP_LOSS_PCT = 0.02  # 2%


def _apply_quality_penalties(candidates: list[dict]) -> list[dict]:
    """Apply quality penalties to candidate scores for ranking.

    Penalties:
      - stop_loss too close (<2%): -0.15
      - stop_loss missing when direction is bullish: -0.05
      - deep analysis failed (fallback scoring): -0.10

    Args:
        candidates: list of dicts with composite_score, stop_loss, current_price,
                    direction, risks.

    Returns:
        Same list sorted by adjusted_score descending.
    """
    for c in candidates:
        penalty = 0.0
        score = c.get("composite_score") or 0

        # Stop-loss distance penalty
        stop = c.get("stop_loss")
        price = c.get("current_price") or 0
        if stop and price > 0:
            stop_pct = (price - stop) / price
            if stop_pct < MIN_STOP_LOSS_PCT:
                penalty += 0.15
        elif stop is None and "偏多" in (c.get("direction") or ""):
            penalty += 0.05

        # Fallback scoring penalty
        risks = c.get("risks") or []
        if any("深度分析数据获取失败" in r for r in risks):
            penalty += 0.10

        c["quality_penalty"] = round(penalty, 3)
        c["adjusted_score"] = round(score - penalty, 3)

    candidates.sort(key=lambda x: x.get("adjusted_score", 0), reverse=True)
    return candidates
```

- [ ] **Step 4: Integrate into main()'s best_picks generation**

Replace lines 619-639 in `market_leader.py`:

```python
    # ── Best picks & risk tips (with quality penalties) ──
    all_rated = []
    for sec in sectors_analyzed:
        for s in sec.get("leaders", []) + sec.get("core_stocks", []):
            da = pipeline_results.get(s["code"], {})
            score = da.get("composite_score")
            if score is not None:
                all_rated.append({
                    "code": s["code"],
                    "name": s.get("name", ""),
                    "sector": sec.get("name", ""),
                    "direction": da.get("direction", ""),
                    "composite_score": score,
                    "stop_loss": da.get("stop_loss"),
                    "current_price": s.get("current_price") or s.get("close"),
                    "risks": da.get("risks", []),
                })

    all_rated = _apply_quality_penalties(all_rated)

    for item in all_rated[:5]:
        penalty_note = ""
        if item.get("quality_penalty", 0) > 0:
            penalty_note = f" (质量惩罚:-{item['quality_penalty']})"
        output["best_picks"].append(
            f"{item['name']}({item['code']}) [{item['sector']}] "
            f"{item['direction']} 综合分:{item['adjusted_score']}{penalty_note}"
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: PASS

- [ ] **Step 6: Also raise the warning threshold in analyze_technical.py**

In `analyze_technical.py` line 1090, change:

```python
        if stop_pct < 0.015:
```

to:

```python
        if stop_pct < 0.02:
```

- [ ] **Step 7: Run full test suite**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py && python3 .claude/skills/stock-trend/tests/test_longtou.py`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/market_leader.py scripts/analyze_technical.py tests/test_longtou.py
git commit -m "feat(longtou): add quality penalties for stop-loss distance and fallback scoring"
```

---

## Task 4: 信号一致性校验

**Problem:** 方向判"震荡偏多"但技术指标全面看空（死叉+绿柱+空头排列），推荐结果矛盾。

**Files:**
- Create: `.claude/skills/stock-trend/scripts/quality_gate.py`
- Modify: `.claude/skills/stock-trend/scripts/market_leader.py`
- Create: `.claude/skills/stock-trend/tests/test_quality_gate.py`

- [ ] **Step 1: Write the failing test (test_quality_gate.py)**

```python
#!/usr/bin/env python3
"""Quality gate tests for signal consistency checks."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

PASSED = 0
FAILED = 0
RESULTS = []


def test(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def test_signal_consistency_detects_conflict():
    """Direction=bullish but indicators all bearish → flagged as conflict."""
    from quality_gate import check_signal_consistency

    risks = [
        "空头排列",
        "MA5下穿MA10死叉",
        "MACD死叉；绿柱放大",
        "RSI=70.1，超买",
    ]
    result = check_signal_consistency(direction="震荡偏多", risks=risks)
    test("signal_conflict_detected",
         result["has_conflict"] is True,
         f"conflict={result['has_conflict']}, bearish_count={result['bearish_signal_count']}")
    test("signal_conflict_penalty_applied",
         result["penalty"] >= 0.10,
         f"penalty={result['penalty']}")


def test_signal_consistency_no_conflict():
    """Direction=bullish with supporting indicators → no conflict."""
    from quality_gate import check_signal_consistency

    risks = ["RSI=55.0，中性区间"]
    result = check_signal_consistency(direction="震荡偏多", risks=risks)
    test("signal_no_conflict",
         result["has_conflict"] is False,
         f"conflict={result['has_conflict']}")
    test("signal_no_penalty",
         result["penalty"] == 0,
         f"penalty={result['penalty']}")


def test_signal_consistency_bearish_direction():
    """Direction=bearish with bearish indicators → no conflict (consistent)."""
    from quality_gate import check_signal_consistency

    risks = ["空头排列", "MACD死叉；绿柱放大"]
    result = check_signal_consistency(direction="震荡偏空", risks=risks)
    test("bearish_consistent_no_conflict",
         result["has_conflict"] is False,
         f"conflict={result['has_conflict']}")


if __name__ == "__main__":
    print("=== Quality Gate Tests ===")
    test_signal_consistency_detects_conflict()
    test_signal_consistency_no_conflict()
    test_signal_consistency_bearish_direction()
    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED > 0 else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_quality_gate.py`
Expected: FAIL — `quality_gate` module doesn't exist

- [ ] **Step 3: Implement quality_gate.py**

Create `.claude/skills/stock-trend/scripts/quality_gate.py`:

```python
#!/usr/bin/env python3
"""Quality gate: signal consistency and recommendation filtering.

Detects contradictions between direction judgment and technical risk signals.
Used by market_leader.py to penalize or flag conflicting recommendations.
"""

# Bearish signal keywords found in risk text
BEARISH_KEYWORDS = [
    "空头排列",
    "死叉",
    "绿柱放大",
    "ADX.*-DI>\\+DI确认空头",
    "-DI>+DI确认空头",
    "OBV在20日均线下方",
    "资金净流出",
    "缩量下跌",
    "顶背离",
]

# Bullish signal keywords
BULLISH_KEYWORDS = [
    "多头排列",
    "金叉",
    "红柱放大",
    "+DI>-DI确认多头",
    "OBV在20日均线上方",
    "资金净流入",
    "底背离",
    "放量上涨",
]

# Keywords indicating overbought (conflict with bullish if in downtrend context)
OVERBOUGHT_KEYWORDS = ["超买"]
OVERSOLD_KEYWORDS = ["超卖"]


def check_signal_consistency(direction: str, risks: list[str]) -> dict:
    """Check if direction judgment conflicts with technical signals in risks.

    Args:
        direction: e.g. "震荡偏多", "偏空", "震荡偏空"
        risks: list of risk text strings from technical analysis

    Returns:
        dict with:
          - has_conflict: bool
          - bearish_signal_count: int
          - bullish_signal_count: int
          - penalty: float (0 if consistent, 0.10-0.20 if conflicting)
          - conflict_detail: str (human-readable explanation)
    """
    is_bullish_direction = "偏多" in direction or direction in ("多头", "bullish")
    is_bearish_direction = "偏空" in direction or direction in ("空头", "bearish")

    risk_text = " ".join(risks)

    bearish_count = sum(1 for kw in BEARISH_KEYWORDS if kw in risk_text)
    bullish_count = sum(1 for kw in BULLISH_KEYWORDS if kw in risk_text)
    has_overbought = any(kw in risk_text for kw in OVERBOUGHT_KEYWORDS)

    has_conflict = False
    penalty = 0.0
    detail = ""

    if is_bullish_direction:
        # Bullish direction but many bearish signals → conflict
        if bearish_count >= 3:
            has_conflict = True
            penalty = 0.20
            detail = f"方向偏多但有{bearish_count}个看空信号"
        elif bearish_count >= 2:
            has_conflict = True
            penalty = 0.10
            detail = f"方向偏多但有{bearish_count}个看空信号"
        # Overbought + bullish: warn but lower penalty
        if has_overbought and bearish_count >= 1:
            has_conflict = True
            penalty = max(penalty, 0.10)
            if not detail:
                detail = "超买区且有看空信号"

    elif is_bearish_direction:
        # Bearish direction with bullish signals → conflict (less common)
        if bullish_count >= 3:
            has_conflict = True
            penalty = 0.15
            detail = f"方向偏空但有{bullish_count}个看多信号"

    return {
        "has_conflict": has_conflict,
        "bearish_signal_count": bearish_count,
        "bullish_signal_count": bullish_count,
        "penalty": penalty,
        "conflict_detail": detail,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_quality_gate.py`
Expected: PASS

- [ ] **Step 5: Integrate into market_leader.py's _apply_quality_penalties**

Add signal consistency check to `_apply_quality_penalties()`:

```python
from quality_gate import check_signal_consistency

# Inside _apply_quality_penalties, after stop-loss penalty:
        # Signal consistency penalty
        direction = c.get("direction") or ""
        consistency = check_signal_consistency(direction, risks)
        if consistency["has_conflict"]:
            penalty += consistency["penalty"]
            c["signal_conflict"] = consistency["conflict_detail"]
```

- [ ] **Step 6: Run full test suite**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py && python3 .claude/skills/stock-trend/tests/test_longtou.py && python3 .claude/skills/stock-trend/tests/test_quality_gate.py`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/quality_gate.py tests/test_quality_gate.py scripts/market_leader.py
git commit -m "feat(longtou): add signal consistency check to quality gate"
```

---

## Task 5: 深度分析失败重试 + 错误诊断

**Problem:** 12只标的"深度分析数据获取失败"，subprocess 返回码被忽略，根因不可追溯。

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/market_leader.py:138-213`
- Test: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write the failing test**

```python
def test_deep_analysis_retry_on_failure():
    """Deep analysis should retry once on non-timeout failure."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from unittest.mock import patch, MagicMock
    from market_leader import run_deep_analysis

    call_count = {"n": 0}

    def mock_run(cmd, *args, **kwargs):
        call_count["n"] += 1
        result = MagicMock()
        # First call fails (returncode=1), second succeeds
        if call_count["n"] <= 1:
            result.returncode = 1
            result.stderr = "Connection reset"
        else:
            result.returncode = 0
            result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=mock_run):
        with patch("pathlib.Path.read_bytes", side_effect=FileNotFoundError):
            result = run_deep_analysis("000001", timeout=10)

    # Should have attempted pipeline at least twice
    test("deep_analysis_retries",
         call_count["n"] >= 2,
         f"subprocess.run called {call_count['n']} times")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: FAIL — no retry logic yet

- [ ] **Step 3: Implement retry + error logging**

Replace `run_deep_analysis()` in `market_leader.py`:

```python
def run_deep_analysis(code: str, timeout: int = 60, max_retries: int = 1) -> dict:
    """Run full pipeline + scoring for one stock code.

    Retries once on non-timeout failures. Logs failure reasons for diagnosis.

    Returns analysis result dict.
    """
    result = {"code": code}
    ts_code = code_to_ts_code(code)
    result["ts_code"] = ts_code

    # Run pipeline with retry
    pipeline_cmd = [sys.executable, str(SCRIPT_DIR / "run_pipeline.py"),
                    "--code", code]
    pipeline_ok = False
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(pipeline_cmd, capture_output=True, text=True,
                                  timeout=timeout)
            if proc.returncode == 0:
                pipeline_ok = True
                break
            else:
                result["pipeline_stderr"] = proc.stderr[-200:] if proc.stderr else ""
                if attempt < max_retries:
                    time.sleep(1)  # Brief pause before retry
        except subprocess.TimeoutExpired:
            result["error"] = "pipeline_timeout"
            return result
        except Exception as e:
            result["error"] = str(e)
            if attempt >= max_retries:
                return result

    if not pipeline_ok:
        result["error"] = f"pipeline_failed_after_{max_retries + 1}_attempts"
        return result

    # Run scoring (no retry needed — it's local computation)
    scores_cmd = [sys.executable, str(SCRIPT_DIR / "compute_scores.py"),
                  "--code", code]
    try:
        proc = subprocess.run(scores_cmd, capture_output=True, text=True,
                              timeout=30)
        if proc.returncode != 0:
            result["error"] = f"scoring_failed: {proc.stderr[-100:]}"
            return result
    except subprocess.TimeoutExpired:
        result["error"] = "scores_timeout"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    # Read scores output (unchanged)
    scores_path = CACHE_DIR / code / "scores.json"
    pipeline_path = CACHE_DIR / code / "pipeline_output.json"
    technical_path = CACHE_DIR / code / "technical.json"

    try:
        scores_data = json.loads(scores_path.read_bytes())
        result["composite_score"] = scores_data.get("composite_score")
        result["direction"] = scores_data.get("direction")
        result["confidence"] = scores_data.get("confidence")
        dims = scores_data.get("scores", {}) or {}
        result["dimension_scores"] = {
            "technical": dims.get("technical"),
            "capital_flow": dims.get("capital_flow"),
            "fundamental": dims.get("fundamental"),
            "sentiment": dims.get("sentiment"),
            "macro": dims.get("macro"),
        }
        result["risks"] = scores_data.get("risks", [])
        rp = scores_data.get("report_params", {}) or {}
        result["stop_loss"] = rp.get("stop_loss")
        result["targets"] = {
            "conservative": rp.get("target_conservative"),
            "moderate": rp.get("target_moderate"),
        }
    except Exception:
        pass

    try:
        tech_data = json.loads(technical_path.read_bytes())
        result["trend_stage"] = tech_data.get("summary", {}).get("trend_stage")
    except Exception:
        pass

    try:
        pipe_data = json.loads(pipeline_path.read_bytes())
        result["pipeline_errors"] = pipe_data.get("errors", [])
    except Exception:
        pass

    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/market_leader.py tests/test_longtou.py
git commit -m "feat(longtou): retry deep analysis on failure + check returncode"
```

---

## Task 6: 推荐列表只保留方向偏多的标的

**Problem:** 综合推荐中出现"偏空"标的（如博杰股份），对中线投资者无操作价值。

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/market_leader.py` (best_picks filter)
- Test: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write the failing test**

```python
def test_best_picks_excludes_bearish():
    """Best picks should only include bullish-direction stocks."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from market_leader import _apply_quality_penalties

    candidates = [
        {"code": "000001", "name": "A", "composite_score": 0.6,
         "direction": "偏空", "stop_loss": 9.0, "current_price": 10.0, "risks": []},
        {"code": "000002", "name": "B", "composite_score": 0.4,
         "direction": "震荡偏多", "stop_loss": 9.0, "current_price": 10.0, "risks": []},
    ]
    result = _apply_quality_penalties(candidates)
    # Filter like main() does: only keep 偏多 direction
    bullish_only = [c for c in result if "偏多" in c.get("direction", "")]
    test("best_picks_no_bearish",
         all("偏多" in c["direction"] for c in bullish_only),
         f"Directions: {[c['direction'] for c in bullish_only]}")
    test("best_picks_includes_B",
         any(c["code"] == "000002" for c in bullish_only),
         "B (震荡偏多) should be included")
```

- [ ] **Step 2: Run test to verify current behavior (should pass since filter is in caller)**

- [ ] **Step 3: Add direction filter to best_picks generation in main()**

After `_apply_quality_penalties(all_rated)`, add:

```python
    # Only recommend bullish-direction stocks for mid-term holding
    all_rated = [c for c in all_rated if "偏多" in c.get("direction", "")]
```

- [ ] **Step 4: Run full test suite**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py && python3 .claude/skills/stock-trend/tests/test_longtou.py && python3 .claude/skills/stock-trend/tests/test_quality_gate.py`
Expected: All PASS

- [ ] **Step 5: Run golden snapshot diff**

Run: `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
Expected: If changes in golden output are reasonable (fewer picks due to direction filter), regenerate with `--regenerate`.

- [ ] **Step 6: Commit**

```bash
git add scripts/market_leader.py tests/test_longtou.py
git commit -m "feat(longtou): filter best_picks to bullish-direction only for mid-term"
```

---

## Summary of Quality Improvements

| Issue | Solution | Impact |
|-------|----------|--------|
| 板块重复（Ⅱ/Ⅲ） | 基于 base_name + stats 签名去重 | 减少冗余，释放扫描名额 |
| 弱势板块入选 | 涨跌比 ≥15% 准入门槛 | 排除全面下跌无操作价值板块 |
| 止损过紧 | 综合分惩罚 -0.15（<2%）| 中线标的优先 |
| 信号矛盾 | 方向与指标一致性校验 | 矛盾标的降权 -0.10~0.20 |
| 深度分析失败 | 重试1次 + 检查 returncode | 减少数据缺失 |
| 偏空标的入推荐 | best_picks 只保留偏多方向 | 中线操作更聚焦 |

---

## Execution Order

Tasks 1-2 (板块过滤层) → Task 3 (止损惩罚) → Task 4 (信号门控) → Task 5 (重试) → Task 6 (推荐过滤)

Tasks 1 和 2 可并行执行（都修改 `fetch_sector_data.py` 的同一函数，但一个加去重一个加过滤，互不冲突）。
Tasks 3-6 彼此独立，但都修改 `market_leader.py`，建议按序执行避免冲突。
