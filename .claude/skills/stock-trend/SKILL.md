---
name: stock-trend
description: 对 A股、港股、ETF 执行日趋势判断，输出结构化报告
triggers:
  - /stock-trend
  - /etf-scan
  - /longtou
  - /market-theme
argument-hint: "<code> [--focus <维度>] [--horizon <周期>] [--multi-timeframe] [--compact] [--no-data]"
allowed-tools:
  - Read
  - Write
  - Bash(python3 .claude/skills/stock-trend/scripts/resolve_code.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/run_pipeline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/compute_scores.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/etf_scanner.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/analyze_technical.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_etf_data.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_futures_data.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_capital_flow.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_fundamental.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_macro_snapshot.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/generate_chart_html.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/generate_report.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/portfolio_manager.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/backtest_engine.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/market_leader.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_sector_data.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/analyze_market_theme.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_sector_kline.py *)
  - WebSearch
  - WebFetch
  - mcp__web-search__bing_search
  - mcp__web-search__crawl_webpage
  - Bash(open *)
  - Bash(open -a "Google Chrome" *)
---

# 股票趋势判断 Skill

> **分支选择**：根据触发命令执行对应流程。
> - 触发 `/etf-scan` → 仅执行「/etf-scan 流程」，**跳过**其余所有流程
> - 触发 `/longtou` → 仅执行「/longtou 流程」，**跳过**其余所有流程
> - 触发 `/market-theme` → 仅执行「/market-theme 流程」，**跳过**其余所有流程
> - 触发 `/stock-trend` → 从「Step 1: 解析输入」开始执行

---

## /etf-scan [--top N] [--focus <板块>] [--output compact|full]

> ⚠️ 当触发 `/etf-scan` 时，只执行本节的 3 个步骤，**不得**执行下方 `/stock-trend` 的 Step 1-4。

扫描精选 ETF 池，输出当日趋势排名和投资建议。

参数：
- `--top N`    深度分析 ETF 数量，默认 10
- `--focus <板块>`  只扫描指定板块：宽基指数、科技、金融、消费医药、制造周期、商品跨境
- `--output compact|full`  输出简版/完整版，默认 full

### /etf-scan 执行步骤

1. **运行扫描脚本**

```bash
python3 .claude/skills/stock-trend/scripts/etf_scanner.py [--top N] [--focus <板块>] [--output compact|full] --output-html
```

脚本输出 JSON 到 stdout，包含 `meta`、`combined_ranking`、`top_picks`、`excluded`、`sector_summary` 字段。
使用 `--output-html` 时同时生成 HTML 报告到 `reports/lists/YYYY-MM-DD-HH-mm.html`。

2. **解析 JSON 并呈现结果**

   完整模式：扫描概览（数量/耗时）→ 综合排名表（排名/代码/名称/速评分/深度分/信号/推荐）→ Top 3 投资逻辑 → 低分排除 → 板块强弱摘要。
   简略模式 (`--compact`)：Top 5 排名表 + Top 1-2 逻辑 + 排除摘要。

3. **补充说明**

- 如果 `deep_score` 为 null（深度分析跳过），仅展示速评分，标注"深度分析跳过"
- 信号方向映射: ≥ +2.0 → ↑↑(看多), +0.5~+2.0 → ↑(偏多), -0.5~+0.5 → →(震荡), < -0.5 → ↓(偏空)
- 推荐星级: 综合分 ≥ 80 → ★★★, ≥ 65 → ★★☆, ≥ 50 → ★☆☆
- 所有输出必须附带免责声明: 本报告仅供学习参考，不构成任何投资建议

---

## /portfolio [list|add|remove|update|status|alerts|kelly] [options]

> 管理 ETF 持仓，追踪浮动盈亏、止损预警、凯利仓位分析、与 etf-scan 排名对比。

参数：
- 无参数或 `list` — 列出全部持仓（含实时盈亏）
- `add --code <代码> --price <买入价> --date <买入日期> --qty <数量> [--name <名称>] [--stop-loss <止损>] [--targets <目标1,目标2>] [--notes <备注>]` — 新增持仓
- `remove --code <代码> [--close-price <平仓价>]` — 平仓标记
- `update --code <代码> [--stop-loss <止损价>] [--targets <目标1,目标2>]` — 更新止损/目标
- `status [--skip-scan]` — 持仓总览 + 预警 + 凯利分析 + etf-scan 对比
- `alerts` — 仅输出预警信息
- `kelly` — 凯利公式仓位分析（对比当前 vs 最优比例）

### /portfolio 执行步骤

1. **执行持仓脚本**
```bash
python3 .claude/skills/stock-trend/scripts/portfolio_manager.py <command> [options]
```

2. **解析 JSON 并呈现结果**

根据命令类型呈现：
- `list/status`: 表格展示持仓 + 盈亏 + 预警
- `add/remove/update`: 操作确认信息
- `alerts`: 预警列表，critical 级别高亮

3. **持仓 vs etf-scan 对比**（仅 `status` 命令）
运行 etf-scan Phase 1，对比持仓 ETF 在当前扫描中的排名变化：
- 仍在 top_pick (评分 ≥ 70): 建议继续持有
- 评分 55-70: 关注变化
- 评分 < 55 或排名靠后: 建议关注/减仓

---

## /etf-backtest [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20] [--etf <代码>]

> 回测验证 ETF Phase 1 速评分模型的预测能力。

参数：
- `--lookback-days N`    回测天数，默认 120（约半年交易日）
- `--focus <板块>`       只回测指定板块
- `--top-n N`            每日取 top N ETF，默认 10
- `--eval-windows`       评估窗口（天），默认 5,10,20
- `--etf <代码>`         只回测单只 ETF
- `--sample-interval N`  采样间隔，默认 5

### /etf-backtest 执行步骤

1. **运行回测脚本**
```bash
python3 .claude/skills/stock-trend/scripts/backtest_engine.py [--lookback-days N] [--focus <板块>] [--top-n N] [--eval-windows 5,10,20]
```

2. **解析 JSON 并呈现结果**

   输出包含：回测区间/采样日期/测试ETF
   → IC表（窗口/IC均值/标准差/t值/正向比率/说明）
   → 命中率、Top vs Bottom 收益差、Top10 平均收益
   → 解读：IC>0.05 且 5%显著=有预测力；命中率>55%=优于随机

---

## /longtou [--top N] [--sector <板块名>] [--compact]

> ⚠️ 当触发 `/longtou` 时，只执行本节的 3 个阶段，**不得**执行 `/stock-trend` 的 Step 1-4 或 `/etf-scan` 流程。

扫描全市场热点板块，识别板块内的龙头股和中军股，结合 pipeline 深度分析输出投资建议。

参数：
- `--top N`    热点板块数量，默认 10
- `--sector <板块名>`  只分析指定板块（如"半导体"），跳过板块扫描
- `--compact`  精简输出（跳过 HTML 生成）
- `--no-html`  跳过 HTML 报告生成

### /longtou 执行步骤

1. **运行扫描脚本**

```bash
python3 .claude/skills/stock-trend/scripts/market_leader.py [--top N] [--sector <板块名>] [--compact] --output-html
```

脚本执行三阶段：
- Phase 1（板块扫描）：通过东方财富 API 获取全市场板块排行，按综合评分（涨幅40%+主力资金30%+涨跌比30%）排序
- Phase 2（龙头中军筛选）：对每个热点板块获取成分股，按条件筛选
  - 龙头筛选：当日涨幅(50%) + 成交额(30%) + 排行位置(20%)
  - 中军筛选：市值(40%) + PE合理性(40%) + 走势稳定性(20%)
- Phase 3（深度分析）：对候选标的调 run_pipeline + compute_scores，获取综合评分

默认生成 HTML 报告到 `reports/lists/longtou-{时间}.html`（`--compact` 或 `--no-html` 跳过）。

2. **打开 HTML 报告**

```bash
open -a "Google Chrome" reports/lists/longtou-{时间}.html
```

> 用 Chrome 打开 HTML 报告。跳过条件：`--compact` 或 `--no-html`。

3. **解析输出并呈现结果**

   脚本输出结构化 JSON + Markdown 报告。

   **完整模式**：扫描概览（板块数/热点/标/耗时）→ 按板块展开（名称/热度/涨幅）
   → 每板块下龙头（个股/涨跌幅/方向/星级/止损/目标）和中军（市值/PE/方向/星级）。

   **简略模式**（`--compact`）：同上但省略止损/目标/PE/市值等细节。

4. **补充分析**（可选）

对 `best_picks` 中 Top 3 标的，可补充搜索消息面信息（政策/行业新闻）增强研判：

```bash
WebSearch("北方华创 半导体设备 2026年5月 政策")
```

5. **综合研判**

基于 pipeline 评分和搜索信息，给出最终建议。信号方向映射同 `/stock-trend`（≥ +2.0 看多，≤ -2.0 看空）。

---

## /market-theme [--top N] [--days N] [--min-score N]

> ⚠️ 当触发 `/market-theme` 时，只执行本节的 3 个阶段，**不得**执行 `/stock-trend`、`/etf-scan` 或 `/longtou` 流程。

扫描全市场板块，结合板块指数 K 线分析过去 N 个交易日的持续性和趋势强度，识别当前市场主线。

参数：
- `--top N`    扫描板块数量，默认 15
- `--days N`   回溯交易日数（分析周期），默认 10
- `--min-score N`  最低持续性分，默认 30
- `--no-html`  跳过 HTML 报告生成

### /market-theme 执行步骤

1. **运行分析脚本**

```bash
python3 .claude/skills/stock-trend/scripts/analyze_market_theme.py [--top 15] [--days 10] [--min-score 30] [--output-html]
```

脚本执行三阶段：
- Phase 1（板块扫描）：通过东方财富 API 获取全市场板块排行，取 Top N
- Phase 2（K 线获取）：对 Top N 板块抓取 BK 指数 K 线（复权），分析周期可配
- Phase 3（持续性分析）：计算每板块的 5 日/10 日涨幅、上涨天数比、近期加速度、波动率 → 综合持续性分

**持续性评分权重**：5 日涨幅 30% + 上涨天数比 25% + 10 日涨幅 20% + 加速度 15% + 稳定性 10%

默认生成 HTML 报告到 `reports/lists/market-theme-{时间}.html`（`--no-html` 跳过）。

2. **打开 HTML 报告**

```bash
open -a "Google Chrome" reports/lists/market-theme-{时间}.html
```

> 使用 `--no-html` 时跳过（无 HTML 文件）。

3. **解析输出并呈现结果**

脚本输出 结构化 JSON + Markdown 报告：

**完整模式**：分析概览（板块数/周期/耗时）→ 板块持续性排名表 → 按分类展开
- **阶段强势（主线确认）**：持续性分 ≥ 70，完整表格（板块/今日涨幅/5 日涨/10 日涨/上涨比/持续性分/趋势）
- **稳步上行（候选主线）**：持续性分 50-70，精简表格
- **新兴主题**：持续性分 40-50，新冒头方向
- **脉冲热点**：今日热度高但持续性不足 50，需警惕追高
- **退潮板块**：持续性分 < 40，正在降温

4. **补充分析**（可选）

对 `strong` 中的 Top 3 主线板块，搜索政策/消息面验证：

```bash
WebSearch("{板块名} {YYYY}年{M}月 政策 行业 新闻")
```

4. **综合研判**

基于持续性分析和搜索信息，给出主线判断与操作建议。

---

<!-- 以下 Steps 1-4 仅适用于 /stock-trend 命令，/etf-scan 触发时不得执行 -->

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

## Step 2: 数据管线 + 四维搜索（并发执行）

> **关键说明**：本步骤包含两步并发操作：(A) 数据管线一键执行 与 (B) 四维并行搜索。两者同时启动，无交叉依赖。

### A. 数据管线一键执行

使用 `--code` 模式一键完成全部数据获取和技术分析：

```bash
python3 .claude/skills/stock-trend/scripts/run_pipeline.py --code <code>
```

内部自动执行：
1. 自动解析标的代码（resolve_code.py）
2. 获取K线数据（Tushare → 东方财富自动降级）
3. 技术分析（analyze_technical.py）
4. ETF数据获取（标的为ETF时）
5. 资金流向获取（含北向/融资融券/龙虎榜）
6. 基本面数据获取（AKShare，ETF跳过）
7. 宏观数据快照（汇率/利率/PMI/CPI/M2）
8. 指数期货数据获取（标的为ETF时，获取对应指数期货的基差、持仓量、成交量信号）

输出到 `.cache/stock-trend/{code}/` 目录：
- `pipeline_output.json` — 管线汇总（包含数据源、记录数、耗时等元信息）
- `kline.json` — K线数据
- `technical.json` — 技术分析结果
- `etf_data.json` — ETF数据（仅ETF标的）
- `futures_data.json` — 指数期货数据（仅ETF标的，含基差、持仓量趋势、成交量确认信号）
- `capital_flow.json` — 资金流向（含 data_extended 增强数据）
- `fundamental.json` — 基本面数据（AKShare，非ETF）
- `macro_snapshot.json` — 宏观快照（AKShare）

**缓存机制**：各数据自动缓存到 `.cache/stock-trend/`。盘中TTL 5分钟，盘后TTL 16小时（宏观数据盘中4h/盘后12h，基本面盘中30min/盘后16h）。同一标的同日重复分析命中缓存可跳过 API 调用。`--no-cache` 强制刷新。

**超时处理**：每步骤超时30s，超时维度标记为timeout，其余继续执行。

管线完成后可选生成K线图用于报告嵌入：

```bash
python3 .claude/skills/stock-trend/scripts/generate_chart_html.py .cache/stock-trend/{code}/kline.json --technical .cache/stock-trend/{code}/technical.json --chip-distribution .cache/stock-trend/{code}/chip_distribution.json -o .cache/stock-trend/{code}/chart_fragment.html
```

使用 `--no-data` 时跳过本步骤。管线失败时各脚本支持 `-h` 查看独立传参。

### B. 四维并行搜索

启动上文 A 数据管线后，**立即**按以下四个维度**并行**搜索（使用同时调用的工具），无需等待管线完成。

#### 并行搜索指令

使用**一次调用多个搜索工具**同时搜索四个维度：

| 维度 | 权重 | 自动化基线 | 搜索关键词 | 反向验证词 |
|------|------|-----------|-----------|-----------|
| 资金面 | 25% | `data_extended.northbound/margin` + `futures_data.json`（基差+OI信号） | `"{stock_name} {ts_code} 资金流向 北向资金 {YYYY}年{M}月"` | `"流出 危机 减持"` |
| 基本面 | 15% | `fundamental.json`（PE/PB/ROE/财务数据已获取） | `"{stock_name} {ts_code} 估值 业绩 {YYYY}年{M}月"` | `"风险 下滑 亏损"` |
| 情绪面 | 15% | 无自动化 | `"{stock_name} 涨跌停 换手率 板块 {YYYY}年{M}月"` | `"下跌 跌停 恐慌"` |
| 宏观面 | 10% | `macro_snapshot.json`（汇率/利率/PMI已获取） | `"今日宏观 政策 利率 汇率 外盘 {YYYY}年{M}月"` | `"鹰派 衰退 收紧"` |

> **年份替换**：`{YYYY}` 和 `{M}` 取自系统当前日期（见 CLAUDE.md `# currentDate`），严禁使用训练截止年份或上一年度。例如当前2026年5月则填"2026年5月"，勿写"2025年5月"。

**四个搜索并行执行，无交叉依赖**。

#### 自动化数据基线

部分维度的自动化数据已在管线中获取（通过 AKShare），Agent 的使用方式：

- **基本面**：读取 `.cache/stock-trend/{code}/fundamental.json` 获取 PE/PB 百分位、营收/利润增速、ROE。Agent 可据此直接撰写摘要，无需重复搜索基础数据。数据质量 `good`/`partial` 时可用。
- **资金面**：`.cache/stock-trend/{code}/capital_flow.json` 的 `data_extended` 包含北向资金持仓变动和融资融券数据
- **宏观面**：`.cache/stock-trend/{code}/macro_snapshot.json` 提供汇率、PMI、CPI、利率等快照

**Agent 评分始终覆盖自动化评分** — 以 Agent 判断为准。

#### 维度摘要与综合研判

为每个非技术面维度撰写**简明摘要**（1-2句话），含利多利空标记和关键数据，例如：
- 资金面：`利多：ETF近20日净申购+1.75亿元；利空：主力交易资金近2日净流出1.59亿元`
- 基本面：`利多：恒生科技PE 22.9倍处历史32%分位偏低；利空：盈利增速仅3-4%偏弱`
- 情绪面：`利多：AI+半导体领涨；利空：指数冲高回落，缩量调整中`
- 宏观面：`利多：中美关税缓和；利空：美联储鹰派维持高利率`

**正反强制**：每个维度必须同时包含利多和利空。只有单向信号时标注"未找到反向信号，可能存在确认偏差"。
摘要格式：`利多：xxx；利空：xxx`（分号分隔，便于程序解析）。

摘要通过 Step 3 的 `--*-summary` 传入。同时撰写**综合研判**（核心矛盾、关键事件、操作建议），通过 `--analysis` 传入。

### C. 数据质量检查

检查 `.cache/stock-trend/{code}/technical.json` 的 `summary.data_quality`：
- `"insufficient"`（数据<30条）：技术面权重降至17.5%
- `"limited"`（数据30-59条）：技术面权重降至25%

### D. 等待汇合

等待管线完成且所有搜索结果收集完毕，进入 Step 3。
- 管线超时（单步30s，见A节超时处理）时对应维度按 0 分处理，标注"管线超时"
- 管线和搜索完成即进入 Step 3

**搜索工具选择**：
1. **`WebSearch`**：首选，用于宏观政策、行业新闻等
2. **`mcp__web-search__bing_search` + `mcp__web-search__crawl_webpage`**：中文财经内容
3. **`Bash(curl)`**：东方财富API等需自定义Header的场景

**禁止使用 `WebFetch`** 访问：`*.eastmoney.com`、`cn.investing.com`、`xueqiu.com`、`10jqka.com.cn`

**技术面内部子权重**（脚本自动应用）：趋势指标(MA/MACD)×1.5、趋势强度(ADX)×1.2、震荡指标(RSI/KDJ)×0.8、通道/量能(布林带/成交量/OBV)×1.0、K线形态×0.5。一致性因子：同向指标数/总指标数影响置信度。

### E. 逆向校验

> 对每个非技术维度做逆向审视，防止确认偏差导致评分偏颇。

对每个非技术维度（资金面/基本面/情绪面/宏观面）执行：

1. [ ] 该维度是否覆盖≥2个必检项？
   - 资金面：主力净流入(P0)、北向/南向资金(P1)、IOPV折溢价(P0·ETF)
   - 基本面：PE/PB估值分位(P0)、盈利增速(P0)、NAV/跟踪误差(P0·ETF)
   - 情绪面：涨跌停/板块联动(P0)、新闻舆情(P0)
   - 宏观面：货币政策(P0)、外盘影响(P1)
2. [ ] 该维度是否同时包含利好和利空信号？
3. [ ] 单一事件贡献是否超过封顶值？（宏观1.5，其他1.0）
4. [ ] 逆向审视：假设当前打分方向错误，最强的反向论据是什么？
5. [ ] 反向论据是否已在摘要中体现？

**未通过项的处理**：
- 缺必检项 → 覆盖折减：维度得分×0.5
- 缺反向信号 → 一致性因子×0.6
- 超封顶 → 自动修正得分至封顶值
- 逆向审视调整得分 → 向0靠近0.5-1.0

**自检结果传入 Step 3**：通过 `--self-check` 参数传入，格式：
```json
{
  "capital_flow": {"counter_found": true, "adjusted": false, "covered_items": 3},
  "fundamental":  {"counter_found": true, "adjusted": false, "covered_items": 2},
  "sentiment":    {"counter_found": false, "adjusted": true, "original": 1.0, "revised": 0.5, "covered_items": 2},
  "macro":        {"counter_found": true, "adjusted": false, "covered_items": 3}
}
```

## Step 3: 计算综合评分

使用 `--code` 模式简化调用：

```bash
python3 .claude/skills/stock-trend/scripts/compute_scores.py --code <code> \
  --capital-flow-score <资金面得分> \
  --fundamental-score <基本面得分> \
  --sentiment-score <情绪面得分> \
  --macro-score <宏观面得分> \
  [--focus <维度>] \
  [--asset-type etf|hk|st|stock] \
  [--self-check '...'] [--signals-info '...'] [--risks '...'] \
  [--capital-summary "..."] [--fundamental-summary "..."] \
  [--sentiment-summary "..."] [--macro-summary "..."] \
  [--analysis '...']
```

评分脚本自动从 `.cache/stock-trend/{code}/` 读取 `technical.json` 和各维度数据文件。`--*-score` 仍为手动传入。

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
- **维度摘要**（`--*-summary`）：每个非技术面维度的1-2句分析摘要，展示在报告"关键信号"表。摘要来自 Step 2 WebSearch 结果
- **综合研判**（`--analysis`）：结构化 JSON，包含 `core_conflict`（核心矛盾）、`events`（关键事件数组）、`advice`（操作建议数组），展示在报告"七、综合研判"。events 元素支持两种字段格式：`{date, event, impact}` 或 `{name, detail, impact}`

输出 `.cache/stock-trend/{code}/scores.json` 包含综合评分、方向、置信度、风险项、维度摘要、综合研判、报告参数等全部字段。

### 判定趋势与置信度

阈值由 `compute_scores.py` 判定：≥+2.0 看多 / ≤-2.0 看空 / 中间震荡。
置信度：评分绝对值≥2.5 高、≥2.0 中、<2.0 低。
多周期共振（`--multi-timeframe`）：日周同向提一级，反向降一级。

## Step 4: 风险管理 + 生成报告

### 风险管理与监控信号

基于 `analyze_technical.py` 的 `summary` 输出：止损位、三级目标（保守/主目标/激进）、R:R比、仓位建议、最大回撤。监控信号按趋势方向自动生成（跌破止损离场、突破目标分批止盈等）。

### 特殊标的的处理

`compute_scores.py` 的 `build_special_section` 自动处理：
- **ST/*ST**：基本面强制 -1
- **港股**：恒指联动、卖空占比、AH溢价
- **ETF**：IOPV折溢价、跟踪误差、基差/OI/期货量
- **可转债**：转股溢价率、强赎风险

### 生成报告

推荐使用 `--code` 模式：

```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py --code <code> \
  --ts-code <ts_code> --stock-name '<名称>' \
  --output-md reports/<ts_code>/<YYYYMMDD-HHmm>.md \
  --output-html reports/<ts_code>/<YYYYMMDD-HHmm>.html
```

`--code` 自动从 `.cache/stock-trend/{code}/` 读取管线数据和评分结果。手动传参见 `generate_report.py -h`。

**精简模式** (`--compact`)：

```
{股票名称}({代码}) {趋势符号}{趋势方向} | 评分{综合评分} | 技术{技术面得分} 资金{资金面得分} 基本{基本面得分} 情绪{情绪面得分} 宏观{宏观面得分}
风险: {关键风险1}; {关键风险2}
支撑/压力: {支撑位}/{压力位}
入场时机: {入场建议} — {确认信号}
```

**保存路径**：

| 格式 | 路径 |
|---|---|
| Markdown | `reports/{ts_code}/{YYYYMMDD-HHmm}.md` |
| HTML | `reports/{ts_code}/{YYYYMMDD-HHmm}.html` |

`ts_code` 使用 Step 1 解析的 Tushare 代码格式（含市场后缀，如 159740.SZ、600519.SH、00700.HK）

精简模式仅输出文本，不保存 HTML。

### 自动打开 HTML 报告

**默认模式**下，生成报告后自动在浏览器中打开 HTML 文件：

```bash
open reports/{ts_code}/{YYYYMMDD-HHmm}.html
```

**精简模式** (`--compact`) 跳过本步骤（无 HTML 文件生成）。

## 免责声明

所有输出必须附带：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。

## 参考文件

- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)
