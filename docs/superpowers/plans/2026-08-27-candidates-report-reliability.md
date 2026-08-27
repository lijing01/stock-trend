# Candidates Report Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/candidates` produce traceable, data-complete candidate reports without silently downgrading valid signals or reporting avoidable degradation.

**Architecture:** Preserve the existing conservative three-bucket recommendation policy. Repair snapshot serialization at its boundary, make optional resonance provenance non-blocking, and make the sector-expansion cap a measurable coverage policy rather than an opaque scan degradation. No new data providers or dependencies are introduced.

**Tech Stack:** Python 3 standard library, `unittest`, existing candidate scanner and recommendation snapshot modules.

---

## Scope and acceptance criteria

- An after-close run with candidate fields such as `trigger_date: "20260825"` persists exactly one official snapshot whose canonical date is `"2026-08-25"`.
- Invalid calendar values (for example `"2026-02-30"`) still fail validation; the fix must not weaken the future-evidence guard.
- A stale optional THS resonance feed is visible in the report but does not mark the whole scan degraded or alter candidate eligibility.
- When the configured sector cap is hit, the report states scanned coverage and whether more candidates could plausibly exist; operators can raise the cap explicitly.
- The full project quality gates remain green:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

### Task 1: Canonicalize snapshot payload dates at the serialization boundary (P0)

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py:25-66`
- Test: `.claude/skills/stock-trend/tests/test_recommendation_snapshot.py`

- [ ] **Step 1: Add failing coverage for compact provider dates.**

  Add a test source field under `candidates[0]` and assert that the built snapshot is canonical:

  ```python
  def test_build_normalizes_compact_nested_dates(self):
      source = src()
      source['candidates'][0]['wyckoff'] = {'trigger_date': '20260820'}
      snapshot = build_snapshot(source)
      self.assertEqual(
          snapshot['content']['candidates'][0]['wyckoff']['trigger_date'],
          '2026-08-20',
      )
  ```

- [ ] **Step 2: Run the focused test and verify it fails with `invalid recommendation_date`.**

  Run:

  ```bash
  python3 -m unittest .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
  ```

  Expected: the new test fails before snapshot persistence.

- [ ] **Step 3: Add a narrowly scoped date-value normalizer.**

  In `_normalize_for_json`, after the `date`/`datetime` branch and before generic strings return, normalize only semantic date fields whose value matches eight digits:

  ```python
  if isinstance(value, str) and _is_date_field(path) and len(value) == 8 and value.isdigit():
      return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
  ```

  Implement `_is_date_field(path)` beside `_normalize_for_json`; it must recognize path segments ending in `date`, plus `as_of` and `basis_date`. Do not normalize IDs or arbitrary eight-digit strings. Let `date(...)` raise `ValueError` for impossible calendar dates so `_validate` continues to reject them.

- [ ] **Step 4: Add guard tests and run the focused suite.**

  Add tests proving `recommendation_date='20260820'` becomes ISO, `trigger_date='20260230'` fails, and an optional empty `data_date` is accepted. Then run the command in Step 2.

  Expected: all snapshot tests pass; `test_future_plan_validity_is_not_future_evidence` remains green.

- [ ] **Step 5: Commit the isolated P0 repair.**

  ```bash
  git add .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
  git commit -m "fix: normalize candidate snapshot dates"
  ```

### Task 2: Add an end-to-end regression for official snapshot tracking (P0)

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:2080-2115`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:49-50,2038-2040`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: Add a failing main-path fixture containing a compact trigger date.**

  Extend `test_main_json_exposes_all_failed_scan_batches` or add an adjacent test that patches `scan_sectors` to return one otherwise eligible candidate with:

  ```python
  {
      'code': '000001', 'name': '测试股', 'quality_adjusted_score': 80,
      'data_quality': {'eligible': True}, 'sector_actionable': True,
      'wyckoff': {'trigger_date': '20260806'},
  }
  ```

  Patch the recommendation history root to `TemporaryDirectory()` and assert `output['meta']['tracking']['status'] == 'created'`.

- [ ] **Step 2: Run the named test before implementation.**

  ```bash
  python3 -m unittest .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the new assertion fails with `validation_failed` until Task 1 is implemented.

- [ ] **Step 3: Run it after Task 1 and verify snapshot idempotency.**

  Invoke the same patched main path twice and assert the second result is `unchanged`, with one `<recommendation_date>.json` file only.

- [ ] **Step 4: Commit the end-to-end guard.**

  ```bash
  git add .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "test: cover candidate snapshot persistence"
  ```

- [ ] **Step 5: Normalize non-native provider scalars before JSON stdout.**

  The live scan can carry NumPy `bool_` values in nested signal fields. Apply the existing snapshot JSON normalizer to the assembled JSON envelope immediately before `json.dumps`; add that scalar to the end-to-end fixture and assert the command exits successfully. This keeps JSON stdout and the persisted snapshot on the same native-type contract.

### Task 3: Separate optional resonance freshness from core scan health (P1)

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:831-859`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` report and JSON performance builders (search `degradation_reasons`)
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py:940-970`

- [ ] **Step 1: Write a failing stale-resonance contract test.**

  Extend `test_pick_hot_sectors_marks_stale_resonance_provenance` to assert:

  ```python
  self.assertEqual(picked[0]['resonance_quality'], 'stale')
  self.assertNotIn('resonance_stale:date_mismatch', metrics['degradation_reasons'])
  self.assertIn('resonance_stale:date_mismatch', metrics['advisory_reasons'])
  ```

- [ ] **Step 2: Run the targeted daily candidate suite.**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the new test fails because stale resonance currently sets global degradation.

- [ ] **Step 3: Record stale resonance as an advisory only.**

  Add `_record_advisory(metrics, reason)` parallel to `_record_degradation`. In the `resonance.date != expected_date` branch, call `_record_advisory(metrics, 'resonance_stale:date_mismatch')`; retain `resonance_quality='stale'` and `resonance_reason='date_mismatch'` on each sector. Provider exceptions remain degradations because they are operational failures.

- [ ] **Step 4: Render advisories separately from degraded scan state.**

  Include an `advisory_reasons` field in JSON performance and append an “辅助共振数据提示” line to HTML/Markdown only when non-empty. Keep `scan_status` driven by actual source failures and incomplete core data.

- [ ] **Step 5: Run targeted tests and commit.**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "fix: isolate stale resonance advisory"
  ```

### Task 4: Make sector expansion coverage explicit and configurable (P1)

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:50-53, 935-958, 1880-1888`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1220-1245`

- [ ] **Step 1: Add failing tests for cap metadata.**

  In the existing cap test, require these fields:

  ```python
  self.assertEqual(metrics['sector_scan_coverage'], 5 / len(contexts))
  self.assertTrue(metrics['sector_expansion_truncated'])
  ```

  Add a second test with `max_sector_expansion=len(contexts)` asserting coverage `1.0` and no truncation flag.

- [ ] **Step 2: Implement factual coverage fields, not a larger default cap.**

  Immediately after computing `scan_limit`, assign:

  ```python
  metrics['sector_scan_coverage'] = round(scan_limit / max(1, len(ordered_sector_codes)), 4)
  metrics['sector_expansion_truncated'] = len(ordered_sector_codes) > scan_limit
  ```

  Keep the default cap at 120 until real runtime measurements show a safe change. Include coverage and the exact `--max-sector-expansion` rerun command in the performance audit.

- [ ] **Step 3: Run the focused test suite and commit.**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "feat: expose candidate scan coverage"
  ```

### Task 5: Verify against the two observed regressions and quality gates (P0 release gate)

**Files:**

- No production file changes.

- [ ] **Step 1: Run the prescribed gates.**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0. Do not regenerate golden snapshots unless intentional output changes are reviewed and documented.

- [ ] **Step 2: Produce a fresh after-close candidate report.**

  ```bash
  python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py --top 30 --min-candidates 20 --json --no-html
  ```

  Expected: `meta.tracking.status` is `created` or `unchanged` on a clean history root (or `conflict` when an existing same-day snapshot has different decision content); no `invalid recommendation_date` or JSON scalar serialization error; `advisory_reasons` can include stale resonance without setting `scan_status=degraded` by itself; scan coverage is explicit.

- [ ] **Step 3: Review behavior, not only test results.**

  Verify manually that a candidate remains in observation when it lacks persistent sector evidence, missing targets, or an incomplete trade plan. The optimization must never promote an item merely to increase recommendation count.

## Rollout order

1. Merge Tasks 1–2 first; they restore the recommendation-history and attribution feedback loop.
2. Merge Task 3 second; it makes operational status honest without loosening risk gates.
3. Merge Task 4 third; use the new coverage metric for several runs before changing the 120-sector default.
4. Only then evaluate whether more historical sector snapshots or a larger expansion cap materially increase *eligible* candidates.

## Explicit non-goals

- Do not lower the 70% data-coverage, sector-persistence, target-source, or R:R gates just to create actionable recommendations.
- Do not turn ATR projections into executable targets.
- Do not add new market-data providers or automatically invoke THS-theme from `/candidates` in this change; both would materially broaden latency and failure surface.
