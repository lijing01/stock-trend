---
name: stock-trend
description: A股/港股/ETF日趋势判断，输出结构化报告
triggers:
  - /stock-trend
  - /etf-scan
  - /portfolio
  - /etf-backtest
  - /longtou
  - /market-theme
  - /ths-theme
  - /integrated-scan
argument-hint: "<code> [--focus <维度>] [--horizon <周期>] [--multi-timeframe] [--compact] [--no-data]"
allowed-tools:
  - Read
  - Write
  - Bash(python3 .claude/skills/stock-trend/scripts/core/resolve_code.py *)           # 代码→ts_code解析
  - Bash(python3 .claude/skills/stock-trend/scripts/pipeline/runner.py *)             # 一键数据管线
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/scores.py *)             # 综合评分
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/kline.py *)              # K线(Tushare)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py *)    # K线(东方财富降级)
  - Bash(python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py *)           # ETF扫描
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/technical.py *)          # 技术分析
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/etf_data.py *)           # ETF净值/IOPV/规模
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/futures_data.py *)       # 期货基差/OI/量
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/capital_flow.py *)       # 资金流向/北向/融资
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/fundamental.py *)        # PE/PB/ROE/增速
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/macro_snapshot.py *)     # 汇率/利率/PMI/CPI/M2
  - Bash(python3 .claude/skills/stock-trend/scripts/reporting/chart.py *)             # K线图HTML
  - Bash(python3 .claude/skills/stock-trend/scripts/reporting/report.py *)            # MD+HTML报告
  - Bash(python3 .claude/skills/stock-trend/scripts/portfolio/manager.py *)           # 持仓管理
  - Bash(python3 .claude/skills/stock-trend/scripts/backtesting/engine.py *)          # 回测引擎
  - Bash(python3 .claude/skills/stock-trend/scripts/scans/market_leader.py *)         # 龙头中军扫描
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/sector_data.py *)        # 板块排行/成分股
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/market_theme.py *)       # 市场主线分析
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/sector_kline.py *)       # BK指数K线
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/zt_replay.py *)           # 涨停复盘(同花顺)
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/ths_theme.py *)           # 涨停热力主题(同花顺)
  - WebSearch
  - WebFetch
  - mcp__web-search__bing_search
  - mcp__web-search__crawl_webpage
  - Bash(open *)
  - Bash(open -a "Google Chrome" *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetchers/longhubang_agg.py *)  # 龙虎榜板块聚合
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py *)  # 龙虎榜信号跟踪
  - Bash(python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py *)  # 周主线报告
  - Bash(python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py *)  # 整合扫描
---

# 股票趋势判断

**分支路由**：`/etf-scan`→ETF扫描；`/longtou`→龙头；`/market-theme`→主线；`/ths-theme`→涨停热力；`/etf-backtest`→回测；`/lhb-tracker`→暗线跟踪；`/weekly`→周主线；`/stock-trend`→下方Step 1-4。各流程独立。

---

## /lhb-tracker [--history N] [--report] [--html] [--snapshot-only]

龙虎榜机构信号跟踪系统 — 每日记录机构净买板块快照，验证后续 3/5/10 日表现。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py [--history 30] [--report] [--html] [--snapshot-only]
```

2. 每日自动保存快照 → 历史数据积累后验证信号有效性
3. `--report` 生成 MD 报告到 `reports/lists/`
4. `--html` 生成 HTML 报告（含 Plotly 交互式信号收益/胜率图 + 信号明细表）
5. 信号验证：胜率 > 60% 视为有效信号；买入/卖出分开统计

---

## /weekly [--weeks N] [--html] [--json]

周主线报告 — 聚合一周数据，识别适合中线持仓（1-6个月）的主线方向。

**评分公式**：周均热度(30%) + 上榜频率(25%) + 最新热度(25%) + 趋势(10%) + LHB验证(10%)

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py [--weeks 1] [--html] [--json]
```

2. 数据来源：市场持续性快照 + 龙虎榜快照 + 今日行业热力
3. 分类：🔥中期主线(≥65) / 👀关注方向(45-64) / ❄️退潮(<30)
4. 需要积累至少3天市场持续性数据才有效

---

## /ths-theme [--top N] [--min-score N] [--json] [--no-zt] [--no-lhb]

基于 AKShare 同花顺数据，对行业/概念板块做热力评分。**默认同时执行涨停概念评分 + 龙虎榜分析**，`--no-zt` / `--no-lhb` 可跳过。

涨停概念热度评分（默认开启）：
1. 拉取东方财富涨停板池（`stock_zt_pool_em`）
2. 按概念聚合涨停数据，计算涨停分（涨停数30%+连板25%+早盘20%+封单15%-炸板10%）
3. 与行业热力交叉匹配 → 识别双引擎确认（涨停+行业共振）和独立涨停方向
4. 报告追加🚀涨停概念热度章节

`--zt-date YYYY-MM-DD` 指定涨停日期（默认今日）。

评分公式：涨跌幅(35%) + 主力净流入(35%) + 上涨比率(30%)

龙虎榜机构板块聚合分析（默认开启）：
1. 拉取东方财富龙虎榜机构买卖明细（`stock_lhb_jgmmtj_em`）
2. 通过板块映射表（BK 分类）将上榜股票按板块聚合
3. 计算龙虎榜板块评分（机构净买额40% + 上榜家数25% + 机构参与度20% + 净买一致性15%）
4. 报告追加🏛️龙虎榜机构板块聚合章节（含净买入/净卖出 Top 3 详情）

`--lhb-date YYYYMMDD` 指定龙虎榜日期（默认最近交易日）。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/ths_theme.py [--top N] [--min-score N] [--json] [--no-zt] [--no-lhb] [--lhb-date YYYYMMDD]
```

2. 呈现：概览（涨跌比/平均涨跌/总净流入）→ 强势板块(≥70) → 活跃板块(50-69) → 涨停概念热度 → 龙虎榜板块聚合 → 概念驱动事件 → 资金流向极端 → 弱势板块

3. 数据来源：同花顺行业实时排行（AKShare）+ 东方财富涨停池 + 东方财富龙虎榜

4. `--json` 输出结构化 JSON 给 Agent 消费。

---

## /integrated-scan [--top N] [--compact] [--output-html] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]

ths-theme + longtou 整合扫描 — 先跑板块热力筛选，再对热板块做龙头扫描，输出整合报告。

**顺序 pipeline**：
1. `ths_theme.py --export-sectors` — 全市场板块热力 + 涨停概念评分
2. 筛选 heat_score≥50 & zt_score≥50 的板块
3. `market_leader.py --sectors-from qualified_sectors.json` — 只扫热板块
4. 拼接为整合报告

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_integrated.py [--top 10] [--compact] [--output-html] [--lhb-date YYYYMMDD] [--zt-date YYYY-MM-DD]
```

2. 呈现：总览（热力板块数、龙头标数、市场情绪）→ 按板块展开（板块热力指标 → 龙头清单 → 综合信号标签）

3. 边界情况：
   - 无热板块：不跑 longtou，只输出 ths-theme 热力报告 + 提示无强信号
   - 映射找不到东方财富板块：标注"东方财富无对应板块"，保留方向参考
   - ths-theme/longtou 任一失败：降级为另一项的输出，不阻塞

---

## /etf-scan [--top N] [--focus <板块>] [--output compact|full]

扫描精选ETF池，输出趋势排名。`--focus`: 宽基指数/科技/金融/消费医药/制造周期/商品跨境。

**步骤**：

1. 运行扫描：
```bash
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py [--top N] [--focus <板块>] [--output compact|full] --output-html
```
输出JSON(stdout)含 `meta`/`combined_ranking`/`top_picks`/`excluded`/`sector_summary`。`--output-html` 生成 `reports/lists/YYYY-MM-DD-HH-mm.html`。

2. 呈现：完整模式→排名表+Top3逻辑+排除+板块强弱。`--compact`→Top5表+Top1-2逻辑+排除摘要。

3. 信号映射：≥+2.0→↑↑看多，+0.5~+2.0→↑偏多，-0.5~+0.5→→震荡，<-0.5→↓偏空。星级：≥80→★★★，≥65→★★☆，≥50→★☆☆。`deep_score`=null时标注"深度分析跳过"。必须附带免责声明。

---

## /portfolio [list|add|remove|update|status|alerts|kelly] [options]

持仓管理：浮动盈亏、止损预警、凯利分析、etf-scan对比。

| 命令 | 说明 |
|------|------|
| (无)/list | 全部持仓+实时盈亏 |
| add | `--code --price --date --qty [--name] [--stop-loss] [--targets] [--notes]` |
| remove | `--code <代码> [--close-price]` 平仓标记 |
| update | `--code [--stop-loss] [--targets]` |
| status | 持仓总览+预警+凯利+etf-scan对比。`--skip-scan`跳过扫描 |
| alerts | 仅预警，critical高亮 |
| kelly | 凯利仓位对比当前vs最优 |

**步骤**：

1. 执行：
```bash
python3 .claude/skills/stock-trend/scripts/portfolio/manager.py <command> [options]
```

2. 呈现结果（表格/确认/预警）。

3. `status`时运行etf-scan Phase 1对比：≥70→继续持有，55-70→关注，<55→建议减仓。

---

## /etf-backtest [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20] [--etf <代码>]

回测ETF Phase 1速评分模型预测力。默认120天/top10/窗口5,10,20/间隔5。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/backtesting/engine.py [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20]
```

2. 呈现：回测区间→IC表(窗口/均值/标准差/t值/正向比)→命中率、Top vs Bottom收益差、Top10平均收益。IC>0.05且5%显著=有预测力；命中率>55%=优于随机。

---

## /longtou [--top N] [--sector <板块名>] [--sectors-from <file>] [--compact]

扫描热点板块→识别龙头/中军→pipeline深度分析。`--top`默认10，`--sector`指定板块跳过扫描。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/scans/market_leader.py [--top N] [--sector <板块名>] [--compact] --output-html
```
三阶段：Phase 1板块扫描(涨幅40%+主力资金30%+涨跌比30%)→Phase 2龙头筛选(涨幅50%+成交额30%+排行20%)/中军筛选(市值40%+PE合理性40%+走势稳定性20%)→Phase 3 pipeline深度分析。

2. 非compact时打开HTML：`open -a "Google Chrome" reports/lists/longtou-{时间}.html`

3. 呈现：扫描概览→按板块展开(龙头:个股/涨跌幅/方向/星级/止损/目标；中军:市值/PE/方向/星级)。compact省略止损/目标/PE/市值。

4. 可选：对Top3搜索消息面 `WebSearch("{个股} {板块} {YYYY}年{M}月 政策")`

5. 综合研判，信号映射同/stock-trend。

---

## /market-theme [--top N] [--days N] [--min-score N]

扫描板块+BK指数K线→持续性/趋势强度分析→识别市场主线。默认top15/10天/min-score 30。

**步骤**：

1. 运行：
```bash
python3 .claude/skills/stock-trend/scripts/analysis/market_theme.py [--top 15] [--days 10] [--min-score 30] [--output-html]
```
三阶段：板块扫描(实时排行API)→快照历史加载→持续性分析(上榜率30%+平均热度20%+排名趋势20%+今日热度15%+上涨率趋势15%)。HTML输出到 `reports/lists/market-theme-{时间}.html`。

2. 非--no-html时打开：`open -a "Google Chrome" reports/lists/market-theme-{时间}.html`

3. 呈现：持续性排名表→分类展开：
   - **阶段强势**(≥70)：完整表(板块/今日涨幅/5日涨/10日涨/上涨比/持续性分/趋势)
   - **稳步上行**(50-70)：精简表
   - **新兴主题**(40-50)：新冒头方向
   - **脉冲热点**：今日热但持续<50，警惕追高
   - **退潮板块**(<40)：降温中

4. 可选：对strong Top3搜索 `WebSearch("{板块名} {YYYY}年{M}月 政策 行业 新闻")`

5. 综合研判。

---

# /stock-trend 流程 (Step 1-4)

## Step 1: 解析输入

```
/stock-trend <code> [--focus technical|capital_flow|fundamental|sentiment] [--horizon intraday|daily|weekly] [--multi-timeframe] [--compact] [--no-data]
```

- `code`(必填)：股票/ETF代码或名称。`--focus`可叠加。`--horizon`默认daily。`--multi-timeframe`同日获取日/周K线计算周期共振。`--compact`精简输出。`--no-data`跳过K线获取。

**代码解析**：
```bash
python3 .claude/skills/stock-trend/scripts/core/resolve_code.py <name_or_code> -o /tmp/resolve.json
```
支持：6位A股/5位港股/带后缀(600519.SH)/中文名称(恒生科技ETF大成/茅台)。输出 `ts_code`/`asset`/`adj`/`market`/`name`。

## Step 2: 数据管线 + 四维搜索（并发）

### A. 数据管线

```bash
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --code <code>
```

内部自动：代码解析→K线(Tushare→东方财富降级)→技术分析→ETF数据(ETF标的)→资金流向(北向/融资/龙虎榜)→基本面(AKShare,ETF跳过)→宏观快照(汇率/利率/PMI/CPI/M2)→期货数据(ETF标的:基差/OI/量)。

输出到 `.cache/stock-trend/{code}/`：`pipeline_output.json`/`kline.json`/`technical.json`/`etf_data.json`/`futures_data.json`/`capital_flow.json`/`fundamental.json`/`macro_snapshot.json`。

**缓存TTL**：盘中5min/盘后16h(宏观:盘中4h/盘后12h,基本面:盘中30min/盘后16h)。`--no-cache`强制刷新。**超时**：每步30s，超时维度标记timeout。

可选K线图：
```bash
python3 .claude/skills/stock-trend/scripts/reporting/chart.py .cache/stock-trend/{code}/kline.json --technical .cache/stock-trend/{code}/technical.json --chip-distribution .cache/stock-trend/{code}/chip_distribution.json -o .cache/stock-trend/{code}/chart_fragment.html
```

### B. 四维并行搜索

管线启动后**立即**四维并行搜索（一次调用多个搜索工具）：

| 维度 | 权重 | 自动化基线 | 搜索词 | 反向验证词 |
|------|------|-----------|--------|-----------|
| 资金面 | 25% | `capital_flow.json`(北向/融资)+`futures_data.json`(基差/OI) | `"{name} {code} 资金流向 北向资金 {YYYY}年{M}月"` | `"流出 危机 减持"` |
| 基本面 | 15% | `fundamental.json`(PE/PB/ROE/增速) | `"{name} {code} 估值 业绩 {YYYY}年{M}月"` | `"风险 下滑 亏损"` |
| 情绪面 | 15% | 无自动化 | `"{name} 涨跌停 换手率 板块 {YYYY}年{M}月"` | `"下跌 跌停 恐慌"` |
| 宏观面 | 10% | `macro_snapshot.json`(汇率/利率/PMI) | `"今日宏观 政策 利率 汇率 外盘 {YYYY}年{M}月"` | `"鹰派 衰退 收紧"` |

> `{YYYY}`和`{M}`取系统当前日期(见CLAUDE.md `# currentDate`)，严禁使用训练截止年份。

**自动化基线使用**：基本面读PE/PB百分位/增速/ROE；资金面读北向/融资数据；宏观面读汇率/PMI/CPI/利率。数据质量`good`/`partial`时可用。Agent评分始终覆盖自动化评分。

**维度摘要**(每个非技术维度1-2句，利多+利空双方向)：
- 格式：`利多：xxx；利空：xxx`(分号分隔)。只有单向时标注"未找到反向信号，可能存在确认偏差"。
- 资金面例：`利多：ETF近20日净申购+1.75亿元；利空：主力近2日净流出1.59亿元`

摘要通过`--*-summary`传入Step 3。**综合研判**(核心矛盾/关键事件/操作建议)通过`--analysis`传入。

**搜索工具**：`WebSearch`(首选宏观政策)→`mcp__web-search__bing_search`+`crawl_webpage`(中文财经)→`Bash(curl)`(东方财富API)。**禁止WebFetch访问**：`*.eastmoney.com`/`cn.investing.com`/`xueqiu.com`/`10jqka.com.cn`。

### C. 数据质量

检查 `technical.json` 的 `summary.data_quality`：`insufficient`(<30条)→技术面权重17.5%，`limited`(30-59条)→25%。

### D. 等待汇合

管线完成+搜索收齐→Step 3。管线超时维度按0分，"管线超时"标注。

### E. 逆向校验

每非技术维度检查：
1. 覆盖≥2个必检项？
   - 资金面：主力净流入(P0)、北向/南向(P1)、IOPV折溢价(P0·ETF)
   - 基本面：PE/PB估值分位(P0)、盈利增速(P0)、NAV/跟踪误差(P0·ETF)
   - 情绪面：涨跌停/板块联动(P0)、新闻舆情(P0)
   - 宏观面：货币政策(P0)、外盘影响(P1)
2. 同时含利好+利空？
3. 单一事件≤封顶值(宏观1.5/其他1.0)？
4. 逆向审视：假设方向错误，最强反向论据？
5. 反向论据已在摘要中？

**未通过处理**：缺必检项→维度分×0.5；缺反向信号→一致性因子×0.6；超封顶→修正至封顶值；逆向调整→向0靠近0.5-1.0。

自检结果通过`--self-check`传入Step 3，JSON格式：
```json
{"capital_flow":{"counter_found":true,"adjusted":false,"covered_items":3},"fundamental":{"counter_found":true,"adjusted":false,"covered_items":2},"sentiment":{"counter_found":false,"adjusted":true,"original":1.0,"revised":0.5,"covered_items":2},"macro":{"counter_found":true,"adjusted":false,"covered_items":3}}
```

## Step 3: 综合评分

```bash
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --code <code> \
  --capital-flow-score <N> --fundamental-score <N> --sentiment-score <N> --macro-score <N> \
  [--focus <维度>] [--asset-type etf|hk|st|stock] \
  [--self-check '...'] [--signals-info '...'] [--risks '...'] \
  [--capital-summary "..."] [--fundamental-summary "..."] [--sentiment-summary "..."] [--macro-summary "..."] \
  [--analysis '...']
```

脚本自动从 `.cache/stock-trend/{code}/` 读取 `technical.json` 及各维度数据。

**评分规则**：
- 技术面：从 `technical.json` 的 `summary.total_score` 自动提取
- 技术面子权重(脚本自动)：趋势(MA/MACD)×1.5、ADX×1.2、震荡(RSI/KDJ)×0.8、通道量能(布林/成交量/OBV)×1.0、K线形态×0.5。一致性因子=同向指标数/总指标数。
- 自动基线(AKShare数据,Agent未传score时生效)：PE/PB百分位<30→+1,>70→-1；利润增速>10%→+1；ROE>15%→+1；HS300涨>1%→+1,<-1%→-1；PMI≥50→+1；人民币升值→+1；北向增持→+1
- Agent显式`--*-score`**始终覆盖**自动基线
- 默认权重：技术35%/资金25%/基本15%/情绪15%/宏观10%
- `--focus`权重调整：technical→技术55%, capital_flow→资金50%, fundamental→基本45%, sentiment→情绪45%
- 数据质量调整：insufficient→技术17.5%, limited→25%
- **趋势判定**：≥+2.0看多，≤-2.0看空，其余震荡
- **置信度**：|score|≥2.5且一致性≥0.7→高；≥2.0且一致性≥0.5→中；其余→低
- 多周期共振(`--multi-timeframe`)：日周同向提一级，反向降一级
- 风险项从`key_signals`自动提取+去重
- ETF/HK/ST特殊标记自动生成
- `--analysis`结构化JSON：`{core_conflict, events:[{date,event,impact}|{name,detail,impact}], advice:[...]}`

输出 `.cache/stock-trend/{code}/scores.json`。

## Step 4: 风险管理 + 生成报告

**风险管理**：基于`technical.json` summary→止损位、三级目标(保守/主目标/激进)、R:R比、仓位建议、最大回撤。监控信号按趋势方向生成。

**特殊标的**(脚本自动)：
- ST/*ST：基本面强制-1
- 港股：恒指联动、卖空占比、AH溢价
- ETF：IOPV折溢价、跟踪误差、基差/OI/期货量
- 可转债：转股溢价率、强赎风险

**生成报告**：
```bash
python3 .claude/skills/stock-trend/scripts/reporting/report.py --code <code> \
  --ts-code <ts_code> --stock-name '<名称>' \
  --output-md reports/<ts_code>/<YYYYMMDD-HHmm>.md \
  --output-html reports/<ts_code>/<YYYYMMDD-HHmm>.html
```

**精简模式**(`--compact`)：
```
{名称}({代码}) {趋势符号}{方向} | 评分{总分} | 技术{技术分} 资金{资金分} 基本{基本分} 情绪{情绪分} 宏观{宏观分}
风险: {风险1}; {风险2}
支撑/压力: {支撑位}/{压力位}
入场时机: {入场建议} — {确认信号}
```
精简模式仅文本输出，不保存HTML。

**保存路径**：MD→`reports/{ts_code}/{YYYYMMDD-HHmm}.md`，HTML→`reports/{ts_code}/{YYYYMMDD-HHmm}.html`。`ts_code`用Tushare格式(含后缀,如159740.SZ)。

**默认模式**生成后自动打开HTML：`open reports/{ts_code}/{YYYYMMDD-HHmm}.html`。compact跳过。

## 免责声明

所有输出必须附带：**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**

## 参考文件

- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)
