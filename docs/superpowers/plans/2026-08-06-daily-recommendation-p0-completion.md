# Daily Recommendation P0 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐“今日推荐”优化方案中尚未落地的 P0 数据时效、质量调分、板块持续性门控与回测窗口测试，同时保持现有 JSON 消费接口兼容。

**Architecture:** `recommendation_quality.py` 继续作为候选数据资格的单一事实源，输出统一元数据、覆盖率、置信度和调整因子；`stock_scanner.py` 保留原始复合分并新增质量调整分；`daily_candidates.py` 统一解析推荐依据交易日、板块持续性和行动分层，并按质量调整分排序。板块持续性只消费已有快照历史，不新增实时外部依赖；缺历史时明确降级为单日脉冲观察，不把相对排名当绝对主线。

**Tech Stack:** Python 3.9+、标准库 `datetime/json/unittest`、现有自定义测试运行器、Markdown/HTML 报告

---

## 文件结构

- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_quality.py` — 统一维度元数据、严格资格和调整因子。
- Modify: `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py` — 写入真实抓取时间。
- Modify: `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py` — 写入真实抓取时间。
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py` — 保存板块资金快照并修正月初日历回退。
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` — 保留原始分，新增质量调整分。
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` — 推荐依据日、板块持续性、脉冲门控、排序和原因展示。
- Modify: `.claude/skills/stock-trend/tests/test_recommendation_quality.py` — 元数据、新鲜度与资格边界。
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py` — 原始分/调整分集成。
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py` — 接入新增测试套件。
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py` — 有效交易日、持续性和行动门控。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` — 20/60 日精确窗口断言。
- Modify: `.claude/skills/stock-trend/SKILL.md` — 更新 `/candidates` P0 完整语义。
- Modify: `docs/daily-recommendation-optimization.md` — 记录完整 P0 的实际边界。

### Task 1: 统一数据元数据和严格推荐资格

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_recommendation_quality.py`
- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`

- [x] **Step 1: 写失败测试**

新增测试覆盖：从 `meta.data_source/fetch_time` 提取 `source/fetched_at`；过期资金输出 `stale_reason=capital_stale`；任一已返回的次级维度为 `error` 时不得推荐；结果包含 `confidence/coverage_factor/freshness_factor`。

- [x] **Step 2: 运行测试确认失败**

Run: `python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py`

Expected: 新增元数据、错误维度和调整因子断言失败。

- [x] **Step 3: 最小实现**

扩展 `_dimension()`，统一输出：

```python
{
    "available": bool,
    "fresh": bool,
    "data_date": "YYYY-MM-DD",
    "fetched_at": str,
    "source": str,
    "quality": str,
    "stale_reason": str,
}
```

`assess_candidate_data()` 保持 K 线 55%、资金 25%、基本面 20%；要求 K 线新鲜、覆盖率至少 70%、至少一个次级维度可用，且已返回的次级维度不得处于 `error`。新增：

```python
coverage_factor = coverage
freshness_factor = 1.0 if kline fresh and no stale/error dimension else 0.5
confidence = round(coverage_factor * freshness_factor, 2)
```

- [x] **Step 4: 运行测试确认通过**

Run: `python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py`

Expected: 全部通过。

### Task 2: 分离原始分与质量调整分

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- [x] **Step 1: 写失败测试**

将“质量元数据不改变复合分”测试升级为：`raw_composite_score == composite_score` 保持兼容，`quality_adjusted_score == raw_composite_score * coverage_factor * freshness_factor`；过期 K 线的调整分低于原始分。

- [x] **Step 2: 运行测试确认失败**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: 缺少 `raw_composite_score/quality_adjusted_score`。

- [x] **Step 3: 最小实现**

在 `run_phase2()` 完成质量评估后输出：

```python
raw = round(composite, 1)
adjusted = round(raw * data_quality["coverage_factor"]
                 * data_quality["freshness_factor"], 1)
```

保留 `composite_score=raw`，新增 `raw_composite_score` 和 `quality_adjusted_score`，避免破坏旧消费者。

- [x] **Step 4: 运行测试确认通过**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: 全部通过。

### Task 3: 有效交易日和板块持续性门控

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

- [x] **Step 1: 写失败测试**

新增纯函数测试：周末使用最近快照交易日；盘前允许上一交易日收盘数据；3 天以上持续且均值达标为 `mainline`；只有当日强度而无历史为 `single_day_pulse`；单日脉冲不得进入今日可执行；扩池和最终排序使用 `quality_adjusted_score`。

- [x] **Step 2: 运行测试确认失败**

Run: `python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: 新的交易日、板块持续性、脉冲和调整分断言失败。

- [x] **Step 3: 最小实现**

新增：

```python
resolve_recommendation_date(now, regime_date, last_trading_date)
enrich_sector_context(ranked, history)
candidate_rank_score(item)
```

板块上下文保留 `absolute_hot_score/relative_hot_score`，计算最近 3/5/10 个快照均值、上榜天数和资金代理连续性；分类为 `mainline/emerging/single_day_pulse`. 自动扫描只让 `mainline/emerging` 进入推荐候选，`single_day_pulse` 候选携带 `sector_actionable=False` 并进入观察池。手动指定板块不伪造持续性，默认仅观察。

分类与排序同时将原因写入候选，MD/HTML 观察池展示首个未升级原因。

- [x] **Step 4: 运行测试确认通过**

Run: `python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: 全部通过。

### Task 4: 补齐回测窗口精确测试和文档

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/SKILL.md`
- Modify: `docs/daily-recommendation-optimization.md`

- [x] **Step 1: 写并运行 20/60 日精确收益测试**

构造 61 根单调 K 线，分别断言目标索引 20 和 60 的精确 close-to-close 收益，并把测试接入自定义 runner。

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py`

Expected: 两个新断言通过；生产函数无需改动。

- [x] **Step 2: 更新技能与优化文档**

记录：推荐依据最近有效交易日；候选同时输出原始分和质量调整分；缺少持续性历史时单日热点只能观察；P0 不包含交易计划、组合分散和完整生产链回测。

- [x] **Step 3: 运行专项回归**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: 零失败。

### Task 5: 仓库质量门与变更审查

**Files:**
- Verify all modified files.

- [x] **Step 1: 运行主质量门**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 两个命令 exit 0；不得通过重生成 Golden 消除差异。

- [x] **Step 2: 检查差异**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: 无空白错误，只包含本计划文件。

## 自审

- Spec coverage：覆盖 P0 数据日期/新鲜度、市场门控既有行为、绝对热点、多日持续性、质量调分、精确回测窗口；完整生产链回测明确保留为 P1。
- Placeholder scan：无 TBD/TODO 或未定义步骤。
- Type consistency：候选保留 `composite_score`，新增字段供新排序使用；板块上下文统一使用 dict，兼容现有 `(code,name,score)` 输入边界。
