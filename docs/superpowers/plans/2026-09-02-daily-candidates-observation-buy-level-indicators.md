# 观察池维科夫潜在买点分级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在“今日推荐”HTML 的观察池中，用低饱和背景色展示严格确认的潜在一级、二级、三级维科夫买点，同时保持观察项不可执行、不可分配仓位且不进入推荐列表。

**Architecture:** 复用现有 `_wyckoff_buy_level()` 作为唯一分级来源，不增加第二套识别规则。将 `_html_candidate_rows()` 的布尔高亮参数改为显式展示上下文 `none/actionable/observation`：可执行区沿用现有颜色和仓位角色，观察池使用独立 CSS class、较浅背景和“观察｜不可执行”标签；`classify_candidates()`、市场门控、交易计划门控、JSON 输出和快照内容保持不变。

**Tech Stack:** Python 3.10+、标准库 `unittest`、现有 Python 字符串 HTML 模板、CSS。

---

## Scope and behavior contract

- 一级仍只接受 `short_term.signal_status == "confirmed"` 的 `spring`。
- 二级仍只接受 `short_term.signal_status == "confirmed"` 的 `lps`。
- 三级仍只接受 `short_term.signal_status == "confirmed"`、`sub_phase == "jac"` 且 `post_lps_reconfirmation is True` 的完整再确认结构。
- 普通 `secondary_test/st`、首次 `jac`、`backup`、`candidate`、`retest_pending`、`failed_breakout` 和缺少确认状态的条目不分级。
- “今日可执行”继续显示“一级/二级/三级 + 试错仓/核心仓/趋势仓”。
- “观察池”只显示“潜在一级/二级/三级 + 观察｜不可执行”，不得出现仓位建议或“买入”措辞。
- 等待触发、次日确认和数据失效区不着色，避免将未完成门控的标的误解为推荐。
- 页面必须明确说明：颜色只表示维科夫结构成熟度，是否可交易仍由市场环境、数据质量、板块持续性和完整交易计划决定。
- 本次不修改 `classify_candidates()`、`build_recommendation_policy()`、`build_json_output()`、推荐快照 schema、候选排序、推荐上限或组合仓位。

## File map

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
  - 将 HTML 行渲染改为显式上下文。
  - 为观察池增加独立标签、浅色背景、图例和不可执行说明。
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
  - 锁定可执行与观察样式的差异。
  - 锁定严格三级条件及非推荐文案。
  - 防止等待、确认和数据失效区被误着色。

## Acceptance criteria

1. 同一个 confirmed LPS 放入 `actionable` 时输出 `wyckoff-buy-level-2` 和“核心仓”；放入 `observation` 时输出 `wyckoff-observation-buy-level-2` 和“观察｜不可执行”。
2. 观察池中的首次 confirmed JAC 不着色；只有 `post_lps_reconfirmation=True` 的 JAC 才显示潜在三级。
3. 观察池说明同时包含“仅表示维科夫结构成熟度”“不是买入建议”“只有今日可执行具备推荐资格”。
4. `waiting_trigger`、`next_day_confirmation`、`data_rejected` 中即使出现 confirmed LPS，也不产生任何买点等级 class。
5. 现有候选分桶、推荐模式、推荐数量、仓位、JSON 字段和 Markdown 内容完全不变。
6. 仓库要求的两道质量门禁全部通过，不更新 golden snapshot 来掩盖失败。

### Task 1: 用失败测试锁定观察池展示语义

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2045`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: 将现有可执行行测试切换为显式上下文**

将 `test_html_highlights_confirmed_lps_as_level_two_row` 中的渲染调用改为：

```python
html = dc._html_candidate_rows(
    [item], buy_level_display="actionable")
self.assertIn("<tr class='wyckoff-buy-level-2'>", html)
self.assertIn("二级 · SOS 后 LPS · 核心仓", html)

plain_html = dc._html_candidate_rows([item])
self.assertNotIn("wyckoff-buy-level-2", plain_html)
```

- [x] **Step 2: 添加观察池潜在二级测试**

在同一测试类中添加：

```python
def test_html_marks_confirmed_lps_as_non_executable_observation_level(self):
    item = candidate("observation-lps")
    item["wyckoff"]["short_term"] = {
        "sub_phase": "lps",
        "signal_status": "confirmed",
    }

    html = dc._html_candidate_rows(
        [item], buy_level_display="observation")

    self.assertIn(
        "<tr class='wyckoff-observation-buy-level-2'>", html)
    self.assertIn("潜在二级 · SOS 后 LPS · 观察｜不可执行", html)
    self.assertNotIn("核心仓", html)
    self.assertNotIn("<tr class='wyckoff-buy-level-2'>", html)
```

- [x] **Step 3: 添加观察池严格三级测试**

```python
def test_observation_level_three_requires_post_lps_reconfirmation(self):
    first_jac = candidate("first-jac")
    first_jac["wyckoff"]["short_term"] = {
        "sub_phase": "jac",
        "signal_status": "confirmed",
        "post_lps_reconfirmation": False,
    }
    reconfirmed_jac = copy.deepcopy(first_jac)
    reconfirmed_jac["code"] = "reconfirmed-jac"
    reconfirmed_jac["wyckoff"]["short_term"][
        "post_lps_reconfirmation"] = True

    html = dc._html_candidate_rows(
        [first_jac, reconfirmed_jac],
        buy_level_display="observation",
    )

    self.assertEqual(
        html.count("wyckoff-observation-buy-level-3"), 1)
    self.assertIn(
        "潜在三级 · JAC/BU 后再确认 · 观察｜不可执行", html)
```

- [x] **Step 4: 运行测试确认 RED**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL，错误包含 `_html_candidate_rows() got an unexpected keyword argument 'buy_level_display'`。

- [x] **Step 5: 提交测试合同**

```bash
git add .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "test(report): specify observation buy levels"
```

### Task 2: 实现显式 HTML 展示上下文

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1948`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: 将行渲染参数替换为三态上下文**

在 `_html_candidate_rows()` 前增加允许值，并将函数开头替换为：

```python
_BUY_LEVEL_DISPLAY_CONTEXTS = {"none", "actionable", "observation"}


def _html_candidate_rows(items, buy_level_display="none"):
    if buy_level_display not in _BUY_LEVEL_DISPLAY_CONTEXTS:
        raise ValueError(
            f"unsupported buy level display: {buy_level_display}")
    if not items:
        return '<tr><td colspan="14">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        buy_level = (
            _wyckoff_buy_level(wyckoff)
            if buy_level_display != "none"
            else None
        )
        if not buy_level:
            row_class = ""
            buy_level_badge = ""
        elif buy_level_display == "observation":
            row_class = (
                " class='wyckoff-observation-buy-level-"
                f"{buy_level['number']}'"
            )
            buy_level_badge = (
                "<br><span class='wyckoff-buy-level-badge observation'>"
                f"潜在{buy_level['name']} · {buy_level['label']} · "
                "观察｜不可执行</span>"
            )
        else:
            row_class = f" class='{buy_level['css_class']}'"
            buy_level_badge = (
                "<br><span class='wyckoff-buy-level-badge'>"
                f"{buy_level['name']} · {buy_level['label']} · "
                f"{buy_level['role']}</span>"
            )
```

保留现有 `<tr{row_class}>` 后续单元格拼接逻辑不变。该实现继续调用 `_wyckoff_buy_level()`，不从中文展示文本推断等级。

- [x] **Step 2: 更新五个分区调用点**

在 `_generate_html()` 中使用：

```python
actionable_rows = _html_candidate_rows(
    buckets["actionable"], buy_level_display="actionable")
waiting_rows = _html_candidate_rows(buckets["waiting_trigger"])
confirmation_rows = _html_candidate_rows(
    buckets.get("next_day_confirmation", []))
observation_rows = _html_candidate_rows(
    buckets["observation"], buy_level_display="observation")
rejected_rows = _html_candidate_rows(
    buckets.get("data_rejected", []))
```

- [x] **Step 3: 更新 BU 候选测试的参数名**

将旧调用：

```python
html = dc._html_candidate_rows([item], highlight_buy_levels=True)
```

替换为：

```python
html = dc._html_candidate_rows(
    [item], buy_level_display="actionable")
```

- [x] **Step 4: 运行 focused tests 确认 GREEN**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部通过。

- [x] **Step 5: 提交渲染逻辑**

```bash
git add \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat(report): mark observation buy levels"
```

### Task 3: 增加观察池浅色样式和防误读说明

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2075`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2080`

- [x] **Step 1: 添加低饱和观察池 CSS**

紧跟现有可执行买点样式增加：

```css
.wyckoff-observation-buy-level-1>td{{background:#fffdf4}}
.wyckoff-observation-buy-level-2>td{{background:#f3fcf6}}
.wyckoff-observation-buy-level-3>td{{background:#f4f8ff}}
.wyckoff-buy-level-badge.observation{{
  background:transparent;
  border:1px dashed #9ca3af;
  color:#6b7280;
}}
.observation-buy-level-note{{
  margin:8px 0;
  padding:8px 10px;
  border:1px solid #e5e7eb;
  border-radius:8px;
  background:#f9fafb;
  color:#4b5563;
  font-size:12px;
}}
.observation-buy-level-legend .level-1{{background:#fffdf4}}
.observation-buy-level-legend .level-2{{background:#f3fcf6}}
.observation-buy-level-legend .level-3{{background:#f4f8ff}}
```

- [x] **Step 2: 在观察池标题下加入说明和独立图例**

将观察池标题与表格之间扩展为：

```html
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<div class="observation-buy-level-note" role="note">
<strong>观察池分级仅表示维科夫结构成熟度，不是买入建议。</strong>
市场环境、数据质量、板块持续性和完整交易计划仍是硬门槛；
只有“今日可执行”区域具备推荐资格。
</div>
<div class="buy-level-legend observation-buy-level-legend"
     aria-label="观察池潜在维科夫买点分级图例">
<span class="level-1">潜在一级 · Spring/Test</span>
<span class="level-2">潜在二级 · SOS 后 LPS</span>
<span class="level-3">潜在三级 · JAC/BU 后再确认</span>
</div>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>中线结构</th><th>周期结论</th><th>短线置信度</th><th>中线置信度</th><th>K线根数/要求</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{observation_rows}</tbody></table>
```

- [x] **Step 3: 将整页作用域测试改为区分两类 class**

把现有 `test_generated_html_highlights_buy_levels_only_in_actionable_table` 重命名为 `test_generated_html_distinguishes_actionable_and_observation_buy_levels`，并将断言替换为：

```python
self.assertEqual(
    html.count("<tr class='wyckoff-buy-level-2'>"), 1)
self.assertEqual(
    html.count("<tr class='wyckoff-observation-buy-level-2'>"), 1)
self.assertIn("二级 · SOS 后 LPS · 核心仓", html)
self.assertIn(
    "潜在二级 · SOS 后 LPS · 观察｜不可执行", html)
self.assertIn(
    "观察池分级仅表示维科夫结构成熟度，不是买入建议", html)
self.assertIn("只有“今日可执行”区域具备推荐资格", html)
self.assertIn("observation-buy-level-legend", html)
```

- [x] **Step 4: 添加非观察分区不着色回归测试**

```python
def test_non_observation_watch_buckets_do_not_receive_buy_level_classes(self):
    lps = candidate("lps")
    lps["wyckoff"]["short_term"] = {
        "sub_phase": "lps",
        "signal_status": "confirmed",
    }
    buckets = {
        "actionable": [],
        "waiting_trigger": [copy.deepcopy(lps)],
        "next_day_confirmation": [copy.deepcopy(lps)],
        "observation": [],
        "data_rejected": [copy.deepcopy(lps)],
    }

    html = dc._generate_html(
        [lps],
        [("BK1", "测试板块", 80)],
        1.0,
        "20260902-160000",
        {
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "reasons": ["regime_weak"],
        },
        buckets,
    )

    self.assertNotIn("<tr class='wyckoff-buy-level-2'>", html)
    self.assertNotIn(
        "<tr class='wyckoff-observation-buy-level-2'>", html)
```

- [x] **Step 5: 运行 focused tests**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部通过，并且测试输出不包含网络请求。

- [x] **Step 6: 提交样式和说明**

```bash
git add \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat(report): clarify observation buy levels"
```

### Task 4: 完整验证与交付

**Files:**

- Inspect: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Inspect: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Inspect: `git diff`

- [x] **Step 1: 运行语法检查**

Run:

```bash
python3 -m py_compile \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py
```

Expected: exit code 0，无输出。

- [x] **Step 2: 运行每日候选专项测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部通过。

- [x] **Step 3: 运行仓库规定的两道质量门禁**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 两个命令均 exit code 0；不得通过重生成 golden snapshot 消除非预期差异。

- [x] **Step 4: 检查展示改动未越过推荐边界**

Run:

```bash
git diff -- \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
git diff --check
```

Expected:

- diff 中没有修改 `classify_candidates()`、`build_recommendation_policy()`、`build_json_output()` 或快照逻辑。
- diff 中没有修改推荐上限、仓位比例、R:R 门槛或交易计划完整性判断。
- `git diff --check` exit code 0。

- [x] **Step 5: 检查工作树和提交记录**

Run:

```bash
git status --short
git log -3 --oneline
```

Expected: 只有计划内文件发生变更；提交信息分别说明测试合同、渲染逻辑和防误读样式。

## Rollback boundary

如果用户反馈观察池颜色仍容易被理解为推荐，只回滚 `buy_level_display="observation"` 调用、观察池 CSS、说明和相关测试；保留现有可执行区的 `_wyckoff_buy_level()`、三级严格识别字段及可执行区颜色。该回滚不触碰扫描、分桶、快照或交易策略。

## Validation note

本计划只改变报告呈现，不宣称观察池买点具有可交易收益。若未来希望观察池分级影响仓位或推荐资格，必须另立计划，先通过 `/wyckoff-backtest` 比较 5/10/20 日胜率、平均收益、最大不利波动、回撤和换手，再单独评审市场环境与板块持续性门控，不能在本展示改动中顺带放开。
