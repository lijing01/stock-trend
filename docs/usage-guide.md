# Stock Trend Skill — 使用指南

> 更新日期：2026-06-01

Agent 扮演专业股票分析师，从消息面、技术面、情绪面三维综合研判。**不推荐日内短线或高频策略**，侧重中线波段建议（1-6个月持仓）。

**用户画像**：上班族，交易时段无法盯盘。

---

## 目录

- [1. 趋势判断 `/stock-trend`](#1-趋势判断-stock-trend)
- [2. ETF 扫描 `/etf-scan`](#2-etf-扫描-etf-scan)
- [3.1 收盘板块快照（无 Tushare）](#31-收盘板块快照无-tushare)
- [4. 持仓管理 `/portfolio`](#4-持仓管理-portfolio)
- [5. 回测 `/etf-backtest`](#5-回测-etf-backtest)

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

## 3.1 收盘板块快照（无 Tushare）

没有 Tushare 权限时，持续性历史只接受东方财富 `push2` 的行业和概念
两类完整排行。AKShare 同花顺行业摘要、东方财富 BK 历史 K 线和旧 Top-30
记录只能作为旁证，不能补齐全量历史覆盖，也不能把当前成分反推成过去的排行。

建议每个交易日收盘后单独运行一次快照任务，不需要先跑耗时的个股候选扫描：

```bash
# 检查已有完整覆盖：不联网、不写入
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --status --json

# 15:10 后采集行业 + 概念完整截面
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --json

# 只拉取并验证，不写缓存或历史
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --dry-run --json
```

`status=saved` 表示候选板块完整快照已写入；`status=validated` 表示
dry-run 校验通过；`not_closed`、`market_closed`、`incomplete` 或 `error`
均不会新增覆盖天数。`--date YYYY-MM-DD` 只能断言当天交易日，不能用于
历史回填。重复运行同一天只更新该日期记录，不增加历史天数。

首次启用通常需要连续积累 2–3 个有效交易日；在此之前报告继续显示
“板块历史快照不足，尚不能验证持续性”是预期行为。失败日保留缺口，下一
交易日继续采集；本流程不自动安装 cron 或 launchd。

---

## 4. 持仓管理 `/portfolio`

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

## 5. 回测 `/etf-backtest`

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
| 涨停数据 | 东方财富涨停池 (AKShare) | 涨停股/封板/连板/炸板 |
| 龙虎榜 | 东方财富龙虎榜 (AKShare) | 机构买卖明细 |
| 持仓管理 | portfolio.yaml | 用户手动录入持仓 |

---

## 免责声明

本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
