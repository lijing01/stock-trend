# ETF Scanner Scoring Optimization Plan

## Context

ETF scanner Phase 1 打分函数 `score_capital_flow`、`score_shares_trend`、`score_iopv` 使用5档阶梯函数，边界处微小输入变化导致大幅跳变（如 net_inflow 2001万 vs 1999万，分差25分）。Phase 1 维度间无矛盾检测。Phase 1 独有维度（shares_trend, iopv）在 combined_score 中仅占约9%权重，Phase 2 独有维度占约28%，信息不对称。

## 修改文件

- `.claude/skills/stock-trend/scripts/etf_scanner.py` — 所有3项改动的主体
- `.claude/skills/stock-trend/scripts/watchlist.yaml` — 添加 p1_exclusive_bonus 配置
- `.claude/skills/stock-trend/tests/test_etf_scanner.py` — 更新断言 + 新增测试

## Opt 1: 连续映射替代阶梯分档

### 1.1 新增 `_piecewise_linear(x, anchors)` 工具函数

在 `_trend_strength` 函数后（约 line 304）添加。输入 anchors 为 `[(input_val, output_score), ...]`，按 input_val 升序排列，分段线性插值，输出 clamp 到 [0, 100]。

### 1.2 替换3个阶梯打分函数

**`score_capital_flow`** — 定义 `CAPITAL_FLOW_ANCHORS` 常量：
```
(-1e9, 0), (-2e8, 10), (-6e7, 20), (0, 40), (6e7, 65), (2e8, 85), (1e9, 100)
```
函数体改为调用 `_piecewise_linear(avg_net, CAPITAL_FLOW_ANCHORS)`

**`score_shares_trend`** — 定义 `SHARES_TREND_ANCHORS`：
```
(-20, 0), (-10, 10), (-3, 20), (0, 40), (3, 65), (10, 85), (30, 100)
```
函数体改为调用 `_piecewise_linear(change_pct, SHARES_TREND_ANCHORS)`

**`score_iopv`** — 定义 `IOPV_ANCHORS`（非单调，最佳点在折价0.1-0.5%）：
```
(-2.0, 10), (-0.5, 40), (-0.3, 85), (-0.05, 65), (0.15, 30), (0.3, 15), (2.0, 0)
```
函数体改为调用 `_piecewise_linear(float(premium), IOPV_ANCHORS)`

### 1.3 RSI 子评分连续化

替换 `score_momentum` 中 RSI 分档逻辑（lines 336-345），定义 `RSI_ANCHORS`：
```
(0, -10), (20, -10), (30, 3), (40, 10), (50, 10), (60, 10), (70, 3), (80, -10), (100, -10)
```
改为 `score += _piecewise_linear(rsi_val, RSI_ANCHORS)`

### 1.4 测试更新

- `test_score_capital_flow_positive`: 断言从 `> 60` 放宽到 `> 50`（avg=40M 现在插值约56.7）
- 新增 `test_piecewise_linear_*` 系列（基础插值、clamp、非单调IOPV、无断崖）
- 新增 `test_continuous_scoring_no_cliffs`: 在旧阶梯边界处 ±2 单位验证 |s1-s2| < 5

## Opt 2: 矛盾信号检测

### 2.1 新增 `detect_contradictions(dimensions: dict) -> list[str]`

定义 `CONTRADICTION_RULES` 常量列表，4条规则：

| 规则名 | 条件 | 消息 |
|--------|------|------|
| 缩量上涨 | momentum≥70 且 volume<40 | "缩量上涨，动能不可靠" |
| 动量资金矛盾 | momentum≥70 且 capital_flow<30 | "动量与资金流向矛盾" |
| 资金流入溢价 | capital_flow≥70 且 iopv<30 | "资金流入但溢价偏高" |
| 放量下跌 | volume≥70 且 momentum<30 | "放量下跌" |

### 2.2 集成到 `compute_quick_score`

在维度计算之后、加权求和之前调用 `detect_contradictions(dims)`，结果存入返回 dict 的 `warnings` 键。

### 2.3 展示到 `build_top_picks` 和 `build_excluded`

- `build_top_picks`: logic_parts 末尾追加 `⚠{warning}` 格式的矛盾提示
- `build_excluded`: reasons 末尾追加矛盾提示
- `build_combined_ranking`: entry 中添加 `"warnings": p1.get("warnings", [])`

### 2.4 测试

- `test_detect_contradictions_*` 系列：4条规则分别触发、None维度安全、一致信号无warning
- `test_compute_quick_score_includes_warnings`: 验证返回 dict 含 warnings 字段

## Opt 3: Phase 1 独有维度 bonus

### 3.1 watchlist.yaml 添加配置

```yaml
p1_exclusive_bonus:
  shares_trend: 0.05
  iopv: 0.05
```

最大 bonus = 0.05×100 + 0.05×100 = 10分。

### 3.2 修改 `build_combined_ranking`

当前公式：`0.3 × quick_score + 0.7 × deep_normalized`

新公式：`0.3 × quick_score + 0.7 × deep_normalized + bonus`

其中 `bonus = Σ(dim_score × p1_exclusive_weight)` 对 Phase 1 独有维度（shares_trend, iopv）。

配置缺省时 bonus=0，行为不变。

结果 dict 新增 `"p1_bonus"` 字段用于透明度。

### 3.3 测试

- `test_build_combined_ranking`: 更新期望值（加 bonus），验证 p1_bonus 字段
- 新增 `test_build_combined_ranking_no_bonus`: settings 为空 dict 时 behavior 与旧版一致

## 实施顺序

1. **Opt 1** — 基础改动，影响所有下游分数
2. **Opt 2** — 纯增量，不改分数计算
3. **Opt 3** — 依赖 Opt 1 稳定分数，改合并公式

## 验证

```bash
# 单元测试
python3 .claude/skills/stock-trend/tests/test_etf_scanner.py

# Golden snapshot（应无变化，因 golden 测 pipeline 不测 scanner）
python3 .claude/skills/stock-trend/tests/test_golden.py --diff

# 集成验证（小规模扫描）
python3 .claude/skills/stock-trend/scripts/etf_scanner.py --no-deep --top-n 3 --output-json
```