# 每日复盘工作流

## /daily-review [--no-refresh] [--json] [--no-html]

今日复盘 + 市场环境评分 — 整合全市场上下文(大盘/成交额/涨跌家数/涨停/资金/板块排行),输出 0-100 市场环境评分 + 每日复盘报告,并持久化 `market_regime.json` 上下文供 `/stock-trend` 做大盘/板块对比。

**评分公式**: 大盘趋势(25%) + 成交额(20%) + 赚钱效应(25%) + 涨停情绪(20%) + 资金(10%)。≥80 强势(可建仓)/ 60-79 中性(轻仓观察) / <60 弱势(降仓/空仓,不找牛股)。

**评分解释与数据资格**: 正式大盘趋势组件当前使用上证(`000001.SH`)、沪深300(`000300.SH`)和深成指(`399001.SZ`)的 MA20 状态平均；中证500、 中证1000、创业板指和科创50不自动计入该正式分，除非后续版本明确变更模型。成交额组件另用上证与深证综指(`399106.SZ`)拼接两市成交额。每次上下文同时保存 `market_explanation/v1`：五项分数、权重、归一化分母、贡献、指数名单及证据资格。`data_quality=partial` 是正式推荐硬门控，不能因为分数可计算就放行。

证据资格与分数状态分开记录：完整/部分/缺失、fresh/stale/unknown、primary/alternative/estimate/unknown、scorable/reference_only/unavailable。市场资金正式使用全市场主力净流入(`market_main_force_net_inflow`)；该数据有效时标记 `primary`/`scorable`，不因北向数据不可用产生 `partial` 或 `regime_data_partial`。其他组件仍按各自完整性标记，缺少来源时间戳不得标记为 fresh。盘中解释额外保存锚分、外推分和混合权重；无法取得锚证据时只显示已存正式分，不声称组件合计解释了盘中混合分。

**盘中混合口径**: 交易时间内跑，评分为「上一收盘锚 + 盘中按已过 240 交易分钟占比外推」的混合 —— 半日成交额/涨停/涨跌家数按已过时间占比放大估全天值再打分，早盘(开盘约 40 分钟内)不外推、直接用上一收盘。越早越依赖昨收，因此**不会因半日数据误报弱势**。报告标 `盘中临时`，输出含 `intraday: true` + `intraday_note`；盘中快照**不写** `market_regime_history.json`(避免 partial 数据污染后续基线)。

**步骤**：

1. 运行(默认生成 HTML,`--no-html` 仅 MD,`--json` stdout 结构化输出):
```bash
python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py [--no-refresh]
```
2. **默认打开 HTML 报告**:
```bash
open -a "Google Chrome" reports/lists/daily-review-<最新时间>.html
```
3. 数据源: 指数K线(东财→BaoStock降级)、行业板块排行、涨停池(AKShare)、地域板块涨跌家数/全市场主力净流入。
4. 输出: 复盘报告(①市场环境 ②板块最强/最弱) → `reports/lists/daily-review-<时间>.md` + `.html`；HTML 另含 ③“观察列表”。该区块唯一标的来源是 `observation_list.yaml`，按同一依据日对 YAML 中每只股票执行今日推荐的六维分析（动量、量价、资金、基本面、板块强度、维科夫），展示六维分数、综合/质量分和数据诊断；不读取候选扫描的 `buckets.observation`，不展示候选新闻影子分或推荐数量门槛。统一入口先呈现“YAML 观察列表六维分析进行中”，分析完成后后台原子替换同一报告链接；失败只降级该区块，不阻断市场评分或正式推荐。分析 artifact 必须绑定依据日和 YAML 内容摘要，历史重放不得使用未来 K 线。
5. 持久化: `market_regime.json`(今日上下文,供 /stock-trend 对比)、`market_regime_history.json`(30天,支撑涨停/成交额均值)。`--no-refresh` 仅重用市场缓存并直接读取观察列表，不写回观察列表配置。

复盘回复只呈现同一次运行的市场评分、宽度/情绪、板块强弱及数据质量说明；盘中或非当日结果必须标注 `intraday_note`/`stale_note`，缺失字段写“未提供/数据缺失”。完整报告保留 Markdown/HTML 路径，并附带共享免责声明。

**与 /stock-trend 联动**: 每次先跑 `/daily-review` 生成市场上下文,`/stock-trend` 报告自动含「📊 大盘/板块对比」段(个股 vs 沪深300 相对强弱 + 所属板块位置)。
