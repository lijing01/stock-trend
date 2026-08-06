# Wyckoff Phase Classification Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除维科夫阶段判定中的吸筹优先偏置，使箱体、突破和破位阶段互斥，并让低证据形态回退为 `phase_unknown`。

**Architecture:** 保留现有 swing、箱体、VSA 和评分结构，仅在 `analysis/wyckoff.py` 内增加价格位置路由和候选阶段仲裁。箱体内的吸筹/派发必须提供方向性量价证据，不再用 LPS/LPSY 兜底；突破、破位和箱体内分类由同一个入口互斥调度。先用合成反例锁定行为，再通过现有历史回放观察信号数量、胜率和阶段分布，暂不调整下游复合评分权重。

**Tech Stack:** Python 3、标准库 `unittest`、现有 Wyckoff 历史回放脚本。

---

## Scope and success criteria

- 同一时点最多产生一个 primary phase；不再由函数调用顺序决定吸筹或派发。
- 箱体内缺乏明确量价方向时返回 `phase_unknown`，不得默认返回 LPS/LPSY。
- 收盘价高于箱顶但尚未达到确认阈值时不得标为吸筹；下破过渡区只有放量 SOW 可以成立，否则保持 unknown。
- `pre_markup`、`pre_markdown` 要么有可验证的返回条件，要么从买点/评分事实源移除；本计划选择补齐返回条件。
- 置信度由价格位置、量价证据、结构事件三项组成，并限制在 `[0, 1]`。
- 新增冲突、模糊箱体、JAC、BU、SOW、Breakdown 和阶段可达性回归测试。
- 两个仓库质量门通过；历史回放输出信号数、阶段分布、胜率和相对基线差异，供人工评估，不以提高胜率为强行验收条件。

## Files

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` — 阶段证据、互斥路由、置信度。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py` — 分类器单元测试和完整流水线反例。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` — 阶段分布与信号选择回归断言。
- Modify: `docs/wyckoff-analysis-design.md` — 同步实际决策树、过渡区和置信度定义。

### Task 1: Lock the known misclassifications with failing tests

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Add a reusable conflict fixture**

```python
def _range_conflict_fixture(latest_close=105.0):
    n = 100
    closes = [105.0] * (n - 1) + [latest_close]
    volumes = [100.0] * n
    lows = [99.0] * n
    highs = [111.0] * n
    atr = [2.0] * n
    swings = [
        {"index": 80, "type": "low", "price": 101.0,
         "volume_ratio": 1.0, "is_climax": False},
        {"index": 90, "type": "high", "price": 109.0,
         "volume_ratio": 1.0, "is_climax": False},
    ]
    trading_range = {
        "support": 100.0, "resistance": 110.0,
        "support_idx": 20, "resistance_idx": 50,
        "duration_bars": 70, "touch_count": 4,
        "is_clear_range": True,
    }
    return swings, closes, volumes, lows, highs, trading_range, atr, n - 1
```

- [ ] **Step 2: Add failing tests for ambiguous and transitional states**

```python
class TestPhaseClassificationConflicts(unittest.TestCase):
    def test_neutral_range_is_not_default_accumulation(self):
        args = _range_conflict_fixture()
        result = classify_accumulation(*args)
        self.assertIsNone(result)

    def test_neutral_range_is_not_default_distribution(self):
        swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture()
        result = classify_distribution(
            swings, closes, volumes, lows, highs, tr, atr, idx
        )
        self.assertIsNone(result)

    def test_unconfirmed_breakout_is_not_lps(self):
        args = _range_conflict_fixture(latest_close=111.0)
        result = classify_accumulation(*args)
        self.assertIsNone(result)
```

- [ ] **Step 3: Run the targeted tests and confirm the current behavior fails**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: FAIL because the current classifiers return `("lps", 0.5)` and `("lpsy", 0.4)`.

- [ ] **Step 4: Commit only the regression tests**

```bash
git add .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "test: lock Wyckoff phase conflicts"
```

### Task 2: Require directional evidence inside a trading range

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:319-475`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Add small evidence helpers next to the phase classifiers**

```python
def _volume_trend_ratio(volumes: list, latest_idx: int, window: int = 20) -> float:
    end = latest_idx + 1
    recent = volumes[max(0, end - window):end]
    if len(recent) < 10:
        return 1.0
    half = len(recent) // 2
    older_avg = sum(recent[:half]) / half
    newer_avg = sum(recent[half:]) / (len(recent) - half)
    return newer_avg / older_avg if older_avg > 0 else 1.0


def _close_location(close: float, trading_range: dict) -> float:
    height = trading_range["resistance"] - trading_range["support"]
    if height <= 0:
        return 0.5
    return (close - trading_range["support"]) / height
```

- [ ] **Step 2: Replace the unconditional accumulation fallback with evidence gates**

After SC/AR/ST/Spring detection, use:

```python
    volume_ratio = _volume_trend_ratio(volumes, latest_idx)
    close_location = _close_location(latest_close, trading_range)
    near_support = abs(latest_close - range_support) <= latest_atr * 0.5

    if near_support and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPS, 0.7)
    if volume_ratio <= 0.8 and close_location >= 0.55:
        return (SUB_PRE_MARKUP, 0.6)
    return None
```

Delete the existing unconditional `return (SUB_LPS, 0.5)`.

- [ ] **Step 3: Replace the unconditional distribution fallback with evidence gates**

After BC/UTAD/SOW detection, use:

```python
    volume_ratio = _volume_trend_ratio(volumes, latest_idx)
    close_location = _close_location(latest_close, trading_range)
    near_resistance = abs(latest_close - range_resistance) <= latest_atr * 0.5

    if near_resistance and volumes[latest_idx] < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPSY, 0.65)
    if volume_ratio >= 1.2 and close_location <= 0.45:
        return (SUB_PRE_MARKDOWN, 0.6)
    return None
```

Delete the existing unconditional `return (SUB_LPSY, 0.4)`.

Also require volume confirmation for the existing SOW branch:

```python
    if latest_close < range_support - latest_atr * 0.5:
        baseline_vol = _ma_of_last_n(volumes, latest_idx, 50)
        if volumes[latest_idx] > baseline_vol * 1.3:
            return (SUB_SOW, 0.7)
        return None
```

- [ ] **Step 4: Add positive tests proving LPS, PRE_MARKUP, LPSY and PRE_MARKDOWN remain reachable**

```python
def test_pre_markup_requires_contracting_volume_and_upper_half_close(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture(107.0)
    volumes[:] = [140.0] * 80 + [70.0] * 20
    self.assertEqual(
        classify_accumulation(swings, closes, volumes, lows, highs, tr, atr, idx),
        (SUB_PRE_MARKUP, 0.6),
    )


def test_pre_markdown_requires_expanding_volume_and_lower_half_close(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture(103.0)
    volumes[:] = [70.0] * 80 + [140.0] * 20
    self.assertEqual(
        classify_distribution(swings, closes, volumes, lows, highs, tr, atr, idx),
        (SUB_PRE_MARKDOWN, 0.6),
    )
```

- [ ] **Step 5: Run the full Wyckoff unit suite**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit the evidence-gate change**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: require evidence for Wyckoff range phases"
```

### Task 3: Make top-level phase routing mutually exclusive

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:726-808`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Add a single price-location router**

```python
def _route_price_location(close: float, trading_range: dict,
                          atr: float) -> str:
    support = trading_range["support"]
    resistance = trading_range["resistance"]
    buffer_size = max(atr, 0.0)
    if close > resistance + buffer_size:
        return "above_range"
    if close < support - buffer_size:
        return "below_range"
    if support - buffer_size * 0.5 <= close <= resistance + buffer_size * 0.5:
        return "in_range"
    if close > resistance + buffer_size * 0.5:
        return "upper_transition"
    return "lower_transition"
```

- [ ] **Step 2: Add candidate arbitration for in-range phases**

```python
def _choose_range_phase(accumulation: tuple | None,
                        distribution: tuple | None,
                        min_margin: float = 0.15) -> tuple | None:
    candidates = [
        (PHASE_ACCUMULATION, accumulation),
        (PHASE_DISTRIBUTION, distribution),
    ]
    valid = [(phase, result) for phase, result in candidates if result]
    if not valid:
        return None
    valid.sort(key=lambda item: item[1][1], reverse=True)
    if len(valid) > 1 and valid[0][1][1] - valid[1][1][1] < min_margin:
        return None
    phase, (sub_phase, confidence) = valid[0]
    return phase, sub_phase, confidence
```

- [ ] **Step 3: Replace sequential priority routing in `analyze_kline_dict`**

```python
    if trading_range:
        location = _route_price_location(
            closes[latest_idx], trading_range, atr_values[latest_idx] or 0.0
        )
        selected = None
        if location == "above_range":
            result = classify_markup(
                swings, closes, volumes, highs, trading_range, atr_values, latest_idx
            )
            if result:
                selected = (PHASE_MARKUP, *result)
        elif location == "below_range":
            result = classify_markdown(
                swings, closes, volumes, lows, highs, trading_range, atr_values, latest_idx
            )
            if result:
                selected = (PHASE_MARKDOWN, *result)
        elif location == "in_range":
            accumulation = classify_accumulation(
                swings, closes, volumes, lows, highs,
                trading_range, atr_values, latest_idx
            )
            distribution = classify_distribution(
                swings, closes, volumes, lows, highs,
                trading_range, atr_values, latest_idx
            )
            selected = _choose_range_phase(accumulation, distribution)
        elif location == "lower_transition":
            result = classify_distribution(
                swings, closes, volumes, lows, highs,
                trading_range, atr_values, latest_idx
            )
            if result and result[0] == SUB_SOW:
                selected = (PHASE_DISTRIBUTION, *result)

        if selected:
            phase, sub_phase, confidence = selected
```

The `upper_transition` route intentionally remains unknown until price confirms a breakout or returns to the box. The `lower_transition` route accepts only a volume-confirmed SOW; without that evidence it remains unknown.

- [ ] **Step 4: Add router and arbitration tests**

```python
def test_route_price_location_has_no_overlap(self):
    tr = {"support": 100.0, "resistance": 110.0}
    self.assertEqual(_route_price_location(105.0, tr, 2.0), "in_range")
    self.assertEqual(_route_price_location(111.0, tr, 2.0), "upper_transition")
    self.assertEqual(_route_price_location(113.0, tr, 2.0), "above_range")
    self.assertEqual(_route_price_location(99.0, tr, 2.0), "lower_transition")
    self.assertEqual(_route_price_location(97.0, tr, 2.0), "below_range")


def test_range_phase_conflict_with_small_margin_is_unknown(self):
    self.assertIsNone(_choose_range_phase((SUB_LPS, 0.6), (SUB_LPSY, 0.55)))


def test_range_phase_selects_stronger_evidence(self):
    self.assertEqual(
        _choose_range_phase((SUB_LPS, 0.75), (SUB_LPSY, 0.45)),
        (PHASE_ACCUMULATION, SUB_LPS, 0.75),
    )
```

- [ ] **Step 5: Add reachability tests for JAC, BU, SOW and Breakdown**

```python
def test_confirmed_breakout_routes_to_jac(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture()
    closes[-3:] = [109.0, 113.0, 114.0]
    result = classify_markup(swings, closes, volumes, highs, tr, atr, idx)
    self.assertEqual(result[0], SUB_JAC)


def test_old_breakout_with_low_volume_pullback_routes_to_backup(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture()
    closes[-10:] = [113.0, 114.0, 115.0, 114.5, 114.0,
                    113.5, 113.0, 112.5, 112.0, 112.0]
    volumes[-1] = 50.0
    result = classify_markup(swings, closes, volumes, highs, tr, atr, idx)
    self.assertEqual(result, (SUB_BU, 0.7))


def test_lower_transition_requires_volume_for_sow(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture(98.8)
    self.assertIsNone(
        classify_distribution(swings, closes, volumes, lows, highs, tr, atr, idx)
    )
    volumes[-1] = 200.0
    self.assertEqual(
        classify_distribution(swings, closes, volumes, lows, highs, tr, atr, idx),
        (SUB_SOW, 0.7),
    )


def test_confirmed_downside_break_routes_to_breakdown(self):
    swings, closes, volumes, lows, highs, tr, atr, idx = _range_conflict_fixture(97.0)
    self.assertEqual(
        classify_markdown(swings, closes, volumes, lows, highs, tr, atr, idx),
        (SUB_BREAKDOWN, 0.7),
    )
```

- [ ] **Step 6: Run the unit suite**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: all tests pass, including neutral and conflict cases returning unknown.

- [ ] **Step 7: Commit the mutually exclusive router**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: make Wyckoff phase routing exclusive"
```

### Task 4: Prevent stale trading ranges from controlling the current phase

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:254-293`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Restrict range detection to a recent analysis window**

Change the signature and filter the swing input before clustering:

```python
def detect_trading_range(swings: list, closes: list, atr_values: list,
                         min_touches: int = RANGE_MIN_TOUCHES,
                         min_bars: int = RANGE_MIN_BARS,
                         max_bars: int = 120) -> dict | None:
    if not closes:
        return None
    first_allowed = max(0, len(closes) - max_bars)
    recent_swings = [s for s in swings if s["index"] >= first_allowed]
    if len(recent_swings) < min_touches:
        return None
    swings = recent_swings
```

Keep the existing clustering and touch validation after this filter.

- [ ] **Step 2: Add a stale-range regression test**

```python
def test_old_swings_do_not_form_current_trading_range(self):
    closes = [100.0] * 200
    atr = [2.0] * 200
    swings = [
        {"index": 10, "type": "low", "price": 90.0},
        {"index": 35, "type": "high", "price": 110.0},
        {"index": 60, "type": "low", "price": 90.0},
        {"index": 70, "type": "high", "price": 110.0},
    ]
    self.assertIsNone(detect_trading_range(swings, closes, atr, max_bars=120))
```

- [ ] **Step 3: Run tests and commit**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: all tests pass.

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "fix: ignore stale Wyckoff trading ranges"
```

### Task 5: Make confidence and alternatives reflect available evidence

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:828-843`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Add bounded confidence composition**

```python
def _compose_confidence(structure_confidence: float,
                        trading_range: dict | None,
                        directional_vsa_count: int) -> float:
    touch_score = min((trading_range or {}).get("touch_count", 0) / 5.0, 1.0)
    vsa_score = min(directional_vsa_count / 3.0, 1.0)
    confidence = (
        structure_confidence * 0.5
        + touch_score * 0.3
        + vsa_score * 0.2
    )
    return round(min(max(confidence, 0.0), 1.0), 2)
```

- [ ] **Step 2: Apply the composed confidence only after phase selection**

Map the existing VSA `type` values to phase direction, count agreeing recent signals, then replace the raw classifier confidence:

```python
    bullish_phases = {PHASE_ACCUMULATION, PHASE_MARKUP}
    bullish_vsa = {"absorption", "no_supply", "stopping_volume"}
    bearish_vsa = {"no_demand", "upthrust"}
    expected_vsa = bullish_vsa if phase in bullish_phases else bearish_vsa
    directional_vsa_count = sum(
        1 for signal in vsa_signals[:10]
        if signal.get("type") in expected_vsa
    )
    if phase != PHASE_UNKNOWN:
        confidence = _compose_confidence(
            confidence, trading_range, directional_vsa_count
        )
```

Do not add a second direction field to the VSA output schema in this change.

- [ ] **Step 3: Populate secondary possibilities for unresolved in-range conflicts**

Replace `_choose_range_phase` with a version that returns both the selected result and ordered alternatives:

```python
def _choose_range_phase(accumulation: tuple | None,
                        distribution: tuple | None,
                        min_margin: float = 0.15) -> tuple:
    candidates = [
        (PHASE_ACCUMULATION, accumulation),
        (PHASE_DISTRIBUTION, distribution),
    ]
    valid = [(phase, result) for phase, result in candidates if result]
    valid.sort(key=lambda item: item[1][1], reverse=True)
    ranked = [
        {"phase": phase, "confidence": round(result[1], 2)}
        for phase, result in valid
    ]
    if not valid:
        return None, []
    if len(valid) > 1 and valid[0][1][1] - valid[1][1][1] < min_margin:
        return None, ranked
    phase, (sub_phase, confidence) = valid[0]
    return (phase, sub_phase, confidence), ranked[1:]
```

Update the in-range call site accordingly:

```python
            selected, secondary_possibilities = _choose_range_phase(
                accumulation, distribution
            )
```

Initialize `secondary_possibilities = []` before routing and use that variable in the result payload instead of the current literal empty list. For an unambiguous selection, only the losing candidate remains in the alternatives.

- [ ] **Step 4: Add confidence boundary and ambiguity tests**

```python
def test_composed_confidence_is_bounded(self):
    tr = {"touch_count": 10}
    self.assertEqual(_compose_confidence(2.0, tr, 10), 1.0)
    self.assertEqual(_compose_confidence(-1.0, None, 0), 0.0)


def test_ambiguous_range_keeps_ranked_alternatives(self):
    selected, alternatives = _choose_range_phase(
        (SUB_LPS, 0.60), (SUB_LPSY, 0.55)
    )
    self.assertIsNone(selected)
    self.assertEqual(
        [item["phase"] for item in alternatives],
        [PHASE_ACCUMULATION, PHASE_DISTRIBUTION],
    )
```

- [ ] **Step 5: Run unit tests and commit**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: all tests pass and ambiguity remains visible without becoming a buy signal.

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: calibrate Wyckoff phase confidence"
```

### Task 6: Update design documentation and verify downstream behavior

**Files:**
- Modify: `docs/wyckoff-analysis-design.md:188-230`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`

- [ ] **Step 1: Document the mutually exclusive decision sequence**

Replace the current decision-tree section with these explicit rules:

```markdown
1. `close > resistance + 1 ATR`：仅进入 Markup 分类。
2. `close < support - 1 ATR`：仅进入 Markdown 分类。
3. `support - 0.5 ATR <= close <= resistance + 0.5 ATR`：同时计算
   Accumulation/Distribution 证据，置信度差小于 0.15 时返回 Unknown。
4. 上方 0.5–1 ATR 过渡区保持 Unknown；下方过渡区仅允许放量 SOW。
5. 箱体内无方向性量价证据时返回 Unknown，不使用 LPS/LPSY 兜底。
6. 置信度 = 结构证据 50% + 箱体触碰质量 30% + 同向 VSA 20%。
```

- [ ] **Step 2: Add a backtest-level assertion that unknown phases never become buy signals**

```python
def test_ambiguous_unknown_phase_is_not_selected_as_signal():
    analysis = {
        "phase": {
            "primary": "phase_unknown",
            "primary_sub_phase": "",
            "confidence": 0.0,
            "secondary_possibilities": [
                {"phase": "accumulation", "confidence": 0.6},
                {"phase": "distribution", "confidence": 0.55},
            ],
        },
        "wyckoff_score": 0.0,
    }
    self.assertIsNone(_classify_signal(analysis, min_confidence=0.3))
```

- [ ] **Step 3: Run all targeted tests**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
```

Expected: all tests pass.

- [ ] **Step 4: Run the repository-required quality gates**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: both commands exit 0. Do not regenerate golden snapshots to hide a failure; inspect every changed phase, confidence and report field.

- [ ] **Step 5: Run a representative historical replay and record comparison metrics**

Use the same liquid-stock universe and date window before and after the change:

```bash
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py \
  --codes 600519,000001,000858,600036,601318 \
  --lookback-days 240 \
  --eval-windows 5,10,20 \
  --min-confidence 0.3 \
  --output /tmp/wyckoff-phase-reliability.json
```

Expected review fields:

- 总信号数不得因异常兜底而增加。
- `phase_unknown` 比例允许上升，这是降低伪精确度的预期结果。
- LPS/LPSY 信号必须有对应方向性证据。
- 记录 5/10/20 日胜率、平均收益、相对基线 alpha 和各子阶段样本数。
- 任一子阶段少于 30 个样本时只标记“样本不足”，不据此调整阈值。

- [ ] **Step 6: Commit documentation and integration verification**

```bash
git add docs/wyckoff-analysis-design.md .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "docs: define reliable Wyckoff phase routing"
```

## Deferred decisions

- 暂不调整维科夫在候选股复合分中的 25% 权重；先获取修正后回测数据。
- 暂不把阈值做成配置项，避免在缺少样本时引入不可验证的灵活性。
- 暂不重构 `wyckoff.py` 文件结构；本轮保持小而可审查的行为修复。
- 若完整候选宇宙回放显示某子阶段样本数 ≥30 且持续无 edge，再单独制定阈值/权重校准计划。

## Final verification checklist

- [ ] 中性箱体不再默认吸筹或派发。
- [ ] 过渡区不再产生 LPS/JAC 冲突。
- [ ] 上破、下破、箱体内三个路径互斥。
- [ ] `pre_markup` 与 `pre_markdown` 均有测试覆盖且可达。
- [ ] Unknown 和 secondary possibilities 不通过买点漏斗。
- [ ] 维科夫、回测、scanner、score mode 测试通过。
- [ ] `test_stock_trend.py` 和 golden diff 两个质量门通过。
- [ ] 历史回放比较已记录，样本不足项没有被过度解读。
