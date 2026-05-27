# S/R 支撑位算法增强计划

## 目标

给 `calc_support_resistance()` 增加 4 种新 S/R 源：VWAP、滚动分位数、Pivot Points、Volume Profile 近似。

## 改动范围

单文件：`analyze_technical.py`。新增 4 个函数 + 修改 `calc_support_resistance()`。

```
calc_support_resistance()     # 改：新增4种源调用
├── calc_vwap()                # 新增 P0
├── calc_quantile_levels()     # 新增 P0
├── calc_pivot_points()        # 新增 P1
└── calc_volume_profile()      # 新增 P1
```

下游（technical.json → scores.json → 报告/图表）**无需修改**，S/R 通过统一列表流动。

---

## P0：高 ROI，低工作量

### P0-1: VWAP 近似（~30 行）

在 `calc_support_resistance()` 内新增调用。作为候选 level 进入现有聚类管道。

- 20 日窗口（中线持仓一致）
- typical price = (H+L+C)/3，成交量加权
- 低于现价 → support，高于 → resistance
- strength: "medium"，source: "vwap"

### P0-2: 滚动分位数（~20 行）

- 60 日窗口收盘价分位数
- 5%/10% → support，90%/95% → resistance
- strength: "medium"，source: "q_05"/"q_10" 等

---

## P1：中 ROI，中等工作量

### P1-1: Pivot Points（~30 行）

- 经典 Pivot：P=(H+L+C)/3, R1=2P-L, S1=2P-H, R2=P+(H-L), S2=P-(H-L)
- 用前一根完成 bar 计算
- strength: "medium"（A 股隔夜跳空多，不加重权重）

### P1-2: Volume Profile 近似（~80 行）

- 60 日日 K，分 20 个等宽价格桶
- 线性插值分配每根 K 的成交量到相邻桶
- 成交量大于均值 1.5 倍的桶 → HVN
- HVN 低于现价 → support，高于 → resistance
- strength: "medium"，带 vol_ratio 元数据

---

## 实施步骤

| # | 操作 | 文件 | 行数 |
|---|---|---|---|
| 1 | 新增 `calc_vwap()` | analyze_technical.py | +25 |
| 2 | 新增 `calc_quantile_levels()` | analyze_technical.py | +18 |
| 3 | `calc_support_resistance()` 调用 vwap + quantile | analyze_technical.py | +8 |
| 4 | 新增 `calc_pivot_points()` | analyze_technical.py | +25 |
| 5 | 新增 `calc_volume_profile()` | analyze_technical.py | +70 |
| 6 | `calc_support_resistance()` 调用 pivot + volume_profile | analyze_technical.py | +10 |
| 7 | 运行 `test_stock_trend.py` | - | 确认 |
| 8 | 运行 `test_golden.py --diff` | - | 确认 |
| 9 | 如果 diff 合理，`test_golden.py --regenerate` + commit | - | - |

---

## 风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| Volume Profile 桶数不当 | 中 | 自适应：`max(20, int(atr_pct*5))` |
| VWAP 与 MA 信息冗余 | 低 | 聚类自动合并邻近价位 |
| Pivot 隔夜跳空后陈旧 | 中 | strength="medium"，竞争中被覆盖 |
| Golden snapshot 数值变化 | 必然 | `--regenerate`，commit 说明新增源 |

---

## 不做

- Order Flow：需 Level-2 逐笔数据
- KDE / Change Point Detection：聚类已覆盖，ROI 低
- Transformer/ML：过度工程

---

## 背景

原始讨论：头脑风暴业内支撑位算法 → 对比当前 6 源自定义聚类 vs 行业方案 → 定位差距主要在成交量和统计分位 → 此处是实施计划。
