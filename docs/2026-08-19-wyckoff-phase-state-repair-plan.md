# 维科夫阶段判断修复计划

## 背景

2026-08-18 的候选报告将圣农发展（002299）标为“阶段 E：离开箱体”。复核发现，程序识别的是 60 日小箱体 15.28–16.56 上方的一次短线 SOS/JAC 尝试；但 8 月 12 日放量回落，8 月 18 日收盘 16.39 已回到箱顶下方，尚不足以确认 Phase E。更合理的判断是 Phase C 后段至 Phase D 早期，等待 SOS 重新确认或 LPS/BU。

## 修复目标

以当前价格状态优先于历史事件为原则，避免“历史上确认过 SOS”持续覆盖当前回落结构；同时将长期结构、短线事件和候选推荐门控分开表达。

## 实施方案

### 1. 重构 SOS/JAC 状态机

涉及：`.claude/skills/stock-trend/scripts/analysis/wyckoff.py`

- 将 `confirmed` 拆分为“历史确认”和“当前仍被价格接受”。
- 状态建议为：`candidate → accepted → retest_pending → lps_confirmed / failed_breakout / expired`。
- 只有最新收盘持续站上箱顶，才允许 `accepted → JAC → Phase E`。
- 已确认 SOS 回落至箱体内时，降级为“Phase D：SOS 后回踩待确认”。
- 仅在缩量、回撤不深、守住箱顶并重新收复时升级为 LPS/BU。
- 放量跌回箱内或跌破失败阈值时，标记 `failed_breakout`，取消买点资格。

### 2. 区分 SOS 历史确认与当前接受

- 突破日必须满足收盘、ATR、实体、收盘位置和量能条件。
- 保留历史 SOS 的可审计确认，但不再把它直接等同于当前 JAC/Phase E。
- 当前是否仍接受突破由最新价格与箱顶缓冲区单独判定。
- 突破确认、当前接受、回踩确认和失败突破应分别记录，避免一个布尔状态承担多个含义。
- 后续可在历史回测验证后再调高持续站稳的确认门槛，避免把真实的回踩误判为 UTAD/派发。

### 3. 优化 Phase C/D/E 输出

报告分开展示：

| 层级 | 内容 |
|---|---|
| 长期结构 | 吸筹、派发或未知，附数据窗和置信度 |
| 当前箱体 | C、D、E 及箱顶/箱底位置 |
| 事件状态 | SOS 候选、已接受、回踩待确认、LPS、失败突破 |

`Phase E` 仅用于当前仍接受突破的结构；`retest_pending` 不得显示为 E，也不得获得 JAC 买点资格。

### 4. 加强长期结构识别

- 日线数据从当前约 250 根扩展到至少 500 根，条件允许时使用 750 根。
- 保留长期、中期、短期三套独立窗口，不让 minor 事件覆盖 context 结论。
- 长期窗口不足或无法覆盖前序下跌时，明确标记“历史窗口不足”，而不是笼统写“长期结构未确认”。
- 长期结构只承担方向门控，不直接把短线事件升级为中线推荐。

### 5. 同步评分和候选门控

- 仅 `accepted JAC` 计入当前买点与高维科夫分数。
- `retest_pending` 进入“次日确认观察”，不得进入今日可执行。
- `failed_breakout` 进入观察池并施加分数惩罚。
- 长短周期冲突时，报告必须显示冲突原因和降级结果。

### 6. 回测验证

扩展维科夫回测，分别统计：

- `accepted JAC`
- `retest_pending`
- `LPS/BU`
- `failed_breakout`

比较 5/10/20 日胜率、平均收益、基线差值和信号失效率。所有历史重放必须只使用当日及以前数据，避免未来数据泄漏。

### 7. 回归测试

涉及：`.claude/skills/stock-trend/tests/test_wyckoff.py`、候选扫描测试和回测测试。

必须新增：

- 已确认 SOS 后回到箱内，不得继续输出 JAC/Phase E；
- 回踩缩量且守住箱顶，升级为 LPS/Phase D；
- 回踩放量或跌破缓冲，输出 `failed_breakout`；
- Phase E 只在当前接受突破时出现；
- 长期结构与短线事件冲突时，长期结构不被短线覆盖；
- HTML/JSON 同时展示周期层级和事件状态；
- 圣农发展历史场景作为固定回归样本，防止问题复发。

## 验证门槛

完成实现后运行：

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

如输出数值或报告结构发生变化，先确认属于预期修复，再更新 golden snapshot；不得为消除失败直接重生成快照。

## 预期结果

## 当前已完成

- 已实现历史 SOS 与当前箱体状态解耦。
- 已实现 `retest_pending` 与 `failed_breakout` 状态，并阻断其买点资格。
- 已将长期结构不明但短线信号未确认的标的门控为观察。
- 圣农发展在 2026-08-18 的重放结果已从“阶段 E/JAC”降为“吸筹阶段/拉升前准备，突破后回踩待确认”，不具备当前可执行买点资格。

长期数据扩展、事件状态的更细粒度回测和额外报告字段仍属于后续工作。

本计划及相关分析仅供学习参考，不构成投资建议。股市有风险，投资需谨慎。
