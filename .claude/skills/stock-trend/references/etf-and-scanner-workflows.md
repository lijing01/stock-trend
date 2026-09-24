# ETF 与扫描器工作流

## /etf-scan [--top N] [--focus <板块>] [--output compact|full]

扫描精选ETF池，输出趋势排名。`--focus`: 宽基指数/科技/金融/消费医药/制造周期/商品跨境。

**步骤**：

1. 运行扫描：
```bash
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py [--top N] [--focus <板块>] [--output compact|full] --output-html
```
输出JSON(stdout)含 `meta`/`combined_ranking`/`top_picks`/`excluded`/`sector_summary`。`--output-html` 生成 `reports/lists/YYYY-MM-DD-HH-mm.html`。

2. 呈现：完整模式→排名表+Top3逻辑+排除+板块强弱。`--compact`→Top5表+Top1-2逻辑+排除摘要。

3. 信号映射：≥+2.0→↑↑看多，+0.5~+2.0→↑偏多，-0.5~+0.5→→震荡，<-0.5→↓偏空。星级：≥80→★★★，≥65→★★☆，≥50→★☆☆。`deep_score`=null时标注"深度分析跳过"。输出须附带共享免责声明。

## /etf-backtest [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20] [--etf <代码>]

回测ETF Phase 1速评分模型预测力。默认120天/top10/窗口5,10,20/间隔5。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/backtesting/engine.py [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20]
```

2. 呈现：回测区间→IC表(窗口/均值/标准差/t值/正向比)→命中率、Top vs Bottom收益差、Top10平均收益。IC>0.05且5%显著=有预测力；命中率>55%=优于随机。

## /wyckoff-backtest [--codes 600519,000001] [--sectors BK0477,...] [--from-candidates <json>] [--lookback-days N] [--eval-windows 5,10,20] [--min-confidence N] [--min-gap N] [--output <path>] [--output-html]

维科夫买点回测 — 验证 `/candidates` 买点信号的历史胜率。历史重放：每采样日用截至当日 K 线跑维科夫分析，检测买点（吸筹/拉升阶段 + Spring/LPS/ST/PRE_MARKUP/JAC/BU 子阶段 + 置信度≥阈值），测 5/10/20 日前向收益，对照全样本基线；同时严格区分一级 Spring/Test、二级 SOS 后 LPS、三级 JAC/BU 后再确认与未分级信号，输出各等级收益、MAE/MFE、样本数和证据状态。

**步骤**：

1. 宇宙来源三选一：
```bash
# 用候选报告验证 (生产路径): /candidates --json > candidates.json
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --from-candidates candidates.json --output-html
# 手动给代码
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --codes 600519,000001
# 复用 /candidates 热点板块宇宙
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --sectors BK0477,BK0897
```

2. 默认 120 天 / 窗口 5,10,20 / 采样间隔 5 / 置信度≥0.3(与漏斗一致) / 同标的信号去重间隔 10 天。

3. 输出 JSON(stdout)：`summary`(信号vs基线 胜率/均收益/α)、`by_sub_phase`/`by_buy_level`/`by_confidence`/`by_phase`/`by_score_100`(分桶胜率)、`risk_by_buy_level`(各等级 MAE/MFE)、`evidence`(各等级样本数及是否达到最低证据门槛)、`health_audit`(确认事件健康门排除计数、状态/事件类型分桶及审计样本)、`ic`(置信度/100分→前向收益)、`strategy_stats`(主窗口,供凯利)、`signals` 明细。`--output-html` 生成 `reports/lists/wyckoff-backtest-*.html`。失效 JAC/Spring 虽保留 `confirmed_event` 与 `event_health` 供审计，但不进入 `signals`、`signal_pairs`、收益统计或买点奖励。

4. 判读：信号胜率显著高于基线=买点有 edge；某子阶段/置信度档位胜率突出=漏斗参数可据此收紧。`evidence.status != ready` 时，分级奖励只视为保守先验，不得据此放大；至少积累每级 100 个信号后，才可据 5/10/20 日收益与 MAE/MFE 调整或归零奖励。

**局限**：close-to-close 无成本无止损模拟；宇宙=当前成分股(幸存者偏差)；买点稀缺时样本少，需用完整 `/candidates` 宇宙跑，单看几只意义不大。

## /stock-scanner [--sectors BK0420,...] [--top N] [--min-score N] [--wyckoff]

A股热点板块成分股筛选器。三阶段：汇聚硬过滤(非A股/ST/市值50-2000亿)→多维打分(动量/量价/资金/基本面/板块强度)→排序定星。

**步骤**：

1. 直接给板块代码：
```bash
python3 .claude/skills/stock-trend/scripts/scans/stock_scanner.py --sectors BK0420,BK0897 --top 10
```

2. **`--wyckoff`(P0-2 选股漏斗)**：只保留维科夫**吸筹/拉升**阶段且子阶段为买点(Spring/LPS/ST/PRE_MARKUP/JAC/BU)、置信度≥0.3 的候选；不足 60 根 K 线的候选丢弃。新增 `wyckoff` 100 分维度，复合分重配为 动量0.25/量价0.15/资金0.15/基本面0.10/板块0.10/wyckoff0.25。输出含 `wyckoff` 字段(阶段/子阶段/置信度/研判)。

3. 呈现：Top 排名表(综合分/维度/信号/预警)。`--wyckoff` 时标注维科夫子阶段与置信度。
