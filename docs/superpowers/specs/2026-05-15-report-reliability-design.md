# 报告评分可靠性优化设计

> 日期: 2026-05-15
> 状态: 设计稿
> 关联: [[2026-05-15-stock-trend-optimization-design]]（并行化+数据源优化）

## 1. 问题

同一标的（159740.SZ）同一天两次 `/stock-trend` 生成报告，非技术面维度评分差异巨大：

| 维度 | 报告A (2321) | 报告B (2330) | 差距 |
|---|---|---|---|
| 资金面 | 0.5 | -1.0 | 1.5 |
| 基本面 | 1.0 | 0.0 | 1.0 |
| 情绪面 | -0.5 | 1.0 | 1.5 |
| 宏观面 | 0.5 | 2.0 | 1.5 |

技术面一致（脚本自动算分），其他4维全靠Agent自由裁量，导致不可复现。

### 根因

1. **无必检项**：Agent自由选择子指标，选不同子指标→不同结论
2. **无事件封顶**：单一事件可占满维度得分（如中美会晤→宏观面2.0）
3. **无正反约束**：Agent选择性采信利好或利空，忽略反向信号
4. **ETF维度归属模糊**：IOPV折价在报告A归资金面、报告B归基本面
5. **无自检机制**：一次评分即定稿，无逆向审视

## 2. 方案

规则约束（治本）+ 自检机制（治偏见），不多次运行避免3倍token消耗。

## 3. 详细设计

### 3.1 必检项清单

每个非技术维度定义必检子指标。Agent必须覆盖至少N项才能打分。

#### 资金面

| 必检项 | 优先级 | 数据源 | ETF替代 |
|---|---|---|---|
| 主力净流入/流出 | P0 | capital_flow.json | ETF申赎净额 |
| 北向/南向资金 | P1 | capital_flow.json data_extended | 南向资金 |
| IOPV折溢价 | P0(ETF) | etf_data.json | — |

最低覆盖：2项。只覆盖1项→维度得分×0.5折减。0项→得分=0，标注"数据不足"。

#### 基本面

| 必检项 | 优先级 | 数据源 | ETF替代 |
|---|---|---|---|
| PE/PB估值分位 | P0 | fundamental.json | 跟踪指数PE分位 |
| 盈利增速 | P0 | fundamental.json | 指数盈利增速 |
| NAV/跟踪误差 | P0(ETF) | etf_data.json | — |

最低覆盖：2项。ETF标的PE分位和盈利增速可从指数数据获取。

#### 情绪面（无自动化数据，全靠搜索）

| 必检项 | 优先级 | 数据源 |
|---|---|---|
| 涨跌停/板块联动 | P0 | 搜索 |
| 新闻舆情 | P0 | 搜索（至少2条来源） |

最低覆盖：2项。新闻舆情必须≥2条独立来源，否则标注"来源单一"。

#### 宏观面

| 必检项 | 优先级 | 数据源 |
|---|---|---|
| 货币政策（利率/LPR/降准） | P0 | macro_snapshot.json + 搜索 |
| 外盘影响（美股/A50） | P1 | macro_snapshot.json + 搜索 |

最低覆盖：2项。

#### 折减规则

```python
COVERAGE_RULES = {
    "capital_flow": {"min_items": 2, "penalty_factor": 0.5},
    "fundamental":  {"min_items": 2, "penalty_factor": 0.5},
    "sentiment":    {"min_items": 2, "penalty_factor": 0.5},
    "macro":        {"min_items": 2, "penalty_factor": 0.5},
}

def apply_coverage_penalty(dim, covered_items, raw_score):
    rule = COVERAGE_RULES[dim]
    if covered_items < rule["min_items"]:
        return raw_score * rule["penalty_factor"], f"{dim}仅覆盖{covered_items}项，低于最低{rule['min_items']}项，得分×0.5"
    return raw_score, None
```

### 3.2 ETF维度归属规则

ETF特殊指标的维度归属固定，禁止跨维度使用：

| ETF指标 | 归属维度 | 说明 |
|---|---|---|
| IOPV折溢价率 | 资金面 | 反映二级市场交易供需 |
| NAV/跟踪误差 | 基本面 | 反映基金管理质量 |
| 申赎净额 | 资金面 | 机构申赎行为 |
| 成交额 | 资金面(辅助) | 流动性信号 |

在 SKILL.md 和 trend-dimensions.md 中明确标注。compute_scores.py 校验时检测跨维度使用并发出警告。

### 3.3 单事件封顶

单一事件对维度得分贡献有上限：

| 维度 | 单事件封顶 | 说明 |
|---|---|---|
| 宏观面 | 1.5 | 地缘事件影响大但不确定性高 |
| 资金面 | 1.0 | 单次资金流入/流出不应主导 |
| 基本面 | 1.0 | 单一财务指标不应主导 |
| 情绪面 | 1.0 | 单条新闻不应主导 |

**判定"单一事件"标准**：同一天同一主题的新闻算一个事件，不重复计分。

**高分要求**：维度得分绝对值>1.5时，必须≥2条独立信号支撑。单信号最多贡献封顶值。

```python
EVENT_CAPS = {
    "macro":       {"single_event_max": 1.5, "high_score_min_signals": 2},
    "capital_flow":{"single_event_max": 1.0, "high_score_min_signals": 2},
    "fundamental": {"single_event_max": 1.0, "high_score_min_signals": 2},
    "sentiment":   {"single_event_max": 1.0, "high_score_min_signals": 2},
}

def validate_event_cap(dim, score, signal_count):
    cap = EVENT_CAPS[dim]
    adjusted = score
    warnings = []

    if signal_count < cap["high_score_min_signals"] and abs(score) > cap["single_event_max"]:
        direction = 1 if score > 0 else -1
        adjusted = direction * cap["single_event_max"]
        warnings.append(
            f"{dim}得分{score}超出单事件封顶{cap['single_event_max']}且仅{signal_count}条信号，已修正为{adjusted}"
        )

    return adjusted, warnings
```

### 3.4 正反强制规则

每个非技术维度必须同时列出至少1条利好+1条利空。

**摘要格式要求**：

```
资金面：利多：ETF近20日净申购+1.75亿；利空：主力5/12净流出1.54亿
宏观面：利多：中美会晤构建战略稳定关系；利空：美联储鹰派维持高利率
```

**违反处理**：
- 搜索结果只有单向信号时，Agent必须标注"未找到反向信号，可能存在确认偏差"
- 缺少反向信号→该维度一致性因子×0.6
- 在摘要中用分号分隔利多利空，便于程序解析

**搜索增强**：

Step 3每个维度的搜索关键词增加反向验证词：
- 资金面：原关键词 + "流出 卖机 减持"
- 基本面：原关键词 + "风险 下滑 亏损"
- 情绪面：原关键词 + "下跌 跌停 恐慌"
- 宏观面：原关键词 + "鹰派 衰退 收紧"

```python
COUNTER_KEYWORDS = {
    "capital_flow": "流出 危机 减持",
    "fundamental":  "风险 下滑 亏损",
    "sentiment":    "下跌 跌停 恐慌",
    "macro":        "鹰派 衰退 收紧",
}
```

### 3.5 Step 3.5: 逆向校验（新增步骤）

在Step 3（搜索+初步打分）和Step 4（计算综合评分）之间新增：

#### 流程

1. Agent完成四维搜索并形成初步打分
2. 对每个非技术维度做逆向审视：
   - 若初步打分>0：强制寻找做空理由
   - 若初步打分<0：强制寻找做多理由
   - 若=0：双向审视
3. 逆向审视结果：
   - 原打分维持 → 通过
   - 发现被忽略的反向信号 → 得分向0靠近0.5-1.0
4. 自检结果传入 compute_scores.py

#### Agent自检清单（SKILL.md中定义）

```markdown
### Step 3.5: 逆向校验

对每个非技术维度执行：
1. [ ] 该维度是否覆盖≥2个必检项？
2. [ ] 该维度是否同时包含利好和利空信号？
3. [ ] 单一事件贡献是否超过封顶值？
4. [ ] 逆向审视：假设当前打分方向错误，最强的反向论据是什么？
5. [ ] 反向论据是否已在摘要中体现？

未通过项的处理：
- 缺必检项 → 应用覆盖折减
- 缺反向信号 → 一致性因子×0.6
- 超封顶 → 自动修正得分
- 逆向审视调整得分 → 向0靠近0.5-1.0
```

#### compute_scores.py 新增参数

```bash
python3 compute_scores.py \
  --self-check '{"capital_flow":{"counter_found":true,"adjusted":false},"sentiment":{"counter_found":false,"adjusted":true,"original":1.0,"revised":0.5}}' \
  ...
```

### 3.6 compute_scores.py 合理性校验

整合上述所有校验规则：

```python
def validate_dimension_scores(scores, signals_info, self_check):
    """
    校验所有维度得分的合理性。
    
    Args:
        scores: dict of {dim: score}
        signals_info: dict of {dim: {"count": int, "has_counter": bool}}
        self_check: dict of {dim: {"counter_found": bool, "adjusted": bool, ...}}
    
    Returns:
        adjusted_scores: dict of {dim: adjusted_score}
        warnings: list of warning strings
        confidence_penalty: float (0.0-1.0, multiplier for confidence)
    """
    adjusted = {}
    warnings = []
    penalty_factors = []

    for dim in ["capital_flow", "fundamental", "sentiment", "macro"]:
        score = scores.get(dim, 0)
        info = signals_info.get(dim, {})
        signal_count = info.get("count", 1)
        has_counter = info.get("has_counter", False)
        check = self_check.get(dim, {})

        # 1. 覆盖折减
        score, w = apply_coverage_penalty(dim, signal_count, score)
        if w: warnings.append(w)

        # 2. 单事件封顶
        score, w = validate_event_cap(dim, score, signal_count)
        if w: warnings.append(w)

        # 3. 正反强制：缺反向信号降低一致性
        if not has_counter:
            penalty_factors.append(0.6)
            warnings.append(f"{dim}缺少反向信号，一致性因子×0.6")

        # 4. 自检调整
        if check.get("adjusted"):
            warnings.append(f"{dim}经逆向校验调整得分")

        adjusted[dim] = score

    # 技术面不校验（脚本自动计算）
    adjusted["technical"] = scores.get("technical", 0)

    # 综合置信度惩罚
    overall_penalty = 1.0
    if penalty_factors:
        overall_penalty = min(penalty_factors)

    return adjusted, warnings, overall_penalty
```

置信度降级规则：
- overall_penalty < 1.0 → 置信度降一档（高→中，中→低，低→低）
- 自检发现得分调整 → 置信度降一档

### 3.7 修改文件清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `SKILL.md` | 修改 | 新增Step 3.5逆向校验；Step 3增加必检项和反向搜索词；增加ETF维度归属表 |
| `references/trend-dimensions.md` | 修改 | 每个维度增加必检项标记；增加单事件封顶值；增加正反强制规则；增加ETF归属表 |
| `compute_scores.py` | 修改 | 新增`--self-check`参数；新增validate_dimension_scores()；整合覆盖折减+事件封顶+正反强制 |
| `generate_report.py` | 修改 | 报告中展示校验警告（如有）；展示必检项覆盖情况 |

不涉及：
- analyze_technical.py（不变）
- fetch_*.py 脚本（不变）
- report-template（不变，警告由generate_report动态注入）

## 4. 对比示例

159740.SZ 两份报告经优化后的预期结果：

| 维度 | 报告A原始 | 报告B原始 | 优化后预期 | 说明 |
|---|---|---|---|---|
| 技术面 | 0 | 0 | 0 | 不变 |
| 资金面 | 0.5 | -1.0 | 0~0.25 | 必须同时看净申购+OBV流出，正反拉平 |
| 基本面 | 1.0 | 0.0 | 0.5~1.0 | PE分位低+盈利弱，折中 |
| 情绪面 | -0.5 | 1.0 | 0~0.5 | 必须同时看中美利好+冲高回落 |
| 宏观面 | 0.5 | 2.0 | 0.5~1.0 | 中美会晤封顶1.5但需独立信号支撑 |

两份报告经相同规则约束后，评分差距从1.0-1.5缩小到0-0.5。

## 5. 验证方式

1. 对159740.SZ执行优化后的`/stock-trend`，确认：
   - 每个维度摘要包含利多利空
   - 无单一事件得分超封顶
   - 必检项覆盖≥2
   - 逆向校验步骤执行
2. 对比优化前后两份报告的综合评分差距
3. 测试覆盖折减：模拟只有1项必检数据，确认得分×0.5
4. 测试事件封顶：模拟单信号高分场景，确认自动修正

## 6. 实施顺序

1. `trend-dimensions.md` — 增加必检项/封顶/正反/归属规则（纯文档，无风险）
2. `SKILL.md` — 新增Step 3.5 + 修改Step 3搜索词（纯文档）
3. `compute_scores.py` — 实现校验逻辑（核心代码）
4. `generate_report.py` — 报告展示校验结果（展示层）
5. 验证：159740.SZ端到端测试