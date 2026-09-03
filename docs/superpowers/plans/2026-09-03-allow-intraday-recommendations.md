# Allow Intraday Recommendations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow eligible waiting/actionable recommendations during trading hours while retaining provisional top-level warnings, zero-risk official snapshot protection, and a separate row-level “盘中临时状态” diagnostic.

**Architecture:** Keep `is_recommendation_session()` and the `provisional` policy marker, but stop replacing the regime-derived mode and limits during market hours. Keep the existing report banner and section suffix driven by `policy.provisional`; route `intraday_provisional` out of the row’s data-error bucket into a dedicated transient-status bucket. Preserve `save_snapshot_if_official()` behavior so intraday runs remain excluded from formal recommendation history.

**Tech Stack:** Python 3, `unittest`, repository-local daily candidate scanner.

---

### Task 1: Lock the requested intraday behavior with regression tests

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2214-2258`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2740-2770`

- [x] **Step 1: Change the strong and neutral intraday policy assertions**

The strong intraday test must assert `actionable`, limit `5`, portfolio cap `60`, and `provisional_target_mode == "actionable"`; the neutral test must assert `waiting_trigger`, limit `2`, portfolio cap `30`, and `provisional_target_mode == "waiting_trigger"`. Both continue asserting `provisional` and `intraday_provisional`.

- [x] **Step 2: Add a row-diagnostic regression test**

Call `_candidate_diagnostic_text()` with an item whose `observation_reasons` contains `intraday_provisional` and `history_insufficient`; assert the result contains `盘中临时状态：盘中数据尚未收盘确认`, does not contain `数据问题/异常：盘中数据尚未收盘确认`, and still retains the history warning.

- [x] **Step 3: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: the modified intraday policy assertions fail because the current implementation still forces `observation`; the new diagnostic assertion fails because the current renderer puts the reason in `数据问题/异常`.

### Task 2: Implement the minimal policy and renderer changes

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1941-1951`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1556-1611`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2170-2176`

- [x] **Step 1: Preserve the regime mode during intraday execution**

Keep the intraday branch’s `provisional`, `provisional_target_mode`, and `intraday_provisional` fields, but remove only the assignments that overwrite `mode`, `max_recommendations`, and `max_portfolio_pct`. Thus a strong/neutral policy can classify eligible items while the output remains explicitly provisional.

- [x] **Step 2: Separate transient status from data errors in row diagnostics**

In `_candidate_diagnostic_text()`, collect `intraday_provisional` into a `transient_reasons` list before data/other classification. Append `盘中临时状态：...` as its own part after the existing data and other-reason parts. Keep the reason code in machine-readable policy and observation fields.

- [x] **Step 3: Avoid labeling the status-only reason as a recommendation downgrade**

When rendering the Markdown report’s `推荐降级` line, omit only `intraday_provisional`; keep genuine reasons such as `regime_weak`. The existing provisional banner and `(盘中临时,收盘确认)` section suffix remain unchanged.

### Task 3: Verify behavior and generated surfaces

**Files:**
- Verify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Verify: `.claude/skills/stock-trend/tests/test_recommendation_lifecycle.py`
- Verify: generated report output under `reports/lists/`

- [x] **Step 1: Run focused tests and confirm GREEN**

Run:

```bash
python3 -m unittest .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 -m unittest .claude/skills/stock-trend/tests/test_recommendation_lifecycle.py
```

- [x] **Step 2: Run the repository-required quality gates**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

- [x] **Step 3: Run a read-only report smoke check**

Generate a report with the existing daily-candidates command, then verify that an intraday report still contains the top warning, `provisional` behavior, and row-level `盘中临时状态`, while the report’s actionable/waiting limits follow the market regime.
