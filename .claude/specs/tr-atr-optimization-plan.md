# TR / ATR 优化修复方案

## 背景

太极集团（600129）案例显示，当前维科夫事件、交易区间（TR）和 ATR 标尺之间存在错配：已确认的 SOS 可能被较新的 candidate SOS 覆盖；TR 在突破后仍被当作旧箱体；ATR14 同时承担风险控制、TR 聚类和突破判定，导致容忍区偏宽。

本方案先修复事件仲裁，再优化 TR 状态和 ATR 用途，避免一次性重写模型。

## P0：事件仲裁

目标：已确认事件不能被同一突破腿中的 candidate 事件覆盖。

涉及文件：`.claude/skills/stock-trend/scripts/analysis/wyckoff.py`

修改 `_current_event()`：

- 有效期内优先选择最新的 `confirmed` 事件。
- 只有不存在有效 confirmed 事件时，才选择 candidate 事件。
- 同一 TR、同一方向、时间间隔较近的 SOS 归为同一突破腿。
- 输出保留 `active_event`、`confirmed_event`、`candidate_event` 和冲突原因。

验收标准：太极集团样例应保留“8月14日 SOS、8月17日确认”；8月17日 candidate 只能作为补充信息，不能将主阶段降级为未确认状态。

## P0：TR 状态机

目标：区分箱体内震荡、突破、突破后回踩和突破失败。

建议状态：

1. `in_range`
2. `breakout_candidate`
3. `breakout_confirmed`
4. `retest`
5. `failed_breakout`

TR 选择不再只按 `minor > swing > context`，还应综合：

- 收盘相对箱顶/箱底的位置；
- 突破后的 K 线数量；
- 连续收盘是否站在箱顶上方；
- TR 的时间连续性；
- 旧 TR 是否已经失效并需要重建。

验收标准：太极集团的 `14.50–16.42` 只能作为背景 TR；8月14日后应进入突破后回踩状态，而不是简单回到 Phase B。

## P1：ATR 分用途

当前 ATR14 使用简单移动平均，公式本身没有错误，但被同时用于风险控制、TR 聚类、突破和 LPS 判定。

建议拆分：

- `ATR14-Wilder`：止损和仓位管理；
- `median_TR20` 或 `ATR20`：TR 聚类容差；
- `breakout_atr`：在 SOS 发生日冻结，用于后续 BU/LPS 回踩判定。

建议参数：

- TR 相关缓冲从 `2×ATR` 收紧为 `min(1×ATR, TR高度×25%)`；
- TR 聚类容差从 `1×median ATR` 收紧为 `0.5–0.75×median ATR`；
- 极端波动后加入 ATR 冷却，避免单日大阴线长期扩大箱体容忍区。

验收标准：太极集团当前 ATR 约0.79时，不能因为 `2×ATR` 缓冲而把接近18元的价格继续视作16.42箱体附近。

## P1：LPS / BU 判定

涉及文件：`.claude/skills/stock-trend/scripts/analysis/wyckoff.py`

- LPS 必须绑定最近一条有效的 confirmed SOS；
- 将 `lps_candidate` 与 `lps_confirmed` 分开；
- 量能同时比较 SOS 当日、近5/10日和 TR 内均量；
- 回撤深度使用 `breakout_atr`，不使用被后续极端波动抬高的即时 ATR；
- 下一根 K 线收复回踩 K 线高点，或重新站稳箱顶后，才升级为 confirmed。

太极集团在8月18日最多应标记为 BU/LPS 候选，不能提前认定为已确认。

## 测试与验证

新增测试覆盖：

- confirmed SOS 不被 candidate 覆盖；
- TR 状态从箱体内到突破、回踩和失败的迁移；
- 高 ATR 不会无限扩大 TR 归属范围；
- LPS 正确绑定 parent SOS；
- 太极集团 2026-07-10 至 2026-08-18 的固定回放样例。

验证命令：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --codes 600129
```

只有确认输出变化符合预期后，才更新 golden snapshot。

## 实施顺序

1. 事件仲裁；
2. TR 状态机；
3. LPS / BU 绑定；
4. ATR 分用途与参数收紧；
5. 太极集团回放、全量回测和回归测试。

本方案是实施计划，尚未修改算法代码。
