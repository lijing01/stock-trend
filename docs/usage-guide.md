# Stock Trend 日常使用指南

> 更新日期：2026-09-12

本技能聚焦四类日常任务：今日推荐、股票/ETF 单标的分析、ETF 扫描和持仓管理。适合无法盯盘的用户，以 1–6 个月的中线波段为主，不做高频或日内 T+0 建议。

## 今日推荐

```text
今日推荐
今日推荐 --top 30 --min-candidates 20
```

统一入口会先刷新市场环境，再运行候选筛选；候选量化逻辑之后附加新闻影子观察，并在报告就绪后启动后台历史评价、周度研究和策略监控。无需先单独运行市场复盘或候选扫描。

市场环境、数据质量、板块持续性、资金和短线结构共同决定候选资格。数据过期或关键证据缺失时只观察；弱市或证据不足时可以“今日无推荐”。新闻影子只作实验观察，不改变正式推荐。报告附带 `workflow` 阶段状态和后台任务 ID；可在内部维护指南中查看任务查询和恢复方式。

## 股票、港股或 ETF 分析

```text
/stock-trend 600519
/stock-trend 00700.HK
/stock-trend 513180
/stock-trend 600519 --multi-timeframe
/stock-trend 513180 --focus technical
```

分析趋势、关键价位、量价和资金；结合标的类型纳入基本面、宏观或 ETF 专属指标。呈现多空证据、数据日期和来源、关键触发条件及失效条件。ETF 关注净值与折溢价、跟踪误差、规模和适用时的基差。

## ETF 扫描

```text
/etf-scan
/etf-scan --top 10 --output compact
/etf-scan --focus 科技
```

扫描精选 ETF 池并给出排名、主要逻辑、排除项和板块强弱。可聚焦宽基指数、科技、金融、消费医药、制造周期或商品跨境。

## 持仓管理

```text
/portfolio list
/portfolio status
/portfolio alerts
/portfolio kelly
/portfolio add --code 600519 --price 1500 --date 2026-05-01 --qty 100
/portfolio update --code 600519 --stop-loss 1400 --targets 1750,1900
/portfolio remove --code 600519 --close-price 1600
```

`status` 汇总持仓、风险预警、仓位分析和 ETF 对比。`add`、`update`、`remove` 按用户明确提供的信息操作；凯利统计或 ETF 扫描不可用时说明数据降级，不伪造结果。

## 数据与报告

- 行情、公告和新闻使用当前可用数据；每份结果标出日期、来源和质量。实时数据不可用时明确显示缓存或降级状态。
- 盘中候选带临时状态，收盘结果以交易日为准。后台研究可能在候选报告返回后继续执行。
- 默认不自动打开浏览器或 GUI；只有用户明确要求查看本地报告时才打开。
- 所有输出均附带：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。

高级诊断、后台任务运维、策略研究和历史回测见 [内部维护指南](stock-trend-internal-maintenance.md)。
