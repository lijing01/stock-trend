# Candidate Risk–Reward Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the fixed, synthetic `R:R 2.0` shown in daily-candidate reports. Compute reward from the planned entry price and independently derived targets, disclose target provenance, and prevent non-structural targets from being promoted to executable recommendations.

**Architecture:** Keep the existing technical risk/reward calculator as the single target-selection engine, but give it an optional entry-price reference. `candidate_trade_plan` passes the actual upper bound of the entry range, keeps resistance targets and ATR projections distinguishable, and never manufactures `entry + n × (entry - stop)` targets. The scanner carries the enriched plan unchanged; the HTML/text renderer shows provenance and renders an unavailable R:R explicitly instead of a misleading number.

**Tech Stack:** Python 3.10, existing `technical.py` indicators, standalone repository tests, no new dependencies.

---

## Scope and acceptance rules

The repair deliberately prefers an unavailable R:R to an invented one:

- `resistance` targets are price levels above the planned entry range. A complete, validated resistance ladder may be eligible for a `buy` action.
- `atr_projection` targets are calculated from ATR rather than stop-loss distance. Their displayed R:R is mathematically real but they are marked “observation only” and cannot by themselves make a candidate actionable.
- `unavailable` means there is no valid ascending target ladder above entry. Its R:R is `None`/`—`, never `2.0`.
- `risk_reward.recomputed` is always `(moderate_target - entry_high) / (entry_high - stop)`, rounded only after the calculation. It must never be set from a configured constant or generated as `entry_high + 2R`.
- Existing intraday, market-regime, timing, position-size, and event-risk gates remain in force. This plan changes target integrity only; it does not relax the trade-selection policy.

## File map

| File | Responsibility after the change |
| --- | --- |
| `.claude/skills/stock-trend/scripts/analysis/technical.py` | Select targets and R:R against an optional planned entry reference; report target provenance. |
| `.claude/skills/stock-trend/scripts/core/candidate_trade_plan.py` | Build and validate plans without synthetic `nR` target fallback; gate executable actions by provenance. |
| `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` | Render source-labelled R:R and report a target-source audit. |
| `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py` | Unit and regression coverage for entry-referenced, non-synthetic plans. |
| `.claude/skills/stock-trend/tests/test_daily_candidates.py` | Rendering and report-audit coverage. |
| `.claude/skills/stock-trend/tests/test_stock_scanner.py` | Integration coverage that the scanner preserves the trade-plan provenance and action gate. |
| `.claude/skills/stock-trend/SKILL.md` | User-facing explanation of target provenance and the meaning of `R:R —`. |

## Task 1: Make target selection entry-referenced and provenance-aware

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py`
- Modify: `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py`

- [ ] **Step 1: Add failing tests for reference price and target source.**

  Add a focused test near the existing risk/reward tests. Use a small deterministic DataFrame, mock or construct support/resistance levels with resistances both below and above the planned entry, and assert all of the following:

  ```python
  result = calc_risk_reward(
      df,
      atr=2.0,
      support_resistance={"resistance": [{"price": 103.0}, {"price": 108.0}, {"price": 112.0}, {"price": 116.0}]},
      direction="bullish",
      is_etf=False,
      entry_price=105.0,
  )

  assert result["entry_reference"] == 105.0
  assert result["target_source"] == "resistance"
  assert result["target_conservative"] == 108.0
  assert result["risk_reward_ratio"] == round(
      (result["target_moderate"] - 105.0) / (105.0 - result["stop_loss"]), 2
  )
  ```

  Add a second test with no resistance data and a valid ATR. Assert `target_source == "atr_projection"`, its three targets are strictly ascending and above `entry_reference`, and `risk_reward_ratio` equals the direct moderate-target formula rather than a fixed expected ratio.

- [ ] **Step 2: Extend `calc_risk_reward` without changing legacy callers.**

  Change the signature to accept an optional keyword argument:

  ```python
  def calc_risk_reward(
      df,
      atr,
      support_resistance,
      direction="bullish",
      is_etf=False,
      entry_price=None,
  ):
  ```

  Directly after `curr_close` is obtained, add a finite positive reference-price guard. Existing callers continue to use `curr_close`:

  ```python
  try:
      entry_reference = float(entry_price) if entry_price is not None else float(curr_close)
  except (TypeError, ValueError):
      entry_reference = float(curr_close)
  if not math.isfinite(entry_reference) or entry_reference <= 0:
      entry_reference = float(curr_close)
  ```

  When `entry_price` is supplied, use `entry_reference`, not `curr_close`, for these calculations:

  ```python
  risk = entry_reference - stop_loss
  resistance_prices = sorted(
      price for price in resistance_prices if price > entry_reference
  )
  reward = target_moderate - entry_reference
  rr_ratio = reward / risk if risk > 0 else None
  ```

  Preserve the legacy target-selection behaviour when `entry_price is None`. In the new entry-referenced branch, do not mix actual resistance targets with ATR projections: set `target_source = "resistance"` only if the complete ascending three-level ladder is selected from resistances above entry. Use `target_source = "atr_projection"` only if no resistance above entry exists and ATR supplies the entire ladder. Base that fallback on `entry_reference`, for example:

  ```python
  target_conservative = entry_reference + atr
  target_moderate = entry_reference + 2 * atr
  target_aggressive = entry_reference + 3 * atr
  ```

  When a partial resistance set cannot produce the complete ladder, or neither source can produce valid targets, use `target_source = "unavailable"` and leave all target and R:R values as `None`. Return these two new fields in every branch:

  ```python
  "entry_reference": round(entry_reference, 2),
  "target_source": target_source,
  ```

  Keep all existing warning text, but append a precise warning for unavailable targets (`"目标位不可用，不能评估盈亏比"`). Do not alter stop-loss selection logic in this task.

- [ ] **Step 3: Run the targeted unit test.**

  Run:

  ```bash
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
  ```

  Expected: all existing tests plus the two new target-source tests pass. Any legacy expectation that assumes close-referenced output must continue passing when `entry_price` is omitted.

- [ ] **Step 4: Commit the isolated calculator contract.**

  ```bash
  git add .claude/skills/stock-trend/scripts/analysis/technical.py .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
  git commit -m "fix: reference risk reward to planned entry"
  ```

## Task 2: Remove synthetic target generation from candidate trade plans

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/core/candidate_trade_plan.py`
- Modify: `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py`

- [ ] **Step 1: Write failing regressions for the current 2R failure mode.**

  Add three tests:

  1. A risk result whose targets lie at or below `entry_high` must produce `target_source == "unavailable"`, `risk_reward["recomputed"] is None`, and no numeric synthetic targets.
  2. A complete resistance ladder above `entry_high` must retain its supplied prices and calculate R:R with `entry_high` and `stop`, not the close price.
  3. A complete `atr_projection` ladder may display a calculated R:R, but the plan action must be `wait` and validation must add `trade_plan_target_source_not_executable` when action is forced to `buy`.

  The core assertions for the first regression are:

  ```python
  assert plan["target_source"] == "unavailable"
  assert plan["targets"] == {
      "conservative": None,
      "primary": None,
      "aggressive": None,
  }
  assert plan["risk_reward"]["recomputed"] is None
  assert plan["action"] == "wait"
  ```

- [ ] **Step 2: Pass the actual entry high to the calculator.**

  In `build_candidate_trade_plan`, calculate `low` and `high` before calling `calc_risk_reward`, then pass the actual worst acceptable entry:

  ```python
  risk = calc_risk_reward(
      df,
      atr_value,
      levels,
      direction="bullish",
      is_etf=False,
      entry_price=high,
  )
  ```

  Keep `stop = risk["stop_loss"]` after this call. Stop selection is already independent of target selection inside `calc_risk_reward`; do not call the helper twice or duplicate its stop-loss logic in the scanner.

- [ ] **Step 3: Replace the `synthetic_fallback` branch with nullable target handling.**

  Delete the current branch that builds targets with `high + one_r`, `high + 2 * one_r`, and `high + 3 * one_r`. Replace it with this exact target-shape policy:

  ```python
  source = str(risk.get("target_source") or "unavailable")
  supplied_targets = [
      _finite_positive(risk.get("target_conservative")),
      _finite_positive(risk.get("target_moderate")),
      _finite_positive(risk.get("target_aggressive")),
  ]
  has_valid_ladder = (
      all(supplied_targets)
      and high < supplied_targets[0] < supplied_targets[1] < supplied_targets[2]
  )
  if not has_valid_ladder:
      source = "unavailable"
      supplied_targets = [None, None, None]
  ```

  Add this small helper beside `_finite_positive` so nullable prices retain the current four-decimal plan precision:

  ```python
  def _round_optional_price(value):
      price = _finite_positive(value)
      return round(price, 4) if price is not None else None
  ```

  Persist the source and a human-readable reason. The public plan keys remain `conservative`, `primary`, and `aggressive` to avoid a schema break:

  ```python
  "target_source": source,
  "target_reason": None if has_valid_ladder else "没有高于计划入场价的有效目标梯度",
  "targets": {
      "conservative": _round_optional_price(supplied_targets[0]),
      "primary": _round_optional_price(supplied_targets[1]),
      "aggressive": _round_optional_price(supplied_targets[2]),
  },
  ```

  Do not use `0` as a missing-price sentinel.

- [ ] **Step 4: Make recomputation and actionability source-aware.**

  Calculate recomputed R:R only for a valid ladder:

  ```python
  recomputed_rr = (
      round((supplied_targets[1] - high) / (high - stop), 2)
      if has_valid_ladder and high > stop
      else None
  )
  executable_target = source == "resistance" and recomputed_rr is not None
  if action == "buy" and not executable_target:
      action = "wait"
      reasons.append("目标仅为ATR投射或不可用，不能形成可执行交易计划")
  ```

  Update `validate_trade_plan` to avoid arithmetic on `None`. It must:

  - append `trade_plan_targets_unavailable` for an absent ladder;
  - append `trade_plan_target_source_not_executable` when a `buy` plan source is not `resistance`;
  - retain `trade_plan_rr_below_min` only when a numeric recomputed value exists;
  - compare stored and recomputed R:R only when both are numeric.

- [ ] **Step 5: Run targeted regression tests.**

  Run:

  ```bash
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
  ```

  Expected: the former synthetic fallback test is replaced by assertions that no code path emits `target_source == "synthetic_fallback"`, and calculated values vary with target/stop geometry.

- [ ] **Step 6: Commit the plan-integrity change.**

  ```bash
  git add .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
  git commit -m "fix: remove synthetic candidate targets"
  ```

## Task 3: Preserve the gate and make the report auditable

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: Add scanner integration coverage.**

  Build one candidate with a valid resistance plan and one with an ATR-only plan. Assert the scanner output preserves `trade_plan["target_source"]`, and assert only the resistance candidate can satisfy the existing complete/actionable trade-plan gate. This test must exercise the public scanner result rather than calling the core builder directly.

- [ ] **Step 2: Keep target provenance in scanner diagnostics.**

  Where `stock_scanner.py` records `trade_plan_status` and `trade_plan_reasons`, add the source value to the diagnostic payload:

  ```python
  item["trade_plan_target_source"] = (
      (item.get("trade_plan") or {}).get("target_source") or "unavailable"
  )
  ```

  Do not change ranking scores or force a candidate into the actionable list in this file. The existing validator remains the authority for the decision.

- [ ] **Step 3: Make report text truthful when targets are not executable.**

  In `_trade_plan_text` in `daily_candidates.py`, add a source-label map:

  ```python
  target_source_labels = {
      "resistance": "阻力位",
      "atr_projection": "ATR投射（仅观察）",
      "unavailable": "目标不可用",
  }
  ```

  Render R:R defensively:

  ```python
  rr_value = (plan.get("risk_reward") or {}).get("recomputed")
  rr_text = f"{float(rr_value):.2f}" if isinstance(rr_value, (int, float)) else "—"
  source_text = target_source_labels.get(plan.get("target_source"), "目标不可用")
  ```

  Include `目标来源 {source_text}` next to the R:R. For `unavailable`, render targets as `—` and include `target_reason`; never render `None`, `0`, or a made-up number. Preserve the existing position cap such as `仓位≤0.0%` because it remains a separate execution-policy signal.

- [ ] **Step 4: Add a compact source audit to generated reports.**

  Count candidate plans by `target_source` at report generation and output a single summary line, for example:

  ```text
  目标来源审计：阻力位 3｜ATR投射（仅观察） 5｜目标不可用 12
  ```

  Count missing plans as `目标不可用`. Add the same content to HTML summary data so the report makes it obvious why a list has few tradeable rows.

- [ ] **Step 5: Add rendering regressions.**

  In `test_daily_candidates.py`, add a table-driven test for all three sources. It must assert:

  ```python
  assert "R:R —" in unavailable_text
  assert "目标来源 目标不可用" in unavailable_text
  assert "ATR投射（仅观察）" in atr_text
  assert "R:R 2.0" not in unavailable_text
  ```

  Add a report-summary assertion for the audit counts. Update existing expectations only where the newly added source label changes the intended output.

- [ ] **Step 6: Run scanner and report tests.**

  Run:

  ```bash
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: all tests pass; test output demonstrates that an ATR-only plan remains observation-only and unavailable targets are visible as `R:R —`.

- [ ] **Step 7: Commit rendering and diagnostics.**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_stock_scanner.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "feat: disclose candidate target provenance"
  ```

## Task 4: Document the contract and run repository quality gates

**Files:**

- Modify: `.claude/skills/stock-trend/SKILL.md`
- Verify: `.claude/skills/stock-trend/tests/test_stock_trend.py`
- Verify: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Update the candidate-report guidance.**

  In the daily-candidate/report output section of `SKILL.md`, add this concise contract:

  ```markdown
  - R:R 以计划入场区间上沿、止损位和目标二计算；不是固定收益倍数。
  - 报告必须标注目标来源：`阻力位`可用于可执行计划，`ATR投射（仅观察）`只作情景参考，`目标不可用`显示 `R:R —`。
  - 不得用 `entry + nR` 生成目标来满足最低 R:R 门槛；目标不足时降为观察，而不是伪造可交易结论。
  ```

- [ ] **Step 2: Run the required project quality gates.**

  The repository requires both commands after Python changes:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Then run the focused suite once more:

  ```bash
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Do not regenerate golden snapshots merely to make the diff pass. If a golden diff is intentional, inspect and explain the exact output change before deciding whether to update the snapshot.

- [ ] **Step 3: Manually inspect a regenerated report only after automated tests pass.**

  Generate a fresh candidate report through the normal daily-candidate command. Check at least one row for each available source and confirm:

  - target prices are strictly above the entry-high price;
  - `R:R` changes when entry, stop, or target changes;
  - ATR-only rows cannot say `可执行`/`买入`;
  - unavailable rows show `R:R —` and a reason;
  - `仓位≤0.0%` is still explained by policy state, not by R:R formatting.

- [ ] **Step 4: Commit documentation after verification.**

  ```bash
  git add .claude/skills/stock-trend/SKILL.md
  git commit -m "docs: clarify candidate risk reward sources"
  ```

## Completion criteria

- No production code contains `synthetic_fallback`, `high + 2 * one_r`, or an equivalent target ladder derived from stop distance.
- The same target/stop prices yield a correct R:R based on the entry-range upper bound, not on close.
- Every report R:R has a visible source label; unavailable target ladders render as `—`.
- An ATR projection cannot independently turn a candidate into a `buy`/actionable recommendation.
- The targeted tests and both repository-required quality gates pass without unreviewed golden changes.
- The report remains educational/reference-only and is not investment advice.
