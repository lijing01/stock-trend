# 今日推荐维科夫证据链修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/candidates` 只有在可审计的 `TR → Spring/Test → SOS → BU/LPS → 再确认` 证据链满足对应等级条件时，才允许进入“今日可执行”或“等待触发”，并阻断普通 ST、PRE_MARKUP、无父 SOS 的 LPS 和普通 JAC 越级推荐。

**Architecture:** 在 `analysis/wyckoff.py` 保留现有事件检测器作为事实来源，新增 `wyckoff-execution-chain/v1` 规范化投影，统一表达结构资格、事件父子关系、确认状态、失败原因和严格买点等级。采用“双门”数据流：`stock_scanner.py` 的发现门保留严格已确认和仍在形成的结构候选，使其能够进入观察池；`daily_candidates.py` 的推荐门只允许规范化投影中的严格确认买点晋级，并继续叠加市场、板块、数据质量和数量门控。旧 JSON 缺少新投影时采用保守兼容：允许展示和回放，但不得新晋级为“今日可执行”。

**Tech Stack:** Python 3 标准库、现有 Wyckoff 分析器、`unittest` 风格脚本测试、现有 Markdown/HTML 报告器与 JSON 快照。

---

## 1. Requirements Summary

### 1.1 必须修复

1. `Spring` 跌破后收回箱体只能表示 Spring 已收回；只有后续 Test 同时满足更高低点、相对缩量、波幅收敛和下跌减速，才获得严格一级资格。
2. 普通 `secondary_test` 不等于 Spring Test；没有父 Spring 的 ST 不得进入严格买点等级。
3. 二级 LPS 必须存在同一 `range_id` 下更早的已确认 Spring Test、已确认 SOS、随后 BU/LPS 候选、守住原阻力和再次转强确认；删除“仅靠近支撑且缩量即 LPS”的推荐捷径。没有 Spring Test 的再吸筹结构仍可观察，但首版不得静默当作完整二级链。
4. 三级 JAC 必须发生在同一箱体已确认 LPS 之后，继续使用 `post_lps_reconfirmation=True`，并增加事件引用可审计性。
5. `ST`、`PRE_MARKUP`、`BU candidate`、普通 JAC、`retest_pending`、`failed_breakout` 不得进入“今日可执行”或“等待触发”。
   这些信号仍可通过发现门保留在观察池，并显示缺失的确认步骤。
6. TR 除几何清晰外，还要输出吸筹来源证据：前置下跌和停止行为；证据不足的箱体允许分析和观察，不得据此确认 Spring/LPS 推荐。
7. LPS 回调判断必须覆盖整个回调片段，而不只比较单根 K 线；至少输出量能趋势、波幅趋势、回调速度、是否守住突破位和是否形成 Higher Low。
8. 当前市场环境、板块持续性、数据质量、资金背离和最低质量分门控保持不变，严格买点等级不能绕过这些门槛。
9. `/candidates` 仍只负责候选发现和分层，不新增入场价、止损价、目标价或仓位比例。
10. 当前用户对候选 HTML 表格横向滚动和诊断列宽度的未提交修改必须保留，不得回退。

### 1.2 非目标

- 不调整市场环境的 `<60 / 60–79 / ≥80` 档位。
- 不改变板块持续性、数据覆盖率 70%、资金增强预算和推荐数量上限。
- 不修改长周期对齐逻辑，也不恢复长周期对 `/candidates` 分桶的门控。
- 不在样本不足时放大 `+1/+3/+2` 买点奖励。
- 不通过重新生成 golden 快照掩盖非预期差异。
- 不发展盘中 T+0 或高频策略。

## 2. Canonical Data Contract

在 `analysis/wyckoff.py` 的结果中增加：

```python
"execution_setup": {
    "schema": "wyckoff-execution-chain/v1",
    "setup": "spring_test",  # spring_test | sos_lps | post_lps_reconfirmation | none
    "status": "confirmed",   # candidate | confirmed | expired | failed | none
    "buy_point_level": 1,     # 1 | 2 | 3 | None
    "range_id": "minor_10",
    "event_index": 31,
    "detected_index": 34,
    "event_date": "2026-09-01",
    "detected_date": "2026-09-03",
    "age_bars": 0,
    "max_age_bars": 8,
    "parent_events": {
        "spring": 30,
        "sos": None,
        "lps": None,
    },
    "checks": {
        "range_valid": True,
        "prior_decline": True,
        "stopping_action": True,
        "support_reclaimed": True,
        "test_higher_low": True,
        "test_lower_volume": True,
        "test_spread_contracting": True,
        "test_speed_contracting": True,
    },
    "reasons": [],
}
```

规则：

- `buy_point_level` 只能由该投影产生；报告、排序、回测不得自行重新推断等级。
- `status != confirmed` 时 `buy_point_level=None`。
- 一级、二级、三级分别使用 8、10、8 根 K 线有效期；超过有效期时投影必须转为 `status=expired,buy_point_level=None`，生产推荐门还需再次校验 `age_bars <= max_age_bars`。
- 旧 payload 无 `execution_setup` 时，`classify_buy_point_level()` 可为历史回放保留旧识别结果，但 `stock_scanner.wyckoff_gate_pass(analysis, require_execution_setup=True)` 必须拒绝其生产推荐资格。
- `event_index` 是形态发生日，`detected_index` 是首次可确认日；回测以 `detected_index` 为信号日，禁止使用未来 K 线。

## 3. File Map

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` — TR 来源证据、Spring Test、SOS/BU/LPS 事件链、`execution_setup` 投影及严格等级唯一来源。
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` — 生产维科夫漏斗改为要求严格 `execution_setup`，保留结构评分与推荐资格分离。
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` — 分桶增加严格买点资格检查，报告展示证据链完成度和未确认原因；保留当前 HTML 可读性改动。
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py` — 以规范化严格等级分桶，补齐 5/10/20 日等级收益与 MAE/MFE 证据。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py` — 锁定 TR、Spring/Test、SOS/LPS 和三级再确认状态机。
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py` — 锁定宽结构信号不能通过生产推荐漏斗。
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py` — 锁定严格等级、市场/板块/数据门控组合以及报告诊断。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` — 锁定三个窗口、等级风险统计和证据阈值。
- Modify: `.claude/specs/stock-trend-skill.md` — 将买点等级合同升级到 `wyckoff-execution-chain/v1`。
- Modify: `.claude/skills/stock-trend/SKILL.md` — 同步 `/candidates` 与 `/wyckoff-backtest` 用户可见规则。
- Modify: `docs/daily-recommendation-optimization.md` — 记录严格证据链修复状态与仍未覆盖的完整生产链收益验证。

## 4. Acceptance Criteria

1. 无有效 TR、无前置下跌或无停止行为时，`execution_setup.buy_point_level is None`。
2. Spring 收回箱体但尚无 Test 时，输出 `setup=spring_test,status=candidate`，不能通过生产推荐漏斗。
3. Test 必须发生在父 Spring 之后；满足更高低点、量低、波幅收敛、速度收敛后，首次输出一级 `confirmed`，确认日不得早于 Test 完成日。
4. 独立普通 ST 即使 `signal_status=confirmed` 也不能获得一级资格或进入可执行/等待触发。
5. 没有同箱体已确认 Spring Test 和已确认 SOS 的近支撑缩量信号不能输出二级 LPS。
6. SOS 后回踩只有在守住阻力、回调过程供给收缩并再次突破回踩局部高点后，才输出二级 `confirmed`。
7. 回踩放量、跌破结构缓冲或重返箱体深处时输出 `failed` 或 `retest_pending`，不能获得任何等级。
8. 三级必须引用同 `range_id` 的父 LPS；普通 JAC、跨箱体 LPS 或事件顺序错误均不分级。
9. `stock_scanner.wyckoff_execution_gate_pass()` 只接受未过期的严格一级、二级、三级；`ST/PRE_MARKUP/BU candidate` 均返回 False。
10. `stock_scanner.wyckoff_discovery_gate_pass()` 保留新鲜的 `candidate/confirmed/retest_pending/failed` 事件，以及普通 ST/PRE_MARKUP 供观察；拒绝过期、派发和下跌结构。失败事件只能进入观察池并显示失效原因。
11. `daily_candidates.classify_candidates()` 即使收到高质量分、强市场和可行动板块，也不能晋级未严格分级信号。
12. 原有市场、板块、数据质量、资金背离、最低分和推荐上限测试继续通过。
13. JSON、Markdown、HTML 均显示 `证据链`、`确认状态` 和首个未满足条件；不得出现仓位、止损、目标或下单价格。
14. 回测按 canonical `detected_index` 对应的全局 K 线索引计算 5/10/20 日收益；确认发生在两个采样点之间时不得推迟到下一个采样日。
15. 每级输出 count、win_rate、avg、avg_mae、avg_mfe 和 `evidence.status`。
16. 任一级成熟信号少于 100 时保持 `evidence_insufficient`，奖励只保留既有 `+1/+3/+2`，不自动放大。
17. 一级、二级、三级的过期 setup 全部不能通过推荐门，也不能继续获得奖励。
18. 当前 `candidate-table-wrap`、1180px 最小表宽和诊断列样式测试继续通过。
19. 两个仓库质量门均为 exit 0，且 `test_golden.py --diff` 没有非预期差异。

### 4.1 首版确定性阈值

首版先固定阈值以保证实现和回测可复现；后续只能在每级证据达到 100 个成熟信号后缩小、归零或调整：

| 证据 | 窗口/阈值 | 截止边界 |
|---|---|---|
| 前置下跌 | TR `support_idx` 前最多 20 根，至少 10 根；首尾跌幅至少 `max(5%, 2×ATR_at_support)` | 只读 `support_idx` 及以前 |
| 停止行为 | selling climax 或 VSA `stopping_volume/absorption`，事件索引不早于 `support_idx-3` | 只读当前 as-of bar 及以前 |
| Spring 下破 | 低点低于支撑至少 `0.3×ATR`，最大穿透沿用现有 `max(2×ATR, 0.8×range_height)` | Spring 发生 bar |
| Spring 收回 | Spring 当根至后 3 根内首次收盘重返支撑 | 收回 bar 即 reclaim detected date |
| Spring Test 窗口 | reclaim 后 1–8 根；必须先至少出现 1 根反弹/横移，再寻找回落局部低点 | 局部低点后首根转强 bar 才能确认 |
| Test Higher Low | Test 低点 `> spring_low + 0.1×ATR_at_test` | Test bar |
| Test 量/波幅 | Test volume `< spring_volume`；Test spread `< 0.8×spring_spread` | Test bar |
| Test 下跌速度 | reaction high 到 Test 的平均负向 close 变化 `<= 0.7×` Spring 前 3 根到 Spring 的平均负向变化 | Test bar 及以前 |
| BU/LPS 窗口 | confirmed SOS 后 1–5 根，回调切片至少 2 根 | candidate bar 及以前 |
| LPS 守位 | close 不低于 `resistance-0.30×breakout_ATR`，low 不低于 `resistance-0.50×breakout_ATR` | 整个回调切片 |
| LPS 供给收缩 | volume slope `<=0`、spread slope `<=0`、下跌速度 `<=0.35 ATR/bar` | 整个回调切片 |
| LPS 转强 | 后 3 根内首次 close 突破 BU high，或连续两根 close `>= resistance` | 确认 bar |
| 深返箱体失败 | close `< resistance-buffer`，其中 `buffer=min(max(ATR,1%×resistance),25%×range_height)`；若同时 volume `>1.2×20日均量`，理由追加 `supply_expanding` | 当前 as-of bar |
| setup 有效期 | 一级 8 根、二级 10 根、三级 8 根 | 从 detected index 计算 |

所有斜率使用本文 `_linear_slope()`；所有“速度”先按 ATR 归一化。任何需要后续 K 线才能确认的事件，`event_index` 保留形态发生日，`detected_index` 必须设为首次已经看见全部条件的日期。

## 5. Implementation Tasks

### Task 0: 固化用户未提交改动基线

**Files:**
- Preserve: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Preserve: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 保存只读基线 patch 和状态**

```bash
git status --short
git diff --binary -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py > /private/tmp/daily-candidates-user-baseline.patch
shasum -a 256 /private/tmp/daily-candidates-user-baseline.patch
```

Expected: patch 包含 `candidate-table-wrap`、`min-width:1180px`、`candidate-diagnostic` 以及对应测试；记录哈希到实施日志，不提交该基线 patch。

- [ ] **Step 2: 约束后续暂存方式**

凡提交包含上述两个文件的任务，一律使用 `git add -p`，随后运行：

```bash
git diff --cached --check
git diff --cached -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: staged diff 只含本计划新增的逻辑/测试 hunks，不含用户原有 HTML/CSS 可读性 hunks。若 hunk 重叠，停止提交但继续在工作树验证；不得整文件暂存。

### Task 1: 先用失败测试锁定严格准入边界

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 在 stock scanner 测试中增加生产严格模式用例**

```python
def test_execution_gate_requires_fresh_confirmed_setup(self):
    base = {
        "phase": {"primary": "accumulation", "primary_sub_phase": "pre_markup", "confidence": 0.8},
        "signal": {"status": "confirmed", "age_bars": 0},
    }
    self.assertFalse(wyckoff_execution_gate_pass(base))

    strict = {
        **base,
        "execution_setup": {
            "schema": "wyckoff-execution-chain/v1",
            "setup": "sos_lps",
            "status": "confirmed",
            "buy_point_level": 2,
            "age_bars": 0,
            "max_age_bars": 10,
        },
    }
    self.assertTrue(wyckoff_execution_gate_pass(strict))

    strict["execution_setup"]["age_bars"] = 11
    self.assertFalse(wyckoff_execution_gate_pass(strict))

def test_discovery_gate_keeps_forming_setup_for_observation(self):
    forming = {
        "phase": {"primary": "accumulation", "confidence": 0.7},
        "execution_setup": {
            "schema": "wyckoff-execution-chain/v1",
            "setup": "sos_lps",
            "status": "candidate",
            "buy_point_level": None,
            "age_bars": 0,
            "max_age_bars": 10,
        },
    }
    self.assertTrue(wyckoff_discovery_gate_pass(forming))
```

- [ ] **Step 2: 在 daily candidates 测试中锁定未分级信号不可晋级**

```python
def test_ungraded_wyckoff_signal_cannot_be_promoted(self):
    item = candidate("pre", adjusted_score=95.0)
    item["wyckoff"]["short_term"].update({
        "sub_phase": "pre_markup",
        "signal_status": "confirmed",
    })
    buckets = classify_candidates([item], {
        "mode": "actionable", "max_recommendations": 5, "reasons": [],
    })
    self.assertEqual(buckets["actionable"], [])
    self.assertIn(
        "wyckoff_execution_chain_unconfirmed",
        buckets["observation"][0]["observation_reasons"],
    )
```

- [ ] **Step 3: 运行测试并确认以缺少新参数/新门控失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL；失败必须来自 discovery/execution 双门尚未实现或未严格分级候选仍被晋级，不得是 fixture/导入错误。

- [ ] **Step 4: 提交仅测试变更**

```bash
git add .claude/skills/stock-trend/tests/test_stock_scanner.py
git add -p .claude/skills/stock-trend/tests/test_daily_candidates.py
git diff --cached --check
git commit -m "test: lock strict wyckoff recommendation gate"
```

### Task 2: 增加规范化 execution_setup 投影

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 写纯函数投影测试**

```python
def test_build_execution_setup_requires_ordered_parent_events(self):
    events = [
        {"type": "spring", "status": "confirmed", "event_index": 10,
         "detected_index": 11, "range_id": "minor_1", "age_bars": 15},
        {"type": "spring_test", "status": "confirmed", "event_index": 13,
         "detected_index": 14, "range_id": "minor_1", "age_bars": 12,
         "parent_event": "spring", "parent_event_index": 10},
        {"type": "sos", "status": "confirmed", "event_index": 20,
         "detected_index": 21, "range_id": "minor_1", "age_bars": 3},
        {"type": "lps", "status": "confirmed", "event_index": 24,
         "detected_index": 26, "range_id": "minor_1", "age_bars": 0,
         "parent_event": "sos", "parent_event_index": 20},
    ]
    setup = build_execution_setup(
        events,
        "minor_1",
        range_evidence={
            "range_valid": True,
            "prior_decline": True,
            "stopping_action": True,
        },
        tr_state={"state": "breakout_confirmed"},
    )
    self.assertEqual(setup["setup"], "sos_lps")
    self.assertEqual(setup["status"], "confirmed")
    self.assertEqual(setup["buy_point_level"], 2)
    self.assertEqual(setup["parent_events"]["spring"], 10)
    self.assertEqual(setup["parent_events"]["sos"], 20)
```

- [ ] **Step 2: 实现固定 schema 构造器**

```python
EXECUTION_SETUP_SCHEMA = "wyckoff-execution-chain/v1"

def _empty_execution_setup(*reasons):
    return {
        "schema": EXECUTION_SETUP_SCHEMA,
        "setup": "none",
        "status": "none",
        "buy_point_level": None,
        "range_id": "",
        "event_index": None,
        "detected_index": None,
        "event_date": "",
        "detected_date": "",
        "age_bars": 0,
        "max_age_bars": 0,
        "parent_events": {"spring": None, "sos": None, "lps": None},
        "checks": {},
        "reasons": list(reasons),
    }
```

`build_execution_setup(events, range_id, range_evidence, tr_state)` 必须按事件发生顺序和同一 `range_id` 选择 Spring Test、LPS 或 LPS 后 SOS；不得仅根据当前 `sub_phase` 猜测等级。`analyze_kline_dict()` 必须先构造 `event_history` 和 `tr_state`，再调用 builder。

构造时按等级写入有效期：

```python
EXECUTION_SETUP_MAX_AGE = {
    "spring_test": 8,
    "sos_lps": 10,
    "post_lps_reconfirmation": 8,
}
```

若当前 `age_bars` 超过对应值，builder 直接输出 `status="expired"`、`buy_point_level=None` 和原因 `execution_setup_expired`。`tr_state.state == "failed_breakout"` 时，无论历史事件是否 confirmed，均输出 `status="failed"`、无等级和原因 `wyckoff_failed_breakout`。`tr_state.state == "retest"` 且尚无已确认 LPS 时输出 `status="retest_pending"`、无等级；若同箱体 LPS 已在当前或更早 bar 完成确认，则以 LPS 自身确认/失效条件为准，不被宽泛的 `retest` 标签覆盖。

- [ ] **Step 3: 让 classify_buy_point_level 优先读取 canonical 投影**

```python
def classify_buy_point_level(wyckoff, allow_legacy=True):
    setup = (wyckoff or {}).get("execution_setup") or {}
    if setup.get("schema") == EXECUTION_SETUP_SCHEMA:
        if (
            setup.get("status") != "confirmed"
            or setup.get("age_bars", 0) > setup.get("max_age_bars", -1)
        ):
            return None
        level_number = setup.get("buy_point_level")
        return next(
            (dict(value) for value in BUY_POINT_LEVELS.values()
             if value["number"] == level_number),
            None,
        )
    if not allow_legacy:
        return None
    # 保留现有旧 payload 回放分支。
```

- [ ] **Step 4: 将唯一 execution_setup 写入 analyze_kline_dict() 返回值**

在 `event_history`、`range_evidence` 和 `tr_state` 完成后构造一次，只写入顶层 `result["execution_setup"]`。`short_term` 不复制 setup status、level、reasons 或 parent events；兼容的 `phase/sub_phase/signal_status` 继续保留，但它们不得成为严格等级事实源。scanner 只完整复制 canonical dict；报告和回测只读取该 dict。

增加一致性测试：若顶层 canonical level=2，而 legacy `short_term.sub_phase` 被故意设为 `spring`，生产 gate、排序和报告仍必须使用 level=2；从 canonical 单向物化的候选顶层 `buy_point_level` 必须与 canonical 一致。

- [ ] **Step 5: 运行目标测试**

在运行前增加一级、二级、三级各一组 `age_bars == max_age_bars` 仍有效及 `age_bars == max_age_bars + 1` 已过期测试；再增加“历史 confirmed LPS + 当前深返箱体”端到端测试，断言 canonical 转为 failed 且执行门拒绝。

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: PASS，且原有事件日期、JAC 再确认和多周期展示测试不回归。

- [ ] **Step 6: 提交**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: add canonical wyckoff execution setup"
```

### Task 3: 给 TR 增加吸筹来源证据

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 写三组失败测试**

覆盖：几何箱体但无前置下跌、前置下跌但无停止行为、前置下跌且存在 SC/AR/ST。断言：

```python
self.assertTrue(result["range"]["is_clear_range"])
self.assertFalse(result["range_evidence"]["accumulation_eligible"])
self.assertIn("prior_decline_missing", result["execution_setup"]["reasons"])
```

有效用例断言 `prior_decline=True`、`stopping_action=True`、`accumulation_eligible=True`。

- [ ] **Step 2: 实现独立证据函数，不修改几何 TR 检测职责**

```python
def build_range_evidence(ohlcv, trading_range, swings, vsa_signals):
    if not trading_range:
        return {
            "range_valid": False,
            "prior_decline": False,
            "stopping_action": False,
            "accumulation_eligible": False,
            "reasons": ["range_missing"],
        }
    closes = ohlcv["close"]
    start = int(trading_range["support_idx"])
    pre_closes = closes[max(0, start - 20):start + 1]
    atr_values = compute_atr(
        ohlcv["high"], ohlcv["low"], closes,
    )
    start_atr = atr_values[start] or 0.0
    decline_floor = max(start_atr * 2.0, pre_closes[0] * 0.05)
    prior_decline = (
        len(pre_closes) >= 10
        and pre_closes[0] - pre_closes[-1] >= decline_floor
    )
    selling_climax = any(
        swing.get("is_climax")
        and swing.get("climax_type") == "selling"
        and start - 3 <= swing["index"] <= len(closes) - 1
        for swing in swings
    )
    stopping_volume = any(
        signal.get("type") in {"stopping_volume", "absorption"}
        and signal.get("bar_index", -1) >= start - 3
        for signal in vsa_signals
    )
    stopping_action = selling_climax or stopping_volume
    reasons = []
    if not prior_decline:
        reasons.append("prior_decline_missing")
    if not stopping_action:
        reasons.append("stopping_action_missing")
    return {
        "range_valid": bool(trading_range.get("is_clear_range")),
        "prior_decline": prior_decline,
        "stopping_action": stopping_action,
        "accumulation_eligible": bool(
            trading_range.get("is_clear_range")
            and prior_decline
            and stopping_action
        ),
        "reasons": reasons,
    }
```

数值口径使用已有 MA/ATR/swing 工具，常量集中定义并加入注释；不得把判断塞进 `detect_trading_range()`，以免几何识别和交易语义耦合。

- [ ] **Step 3: 将 accumulation_eligible 接入 execution_setup，而非删除箱体**

几何箱体仍输出供观察和其他工作流使用；只有严格推荐投影因 `range_evidence` 不足而返回 `buy_point_level=None`。

- [ ] **Step 4: 运行测试并提交**

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: PASS。

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: require accumulation evidence for buy setups"
```

### Task 4: 实现 Spring → Test 一级状态链

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 扩展事件测试 fixture**

在现有 `_event_fixture()` 旁增加确定性数据，覆盖：Spring 收回但无 Test、合格 Test、Test 创新低、Test 放量、Test 波幅扩大和 Test 尚未完成。
再增加价格绝对跌幅相同但 ATR 分别扩大/收缩的两组样本，断言速度条件按 ATR 归一化后产生不同结果，防止退回原始价格 delta 比较。

- [ ] **Step 2: 锁定候选与确认日期**

```python
events = detect_wyckoff_events(ohlcv, atr, trading_range)
test_event = next(event for event in events if event["type"] == "spring_test")
self.assertEqual(test_event["parent_event"], "spring")
self.assertGreater(test_event["detected_index"], test_event["parent_event_index"])
self.assertEqual(test_event["status"], "confirmed")
self.assertTrue(test_event["checks"]["higher_low"])
self.assertTrue(test_event["checks"]["lower_volume"])
```

- [ ] **Step 3: 实现 Test 检测器**

```python
SPRING_TEST_MAX_BARS = 8
SPRING_TEST_HIGHER_LOW_ATR = 0.10
SPRING_TEST_MAX_SPREAD_RATIO = 0.80
SPRING_TEST_MAX_SPEED_RATIO = 0.70

def _average_negative_close_delta_atr(closes, atr_values, start, stop):
    declines = [
        (closes[index - 1] - closes[index]) / atr_values[index]
        for index in range(start + 1, stop + 1)
        if closes[index] < closes[index - 1]
        and atr_values[index]
        and atr_values[index] > 0
    ]
    return sum(declines) / len(declines) if declines else 0.0

def detect_spring_test(ohlcv, atr_values, trading_range, spring_event, end_index):
    """Return the first observable Test after a reclaimed Spring."""
    spring_index = spring_event["event_index"]
    reclaim_index = spring_event["detected_index"]
    spring_low = ohlcv["low"][spring_index]
    spring_volume = ohlcv["volume"][spring_index]
    spring_spread = ohlcv["high"][spring_index] - spring_low
    stop = min(end_index, reclaim_index + SPRING_TEST_MAX_BARS)
    spring_speed = _average_negative_close_delta_atr(
        ohlcv["close"], atr_values,
        max(0, spring_index - 3), spring_index,
    )
    candidate = None
    for confirmation_index in range(reclaim_index + 2, stop + 1):
        test_index = confirmation_index - 1
        reaction_index = max(
            range(reclaim_index, test_index),
            key=lambda index: ohlcv["close"][index],
        )
        if (
            reaction_index >= test_index
            or ohlcv["close"][test_index] >= ohlcv["close"][reaction_index]
            or ohlcv["close"][confirmation_index] <= ohlcv["close"][test_index]
        ):
            continue
        spread = ohlcv["high"][test_index] - ohlcv["low"][test_index]
        test_speed = _average_negative_close_delta_atr(
            ohlcv["close"], atr_values, reaction_index, test_index,
        )
        checks = {
            "higher_low": ohlcv["low"][test_index] > (
                spring_low + (atr_values[test_index] or 0.0)
                * SPRING_TEST_HIGHER_LOW_ATR
            ),
            "lower_volume": ohlcv["volume"][test_index] < spring_volume,
            "spread_contracting": (
                spread < spring_spread * SPRING_TEST_MAX_SPREAD_RATIO
            ),
            "speed_contracting": (
                spring_speed > 0
                and test_speed <= spring_speed * SPRING_TEST_MAX_SPEED_RATIO
            ),
            "support_held": (
                ohlcv["close"][test_index] >= trading_range["support"]
            ),
        }
        candidate = _event_record(
            "spring_test", test_index, confirmation_index,
            ohlcv["date"], "candidate",
            trading_range.get("level", "single"), trading_range, 0.62,
        )
        candidate.update({
            "parent_event": "spring",
            "parent_event_index": spring_index,
            "reaction_index": reaction_index,
            "checks": checks,
        })
        if all(checks.values()):
            candidate["status"] = "confirmed"
            candidate["confidence"] = 0.78
            return candidate
    return candidate
```

Spring 本身继续记录 `candidate/confirmed reclaim`，但 `execution_setup` 只有读取到 `spring_test.status == confirmed` 才输出一级。

- [ ] **Step 4: 将普通 ST 与 Spring Test 分离**

`SUB_ST` 继续表示箱体 Phase B 二次测试，可用于结构分析；从生产推荐准入和一级标签中排除。禁止把名称为 `secondary_test` 的旧信号静默升级为 `spring_test`。

- [ ] **Step 5: 运行测试并提交**

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: PASS；Spring reclaim 的既有测试需更新为“Spring 事件确认，但执行等级仍为空”，不能删除事件历史断言。

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: require spring test for level one setup"
```

### Task 5: 收紧 SOS → BU/LPS 二级链并增加过程供给指标

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 写缺父事件和多根回调失败测试**

```python
def test_near_support_low_volume_without_sos_is_not_lps(self):
    result = analyze_kline_dict(self._near_support_without_sos())
    self.assertNotEqual(result["execution_setup"]["buy_point_level"], 2)

def test_sos_without_prior_spring_test_is_not_level_two(self):
    result = analyze_kline_dict(self._sos_without_spring_test())
    self.assertNotEqual(result["execution_setup"]["buy_point_level"], 2)
    self.assertIn(
        "spring_test_missing",
        result["execution_setup"]["reasons"],
    )

def test_lps_requires_contracting_pullback_sequence(self):
    result = analyze_kline_dict(self._sos_then_expanding_pullback())
    self.assertIn(
        "pullback_supply_not_contracting",
        result["execution_setup"]["reasons"],
    )
```

- [ ] **Step 2: 删除两个近支撑缩量直接返回 LPS 的捷径**

将 `classify_accumulation()` 中没有父 SOS 的两个近支撑 LPS 返回分支改为 `return (SUB_ST, 0.6, latest_idx)`；该结果只表示箱体测试，不得仅修改标签后仍赋予严格等级。

- [ ] **Step 3: 将 `_bu_candidate_evidence()` 从单 bar 扩展为 pullback slice**

```python
LPS_MAX_DECLINE_SPEED_ATR = 0.35

def _linear_slope(values):
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2.0
    y_mean = sum(values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    if denominator == 0:
        return 0.0
    return sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(values)
    ) / denominator

def _pullback_supply_evidence(ohlcv, trading_range, sos_event, candidate_index):
    start = int(sos_event["detected_index"]) + 1
    stop = candidate_index + 1
    closes = ohlcv["close"][start:stop]
    lows = ohlcv["low"][start:stop]
    volumes = ohlcv["volume"][start:stop]
    spreads = [
        high - low
        for high, low in zip(
            ohlcv["high"][start:stop], ohlcv["low"][start:stop],
        )
    ]
    if len(closes) < 2:
        return {
            "bars": len(closes),
            "volume_slope": 0.0,
            "spread_slope": 0.0,
            "decline_speed_ratio": None,
            "higher_low": False,
            "support_held": False,
            "low_held": False,
            "supply_contracting": False,
        }
    breakout_atr = float(sos_event["breakout_atr"])
    volume_slope = _linear_slope(volumes)
    spread_slope = _linear_slope(spreads)
    decline_speed_ratio = abs(closes[-1] - closes[0]) / (
        max(1, len(closes) - 1) * breakout_atr
    )
    higher_low = min(lows) > trading_range["support"]
    support_held = min(closes) >= (
        trading_range["resistance"] - breakout_atr * LPS_SUPPORT_CLOSE_ATR
    )
    low_held = min(lows) >= (
        trading_range["resistance"] - breakout_atr * LPS_SUPPORT_LOW_ATR
    )
    supply_contracting = (
        volume_slope <= 0
        and spread_slope <= 0
        and decline_speed_ratio <= LPS_MAX_DECLINE_SPEED_ATR
        and higher_low
        and support_held
        and low_held
    )
    return {
        "bars": len(closes),
        "volume_slope": round(volume_slope, 6),
        "spread_slope": round(spread_slope, 6),
        "decline_speed_ratio": round(decline_speed_ratio, 4),
        "higher_low": higher_low,
        "support_held": support_held,
        "low_held": low_held,
        "supply_contracting": supply_contracting,
    }
```

至少使用 SOS 确认后的完整回调切片；不得用当前 K 线与均值的一次比较代替过程趋势。切片不足两根时保留 `candidate`，不确认。

- [ ] **Step 4: 保留并强化现有转强确认**

继续使用“突破 BU 高点”或“连续两收盘站上阻力”，但写入 `confirmation`、`parent_event_index`、`candidate_event_index` 和 `checks`。回调放量或深返箱体时输出 `failed`，而不是产生新的 LPS。

- [ ] **Step 5: 运行测试并提交**

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: PASS，现有 `retest_pending`、`failed_breakout` 和 LPS 后再确认测试不回归。

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: enforce ordered sos lps evidence chain"
```

### Task 6: 将 discovery/execution 双门接入 scanner 和今日推荐分桶

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 增加发现门与执行门，禁止 scanner 过早丢弃观察候选**

```python
def wyckoff_discovery_gate_pass(analysis):
    if not analysis:
        return False
    setup = analysis.get("execution_setup") or {}
    if setup.get("schema") == "wyckoff-execution-chain/v1":
        if (
            setup.get("setup") in {
                "spring_test", "sos_lps", "post_lps_reconfirmation",
            }
            and setup.get("status") in {
                "candidate", "confirmed", "retest_pending", "failed",
            }
            and setup.get("age_bars", 0) <= setup.get("max_age_bars", -1)
        ):
            return True
        if setup.get("status") == "expired":
            return False
    short = analysis.get("short_term") or {}
    phase = short.get("phase") or analysis.get("phase", {}).get("primary")
    sub_phase = (
        short.get("sub_phase")
        or analysis.get("phase", {}).get("primary_sub_phase")
    )
    status = short.get("signal_status") or analysis.get("signal", {}).get("status")
    return (
        phase in {PHASE_ACCUMULATION, PHASE_MARKUP}
        and sub_phase in {
            SUB_SPRING, SUB_ST, SUB_PRE_MARKUP, SUB_BU, SUB_LPS, SUB_JAC,
        }
        and status in {
            "candidate", "confirmed", "retest_pending", "failed_breakout",
        }
    )

def wyckoff_execution_gate_pass(analysis):
    setup = (analysis or {}).get("execution_setup") or {}
    return (
        setup.get("schema") == "wyckoff-execution-chain/v1"
        and setup.get("status") == "confirmed"
        and setup.get("buy_point_level") in {1, 2, 3}
        and setup.get("age_bars", 0) <= setup.get("max_age_bars", -1)
    )
```

`run_phase2(candidates, enable_wyckoff=True)` 使用 `wyckoff_discovery_gate_pass()` 决定是否保留候选，不能调用执行门后直接 `continue`。严格晋级只在 `daily_candidates.classify_candidates()` 中调用 `wyckoff_execution_gate_pass()`；这样 Spring/Test 或 BU/LPS 形成态能够进入观察池并解释缺少的确认步骤。

`stock_scanner.py` 当前在 legacy 与优化 Phase 2 两处分别组装精简 `item["wyckoff"]`。两处都必须原样复制：

```python
"execution_setup": copy.deepcopy(wk.get("execution_setup") or {}),
"range_evidence": copy.deepcopy(wk.get("range_evidence") or {}),
```

对应位置以实现时的符号为准：`_run_phase2_legacy()` 与 `run_phase2()`；不得只修其中一条路径。

- [ ] **Step 2: 增加候选级唯一资格谓词**

```python
def _strict_wyckoff_execution_eligible(item):
    return wyckoff_execution_gate_pass(item.get("wyckoff"))
```

在 `classify_candidates()` 的晋级 `eligible` 列表中加入该条件；发现门保留下来的非严格候选进入 observation，原因使用稳定码 `wyckoff_execution_chain_unconfirmed`。不要改变市场、板块、质量和资金条件的先后语义。

- [ ] **Step 3: 让买点奖励只读取 canonical 等级**

生产候选调用 `classify_buy_point_level(wyckoff, allow_legacy=False)`；旧快照渲染可继续 `allow_legacy=True`，但不能重写快照或改变其历史资格。

- [ ] **Step 4: 增加组合门控矩阵测试**

至少覆盖：强市场+高质量+未分级、弱市场+二级、板块未验证+二级、数据不合格+二级、资金背离+无板块资金证据、`retest_pending`、`failed_breakout`、过期一/二/三级、严格一/二/三级正常晋级。

增加不 mock 候选 payload 的链路测试：固定 K 线经 `analyze_kline_dict()` 产生 forming/confirmed setup，经 `run_phase2()` 的 payload 裁剪后传入 `classify_candidates()`；断言 forming 保留在观察池、confirmed 在其他门控满足时晋级、两者都保留同一 schema 和事件索引。

同时更新 `test_daily_candidates.py` 的 `candidate()` 与 `_set_buy_level()` fixture，使默认可执行候选携带合法的 `execution_setup`；专门测试旧 payload 或未分级信号时必须显式删除该字段，防止 fixture 默认值掩盖兼容边界。

- [ ] **Step 5: 运行测试并提交**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: PASS，包括当前 HTML 横向滚动和诊断列测试。

```bash
git add .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git add -p .claude/skills/stock-trend/scripts/scans/daily_candidates.py
git add -p .claude/skills/stock-trend/tests/test_daily_candidates.py
git diff --cached --check
git commit -m "fix: gate daily recommendations on strict setups"
```

### Task 7: 在报告中展示证据链与失败原因

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 写 Markdown/HTML 语义测试**

```python
self.assertIn("证据链：SOS已确认 → BU缩量守位 → LPS再次转强", markdown)
self.assertIn("未确认：等待 Spring 后缩量 Test", markdown)
self.assertNotIn("止损价", markdown)
self.assertNotIn("建议仓位", html)
```

- [ ] **Step 2: 增加统一文本函数**

```python
def _execution_setup_text(item):
    setup = item.get("wyckoff", {}).get("execution_setup", {})
    if setup.get("status") == "confirmed":
        return EXECUTION_SETUP_LABELS.get(setup.get("setup"), "严格买点已确认")
    reason = next(iter(setup.get("reasons") or []), "证据链未确认")
    return f"未确认：{EXECUTION_REASON_LABELS.get(reason, reason)}"
```

将文本加入现有“数据问题/异常及原因”诊断，不增加交易计划列，避免进一步挤压当前表格宽度。

- [ ] **Step 3: JSON 输出保留完整 execution_setup**

确认 `candidates`、三层 bucket 和正式快照中的候选均带相同投影；不复制第二套简化字段，不修改 write-once 快照冲突语义。

- [ ] **Step 4: 运行测试并提交**

Run: `python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: PASS；五张候选表、观察池非推荐语义和免责声明继续存在。

```bash
git add -p .claude/skills/stock-trend/scripts/scans/daily_candidates.py
git add -p .claude/skills/stock-trend/tests/test_daily_candidates.py
git diff --cached --check
git commit -m "feat: explain wyckoff evidence chain in candidates"
```

### Task 8: 补齐严格等级回测和证据阈值

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`

- [ ] **Step 1: 写三个窗口的精确等级统计测试**

构造一级、二级、三级和未分级信号，各自包含 `returns={5,10,20}` 与 `excursions={5,10,20}`，逐字段断言：

```python
self.assertEqual(result["by_buy_level"]["10"]["level_2"]["count"], 1)
self.assertEqual(result["risk_by_buy_level"]["20"]["level_2"]["avg_mae"], -0.04)
self.assertEqual(result["risk_by_buy_level"]["20"]["level_2"]["avg_mfe"], 0.12)
```

- [ ] **Step 2: 锁定 100 个成熟信号门槛**

分别构造 99 与 100 个同级成熟信号：99 必须 `evidence_insufficient`；100 只能变为 `ready`，不得自动修改生产奖励。

- [ ] **Step 3: 回测只按 detected_index 入场**

增加 Spring 发生日早于 Test 确认日、且确认日位于两个五日采样点之间的样本，断言前向收益起点为 Test 的 `detected_index`，而不是 Spring 发生日或下一个采样日；对 LPS 使用再次转强确认日。

- [ ] **Step 4: 改造信号提取、全局索引映射和去重**

让 `_classify_signal()` 返回 canonical 信息：

```python
def _canonical_signal(analysis):
    setup = (analysis or {}).get("execution_setup") or {}
    if (
        setup.get("schema") != "wyckoff-execution-chain/v1"
        or setup.get("status") != "confirmed"
        or setup.get("buy_point_level") not in {1, 2, 3}
    ):
        return None
    return {
        "setup": setup["setup"],
        "buy_point_level": setup["buy_point_level"],
        "range_id": setup["range_id"],
        "event_index": setup["event_index"],
        "detected_index": setup["detected_index"],
        "event_date": setup["event_date"],
        "detected_date": setup["detected_date"],
        "age_bars": setup["age_bars"],
    }

def _signal_identity(code, signal):
    return (
        code,
        signal["range_id"],
        signal["setup"],
        signal["event_index"],
        signal["detected_index"],
    )
```

在每只股票完整 K 线中建立 `date_to_global_index`。采样日分析返回 canonical signal 后执行以下验证，禁止直接把较晚样本识别出的箱体回填到过去：

```python
detected_global_index = date_to_global_index[signal["detected_date"]]
confirmation_prefix = {
    **kline_payload,
    "data": full_rows[:detected_global_index + 1],
}
confirmation_analysis = analyze_kline_dict(confirmation_prefix)
confirmed_on_date = _canonical_signal(confirmation_analysis)
if confirmed_on_date is None:
    continue
if _signal_identity(code, confirmed_on_date) != _signal_identity(code, signal):
    continue
```

只有用截至 `detected_date` 的前缀重跑后，同一 canonical identity 当日已经 confirmed，才从 `detected_global_index` 计算 forward return、MAE、MFE。这样确认日之后、采样日之前的 K 线不能参与箱体选择或事件确认。同一 `_signal_identity()` 只记录一次；现有 `min_gap` 只用于不同事件之间的 episode 去重，不能把同一事件在多个采样点重复计数，也不能替代事件身份。

测试必须包含一组“确认日之后的 K 线改变当前箱体”的样本：较晚采样分析能回看出旧事件，但截至 claimed `detected_date` 重跑无法得到同一 `range_id/setup/event_index`，该信号必须被丢弃且不能进入收益统计。

- [ ] **Step 5: 运行测试并提交**

Run: `python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py`

Expected: PASS，三个窗口均有收益、MAE、MFE、样本数和证据状态。

```bash
git add .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "fix: backtest strict setups from confirmation date"
```

### Task 9: 同步规范、技能说明与最终质量门

**Files:**
- Modify: `.claude/specs/stock-trend-skill.md`
- Modify: `.claude/skills/stock-trend/SKILL.md`
- Modify: `docs/daily-recommendation-optimization.md`

- [ ] **Step 1: 更新规范合同**

写明 `execution_setup` 是严格等级唯一来源；普通 ST/PRE_MARKUP 只观察；一级必须 Spring Test，二级必须同箱体 SOS→BU/LPS→再转强，三级必须 LPS 后 SOS/JAC 再确认。

- [ ] **Step 2: 更新用户可见路由说明**

`/candidates` 仍输出今日可执行、等待触发、观察池；报告新增证据链与未确认原因，但仍不提供入场、止损、目标、仓位或有效期。

- [ ] **Step 3: 更新优化状态与证据边界**

记录“严格事件链准入已修复”，同时保留“完整热点→候选→Top-K 生产链收益尚未验证；每级少于 100 个成熟信号时证据不足”。

- [ ] **Step 4: 运行全部目标测试**

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: 全部 exit 0。

- [ ] **Step 5: 运行仓库强制质量门**

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 两者 exit 0；golden diff 为空或仅包含逐项确认的预期文案变化。不得为了通过测试直接重生成快照。

- [ ] **Step 6: 检查工作区与差异完整性**

```bash
git diff --check
git status --short
git diff -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
rg -n 'candidate-table-wrap|1180px|candidate-diagnostic|test_candidate_html_keeps_diagnostic_column_readable' .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: `git diff --check` exit 0；四个基线标记全部存在，最初的 HTML 横向滚动和诊断列宽度修改仍保留。重新计算 `/private/tmp/daily-candidates-user-baseline.patch` 中原始新增行的集合，逐行确认未被删除；不得要求整文件 diff 哈希保持不变，因为本计划会在同文件增加新逻辑。

- [ ] **Step 7: 提交文档与最终修复**

```bash
git add .claude/specs/stock-trend-skill.md .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define strict wyckoff evidence contract"
```

## 6. Risks and Mitigations

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 严格门控后今日推荐数量显著下降 | 短期经常出现“今日无推荐” | 接受空推荐，不降低市场、数据或证据门槛；观察池保留候选发现能力 |
| Spring Test 条件过严导致一级样本稀缺 | 无法达到回测证据门槛 | 候选与确认分开记录；参数只通过回测缩小或归零，不凭主观放宽 |
| TR 前置下跌算法误伤再吸筹结构 | 趋势中的二次吸筹被排除 | `range_evidence` 区分首次吸筹与再吸筹；本计划先阻断无证据晋级，不删除结构分析结果 |
| 旧快照没有 execution_setup | 历史报告或回测兼容性下降 | 旧 payload 可展示/回放；生产新推荐必须 canonical，禁止旧数据静默晋级 |
| 多根回调判断引入未来数据 | 回测收益虚高 | `event_index/detected_index` 分离，所有确认条件只读取截至 detected bar 的数据 |
| 事件检测与 legacy phase 分类互相覆盖 | 同一标的出现冲突状态 | `execution_setup` 只从 event history 和 range evidence 构造；phase 仅用于描述和结构评分 |
| 修改 daily_candidates.py 时覆盖用户 HTML 改动 | 丢失当前工作 | 实施前后保存并比较定向 diff；报告改动只追加诊断文本，不重写表格 CSS/容器 |

## 7. Verification Matrix

| 层级 | 证明内容 | 命令 |
|---|---|---|
| Unit | TR 来源、Spring Test、LPS 父子链、JAC 再确认 | `python3 .claude/skills/stock-trend/tests/test_wyckoff.py` |
| Integration | 严格事件链进入 scanner，宽信号被拒绝 | `python3 .claude/skills/stock-trend/tests/test_stock_scanner.py` |
| Policy | 市场/板块/质量/资金/严格买点组合门控 | `python3 .claude/skills/stock-trend/tests/test_daily_candidates.py` |
| Backtest | 5/10/20 日收益、MAE/MFE、100 样本证据门 | `python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py` |
| Repository | 主测试与 golden 合同 | `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`；`python3 .claude/skills/stock-trend/tests/test_golden.py --diff` |
| Hygiene | 无空白错误且用户改动保留 | `git diff --check`；定向 `git diff` |

## 8. Stop Conditions

实现只有在以下条件全部满足时才可宣称完成：

1. 严格一/二/三级均由 `execution_setup` 唯一产生。
2. 四类已知越级路径全部被测试阻断：普通 ST、PRE_MARKUP、无 SOS 的 LPS、普通 JAC。
3. Spring reclaim 与 Spring Test 确认已分离，回测使用确认日。
4. 原有市场、板块、数据质量和数量限制测试无回归。
5. 两个仓库强制质量门 exit 0。
6. 用户现有 HTML 可读性改动仍存在。
7. 文档明确策略收益仍需足够成熟样本验证。

若任一项不满足，保持任务未完成并继续修复；不得通过放宽门槛、删除测试或重生成 golden 掩盖失败。

## 9. Deferred Follow-ups

以下事项有价值，但不与本次正确性修复混做：

1. 候选宇宙从“板块当日涨幅前列”扩展为龙头、中军、低位转强三类。
2. 记录距箱顶/支撑的 ATR 距离、SOS 后延伸幅度和 LPS 序号，在 shadow mode 评估追高风险；没有足够回测证据前不改变排序或准入。
3. 增加单板块 1–2 只和相关主题合并暴露的组合分散约束。
4. 将入场、结构止损、目标和仓位放入独立执行计划工作流；不恢复到 `/candidates` 候选报告。
5. 完成“历史热点板块 → 候选 → Top 3/5 → 次日可成交 → 成本与退出”的生产链回放后，再决定是否调整 `+1/+3/+2`。
