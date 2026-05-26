# 投资回报率优化 — 实施计划

## 背景分析

当前系统已具备：
- ETF 扫描评分（5维: momentum/volume/capital_flow/shares_trend/iopv）
- 个股综合趋势研判（5维: technical/capital_flow/fundamental/sentiment/macro）
- Kelly仓位计算（单持仓级，基于回测胜率+赔率）
- 组合回撤预警（10%/15%阈值）
- ATR吊灯止损/止盈
- 时间止损 + 低效提醒
- 持仓重叠检测

**核心瓶颈**：系统擅长"单标的评分"和"被动预警"，但缺乏**主动决策支持**（何时换仓、怎么分配、如何择时入场）。

---

## Phase 1: 板块轮动信号（影响最大，1-2天）

### 目标
从"每次扫描各看各"变为"跟踪板块动量趋势 + 自动建议轮动"。

### 新增脚本: `rotation_signal.py`

**功能**：
1. 读取 `scan_history.json`（etf_scanner 已维护）中的历史扫描记录
2. 计算每个板块的**滚动动量**（5日、10日、20日板块平均得分变化）
3. 检测板块**加速/衰减拐点**（二阶导数变号）
4. 对比当前持仓所在板块 vs 最强板块，输出轮动建议

**输出 JSON**：
```json
{
  "rotation_signals": [
    {
      "action": "rotate_in",
      "sector": "科技",
      "reason": "5日动量+15% 加速，10日趋势确认",
      "top_etf": "512760",
      "urgency": "medium"
    },
    {
      "action": "rotate_out",
      "sector": "消费",
      "reason": "动量连续3次衰减，20日拐点确认",
      "holding_codes": ["159928"],
      "urgency": "low"
    }
  ],
  "sector_momentum": {...}
}
```

**触发方式**：集成到 `/portfolio status`，自动附加轮动建议。

### 修改文件
- 新增: `scripts/rotation_signal.py`
- 修改: `scripts/portfolio_manager.py` — `status` 命令调用 rotation_signal
- 修改: `SKILL.md` — `/portfolio status` 输出增加轮动建议段

---

## Phase 2: 入场择时优化（中等影响，1天）

### 目标
当 etf-scan 选出 Top ETF 后，不是"立即买入"而是给出**条件委托价位**。

### 新增脚本: `entry_optimizer.py`

**功能**：
1. 接收一个 ETF code + 当前技术分析数据
2. 判断入场策略类型：
   - **趋势回调**：上升趋势 + 当前回调至MA20/MA60 → 限价单区间
   - **突破确认**：横盘突破前高 + 放量 → 突破价上方小幅追入
   - **底部反转**：连续下跌后出现看涨K线形态 → 确认次日买入
3. 输出建议挂单价位（基于ATR计算）和失效条件

**输出**：
```json
{
  "entry_type": "pullback_to_ma20",
  "entry_zone": {"low": 1.520, "high": 1.545},
  "current_price": 1.568,
  "wait_signal": "回调至MA20(1.545)附近",
  "invalidation": "跌破MA60(1.480)入场逻辑失效",
  "max_wait_days": 5,
  "fallback": "5日内未回调则以市价少量建仓"
}
```

**关键**：适合上班族——算好价位，挂好限价单，不用盯盘。

### 修改文件
- 新增: `scripts/entry_optimizer.py`
- 修改: `scripts/etf_scanner.py` — Phase 2 top_picks 增加 entry_optimizer 调用（已有 trading_plan 框架，增强）
- 修改: `SKILL.md` — 增加 entry_optimizer 到 allowed-tools

---

## Phase 3: 市场Regime自动检测（中等影响，0.5天）

### 目标
当前 `regime_coef` 在 portfolio_manager 中需手动判断。自动化它。

### 新增脚本: `regime_detector.py`

**功能**：
1. 获取沪深300近60日K线
2. 计算regime指标：
   - **趋势**：MA5/MA20/MA60排列（多头=牛、空头=熊、缠绕=震荡）
   - **波动率**：20日ATR占比 vs 历史中位数（高波→熊/过渡）
   - **宽度**：沪深300成分股站上MA20比例（>70%牛, <30%熊）
3. 综合输出 regime: bull / bear / oscillate + confidence

**输出**：
```json
{
  "regime": "oscillate",
  "confidence": 0.72,
  "indicators": {
    "ma_alignment": "mixed",
    "volatility_rank": 0.55,
    "breadth_pct": 48
  },
  "regime_coef": 0.8,
  "position_guidance": "总仓位建议40-60%"
}
```

**集成点**：
- `portfolio_manager.py` 的 `cash_ratio_suggestion()` 和 `kelly` 命令自动调用
- `etf_scanner.py` Phase 2 仓位建议引用

### 修改文件
- 新增: `scripts/regime_detector.py`
- 修改: `scripts/portfolio_manager.py` — `status`/`kelly` 自动调用 regime_detector
- 修改: `SKILL.md` — allowed-tools 添加 regime_detector

---

## Phase 4: 事件日历避险（小改动，高实用，0.5天）

### 目标
自动提醒关键事件日前后的风险窗口。

### 新增脚本: `event_calendar.py`

**功能**：
1. 维护一个事件日历（YAML配置 + 动态获取）：
   - 固定事件：A股期权行权日（每月第四个周三）、美股期权（每月第三个周五）、FOMC会议日
   - 动态事件：通过 web search 获取近期重要财报日、政策会议
2. 检查当前日期 ± 3 天内是否有重大事件
3. 输出避险建议：
   - 事件前1-2天：不建议新开仓
   - 持仓到期前事件：考虑减仓或对冲

**触发**：自动集成到 `/portfolio status` 和 `/etf-scan` 顶部显示。

### 修改文件
- 新增: `scripts/event_calendar.py`
- 新增: `data/events.yaml`（固定事件表）
- 修改: `scripts/portfolio_manager.py` — status 命令增加事件提醒
- 修改: `scripts/etf_scanner.py` — meta 区域增加事件提醒

---

## Phase 5: 持仓相关性监控（补充，0.5天）

### 目标
避免"多只ETF同涨同跌"集中风险。

### 增强: `portfolio_manager.py`

**功能**：
1. 获取所有活跃持仓的近30日收盘价
2. 计算两两收益率相关性矩阵
3. 若组合平均相关性 > 0.7，警告"分散度不足"
4. 建议：列出与当前持仓相关性最低的 top 板块

**输出（在 status 中）**：
```
⚠️ 持仓相关性偏高 (avg ρ=0.82)
  513180↔159740: ρ=0.96 (同为恒生科技)
  512760↔588200: ρ=0.88 (同为芯片半导体)
建议考虑配置低相关板块: 黄金(ρ=0.12), 债券(ρ=-0.15)
```

### 修改文件
- 修改: `scripts/portfolio_manager.py` — 新增 `correlation_check()` 函数
- 集成到 `status` 命令输出

---

## 实施优先级与预期效果

| Phase | 特性 | 预期回报提升 | 工作量 | 适配上班族 |
|-------|------|-------------|--------|-----------|
| 1 | 板块轮动信号 | ★★★★ | 1-2天 | ✅ 周度检查 |
| 2 | 入场择时 | ★★★ | 1天 | ✅ 挂限价单 |
| 3 | Regime检测 | ★★★ | 0.5天 | ✅ 自动调仓位 |
| 4 | 事件日历 | ★★ | 0.5天 | ✅ 提前避险 |
| 5 | 相关性监控 | ★★ | 0.5天 | ✅ 分散风险 |

**总工作量**: ~4天
**预期效果**: 通过主动轮动+择时入场+风控增强，在中线周期（1-6月）内可将系统的**风险调整后收益**提升约 20-40%（主要来自减少踏空+控制回撤）。

---

## 测试计划

每个 Phase 完成后：
1. `python3 .claude/skills/stock-trend/tests/test_stock_trend.py` — 现有测试
2. `python3 .claude/skills/stock-trend/tests/test_golden.py --diff` — Golden snapshot
3. 新脚本需新增对应单元测试（mock数据，不依赖网络）
4. 手动跑一次 `/portfolio status` 验证集成输出

---

## 依赖关系

```
Phase 3 (Regime) ──┐
                   ├──→ Phase 1 (轮动, 依赖regime判断牛熊市轮动策略)
Phase 5 (相关性) ──┘
Phase 2 (择时) ────────→ 独立，可并行
Phase 4 (事件) ────────→ 独立，可并行
```

**推荐实施顺序**: Phase 3 → Phase 1 → Phase 2 / Phase 4 / Phase 5（后三者并行）
