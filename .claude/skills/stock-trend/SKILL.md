---
name: stock-trend
description: 对 A股、港股、ETF 执行日趋势判断，输出结构化报告
triggers:
  - /stock-trend
---

# 股票趋势判断 Skill

## 工作流

### Step 1: 解析输入

解析用户输入的股票代码和参数：

```
/stock-trend <code> [--focus <维度>] [--horizon <周期>] [--compact]
```

- `code`（必填）：股票/ETF 代码
- `--focus`（可选，可叠加）：侧重维度 `technical | capital_flow | fundamental | sentiment`
- `--horizon`（可选，默认 `daily`）：`intraday | daily | weekly`
- `--compact`（可选）：精简输出模式

### Step 2: 识别市场与标的

根据代码模式自动识别市场和标的类型：

| 模式 | 市场 |
|---|---|
| `6xxxxx` / `6xxxxx.SH` | 上交所 A股 |
| `0xxxxx` / `0xxxxx.SZ` | 深交所 A股 |
| `3xxxxx` / `3xxxxx.SZ` | 创业板 |
| `688xxx` / `688xxx.SH` | 科创板 |
| `5xxxxx` | 上交所 ETF |
| `15xxxx` | 深交所 ETF |
| `0xxxx` / `0xxxx.HK` | 港股 |

特殊标记：
- A股 ST/*ST → 退市风险警示
- 港股 → 无涨跌停限制
- ETF → 需分析 IOPV 折溢价

### Step 3: 五维分析

按以下五个维度执行趋势分析，详细检查标准见 **references/review-dimensions.md**：

1. **技术面** (Technical) — MA/MACD/RSI/KDJ/布林带/成交量
2. **资金面** (Capital Flow) — 主力流入/北向资金/融资融券/龙虎榜
3. **基本面** (Fundamental) — PE-PB/业绩增速/行业景气/股息率
4. **情绪面** (Sentiment) — 涨跌停/换手率/板块联动/舆情
5. **宏观面** (Macro) — 货币政策/行业政策/外盘/汇率

每个维度评分范围: **-3 ~ +3**

### Step 4: 计算综合评分

```
综合评分 = Σ(维度得分 × 维度权重)
```

默认权重：技术面 35% / 资金面 25% / 基本面 15% / 情绪面 15% / 宏观面 10%

`--focus` 会调整权重分配，详见规格说明。

### Step 5: 判定趋势

| 综合评分 | 趋势 |
|---|---|
| ≥ +2.0 | ▲ 看多 |
| ≤ -2.0 | ▼ 看空 |
| 其他 | ◆ 震荡 |

### Step 6: 生成报告

- 默认模式：使用 **assets/report-template.md** 生成完整报告
- `--compact` 模式：输出一行摘要

### Step 7: 附加免责声明

所有输出必须附带免责声明：本报告仅供学习参考，不构成投资建议。

## 参考文件

- 维度检查标准: [references/trend-dimensions.md](references/trend-dimensions.md)
- 报告模板: [assets/report-template.md](assets/report-template.md)
- 功能规格说明: [../../specs/stock-trend-skill.md](../../specs/stock-trend-skill.md)