# ETF 投资回报率优化方向

> 基于现有代码分析，按优先级排列。每个方向标注影响范围、实现难度、涉及文件。

---

## P0 — 立即修复（1行改动，高ROI）

### 1. 动量评分方向不对称

`etf_scanner.py:784-795` — 价格偏离 MA20 的惩罚只对上涨偏离显著扣分，下跌偏离只扣 5 分：

```python
# 当前逻辑
if deviation_pct > 12:       score -= 20
elif deviation_pct > 8:      score -= 15
elif deviation_pct > 5:      score -= 8
elif deviation_pct > 3:      score -= 3
elif deviation_pct < -3:     score -= 5    # ← 无论跌多少都只扣5分
```

**影响**：任何下跌行情中，所有 ETF 的动量评分系统性高估，回测 IC 被人为压低。

**修复**：改为对称惩罚，下跌偏离 > 8% 同样扣 15-20 分（恐慌性杀跌风险不亚于追涨风险）。

**涉及文件**：`etf_scanner.py` — `score_momentum()` 函数。

---

## P1 — 本周内完成（模型有效性验证 + 大环境判断）

### 2. 回测回填缺失维度（3/5 → 4/5）

`backtest_engine.py:224-228` 把 `capital_flow`、`shares_trend`、`iopv` 全部硬编码为 `None`，回测仅覆盖 2/5 维度（动量+成交量，权重只占 50/100）。

实际上历史数据可以部分回填：

| 维度 | 当前回测 | 实际可行性 |
|------|---------|-----------|
| `shares_trend` | None | 东财 `Data_flvol` 数组含历史份额，可逐日切片 |
| `capital_flow` | None | 东财 API `lmt=250` 可获取历史主力资金流 |
| `iopv` | None | 确实无历史 IOPV，但可用「收盘价/净值偏离」近似 |

**影响**：当前回测结果（IC 0.04-0.07，命中率 55-62%）无法判断是模型无效还是数据不足。回填后 IC 和命中率的结论才有可信度，后续优化才有方向。

**修复**：
1. 回测采样循环中，对每个历史日期调用 `fetch_kline_eastmoney.py` 获取 `amount`（成交额已含在 K 线中，无需额外 API）
2. ETF 份额数据从 `Data_flvol` 数组中按日期切片取出对应时间点的份额
3. `backtest_engine.py` 的 `dim_ic_result` 按维度拆分 IC（现有注释 line 279 已预留位置）

**涉及文件**：`backtest_engine.py`、`fetch_etf_data.py`。

---

### 3. 市场状态过滤器（Regime Filter）

整个评分和仓位系统没有"大盘牛熊"判断层。`compute_scores.py:659-665` 的宏观自动化只看 HS300 单日涨跌，不看趋势方向。

**影响**：牛市中 ETN 评分普遍偏高（假阳性），熊市中 top pick 依然建议正常仓位（假信心）。不区分牛熊的仓位建议等于没有仓位建议。

**方案**：

| 大盘状态 | 判断条件 | 仓位系数 | 止损紧度 | 优先板块 |
|----------|---------|---------|---------|---------|
| 牛市 | HS300 > MA60 且 MA20 > MA60 | 1.0x | 正常 | 科技、券商 |
| 震荡 | HS300 在 MA60 ±3% 区间 | 0.7x | 正常 | 消费、红利 |
| 熊市 | HS300 < MA60 且 MA20 < MA60 | 0.4x | 1.5x 紧 | 债券、货币 |

**实现**：
- 在 `etf_scanner.py` 启动时取一次 HS300 K 线，判断 regime
- `build_trading_plan()` 的 `position_pct` 乘以 regime 系数
- `portfolio_manager.py` 的 `status` 显示当前 regime
- Regime 切换时生成告警（牛→熊：建议减仓）

**涉及文件**：`etf_scanner.py:529-622`（`build_trading_plan`）、`portfolio_manager.py`、`compute_scores.py`。

---

### 4. 趋势阶段缺少下跌分类

`etf_scanner.py:363-430` 的 `detect_trend_stage()` 只检测上涨趋势的 early/mid/late：

```python
# line 382-383: 只判断多头
is_bullish = ma5 > ma20
is_strong_bullish = ma5 > ma20 > ma60
```

下跌趋势的 ETF 永远被归类为 `mid` + `multiplier=1.0`，不会受到任何惩罚。一个 MA5 < MA20 < MA60（空头排列）且 RSI < 30 的 ETF，当前评分和不涨不跌的 ETF 同级。

**修复**：加入 `decline` 阶段检测 — 空头排列 + RSI < 30 → `multiplier=0.5-0.7`，与上涨 late 阶段对称。

**涉及文件**：`etf_scanner.py` — `detect_trend_stage()`。

---

## P2 — 本月内完成（止损/止盈 + 板块轮动）

### 5. 动态止损/止盈

现有止损在建仓后固定不变。

**a) 移动止损**（`etf_scanner.py:562-569`、`analyze_technical.py:1001-1105`）

当前 `stop_price = max(ATR stop, MA20*0.98, 结构低点*0.995)`，建仓后不更新。

- 价格涨 5% 后：止损从成本价上移到 MA20（保本止损）
- 价格涨 10% 后：止损上移到最高点回撤 8%（利润回撤止盈）

**b) 分批止盈**

`build_trading_plan()` 已有 tp1/tp2 两级目标，但 `portfolio_manager.py:133-142` 只检查第一个目标价。应在 `alerts` 中为 tp1/tp2 分别生成不同级别的告警，建议分批卖出而非一次性。

**c) 时间止损**

`portfolio_manager.py` 有 `hold_days` 计算（line 176）但完全不用：
- 持仓 > 3 月且浮亏 > 5% → 时间成本过高告警
- 持仓 > 1 月且浮盈 < 2% → 效率低下提示

**涉及文件**：`etf_scanner.py`（`build_trading_plan`）、`portfolio_manager.py`（`calc_pnl`、`check_alerts`）。

---

### 6. 板块轮动

`etf_scanner.py` 的扫描是纯横截面对比，没有时间序列维度的板块分析。`compute_sector_ranking()` (line 502-521) 已有数据结构基础，只是没做时间序列。

**a) 板块动量排名变化**
- 追踪过去 5/10/20 日各板块平均分趋势
- 板块排名连续上升 → 加分；连续下降 → 减分

**b) 领先板块见顶预警**
- 领先板块连续 3 日评分下降 → 大盘可能调整
- 在 `sector_summary` 输出中标注

**c) 板块内 ETF 联动强度**
- 同板块 ETF 同涨比例 > 70% → 板块趋势确认
- 同板块内部分化严重 → 选股风险增大

**涉及文件**：`etf_scanner.py`（`compute_sector_ranking`）、新增缓存文件存历史板块评分。

---

### 7. 入场时机细化

`build_trading_plan()` (line 529-622) 的 entry 建议只有 3 种：early=立即，mid=回调 MA20，late=回避。

**增强方向**：
- **放量突破确认**：价格突破压力位 + 成交量 > 1.3x MA20 量 → 确认入场
- **RSI 拐点入场**：等 RSI 从 < 30 拐头向上再入场，而非 RSI < 30 就加分
- **布林带收窄后突破**：带宽 < 20% 分位 + 价格突破上轨 → 变盘确认入场
- **缩量回踩支撑**：价格回踩 MA20 + 成交量萎缩 → 加仓信号

**涉及文件**：`etf_scanner.py` — `build_trading_plan()`。

---

### 8. 仓位管理升级

`build_trading_plan()` 的仓位公式（line 575-582）：`base 20% * star * volatility_adjust`，太简单。

**a) 凯利公式**
- 根据回测胜率和赔率动态算仓位：`f = (bp - q) / b`
- 配合半凯利（f/2）降低波动

**b) 相关性控制**
- 同板块 ETF 持仓不超过总仓位 40%
- 沪深 300 + 中证 500 同时持仓 = 部分重叠，需提示

**c) 反马丁格尔**
- 盈利加仓（趋势确认后追加）、亏损减仓（趋势失效后缩仓）
- 当前没有这个逻辑

**涉及文件**：`etf_scanner.py`（`build_trading_plan`）、`portfolio_manager.py`（`status` 命令加相关性检查）。

---

### 9. IOPV 溢价模型精细化

`compute_scores.py:57-95` — 分段线性映射，甜点在 -0.3%。但只用了绝对溢价率。

**增强**：
- **溢价历史分位数**：同一 ETF 溢价 5% 在不同时间段含义不同，用历史百分位替代绝对值
- **溢价趋势**：连续 3 日溢价收窄 vs 刚从折价翻正，方向相反
- **溢价+规模联动**：溢价扩大 + 份额增长 = 资金认可（相对正面），溢价扩大 + 份额下降 = 纯炒作（负面）

**涉及文件**：`compute_scores.py`（`score_iopv_capital_flow`）、`etf_scanner.py`（`score_iopv`）。

---

## P3 — 下月迭代（基础设施 + 组合层面）

### 10. 情绪维度自动化基线

Phase 2 的 `sentiment` 维度完全依赖 Agent 手动搜索，无自动基线。

**可自动化的数据源**：
- **北向资金连续净流入天数**（AKShare `stock_hsgt_hist_em` 已有）
- **融资余额变化趋势**（AKShare `stock_margin_detail_sse` 已有）
- **板块联动强度**：同板块 ETF 同涨比例（可从扫描结果计算）
- **涨停/跌停家数**（AKShare 有）

**涉及文件**：`compute_scores.py`（sentiment 自动化评分）、`fetch_capital_flow.py`。

---

### 11. 基本面评分因子扩充

`compute_scores.py:579-607` — 当前只用了 PE 百分位、营收/利润增速、ROE 三个因子。对 ETF 额外加了 PE 分位和股息率。

**可加入**：
- **股息率**（ETF `index_valuation` 已有，个股也可加入）
- **基金规模变化**（规模持续增长说明资金认可，`fetch_etf_data.py` 已有）
- **跟踪误差**（越小管理越好，对 ETF 尤其重要）
- **份额变动率**（当前在 Phase 1 作为独立维度，也应计入基本面）

**涉及文件**：`compute_scores.py`、`fetch_etf_data.py`。

---

### 12. 组合层面风控

`portfolio_manager.py` 的 `alerts` 只看个股止损/止盈，没有组合层面：

- **组合最大回撤控制**：组合整体回撤 > 10% → 强制减仓告警
- **组合相关性监控**：两只 ETF 跟踪同一指数 → 提示重叠风险
- **组合仓位联动**：牛熊仓位自动调整（依赖 P1 的 regime filter）
- **现金比例建议**：根据当前持仓总风险敞口建议保留现金比例

**涉及文件**：`portfolio_manager.py`。

---

### 13. 扫描结果持久化 + Top Pick 持续性追踪

每次扫描独立运行，没有跨日对比。

**增强**：
- **上次推荐 vs 本次推荐连续性检查**：上次 top pick 现在在哪
- **Top Pick 持续性**：连续 N 天在 Top 10 的 ETF 标注 `streak` 字段
- **历史推荐的命中率跟踪**：N 天前推荐的 ETF 实际表现
- **推荐翻车归因**：高评分但实际下跌的案例复盘

**涉及文件**：`etf_scanner.py`（加缓存持久化 + `streak` 字段）。

---

### 14. Watchlist 动态更新

100 只 ETF 静态配置在 `watchlist.yaml`。

- 成交额连续 N 日 < 阈值的 ETF 自动标记为"低流动性"
- 新上市 ETF 自动加入观察池
- 基于近期表现动态调整关注权重

**涉及文件**：`watchlist.yaml`、`etf_scanner.py`。

---

## 实施路线图

```
Week 1:  P0 #1 + P1 #2 #3 #4     → 基础准确性 + 回测可信度
Week 2:  P2 #5 #6                 → 止损止盈 + 板块轮动
Week 3:  P2 #7 #8 #9              → 入场细化 + 仓位升级 + IOPV
Week 4:  P3 #10 #11 #12 #13 #14   → 组合风控 + 基础设施
```

## 关键指标

| 指标 | 当前基线 | 目标值 |
|------|---------|-------|
| 回测 IC (20日) | 0.04 | > 0.08 |
| 回测命中率 (20日) | 55% | > 65% |
| Top-Bottom 收益差 (20日) | 未知 | > 4% |
| 回测覆盖维度 | 2/5 | 4/5 |
| 组合层面风控项 | 0 | 3+ |
