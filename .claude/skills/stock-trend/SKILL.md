---
name: stock-trend
description: 对 A股、港股、ETF 执行日趋势判断，输出结构化报告
triggers:
  - /stock-trend
argument-hint: "<code> [--focus <维度>] [--horizon <周期>] [--multi-timeframe] [--compact] [--no-data]"
allowed-tools:
  - Read
  - Write
  - Bash(python3 .claude/skills/stock-trend/scripts/resolve_code.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/run_pipeline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/compute_scores.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/analyze_technical.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_etf_data.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_capital_flow.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/generate_report.py *)
  - WebSearch
  - WebFetch
  - mcp__web-search__bing_search
  - mcp__web-search__crawl_webpage
  - Bash(open *)
---

# 股票趋势判断 Skill

## Step 1: 解析输入

```
/stock-trend <code> [--focus <维度>] [--horizon <周期>] [--multi-timeframe] [--compact] [--no-data]
```

- `code`（必填）：股票/ETF 代码或名称。缺失时提示用法
- `--focus`（可选，可叠加）：`technical | capital_flow | fundamental | sentiment`
- `--horizon`（可选，默认 `daily`）：`intraday | daily | weekly`
- `--multi-timeframe`：多周期共振模式，同时获取日/周K线并计算周期共振得分
- `--compact`：精简输出模式
- `--no-data`：跳过K线数据获取

**自动解析标的代码**：使用 `resolve_code.py` 自动识别代码或名称：

```bash
python3 .claude/skills/stock-trend/scripts/resolve_code.py <name_or_code> -o /tmp/resolve.json
```

支持输入格式：
- 6位A股代码：`600519`、`513180`
- 5位港股代码：`00700`
- 带后缀代码：`600519.SH`、`159740.SZ`、`00700.HK`
- 中文名称：`恒生科技ETF大成`、`贵州茅台`、`茅台`

输出包含 `ts_code`、`asset`、`adj`、`market`、`name`，后续步骤直接使用输出值。

## Step 2: 数据管线一键执行

使用 `run_pipeline.py` 自动完成数据获取和技术分析：

```bash
python3 .claude/skills/stock-trend/scripts/run_pipeline.py <ts_code> --asset <E|FD> --adj <qfq|none> [-o /tmp]
```

脚本自动执行：
1. 诊断数据源可用性（检查缓存）
2. 获取K线数据（Tushare → 东方财富自动降级）
3. 技术分析（analyze_technical.py）
4. ETF数据获取（标的为ETF时）
5. 资金流向获取

输出文件：
- `/tmp/pipeline_output.json` — 管线汇总（包含数据源、记录数、耗时等元信息）
- `/tmp/kline.json` — K线数据
- `/tmp/technical.json` — 技术分析结果
- `/tmp/etf_data.json` — ETF数据（仅ETF标的）
- `/tmp/capital_flow.json` — 资金流向

**管线失败时的手动降级**：如果管线整体失败，可按以下步骤手动执行：

```bash
# K线数据获取（Tushare）
python3 .claude/skills/stock-trend/scripts/fetch_kline.py <ts_code> --asset <E|FD> --freq <D|W> --adj <qfq|none> -o /tmp/kline.json
# Tushare失败时降级东方财富
python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py <ts_code> --asset <E|FD> --freq <D|W> -o /tmp/kline.json
# 技术分析
python3 .claude/skills/stock-trend/scripts/analyze_technical.py /tmp/kline.json -o /tmp/technical.json
# ETF数据（仅ETF标的）
python3 .claude/skills/stock-trend/scripts/fetch_etf_data.py <fund_code> -o /tmp/etf_data.json
# 资金流向
python3 .claude/skills/stock-trend/scripts/fetch_capital_flow.py <ts_code> --asset <E|FD> -o /tmp/capital_flow.json
```

**数据源降级链**：
- A股/ETF：Tushare → 东方财富(增强头+节点轮换) → BaoStock → 无数据模式
- 港股(.HK)：Tushare → 腾讯财经港股API → 无数据模式

判断 Tushare 是否失败：检查 JSON 的 `meta.data_source`，为 `error` 则降级。`meta.error_type: "permission"` 表示权限不足，直接降级不需要重试。所有数据源均失败时，技术面按 0 分处理并标注"无数据源"。数据不足 60 条时标注。

使用 `--no-data` 时跳过本步骤。

**多周期共振模式** (`--multi-timeframe`)：
除获取日线数据外，额外获取周线数据（`--freq W`），输出到 `/tmp/kline_weekly.json` 和 `/tmp/technical_weekly.json`。在 Step 5 中对比日线与周线趋势方向，计算周期共振得分。

Tushare Token 配置优先级：命令行 `--token` > 环境变量 `TUSHARE_TOKEN` > `.claude/tushare-config.json`。未配置时自动降级东方财富。

## Step 3: 五维分析

**数据质量检查**：检查 `technical.json` 的 `summary.data_quality`：
- `"insufficient"`（数据<30条）：技术面权重降至17.5%，其余维度按比例分配
- `"limited"`（数据30-59条）：技术面权重降至25%，在报告中标注数据质量警告
- 数据不足时，`key_signals` 中会自动包含警告信息

每个维度评分范围 **-3 ~ +3**，详细评分标准见 **references/trend-dimensions.md**。

1. **技术面** — 使用 `analyze_technical.py` 输出：`latest.*.signal` 判断各指标信号（含MA/MACD/RSI/KDJ/布林带/ADX/OBV），`patterns` 判断K线形态（详见 **references/kline-patterns.md**），`summary.total_score` 作为基础得分
2. **资金面** — 主力流入/北向资金/融资融券/龙虎榜
3. **基本面** — PE-PB/业绩增速/行业景气/股息率
4. **情绪面** — 涨跌停/换手率/板块联动/舆情
5. **宏观面** — 货币政策/行业政策/外盘/汇率

### 非技术面数据获取方式

技术面数据由管线自动获取。非技术面（资金面/基本面/情绪面/宏观面）数据需从外部来源获取，按以下优先级：

1. **`WebSearch`**：首选搜索方式，用于宏观政策、行业新闻等公开信息搜索
2. **`mcp__web-search__bing_search` + `mcp__web-search__crawl_webpage`**：中文财经网站内容抓取
3. **`Bash(curl)`**：用于东方财富API等需要自定义Header的场景

**搜索关键词模板**（帮助高效构造搜索）：
- `"{stock_name} {ts_code} 资金流向 南向资金"` — 资金面
- `"{stock_name} {ts_code} 行情分析 估值"` — 基本面
- `"{index_name} 宏观政策 中美关系"` — 宏观面

**禁止使用 `WebFetch` 访问以下域名**（域名安全验证会失败）：
- `*.eastmoney.com`（东方财富系列）
- `cn.investing.com`
- `xueqiu.com`（雪球）
- `10jqka.com.cn`（同花顺）

如需获取上述站点数据，请使用 `mcp__web-search__crawl_webpage` 或 `Bash(curl)` 替代。

**技术面内部子权重**（由 `analyze_technical.py` 的 `build_summary` 自动应用）：
- 趋势指标（MA、MACD）：×1.5
- 趋势强度（ADX）：×1.2
- 震荡指标（RSI、KDJ）：×0.8
- 其他（布林带、成交量、OBV）：×1.0
- K线形态：×0.5

**一致性因子**：同向指标数/总指标数影响置信度，5指标全偏多比3偏多2偏空置信度更高。

## Step 4: 计算综合评分

使用 `compute_scores.py` 自动计算：

```bash
python3 .claude/skills/stock-trend/scripts/compute_scores.py \
  --technical /tmp/technical.json \
  --capital-flow-score <资金面得分> \
  --fundamental-score <基本面得分> \
  --sentiment-score <情绪面得分> \
  --macro-score <宏观面得分> \
  [--focus <维度>] \
  [--asset-type etf|hk|st|stock] \
  [--etf-data /tmp/etf_data.json] \
  [--risks '["风险1","风险2"]'] \
  -o /tmp/scores.json
```

**计算规则**（脚本自动处理）：
- 技术面得分：从 `technical.json` 的 `summary.total_score` 自动提取
- 非技术面得分：通过命令行参数传入（agent 根据 WebSearch 结果判定）
- 权重计算：默认 技术面35% / 资金面25% / 基本面15% / 情绪面15% / 宏观面10%
- `--focus` 调整权重：`technical`→技术55%, `capital_flow`→资金50%, `fundamental`→基本45%, `sentiment`→情绪45%
- 数据质量自动调整权重：insufficient→技术17.5%, limited→技术25%
- 趋势判定：≥ +2.0 看多，≤ -2.0 看空，其他震荡
- 置信度：评分绝对值 ≥ 2.5 且一致性 ≥ 0.7 → 高；≥ 2.0 且一致性 ≥ 0.5 → 中；其他 → 低
- 风险项自动从 `key_signals` 提取
- ETF/HK/ST 特殊标记自动生成

输出 `/tmp/scores.json` 包含综合评分、方向、置信度、风险项、报告参数等全部字段。

## Step 5: 判定趋势与置信度

| 综合评分 | 趋势 | 评分绝对值 | 置信度 |
|---|---|---|---|
| ≥ +2.0 | ▲ 看多 (Bullish) | ≥ 2.5 | 高 |
| ≤ -2.0 | ▼ 看空 (Bearish) | 2.0~2.5 | 中 |
| 其他 | ◆ 震荡 (Neutral) | < 2.0 | 低 |

**多周期共振调整**（`--multi-timeframe` 时适用）：
- 日线与周线同向 → 置信度提升一级（低→中，中→高）
- 日线与周线反向 → 置信度降低一级（高→中，中→低），并在报告中标注"周线趋势相反，注意风险"
- 日线看多+周线看空 = 抢反弹策略，标注"短线操作"
- 日线看空+周线看多 = 回调买入机会，标注"回调关注"

## Step 6: 风险管理与监控信号

基于 `analyze_technical.py` 的 `summary` 输出生成：

**止损位**：`summary.stop_loss`（max(支撑位 - 1×ATR, 当前价 - 2×ATR)）
**目标价位**：三级目标体系
- `summary.target_conservative`：最近压力位（保守目标）
- `summary.target_moderate`：第一个R:R ≥ 1.5的压力位（主目标）
- `summary.target_aggressive`：主目标之后的下一个压力位（激进目标）
**风险收益比**：`summary.risk_reward_ratio`（基于主目标，R:R ≥ 1.5 值得入场）
**R:R质量**：`summary.favorable_rr`（true/false，R:R ≥ 1.5为有利）
**R:R警告**：`summary.risk_reward_warning`（当支撑/压力位过近时自动标注）
**仓位建议**：`summary.position_sizing`（基于 ATR% 波动率）
**最大回撤**：`summary.max_drawdown_pct`（历史区间最大回撤）

**持仓监控信号**（根据趋势方向和关键价位生成）：

| 趋势方向 | 监控条件 | 触发动作 |
|---|---|---|
| 看多 | 跌破止损位 | 立即止损离场 |
| 看多 | 跌破最强支撑位 | 减半仓位，重新评估 |
| 看多 | 突破目标价 | 可分批止盈 |
| 看空 | 突破止损位（空仓反向） | 立即离场 |
| 震荡 | 突破压力/跌破支撑 | 突破方向追入，需量价确认 |

## Step 7: 特殊标的的处理

- **ST/*ST**：基本面强制 -1；标题行标注退市风险
- **港股**：增恒指联动、卖空占比、南向资金、AH溢价；标注无涨跌停
- **ETF**：增 IOPV 折溢价、跟踪误差、申赎、成交额分析
- **可转债**：增转股溢价率、纯债价值、强赎风险

## Step 8: 生成报告

**默认模式**：使用 `generate_report.py` 脚本生成报告

**方式一（推荐，使用管线+评分文件）**：
```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --pipeline /tmp/pipeline_output.json \
  --scores-file /tmp/scores.json \
  --ts-code 159740.SZ --stock-name '恒生科技ETF大成' \
  --output-md reports/159740.SZ/20260514-2200.md \
  --output-html reports/159740.SZ/20260514-2200.html
```

`--pipeline` 自动填充 `--kline`、`--technical`、`--etf-data`、`--capital-flow` 参数；
`--scores-file` 自动填充 `--scores`、`--direction`、`--score`、`--confidence`、`--risks`、`--special` 参数。

**方式二（手动传参，兼容旧方式）**：
```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --technical /tmp/technical.json \
  --kline /tmp/kline.json \
  --etf-data /tmp/etf_data.json \
  --capital-flow /tmp/capital_flow.json \
  --scores '{"technical":1,"capital_flow":0.5,"fundamental":-1,"sentiment":0,"macro":0}' \
  --direction '看多' --score 1.2 --confidence '中' \
  --risks '["布林带极度收口","RSI顶背离"]' \
  --special '{"type":"etf","title":"ETF 特殊分析","content":"IOPV折溢价率: +0.15%"}' \
  --ts-code 159740.SZ --stock-name '恒生科技ETF大成' \
  --output-md reports/159740.SZ/20260514-2200.md \
  --output-html reports/159740.SZ/20260514-2200.html
```

**精简模式** (`--compact`)：

```
{股票名称}({代码}) {趋势符号}{趋势方向} | 评分{综合评分} | 技术{技术面得分} 资金{资金面得分} 基本{基本面得分} 情绪{情绪面得分} 宏观{宏观面得分}
风险: {关键风险1}; {关键风险2}
支撑/压力: {支撑位}/{压力位}
```

**保存路径**：

| 格式 | 路径 |
|---|---|
| Markdown | `reports/{ts_code}/{YYYYMMDD-HHmm}.md` |
| HTML | `reports/{ts_code}/{YYYYMMDD-HHmm}.html` |

`ts_code` 使用 Step 1 解析的 Tushare 代码格式（含市场后缀，如 159740.SZ、600519.SH、00700.HK）

精简模式仅输出文本，不保存 HTML。

## Step 9: 自动打开 HTML 报告

**默认模式**下，生成报告后自动在浏览器中打开 HTML 文件：

```bash
open reports/{ts_code}/{YYYYMMDD-HHmm}.html
```

**精简模式** (`--compact`) 跳过本步骤（无 HTML 文件生成）。

## Step 10: 免责声明

所有输出必须附带：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。

## 参考文件

- 维度评分标准: [references/trend-dimensions.md](references/trend-dimensions.md)
- K线形态参考: [references/kline-patterns.md](references/kline-patterns.md)
- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)