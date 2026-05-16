# ETF Scan 优化：Price Extension 趋势延伸惩罚

> 设计文档 — 2026-05-16

## 问题

159949 等高 Beta ETF 出现追涨陷阱：MA5>MA20>MA60 完美排列确认时，价格已走完大部分涨幅，按当前 momentum 评分（+15 MA排列 + 8 MACD + 8 趋势 + RSI ≈ 85分）仍排前列，导致推荐即见顶。

## 方案：Price Extension 惩罚（连续衰减）

在 `score_momentum()` 中新增检查：`收盘价偏离 MA20 的百分比`。

### 取值逻辑

```
偏离 MA20          扣分    含义
────────────────────────────────
< -3%               -5    趋势走弱（跌破 MA20）
-3% ~ +3%            0    正常范围，健康趋势
+3% ~ +5%           -3    轻微延伸，注意追涨
+5% ~ +8%           -8    明显延伸，风险上升
+8% ~ +12%         -15    严重延伸，不宜追
> +12%              -20    极端追涨风险
```

- 使用 `_piecewise_linear()` 做连续插值（已有工具函数），避免硬边界跳变
- 对称处理多头和空头延伸（多头过度→扣分，空头过度→轻微扣分）

### 效果预期

| ETF | 场景 | 改前 momentum | 改后 momentum | 排名变化 |
|---|---|---|---|---|
| 159949 | 大涨15%后 | ~89 | ~74 | 下降2-4位 |
| 513180 | 趋势初期 | ~75 | ~75 | 不变 |
| 510050 | 稳步上涨 | ~70 | ~68 | 几乎不变 |
| 513100 | 慢牛趋势 | ~80 | ~77 | 小幅下降 |

### Contradiction Warning 联动

新增一条 contradiction rule：

```python
{
    "name": "price_extension",
    "condition": lambda dims: dims.get("price_ext_pct", 0) > 5,
    "message": "价格偏离均线过大，追涨风险高",
}
```

### 改动范围

仅 `etf_scanner.py`：
- `score_momentum()`：新增 ~15 行 price extension 检查
- `CONTRADICTION_RULES`：新增 1 条规则
- `compute_quick_score()`：将 price_ext_pct 传入 dimensions

### 不涉及

- Phase 2 深度分析管线不做修改
- watchlist.yaml 不变
- 报告模板不变
