# 删除今日推荐中线结构与周期结论 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 `/candidates`（今日推荐）报告中“中线结构/周期结论”及其派生字段的展示，并移除基于长短周期对齐结果的推荐分桶门控，同时保留短线维科夫买点和其他安全门槛。

**Architecture:** 在 `daily_candidates.py` 的展示与分桶层完成变更：Markdown/HTML 只保留小级别阶段、短线买点、短线置信度、评分、质量和诊断；候选资格不再读取 `wyckoff.alignment.recommendation_gate`，而是独立保留短线 `retest_pending`/`failed_breakout` 的观察逻辑。共享 `wyckoff.py` 的长期分析、`stock_scanner.py` 的通用 payload 和 `/stock-trend` 单股报告保持不变，避免影响其他工作流；`/candidates` JSON 中已有的原始 Wyckoff 字段继续保留以兼容机器消费者，但不再用于今日推荐判断或人类报告展示。

**Tech Stack:** Python 3.10+、标准库 `unittest`、现有 Markdown/HTML 字符串模板、仓库既有 golden diff 质量门禁。

---

## Scope and behavior contract

- 删除 `/candidates` Markdown 与 HTML 表格中的四个相关字段：`中线结构`、`周期结论`、`中线置信度`、`K线根数/要求`。
- 删除每日推荐分桶对 `wyckoff.alignment.recommendation_gate` 的依赖；长期逆势不会再单独把短线买点降级为观察池。
- 继续保留短线维科夫漏斗、`wyckoff` 评分、60 根 K 线最低要求、市场环境、数据质量、板块持续性、交易计划和推荐数量/仓位门控。
- `retest_pending` 与 `failed_breakout` 仍作为短线未完成状态留在观察池，并保留各自原因码。
- 不修改 `analysis/wyckoff.py` 的长期结构计算与 `build_period_alignment()`，不修改单股报告模板中的长短周期章节；这些属于其他工作流。
- 不删除 `/candidates` JSON 中已有的 `wyckoff.long_term`/`alignment` 原始字段，也不修改推荐快照 schema，避免破坏下游消费者；本次删除范围是展示和推荐判断。
- 不重新生成 golden snapshot 来掩盖输出差异；输出变化必须由测试明确验证。

## File map

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
  - 更新 Markdown/HTML 展示断言。
  - 将长短周期逆势测试改为“不会阻断推荐”的回归测试。
  - 增加短线回踩待确认/突破失败仍留在观察池的测试。
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
  - 删除中线展示 helper、表格字段和 `wyckoff_countertrend` 原因码。
  - 移除 alignment 推荐门控，保留独立短线状态门控。
  - 同步 Markdown/HTML 列数及空表 `colspan`。
- Modify: `.claude/skills/stock-trend/SKILL.md`
  - 更新 `/candidates` 用户可见契约，明确不展示、不依据长短周期对齐结论分桶。
- Do not modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`、`stock_scanner.py`、`report-template.md`、`report-template.html`
  - 这些文件服务于通用维科夫分析或 `/stock-trend` 报告，不在“今日推荐”本次范围内。

### Task 1: 先用回归测试锁定删除范围与判断边界

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2490-2788`

- [ ] **Step 1: 更新 Markdown 与 HTML 的展示断言**

在现有 `test_report_renders_all_buckets_and_full_disclaimer` 和对应 HTML 测试中，删除对中线列、周期列、中线置信度和 250 根结构深度的正向断言，改为对所有推荐表区域都不出现这些展示内容；保留短线字段断言。测试断言使用以下代码：

```python
removed_labels = (
    "中线结构", "周期结论", "中线置信度", "K线根数/要求",
    "中线吸筹，短线买点确认", "251/250",
)
for label in removed_labels:
    self.assertNotIn(label, report)

self.assertIn("小级别维科夫阶段", report)
self.assertIn("短线买点", report)
self.assertIn("短线置信度", report)
self.assertIn("阶段D：需求确认", report)
```

HTML 测试对变量 `html` 使用同一组 `removed_labels` 断言，并继续断言“今日可执行”“观察池”和免责声明存在。

- [ ] **Step 2: 将长短周期逆势测试改成不阻断推荐的回归测试**

将 `test_countertrend_wyckoff_candidate_is_observation_only` 替换为：

```python
def test_long_term_countertrend_does_not_block_short_term_recommendation(self):
    item = candidate("countertrend")
    item["wyckoff"]["alignment"] = {
        "status": "countertrend",
        "label": "中线偏空，短线买点属逆势反弹",
        "recommendation_gate": "observation",
    }
    policy = {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
    }

    buckets = classify_candidates([item], policy)

    self.assertEqual([row["code"] for row in buckets["actionable"]],
                     ["countertrend"])
    self.assertEqual(buckets["observation"], [])
    self.assertNotIn(
        "wyckoff_countertrend",
        buckets["actionable"][0].get("observation_reasons", []),
    )
```

- [ ] **Step 3: 锁定短线待确认状态仍然不能升级**

新增一个参数化式循环测试，确保移除 alignment 后不会误放行短线未完成信号：

```python
def test_short_term_pending_states_remain_observation_only(self):
    cases = (
        ("retest_pending", "wyckoff_retest_pending"),
        ("failed_breakout", "wyckoff_failed_breakout"),
    )
    policy = {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
    }

    for signal_status, reason in cases:
        with self.subTest(signal_status=signal_status):
            item = candidate(signal_status)
            item["wyckoff"]["short_term"] = {
                "sub_phase": "lps",
                "signal_status": signal_status,
            }
            buckets = classify_candidates([item], policy)

            self.assertEqual(buckets["actionable"], [])
            self.assertEqual(
                [row["code"] for row in buckets["observation"]],
                [signal_status],
            )
            self.assertIn(reason, buckets["observation"][0]["observation_reasons"])
```

- [ ] **Step 4: 运行测试确认当前实现按预期失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 新增的“长周期逆势仍可推荐”和展示 `assertNotIn` 测试失败；这是因为当前代码仍读取 `alignment.recommendation_gate`，并仍渲染四个中线/周期相关字段。

### Task 2: 删除今日推荐的中线/周期展示并重写分桶门控

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:63-92,1677-1761,1863-1889,1982-2070,2226-2277,2404-2422`

- [ ] **Step 1: 删除只服务于中线展示的 helper 和旧原因码**

从 `REASON_LABELS` 删除：

```python
"wyckoff_countertrend": "维科夫长短周期逆势，降级为观察",
```

删除以下三个不再有调用方的函数及其完整函数体：

- `_long_term_structure_text(wyckoff)`（当前约 1677–1683 行）
- `_long_term_confidence_text(wyckoff)`（当前约 1748–1755 行）
- `_kline_depth_text(wyckoff)`（当前约 1757–1761 行）

删除后用 `rg` 确认 `daily_candidates.py` 不再引用这三个函数或 `wyckoff_countertrend`。

- [ ] **Step 2: 增加独立的短线观察原因函数**

在 `classify_candidates()` 前增加以下纯函数，让短线状态不再依赖 alignment：

```python
def _short_term_observation_reason(item):
    """Return a demotion reason for an unfinished short-term signal."""
    wyckoff = item.get("wyckoff") or {}
    short_term = wyckoff.get("short_term") or {}
    signal_status = (
        short_term.get("signal_status")
        or wyckoff.get("signal_status")
        or ""
    )
    return {
        "retest_pending": "wyckoff_retest_pending",
        "failed_breakout": "wyckoff_failed_breakout",
    }.get(str(signal_status).strip().lower())
```

- [ ] **Step 3: 只保留短线状态门控，移除长短周期门控**

在 `classify_candidates()` 的 `eligible` 列表中，用以下条件替换当前读取 `alignment.recommendation_gate` 的条件：

```python
and _short_term_observation_reason(item) is None
```

在观察池原因收集中，删除当前整段读取 `alignment_status` 并追加 `wyckoff_countertrend` 的代码，改为：

```python
short_term_reason = _short_term_observation_reason(item)
if short_term_reason:
    reasons.append(short_term_reason)
```

这样长期 `countertrend`、`aligned_bearish` 或未知长期结构不再参与今日推荐分桶；短线 `retest_pending` 和 `failed_breakout` 仍会被明确降级。

- [ ] **Step 4: 缩减 Markdown 表格到 10 列**

将 `_append_candidate_table()` 的表头和行拼接改为：

```python
lines.extend([
    "| # | 名称(代码) | 板块 | 小级别维科夫阶段 | 短线买点 | 短线置信度 | 原始分 | 质量分 | 数据维度覆盖率 | 数据问题/异常及原因 |",
    "|---|---|---|---|---|---|---|---|---|---|",
])
for index, item in enumerate(items, 1):
    wyckoff = item.get("wyckoff", {})
    quality = item.get("data_quality", {})
    detail = _markdown_cell(_candidate_diagnostic_text(item))
    plan_text = _markdown_cell(_trade_plan_text(item))
    lines.append(
        f"| {index} | {item['name']}({item['code']}) | "
        f"{_sector_text(item)} | {_minor_phase_text(wyckoff)} | "
        f"{wyckoff.get('sub_phase', '-')} | "
        f"{wyckoff.get('confidence', 0):.0%} | "
        f"{item['composite_score']:.1f} | "
        f"{candidate_rank_score(item):.1f} | "
        f"{quality.get('coverage', 0):.0%} | "
        f"{detail}；{plan_text} |"
    )
```

- [ ] **Step 5: 缩减 HTML 行和五个分区表头**

在 `_html_candidate_rows()` 中把无数据行的 `colspan` 从 14 改成 10，并把每行中与中线/周期相关的四个 `<td>` 删除；行保留以下 10 个单元格顺序：

```python
f"<tr{row_class}><td>{index}</td><td><strong>{item['name']}</strong><br>"
f"<span style='color:#86868b;font-size:12px'>{item['code']}</span>"
f"{buy_level_badge}</td>"
f"<td>{_sector_text(item)}</td>"
f"<td>{_minor_phase_html(wyckoff)}</td>"
f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
f"<td><strong>{item['composite_score']:.1f}</strong></td>"
f"<td><strong>{candidate_rank_score(item):.1f}</strong></td>"
f"<td>{quality.get('coverage', 0):.0%}</td>"
f"<td>{escape(detail)}；{plan_text}</td></tr>"
```

将 `_generate_html()` 中 actionable、waiting、next-day confirmation、observation、data-rejected 五个重复表头统一替换为：

```html
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{rows}</tbody></table>
```

其中 `{rows}` 分别替换为现有的 `actionable_rows`、`waiting_rows`、`confirmation_rows`、`observation_rows` 和 `rejected_rows`。

- [ ] **Step 6: 运行定向测试确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: `TestRecommendationPolicy` 全部 PASS，且不再出现中线/周期展示或 `wyckoff_countertrend` 原因码相关失败。

### Task 3: 同步 `/candidates` 用户契约

**Files:**

- Modify: `.claude/skills/stock-trend/SKILL.md:286`

- [ ] **Step 1: 更新数据质量与展示说明**

将 `/candidates` 第 3 条从：

```text
报告将该指标标为“数据维度覆盖率”，另列中期结构所用的 K 线根数，二者不得混用。
```

改为：

```text
报告将该指标标为“数据维度覆盖率”；候选表只展示小级别维科夫阶段、短线买点和短线置信度，不展示中线结构、周期结论、中线置信度或中期结构 K 线根数。今日推荐分桶不读取长短周期对齐结论，仍由短线买点、市场环境、数据质量、板块持续性和完整交易计划共同决定。
```

- [ ] **Step 2: 保留其他 `/candidates` 契约不变**

确认以下内容没有被同步删除或改写：`wyckoff` 短线买点漏斗、70% 数据覆盖率、市场环境三档、板块持续性门控、`candidate-trade-plan/v1`、20–120 交易日交易计划周期、JSON 兼容字段和推荐快照追踪。

### Task 4: 完成质量门禁与残留检查

**Files:**

- No additional implementation files.

- [ ] **Step 1: 运行候选模块与相关 scanner 测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: 两个测试文件退出码为 0；scanner 的通用 `long_term`/`alignment` 测试保持通过。

- [ ] **Step 2: 运行仓库要求的两道质量门禁**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 主测试入口与 golden diff 均通过；golden 文件不被改写。

- [ ] **Step 3: 做静态残留与补丁格式检查**

Run:

```bash
rg -n '中线结构|周期结论|中线置信度|K线根数/要求|_long_term_structure_text|_long_term_confidence_text|_kline_depth_text|wyckoff_countertrend|alignment.*recommendation_gate' .claude/skills/stock-trend/scripts/scans/daily_candidates.py
git diff --check
```

Expected: 第一条命令无输出；第二条命令退出码为 0。全仓库不要求这些词全部消失，因为通用 `/stock-trend` 报告和 `wyckoff.py` 仍合法保留长短周期分析。

- [ ] **Step 4: 检查最终变更范围**

确认最终 diff 只包含本计划列出的 `daily_candidates.py`、`test_daily_candidates.py`、`SKILL.md` 以及本计划文档；没有报告生成物、`.cache` 文件或 golden 快照变更。

## Acceptance criteria

1. `/candidates` 的 Markdown 和 HTML 五个结果区都不再包含“中线结构”“周期结论”“中线置信度”“K线根数/要求”。
2. 长期 `countertrend`/其他 alignment 结果不再阻断满足现有短线、数据、板块、市场和交易计划门槛的推荐。
3. 短线 `retest_pending` 与 `failed_breakout` 仍只进入观察池并保留明确原因。
4. 短线维科夫阶段、买点、置信度、评分、数据质量、交易计划、市场/板块门控和快照 JSON 兼容性保持不变。
5. `test_daily_candidates.py`、`test_stock_scanner.py`、`test_stock_trend.py` 和 `test_golden.py --diff` 全部通过，且不更新 golden 快照。

## Self-review

- Spec coverage: 展示删除、判断删除、短线逻辑保留、兼容边界、文档同步、测试和质量门禁分别映射到 Task 1–4。
- 占位内容检查：计划中的代码、命令、预期结果和文件路径均已给出，没有未定义的后续步骤。
- Type consistency: `_short_term_observation_reason(item)` 返回 `str | None`，被 `eligible` 资格条件和观察原因收集共用；HTML 行列数从 14 同步降为 10，空行 `colspan` 也同步为 10。
