# 报告评分可靠性优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add mandatory check items, event caps, bullish/bearish balance rules, and self-check mechanism to non-technical dimension scoring, making reports reproducible across runs.

**Architecture:** Pure constraints layer — no new data sources or pipelines. Three touchpoints: (1) documentation (trend-dimensions.md, SKILL.md) defines rules for the Agent, (2) compute_scores.py enforces rules programmatically when scores are passed, (3) generate_report.py surfaces warnings. Step 3.5 in SKILL.md guides Agent self-check between search and scoring.

**Tech Stack:** Python 3 (existing scripts), Markdown (existing docs)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `.claude/skills/stock-trend/references/trend-dimensions.md` | Modify | Add mandatory items, event caps, balance rules, ETF ownership table |
| `.claude/skills/stock-trend/SKILL.md` | Modify | Add Step 3.5, modify Step 3 search keywords, add ETF ownership table |
| `.claude/skills/stock-trend/scripts/compute_scores.py` | Modify | Add validation logic + `--self-check` param + `--signals-info` param |
| `.claude/skills/stock-trend/scripts/generate_report.py` | Modify | Render validation warnings in report footer area |
| `.claude/skills/stock-trend/assets/report-template.md` | Modify | Add `{{#校验警告}}` section |
| `docs/superpowers/specs/2026-05-15-report-reliability-design.md` | No change | Already written |

---

### Task 1: Add mandatory items, event caps, balance rules, ETF ownership to trend-dimensions.md

**Files:**
- Modify: `.claude/skills/stock-trend/references/trend-dimensions.md:111-164`

- [ ] **Step 1: Add mandatory check items to each dimension**

After line 118 (资金面 table end), add:

```markdown
#### 必检项（最低覆盖2项，否则得分×0.5折减）

| 必检项 | 优先级 | 数据源 | ETF替代指标 |
|---|---|---|---|
| 主力净流入/流出 | P0 | capital_flow.json | ETF申赎净额 |
| 北向/南向资金 | P1 | capital_flow.json data_extended | 南向资金 |
| IOPV折溢价 | P0(ETF) | etf_data.json | — |
```

After line 130 (基本面 table end), add:

```markdown
#### 必检项（最低覆盖2项，否则得分×0.5折减）

| 必检项 | 优先级 | 数据源 | ETF替代指标 |
|---|---|---|---|
| PE/PB估值分位 | P0 | fundamental.json | 跟踪指数PE分位 |
| 盈利增速 | P0 | fundamental.json | 指数盈利增速 |
| NAV/跟踪误差 | P0(ETF) | etf_data.json | — |
```

After line 140 (情绪面 table end), add:

```markdown
#### 必检项（最低覆盖2项，否则得分×0.5折减）

| 必检项 | 优先级 | 数据源 |
|---|---|---|
| 涨跌停/板块联动 | P0 | 搜索 |
| 新闻舆情 | P0 | 搜索（至少2条独立来源） |
```

After line 152 (宏观面 table end), add:

```markdown
#### 必检项（最低覆盖2项，否则得分×0.5折减）

| 必检项 | 优先级 | 数据源 |
|---|---|---|
| 货币政策（利率/LPR/降准） | P0 | macro_snapshot.json + 搜索 |
| 外盘影响（美股/A50） | P1 | macro_snapshot.json + 搜索 |
```

- [ ] **Step 2: Add event cap and bullish/bearish balance rules**

After line 152 (宏观面 section end), add a new section:

```markdown
---

## 6. 单事件封顶规则

单一事件对维度得分贡献有上限：

| 维度 | 单事件封顶 | 高分最低信号数 |
|---|---|---|
| 宏观面 | 1.5 | 2（得分>1.5需≥2条独立信号） |
| 资金面 | 1.0 | 2（得分>1.0需≥2条独立信号） |
| 基本面 | 1.0 | 2（得分>1.0需≥2条独立信号） |
| 情绪面 | 1.0 | 2（得分>1.0需≥2条独立信号） |

**判定标准**：同一天同一主题的新闻算一个事件，不重复计分。

## 7. 正反强制规则

每个非技术维度必须同时列出至少1条利好+1条利空。

**摘要格式**：
```
资金面：利多：ETF近20日净申购+1.75亿；利空：主力5/12净流出1.54亿
```

**违反处理**：
- 搜索结果只有单向信号 → 标注"未找到反向信号，可能存在确认偏差"
- 缺少反向信号 → 该维度一致性因子×0.6

**反向验证搜索词**（追加到Step 3搜索关键词后）：

| 维度 | 反向验证词 |
|---|---|
| 资金面 | 流出 危机 减持 |
| 基本面 | 风险 下滑 亏损 |
| 情绪面 | 下跌 跌停 恐慌 |
| 宏观面 | 鹰派 衰退 收紧 |
```

- [ ] **Step 3: Add ETF dimension ownership table**

After line 162 (可转债 section end), add:

```markdown
---

## 8. ETF指标维度归属（禁止跨维度使用）

| ETF指标 | 归属维度 | 说明 |
|---|---|---|
| IOPV折溢价率 | 资金面 | 反映二级市场交易供需 |
| NAV/跟踪误差 | 基本面 | 反映基金管理质量 |
| 申赎净额 | 资金面 | 机构申赎行为 |
| 成交额 | 资金面(辅助) | 流动性信号 |
```

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-trend/references/trend-dimensions.md
git commit -m "feat: add mandatory items, event caps, balance rules, ETF ownership to trend-dimensions"
```

---

### Task 2: Add Step 3.5 and modify Step 3 in SKILL.md

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md:96-154`

- [ ] **Step 1: Modify Step 3 search keywords to add reverse verification words**

Replace the search keywords table at line 104-109 with:

```markdown
| 维度 | 权重 | 自动化基线 | 搜索关键词 | 反向验证词 |
|------|------|-----------|-----------|-----------|
| 资金面 | 25% | `data_extended.northbound/margin`（管线已获取） | `"{stock_name} {ts_code} 资金流向 北向资金 {YYYY}年{M}月"` | `"流出 危机 减持"` |
| 基本面 | 15% | `fundamental.json`（PE/PB/ROE/财务数据已获取） | `"{stock_name} {ts_code} 估值 业绩 {YYYY}年{M}月"` | `"风险 下滑 亏损"` |
| 情绪面 | 15% | 无自动化 | `"{stock_name} 涨跌停 换手率 板块 {YYYY}年{M}月"` | `"下跌 跌停 恐慌"` |
| 宏观面 | 10% | `macro_snapshot.json`（汇率/利率/PMI已获取） | `"今日宏观 政策 利率 汇率 外盘 {YYYY}年{M}月"` | `"鹰派 衰退 收紧"` |
```

- [ ] **Step 2: Modify dimension summary format to require bullish/bearish markers**

Replace the summary examples at lines 127-131 with:

```markdown
为每个非技术面维度撰写**简明摘要**（1-2句话），含利多利空标记和关键数据，例如：
- 资金面：`利多：ETF近20日净申购+1.75亿元；利空：主力交易资金近2日净流出1.59亿元`
- 基本面：`利多：恒生科技PE 22.9倍处历史32%分位偏低；利空：盈利增速仅3-4%偏弱`
- 情绪面：`利多：AI+半导体领涨；利空：指数冲高回落，缩量调整中`
- 宏观面：`利多：中美关税缓和；利空：美联储鹰派维持高利率`

**正反强制**：每个维度必须同时包含利多和利空。只有单向信号时标注"未找到反向信号，可能存在确认偏差"。
摘要格式：`利多：xxx；利空：xxx`（分号分隔，便于程序解析）。
```

- [ ] **Step 3: Add Step 3.5 between current Step 3 and Step 4**

Insert after line 154 (end of Step 3), before Step 4:

```markdown
## Step 3.5: 逆向校验

> 对每个非技术维度做逆向审视，防止确认偏差导致评分偏颇。

对每个非技术维度（资金面/基本面/情绪面/宏观面）执行：

1. [ ] 该维度是否覆盖≥2个必检项？（必检项清单见 [trend-dimensions.md](references/trend-dimensions.md)）
2. [ ] 该维度是否同时包含利好和利空信号？
3. [ ] 单一事件贡献是否超过封顶值？（宏观1.5，其他1.0）
4. [ ] 逆向审视：假设当前打分方向错误，最强的反向论据是什么？
5. [ ] 反向论据是否已在摘要中体现？

**未通过项的处理**：
- 缺必检项 → 覆盖折减：维度得分×0.5
- 缺反向信号 → 一致性因子×0.6
- 超封顶 → 自动修正得分至封顶值
- 逆向审视调整得分 → 向0靠近0.5-1.0

**自检结果传入 Step 4**：通过 `--self-check` 参数传入，格式：
```json
{
  "capital_flow": {"counter_found": true, "adjusted": false, "covered_items": 3},
  "fundamental":  {"counter_found": true, "adjusted": false, "covered_items": 2},
  "sentiment":    {"counter_found": false, "adjusted": true, "original": 1.0, "revised": 0.5, "covered_items": 2},
  "macro":        {"counter_found": true, "adjusted": false, "covered_items": 3}
}
```
```

- [ ] **Step 4: Add `--self-check` and `--signals-info` to Step 4 compute_scores.py command**

Replace the compute_scores.py command block at lines 160-179 with:

```markdown
```bash
python3 .claude/skills/stock-trend/scripts/compute_scores.py \
  --technical /tmp/technical.json \
  --capital-flow-score <资金面得分> \
  --fundamental-score <基本面得分> \
  --sentiment-score <情绪面得分> \
  --macro-score <宏观面得分> \
  [--focus <维度>] \
  [--asset-type etf|hk|st|stock] \
  [--etf-data /tmp/etf_data.json] \
  [--capital-flow-data /tmp/capital_flow.json] \
  [--fundamental-data /tmp/fundamental.json] \
  [--macro-data /tmp/macro_snapshot.json] \
  [--self-check '{"capital_flow":{"counter_found":true,"adjusted":false,"covered_items":3},...}'] \
  [--signals-info '{"capital_flow":{"count":3,"has_counter":true},...}'] \
  [--risks '["风险1","风险2"]'] \
  [--capital-summary "利多：ETF近20日净申购+1.75亿；利空：主力净流出1.54亿"] \
  [--fundamental-summary "利多：PE 22.9倍偏低；利空：盈利增速3-4%偏弱"] \
  [--sentiment-summary "利多：AI领涨；利空：冲高回落"] \
  [--macro-summary "利多：中美缓和；利空：美联储鹰派"] \
  [--analysis '{"core_conflict":"核心矛盾...","events":[{"date":"5月15日","event":"事件","impact":"影响"}],"advice":["建议1","建议2"]}'] \
  -o /tmp/scores.json
```
```

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/SKILL.md
git commit -m "feat: add Step 3.5 reverse check, mandatory items and balance rules to SKILL"
```

---

### Task 3: Add validation logic to compute_scores.py

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/compute_scores.py`

- [ ] **Step 1: Add validation constants after DATA_QUALITY_WEIGHTS (line 58)**

```python
# --- Dimension validation rules ---

COVERAGE_RULES = {
    "capital_flow": {"min_items": 2, "penalty_factor": 0.5},
    "fundamental":  {"min_items": 2, "penalty_factor": 0.5},
    "sentiment":    {"min_items": 2, "penalty_factor": 0.5},
    "macro":        {"min_items": 2, "penalty_factor": 0.5},
}

EVENT_CAPS = {
    "macro":       {"single_event_max": 1.5, "high_score_min_signals": 2},
    "capital_flow":{"single_event_max": 1.0, "high_score_min_signals": 2},
    "fundamental": {"single_event_max": 1.0, "high_score_min_signals": 2},
    "sentiment":   {"single_event_max": 1.0, "high_score_min_signals": 2},
}
```

- [ ] **Step 2: Add validation functions after determine_confidence (line 107)**

```python
def apply_coverage_penalty(dim, covered_items, raw_score):
    """Apply score penalty when dimension has insufficient mandatory item coverage."""
    rule = COVERAGE_RULES.get(dim)
    if not rule:
        return raw_score, None
    if covered_items < rule["min_items"]:
        adjusted = raw_score * rule["penalty_factor"]
        warning = f"{dim}仅覆盖{covered_items}项，低于最低{rule['min_items']}项，得分×0.5"
        return round(adjusted, 2), warning
    return raw_score, None


def validate_event_cap(dim, score, signal_count):
    """Cap dimension score when backed by insufficient independent signals."""
    cap = EVENT_CAPS.get(dim)
    if not cap:
        return score, []
    adjusted = score
    warnings = []
    if signal_count < cap["high_score_min_signals"] and abs(score) > cap["single_event_max"]:
        direction = 1 if score > 0 else -1
        adjusted = round(direction * cap["single_event_max"], 2)
        warnings.append(
            f"{dim}得分{score}超出单事件封顶{cap['single_event_max']}且仅{signal_count}条信号，已修正为{adjusted}"
        )
    return adjusted, warnings


def validate_dimension_scores(scores, signals_info, self_check):
    """Validate all non-technical dimension scores for reasonableness.

    Args:
        scores: dict of {dim: score}
        signals_info: dict of {dim: {"count": int, "has_counter": bool}}
        self_check: dict of {dim: {"counter_found": bool, "adjusted": bool, "covered_items": int, ...}}

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

        # 1. Coverage penalty
        covered_items = check.get("covered_items", signal_count)
        score, w = apply_coverage_penalty(dim, covered_items, score)
        if w:
            warnings.append(w)

        # 2. Event cap
        score, ws = validate_event_cap(dim, score, signal_count)
        warnings.extend(ws)

        # 3. Bullish/bearish balance: missing counter-signal reduces consistency
        if not has_counter:
            penalty_factors.append(0.6)
            warnings.append(f"{dim}缺少反向信号，一致性因子×0.6")

        # 4. Self-check adjustments
        if check.get("adjusted") and check.get("revised") is not None:
            score = round(check["revised"], 2)
            warnings.append(f"{dim}经逆向校验调整得分至{score}")

        adjusted[dim] = score

    # Technical dimension: no validation (script-computed)
    adjusted["technical"] = scores.get("technical", 0)

    # Overall confidence penalty
    overall_penalty = 1.0
    if penalty_factors:
        overall_penalty = min(penalty_factors)

    return adjusted, warnings, overall_penalty
```

- [ ] **Step 3: Add CLI arguments for --self-check and --signals-info**

After line 244 (`parser.add_argument("--risks", ...)`), add:

```python
    parser.add_argument("--self-check", default=None,
                        help="JSON with self-check results per dimension: {dim: {counter_found, adjusted, covered_items, original, revised}}")
    parser.add_argument("--signals-info", default=None,
                        help="JSON with signal counts per dimension: {dim: {count, has_counter}}")
```

- [ ] **Step 4: Parse new arguments and call validation in main()**

After the automated capital flow scoring block (after line 361), add:

```python
    # Parse self-check and signals-info
    self_check = {}
    if args.self_check:
        try:
            self_check = json.loads(args.self_check)
        except json.JSONDecodeError:
            pass
    signals_info = {}
    if args.signals_info:
        try:
            signals_info = json.loads(args.signals_info)
        except json.JSONDecodeError:
            pass

    # Validate dimension scores
    validation_warnings = []
    confidence_penalty = 1.0
    if self_check or signals_info:
        scores, validation_warnings, confidence_penalty = validate_dimension_scores(
            scores, signals_info, self_check
        )
```

- [ ] **Step 5: Apply confidence penalty to confidence determination**

Replace lines 382-383 (confidence calculation) with:

```python
    consistency = summary.get("consistency", 0)
    # Apply validation penalty to consistency before confidence determination
    adjusted_consistency = consistency * confidence_penalty if confidence_penalty < 1.0 else consistency
    confidence = determine_confidence(abs(composite), adjusted_consistency)

    # If self-check resulted in score adjustments, downgrade confidence one level
    any_adjusted = any(v.get("adjusted") for v in self_check.values()) if self_check else False
    if any_adjusted:
        confidence_map = {"高": "中", "中": "低", "低": "低"}
        confidence = confidence_map.get(confidence, confidence)
```

- [ ] **Step 6: Add validation_warnings to output JSON**

In the output dict (after line 456 `"automated_sources"`), add:

```python
        # Validation results
        "validation_warnings": validation_warnings,
        "confidence_penalty": confidence_penalty,
```

- [ ] **Step 7: Print validation warnings to stdout**

After line 490 (`print(f"Output: {output_path}")`), add:

```python
    if validation_warnings:
        print(f"Validation warnings: {validation_warnings}")
    if confidence_penalty < 1.0:
        print(f"Confidence penalty: {confidence_penalty}")
```

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/stock-trend/scripts/compute_scores.py
git commit -m "feat: add dimension score validation (coverage, event cap, balance) to compute_scores"
```

---

### Task 4: Add validation warnings to report template and generate_report.py

**Files:**
- Modify: `.claude/skills/stock-trend/assets/report-template.md`
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py`

- [ ] **Step 1: Add validation warnings section to report template**

After line 89 (`{{/综合研判}}`), before the `---` separator, insert:

```markdown
{{#校验警告}}
## 校验提示

{{#校验警告列表}}
- {{警告项}}
{{/校验警告列表}}
{{/校验警告}}
```

- [ ] **Step 2: Add validation warnings context in generate_report.py build_context()**

After line 460 (`context["操作建议列表"] = []`) in build_context(), before the return statement, add:

```python
    # Validation warnings from scores file
    validation_warnings = []
    if args.scores_file:
        try:
            with open(args.scores_file, "r", encoding="utf-8") as f:
                sf = json.load(f)
            validation_warnings = sf.get("validation_warnings", [])
        except Exception:
            pass
    context["校验警告"] = len(validation_warnings) > 0
    context["校验警告列表"] = [{"警告项": w} for w in validation_warnings] if validation_warnings else []
```

- [ ] **Step 3: Also pass validation_warnings from scores_file in the scores_file loading block**

In the scores_file loading block (after line 562 where analysis is loaded), add:

```python
            # Validation warnings (no override needed, read in build_context)
```

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-trend/assets/report-template.md .claude/skills/stock-trend/scripts/generate_report.py
git commit -m "feat: render validation warnings in report output"
```

---

### Task 5: End-to-end verification with 159740.SZ

**Files:**
- No file changes — verification only

- [ ] **Step 1: Run compute_scores.py with test data to verify validation logic**

Create a quick test by running compute_scores.py with mock self-check data simulating the 2330 report's macro score of 2.0 with only 1 signal:

```bash
# First, need a technical.json to exist (from any previous run or create minimal one)
# Use the existing 159740.SZ technical data if available
python3 .claude/skills/stock-trend/scripts/compute_scores.py \
  --technical /tmp/technical.json \
  --capital-flow-score 0.5 \
  --fundamental-score 1.0 \
  --sentiment-score -0.5 \
  --macro-score 2.0 \
  --asset-type etf \
  --signals-info '{"macro":{"count":1,"has_counter":false},"capital_flow":{"count":2,"has_counter":true},"fundamental":{"count":2,"has_counter":true},"sentiment":{"count":2,"has_counter":true}}' \
  --self-check '{"capital_flow":{"counter_found":true,"adjusted":false,"covered_items":2},"fundamental":{"counter_found":true,"adjusted":false,"covered_items":2},"sentiment":{"counter_found":false,"adjusted":true,"covered_items":2,"original":-0.5,"revised":0},"macro":{"counter_found":false,"adjusted":false,"covered_items":1}}' \
  -o /tmp/scores_test.json
```

Expected:
- macro score 2.0 capped to 1.5 (single event cap with only 1 signal)
- macro also gets coverage penalty (covered_items=1 < min=2) → 1.5 × 0.5 = 0.75
- sentiment missing counter → confidence penalty 0.6
- validation_warnings list non-empty
- Output printed to stdout includes warnings

- [ ] **Step 2: Verify scores_test.json output**

```bash
cat /tmp/scores_test.json | python3 -c "import sys,json; d=json.load(sys.stdin); print('macro:', d['scores']['macro']); print('warnings:', d['validation_warnings']); print('penalty:', d['confidence_penalty'])"
```

Expected output shows macro score adjusted and warnings present.

- [ ] **Step 3: Generate a test report with warnings**

```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --technical /tmp/technical.json \
  --scores-file /tmp/scores_test.json \
  --ts-code 159740.SZ --stock-name '恒生科技ETF大成' \
  --output-md /tmp/test_report.md
```

Verify the generated markdown contains a "校验提示" section with the warnings.

- [ ] **Step 4: Run full /stock-trend 159740 to verify end-to-end**

```bash
# This is a manual verification — trigger the skill and check that:
# 1. Each dimension summary contains 利多/利空 markers
# 2. No single event score exceeds cap
# 3. Mandatory items coverage ≥ 2
# 4. Step 3.5 reverse check is executed
# 5. Validation warnings appear in report if any rules are violated
```

- [ ] **Step 5: Final commit (if any adjustments needed from verification)**

```bash
git add -A
git commit -m "fix: adjustments from end-to-end verification of report reliability"
```

---

## Self-Review

### Spec Coverage

| Spec Section | Task |
|---|---|
| 3.1 必检项清单 | Task 1 (docs) + Task 3 (code) |
| 3.2 ETF维度归属 | Task 1 (docs) |
| 3.3 单事件封顶 | Task 1 (docs) + Task 3 (code) |
| 3.4 正反强制 | Task 1 (docs) + Task 2 (SKILL.md) |
| 3.5 Step 3.5 逆向校验 | Task 2 (SKILL.md) + Task 3 (code) |
| 3.6 合理性校验 | Task 3 (code) |
| 3.7 修改文件清单 | All tasks |
| 5 验证方式 | Task 5 |

### Placeholder Scan

No TBD/TODO found. All code blocks contain complete implementations.

### Type Consistency

- `validate_dimension_scores` returns `(adjusted_scores: dict, warnings: list, confidence_penalty: float)` — matches usage in Task 3 Steps 4-6
- `apply_coverage_penalty` returns `(score: float, warning: str|None)` — matches call in `validate_dimension_scores`
- `validate_event_cap` returns `(score: float, warnings: list)` — matches call in `validate_dimension_scores`
- `self_check` dict keys `counter_found`, `adjusted`, `covered_items`, `original`, `revised` — consistent between SKILL.md (Task 2) and compute_scores.py (Task 3)
- `signals_info` dict keys `count`, `has_counter` — consistent between SKILL.md and compute_scores.py
- Template variable `校验警告` / `校验警告列表` — consistent between template (Task 4 Step 1) and build_context (Task 4 Step 2)