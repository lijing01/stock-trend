---
name: stock-trend
description: 对 A股、港股、ETF 执行日趋势判断，输出结构化报告
triggers:
  - /stock-trend
argument-hint: "<code> [--focus <维度>] [--horizon <周期>] [--multi-timeframe] [--compact] [--no-data]"
allowed-tools:
  - Read
  - Write
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/analyze_technical.py *)
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

- `code`（必填）：股票/ETF 代码。缺失时提示用法
- `--focus`（可选，可叠加）：`technical | capital_flow | fundamental | sentiment`
- `--horizon`（可选，默认 `daily`）：`intraday | daily | weekly`
- `--multi-timeframe`：多周期共振模式，同时获取日/周K线并计算周期共振得分
- `--compact`：精简输出模式
- `--no-data`：跳过K线数据获取

代码格式校验：6位A股(如600519)、5位港股(如00700)、带后缀(如600519.SH)。不匹配时报错。

## Step 2: 识别市场与标的

| 代码模式 | 市场 | Tushare ts_code | asset | 复权 |
|---|---|---|---|---|
| `6xxxxx` / `6xxxxx.SH` | 上交所 A股 | {code}.SH | E | qfq |
| `0xxxxx` / `0xxxxx.SZ` | 深交所 A股 | {code}.SZ | E | qfq |
| `3xxxxx` / `3xxxxx.SZ` | 创业板 | {code}.SZ | E | qfq |
| `688xxx` / `688xxx.SH` | 科创板 | {code}.SH | E | qfq |
| `5xxxxx` | 上交所 ETF | {code}.SH | FD | qfq |
| `15xxxx` | 深交所 ETF | {code}.SZ | FD | qfq |
| `0xxxx` (5位) / `0xxxx.HK` | 港股 | {code}.HK | E | none |

特殊标记：ST/*ST → 退市风险警示；港股 → 无涨跌停；ETF → IOPV 折溢价

## Step 3: 获取K线数据

```bash
# 1. 先尝试 Tushare
python3 .claude/skills/stock-trend/scripts/fetch_kline.py <ts_code> --asset <E|FD> --freq <D|W> --adj <qfq|none> -o /tmp/kline.json
# 2. Tushare 失败时降级东方财富（不支持港股）
#    脚本内部自动轮换节点：push2his → 38.push2his → 48.push2his
#    东方财富全节点失败时自动降级 BaoStock
python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py <ts_code> --asset <E|FD> --freq <D|W> -o /tmp/kline.json
# 3. 技术分析
python3 .claude/skills/stock-trend/scripts/analyze_technical.py /tmp/kline.json -o /tmp/technical.json
```

**数据源降级链**：Tushare → 东方财富(增强头+节点轮换) → BaoStock → 无数据模式

判断 Tushare 是否失败：检查 JSON 的 `meta.data_source`，为 `error` 则降级。三个数据源均失败时，技术面按 0 分处理并标注"无数据源"。数据不足 60 条时标注。

使用 `--no-data` 时跳过本步骤。

**多周期共振模式** (`--multi-timeframe`)：
除获取日线数据外，额外获取周线数据（`--freq W`），输出到 `/tmp/kline_weekly.json` 和 `/tmp/technical_weekly.json`。在 Step 5 中对比日线与周线趋势方向，计算周期共振得分。

Tushare Token 配置优先级：命令行 `--token` > 环境变量 `TUSHARE_TOKEN` > `.claude/tushare-config.json`。未配置时自动降级东方财富。

## Step 4: 五维分析

每个维度评分范围 **-3 ~ +3**，详细评分标准见 **references/trend-dimensions.md**。

1. **技术面** — 使用 `analyze_technical.py` 输出：`latest.*.signal` 判断各指标信号（含MA/MACD/RSI/KDJ/布林带/ADX/OBV），`patterns` 判断K线形态（详见 **references/kline-patterns.md**），`summary.total_score` 作为基础得分
2. **资金面** — 主力流入/北向资金/融资融券/龙虎榜
3. **基本面** — PE-PB/业绩增速/行业景气/股息率
4. **情绪面** — 涨跌停/换手率/板块联动/舆情
5. **宏观面** — 货币政策/行业政策/外盘/汇率

### 非技术面数据获取方式

技术面数据由脚本自动获取。非技术面（资金面/基本面/情绪面/宏观面）数据需从外部来源获取，按以下优先级：

1. **`mcp__web-search__bing_search` + `mcp__web-search__crawl_webpage`**：首选方式，可抓取中文财经网站内容
2. **`WebSearch`**：用于搜索宏观政策、行业新闻等公开信息
3. **`Bash(curl)`**：用于东方财富API等需要自定义Header的场景，示例：
   ```bash
   # ETF基金信息（净值、IOPV折溢价等）
   curl -s "http://fund.eastmoney.com/pingzhongdata/{code}.js" -H "Referer: http://fund.eastmoney.com/" | head -50
   # 资金流向
   curl -s "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=0.{code}&fields1=f1,f2,f3,f7&klt=101&lmt=5" -H "Referer: https://quote.eastmoney.com/"
   ```

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

## Step 5: 计算综合评分

```
综合评分 = Σ(维度得分 × 维度权重)
```

**默认权重**：技术面 35%、资金面 25%、基本面 15%、情绪面 15%、宏观面 10%

**`--focus` 权重调整**：

| focus | 调整后权重 |
|---|---|
| `technical` | 技术 55%, 资金 20%, 其他均分 |
| `capital_flow` | 资金 50%, 技术 20%, 其他均分 |
| `fundamental` | 基本 45%, 宏观 20%, 其他均分 |
| `sentiment` | 情绪 45%, 技术 25%, 其他均分 |

多 focus 叠加时权重平均合并。

## Step 6: 判定趋势与置信度

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

## Step 7: 风险管理与监控信号

基于 `analyze_technical.py` 的 `summary` 输出生成：

**止损位**：`summary.stop_loss`（支撑位 - 1×ATR，或当前价 - 2×ATR）
**目标价位**：`summary.target`（最近压力位）
**风险收益比**：`summary.risk_reward_ratio`（R:R ≥ 2:1 值得入场）
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

## Step 9: 特殊标的处理

- **ST/*ST**：基本面强制 -1；标题行标注退市风险
- **港股**：增恒指联动、卖空占比、南向资金、AH溢价；标注无涨跌停
- **ETF**：增 IOPV 折溢价、跟踪误差、申赎、成交额分析
- **可转债**：增转股溢价率、纯债价值、强赎风险

## Step 10: 生成报告

**默认模式**：使用 **assets/report-template.md** 和 **assets/report-template.html** 生成完整报告

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

`ts_code` 使用 Step 2 识别的 Tushare 代码格式（含市场后缀，如 159740.SZ、600519.SH、00700.HK）

精简模式仅输出文本，不保存 HTML。

## Step 11: 自动打开 HTML 报告

**默认模式**下，生成报告后自动在浏览器中打开 HTML 文件：

```bash
open reports/{ts_code}/{YYYYMMDD-HHmm}.html
```

**精简模式** (`--compact`) 跳过本步骤（无 HTML 文件生成）。

`open` 命令使用系统默认浏览器打开 HTML 文件。路径与 Step 8 保存路径一致。

## Step 12: 免责声明

所有输出必须附带：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。

## 参考文件

- 维度评分标准: [references/trend-dimensions.md](references/trend-dimensions.md)
- K线形态参考: [references/kline-patterns.md](references/kline-patterns.md)
- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)