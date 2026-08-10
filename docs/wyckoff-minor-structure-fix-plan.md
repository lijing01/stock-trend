# 小级别维科夫结构识别修复计划

日期：2026-08-10  
状态：待实施

## 1. 背景

当前维科夫引擎可以输出吸筹、拉升、派发、砸盘及 Spring、LPS、JAC、BU 等子阶段，但实现本质上仍是：

1. 从整个日 K 窗口中选择一个交易区间；
2. 根据最新价格相对区间的位置进行单点分类；
3. 只输出当前阶段，不保存完整事件演进；
4. 不区分大级别背景和小级别当前结构。

以泰格医药为例，人工识别的小级别序列为：

```text
60.78 → 35.44：Markdown
39 附近首次止跌：Phase A
38–40 反复震荡：Phase B
35.44：Phase C Spring / Shakeout
35.44 → 48：Spring Response + SOS
42–45：Phase D LPS / Backup
45–50：Re-accumulation
54.35 大阳线：新的 SOS 尝试
```

现有算法使用截至 2026-08-07 的 250 根日 K 运行后，却输出：

```text
阶段：Accumulation / Spring
置信度：0.70
箱体：46.35–61.32
箱体持续时间：222 根 K
```

这说明陈旧的大级别箱体覆盖了近期小结构，历史 Spring 也被错误当成当前事件。

## 2. 修复目标

修复完成后应满足：

- 35.44 可以作为历史 Spring/Shakeout 事件保留，但不能在价格上涨至 54.35 后继续输出为当前 Spring。
- 能识别 45–50 附近的小级别再吸筹区间。
- 54.35 应输出小级别 `SOS/JAC` 或 `SOS candidate`，而不是大箱体中的 Spring。
- 同时保留大级别背景和小级别当前状态，避免二者互相覆盖。
- 没有足够证据时返回 `phase_unknown`，不再使用 LPS/LPSY 无条件兜底。
- 保持 scanner、candidates、scores、report 和 backtest 现有字段兼容。
- 所有历史重放严格使用当时可见数据，禁止未来数据泄漏。

## 3. 非目标

本轮不包含：

- 分钟级或盘中高频维科夫策略；
- 根据单个泰格医药样例硬编码价格阈值；
- 为提高回测胜率而调整复合评分权重；
- 自动重新生成 golden snapshot；
- 用主观阶段标签替代量价与结构证据。

## 4. 总体架构

修复分为三层：

```text
OHLCV
  └── 多尺度 Swing 与候选箱体
        ├── Context：150–250 根，大级别背景
        ├── Swing：60–120 根，中级别波段
        └── Minor：20–60 根，小级别当前结构
              └── 事件状态机
                    SC → AR → ST → Spring/Test → SOS/JAC → LPS/BU
                                                        └── Re-accumulation → 新 SOS
```

旧的 `phase.primary` 和 `phase.primary_sub_phase` 继续输出，供现有下游消费；新增 `timeframes`、`structures` 和 `event_history` 表达多级别结构。

## 5. 阶段一：锁定现有误判

### 5.1 涉及文件

- 修改：`.claude/skills/stock-trend/tests/test_wyckoff.py`
- 新增：`.claude/skills/stock-trend/tests/fixtures/wyckoff_taige_minor.json`

### 5.2 回归样例

新增一组确定性 OHLCV 数据，覆盖：

1. Markdown 至 35.44；
2. 35.44 跌破后收回；
3. 35.44→48 的 Spring Response；
4. 42–45 回踩；
5. 45–50 横盘；
6. 54.35 放量突破。

fixture 应去除与测试无关的数据字段，只保留日期和 OHLCV。测试不得依赖运行时缓存或联网行情。

### 5.3 核心断言

```text
截至 6 月 9 日：
  35.44 只能是 spring_candidate，不能提前使用未来 K 线确认。

截至 6 月 11 日：
  历史事件中 Spring 得到确认，当前状态进入 spring_response。

截至 7 月 3 日：
  当前可判为 LPS/Backup，但不得继续输出 Spring。

截至 8 月 7 日：
  当前事件为 SOS/JAC 或 SOS candidate；
  历史事件保留 Spring；
  当前事件不得为 Spring。
```

同时加入以下反例：

- 中性箱体不能默认 LPS；
- 中性箱体不能默认 LPSY；
- 陈旧事件不能直接成为当前事件；
- 大箱体不能吞掉有效的近期小箱体；
- 未突破箱顶不能判 SOS/JAC；
- 单根放量阳线没有突破结构压力时，最多为 `sos_candidate`；
- `phase_unknown` 和未确认候选事件不得进入买点集合。

### 5.4 验收

先运行新增测试并确认其在当前实现上失败，再开始修改分类器。不得先调整 fixture 迎合当前输出。

## 6. 阶段二：修复当前单箱体分类器

### 6.1 涉及文件

- 修改：`.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- 修改：`.claude/skills/stock-trend/tests/test_wyckoff.py`

在修改 Python 前，应先说明预期改变：消除吸筹优先偏置、移除无证据兜底，并阻止陈旧事件成为当前信号。影响范围包括维科夫阶段、置信度、选股漏斗信号数量和 golden 输出。

### 6.2 删除无证据兜底

删除当前分类器末尾的：

```python
return (SUB_LPS, 0.5)
return (SUB_LPSY, 0.4)
```

LPS、LPSY、PRE_MARKUP、PRE_MARKDOWN 必须有明确量价、位置和结构证据；否则返回 `None`。

### 6.3 互斥位置路由

顶层分类顺序改为：

```text
明确高于箱顶 → 只检查 Markup/SOS/JAC
明确低于箱底 → 只检查 Markdown/Breakdown
箱体内部     → 仲裁 Accumulation 与 Distribution
上下过渡区   → Unknown，等待确认
```

禁止继续使用“先试 Accumulation，失败再试 Distribution/Markup/Markdown”的调用顺序。

箱体内部同时存在多空证据时：

- 比较候选置信度；
- 置信度差小于 0.15 时返回 `phase_unknown`；
- 将候选方向写入 `secondary_possibilities`，但不得产生买点。

### 6.4 事件时效性

所有结构事件增加：

```json
{
  "event_index": 207,
  "event_date": "20260609",
  "detected_date": "20260611",
  "bars_since_event": 41,
  "status": "confirmed"
}
```

初始有效期建议：

| 事件 | 当前信号最大有效期 |
|---|---:|
| Spring / Shakeout | 8 根 K |
| SOS / JAC | 8 根 K |
| LPS / BU | 10 根 K |
| ST / Test | 8 根 K |

超过有效期后，事件只能保留在 `event_history`，不得继续作为 `primary_sub_phase` 或当前买点。

### 6.5 Spring 条件

Spring 至少支持三种状态：

- `spring_candidate`：发生跌破，但尚未完成收回确认；
- `shakeout_high_volume`：放量跌破后快速收回；
- `spring_low_volume`：缩量刺破、供应枯竭后收回。

确认必须包含：

```text
跌破箱底
→ 1–3 根内重新收回
→ 后续没有继续放量破低
```

当前实现只接受摆动量比大于 1.5 的 Spring，需要修正为量价组合证据，不能把“放量”设为唯一合法类型。

中文名称从“初支（Spring）”改为“Spring（弹簧效应/震仓）”，避免与 PS（Preliminary Support，初步支撑）混淆。

### 6.6 验收

- 中性箱体返回 unknown；
- 明确突破不会被 Accumulation 抢占；
- 过期 Spring 不再成为当前子阶段；
- 原有 SC、AR、ST、LPS、JAC、BU、SOW、Breakdown 正向用例仍可达。

## 7. 阶段三：实现多级别箱体

### 7.1 涉及文件

- 修改：`.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- 修改：`.claude/skills/stock-trend/tests/test_wyckoff.py`
- 修改：`docs/wyckoff-analysis-design.md`

### 7.2 多尺度范围

分别搜索：

| 级别 | 建议窗口 | 用途 |
|---|---:|---|
| `context` | 150–250 根 | 大级别背景 |
| `swing` | 60–120 根 | 中级别主波段 |
| `minor` | 20–60 根 | 小级别吸筹、再吸筹和当前突破 |

固定窗口只定义候选搜索范围。最终箱体边界仍由 swing touch、ATR 容差、持续时间和量价结构共同决定，不得直接把窗口最高低点当作箱体。

### 7.3 候选箱体结构

```json
{
  "id": "minor_20260703",
  "level": "minor",
  "support": 44.8,
  "resistance": 50.5,
  "start_index": 224,
  "end_index": 248,
  "touch_count": 6,
  "recency_score": 0.94,
  "quality_score": 0.78
}
```

候选箱体质量评分建议为：

```text
触碰质量             25%
持续时间             20%
边界离散度           20%
当前相关性           20%
量能收敛和结构证据   15%
```

当前阶段使用得分最高且仍然有效的近期箱体；大箱体只提供背景，不能覆盖小箱体的当前事件。

### 7.4 数据结构兼容

保留旧输出：

```json
"phase": {
  "primary": "markup",
  "primary_sub_phase": "jac",
  "confidence": 0.78
}
```

新增：

```json
"timeframes": {
  "context": {
    "phase": "markup",
    "structure": "post_accumulation"
  },
  "minor": {
    "phase": "markup",
    "phase_letter": "D/E",
    "structure": "re_accumulation",
    "current_event": "sos",
    "range_id": "minor_20260703"
  }
},
"structures": [],
"event_history": []
```

旧 `phase` 字段由“当前最相关、已确认、未过期的小级别结构”投影产生。若小级别证据不足，再降级到 swing/context，但必须降低置信度并标明来源级别。

### 7.5 验收

- 同一数据上能够同时输出大级别和小级别箱体；
- 小级别箱体不会被 150–250 根窗口的密集价格簇覆盖；
- 每个阶段结论都能追溯到 `range_id`；
- 低质量候选箱体不会产生已确认买点。

## 8. 阶段四：加入事件状态机

### 8.1 建议接口

```python
detect_wyckoff_events(...)
build_structure_state(...)
classify_current_event(...)
```

### 8.2 事件枚举

```text
PS / SC / AR / ST
Spring / Shakeout / Test
SOS / JAC
LPS / Backup
Re-accumulation
SOW / LPSY / UTAD
```

`JAC` 是突破动作的现有兼容子阶段；`SOS` 是事件语义。允许同一个已确认事件同时输出：

```json
{
  "current_event": "sos",
  "primary_sub_phase": "jac"
}
```

### 8.3 状态转移

至少约束为：

```text
SC → AR → ST → Spring/Test → SOS/JAC → LPS/BU → Markup
                                     ↓
                             Re-accumulation → 新 SOS
```

规则：

- SOS 必须发生在 Spring、吸筹或再吸筹背景之后；
- BU/LPS 必须发生在已确认突破之后；
- Re-accumulation 必须处于既有上升背景，并位于前一个主要箱体上方或前箱体上沿附近；
- 不符合事件顺序的标签降为 `candidate` 或 `unknown`；
- 历史事件可以补充背景，但不能绕过当前确认条件。

### 8.4 SOS 判定

建议以组合证据判断：

```text
收盘突破当前小箱体阻力和确认缓冲
+ K 线实体/真实波幅明显扩张
+ 成交量相对近期基准放大
+ 收盘位于当日高位区域
+ 可选的后续跟随确认
```

初始阈值建议：

- 收盘高于阻力至少 `max(0.3 ATR, 0.5%)`；
- 当日 spread 至少为 ATR 的 1.2 倍；
- 成交量至少为近 20/50 日基准的 1.2 倍；
- 收盘位于当日振幅上部 30%；
- 若只有部分条件通过，输出 `sos_candidate`；
- 若突破后 1–3 根保持箱顶之上，可升级为 `confirmed`。

阈值必须通过历史回放校准，不能只根据单一样例确定。

### 8.5 验收

- Spring、SOS、LPS/BU 具备时间顺序；
- SOS candidate 与 confirmed 明确区分；
- Re-accumulation 可以产生新的 SOS；
- 单日回放不会引用未来确认信息；
- 54.35 不再输出为当前 Spring。

## 9. 阶段五：调整买点和评分

### 9.1 涉及文件

- 修改：`.claude/skills/stock-trend/scripts/analysis/scores.py`
- 修改：`.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- 修改：`.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`
- 修改对应测试。

### 9.2 买点资格

| 事件 | 买点资格 |
|---|---|
| Spring candidate | 否 |
| Spring confirmed | 轻仓观察，默认不作为正式确认买点 |
| Spring Test | 是 |
| SOS candidate | 否 |
| SOS/JAC confirmed | 是 |
| LPS/BU confirmed | 是 |
| Re-accumulation 箱体内 | 否 |
| Re-accumulation 新 SOS | 是 |
| 陈旧 Spring/LPS | 否 |

新增信号字段：

```json
{
  "is_buy_point": true,
  "signal_status": "confirmed",
  "signal_age_bars": 0,
  "structure_level": "minor",
  "range_id": "minor_20260703"
}
```

正式买点必须同时满足：

```text
signal_status == confirmed
+ signal_age_bars 在有效期内
+ 当前结构仍有效
+ confidence 达到门槛
+ 结构级别符合扫描策略
```

不能再只根据 `phase + sub_phase` 判定买点。

### 9.3 兼容迁移

分两步迁移：

1. 引擎先同时输出旧字段和新字段，下游保持旧逻辑但记录差异；
2. 回测确认后，下游切换到 `signal_status + freshness + structure_level` 门控。

迁移期应输出旧规则与新规则的信号数量差异，避免一次性改变每日候选池而无法定位原因。

## 10. 阶段六：验证与回测

### 10.1 单元测试

覆盖：

- 多箱体发现与排序；
- 大小级别互不覆盖；
- Spring 高量和低量两种形态；
- Spring candidate/confirmed；
- 未来数据隔离；
- SOS candidate/confirmed；
- Spring→SOS→BU 顺序；
- Re-accumulation→新 SOS；
- 过期事件不能进入买点；
- 原有输出字段兼容；
- unknown 和候选事件不能进入扫描信号。

### 10.2 历史回放指标

至少比较修改前后：

- 总信号数量；
- Unknown 比例；
- Spring、SOS、LPS/BU 阶段分布；
- 信号持续天数；
- 同一事件重复发信次数；
- 5/10/20 日收益；
- 相对全样本基线；
- 小级别信号与大级别方向一致/冲突时的表现；
- 候选信号升级为确认信号的比例。

历史重放必须按当日可见数据运行。Pivot 需要右侧 K 线确认时，事件可以记录实际发生日，但可交易的 `detected_date` 必须是确认信息出现日。

### 10.3 泰格医药专项验收

最终输出不强制逐字等同人工标签，但必须满足：

```text
35.44 作为历史 Spring 候选/确认事件可追溯；
54.35 不再输出当前 Spring；
识别最近小箱体，而不是只输出 46.35–61.32 的 222 日大箱体；
54.35 至少为 minor SOS candidate；
量价和突破门槛通过时为 confirmed SOS/JAC；
同时输出大级别背景和小级别当前结构。
```

### 10.4 仓库质量门

任何 Python 修改后都必须运行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

同时运行维科夫专项测试：

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
```

不得通过重新生成 golden snapshot 掩盖失败。只有确认每个数值和输出变化均属预期时，才能更新 snapshot，并在提交信息中解释原因。

## 11. 实施顺序与提交边界

建议拆成五个可独立审查的提交：

1. `test: lock minor Wyckoff sequence regressions`
2. `fix: make Wyckoff phase routing evidence based`
3. `feat: detect multi-scale Wyckoff ranges`
4. `feat: track SOS and re-accumulation events`
5. `fix: gate Wyckoff signals by confirmation and freshness`

前两个提交优先完成，可消除“54.35 仍判 Spring”等明显错误；第 3–5 个提交完成后，才算真正支持小级别维科夫。

每个提交必须满足：

- 只包含对应阶段的代码、测试和必要文档；
- 相关专项测试通过；
- 两个仓库强制质量门通过；
- 不提交运行时缓存或生成报告；
- 不在同一提交中顺带调整评分权重。

## 12. 风险与回退

### 12.1 主要风险

- 移除 LPS/LPSY 兜底后，Unknown 比例会明显上升；
- 多尺度箱体可能增加计算量和候选结构冲突；
- 新鲜度门槛会减少 scanner 和 candidates 的信号数量；
- 新事件字段会影响报告 golden；
- 过度拟合泰格医药会降低跨标的泛化能力。

### 12.2 控制措施

- 新旧信号并行输出一个迁移周期；
- 回测记录信号数量和阶段分布，不以提高胜率作为唯一验收条件；
- 所有阈值集中定义并记录来源；
- 泰格医药只作为回归案例之一，再加入吸筹、派发、假突破和无结构反例；
- 保留旧 `phase` 字段，确保可以按提交回退。

## 13. 完成定义

全部满足以下条件才视为完成：

- 泰格医药专项回归通过；
- 单箱体分类互斥且无无条件 LPS/LPSY；
- 支持 context/swing/minor 多级别结构；
- Spring、SOS、LPS/BU 和再吸筹具备事件顺序；
- 当前事件与历史事件分离；
- 买点包含确认状态、信号年龄和结构级别门控；
- 无未来数据泄漏；
- 所有专项测试与仓库质量门通过；
- 设计文档和输出 schema 与实际实现一致。

## 14. 免责声明

本计划及后续分析仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
