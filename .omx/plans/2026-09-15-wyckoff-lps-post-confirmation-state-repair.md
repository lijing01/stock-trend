# 修复计划：LPS 确认后的状态失真

## Requirements Summary

目标是修复维科夫引擎把“历史上已确认的 LPS”持续展示成“当前仍然有效”的语义混淆，同时保留事件历史、避免前视偏差，并确保候选门控、JSON、Markdown 和 HTML 使用同一状态。

本次修复只处理 LPS/SOS 确认后的生命周期、执行资格和展示，不修改市场环境、板块持续性、新闻影子层、复合评分权重或数据源。

绝味食品（603517.SH）作为验收样本：

- LPS 事件发生于 2026-09-07、确认于 2026-09-09，历史确认事实应保留。
- 事件来自 `minor_191`，原箱体阻力约为 10.74；2026-09-15 收盘 11.50 尚未构成箱体突破彻底失败。
- 但收盘已跌破 LPS 触发低点/收盘 11.93/11.95，且下跌放量，因此当前应显示“确认后转弱、待重新确认、不可执行”，不能只显示“LPS已确认”。

## Root Cause

1. `MINOR_PHASES` 对 `(markup, lps)` 使用固定文案“阶段D：LPS已确认”，不读取当前状态，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:72-82`。
2. `_current_event()` 在结构可见期内始终优先选择 confirmed LPS；它只按事件年龄筛选，不评估确认后的价格路径，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:1370-1400`。
3. `analyze_kline_dict()` 只对 confirmed SOS 运行 retest/failed-breakout 降级，没有对 confirmed LPS 做确认后健康检查，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:1671-1691`。
4. `_tr_state()` 使用当前选中的 `trading_range`；绝味食品当前选中的是较大的 `swing_137`，而 LPS 来源是 `minor_191`，因此不能用它验证该事件自己的结构边界，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:598-614`、`:1482-1512`、`:1677`。
5. `build_entry_timing()` 只惩罚向上扩张，负向偏离经 `max(0, extension_atr)` 归零；且年龄过期在距离判断前返回，所以“跌破触发位”最终只显示为 stale，见 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:263-305`。
6. 下游已经支持 `retest_pending` 和 `failed_breakout` 的观察池硬门控，但缺少“LPS确认后转弱”状态，见 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:3180-3186`、`:3231-3239`。

## Design Decision

采用“历史事件状态 + 当前健康状态 + 入场时机”三层模型：

- `confirmed_event.status`：不可变历史事实，继续记录事件何时被确认。
- `short_term.current_state`：新增当前健康状态，首版使用 `confirmed_holding`、`follow_through_weakened`、`failed_breakout`、`state_unknown`。
- `entry_timing`：继续负责是否可执行，但健康状态阻断优先于年龄/追高判断。

不采用以下方案：

- 仅改报告文案：核心 JSON 和候选门控仍会误解状态。
- 直接把历史 `confirmed_event.status` 改成 failed：会破坏审计、回测事件时间线，并可能引入前视污染。
- 以跌破 LPS 触发价直接判定整个突破失败：会混淆“短线跟随失败”和“跌回原箱体”；绝味食品仍高于事件所属箱体阻力 10.74。

## Acceptance Criteria

### Core state

- 新鲜、守住触发区域的 confirmed LPS 输出 `current_state=confirmed_holding`。
- 最新收盘有效跌破 LPS 事件低点/触发区域，但仍高于事件所属箱体阻力缓冲区时，输出 `current_state=follow_through_weakened`。
- 收盘跌破事件所属箱体阻力减去既有 ATR 缓冲时，输出 `current_state=failed_breakout`。
- 只出现盘中下影跌破、收盘重新站回边界时，不得直接标记 `failed_breakout`。
- 找不到 `active_event.range_id` 对应箱体时输出 `state_unknown`，不得假装健康。
- 一旦发生 hard failure，旧 LPS 不得因随后一根 K 线简单收回而自动复活；恢复必须来自新的、可审计的确认事件。

### 603517 regression

- 固定样本中 `confirmed_event.status` 仍为 `confirmed`，事件日和确认日不变。
- `short_term.current_state` 为 `follow_through_weakened`，不是 `failed_breakout`。
- `entry_timing.executable=false`，原因码为 `wyckoff_lps_follow_through_weakened`，优先于通用 `wyckoff_signal_stale`。
- 不获得二级 LPS 买点奖励，不进入“今日可执行”“等待触发”或“次日确认”，但保留在观察池。
- 报告不得只显示“阶段D：LPS已确认”；必须显示“历史已确认，后续转弱/待重新确认”等等价文案。

### Compatibility

- `event_history`、`confirmed_event`、事件日期、确认日期和原始 `range_id` 保持兼容。
- 健康的 LPS、Spring、JAC 现有行为不变。
- JSON、候选快照、score ledger、Markdown 和 HTML 的当前状态与原因码一致。
- 历史缓存没有新字段时保持可读，并明确显示 `not_evaluated`/`unknown`；新鲜运行必须生成该字段。
- 所有状态仅使用当前回放切片内的 K 线，历史回放不得读取未来数据。

## Implementation Steps

### 1. 先锁定回归行为

在 `.claude/skills/stock-trend/tests/test_wyckoff.py` 增加确定性 fixture，覆盖：

- confirmed LPS 后正常守位；
- 跌破 LPS 触发区域但仍高于原箱体阻力；
- 跌破原箱体阻力并形成 hard failure；
- 盘中低点刺穿但收盘收回；
- hard failure 后未出现新事件却重新收回；
- 事件所属 range 与当前选中 range 不同。

测试先证明现有代码会把 603517 型样本继续输出为裸 `confirmed`，再修改实现。

### 2. 在核心引擎增加事件生命周期判定

修改 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`：

- 增加按 `active_event.range_id` 查找原始箱体的纯函数，禁止借用当前价格选择出的其他级别箱体。
- 增加 confirmed LPS 当前状态判定纯函数；输入事件、原始箱体、截至当日 OHLCV 和 ATR，输出状态、原因、边界及审计值。
- 将阈值集中定义并复用现有 `LPS_SUPPORT_CLOSE_ATR` / `LPS_SUPPORT_LOW_ATR`；如需触发位转弱缓冲，新增命名常量和规则版本，禁止在报告层复制阈值。
- 扫描确认日至当前日的路径，使 hard failure 具有粘性；只有新的 confirmed 事件可以恢复。
- 保留 `confirmed_event` 不变，把当前判断写入 `signal.current_state`、`short_term.current_state` 和独立 `event_health` 审计对象。

`event_health` 至少包含：`rule_version`、`state`、`reason_code`、`event_range_id`、`structural_floor`、`trigger_low`、`trigger_close`、`current_close`、`current_atr`、`adverse_move_atr`、`evaluated_through`。

### 3. 让执行资格优先服从健康状态

继续在 `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` 调整：

- 扩展 `build_entry_timing()`，先处理 `follow_through_weakened`、`failed_breakout`、`state_unknown`，再处理年龄和向上扩张。
- `follow_through_weakened` 返回 `executable=false` 和 `wyckoff_lps_follow_through_weakened`。
- `failed_breakout` 复用现有硬阻断原因 `wyckoff_failed_breakout`。
- `classify_buy_point_level()` 对非健康状态不返回严格买点等级，防止早期跌破在 age 仍新鲜时继续获得奖励。
- 保持结构研究可见性：旧事件仍可进入观察研究池，但不能成为可执行买点。

### 4. 接入候选门控、记账和展示

修改以下消费者：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`：新增原因码中文映射，把 `follow_through_weakened` 作为观察池硬阻断；诊断中同时展示“历史确认”和“当前转弱”，不能被 stale 文案覆盖。
- `.claude/skills/stock-trend/scripts/core/candidate_score_ledger.py`：在 entry-timing/wyckoff 证据中记录 `current_state`、结构边界和阻断原因。
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`：确认完整透传 `short_term.current_state` 和 `event_health`，不在扫描层重新计算规则。
- `.claude/skills/stock-trend/scripts/reporting/report.py`：扩展下一阶段判断，使 LPS 转弱与真正 failed breakout 使用不同文案。
- `.claude/skills/stock-trend/assets/report-template.md` 与 `report-template.html`：展示“历史事件状态 / 当前健康状态 / 入场状态”三项，避免裸“LPS已确认”被理解为当前建议。

### 5. 增加下游和端到端回归

在 `.claude/skills/stock-trend/tests/test_daily_candidates.py`、`test_stock_trend.py` 增加：

- 转弱 LPS 仅进入观察池；
- 不获得 `strict_level_2` 奖励；
- 原因码、score ledger、JSON、MD、HTML 一致；
- 报告显示“LPS历史已确认、后续转弱”，不显示可执行二级徽章；
- hard failure 继续沿用已有“突破失败”路径；
- 健康 LPS 的现有二级徽章与排序不回归。

在 `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` 增加 as-of 回放测试，证明状态变化只在相应 K 线出现后生效。

### 6. 用真实样本做只读复核

用 603517.SH 的冻结 K 线执行本地重放并核对：

- 2026-09-09：历史确认，当前健康；
- 2026-09-14：仍在触发区域附近；
- 2026-09-15：确认后转弱、不可执行，但不是箱体突破彻底失败。

另选一个真实 hard-failure 样本，验证跌回原箱体后输出 `failed_breakout`。真实样本只作为集成证据，单元测试仍使用确定性 fixture。

## Risks and Mitigations

- **把正常二次回踩误判为失败**：分离 `follow_through_weakened` 与 `failed_breakout`；hard failure 必须相对事件所属箱体判断。
- **当前 range 与事件 range 错配**：只通过 `range_id` 解析原始箱体；缺失时输出 unknown，不猜测。
- **历史事件被未来数据改写**：`confirmed_event` 保持不可变，当前状态独立记录；回测按 as-of 切片计算。
- **门控过严减少候选**：转弱事件保留在观察池，只取消执行资格和奖励。
- **旧缓存兼容问题**：新增字段为 additive；旧缓存显示未评估，新鲜扫描必须完整生成。
- **多个消费者产生不同文案**：核心层输出稳定状态和原因码，报告层只映射展示文本，不重复判断。

## Verification Steps

先运行针对性测试：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Python 修改后必须运行项目强制质量门：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

最后执行：

- 603517 三个 as-of 日期的确定性重放；
- 一次候选 JSON/HTML 生成，核对状态、门控、奖励和文案一致；
- `git diff --check`；
- 不为消除失败而更新 golden，只有确认输出变化符合上述语义时才调整快照。

## Stop Condition

当核心状态、候选门控、报告文案和历史回放全部满足验收标准，针对性测试与两个强制质量门均通过，且 603517 被稳定判定为“历史 LPS 已确认、当前后续转弱、不可执行、非 hard failure”时，本修复完成。
