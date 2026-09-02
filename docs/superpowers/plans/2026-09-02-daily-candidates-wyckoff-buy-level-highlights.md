# 今日推荐维科夫买点分级高亮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每日候选 HTML 的“今日可执行”区域，用不同背景色明确标出已确认的一级 Spring/Test、二级 SOS 后 LPS、三级 JAC/BU 趋势买点，同时避免把候选或观察项误标成可执行买点。

**Architecture:** 保持扫描、评分、分桶和交易计划门控不变；维科夫引擎在短周期输出中增加“confirmed LPS 之后再次 confirmed SOS/JAC”的审计字段，展示层据此分级。分类优先读取生产 payload 的 `wyckoff.short_term.sub_phase` 与 `signal_status`；`_html_candidate_rows()` 通过显式参数决定是否着色，因此只有“今日可执行”表格会应用整行背景色。

**Tech Stack:** Python 3.10+、标准库 `unittest`、现有字符串模板 HTML。

---

## Scope and behavior contract

- 一级：已确认且新鲜度已由上游门控保证的 `spring`；展示为“试错仓”。普通 Phase B `secondary_test` 不等同于 Spring 后 Test，不着色。
- 二级：已确认的 `lps`；展示为“核心仓”。现有 BU 候选不会被误标为 LPS。
- 三级：仅 `post_lps_reconfirmation=true` 的已确认 `jac`；展示为“趋势仓”。首次 confirmed `jac` 仅代表 SOS 突破，按 Plan 不追高，因此不着色。
- `candidate`、`retest_pending`、`failed_breakout` 和缺少确认状态的条目均不分级。
- 仅 `buckets["actionable"]` 启用整行背景色；等待触发、次日确认、观察池、数据失效区不着色。
- 本次不修改第三级识别阈值、候选分桶、排序或仓位比例；这些属于策略定义扩展，不以展示需求反向改变交易门控。

### Task 1: 用失败测试锁定分级与作用域

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [x] **Step 1: 添加生产字段形状下的三级分类测试**

构造 `wyckoff.short_term.sub_phase` 分别为 `spring`、`lps`、带 `post_lps_reconfirmation=true` 的 `jac`，状态为 `confirmed`，断言返回一级、二级、三级；再断言 `secondary_test/confirmed`、首次 `jac/confirmed`、`backup/candidate` 与缺状态条目不分级。

- [x] **Step 2: 添加“仅今日可执行整行着色”的 HTML 测试**

直接调用 `_html_candidate_rows([item], highlight_buy_levels=True)`，断言 `<tr class='wyckoff-buy-level-2'>`；默认调用断言没有该 class。调用 `_generate_html()` 时，将同一买点分别放入 actionable 与 observation，断言只出现一次分级行 class，并存在三级颜色图例。

- [x] **Step 3: 运行 focused tests 确认 RED**

Run:

```bash
python3 -m unittest \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 新测试因分类函数、参数和行 class 尚不存在而失败。

### Task 2: 最小实现分级行与图例

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [x] **Step 1: 添加纯函数 `_wyckoff_buy_level()`**

优先读取 `short_term.sub_phase`，兼容顶层英文/中文展示字段；只接受 `confirmed`。返回包含 `number`、`label`、`role`、`css_class` 的字典或 `None`。

- [x] **Step 2: 为 HTML 行增加可选分级标记**

给 `_html_candidate_rows(items, highlight_buy_levels=False)` 增加参数。启用时将分级结果写入 `<tr class='...'>`，并在名称下展示“一级·Spring/Test·试错仓”等可读标签；未启用或未确认时保持现有 HTML。

- [x] **Step 3: 只对今日可执行启用，并加入图例/CSS**

`_generate_html()` 仅在构造 `actionable_rows` 时传入 `True`。增加三个低饱和背景色和图例，确保文字对比度；用 `td` 选择器覆盖现有表格底色，避免只给单个阶段标签着色。

- [x] **Step 4: 运行 focused tests 确认 GREEN**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部通过。

### Task 3: 仓库质量门禁与差异审查

**Files:**

- Inspect: `git diff -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: 运行静态语法检查**

```bash
python3 -m py_compile .claude/skills/stock-trend/scripts/scans/daily_candidates.py
```

- [x] **Step 2: 运行仓库要求的两道质量门禁**

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

- [x] **Step 3: 审查工作树**

确认没有修改 `reports/`、golden snapshots 或与本需求无关的文件；对新增样式和测试运行 `git diff --check`。
