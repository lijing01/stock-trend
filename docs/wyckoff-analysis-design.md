# 维科夫操盘法（Wyckoff Method）集成设计

日期：2026-07-14
状态：设计稿

## 1. 概述

为 stock-trend skill 新增维科夫操盘法分析模块，从 Wyckoff 四阶段（吸筹/拉升/派发/砸盘）角度研判个股趋势，提供独立分析 section 并参与复合评分。

### 集成范围

| 维度 | 决策 |
|---|---|
| 集成深度 | 中等 — 独立 Wyckoff 模块 + 评分权重影响 Composite Score |
| 标的范围 | A 股/港股（个股为主） |
| 数据窗口 | 250 交易日（pipeline 默认加长） |
| 报告形式 | 独立 section + 关键信号联动评分 |
| 分析引擎 | 计算驱动（规则引擎阶段判定 + VSA 量价分析 + 因果关系量化） |

## 2. 架构

```
pipeline/runner.py
  ├── fetch_kline(days=250)          # 现有步骤，days 从 120→250
  ├── analysis/technical.py          # 不变
  ├── analysis/wyckoff.py  [新增]    # Wyckoff 分析
  └── analysis/scores.py             # 新增 `--wyckoff` 参数，12% 权重
       │
       ▼
reporting/report.py → report-template.md
       └── {{#wyckoff}} section [新增]
```

### 文件清单

| 文件 | 类型 | 操作 |
|---|---|---|
| `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` | 新增 | ~1500 行，核心引擎 |
| `.claude/skills/stock-trend/scripts/pipeline/runner.py` | 修改 | 加 Wyckoff 步骤，延长 K-line 默认天数 |
| `.claude/skills/stock-trend/scripts/analysis/scores.py` | 修改 | 新增 `--wyckoff` 参数，+12% 权重 |
| `.claude/skills/stock-trend/scripts/reporting/report.py` | 修改 | 嵌入 wyckoff.json → template context |
| `.claude/skills/stock-trend/assets/report-template.md` | 修改 | 新增 `{{#wyckoff}}` section |
| `.claude/skills/stock-trend/assets/report-template.html` | 修改 | 同步新增 Wyckoff section |
| `.claude/skills/stock-trend/tests/` | 新增 | `test_wyckoff.py` + golden data |

### 零额外网络开销

Wyckoff 分析**仅依赖已有数据**：
- K-line（OHLCV）— `kline.json`，250 日
- ATR — `technical.json` 复用
- MA20/MA60 — `technical.json` 复用

## 3. Data Schema

### wyckoff.json 输出结构

```json
{
  "meta": {
    "ts_code": "600519.SH",
    "name": "贵州茅台",
    "calc_date": "2026-07-14",
    "kline_days": 250,
    "data_quality": "good"
  },
  "phase": {
    "primary": "accumulation",
    "primary_name": "吸筹阶段",
    "confidence": 0.78,
    "secondary_possibilities": [
      {"phase": "markup", "confidence": 0.15},
      {"phase": "phase_unknown", "confidence": 0.07}
    ],
    "primary_sub_phase": "secondary_test",
    "sub_phase_name": "二次测试"
  },
  "range": {
    "support": 145.50,
    "resistance": 168.80,
    "range_height": 23.30,
    "range_height_pct": 16.0,
    "duration_bars": 65,
    "touch_count": 5,
    "is_clear_range": true
  },
  "ranges": [
    {"id": "context_12", "level": "context", "support": 145.5, "resistance": 168.8, "quality_score": 0.76},
    {"id": "minor_96", "level": "minor", "support": 160.2, "resistance": 166.1, "quality_score": 0.81}
  ],
  "timeframes": {
    "context": {"range_id": "context_12", "phase": "context", "is_current": false},
    "minor": {"range_id": "minor_96", "phase": "markup", "structure": "re_accumulation", "current_event": "sos", "is_current": true}
  },
  "event_history": [
    {"type": "spring", "event_date": "2026-06-09", "detected_date": "2026-06-11", "status": "confirmed", "bars_since_event": 20},
    {"type": "sos", "event_date": "2026-07-14", "detected_date": "2026-07-14", "status": "candidate", "bars_since_event": 0}
  ],
  "signal": {"status": "candidate", "age_bars": 0, "structure_level": "minor", "range_id": "minor_96"},
  "swing_points": [
    {"index": 12, "date": "2026-03-15", "type": "low", "price": 142.0, "volume_ratio": 2.1, "is_climax": true, "climax_type": "selling"},
    {"index": 25, "date": "2026-04-02", "type": "high", "price": 165.0, "volume_ratio": 1.3, "is_climax": false}
  ],
  "vsa_signals": [
    {
      "type": "absorption",
      "sub_type": "effort_no_result",
      "strength": 2,
      "bar_index": 98,
      "date": "2026-07-08",
      "description": "放量窄幅，主力吸筹特征"
    },
    {
      "type": "supply_exhaustion",
      "sub_type": "narrow_spread_down",
      "strength": 1,
      "bar_index": 102,
      "description": "缩量下跌，抛压枯竭"
    }
  ],
  "cause_effect": {
    "horizontal_count": 65,
    "vertical_height": 23.30,
    "targets": [
      {"level": 1, "price": 192.10, "ratio": 1.0},
      {"level": 2, "price": 215.40, "ratio": 1.5},
      {"level": 3, "price": 238.70, "ratio": 2.0}
    ],
    "time_projection_days": 33,
    "cause_description": "65 根 K 线横盘吸筹，箱体高度 23.30 (16.0%)"
  },
  "wyckoff_score": 1.5,
  "wyckoff_signals": {
    "verdict": "cautiously_bullish",
    "key_signals": [
      "ST 二次测试缩量确认支撑",
      "VSA 放量窄幅吸筹信号出现",
      "JAC 尚未出现，等待放量突破箱体确认"
    ],
    "trading_implication": "吸筹末期，关注放量突破箱体 + JAC 确认信号后入场"
  }
}
```

### phase 枚举

| primary | 中文 | 子阶段列表 |
|---|---|---|
| `accumulation` | 吸筹阶段 | `selling_climax`, `automatic_rally`, `secondary_test`, `spring`, `lps`, `pre_markup` |
| `markup` | 拉升阶段 | `jac`, `backup`, `continuation` |
| `distribution` | 派发阶段 | `buying_climax`, `utad`, `lpsy`, `sign_of_weakness`, `pre_markdown` |
| `markdown` | 砸盘阶段 | `breakdown`, `panic_selling`, `stopping_volume` |
| `phase_unknown` | 无法判定 | — |

### wyckoff_score 映射

| 阶段 | score | 说明 |
|---|---|---|
| accumulation (early) | +0.5 | 吸筹初期，仍需确认 |
| accumulation (mid/late) | +1.5 | 吸筹中后期，偏多 |
| markup (early) | +2.0 | 初始拉升，强信号 |
| markup (mid) | +1.5 | 拉升中段 |
| markup (late/parabolic) | 0.0 或负值 | 末端加速，风险上升 |
| distribution (early) | -1.0 | 派发初期 |
| distribution (mid/late) | -2.0 | 派发确认 |
| markdown | -2.5 | 砸盘阶段 |
| phase_unknown | 0.0 | 无法判定 |

## 4. 阶段判定引擎

### 4.1 输入

- OHLCV + volume（250 日 K-line）
- ATR14（复用 technical.json）
- MA20/MA60（复用 technical.json）

### 4.2 Swing Point 检测

```
5-bar 窗口找 pivot high/low:
  pivot_high: high > 左2/右2 且高度 > ATR * 0.5
  pivot_low:  low  < 左2/右2 且深度 > ATR * 0.5

输出: swing_points[] {index, date, type, price, volume_ratio, is_climax}
```

Climax 标记条件：
- Selling Climax: 下跌 + volume > MA50 * 2 + 长下影（影线/实体 > 1.5）
- Buying Climax: 上涨 + volume > MA50 * 2 + 长上影（影线/实体 > 1.5）

### 4.3 交易区间检测

```
聚合 swing_point 高点/低点 → 候选区间边界:
  - 至少 3 个 swing touch 同一价格区（ATR * 1.0 容差）
  - 区间高度 > ATR * 3（排除无意义横盘）
  - 区间持续时间 ≥ 20 根 K 线

输出: range {support, resistance, height, duration, touches}
```

### 4.4 多级别区间与事件分类

引擎同时搜索 `context`（150–250根）、`swing`（60–120根）和 `minor`（20–60根）候选箱体。`context` 只作为背景；当前 `phase` 由最近、质量合格的 `minor` 或 `swing` 箱体投影，避免陈旧大箱体覆盖当前小级别结构。

同一箱体的顶层路由互斥：

1. `close > resistance + 1 ATR`：只进入 Markup 分类。
2. `close < support - 1 ATR`：只进入 Markdown 分类。
3. `support - 0.5 ATR <= close <= resistance + 0.5 ATR`：同时计算 Accumulation/Distribution 证据；两者置信度差小于 0.15 时返回 Unknown。
4. 上方 0.5–1 ATR 过渡区保持 Unknown；下方过渡区只接受放量 SOW。
5. 箱体内缺少方向性量价证据时返回 Unknown，不能以 LPS/LPSY 兜底。

`event_history` 保存事件发生日与可交易确认日。Spring 需要跌破后 1–3 根内收回支撑；SOS 在突破日为 candidate，只有后续保持箱顶之上才成为 confirmed。事件超出新鲜度窗口后只能作为背景，不能继续作为当前子阶段或买点。

### 4.5 买点兼容与状态迁移

旧字段 `phase.primary` 与 `phase.primary_sub_phase` 继续保留，以避免影响报告、扫描和回测消费者。新消费者必须额外读取 `signal`：只有 `status == "confirmed"`、`age_bars` 在子阶段有效期内、且阶段属于原有买点集合时，才允许作为正式买点。

Spring 允许高量 Shakeout 与缩量供应枯竭两种变体；其发生日、确认日和当前年龄由事件模型给出。SOS 复用既有 `jac` 子阶段以保持兼容：突破日可以输出 `markup/jac + signal.status=candidate`，后续持稳才成为可交易确认信号。

### 4.6 置信度计算

| 因素 | 权重 | 条件 |
|---|---|---|
| 区间清晰度 | 30% | touch_count ≥ 5 → 1.0; < 3 → 0.3 |
| 成交量验证 | 30% | 量价行为与阶段特征一致 |
| VSA 信号 | 20% | 同向 VSA 信号比例 |
| 一致性 | 20% | 多个子阶段判定方向一致 |

## 5. VSA（Volume Spread Analysis）

独立于阶段判定的量价关系分析，输出信号列表。

### 5.1 信号矩阵

| 信号 | 检测条件 | Strength |
|---|---|---|
| **Absorption**（吸筹） | volume↑ + spread↓ (range/ATR < 0.6) | (vol_ratio - 1) * 2 |
| **Upthrust**（假突破派发） | high > prev_high + close退回 | 影线/实体比 |
| **No Supply**（抛压枯竭） | narrow spread down + vol↓ | (1 - vol_ratio) * 3 |
| **No Demand**（买盘枯竭） | narrow spread up + vol↓ | (1 - vol_ratio) * 3 |
| **Stopping Volume**（止跌量） | down + high vol + small spread + lower shadow | 下影线/实体比 |
| **Preliminary Support**（初始支撑） | down + vol spike + bounce close | vol_ratio * 0.5 |
| **Buying Effort**（买入努力） | vol↑ + close at high | vol_ratio * spread_ratio |
| **Selling Effort**（卖出努力） | vol↑ + close at low | vol_ratio * spread_ratio |

### 5.2 Effort vs Result 背离

```
Effort = volume_ratio（当日量 / MA50 均量）
Result = (close - open) / ATR（当日价格变动幅度）

背离检测：
  Effort↑(>1.5) + Result↓(<0.3) → Absorption / Upthrust
  Effort↓(<0.6) + Price not down → 抛压枯竭 / 支撑确认
  Effort↑(>2.0) + Price not up   → 派发 / 阻力确认

strength = min(abs(Effort - 1) * 2, 3)   # [1, 3]
```

## 6. Cause-Effect 量化

Wyckoff "因果对应"原则的数值化实现。

### 6.1 Horizontal Count（横盘时长→时间目标）

```
箱体横盘 N 个交易日的震荡：
  → 突破后趋势持续 ≥ 0.5 × N（保守）
  → 突破后趋势持续 ≥ 1.0 × N（中性）

输出: time_projection = floor(N * 0.5)  # 保守估算
```

### 6.2 Vertical Count（箱体高度→价格目标）

```
箱体高度 H = resistance - support

价格目标（从突破点起算）:
  target_1 = breakout_price + H × 1.0
  target_2 = breakout_price + H × 1.5
  target_3 = breakout_price + H × 2.0

适用于 Accumulation→Markup
Distribution→Markdown 反向同理
```

### 6.3 条件守卫

- 箱体高度 > ATR × 3（过窄箱体无效）
- 箱体 touch_count ≥ 3（充分测试）
- 非 phase_unknown 状态

## 7. 评分集成（scores.py 变更）

### 7.1 权重分配

| 维度 | 当前权重 | 调整后 |
|---|---|---|
| 技术面 | 35% | 30% (-5%) |
| 资金面 | 25% | 25% |
| 基本面 | 15% | 15% |
| 情绪面 | 15% | 15% |
| 宏观面 | 10% | 10% |
| **维科夫** | — | **12%** (新增) |

调整逻辑：从技术面腾出 5%，因为 Wyckoff 部分替代了传统趋势分析。总权重 107%，归一化后各维度的有效权重为：技术 28% / 资金 23% / 基本 14% / 情绪 14% / 宏观 9% / 维科夫 11%。

### 7.2 折让机制

- K-line < 150 日 → wyckoff 权重降至 6%
- `is_clear_range == false` → score 折让 0.5
- 无 VSA 信号 → 折让 0.3

## 8. 报告展示

### 8.1 新增 Section 内容

report-template.md 新增 `{{#wyckoff}}...{{/wyckoff}}` block，包含：

```
### 维科夫操盘法分析

**阶段判定**: {{phase_name}}（置信度 {{confidence}}%）
**当前子阶段**: {{sub_phase_name}}

**交易区间**: {{support}} - {{resistance}}（横盘 {{duration_bars}} 日）

**因果量化**:
- 横盘时长: {{horizontal_count}} 日 → 延续预期 {{time_projection}} 日
- 箱体高度: {{vertical_height}} ({{height_pct}}%)
- 目标价: T1 {{target_1}} / T2 {{target_2}} / T3 {{target_3}}

**VSA 核心信号**:
{{#vsa_signals}}
  - {{description}}（强度: {{strength}}）
{{/vsa_signals}}

**操作含义**: {{trading_implication}}
```

### 8.2 与现有 Section 联动

- `Comprehensive Analysis` section：Agent 在综合研判时引用 Wyckoff 阶段结论
- `Key Signals` table：新增 "维科夫" 维度行，显示阶段+子阶段+分数
- `Trading Action Plan`：cause_effect targets 影响目标价设定

## 9. 实施计划

### Phase 1：核心算法

- `analysis/wyckoff.py` — swing 检测 + 区间判定 + 决策树分类
- pipeline runner 集成

### Phase 2：VSA + 因果量化

- VSA 信号矩阵实现
- cause_effect 计算
- scores.py 集成

### Phase 3：报告 + 测试

- template 更新
- `test_wyckoff.py` + golden data
- 端到端验证

## 10. 测试策略

| 层级 | 覆盖 |
|---|---|
| 单元测试 | swing 检测 / VSA 信号 / 阶段判定（mock K-line） |
| Golden 测试 | 典型形态 snapshot：真实 600519（吸筹）/ 000858（拉升）/ 300750（派发）/ 000002（砸盘） |
| 边界测试 | 数据不足 / 无清晰区间 / 剧烈波动行情 / 新股 |
| 集成测试 | pipeline 全流程 + scores 权重校验 |
