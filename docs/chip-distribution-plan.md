# 日线筹码分布近似方案

> 用日线 OHLCV + Volume 近似筹码分布，无需 Level-2 数据。

## 目标

替换现有 `calc_volume_profile`（20桶 + 30/40/30 分布 → 只提取 S/R，丢弃形状）。新方案产出完整分布结构 + 关键度量，融入报告与图表。

## 改动清单

### 1. 新文件: `compute_chip_distribution.py`

**输入**: kline.json (OHLCV)
**输出**: chip_distribution.json

**算法 — 三角权重分布**:

- 50 桶等分 N 日价格范围（N=120，覆盖约半年）
- 每根 K 线 volume 按三角权重分配给 H-L 价格范围，重心在 Close
  - 阳线(close>open)：分布偏上
  - 阴线(close<open)：分布偏下
  - 权重公式：`w(i) = 1 - |price_i - C| / (H - L)`，归一化 × volume
- 全桶累积后计算：

| 输出字段 | 含义 |
|---|---|
| `distribution` | `[{price, volume, vol_ratio}]` 数组，按 price 排序 |
| `avg_cost` | 加权平均成本 Σ(p×v)/Σv |
| `profit_ratio` | 获利盘比例（成本 < 当前价的筹码占比） |
| `concentration` | 集中度（当前价 ± ATR×1 范围内筹码占比） |
| `high_volume_nodes` | Top 5 高量峰值节点（price, volume, vol_ratio） |

### 2. 修改 `run_pipeline.py`

pipeline 新增一步：
```
fetch_kline → analyze_technical → ... → compute_chip_distribution
```

`pipeline_output.json` 新增 `chip_distribution` 条目。

### 3. 修改 `generate_chart_html.py`

ECharts K 线图底部叠加横向筹码分布柱状图（类似盘口量能分布可视化）。

### 4. 修改报告模板 `report-template.md`

新增 `{{#chip_distribution}}` 条件区块：

```
{{#chip_distribution}}
## 六、筹码分布

| 指标 | 数值 |
|---|---|
| 加权平均成本 | {{avg_cost}} |
| 当前价 | {{current_price}} |
| 获利盘比例 | {{profit_ratio}} |
| 筹码集中度 | {{concentration}} |

**量能峰值**:
| 价格 | 量比 |
|---|---|
{{#high_volume_nodes}}
| {{price}} | {{vol_ratio}} |
{{/high_volume_nodes}}
{{/chip_distribution}}
```

原 `## 六、{{特殊标记标题}}` 向后顺延为 `## 七、`。

### 5. 修改 `generate_report.py`

读取 chip_distribution.json 数据注入 report context。模板条件 `{{#chip_distribution}}` 自动控制渲染。

## 不修改的文件

- `analyze_technical.py` — 现有 S/R 逻辑不动
- `compute_scores.py` — 评分逻辑不变，筹码数据仅供展示

## 实现顺序

```
compute_chip_distribution.py
  → run_pipeline.py (集成)
  → generate_chart_html.py (可视化)
  → report-template.md + generate_report.py (报告)
```
