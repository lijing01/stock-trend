# Stock-Trend Skill 优化设计文档

> 日期: 2026-05-15
> 状态: 设计稿

## 1. 动机

当前 `/stock-trend` 流程串行执行，非技术面维度（基本面/资金面/情绪面/宏观面）完全依赖 Agent 手动 WebSearch，Token 消耗大且耗时长。优化目标是：

1. **并行化** — 无明显依赖的执行步骤改为并发，缩短端到端耗时
2. **新增数据源** — 利用 AKShare（已安装）自动化非技术面数据获取，减少 WebSearch 依赖，提升分析质量

## 2. 范围

### 不做的
- 不减少 SKILL.md 步骤数量（保持 10 步结构）
- 不替换已有数据源（Tushare/东方财富/BaoStock 保持主数据源）
- 不引入新外部依赖（AKShare 已安装，无需额外 pip install）
- 不改动现有脚本的输入/输出接口（向后兼容）

### 要做的

| # | 变更 | 类型 |
|---|------|------|
| 1 | `fetch_fundamental.py` — 新增基本面自动化脚本 | 新增 |
| 2 | `fetch_macro_snapshot.py` — 新增宏观快照脚本 | 新增 |
| 3 | `fetch_capital_flow.py` — 增强资金面（北向/融资融券/龙虎榜） | 修改 |
| 4 | `run_pipeline.py` — 集成新脚本到并行池 | 修改 |
| 5 | `compute_scores.py` — 支持从数据文件自动评分 | 修改 |
| 6 | `generate_report.py` — 支持图表嵌入和新数据源 | 修改 |
| 7 | `report-template.html` / `report-template.md` — 新增占位符 | 修改 |
| 8 | `SKILL.md` — 重构为并发分发流程 | 修改 |

不涉及：
- `analyze_technical.py`（无变化）
- `fetch_kline.py` / `fetch_kline_eastmoney.py`（无变化）
- `resolve_code.py`（无变化）
- `generate_chart_html.py`（无变化，仅被调用方调用）
- `diagnose.py` / `test_stock_trend.py`（无变化）

## 3. 架构变更

### 3.1 执行流变化

```
当前 (串行):
Step 2: run_pipeline.py ──→ Step 3: WebSearch 资金 → 基本 → 情绪 → 宏观
                                        ↓
                               Step 4: compute_scores.py → Step 8: generate_report

优化后 (并行):
Step 2 & 3:  管线启动 ──→ ├── Branch A: run_pipeline.py (含新脚本)
                           │   └── 管线完成 → generate_chart_html.py（可选）
                           ├── Branch B: 4维并行 WebSearch
                           ↓
              等待两分支完成
                           ↓
Step 4: compute_scores.py（读取技术面+自动数据+Agent评分）
Step 5-7: 不变
Step 8: generate_report.py（嵌K线图）
Step 9-10: 不变
```

### 3.2 新增脚本接口

#### `fetch_fundamental.py`

```bash
python3 fetch_fundamental.py <ts_code> [--asset E|FD] [-o output.json]
```

- 输入：ts_code（如 `600519.SH`）
- 输出：`fundamental.json`
- 数据源：AKShare（`stock_individual_info_em`, `stock_financial_analysis_indicator` 等）
- 跳过条件：`asset=FD`（ETF 无基本面分析意义）
- 失败隔离：每条数据独立 try/except，部分失败仍返回有效数据

#### `fetch_macro_snapshot.py`

```bash
python3 fetch_macro_snapshot.py [-o output.json] [--focus rate|forex|index]
```

- 输入：无（市场级数据，不依赖标的）
- 输出：`macro_snapshot.json`
- 数据源：AKShare（汇率、利率、PMI、CPI、M2、美债收益率等）
- 缓存：同一次 `/stock-trend` 调用内只取一次，多个标的不重复请求

### 3.3 数据流

```
run_pipeline.py (ThreadPoolExecutor max_workers=4)
├── fetch_kline ──→ analyze_technical ──→ /tmp/technical.json
├── fetch_etf_data.py ──→ /tmp/etf_data.json            (仅 ETF)
├── fetch_capital_flow.py(增强) ──→ /tmp/capital_flow.json
├── fetch_fundamental.py ──→ /tmp/fundamental.json       (新增)
└── fetch_macro_snapshot.py ──→ /tmp/macro_snapshot.json (新增)

完成后（可选）:
└── generate_chart_html.py ──→ /tmp/chart_fragment.html

compute_scores.py 读取:
├── /tmp/technical.json        (已有)
├── /tmp/fundamental.json      (新增 → 自动基础评分)
├── /tmp/macro_snapshot.json   (新增 → 自动基础评分)
├── /tmp/capital_flow.json     (增强 → 自动基础评分)
└── Agent 命令行参数           (不变，可覆盖自动化评分)

generate_report.py 读取:
├── /tmp/scores.json           (已有)
├── /tmp/technical.json        (已有)
├── /tmp/kline.json            (已有)
├── /tmp/etf_data.json         (已有)
├── /tmp/capital_flow.json     (已有)
├── /tmp/fundamental.json      (新增)
├── /tmp/macro_snapshot.json   (新增)
└── /tmp/chart_fragment.html   (新增 → 嵌入HTML报告)
```

## 4. 详细设计

### 4.1 新增：`fetch_fundamental.py`

#### 命令行接口

```python
parser.add_argument("ts_code", help="股票代码，如 600519.SH")
parser.add_argument("--asset", default="E", choices=["E", "FD"])
parser.add_argument("-o", "--output", default="/tmp/fundamental.json")
```

#### 输出 JSON 结构

```json
{
  "meta": {
    "ts_code": "600519.SH",
    "data_source": "akshare",
    "fetch_time": "20260515-210000",
    "asset": "E"
  },
  "summary": {
    "data_quality": "good" | "partial" | "error",
    "pe_ttm": 25.3,
    "pb": 8.1,
    "pe_percentile_3y": 45.2,
    "pb_percentile_3y": 38.7,
    "market_cap_billion": 2100.5,
    "industry": "白酒",
    "roe": 18.5,
    "eps": 42.15,
    "revenue_growth_pct": 12.3,
    "profit_growth_pct": 15.1,
    "dividend_yield_pct": 2.1,
    "debt_ratio": 22.5
  },
  "data": {},
  "errors": []
}
```

#### AKShare API 映射

| 数据点 | AKShare 函数 | 说明 |
|--------|-------------|------|
| PE/PB/市值/行业 | `stock_individual_info_em(symbol)` | 基础信息 |
| PE/PB 三年百分位 | `stock_zh_valuation_baidu(symbol, indicator, period)` | 需传具体指标名 |
| 财务指标(ROE/EPS) | `stock_financial_analysis_indicator(symbol, year)` | 年报数据 |
| 营收/利润增速 | `stock_yjbb_em(date)` | 季报数据 |
| 股息率 | 从 PE 倒数估算或 `stock_individual_info_em` | — |
| 资产负债率 | `stock_financial_analysis_indicator(symbol, year)` | — |
| 港股 | `stock_hk_financial_indicator_em(symbol)` | 港股专用 |
| 港股 PE/PB | `stock_hk_valuation_baidu(symbol, indicator)` | 港股专用 |

#### 错误处理

- 每个 AKShare 调用包装在独立 try/except 中
- 失败时该数据点返回 `null`，`errors[]` 追加描述
- `data_quality` 根据缺失率判定：无缺失=good, <30%缺失=partial, >=30%=error
- 港股分支和 A 股分支互不干扰

### 4.2 新增：`fetch_macro_snapshot.py`

#### 命令行接口

```python
parser.add_argument("-o", "--output", default="/tmp/macro_snapshot.json")
parser.add_argument("--focus", nargs="*", choices=["rate", "forex", "index", "policy"])
```

#### 输出 JSON 结构

```json
{
  "meta": {
    "data_source": "akshare",
    "fetch_time": "20260515-210000"
  },
  "summary": {
    "data_quality": "good",
    "usd_cny": 7.12,
    "usd_cny_change_pct": -0.15,
    "china_10y_yield": 1.76,
    "us_10y_yield": 4.47,
    "shibor_1w": 1.30,
    "lpr_1y": 3.10,
    "lpr_5y": 3.60,
    "pmi": 50.8,
    "cpi_yoy": 0.3,
    "m2_growth_pct": 7.4,
    "reserve_ratio": 9.5,
    "loan_rate": 3.45,
    "hs300_close": 4135.39,
    "hs300_change_pct": -1.02
  },
  "data": {},
  "errors": []
}
```

#### AKShare API 映射

| 数据点 | AKShare 函数 | 说明 |
|--------|-------------|------|
| USD/CNY 中间价 | `currency_boc_sina("美元")` | 中国银行牌价 |
| 中美 10Y 国债 | `bond_zh_us_rate()` | 需要点时间 |
| SHIBOR | `macro_china_shibor_all()` | 上海银行间拆借 |
| LPR | `macro_china_lpr()` | 贷款市场报价利率 |
| PMI | `macro_china_pmi()` | 采购经理人指数 |
| CPI | `macro_china_cpi()` | 消费者物价指数 |
| M2 | `macro_china_money_supply()` | 货币供应量 |
| 存准率 | `macro_china_reserve_requirement_ratio()` | 准备金率 |
| 存贷款基准利率 | `macro_bank_china_interest_rate()` | — |
| HS300 行情 | `stock_zh_index_hist("000300", "daily", ...)` | 最近一日 |

#### 缓存策略

管线执行期内，`/tmp/macro_snapshot.json` 只生成一次。后续步骤检测文件存在即跳过。

### 4.3 修改：`fetch_capital_flow.py`

在现有输出结构中增加三个可选节：

```json
{
  "meta": { /* 不变 */ },
  "data": [ /* 已有主力资金流向数据 */ ],
  "data_source_northbound": {
    "market": [ /* 北向资金日数据 */ ],
    "individual": [ /* 个股北向持仓 */ ]
  },
  "data_source_margin": {
    "detail": [ /* 融资融券明细 */ ]
  },
  "data_source_longhubang": [
    /* 龙虎榜明细 */
  ],
  "warnings": []
}
```

#### 新增 AKShare API

| 数据点 | AKShare 函数 | 适用场景 |
|--------|-------------|---------|
| 北向资金流向历史 | `stock_hsgt_hist_em("北向资金")` | A 股看多信号 |
| 个股北向持仓 | `stock_hsgt_individual_em(code)` | 持仓变动追踪 |
| 沪市融资融券 | `stock_margin_detail_sse(date)` | 上交所标的 |
| 深市融资融券 | `stock_margin_detail_szse(date)` | 深交所标的 |
| 龙虎榜 | `stock_lhb_detail_em(date)` | 异常波动标的 |

#### 向后兼容

- 新增字段均为 `data_source_*` 命名空间，不覆盖原有 data/meta
- 不读取新字段的消费者（如旧版 generate_report）完全不受影响
- 所有新增数据点独立 try/except，失败仅对应字段为 null

### 4.4 管线集成：`run_pipeline.py` 变更

```python
# 扩大并行池
with ThreadPoolExecutor(max_workers=4) as executor:
    # 已有任务
    tasks.append((fetch_kline_cmd, "fetch_kline", kline_path))
    tasks.append((fetch_etf_cmd, "fetch_etf", etf_path))
    tasks.append((fetch_capital_cmd, "fetch_capital", capital_path))
    # 新增任务（ETF 跳过基本面）
    if asset != "FD":
        tasks.append((fetch_fundamental_cmd, "fetch_fundamental", fundamental_path))
    tasks.append((fetch_macro_cmd, "fetch_macro", macro_path))
```

#### 新增 CLI 参数

```python
parser.add_argument("--no-fundamental", action="store_true")
parser.add_argument("--no-macro", action="store_true")
```

#### pipeline_output.json 新增

```json
{
  /* 已有字段 */
  "output_files": {
    "kline": "/tmp/kline.json",
    "technical": "/tmp/technical.json",
    "etf_data": "/tmp/etf_data.json",
    "capital_flow": "/tmp/capital_flow.json",
    "fundamental": "/tmp/fundamental.json",
    "macro_snapshot": "/tmp/macro_snapshot.json"
  }
}
```

### 4.5 评分集成：`compute_scores.py` 变更

```python
# 新增 CLI
parser.add_argument("--fundamental-data", help="基本面数据文件")
parser.add_argument("--macro-data", help="宏观快照数据文件")
```

#### 自动评分逻辑

```
基本面自动评分 (fundamental 生效时):
  if pe_percentile_3y < 30 且 profit_growth > 0 => 基础分 +1
  if pe_percentile_3y > 70 且 profit_growth < 0 => 基础分 -1
  else => 基础分 0
  
宏观看多信号:
  if us_10y_yield 下行且 cpi 温和 => 加分 +0.5
  if pmi < 50 且 m2 收缩 => 减分 -0.5

资金面自动评分 (capital_flow 增强后):
  if 北向资金连续净流入 => 加分 +1
  if 融资余额上升 => 加分 +0.5
```

**重要**：自动评分仅作为 Agent 手动评分的**基线参考**。Agent 仍可通过命令行参数覆盖。当数据不可用时，自动评分跳过，不报错。

### 4.6 报告集成：`generate_report.py` 变更

#### 新增 CLI 参数

```python
parser.add_argument("--chart", help="K线图 HTML 片段路径")
parser.add_argument("--fundamental-data", help="基本面数据文件")
parser.add_argument("--macro-data", help="宏观快照数据文件")
```

#### build_context() 新增

```python
# 图表
context["has_chart"] = args.chart and os.path.exists(args.chart)

# 基本面数据摘要
if args.fundamental_data:
    fund = load_json(args.fundamental_data, {})
    summary = fund.get("summary", {})
    context["pe_ttm"] = summary.get("pe_ttm", "—")
    context["pb"] = summary.get("pb", "—")
    context["pe_percentile"] = summary.get("pe_percentile_3y", "—")
    context["dividend_yield"] = summary.get("dividend_yield_pct", "—")
    context["revenue_growth"] = summary.get("revenue_growth_pct", "—")
    context["profit_growth"] = summary.get("profit_growth_pct", "—")
    context["data_fundamental"] = summary.get("data_quality") in ("good", "partial")

# 宏观快照摘要
if args.macro_data:
    macro = load_json(args.macro_data, {})
    ms = macro.get("summary", {})
    context["usd_cny"] = ms.get("usd_cny", "—")
    context["macro_pmi"] = ms.get("pmi", "—")
    context["macro_data_avail"] = ms.get("data_quality") in ("good", "partial")
```

#### HTML 报告嵌入图表

```
在 <footer> 之前插入 chart_fragment.html 内容
```

#### Markdown 报告增加数据源脚注

```
---
*数据来源: 技术面=Tushare/东方财富 基本面=AKShare 资金面=东方财富+AKShare 宏观=AKShare*
```

### 4.7 SKILL.md 重构

#### 步骤变化

| 当前 | 优化后 | 说明 |
|------|--------|------|
| Step 1 | Step 1 | 不变 |
| Step 2 | Step 2 | 启动数据管线（不变，内部新增并行任务） |
| Step 3 | Step 3 | 执行并行搜索（4 维改为并行而非串行） |
| — | **注** | Step 2 和 Step 3 在**执行上并发**，步骤编号独立 |
| Step 4 | Step 4 | 汇合点（等待两分支完成） |
| Step 5-7 | Step 5-7 | 不变 |
| Step 8 | Step 8 | 报告生成（增加 `--chart` 参数） |
| Step 9-10 | Step 9-10 | 不变，但 Step 9 新增打开含图表的 HTML |

#### 并发指令示例

```markdown
## Step 2: 数据管线

Step 1 解析完成后，启动数据管线。**与此同时**，在下一回合直接进入 Step 3 开始搜索，无需等待管线完成。

```bash
python3 .claude/skills/stock-trend/scripts/run_pipeline.py <ts_code> --asset <E|FD> [options]
```
...

管线完成后（可选）生成K线图，在 Step 4 之前完成即可。

## Step 3: 四维并行搜索（与 Step 2 同时执行）

启动管线后，**立即并行**搜索四个维度，使用同时调用的平行工具：

### Branch A: 数据管线（后台执行）

```bash
python3 .claude/skills/stock-trend/scripts/run_pipeline.py <ts_code> --asset <E|FD> [options]
```

管线输出: /tmp/pipeline_output.json, /tmp/kline.json, /tmp/technical.json, /tmp/fundamental.json (新增), /tmp/macro_snapshot.json (新增) 等。

管线完成后（可选）生成K线图:
```bash
python3 .claude/skills/stock-trend/scripts/generate_chart_html.py /tmp/kline.json --technical /tmp/technical.json -o /tmp/chart_fragment.html
```

### Branch B: 四维并行搜索（与管线同时执行）

启动管线后，使用**同时调用**（同一消息内多条搜索）并行搜索四个维度:
1. 资金面: `"{stock_name} {ts_code} 资金流向 北向资金"`
2. 基本面: `"{stock_name} {ts_code} PE PB 估值 业绩"`
3. 情绪面: `"{stock_name} 涨跌停 换手率 板块 走势"`
4. 宏观面: `"今日宏观 政策 利率 汇率 外盘"`

### 等待汇合

等待管线完成且所有搜索结果收集完毕，进入 Step 4。
超出 60 秒管线未完成时，技术面按 0 分并标注"管线超时"。
```

## 5. 风险与应对

| 风险 | 可能性 | 影响 | 应对 |
|------|--------|------|------|
| AKShare API 请求失败 | 中 | 自动化数据缺失 | 独立 try/except，不影响管线其他部分 |
| AKShare 进度条污染 stderr | 高 | Agent 日志变乱 | `akshare.set_verbose(False)` 或抑制 stderr |
| 并行搜索触发频率限制 | 低 | 部分搜索无结果 | 500ms 错开发起，增加重试说明 |
| 港股 AKShare API 不可用 | 中 | 港股基本面缺失 | 港股分支失败降级到无数据模式 |
| 管线超时（60s） | 低 | 技术面评分延迟 | 超时后技术面按 0 分，管线数据在可用时追加 |

## 6. 验证方式

1. **单元测试** — 新脚本独立运行验证 JSON 输出格式
2. **管线集成测试** — `run_pipeline.py` 运行验证新输出文件存在且格式正确
3. **评分验证** — `compute_scores.py` 新增参数验证自动评分逻辑
4. **报告验证** — HTML 报告中确认图表嵌入、数据源脚注
5. **端到端验证** — 完整 `/stock-trend 159740` 流程，对比优化前后输出

## 7. 实施顺序

### Phase 1: 数据源脚本（无流程变更，可独立测试）
1. `fetch_fundamental.py` — 新增
2. `fetch_macro_snapshot.py` — 新增
3. `fetch_capital_flow.py` — 增强

### Phase 2: 管线集成
4. `run_pipeline.py` — 集成新脚本

### Phase 3: 报告集成
5. `report-template.html` / `report-template.md` — 新增占位符
6. `generate_report.py` — 支持图表和数据源

### Phase 4: 评分与流程
7. `compute_scores.py` — 支持自动评分
8. `SKILL.md` — 重构为并发分发

### Phase 5: 验证
9. 测试全部变更
