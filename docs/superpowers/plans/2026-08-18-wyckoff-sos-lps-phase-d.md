# Wyckoff SOS 后 LPS Phase D 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重写现有维科夫识别器和下游门控的前提下，识别“已确认 SOS → 缩量浅回调 → 原阻力附近获得支撑”的 LPS，并将其输出为 Phase D。

**Architecture:** 继续使用 `wyckoff.py` 作为唯一事件判断入口。扩展现有 `event_history`，让 LPS 只能由同一交易区间内较早的 confirmed SOS 触发；当前回踩若满足量价和位置条件则输出 `markup/lps`、Phase D，证据不足时保留现有 `markup/bu`。旧字段、`is_buy_signal()` 和候选扫描默认门控继续兼容。

**Tech Stack:** Python 3.10+, 现有 `unittest`，OHLCV/ATR，既有 Wyckoff scanner 与 backtest。

---

## 设计边界

- 不把 `MINOR_PHASES` 展示映射当作识别逻辑；Phase D 必须来自真实 `markup/lps` 结果。
- 不把 SOS candidate 当作 LPS 的前置确认；只有 SOS `status == "confirmed"` 才能产生 LPS。
- 不删除 `BU`。`BU` 表示突破后的普通回踩；只有额外满足“浅回调、缩量、靠近原阻力并守住”的回调才升级为 LPS。
- 不改变 Spring、SOS/JAC、现有 accumulation/LPS 的语义；新增 `markup/lps` 只扩展组合键。
- 不使用未来 K 线确认当前结果。历史回放在检测到满足条件的当前 bar 时记录 LPS。
- LPS 的正式买点资格沿用现有 `confirmed + freshness + BUY_SUB_PHASES` 门控；candidate 不得进入正式候选。

## 文件影响面

| 文件 | 责任 | 变更范围 |
|---|---|---|
| `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` | LPS 事件、阶段映射、当前信号 | 主要实现文件 |
| `.claude/skills/stock-trend/tests/test_wyckoff.py` | 确定性事件序列回归 | 新增 SOS→LPS fixture/tests |
| `.claude/skills/stock-trend/tests/test_stock_scanner.py` | 下游门控兼容性 | 验证 `markup/lps` 是买点且 candidate 不是 |
| `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` | 回测字段兼容 | 仅在现有分桶断言需要时补充 `markup/lps` |
| `docs/wyckoff-analysis-design.md` | 行为契约 | 补充 SOS→LPS→Markup 顺序 |

---

### Task 1: 先锁定 SOS→LPS 的最小可验证行为

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 添加合成 K 线 fixture，覆盖完整序列**

构造不依赖缓存和联网的 OHLCV 序列：先在 `support/resistance` 箱体内横盘，随后放量突破并在 1–3 根内站稳形成 confirmed SOS，之后创出短期高点，再出现一根浅回调：

```text
箱体：100–110
SOS：收盘 > 110 + 0.3 ATR，spread >= 1.2 ATR，volume >= 1.2×基准，收盘靠近最高
LPS：回调低点约 110 附近，close >= 110 - 0.3 ATR，volume <= 0.8×基准，未回到 TR 中部
```

fixture 直接 patch `detect_trading_ranges()` 返回固定 minor range，避免测试被箱体发现算法干扰。

- [ ] **Step 2: 写失败测试，锁定事件顺序和结果**

新增测试应至少断言：

```python
events = detect_wyckoff_events(ohlcv, atr, trading_range)
sos = next(e for e in events if e["type"] == "sos")
lps = next(e for e in events if e["type"] == "lps")
self.assertEqual(sos["status"], "confirmed")
self.assertGreater(lps["event_index"], sos["detected_index"])
self.assertEqual(lps["status"], "confirmed")

result = analyze_kline_dict(kline_data)
self.assertEqual(result["phase"]["primary"], PHASE_MARKUP)
self.assertEqual(result["phase"]["primary_sub_phase"], SUB_LPS)
self.assertEqual(result["phase"]["minor_phase"]["code"], "D")
self.assertEqual(result["signal"]["event"], "lps")
```

同时添加三个反例：

1. SOS 仍为 candidate 时，后面的回调不能输出 confirmed LPS；
2. 回调放量或跌回 `resistance - 0.5 ATR` 以下时，只能是 `BU`/unknown，不能是 LPS；
3. 没有先发生 confirmed SOS 的普通缩量回踩，不能输出 `markup/lps`。

- [ ] **Step 3: 运行 focused test，确认当前实现失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: 新增 LPS 事件或 `markup/lps` 断言失败；现有测试保持通过。

---

### Task 2: 扩展核心事件识别，加入 SOS 后 LPS

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:35-90,130-155,591-625,933-1002,1123-1152`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: 增加 `markup/lps` 的兼容映射和新鲜度**

保持现有 `SUB_LPS = "lps"`，只增加组合映射，不新建重复枚举：

```python
MINOR_PHASES[(PHASE_MARKUP, SUB_LPS)] = (
    "D", "阶段D：SOS 后 LPS",
    "SOS 后回调缩量、跌幅收敛，并在原阻力附近获得支撑",
)
PHASE_SCORES[(PHASE_MARKUP, SUB_LPS)] = 2.0
EVENT_MAX_AGE[SUB_LPS] = 10
```

`BUY_SUB_PHASES` 已包含 `SUB_LPS`，因此 `is_buy_point()` 无需改动即可接受 confirmed `markup/lps`。

- [ ] **Step 2: 添加纯函数 `_is_lps_pullback()`**

函数只接收截至当前 bar 可见的数据和已确认 SOS，建议签名：

```python
def _is_lps_pullback(
    ohlcv: dict,
    atr_values: list,
    trading_range: dict,
    sos_event: dict,
    index: int,
) -> bool:
```

最小条件全部满足才返回 True：

```text
index > sos_event.detected_index
当前收盘仍 >= resistance - 0.3*ATR
当前最低价没有跌入 TR 中部（>= resistance - 0.5*ATR）
回调相对 SOS 后局部高点不超过 2 ATR
当前成交量 <= 0.8 * 近50日均量
当前实体/真实波幅 <= 1.0 ATR，且 close <= 前一收盘或当日为回调 bar
```

不要把“碰到阻力”单独视为 LPS；位置、缩量和回调幅度必须同时成立。

- [ ] **Step 3: 在 `detect_wyckoff_events()` 中按时间顺序生成 LPS**

保留现有 Spring/SOS 循环。每次产生 confirmed SOS 后，向后扫描最多 `EVENT_MAX_AGE[SUB_LPS]` 根 K 线；遇到第一个满足 `_is_lps_pullback()` 的 bar，调用 `_event_record("lps", ...)`：

```python
if sos_event["status"] == "confirmed":
    for j in range(sos_event["detected_index"] + 1,
                   min(sos_event["detected_index"] + 1 + EVENT_MAX_AGE[SUB_LPS], len(closes))):
        if _is_lps_pullback(ohlcv, atr_values, trading_range, sos_event, j):
            events.append(_event_record(
                "lps", j, j, dates, "confirmed",
                trading_range.get("level", "single"), trading_range, 0.78,
            ))
            break
```

实现时必须避免同一 SOS 产生多个 LPS；事件记录补充 `parent_event="sos"` 和 `parent_event_index`，便于报告和回测审计。

- [ ] **Step 4: 当前事件选择优先使用最新有效 LPS**

将 `_current_event()` 的有效期映射改为：`sos -> SUB_JAC`、`lps -> SUB_LPS`、`spring -> SUB_SPRING`。现有按 `event_index` 取最新事件的逻辑可保留。

在 `analyze_kline_dict()` 中扩展事件到阶段的映射：

```python
if active_event["type"] == "spring":
    phase, sub_phase = PHASE_ACCUMULATION, SUB_SPRING
elif active_event["type"] == "sos":
    phase, sub_phase = PHASE_MARKUP, SUB_JAC
else:  # lps
    phase, sub_phase = PHASE_MARKUP, SUB_LPS
```

同时保留 `signal.event == "lps"`、`event_date`、`detected_date`、`age_bars`、`range_id`，不改旧字段名。

- [ ] **Step 5: 运行 focused tests，确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: 新增序列和反例测试通过，原有 Spring/SOS candidate/JAC/BU 测试不回归。

---

### Task 3: 保持 scanner、报告和评分的最小兼容

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`（仅补字段断言）
- Inspect/modify only if needed: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- [ ] **Step 1: 锁定 `markup/lps` 的下游行为**

新增单元测试：

```python
wk = _wk(phase="markup", sub="lps", conf=0.7, score=2.0)
wk["signal"] = {"status": "confirmed", "age_bars": 1, "event": "lps"}
self.assertTrue(sc.wyckoff_gate_pass(wk))

wk["signal"]["status"] = "candidate"
self.assertFalse(sc.wyckoff_gate_pass(wk))
```

验证候选 payload 仍保留 `phase`、`sub_phase`、`minor_phase`、`signal_status` 和 `signal_age_bars`，且报告显示“阶段D：SOS 后 LPS”。

- [ ] **Step 2: 只在兼容性失败时修改 scanner**

当前 `BUY_SUB_PHASES` 已包含 `SUB_LPS`，预计 scanner 无需逻辑修改。若测试发现 scanner 只允许 accumulation/lps，则将判断改为统一调用 `is_buy_signal()`，不要增加第二套 phase/sub-phase 白名单。

- [ ] **Step 3: 运行 scanner 与 candidates 测试**

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 所有现有测试和新增 LPS 门控测试通过。

---

### Task 4: 回测与质量门禁

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` only if existing assertions enumerate phases/sub-phases
- Modify: `docs/wyckoff-analysis-design.md`

- [ ] **Step 1: 补充回测兼容断言**

确认回测对 `markup/lps`：

- 读取 `signal.status` 和 `age_bars`，而不是仅依赖 phase/sub-phase；
- 将 confirmed LPS 计入买点信号；
- 不把 LPS candidate 计入信号；
- `by_sub_phase` 可以出现 `lps`，不破坏已有 `spring/jac/bu` 分桶。

- [ ] **Step 2: 更新设计文档的事件顺序**

补充明确契约：

```text
Spring → Test → SOS confirmed → LPS confirmed → Markup continuation
```

并说明 `BU` 是普通突破后回踩，`LPS` 是满足缩量、浅回调和原阻力支撑证据的更强子阶段；两者都必须经过 freshness 门控。

- [ ] **Step 3: 运行项目质量门禁**

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

不得通过重新生成 golden snapshot 消除失败；若输出变化是预期的，只记录差异并更新对应测试契约。

---

## 验收标准

1. 没有 confirmed SOS，就不会产生 confirmed `markup/lps`。
2. SOS candidate 后的回调不会被升级成 LPS。
3. confirmed SOS 后，浅幅、缩量、慢跌、守住原阻力的回调输出 `phase=markup`、`sub_phase=lps`、`minor_phase.code=D`。
4. 放量深回调不会输出 LPS；可保留为 BU 或 unknown。
5. LPS 事件携带父 SOS 的日期/索引，且只使用当前及之前 K 线。
6. confirmed LPS 进入现有买点门控，candidate/stale LPS 不进入。
7. Spring、SOS/JAC、BU、accumulation/LPS 原有行为和 JSON 字段保持兼容。
8. 回测与两项仓库质量门禁通过，golden 变化不被无理由重生成掩盖。

## 未纳入本次最小改动的事项

- 不在本轮引入独立的 `forming` tier、执行计划或市场机会分数；这些属于 2026-08-18 timing/actionability 计划的更大范围。
- 不在本轮调节 LPS 评分权重；先用现有 `+2.0`，待历史回放有足够样本后再评估。
- 不把“主仓放 LPS”直接编码成仓位规则；本轮只提供更可靠、可审计的信号层。
