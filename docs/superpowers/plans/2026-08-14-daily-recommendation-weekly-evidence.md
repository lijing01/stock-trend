# Daily Recommendation Weekly Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `/candidates` 增加最近 5 个交易日的市场、板块和候选轨迹证据，在不改变原始复合评分权重的前提下，对单日脉冲、历史走弱和证据不足的候选做可解释降级。

**Architecture:** 新增两个小型核心模块：`weekly_evidence.py` 只负责纯函数计算，`recommendation_history.py` 只负责追加式快照持久化和读取。`daily_candidates.py` 继续拥有推荐策略、分层和报告编排；周度证据只允许维持或降低行动等级，不能绕过数据质量、市场环境、板块持续性和维科夫硬门控。首版使用最近 5 个有效交易日，候选历史不足时保持现有行为并显式标记 bootstrap，避免上线第一周将全部候选误降级。

**Tech Stack:** Python 3.11+、标准库 `json/pathlib/statistics/hashlib`、现有 `unittest` 测试入口、Markdown/HTML/JSON 报告

---

## 发布边界

本计划交付：

- 最近 5 个交易日窗口及来源说明；
- 市场周度状态、板块周度状态、候选历史轨迹；
- 追加式每日推荐快照；
- 周度证据驱动的“只降不升”行动门控；
- JSON、Markdown、HTML 的周度证据展示；
- 完整单元测试、主测试入口和 golden 验证。

本计划不交付：

- 不调整动量、量价、资金、基本面、板块和维科夫的原始权重；
- 不用历史推荐收益直接给当天候选加分；
- 不实现 5/10/20/60 日业绩归因和生产链回测；
- 不引入数据库或第三方依赖；
- 不把盘中临时观察结果作为正式历史证据。

## 文件结构

### 新增文件

- `.claude/skills/stock-trend/scripts/core/weekly_evidence.py`：交易日窗口、市场/板块/候选周度证据和降级决策的纯函数。
- `.claude/skills/stock-trend/scripts/core/recommendation_history.py`：正式推荐快照的追加式保存、校验和按交易日读取。
- `.claude/skills/stock-trend/tests/test_weekly_evidence.py`：周度窗口、状态分类、未来数据隔离和降级规则测试。
- `.claude/skills/stock-trend/tests/test_recommendation_history.py`：快照追加、幂等、损坏文件隔离和正式/临时快照过滤测试。

### 修改文件

- `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`：暴露最近 N 个有效交易日的公共读取函数。
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`：加载周度上下文、附加候选轨迹、执行降级、保存正式快照并渲染输出。
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`：集成门控、两日 emerging 边界、兼容字段和输出测试。
- `.claude/skills/stock-trend/tests/test_stock_trend.py`：把两个新测试套件接入质量门。
- `.claude/skills/stock-trend/SKILL.md`：更新 `/candidates` 的 5 日证据、降级和快照语义。
- `docs/daily-recommendation-optimization.md`：登记该阶段的完成边界和后续归因工作。

---

### Task 1: 建立最近 5 个交易日窗口

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:960-1023`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 写交易日窗口失败测试**

在 `TestRecommendationPolicy` 中增加：

```python
def test_recent_trading_days_use_calendar_and_exclude_future_dates(self):
    calendar = {
        "2026-08-07", "2026-08-10", "2026-08-11",
        "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17",
    }
    with patch("fetchers.sector_data._load_authoritative_trading_dates",
               return_value=calendar):
        dates, source = dc.get_recent_trading_days(
            "2026-08-14", days=5, fallback_dates=[])
    self.assertEqual(dates, [
        "2026-08-10", "2026-08-11", "2026-08-12",
        "2026-08-13", "2026-08-14",
    ])
    self.assertEqual(source, "calendar")

def test_recent_trading_days_fall_back_to_evidence_dates(self):
    with patch("fetchers.sector_data._load_authoritative_trading_dates",
               return_value=set()):
        dates, source = dc.get_recent_trading_days(
            "2026-08-14", days=5,
            fallback_dates=["2026-08-11", "2026-08-12", "2026-08-14"])
    self.assertEqual(
        dates, ["2026-08-11", "2026-08-12", "2026-08-14"])
    self.assertEqual(source, "evidence")
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL，提示 `get_recent_trading_days` 尚不存在或未从 `daily_candidates` 导入。

- [ ] **Step 3: 实现公共交易日窗口函数**

在 `sector_data.py` 的 `_load_authoritative_trading_dates()` 之后增加：

```python
def get_recent_trading_days(as_of_date: str, days: int = 5,
                            fallback_dates=None) -> tuple[list[str], str]:
    """Return up to N verified trading dates ending at as_of_date."""
    expected = _strict_calendar_date(as_of_date)
    if not expected or days <= 0:
        return [], "invalid"

    calendar_dates = sorted(
        value for value in _load_authoritative_trading_dates()
        if value <= expected
    )
    if calendar_dates:
        return calendar_dates[-days:], "calendar"

    evidence_dates = sorted({
        value for value in (fallback_dates or [])
        if _strict_calendar_date(value) and value <= expected
    })
    return evidence_dates[-days:], "evidence" if evidence_dates else "missing"
```

在 `daily_candidates.py` 顶层导入并重新暴露：

```python
from fetchers.sector_data import get_recent_trading_days
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add .claude/skills/stock-trend/scripts/fetchers/sector_data.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: add weekly trading-day window"
```

---

### Task 2: 实现周度证据纯函数

**Files:**
- Create: `.claude/skills/stock-trend/scripts/core/weekly_evidence.py`
- Create: `.claude/skills/stock-trend/tests/test_weekly_evidence.py`

- [ ] **Step 1: 写市场、板块和候选轨迹失败测试**

创建 `test_weekly_evidence.py`，使用 `unittest`，至少包含以下用例：

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.weekly_evidence import (
    build_candidate_week_evidence,
    build_market_week_evidence,
    build_sector_week_evidence,
    decide_weekly_gate,
)


DATES = [
    "2026-08-10", "2026-08-11", "2026-08-12",
    "2026-08-13", "2026-08-14",
]


class TestWeeklyEvidence(unittest.TestCase):
    def test_market_three_weak_days_requires_downgrade(self):
        history = {
            DATES[0]: {"regime_score": 55},
            DATES[1]: {"regime_score": 58},
            DATES[2]: {"regime_score": 59},
            DATES[3]: {"regime_score": 72},
            DATES[4]: {"regime_score": 82},
        }
        result = build_market_week_evidence(history, DATES)
        self.assertEqual(result["status"], "fragile_rebound")
        self.assertEqual(result["weak_days"], 3)

    def test_sector_two_days_is_insufficient_and_not_actionable(self):
        history = {
            DATES[3]: [{"code": "BK1", "hot_score": 70,
                        "rank": 4, "net_flow": 1.0}],
            DATES[4]: [{"code": "BK1", "hot_score": 75,
                        "rank": 2, "net_flow": 1.5}],
        }
        result = build_sector_week_evidence(history, "BK1", DATES)
        self.assertEqual(result["status"], "insufficient")
        self.assertFalse(result["confirmed"])

    def test_candidate_strengthening_uses_only_dates_up_to_as_of(self):
        snapshots = {
            DATES[1]: {"candidates": [{
                "code": "600000", "quality_adjusted_score": 61,
                "action_level": "waiting_trigger",
            }]},
            DATES[3]: {"candidates": [{
                "code": "600000", "quality_adjusted_score": 67,
                "action_level": "waiting_trigger",
            }]},
            "2026-08-17": {"candidates": [{
                "code": "600000", "quality_adjusted_score": 99,
                "action_level": "actionable",
            }]},
        }
        current = {"code": "600000", "quality_adjusted_score": 72}
        result = build_candidate_week_evidence(
            snapshots, current, DATES, as_of_date="2026-08-14")
        self.assertEqual(result["status"], "strengthening")
        self.assertEqual(result["score_delta"], 11.0)

    def test_weekly_gate_only_downgrades(self):
        decision = decide_weekly_gate(
            current_level="actionable",
            market={"status": "fragile_rebound", "coverage_days": 5},
            sector={"status": "confirmed", "coverage_days": 5},
            candidate={"status": "stable", "appearance_days": 3},
        )
        self.assertEqual(decision["level"], "waiting_trigger")
        self.assertIn("market_week_fragile", decision["reasons"])

    def test_bootstrap_history_preserves_current_level(self):
        decision = decide_weekly_gate(
            current_level="actionable",
            market={"status": "bootstrap", "coverage_days": 1},
            sector={"status": "confirmed", "coverage_days": 4},
            candidate={"status": "new_signal", "appearance_days": 0},
        )
        self.assertEqual(decision["level"], "actionable")
        self.assertEqual(decision["mode"], "bootstrap")


def run_weekly_evidence_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestWeeklyEvidence)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_weekly_evidence_tests()
    raise SystemExit(1 if failed else 0)
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_weekly_evidence.py
```

Expected: FAIL，提示 `core.weekly_evidence` 不存在。

- [ ] **Step 3: 实现周度证据数据契约**

创建 `weekly_evidence.py`，实现以下公共接口：

```python
from statistics import mean, median, pstdev


LEVEL_ORDER = {"observation": 0, "waiting_trigger": 1, "actionable": 2}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_market_week_evidence(history, trading_dates):
    scores = [
        _safe_float(history[day].get("regime_score"))
        for day in trading_dates if isinstance(history.get(day), dict)
        and history[day].get("regime_score") is not None
    ]
    coverage = len(scores)
    if coverage < 3:
        status = "bootstrap"
    elif sum(score < 60 for score in scores) >= 3:
        status = "fragile_rebound"
    elif len(scores) >= 2 and scores[-1] > scores[0]:
        status = "improving"
    else:
        status = "stable"
    return {
        "status": status,
        "scores": scores,
        "coverage_days": coverage,
        "median_score": round(median(scores), 1) if scores else None,
        "weak_days": sum(score < 60 for score in scores),
    }


def build_sector_week_evidence(history, sector_code, trading_dates):
    rows = []
    for day in trading_dates:
        row = next((item for item in history.get(day, [])
                    if item.get("code") == sector_code), None)
        if row:
            rows.append((day, row))
    hot = [_safe_float(row.get("hot_score")) for _, row in rows]
    ranks = [_safe_float(row.get("rank"), 999.0) for _, row in rows]
    positive_flow_days = sum(
        _safe_float(row.get("net_flow")) > 0 for _, row in rows
    )
    coverage = len(rows)
    latest_present = bool(rows and rows[-1][0] == trading_dates[-1])
    confirmed = coverage >= 3 and latest_present
    if coverage < 3:
        status = "insufficient"
    elif not latest_present:
        status = "fading"
    elif len(hot) >= 2 and hot[-1] <= hot[0] - 10:
        status = "weakening"
    elif coverage >= 4 and positive_flow_days >= 3:
        status = "confirmed"
    else:
        status = "emerging_confirmed"
    return {
        "status": status,
        "confirmed": confirmed,
        "coverage_days": coverage,
        "appearance_rate": round(coverage / max(1, len(trading_dates)), 2),
        "weighted_hot": round(sum(
            value * weight for value, weight in zip(hot, range(1, len(hot) + 1))
        ) / max(1, sum(range(1, len(hot) + 1))), 1) if hot else None,
        "rank_delta": round(ranks[0] - ranks[-1], 1) if len(ranks) >= 2 else None,
        "positive_flow_days": positive_flow_days,
    }


def build_candidate_week_evidence(snapshots, current, trading_dates,
                                  as_of_date):
    rows = []
    for day in trading_dates:
        if day > as_of_date:
            continue
        row = next((item for item in snapshots.get(day, {}).get("candidates", [])
                    if item.get("code") == current.get("code")), None)
        if row:
            rows.append(row)
    prior_scores = [_safe_float(row.get("quality_adjusted_score")) for row in rows]
    scores = prior_scores + [_safe_float(current.get("quality_adjusted_score"))]
    if not prior_scores:
        status = "new_signal"
    elif scores[-1] <= scores[0] - 8:
        status = "weakening"
    elif scores[-1] >= scores[0] + 5:
        status = "strengthening"
    elif len(scores) >= 3:
        status = "stable"
    else:
        status = "building"
    return {
        "status": status,
        "appearance_days": len(prior_scores),
        "score_series": scores,
        "score_delta": round(scores[-1] - scores[0], 1),
        "score_volatility": round(pstdev(scores), 1) if len(scores) > 1 else 0.0,
    }


def decide_weekly_gate(current_level, market, sector, candidate):
    if market.get("coverage_days", 0) < 3:
        return {"level": current_level, "mode": "bootstrap", "reasons": []}
    target = current_level
    reasons = []
    if market.get("status") == "fragile_rebound" and target == "actionable":
        target = "waiting_trigger"
        reasons.append("market_week_fragile")
    if sector.get("status") in {"insufficient", "fading", "weakening"}:
        target = "observation"
        reasons.append("sector_week_unconfirmed")
    if candidate.get("status") == "weakening" and target == "actionable":
        target = "waiting_trigger"
        reasons.append("candidate_week_weakening")
    return {"level": target, "mode": "active", "reasons": reasons}
```

- [ ] **Step 4: 运行周度证据测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_weekly_evidence.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add .claude/skills/stock-trend/scripts/core/weekly_evidence.py .claude/skills/stock-trend/tests/test_weekly_evidence.py
git commit -m "feat: compute five-day recommendation evidence"
```

---

### Task 3: 增加追加式推荐快照

**Files:**
- Create: `.claude/skills/stock-trend/scripts/core/recommendation_history.py`
- Create: `.claude/skills/stock-trend/tests/test_recommendation_history.py`

- [ ] **Step 1: 写快照失败测试**

测试必须验证：正式快照写入 `<root>/<date>/<generated_at>.json`；相同内容幂等；同日不同内容追加新版本；读取时每个交易日选择最新正式版本；临时盘中快照和损坏 JSON 不参与证据。

核心用例：

```python
def test_save_and_load_latest_formal_snapshot(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        first = make_snapshot("2026-08-13", "20260813-160000", 61)
        second = make_snapshot("2026-08-13", "20260813-170000", 68)
        save_recommendation_snapshot(first, root=root)
        save_recommendation_snapshot(second, root=root)
        loaded = load_recommendation_history(
            ["2026-08-13"], root=root)
        self.assertEqual(
            loaded["2026-08-13"]["candidates"][0]["quality_adjusted_score"],
            68,
        )

def test_provisional_snapshot_is_not_loaded_as_formal_evidence(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        payload = make_snapshot("2026-08-14", "20260814-110000", 80)
        payload["snapshot_type"] = "provisional"
        save_recommendation_snapshot(payload, root=root)
        self.assertEqual(
            load_recommendation_history(["2026-08-14"], root=root), {})
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_recommendation_history.py
```

Expected: FAIL，提示模块不存在。

- [ ] **Step 3: 实现快照存储契约**

实现以下接口：

```python
SNAPSHOT_ROOT = CACHE_DIR / "recommendation_snapshots"
SCHEMA_VERSION = 1


def build_recommendation_snapshot(as_of_date, generated_at, policy,
                                  candidates, buckets, snapshot_type):
    action_by_code = {}
    for level, key in (("actionable", "actionable"),
                       ("waiting_trigger", "waiting_trigger"),
                       ("observation", "observation")):
        for item in buckets.get(key, []):
            action_by_code[item.get("code")] = level
    rows = []
    for item in candidates:
        rows.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "quality_adjusted_score": item.get("quality_adjusted_score"),
            "raw_composite_score": item.get("raw_composite_score"),
            "action_level": action_by_code.get(item.get("code"), "observation"),
            "sector_code": item.get("sector_code"),
            "sector_type": item.get("sector_type"),
            "wyckoff_phase": item.get("wyckoff", {}).get("phase"),
            "wyckoff_sub_phase": item.get("wyckoff", {}).get("sub_phase"),
            "confidence": item.get("wyckoff", {}).get("confidence"),
            "weekly_evidence": item.get("weekly_evidence", {}),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": "candidates-weekly-v1",
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "snapshot_type": snapshot_type,
        "policy": policy,
        "candidates": rows,
    }
```

使用以下完整持久化实现；它使用追加式文件名、内容摘要处理同时间戳冲突，并在读取时只选择每个交易日最新的正式版本：

```python
import hashlib
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[5]
CACHE_DIR = Path(os.environ.get(
    "STOCK_TREND_CACHE_DIR",
    str(PROJECT_ROOT / ".cache" / "stock-trend"),
))
SNAPSHOT_ROOT = CACHE_DIR / "recommendation_snapshots"
SCHEMA_VERSION = 1


def _encoded(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def save_recommendation_snapshot(payload, root=SNAPSHOT_ROOT):
    day = str(payload.get("as_of_date", ""))
    generated_at = str(payload.get("generated_at", ""))
    if len(day) != 10 or not generated_at:
        raise ValueError("snapshot requires as_of_date and generated_at")
    directory = Path(root) / day
    directory.mkdir(parents=True, exist_ok=True)
    content = _encoded(payload)
    digest = hashlib.sha256(content).hexdigest()[:8]
    base = directory / f"{generated_at}.json"
    if base.exists():
        if base.read_bytes() == content:
            return base
        base = directory / f"{generated_at}-{digest}.json"
        if base.exists() and base.read_bytes() == content:
            return base
    temporary = base.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(base)
    return base


def _valid_snapshot(path, expected_date):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("as_of_date") != expected_date:
        return None
    if payload.get("snapshot_type") != "formal":
        return None
    if not isinstance(payload.get("candidates"), list):
        return None
    return payload


def load_recommendation_history(trading_dates, root=SNAPSHOT_ROOT):
    history = {}
    for day in trading_dates:
        directory = Path(root) / day
        versions = []
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = _valid_snapshot(path, day)
            if payload:
                versions.append(payload)
        if versions:
            history[day] = max(
                versions, key=lambda item: str(item.get("generated_at", "")))
    return history
```

- [ ] **Step 4: 运行快照测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_recommendation_history.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add .claude/skills/stock-trend/scripts/core/recommendation_history.py .claude/skills/stock-trend/tests/test_recommendation_history.py
git commit -m "feat: persist recommendation history snapshots"
```

---

### Task 4: 将周度证据接入候选分层

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:354-480`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1076-1169`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 写两日板块和只降不升失败测试**

增加以下断言：

```python
def test_two_day_emerging_sector_is_not_actionable(self):
    ranked = [{
        "code": "BK1", "name": "测试板块", "hot_score": 70,
        "absolute_hot_score": 70, "change_pct": 1.0,
    }]
    history = {
        "2026-08-13": [{"code": "BK1", "hot_score": 60}],
        "2026-08-14": [{"code": "BK1", "hot_score": 70}],
    }
    sector = enrich_sector_context(
        ranked, history, as_of_date="2026-08-14")[0]
    self.assertEqual(sector["persistence_status"], "history_insufficient")
    self.assertFalse(sector["sector_actionable"])

def test_weekly_gate_moves_actionable_candidate_to_waiting(self):
    item = candidate("600000")
    item["weekly_evidence"] = {
        "decision": {
            "level": "waiting_trigger",
            "mode": "active",
            "reasons": ["market_week_fragile"],
        }
    }
    policy = {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
    }
    buckets = classify_candidates([item], policy)
    self.assertEqual(buckets["actionable"], [])
    self.assertEqual([row["code"] for row in buckets["waiting_trigger"]],
                     ["600000"])
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 两个新增测试 FAIL。

- [ ] **Step 3: 收紧两日 emerging 行动资格**

在 `enrich_sector_context()` 中保持 `sector_type="emerging"` 作为展示分类，但修改行动资格：

```python
history_insufficient = len(dates) < 3 or days < 3
sector_actionable = (
    sector_type in ("mainline", "emerging")
    and not history_insufficient
)
```

写入字段时使用 `"sector_actionable": sector_actionable`。

- [ ] **Step 4: 增加周度上下文装配函数**

在 `daily_candidates.py` 增加：

```python
def attach_weekly_evidence(candidates, regime_history, sector_history,
                           recommendation_history, trading_dates,
                           as_of_date):
    market = build_market_week_evidence(regime_history, trading_dates)
    enriched = []
    for source in candidates:
        item = copy.deepcopy(source)
        sector = build_sector_week_evidence(
            sector_history, item.get("sector_code", ""), trading_dates)
        candidate_week = build_candidate_week_evidence(
            recommendation_history, item, trading_dates, as_of_date)
        current_level = "actionable" if item.get("sector_actionable") else "observation"
        decision = decide_weekly_gate(
            current_level, market, sector, candidate_week)
        item["weekly_evidence"] = {
            "trading_dates": trading_dates,
            "market": market,
            "sector": sector,
            "candidate": candidate_week,
            "decision": decision,
        }
        enriched.append(item)
    return enriched
```

在 `select_candidate_pool()` 之后、`classify_candidates()` 之前调用。候选快照只加载 `trading_dates[:-1]`，当前日由当前候选补入，避免读取同日旧运行造成重复计数。

- [ ] **Step 5: 让分层遵守周度降级结果**

在现有 `eligible` 计算之后使用以下分层逻辑。它先应用原有硬门控，再读取周度决策，因此周度字段没有升级基础不合格候选的路径：

```python
def _weekly_level(item):
    return item.get("weekly_evidence", {}).get(
        "decision", {}).get("level", "actionable")


limit = policy.get("max_recommendations", 0)
actionable = []
waiting = []
if policy.get("mode") == "actionable":
    actionable = [item for item in eligible
                  if _weekly_level(item) == "actionable"][:limit]
    remaining = max(0, limit - len(actionable))
    waiting = [item for item in eligible
               if _weekly_level(item) == "waiting_trigger"][:remaining]
elif policy.get("mode") == "waiting_trigger":
    waiting = [item for item in eligible
               if _weekly_level(item) != "observation"][:limit]

promoted = {item["code"] for item in actionable + waiting}
```

构建观察池时，在现有 `reasons` 后追加：

```python
weekly = item.get("weekly_evidence", {}).get("decision", {})
if item.get("code") not in promoted:
    reasons.extend(weekly.get("reasons", []))
```

`next_day_confirmation` 保留现有生成方式，但排除 `_weekly_level(item) == "observation"` 的候选。

- [ ] **Step 6: 运行集成测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_weekly_evidence.py
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: gate daily picks with weekly evidence"
```

---

### Task 5: 接入输出和正式快照生命周期

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1172-1335`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1338-1490`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 写 JSON 和快照生命周期失败测试**

验证：

```python
def test_json_exposes_weekly_context_without_removing_legacy_fields(self):
    item = candidate("600000")
    item["weekly_evidence"] = {
        "trading_dates": ["2026-08-12", "2026-08-13", "2026-08-14"],
        "candidate": {"status": "stable"},
        "decision": {"level": "actionable", "reasons": []},
    }
    policy = {"mode": "actionable", "max_recommendations": 5,
              "max_portfolio_pct": 60, "reasons": []}
    buckets = {"actionable": [item], "waiting_trigger": [],
               "next_day_confirmation": [], "observation": []}
    output = build_json_output([item], [], 1.0, policy, buckets)
    self.assertIn("weekly_evidence", output["candidates"][0])
    self.assertEqual(output["recommendations"][0]["code"], "600000")
```

另用 `patch("scans.daily_candidates.save_recommendation_snapshot")` 验证：盘中策略保存 `snapshot_type="provisional"`，盘后或盘前基于最近收盘日的正式结果保存 `snapshot_type="formal"`，`--json` 和报告模式都恰好保存一次。

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 生命周期测试 FAIL。

- [ ] **Step 3: 扩展输出契约**

在 JSON `meta` 增加：

```python
"weekly_window": {
    "trading_dates": trading_dates,
    "source": trading_date_source,
    "coverage_days": len(trading_dates),
    "model_version": "candidates-weekly-v1",
}
```

Markdown/HTML 每只候选增加两列：

- `本周轨迹`：新信号、增强、稳定、构建中、走弱；
- `周度处理`：保持、降为等待、降为观察，并展示原因码的中文映射。

在报告顶部增加周度市场摘要：覆盖日期、市场中位分、弱势天数和证据状态。旧字段 `candidates/recommendations/waiting_trigger/observation` 保持不变。

- [ ] **Step 4: 保存一次运行快照**

候选完成周度附加和最终分层后立即构造一次 snapshot payload。`snapshot_type` 使用：

```python
snapshot_type = "provisional" if is_recommendation_session() else "formal"
```

把以下代码放在 JSON/Markdown 分支之前，确保任何输出模式都只执行一次：

```python
snapshot_error = ""
generated_at = datetime.now().strftime("%Y%m%d-%H%M%S")
snapshot_type = (
    "provisional" if is_recommendation_session() else "formal"
)
snapshot = build_recommendation_snapshot(
    as_of_date=expected_date,
    generated_at=generated_at,
    policy=policy,
    candidates=candidates,
    buckets=buckets,
    snapshot_type=snapshot_type,
)
try:
    snapshot_path = save_recommendation_snapshot(snapshot)
    performance["recommendation_snapshot"] = str(snapshot_path)
except (OSError, ValueError, TypeError) as exc:
    snapshot_error = str(exc)
    performance["snapshot_error"] = snapshot_error
    print(f"⚠️ 推荐快照保存失败: {snapshot_error}", file=sys.stderr)
```

JSON `meta.performance` 和报告性能审计会沿用现有 envelope 携带 `snapshot_error`，保存失败不得让本次候选结果失败。

- [ ] **Step 5: 运行输出与生命周期测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_recommendation_history.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: report and persist weekly pick context"
```

---

### Task 6: 接入质量门并更新文档

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py:1350-1375`
- Modify: `.claude/skills/stock-trend/SKILL.md`
- Modify: `docs/daily-recommendation-optimization.md`

- [ ] **Step 1: 将新测试接入主测试入口**

在 `run_daily_recommendation_tests()` 导入：

```python
from test_recommendation_history import run_recommendation_history_tests
from test_weekly_evidence import run_weekly_evidence_tests
```

并把两个 runner 插入 `run_recommendation_quality_tests` 之后、`run_daily_candidates_tests` 之前。

- [ ] **Step 2: 运行主质量门**

由于本计划修改了 `.claude/skills/stock-trend/scripts/` 下的 Python 文件，必须运行仓库规定的两个质量门：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 两条命令退出码均为 0；golden diff 只包含已明确接受的周度字段和报告列变化。不得通过重生成快照掩盖非预期变化。

- [ ] **Step 3: 更新技能文档**

在 `/candidates` 段落写明：

- 周度窗口为最近 5 个有效交易日；
- 市场、板块和候选轨迹只用于维持或降低行动等级；
- 少于 3 日正式历史时进入 bootstrap，保持旧行为但显示证据不足；
- 盘中临时快照不参与后续推荐；
- 原始复合分和质量调整分公式不变。

- [ ] **Step 4: 更新优化路线图**

在 `docs/daily-recommendation-optimization.md` 增加 `R1-W`：周度证据与推荐快照已完成；同时保留 5/10/20/60 日自动归因、交易计划和完整生产链回测为后续工作，避免把“有历史轨迹”表述成“收益已验证”。

- [ ] **Step 5: 做静态一致性检查**

Run:

```bash
rg -n "candidates-weekly-v1|weekly_evidence|recommendation_snapshots|market_week_fragile|sector_week_unconfirmed" .claude/skills/stock-trend docs --glob '!reports/**'
git diff --check
```

Expected: 所有字段名在实现、测试和文档中一致；`git diff --check` 无输出。

- [ ] **Step 6: 提交最终文档和质量门接入**

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define weekly recommendation evidence"
```

---

## 完成定义

以下条件全部满足才可标记完成：

- 最近 5 个交易日不包含 `as_of_date` 之后的数据；
- 交易日历不可用时明确标记 evidence/missing 来源；
- 两日 emerging 板块不能进入今日可执行；
- 周度证据不能把原本观察或等待的候选升级；
- 市场一周至少 3 个弱势日时，当天可执行候选最多降为等待触发；
- 正式快照追加保存，盘中快照不进入后续证据；
- 旧 JSON 顶层字段保持兼容；
- `test_stock_trend.py` 和 `test_golden.py --diff` 均通过；
- 报告明确显示周度状态、降级原因、数据日期和模型版本；
- 文档明确说明本阶段没有完成收益验证。

## 后续独立计划

本计划完成后再分别制定：

1. 完整交易计划：入场区间、无效位、止损、目标、R:R、仓位和有效期；
2. 推荐归因：5/10/20/60 日绝对/相对收益、MFE/MAE、止损/目标触发；
3. 生产链回测：历史热点→候选→Top 3/5→次日成交→成本与组合约束；
4. 样本达到至少 40～60 个交易日后，才评估周度证据是否参与数值评分。

本方案及实现输出仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
