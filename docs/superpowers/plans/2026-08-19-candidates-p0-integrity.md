# Candidates P0 Integrity Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent `/candidates` from treating negative or sparse sector capital flow as proof during a weak-market capital divergence, and ensure a stock appears in exactly one recommendation bucket.

**Architecture:** Keep the existing sector-capital score and recommendation policy intact, but make the capital-evidence state explicit: data presence is not positive proof. `classify_candidates()` will only promote a candidate under `requires_sector_capital_proof` when its primary sector has `positive_verified` evidence. The next-day confirmation bucket remains a non-recommendation bucket and becomes mutually exclusive with the observation bucket.

**Tech Stack:** Python 3 standard library, `unittest`, existing `/candidates` scan and report pipeline.

---

## Scope and non-goals

- In scope: only the two P0 integrity defects in `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` and their regression tests.
- In scope: preserve the existing `capital_persistence` score calculation and existing market-score thresholds.
- Out of scope: changing board ranking, expanding the sector universe, changing position limits, or adding trade-entry/stop fields.
- Compatibility: the JSON field `sector_capital_evidence` remains a string. Its promotable value changes from the ambiguous `"verified"` to `"positive_verified"`; `"verified"` is treated as historical/data-present evidence and must not unlock promotion.

## Files

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:428-479` — derive explicit capital-evidence states from recent sector net flow.
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1141-1205` — require positive proof under divergence and make recommendation buckets exclusive.
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:504-611` — test capital-evidence derivation.
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1334-1373` — test divergence promotion and bucket exclusivity.

### Task 1: Lock the positive-capital-proof contract

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:504-611`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1334-1352`

- [ ] **Step 1: Add failing derivation tests**

  Extend the existing three-positive-day test with this assertion; it fails before the production change because the current code returns `"verified"`:

  ```python
  self.assertEqual(sector["capital_evidence"], "positive_verified")
  ```

  Add a separate negative-flow test next to `test_capital_persistence_measures_positive_days_not_amount`:

  ```python
  def test_negative_sector_flows_are_not_positive_verified(self):
      ranked = [{"code": "BK1", "name": "资金流出", "absolute_hot_score": 70,
                 "hot_score": 80}]
      history = {
          "2026-08-04": [{"code": "BK1", "hot_score": 70, "net_flow": -2e8}],
          "2026-08-05": [{"code": "BK1", "hot_score": 70, "net_flow": -1e8}],
          "2026-08-06": [{"code": "BK1", "hot_score": 70, "net_flow": -3e8}],
      }
      sector = enrich_sector_context(ranked, history)[0]
      self.assertEqual(sector["capital_evidence"], "partial")
  ```

  Modify the existing divergence test so it verifies that only the new explicit proof value unlocks the gate:

  ```python
  historical = candidate("historical")
  historical["sector_capital_evidence"] = "verified"
  positive = candidate("positive")
  positive["sector_capital_evidence"] = "positive_verified"
  buckets = classify_candidates([historical, positive], policy)
  self.assertEqual([item["code"] for item in buckets["waiting_trigger"]], ["positive"])
  self.assertIn("breadth_capital_divergence",
                buckets["observation"][0]["observation_reasons"])
  ```

- [ ] **Step 2: Run the targeted test and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: failures asserting that `capital_evidence` is `positive_verified` and that the historical `verified` value cannot pass the divergence gate. Do not proceed if failures are unrelated.

- [ ] **Step 3: Implement the minimal evidence state machine**

  In `enrich_sector_context()`, replace the current presence-only assignment with:

  ```python
  positive_capital_proof = (
      len(net_flows) >= 3
      and capital_positive_days >= 2
      and sum(net_flows) > 0
  )
  capital_evidence = (
      "positive_verified" if positive_capital_proof
      else ("partial" if net_flows else "unknown")
  )
  ```

  Keep `capital_persistence`, `capital_positive_days`, and `capital_streak` unchanged. The new condition deliberately requires three valid observations, a majority of positive days, and a positive aggregate flow.

  In `classify_candidates()`, change the divergence predicate to:

  ```python
  item.get("sector_capital_evidence") == "positive_verified"
  ```

  Do not change the behaviour when `requires_sector_capital_proof` is false.

- [ ] **Step 4: Run the targeted test and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: all tests pass, including the new positive and negative capital-evidence cases.

- [ ] **Step 5: Commit the isolated capital-proof change**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "fix(candidates): require positive sector capital proof"
  ```

### Task 2: Make confirmation and observation buckets mutually exclusive

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1353-1373`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1152-1205`

- [ ] **Step 1: Add a failing exclusivity assertion**

  Extend `test_neutral_market_builds_non_recommendation_confirmation_list` after its existing assertions:

  ```python
  self.assertEqual(buckets["observation"], [])
  ```

  Add an explicit invariant test:

  ```python
  def test_confirmation_candidate_is_not_duplicated_in_observation(self):
      policy = build_recommendation_policy(
          {"score": 65, "data_date": "2026-08-06", "capital_score": 50},
          "2026-08-06")
      watch = candidate("watch", sector_actionable=False)
      buckets = classify_candidates([watch], policy)
      confirmation_codes = {row["code"] for row in buckets["next_day_confirmation"]}
      observation_codes = {row["code"] for row in buckets["observation"]}
      self.assertTrue(confirmation_codes)
      self.assertFalse(confirmation_codes & observation_codes)
  ```

- [ ] **Step 2: Run the targeted test and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the new assertions fail because the current loop excludes only promoted codes, not confirmation codes.

- [ ] **Step 3: Implement bucket exclusivity**

  After computing `confirmations`, derive their codes before the observation loop:

  ```python
  confirmation_codes = {item["code"] for item in confirmations}
  ```

  At the beginning of the observation loop, change the skip condition to:

  ```python
  if item.get("code") in promoted or item.get("code") in confirmation_codes:
      continue
  ```

  Keep the existing `confirmation_conditions` text and the rule that this bucket is non-recommendatory. Do not change the waiting-trigger cap or observation reasons for rows which remain in the observation bucket.

- [ ] **Step 4: Run the targeted test and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: all tests pass and the confirmation candidate occurs only in `next_day_confirmation`.

- [ ] **Step 5: Commit the isolated bucket-integrity change**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "fix(candidates): keep recommendation buckets exclusive"
  ```

### Task 3: Run repository quality gates and inspect the contract

**Files:**

- Verify only: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Verify only: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: Run the required Python quality gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0. Do not regenerate golden snapshots; these changes affect gating metadata and bucket membership, so any golden diff must be reviewed as an intentional contract change rather than accepted automatically.

- [ ] **Step 2: Run static diff checks**

  Run:

  ```bash
  git diff --check
  git diff -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: no whitespace errors; the diff is limited to the two P0 contracts and their tests.

- [ ] **Step 3: Perform a contract review of the JSON result**

  Confirm from the test fixtures or a mocked invocation that:

  ```text
  - sector_capital_evidence is unknown, partial, or positive_verified.
  - In a capital-divergence policy, only positive_verified candidates can enter actionable/waiting_trigger.
  - A code in next_day_confirmation is absent from observation.
  - recommendations, waiting_trigger, next_day_confirmation, and observation remain present in JSON output.
  ```

- [ ] **Step 4: Commit verification-only follow-up only if it contains intentional test/document changes**

  ```bash
  git status --short
  ```

  Expected: clean worktree after the Task 1 and Task 2 commits. Do not create an empty commit.

## Acceptance criteria

- A sector with fewer than three valid net-flow readings, fewer than two positive readings, or non-positive aggregate flow cannot provide capital proof.
- A sector with at least three valid readings, at least two positive readings, and positive aggregate flow emits `positive_verified` and can unlock the existing divergence gate.
- The divergence gate still applies only when market `capital_score < 35`.
- A candidate appears in at most one of `waiting_trigger`, `next_day_confirmation`, and `observation`; `actionable` remains disjoint because promoted codes are already excluded.
- Existing output keys and non-divergence recommendation behavior remain unchanged.
- Targeted tests and both repository-mandated Python quality gates pass without regenerating golden files.

## Plan self-review

- Coverage: Task 1 covers the positive-capital-proof defect; Task 2 covers duplicate bucket membership; Task 3 verifies the required repository gates and output contract.
- No scope expansion: no scoring weights, external fetchers, position limits, or report redesign are changed.
- Naming consistency: `positive_verified` is defined in Task 1 and used consistently in implementation, tests, and acceptance criteria.
