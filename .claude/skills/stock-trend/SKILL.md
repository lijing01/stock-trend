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
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_fundamental.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_macro_snapshot.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/generate_chart_html.py *)
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

## Step 2: 数据管线一键执行（与 Step 3 并发运行）

> **关键说明**：本步骤与 Step 3 **同时执行**。启动管线后，立即进入 Step 3 开始搜索，无需等待管线完成。管线数据（technical.json）在 Step 4 之前就绪即可。

使用 `run_pipeline.py` 自动完成数据获取和技术分析：

```bash
python3 .claude/skills/stock-trend/scripts/run_pipeline.py <ts_code> --asset <E|FD> --adj <qfq|none> [-o /tmp] [--no-cache]
```

**缓存机制**：各数据脚本自动缓存结果到 `/tmp/stock-trend-cache/`。盘中 TTL 5分钟，盘后 TTL 16小时（宏观数据盘中4h/盘后12h，基本面盘中30min/盘后16h）。同一标的同日重复分析命中缓存可跳过 API 调用。`--no-cache` 强制刷新。

脚本自动执行：
1. 诊断数据源可用性（检查缓存）
2. 获取K线数据（Tushare → 东方财富自动降级）
3. 技术分析（analyze_technical.py）
4. ETF数据获取（标的为ETF时）
5. 资金流向获取（含北向/融资融券/龙虎榜数据）
6. 基本面数据获取（通过 AKShare，ETF 标的跳过）
7. 宏观数据快照获取（汇率/利率/PMI/CPI/M2，独立进程）

输出文件：
- `/tmp/pipeline_output.json` — 管线汇总（包含数据源、记录数、耗时等元信息）
- `/tmp/kline.json` — K线数据
- `/tmp/technical.json` — 技术分析结果
- `/tmp/etf_data.json` — ETF数据（仅ETF标的）
- `/tmp/capital_flow.json` — 资金流向（含 data_extended 增强数据）
- `/tmp/fundamental.json` — 基本面数据（AKShare，非ETF）
- `/tmp/macro_snapshot.json` — 宏观快照（AKShare）

**管线完成后**（可选）生成K线图用于报告嵌入：

```bash
python3 .claude/skills/stock-trend/scripts/generate_chart_html.py /tmp/kline.json --technical /tmp/technical.json -o /tmp/chart_fragment.html
```

使用 `--no-data` 时跳过本步骤。管线失败或需要手动降级时，参考 [references/troubleshooting.md](references/troubleshooting.md)。

## Step 3: 四维并行搜索（与 Step 2 同时执行）

> **关键说明**：启动 Step 2 管线后，**立即**按以下四个维度**并行**搜索（使用同时调用的工具），无需等待管线完成。

### 并行搜索指令

使用**一次调用多个搜索工具**同时搜索四个维度：

| 维度 | 权重 | 自动化基线 | 搜索关键词 |
|------|------|-----------|-----------|
| 资金面 | 25% | `data_extended.northbound/margin`（管线已获取） | `"{stock_name} {ts_code} 资金流向 北向资金"` |
| 基本面 | 15% | `fundamental.json`（PE/PB/ROE/财务数据已获取） | `"{stock_name} {ts_code} 估值 业绩"` |
| 情绪面 | 15% | 无自动化 | `"{stock_name} 涨跌停 换手率 板块"` |
| 宏观面 | 10% | `macro_snapshot.json`（汇率/利率/PMI已获取） | `"今日宏观 政策 利率 汇率 外盘"` |

**四个搜索并行执行，无交叉依赖**。

### 自动化数据基线

部分维度的自动化数据已在管线中获取（通过 AKShare），Agent 的使用方式：

- **基本面**：读取 `fundamental.json` 获取 PE/PB 百分位、营收/利润增速、ROE。Agent 可据此直接撰写摘要，无需重复搜索基础数据。数据质量 `good`/`partial` 时可用。
- **资金面**：`capital_flow.json` 的 `data_extended` 包含北向资金持仓变动和融资融券数据
- **宏观面**：`macro_snapshot.json` 提供汇率、PMI、CPI、利率等快照

**Agent 评分始终覆盖自动化评分** — 以 Agent 判断为准。

### 维度摘要与综合研判

为每个非技术面维度撰写**简明摘要**（1-2句话），含关键数据和判断依据，例如：
- 资金面：`ETF近20日净申购+1.75亿元，但主力交易资金近2日净流出1.59亿元`
- 基本面：`恒生科技PE 22.9倍处历史32%分位偏低；腾讯Q1净利润+11%`
- 情绪面：`AI+半导体领涨；指数近1月反弹+4.31%但缩量调整中`
- 宏观面：`中美关税缓和；美元偏弱离岸人民币升破6.8；CPI 3.8%降息推迟`

摘要通过 Step 4 的 `--*-summary` 传入。同时撰写**综合研判**（核心矛盾、关键事件、操作建议），通过 `--analysis` 传入。

### 数据质量检查

检查 `technical.json` 的 `summary.data_quality`：
- `"insufficient"`（数据<30条）：技术面权重降至17.5%
- `"limited"`（数据30-59条）：技术面权重降至25%

### 等待汇合

等待管线完成且所有搜索结果收集完毕，进入 Step 4。
- 管线超时（60s）时技术面按 0 分处理，标注"管线超时"
- 管线和搜索完成即进入 Step 4

**搜索工具选择**：
1. **`WebSearch`**：首选，用于宏观政策、行业新闻等
2. **`mcp__web-search__bing_search` + `mcp__web-search__crawl_webpage`**：中文财经内容
3. **`Bash(curl)`**：东方财富API等需自定义Header的场景

**禁止使用 `WebFetch`** 访问：`*.eastmoney.com`、`cn.investing.com`、`xueqiu.com`、`10jqka.com.cn`

**技术面内部子权重**（脚本自动应用）：趋势指标(MA/MACD)×1.5、趋势强度(ADX)×1.2、震荡指标(RSI/KDJ)×0.8、通道/量能(布林带/成交量/OBV)×1.0、K线形态×0.5。一致性因子：同向指标数/总指标数影响置信度。详细评分标准参考 [references/trend-dimensions.md](references/trend-dimensions.md)。

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
  [--capital-flow-data /tmp/capital_flow.json] \
  [--fundamental-data /tmp/fundamental.json] \
  [--macro-data /tmp/macro_snapshot.json] \
  [--risks '["风险1","风险2"]'] \
  [--capital-summary "ETF近20日净申购+1.75亿元..."] \
  [--fundamental-summary "恒生科技PE 22.9倍..."] \
  [--sentiment-summary "AI+半导体领涨..."] \
  [--macro-summary "中美关税缓和..."] \
  [--analysis '{"core_conflict":"核心矛盾...","events":[{"date":"5月15日","event":"事件","impact":"影响"}],"advice":["建议1","建议2"]}'] \
  -o /tmp/scores.json
```

**计算规则**（脚本自动处理）：
- 技术面得分：从 `technical.json` 的 `summary.total_score` 自动提取
- **自动基线评分**：当 Agent 未显式传递 `--*-score` 时，脚本从管线数据文件判断：
  - `--fundamental-data`：PE/PB 百分位 <30 → 看多 +1，>70 → 看空 -1；利润增速 >10% → +1；ROE >15% → +1
  - `--macro-data`：HS300 涨幅 >1% → +1，< -1% → -1；PMI ≥50 → +1；人民币升值 → +1
  - `--capital-flow-data`：北向持仓增加 → +1
- **Agent 显式传递的 `--*-score` 始终覆盖自动基线评分**
- 权重计算：默认 技术面35% / 资金面25% / 基本面15% / 情绪面15% / 宏观面10%
- `--focus` 调整权重：`technical`→技术55%, `capital_flow`→资金50%, `fundamental`→基本45%, `sentiment`→情绪45%
- 数据质量自动调整权重：insufficient→技术17.5%, limited→技术25%
- 趋势判定：≥ +2.0 看多，≤ -2.0 看空，其他震荡
- 置信度：评分绝对值 ≥ 2.5 且一致性 ≥ 0.7 → 高；≥ 2.0 且一致性 ≥ 0.5 → 中；其他 → 低
- 风险项自动从 `key_signals` 提取（自动去重同主题风险）
- ETF/HK/ST 特殊标记自动生成
- **维度摘要**（`--*-summary`）：每个非技术面维度的1-2句分析摘要，展示在报告"关键信号"表。摘要来自 Step 3 WebSearch 结果
- **综合研判**（`--analysis`）：结构化 JSON，包含 `core_conflict`（核心矛盾）、`events`（关键事件数组）、`advice`（操作建议数组），展示在报告"七、综合研判"

输出 `/tmp/scores.json` 包含综合评分、方向、置信度、风险项、维度摘要、综合研判、报告参数等全部字段。

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
  --chart /tmp/chart_fragment.html \
  --ts-code 159740.SZ --stock-name '恒生科技ETF大成' \
  --output-md reports/159740.SZ/20260514-2200.md \
  --output-html reports/159740.SZ/20260514-2200.html
```

`--pipeline` 自动填充 `--kline`、`--technical`、`--etf-data`、`--capital-flow` 参数；
`--scores-file` 自动填充 `--scores`、`--direction`、`--score`、`--confidence`、`--risks`、`--special`、维度摘要（`--*-summary`）、综合研判（`--analysis`）参数。

**方式二（手动传参）**：参考 [references/troubleshooting.md](references/troubleshooting.md) 的第 4 节。

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