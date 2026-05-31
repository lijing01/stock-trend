# Stock Trend Skill — 使用指南

> 更新日期：2026-06-01

Agent 扮演专业股票分析师，从消息面、技术面、情绪面三维综合研判。**不推荐日内短线或高频策略**，侧重中线波段建议（1-6个月持仓）。

**用户画像**：上班族，交易时段无法盯盘。

---

## 目录

- [1. 趋势判断 `/stock-trend`](#1-趋势判断-stock-trend)
- [2. ETF 扫描 `/etf-scan`](#2-etf-扫描-etf-scan)
- [3. 市场主线 `/market-theme`](#3-市场主线-market-theme)
- [4. 涨停热力 `/ths-theme`](#4-涨停热力-ths-theme)
- [5. 龙虎榜跟踪 `/lhb-tracker`](#5-龙虎榜跟踪-lhb-tracker)
- [6. 周主线报告 `/weekly`](#6-周主线报告-weekly)
- [7. 持仓管理 `/portfolio`](#7-持仓管理-portfolio)
- [8. 龙头扫描 `/longtou`](#8-龙头扫描-longtou)
- [9. 整合扫描 `/integrated-scan`](#9-整合扫描-integrated-scan)
- [10. 回测 `/etf-backtest`](#10-回测-etf-backtest)

---

## 1. 趋势判断 `/stock-trend`

对单只股票或 ETF 做四维综合评分，输出结构化报告。

```bash
# 分析标的
/stock-trend <code>

# 聚焦维度
/stock-trend <code> --focus technical|capital_flow|fundamental|sentiment

# 多周期共振
/stock-trend <code> --multi-timeframe

# 精简输出
/stock-trend <code> --compact
```

**流程**：

```
Step 1: 解析代码 → Step 2: 数据管线(并发+四维搜索) → Step 3: 综合评分 → Step 4: 风险管理+报告
```

**评分权重**：技术 35% / 资金 25% / 基本 15% / 情绪 15% / 宏观 10%

**信号**：≥+2.0 看多 / ≤-2.0 看空 / 其余震荡

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/core/resolve_code.py <code> -o /tmp/resolve.json
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --code <code>
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --code <code> [维度参数]
python3 .claude/skills/stock-trend/scripts/reporting/report.py --code <code> [输出参数]
```

---

## 2. ETF 扫描 `/etf-scan`

扫描精选 ETF 池，输出趋势排名。

```bash
# 全量扫描
/etf-scan

# 聚焦板块
/etf-scan --focus 科技|金融|消费医药|制造周期|商品跨境|宽基指数

# Top N + 精简模式
/etf-scan --top 10 --output compact
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py [--top N] [--focus <板块>] [--output compact|full] --output-html
```

**信号映射**：
| 条件 | 信号 |
|------|------|
| ≥+2.0 | ↑↑ 看多 |
| +0.5 ~ +2.0 | ↑ 偏多 |
| -0.5 ~ +0.5 | → 震荡 |
| < -0.5 | ↓ 偏空 |

**星级**：≥80 ★★★ / ≥65 ★★☆ / ≥50 ★☆☆

---

## 3. 市场主线 `/market-theme`

扫描板块 + BK 指数 K 线 → 持续性/趋势强度分析 → 识别市场主线。

```bash
# 默认 Top 15，回溯 10 天
/market-theme

# 自定义
/market-theme --top 20 --days 5 --min-score 40
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/market_theme.py [--top 15] [--days 10] [--min-score 30] [--output-html]
```

**三阶段**：
1. 板块扫描（实时排行 API）
2. 快照历史加载
3. 持续性分析：上榜率 30% + 平均热度 20% + 排名趋势 20% + 今日热度 15% + 上涨率趋势 15%

**分类**：
| 类别 | 分数 | 含义 |
|------|------|------|
| 阶段强势 | ≥70 | 持续上榜，趋势向上 |
| 稳步上行 | 50-69 | 温和走强 |
| 新兴主题 | 40-50 | 新冒头方向 |
| 脉冲热点 | — | 今日热但持续<50，追高警惕 |
| 退潮板块 | <40 | 降温中 |

---

## 4. 涨停热力 `/ths-theme`

基于 AKShare 同花顺数据，对行业/概念板块做热力评分。**默认同时执行涨停概念评分 + 龙虎榜分析**。

```bash
# 全量（行业 + 涨停 + 龙虎榜）
/ths-theme

# 仅行业热力（跳过涨停和龙虎榜）
/ths-theme --no-zt --no-lhb

# 指定日期
/ths-theme --zt-date 2026-05-29 --lhb-date 20260529

# JSON 输出
/ths-theme --json
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/ths_theme.py [--top N] [--min-score N] [--json] [--no-zt] [--no-lhb] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]
```

**行业评分**：涨跌幅 35% + 主力净流入 35% + 上涨比率 30%

**涨停评分**（默认开启）：涨停数 30% + 连板强度 25% + 早盘强度 20% + 封单强度 15% - 炸板惩罚 10%

**龙虎榜评分**（默认开启）：机构净买额 40% + 上榜家数 25% + 机构参与度 20% + 净买一致性 15%

**数据源**：AKShare 同花顺行业排行 + 东方财富涨停池 + 东方财富龙虎榜

---

## 5. 龙虎榜跟踪 `/lhb-tracker`

每日记录龙虎榜机构净买板块快照，验证后续 3/5/10 日表现。

```bash
# 保存今日快照 + 查看历史
/lhb-tracker

# 仅保存快照
/lhb-tracker --snapshot-only

# 生成报告
/lhb-tracker --report
/lhb-tracker --html       # 含 Plotly 图表
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py [--history 30] [--report] [--html] [--snapshot-only]
```

**数据存储**：`.cache/stock-trend/lhb_snapshots/YYYY-MM-DD.json`

**信号验证**：胜率 > 60% 视为有效信号；买入/卖出分开统计

---

## 6. 周主线报告 `/weekly`

聚合一周数据（行业热力 + 持续性 + 龙虎榜机构信号），识别中期主线方向。

```bash
# 本周报告
/weekly

# HTML 报告
/weekly --html

# JSON 输出
/weekly --json

# 回溯 2 周
/weekly --weeks 2
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py [--weeks 1] [--html] [--json]
```

**评分公式**：周均热度 30% + 上榜频率 25% + 最新热度 25% + 趋势方向 10% + LHB 验证 10%

**分类**：
| 类别 | 分数 | 含义 |
|------|------|------|
| 🔥 中期主线 | ≥65 | 持续强势，适合中线持仓 |
| 👀 关注方向 | 45-64 | 温和走强，跟踪观察 |
| ❄️ 退潮方向 | <30 | 趋势走弱，规避 |

**数据依赖**：需要市场持续性快照（`/market-theme` 每日运行积累）和 LHB 快照（`/lhb-tracker` 每日运行积累）。

---

## 7. 持仓管理 `/portfolio`

浮动盈亏、止损预警、凯利分析。

```bash
# 查看持仓
/portfolio
/portfolio list

# 添加
/portfolio add --code 600519 --price 1500 --date 2026-05-01 --qty 100 [--stop-loss 1350] [--targets 1700,1800]

# 平仓
/portfolio remove --code 600519 --close-price 1600

# 更新止损/目标
/portfolio update --code 600519 --stop-loss 1400 --targets 1750,1900

# 全面状态（含预警+凯利+ETF对比）
/portfolio status

# 仅预警
/portfolio alerts

# 凯利仓位计算
/portfolio kelly
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/portfolio/manager.py <command> [options]
```

**数据文件**：`.claude/skills/stock-trend/data/portfolio.yaml`

---

## 8. 龙头扫描 `/longtou`

扫描热点板块 → 识别龙头/中军 → pipeline 深度分析。

```bash
# 全市场扫描
/longtou

# 指定板块
/longtou --sector 白酒

# 从 ths-theme 热力数据导入板块（整合模式）
/longtou --sectors-from .cache/stock-trend/qualified_sectors.json

# 精简输出
/longtou --compact
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/scans/market_leader.py [--top N] [--sector <板块名>] [--sectors-from <file>] [--compact] --output-html
```

**三阶段**：
1. 板块扫描（涨幅 40%+主力资金 30%+涨跌比 30%）
2. 龙头筛选（涨幅 50%+成交额 30%+排行 20%）/ 中军筛选（市值 40%+PE 合理性 40%+走势稳定性 20%）
3. Pipeline 深度分析

**板块热力加成**：使用 `--sectors-from` 导入 ths-theme 热板块数据后，龙头评分增加板块热力加成：`composite = 原评分×70% + heat_score×15% + zt_score×15%`。

---

## 9. 整合扫描 `/integrated-scan`

ths-theme + longtou 整合扫描 — 先跑板块热力筛选，再对热板块做龙头扫描，输出整合报告。

```bash
# 默认 Top 10 热板块
/integrated-scan

# 指定数量 + HTML
/integrated-scan --top 15 --output-html

# 精简输出
/integrated-scan --compact
```

**Pipeline 流程**：

```
Step 1: ths_theme.py --export-sectors        → 全市场板块热力 + 涨停概念评分
Step 2: 筛选 heat_score≥50 & zt_score≥50 的板块
Step 3: market_leader.py --sectors-from      → 只扫热板块内的龙头/中军
Step 4: 拼接为整合报告                         → 板块热力 + 龙头清单 + 综合信号标签
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py [--top 10] [--compact] [--output-html] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]
```

**信号标签**：

| heat≥50+zt≥50 | 龙头评分 | 标签 | 含义 |
|:---:|:---:|------|------|
| ✅ | ≥ 1.0 | 🟢 双强·龙头确认 | 首选，可建仓 |
| ✅ | 0 ~ 1.0 | 🔵 双强·关注中 | 板块有力，个股待确认 |
| ✅ | < 0 | ⚪ 双强·无龙头 | 板块热但群龙无首，观望 |
| ❌ | ≥ 1.0 | 🟡 龙头·板块待确认 | 个股强但板块不一致，严设止损 |
| ❌ | < 0 | ⚫ 弱势区 | 不参与 |

**龙头评分融合**：板块热力（heat_score × 15% + zt_score × 15%）叠加到龙头 composite_score 上，板块越热龙头得分越高。

**边界行为**：
- 无热板块 → 不跑 longtou，仅输出 ths-theme 热力报告 + 提示无强信号
- ths-theme 失败 → 降级为 longtou 全市场扫描
- longtou 失败 → 降级为 ths-theme 热力报告

---

## 10. 回测 `/etf-backtest`

回测 ETF Phase 1 速评分模型预测力。

```bash
# 默认：120 天 / Top 10 / 窗口 5,10,20
/etf-backtest

# 聚焦板块 + 自定义窗口
/etf-backtest --focus 科技 --eval-windows 5,10,20
```

**后台脚本**：
```bash
python3 .claude/skills/stock-trend/scripts/backtesting/engine.py [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20]
```

**评判标准**：IC > 0.05 且 5% 显著 = 有预测力；命中率 > 55% = 优于随机

---

## 数据源一览

| 系统 | 数据源 | 数据内容 |
|------|--------|---------|
| 趋势判断 | AKShare / Tushare / 东方财富 | K线、资金流向、基本面、宏观 |
| ETF 扫描 | AKShare / 东方财富 | ETF 净值/IOPV/规模/期货基差 |
| 市场主题 | 东方财富 push2 API | BK 板块排行 + 成分股 |
| 同花顺热力 | AKShare 同花顺接口 | 行业排行 + 概念事件 |
| 涨停数据 | 东方财富涨停池 (AKShare) | 涨停股/封板/连板/炸板 |
| 龙虎榜 | 东方财富龙虎榜 (AKShare) | 机构买卖明细 |
| 整合扫描 | ths-theme + market_leader | 板块热力 + 龙头扫描 + 信号标签 |
| 持仓管理 | portfolio.yaml | 用户手动录入持仓 |

---

## 免责声明

本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
