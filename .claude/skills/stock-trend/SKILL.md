---
name: stock-trend
description: 对 A股、港股、ETF 执行日趋势判断，输出结构化报告
triggers:
  - /stock-trend
argument-hint: "<code> [--focus <维度>] [--horizon <周期>] [--compact] [--no-data]"
allowed-tools:
  - Read
  - Write
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py *)
  - Bash(python3 .claude/skills/stock-trend/scripts/analyze_technical.py *)
  - WebSearch
  - WebFetch
---

# 股票趋势判断 Skill

## 工作流

### Step 1: 解析输入

解析用户输入的股票代码和参数：

```
/stock-trend <code> [--focus <维度>] [--horizon <周期>] [--compact] [--no-data]
```

- `code`（必填）：股票/ETF 代码
- `--focus`（可选，可叠加）：侧重维度 `technical | capital_flow | fundamental | sentiment`
- `--horizon`（可选，默认 `daily`）：`intraday | daily | weekly`
- `--compact`（可选）：精简输出模式
- `--no-data`（可选）：不获取K线数据，仅基于已有信息分析

**输入验证规则**：

1. `code` 为必填参数，缺失时提示用法：`用法: /stock-trend <code> [--focus <维度>] [--horizon <周期>] [--compact] [--no-data]`
2. 代码格式不匹配任何市场规则时，报错并给出示例：`代码格式无法识别，请使用6位A股代码(如600519)、5位港股代码(如00700)或带后缀格式(如600519.SH)`
3. `--focus` 可叠加使用，如 `--focus technical --focus capital_flow`
4. `--horizon` 仅接受 `intraday | daily | weekly`，其他值报错
5. `--no-data` 跳过 Tushare K线数据获取，技术面维度按0分处理并标注"无数据源"

### Step 2: 识别市场与标的

根据代码模式自动识别市场和标的类型：

| 代码模式 | 市场 | 示例 |
|---|---|---|
| `6xxxxx` / `6xxxxx.SH` | 上交所 A股 | 600519.SH 贵州茅台 |
| `0xxxxx` / `0xxxxx.SZ` | 深交所 A股 | 000001.SZ 平安银行 |
| `3xxxxx` / `3xxxxx.SZ` | 创业板 | 300750.SZ 宁德时代 |
| `688xxx` / `688xxx.SH` | 科创板 | 688981.SH 中芯国际 |
| `5xxxxx` | 上交所 ETF/基金 | 510050 上证50ETF |
| `15xxxx` | 深交所 ETF | 159919 沪深300ETF |
| `0xxxx` / `0xxxx.HK` | 港股 | 00700 腾讯控股 |

**Tushare 代码转换规则**：

| 代码模式 | Tushare ts_code | asset 参数 | 复权方式 |
|---|---|---|---|
| `6xxxxx` | {code}.SH | E | qfq |
| `0xxxxx` / `3xxxxx` | {code}.SZ | E | qfq |
| `688xxx` | {code}.SH | E | qfq |
| `5xxxxx` | {code}.SH | FD | qfq |
| `15xxxx` | {code}.SZ | FD | qfq |
| `0xxxx` (5位) | {code}.HK | E | 无复权 |

带后缀的代码直接使用后缀格式（如 `600519.SH` 直接使用）。

**特殊标的标记**：

- A股代码前标 `ST` 或 `*ST` → 标记退市风险警示
- 港股代码 → 标记无涨跌停限制
- ETF 代码 → 标记需分析 IOPV 折溢价

### Step 3: 获取K线数据

根据 Step 2 识别的市场类型和代码，通过脚本获取K线行情数据。

1. **执行 Tushare 数据获取脚本**：

   ```bash
   python3 .claude/skills/stock-trend/scripts/fetch_kline.py <ts_code> --asset <E|FD> --freq <D|W> --adj <qfq|none> -o /tmp/kline.json
   ```

   参数说明：
   - `ts_code`：根据 Step 2 的转换规则生成（如 600519 → 600519.SH）
   - `asset`：A股=`E`，ETF=`FD`（脚本可自动识别，也可手动指定）
   - `freq`：日线→`D`（默认），周线→`W`
   - `adj`：前复权 `qfq`（默认），港股使用 `none`

2. **检查 Tushare 返回结果**：
   - 读取 JSON 的 `meta.data_source`
   - 若为 `error`，读取 `meta.error` 确定原因
   - **若 Tushare 失败，执行东方财富降级脚本**：

   ```bash
   python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py <ts_code> --asset <E|FD> --freq <D|W> -o /tmp/kline.json
   ```

   东方财富脚本说明：
   - 无需 Token，免费调用
   - 不支持港股（`.HK` 后缀），港股仍依赖 Tushare
   - 参数与 Tushare 脚本保持一致（`ts_code`, `--asset`, `--freq`, `-o`）
   - 输出 JSON 格式与 Tushare 脚本完全一致，可直接供 `analyze_technical.py` 使用

3. **若两个数据源均失败**：
   - 技术面维度按0分处理，报告中标注"无数据源"

4. **检查数据量**：
   - 若 `meta.record_count` < 60，标注数据不足

5. **执行技术分析脚本**：

   ```bash
   python3 .claude/skills/stock-trend/scripts/analyze_technical.py /tmp/kline.json -o /tmp/technical.json
   ```

6. **使用分析结果**：读取 `latest`（各指标信号）、`patterns`（K线形态）、`summary`（技术面汇总）

若使用 `--no-data` 参数，跳过本步骤。

**错误处理**：

| 场景 | 脚本行为 | LLM 后续处理 |
|---|---|---|
| 代码无法识别 | Step 2 已处理 | 不调用脚本 |
| Tushare Token 缺失 | 输出 `meta.data_source: "error"` | 自动降级到东方财富脚本 |
| Tushare SDK 不可用 | 自动降级到 HTTP API | 正常使用数据 |
| Tushare API 限频/网络错误 | 重试1次（间隔2秒） | 仍失败则降级到东方财富 |
| Tushare 积分不足 | 输出 `meta.data_source: "error"` | 自动降级到东方财富脚本 |
| 东方财富不支持港股 | 输出 `meta.data_source: "error"` | 港股仅依赖 Tushare |
| 东方财富网络错误 | 重试1次（间隔2秒） | 仍失败则降级为无数据模式 |
| 两个数据源均失败 | — | 技术面按0分，标注"无数据源" |
| 数据不足(<60条) | 输出数据 + `meta.warnings` | 标注数据缺失对评分的影响 |

### Step 4: 五维分析

按以下五个维度执行趋势分析，详细检查标准见 **references/trend-dimensions.md**：

1. **技术面** (Technical) — MA/MACD/RSI/KDJ/布林带/成交量/K线形态
2. **资金面** (Capital Flow) — 主力流入/北向资金/融资融券/龙虎榜
3. **基本面** (Fundamental) — PE-PB/业绩增速/行业景气/股息率
4. **情绪面** (Sentiment) — 涨跌停/换手率/板块联动/舆情
5. **宏观面** (Macro) — 货币政策/行业政策/外盘/汇率

每个维度评分范围: **-3 ~ +3**

当 Step 3 成功获取K线数据时，技术面分析应：
- 使用 `analyze_technical.py` 输出的 `latest.ma.signal` 判断均线信号
- 使用 `latest.macd.signal`、`latest.rsi.signal`、`latest.kdj.signal`、`latest.bollinger.signal` 判断指标信号
- 使用 `latest.volume.signal` 判断量价配合
- 使用 `patterns` 数组中的K线形态结果（详细识别标准见 references/kline-patterns.md）
- 使用 `summary.total_score` 作为技术面维度基础得分，结合其他信号综合判定

### Step 5: 计算综合评分

```
综合评分 = Σ(维度得分 × 维度权重)
```

**默认权重**：

| 维度 | 权重 |
|---|---|
| 技术面 | 35% |
| 资金面 | 25% |
| 基本面 | 15% |
| 情绪面 | 15% |
| 宏观面 | 10% |

**`--focus` 权重调整**：

| focus 值 | 调整后权重 |
|---|---|
| `technical` | 技术面 55%, 资金面 20%, 其他均分 |
| `capital_flow` | 资金面 50%, 技术面 20%, 其他均分 |
| `fundamental` | 基本面 45%, 宏观面 20%, 其他均分 |
| `sentiment` | 情绪面 45%, 技术面 25%, 其他均分 |

多个 focus 叠加时，权重平均分配后合并。例如 `--focus technical --focus capital_flow`：技术面权重 = (55%+20%)/2 = 37.5%，资金面权重 = (20%+50%)/2 = 35%，其余均分。

### Step 6: 判定趋势与置信度

**趋势判定**：

| 综合评分 | 趋势 |
|---|---|
| ≥ +2.0 | ▲ 看多 (Bullish) |
| ≤ -2.0 | ▼ 看空 (Bearish) |
| 其他 | ◆ 震荡 (Neutral) |

**置信度**：

| 评分绝对值 | 置信度 |
|---|---|
| ≥ 2.5 | 高 |
| 2.0 ~ 2.5 | 中 |
| < 2.0 | 低（震荡区间） |

### Step 7: 特殊标的处理

| 标的类型 | 额外处理 |
|---|---|
| A股 ST/*ST | 基本面维度强制 -1；标题行标注退市风险 |
| 港股 | 增加恒指联动分析、卖空占比、南向资金、AH溢价；标注无涨跌停限制 |
| ETF | 增加 IOPV 折溢价、跟踪误差、申赎清单、成交额分析 |
| 可转债 | 增加转股溢价率、纯债价值、强赎风险 |

### Step 8: 生成报告并保存

**默认模式**：使用 **assets/report-template.md** 和 **assets/report-template.html** 生成完整报告

**精简模式** (`--compact`)：

```
{股票名称}({代码}) {趋势符号}{趋势方向} | 评分{综合评分} | 技术{技术面得分} 资金{资金面得分} 基本{基本面得分} 情绪{情绪面得分} 宏观{宏观面得分}
风险: {关键风险1}; {关键风险2}
支撑/压力: {支撑位}/{压力位}
```

示例：`腾讯控股(00700.HK) ▼看空 | 评分-2.3 | 技术-2 资金-2 基本0 情绪-1 宏观-1`

**保存报告**：生成报告后，将报告内容保存到项目目录：

| 格式 | 保存路径 |
|---|---|
| Markdown | `reports/{股票代码}/{YYYYMMDD-HHmm}.md` |
| HTML | `reports/{股票代码}/{YYYYMMDD-HHmm}.html` |

- `{股票代码}`：使用 Step 2 识别的 Tushare 代码格式（如 `600519.SH`、`00700.HK`、`510050`）
- `{YYYYMMDD-HHmm}`：当前执行时刻（如 `20260504-1430`）
- 使用 Write 工具写入文件（自动创建目录）
- 默认模式同时生成 MD 和 HTML 两种格式
- 精简模式仅输出文本到对话，不保存 HTML 报告

### Step 9: 附加免责声明

所有输出必须附带免责声明：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。

## 数据源配置

### Tushare Pro API（主数据源）

本技能使用 Tushare Pro API 作为主K线行情数据源，通过 `scripts/fetch_kline.py` 脚本调用。

**Token 配置方式（优先级从高到低）**：

1. 命令行参数：`--token your_token`
2. 环境变量：`export TUSHARE_TOKEN=your_token`
3. 配置文件：`.claude/tushare-config.json`，内容为 `{"token": "your_token"}`
4. 未配置时：自动降级到东方财富数据源

**获取 Token**：注册 https://tushare.pro 并完善个人信息获取基础积分(120+)，即可调用日线行情接口。

**积分要求**：
- 日线/周线行情：120 积分起（注册+完善信息即可获得）
- ETF日线（fund_daily）：需要更高积分
- 每分钟请求限制：500次（基础积分）
- 每次返回数据上限：6000条

### 东方财富 API（降级数据源）

当 Tushare 不可用（Token 缺失、积分不足、接口权限受限）时，自动降级到东方财富API，通过 `scripts/fetch_kline_eastmoney.py` 脚本调用。

**特点**：
- 无需 Token，免费调用
- 支持上交所和深交所的A股及ETF
- 不支持港股（港股仍需 Tushare）
- 不返回均线数据，由 `analyze_technical.py` 自行计算
- 返回数据量：最多250条

**降级流程**：先执行 `fetch_kline.py` → 若 `data_source` 为 `error` → 执行 `fetch_kline_eastmoney.py`

## 参考文件

- 维度检查标准: [references/trend-dimensions.md](references/trend-dimensions.md)
- K线形态参考: [references/kline-patterns.md](references/kline-patterns.md)
- 报告模板(MD): [assets/report-template.md](assets/report-template.md)
- 报告模板(HTML): [assets/report-template.html](assets/report-template.html)
- 功能规格说明: [../../specs/stock-trend-skill.md](../../specs/stock-trend-skill.md)
- Tushare 数据获取脚本: [scripts/fetch_kline.py](scripts/fetch_kline.py)
- 东方财富数据获取脚本: [scripts/fetch_kline_eastmoney.py](scripts/fetch_kline_eastmoney.py)
- 技术分析脚本: [scripts/analyze_technical.py](scripts/analyze_technical.py)
