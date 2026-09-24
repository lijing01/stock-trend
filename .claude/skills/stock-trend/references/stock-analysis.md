# 个股 / ETF 趋势分析流程

## /stock-trend 流程 (Step 1-4)

### Step 1: 解析输入

```
/stock-trend <code> [--focus technical|capital_flow|fundamental|sentiment] [--horizon intraday|daily|weekly] [--multi-timeframe] [--compact] [--no-data]
```

- `code`(必填)：股票/ETF代码或名称。`--focus`可叠加。`--horizon`默认daily。`--multi-timeframe`同日获取日/周K线计算周期共振。`--compact`精简输出。`--no-data`跳过K线获取。

**代码解析**：
```bash
python3 .claude/skills/stock-trend/scripts/core/resolve_code.py <name_or_code> -o /tmp/resolve.json
```
支持：6位A股/5位港股/带后缀(600519.SH)/中文名称(恒生科技ETF大成/茅台)。输出 `ts_code`/`asset`/`adj`/`market`/`name`。

### Step 2: 数据管线 + 四维搜索（并发）

#### A. 数据管线

```bash
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --code <code>
```

内部自动：代码解析→K线(Tushare→东方财富降级)→技术分析→ETF数据(ETF标的)→资金流向(北向/融资/龙虎榜)→基本面(AKShare,ETF跳过)→宏观快照(汇率/利率/PMI/CPI/M2)→期货数据(ETF标的:基差/OI/量)。

输出到 `.cache/stock-trend/{code}/`：`pipeline_output.json`/`kline.json`/`technical.json`/`etf_data.json`/`futures_data.json`/`capital_flow.json`/`fundamental.json`/`macro_snapshot.json`/`wyckoff.json`。

**缓存TTL**：盘中5min/盘后16h(宏观:盘中4h/盘后12h,基本面:盘中30min/盘后16h)。`--no-cache`强制刷新。**超时**：每步30s，超时维度标记timeout。

可选K线图：
```bash
python3 .claude/skills/stock-trend/scripts/reporting/chart.py .cache/stock-trend/{code}/kline.json --technical .cache/stock-trend/{code}/technical.json --chip-distribution .cache/stock-trend/{code}/chip_distribution.json -o .cache/stock-trend/{code}/chart_fragment.html
```

#### B. 四维并行搜索

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

**搜索工具**：使用当前环境可用的联网搜索/网页工具，优先官方公告、交易所、监管机构和数据源；脚本内置数据源作为结构化行情的降级路径。不要直接抓取限制自动访问或结果不稳定的网页（包括 `*.eastmoney.com`、`cn.investing.com`、`xueqiu.com`、`10jqka.com.cn`），需要东方财富数据时优先运行已有 fetcher。

#### C. 数据质量

检查 `technical.json` 的 `summary.data_quality`：`insufficient`(<30条)→技术面权重17.5%，`limited`(30-59条)→25%。

#### D. 等待汇合

管线完成+搜索收齐→Step 3。管线超时维度按0分，"管线超时"标注。

#### E. 逆向校验

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

### Step 3: 综合评分

```bash
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --code <code> \
  --capital-flow-score <N> --fundamental-score <N> --sentiment-score <N> --macro-score <N> \
  [--focus <维度>] [--asset-type etf|hk|st|stock] \
  [--self-check '...'] [--signals-info '...'] [--risks '...'] \
  [--capital-summary "..."] [--fundamental-summary "..."] [--sentiment-summary "..."] [--macro-summary "..."] \
  [--analysis '...']
```

脚本自动从 `.cache/stock-trend/{code}/` 读取 `technical.json` 及各维度数据。

**`--mode wyckoff`(P0-3 独立维科夫 100 分制)**:不进复合分,单跑维科夫出 0-100 分。分数 = 阶段分70%(`wyckoff_score` [-3,+3]→[0,100],quality limited×0.9/insufficient→50) + VSA20%(信号强度均值) + 置信度10%。输出含买点判定(吸筹/拉升阶段买点子阶段)与 verdict,写 `wyckoff_score.json`。
```bash
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --mode wyckoff --code <code>
# 或直接给文件
python3 .claude/skills/stock-trend/scripts/analysis/scores.py --mode wyckoff --wyckoff-data <wyckoff.json> -o /tmp/wyckoff_score.json
```

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

### Step 4: 风险管理 + 生成报告

**大盘/板块对比**(report.py 自动): 若 `.cache/stock-trend/market_regime.json` 存在(今日复盘生成),报告含「📊 大盘/板块对比」段 — 市场评分背景、个股 vs 沪深300 相对强弱、所属板块位置。缺失时提示先跑 `/daily-review`。

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
  --output-md reports/stocks/<ts_code>/<YYYYMMDD-HHmm>.md \
  --output-html reports/stocks/<ts_code>/<YYYYMMDD-HHmm>.html
```

**精简模式**(`--compact`)：
```
{名称}({代码}) {趋势符号}{方向} | 评分{总分} | 技术{技术分} 资金{资金分} 基本{基本分} 情绪{情绪分} 宏观{宏观分}
风险: {风险1}; {风险2}
支撑/压力: {支撑位}/{压力位}
入场时机: {入场建议} — {确认信号}
```
精简模式仅文本输出，不保存HTML。

**保存路径**：MD→`reports/stocks/{ts_code}/{YYYYMMDD-HHmm}.md`，HTML→`reports/stocks/{ts_code}/{YYYYMMDD-HHmm}.html`。`ts_code`用Tushare格式(含后缀,如159740.SZ)。

**默认模式**生成 HTML 后返回文件路径。只有用户明确要求查看时才打开该文件；`compact` 跳过 HTML。
