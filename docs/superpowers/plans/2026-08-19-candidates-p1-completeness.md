# Candidates P1 Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/candidates` scan every qualifying sector until its valid-candidate target is met, while exposing—and surviving—ranking cache, resonance, and per-batch failures.

**Architecture:** Keep the existing ranking → sector-context → batch funnel intact. Remove the implicit top-20 ceiling by letting `pick_hot_sectors()` return all absolute-heat-qualified sectors in rank order; `scan_sectors()` already stops as soon as it has enough valid candidates. Introduce a compact, additive scan-health contract in `performance` so every degraded input or failed batch is visible in JSON, Markdown, and HTML without turning a usable scan into a hard failure.

**Tech Stack:** Python 3 standard library, `unittest`, existing `RunSourceHealth`, `/candidates` JSON/Markdown/HTML reporting.

---

## Scope and decisions

- In scope: the P1 findings in `/candidates`: top-20 universe truncation, cache/snapshot-write resilience, resonance failure visibility, and batch failure visibility.
- In scope: deterministic tests for these degradation paths plus one mocked `main()` JSON-contract test.
- Out of scope: changing candidate scores, sector heat weights, the P0 recommendation gates, live provider retry policy, or peer-cohort performance refactoring.
- Decision: a partial scan remains usable and returns candidates, but has `scan_status: "degraded"`; a scan in which every attempted batch fails has `scan_status: "error"` and an empty candidate result. Both states are evidence, not recommendations.
- Additive contract: `performance` gains `scan_status`, `degradation_reasons`, and `failed_batches`. Existing fields and JSON keys remain unchanged.

## Files

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:104-285` — add normalized scan-health recording and render it in all output formats.
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:601-742` — remove the top-20 ceiling and make cache/snapshot/resonance failures non-fatal but observable.
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:745-886` — record failed sector batches and compute final scan status.
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:640-1085` — cover expanded sector universe, degradation records, cache/snapshot failures, and batch failures.
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1570-1640` — add mocked `main()` JSON output contract coverage.

### Task 1: Expand the automatic sector universe beyond the top-20 ceiling

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:601-715`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:640-700`

- [x] **Step 1: Write a failing universe-expansion test**

  Add this test near `test_pick_hot_sectors_uses_absolute_threshold`. It verifies that the 21st qualifying sector reaches the batch scanner rather than being truncated at the legacy default of 20:

  ```python
  def test_pick_hot_sectors_returns_all_absolute_heat_qualified_sectors(self):
      rows = [
          {"code": f"BK{i:02d}", "name": f"板块{i:02d}",
           "change_pct": 2.0, "main_force_net": 1e8,
           "up_count": 9, "down_count": 1}
          for i in range(21)
      ]
      rankings = {"meta": {"complete": True}, "sectors": rows}
      history = {
          date: [{"code": row["code"], "hot_score": 70,
                  "net_flow": 1e8} for row in rows]
          for date in ("2026-08-04", "2026-08-05", "2026-08-06")
      }
      with patch("fetchers.sector_data.get_sector_rankings", return_value=rankings), \
           patch("fetchers.sector_data.save_rankings_cache"), \
           patch("fetchers.sector_data.append_daily_snapshot"), \
           patch("fetchers.sector_data.load_snapshot_history", return_value=history):
          picked = dc.pick_hot_sectors(min_stocks=1, as_of_date="2026-08-06")
      self.assertEqual(len(picked), 21)
      self.assertEqual(picked[-1]["code"], "BK20")
  ```

- [x] **Step 2: Run the target test and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the new test fails with 20 returned sectors, proving the top-20 ceiling exists.

- [x] **Step 3: Make the ranked-universe limit optional**

  Change `pick_hot_sectors()` to default `top_n` to `None`, and continue passing that value to `rank_hot_sectors()`:

  ```python
  def pick_hot_sectors(top_n=None, min_hot=45, min_stocks=10, regime=None,
                       as_of_date="", source_health=None, metrics=None):
      """Return every absolute-heat-qualified sector in ranked order."""
  ```

  `rank_hot_sectors(..., top_n=None)` already returns all list members through Python slicing. Do not alter its scoring or filtering. The existing `scan_sectors()` early-stop condition at `eligible_count >= min_candidates` remains the execution budget guard.

- [x] **Step 4: Run the target test and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: all tests pass; a 21-sector qualifying universe is retained, while ordinary scans still stop after the configured valid-candidate target.

- [x] **Step 5: Commit the universe-expansion change**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "fix(candidates): expand qualified sector universe"
  ```

### Task 2: Add a single additive scan-health contract

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:104-285`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:110-250`

- [x] **Step 1: Write failing contract tests**

  Add a helper-level test that supplies degradation evidence before `_complete_performance()` finalizes the result:

  ```python
  def test_complete_performance_preserves_degraded_scan_evidence(self):
      performance = {
          "degradation_reasons": ["resonance_error:RuntimeError"],
          "failed_batches": [{"sectors": ["BK1"], "reason": "OSError"}],
      }
      completed = _complete_performance(
          performance, None, [],
          {"actionable": [], "waiting_trigger": [], "observation": []},
          min_score=50, total_seconds=1.0)
      self.assertEqual(completed["scan_status"], "degraded")
      self.assertEqual(completed["degradation_reasons"],
                       ["resonance_error:RuntimeError"])
      self.assertEqual(completed["failed_batches"][0]["sectors"], ["BK1"])
  ```

  Add a second test where every attempted batch is recorded as failed and assert `scan_status == "error"`.

  Use this exact fixture for that assertion:

  ```python
  def test_complete_performance_marks_all_failed_batches_as_error(self):
      performance = {
          "batch_count": 2,
          "failed_batches": [
              {"sectors": ["BK1"], "reason": "OSError"},
              {"sectors": ["BK2"], "reason": "TimeoutError"},
          ],
          "degradation_reasons": [
              "batch_error:OSError", "batch_error:TimeoutError",
          ],
      }
      completed = _complete_performance(
          performance, None, [],
          {"actionable": [], "waiting_trigger": [], "observation": []},
          min_score=50, total_seconds=1.0)
      self.assertEqual(completed["scan_status"], "error")
  ```

- [x] **Step 2: Run the target test and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: failures because `scan_status` and the normalized degradation fields do not yet exist.

- [x] **Step 3: Implement the normalized health helpers and final status**

  Add these helpers above `_complete_performance()`:

  ```python
  def _record_degradation(metrics, reason):
      reasons = metrics.setdefault("degradation_reasons", [])
      if reason not in reasons:
          reasons.append(reason)


  def _record_failed_batch(metrics, batch, exc):
      metrics.setdefault("failed_batches", []).append({
          "sectors": list(batch),
          "reason": type(exc).__name__,
      })
      _record_degradation(metrics, f"batch_error:{type(exc).__name__}")
  ```

  In `_complete_performance()`, normalize absent fields and compute status exactly once:

  ```python
  completed.setdefault("degradation_reasons", [])
  completed.setdefault("failed_batches", [])
  attempted = int(completed.get("batch_count", 0))
  failed = len(completed["failed_batches"])
  completed["scan_status"] = (
      "error" if attempted > 0 and failed == attempted
      else ("degraded" if completed["degradation_reasons"] else "complete")
  )
  ```

  Render this additive state:

  ```python
  status = performance.get("scan_status", "complete")
  reasons = "、".join(performance.get("degradation_reasons", [])) or "无"
  lines.append(f"**扫描状态**: {status} | 降级原因: {reasons}")
  ```

  Add an equivalent escaped `扫描状态` paragraph to `_performance_html()`. Do not change existing timing, funnel, or source-health fields.

- [x] **Step 4: Run the target tests and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: complete scans retain `complete`; scans with one recoverable failure are `degraded`; scans whose every attempted batch failed are `error`.

- [x] **Step 5: Commit the scan-health contract**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "feat(candidates): expose scan degradation evidence"
  ```

### Task 3: Preserve usable ranking data when persistence or resonance side effects fail

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:601-742`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:640-805`

- [x] **Step 1: Write failing resilience tests**

  Add the following tests. They all use a complete live ranking payload and three days of valid history, so only the injected failure can cause degradation:

  Define this shared fixture before the tests:

  ```python
  def _complete_rankings_and_history():
      row = {
          "code": "BK1", "name": "测试板块", "change_pct": 2.0,
          "main_force_net": 1e8, "up_count": 9, "down_count": 1,
      }
      rankings = {
          "meta": {"complete": True, "data_date": "2026-08-06"},
          "sectors": [row],
      }
      history = {
          date: [{"code": "BK1", "hot_score": 70, "net_flow": 1e8}]
          for date in ("2026-08-04", "2026-08-05", "2026-08-06")
      }
      return rankings, history
  ```

  ```python
  def test_pick_hot_sectors_survives_cache_write_failure(self):
      rankings, history = _complete_rankings_and_history()
      metrics = {}
      with patch("fetchers.sector_data.get_sector_rankings", return_value=rankings), \
           patch("fetchers.sector_data.save_rankings_cache", side_effect=OSError("disk")), \
           patch("fetchers.sector_data.append_daily_snapshot"), \
           patch("fetchers.sector_data.load_snapshot_history", return_value=history):
          picked = dc.pick_hot_sectors(min_stocks=1, as_of_date="2026-08-06",
                                       metrics=metrics)
      self.assertTrue(picked)
      self.assertIn("ranking_cache_write_error:OSError",
                    metrics["degradation_reasons"])
  ```

  Add the snapshot-write and resonance-error tests with the same fixture:

  ```python
  def test_pick_hot_sectors_survives_snapshot_write_failure(self):
      rankings, history = _complete_rankings_and_history()
      metrics = {}
      with patch("fetchers.sector_data.get_sector_rankings", return_value=rankings), \
           patch("fetchers.sector_data.save_rankings_cache"), \
           patch("fetchers.sector_data.append_daily_snapshot",
                 side_effect=OSError("disk")), \
           patch("fetchers.sector_data.load_snapshot_history", return_value=history):
          picked = dc.pick_hot_sectors(min_stocks=1, as_of_date="2026-08-06",
                                       metrics=metrics)
      self.assertTrue(picked)
      self.assertIn("sector_snapshot_write_error:OSError",
                    metrics["degradation_reasons"])


  def test_pick_hot_sectors_exposes_resonance_failure(self):
      rankings, history = _complete_rankings_and_history()
      metrics = {}
      with patch("fetchers.sector_data.get_sector_rankings", return_value=rankings), \
           patch("fetchers.sector_data.save_rankings_cache"), \
           patch("fetchers.sector_data.append_daily_snapshot"), \
           patch("fetchers.sector_data.load_snapshot_history", return_value=history), \
           patch("bridge.sector_feeder.load_qualified_sectors",
                 side_effect=RuntimeError("offline")):
          picked = dc.pick_hot_sectors(min_stocks=1, as_of_date="2026-08-06",
                                       metrics=metrics)
      self.assertEqual(picked[0]["resonance_quality"], "error")
      self.assertTrue(picked[0]["sector_actionable"])
      self.assertIn("resonance_error:RuntimeError",
                    metrics["degradation_reasons"])
  ```

  The resonance test must also assert `picked[0]["resonance_quality"] == "error"` and preserve the sector as scanable based on its independent ranking evidence.

- [x] **Step 2: Run the target tests and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the cache/snapshot tests error out before returning a sector; the resonance test has no structured error state.

- [x] **Step 3: Implement side-effect isolation and resonance provenance**

  In `pick_hot_sectors()`, initialize `metrics = metrics if metrics is not None else {}`. On a complete live ranking, write the cache and snapshot independently:

  ```python
  if active and live_meta.get("complete", False):
      if as_of_date:
          try:
              save_rankings_cache(rankings, data_date=as_of_date)
          except (OSError, TypeError, ValueError) as exc:
              _record_degradation(
                  metrics, f"ranking_cache_write_error:{type(exc).__name__}")
          try:
              append_daily_snapshot(rankings, override_date=as_of_date)
          except (OSError, TypeError, ValueError) as exc:
              _record_degradation(
                  metrics, f"sector_snapshot_write_error:{type(exc).__name__}")
  ```

  Replace the broad silent resonance block with this explicit contract:

  ```python
  resonance_quality = "not_available"
  resonance_reason = ""
  if expected_date:
      try:
          from bridge.sector_feeder import load_qualified_sectors
          resonance = load_qualified_sectors()
          if resonance.date == expected_date:
              qualified = merge_sector_resonance(qualified, resonance.sectors)
              resonance_quality = "good"
          else:
              resonance_quality = "stale"
              resonance_reason = "date_mismatch"
              _record_degradation(metrics, "resonance_stale:date_mismatch")
      except Exception as exc:
          resonance_quality = "error"
          resonance_reason = type(exc).__name__
          _record_degradation(metrics, f"resonance_error:{type(exc).__name__}")
  for sector in qualified:
      sector["resonance_quality"] = resonance_quality
      sector["resonance_reason"] = resonance_reason
  ```

  Keep the existing policy that resonance absence does not by itself promote or reject a sector; it changes evidence quality only.

- [x] **Step 4: Run the target tests and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: ranking data remains usable after either write failure, and every resonance failure/mismatch is visible in both metrics and per-sector provenance.

- [x] **Step 5: Commit the resilience change**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "fix(candidates): retain ranking results on side-effect failures"
  ```

### Task 4: Record failed batches and validate the public JSON contract through `main()`

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:796-886`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:804-1085`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1570-1640`

- [x] **Step 1: Write failing batch and main-contract tests**

  Add a batch test that lets the first gather call fail and the second succeed:

  ```python
  def test_scan_records_failed_batch_and_keeps_later_candidates(self):
      metrics = {}
      def gather(batch, **_kwargs):
          if batch == ["BK1"]:
              raise OSError("upstream")
          return {"candidates": [{"code": "600002", "sector_code": "BK2"}]}
      with patch.object(dc, "gather_candidates", side_effect=gather), \
           patch.object(dc, "run_phase2", return_value=[{
               "code": "600002", "sector_code": "BK2", "composite_score": 80,
               "quality_adjusted_score": 80, "data_quality": {"eligible": True},
           }]):
          scored = dc.scan_sectors(
              ["BK1", "BK2"], batch_size=1, min_candidates=99,
              sector_context={"BK1": {}, "BK2": {}}, metrics=metrics)
      self.assertEqual([item["code"] for item in scored], ["600002"])
      self.assertEqual(metrics["failed_batches"],
                       [{"sectors": ["BK1"], "reason": "OSError"}])
  ```

  Add a mocked `main()` test with `--json --no-html`: patch `pick_hot_sectors()` to return one sector, patch `scan_sectors()` to set `performance["failed_batches"]`, and capture stdout with `redirect_stdout(io.StringIO())`. Assert:

  The complete isolation setup must patch `dc.load_regime_context`,
  `fetchers.sector_data.get_last_trading_day`, `dc.resolve_recommendation_date`,
  and `dc.REPORTS_DIR` to a temporary directory, so the test performs no live
  calendar lookup and writes no repository report.

  ```python
  def test_main_json_exposes_scan_degradation(self):
      fake_sector = {"code": "BK1", "name": "测试板块", "sector_score": 70}

      def fake_scan(*_args, metrics=None, **_kwargs):
          metrics["batch_count"] = 1
          metrics["failed_batches"] = [
              {"sectors": ["BK1"], "reason": "OSError"}
          ]
          metrics["degradation_reasons"] = ["batch_error:OSError"]
          return []

      with tempfile.TemporaryDirectory() as tmpdir, \
           patch.object(dc, "load_regime_context", return_value=None), \
           patch("fetchers.sector_data.get_last_trading_day",
                 return_value=("2026-08-06", "snapshot")), \
           patch.object(dc, "resolve_recommendation_date",
                        return_value="2026-08-06"), \
           patch.object(dc, "pick_hot_sectors",
                        return_value=[fake_sector]), \
           patch.object(dc, "scan_sectors", side_effect=fake_scan), \
           patch.object(dc, "REPORTS_DIR", Path(tmpdir)), \
           patch.object(sys, "argv",
                        ["daily_candidates.py", "--json", "--no-html"]):
          stdout = io.StringIO()
          with redirect_stdout(stdout):
              dc.main()

      payload = json.loads(stdout.getvalue())
      self.assertEqual(payload["meta"]["performance"]["scan_status"],
                       "degraded")
      self.assertEqual(
          payload["meta"]["performance"]["failed_batches"][0]["sectors"],
          ["BK1"],
      )
  ```

- [x] **Step 2: Run the target tests and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: the batch test finds no `failed_batches` record; the main JSON has no scan status or degradation evidence.

- [x] **Step 3: Record each recoverable batch failure**

  In the `except Exception as exc` around `gather_candidates()` in `scan_sectors()`, retain the stderr diagnostic and add:

  ```python
  _record_failed_batch(metrics, batch, exc)
  ```

  Do not catch `run_phase2()` exceptions in this P1 change; they represent a different pipeline boundary and need their own recovery design. Let the existing batch loop continue only for phase-1 gather failures.

  In `main()`, pass the shared `performance` mapping to `pick_hot_sectors()`:

  ```python
  sector_codes = pick_hot_sectors(
      regime=regime, as_of_date=expected_date,
      source_health=source_health, metrics=performance)
  ```

  `_complete_performance()` then makes this data visible through the existing `build_json_output()` performance envelope.

- [x] **Step 4: Run the target tests and confirm GREEN**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: a later successful batch is retained, the missing batch is disclosed, and the JSON public contract reports `degraded`.

- [x] **Step 5: Commit the batch-observability change**

  ```bash
  git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  git commit -m "feat(candidates): report incomplete batch coverage"
  ```

### Task 5: Run full regression and inspect the intentional contract change

**Files:**

- Verify only: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Verify only: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: Run the focused suite**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: all existing and new tests pass. The focused suite proves that scan status, failure evidence, cache-write recovery, resonance provenance, and sector-universe expansion work together.

- [x] **Step 2: Run the repository-mandated Python gates**

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0. Do not regenerate golden snapshots. Any output difference must be reviewed as an intended additive reporting-contract change.

- [x] **Step 3: Verify the change boundary**

  ```bash
  git diff --check
  git diff -- .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: no whitespace errors; no changes outside the candidates scanner, its tests, and this plan.

## Acceptance criteria

- Automatic `/candidates` selection considers every sector satisfying the existing absolute heat floor; batch scanning still stops once `min_candidates` valid candidates exist.
- A ranking-cache write failure or snapshot-write failure never discards a complete live ranking response.
- Resonance fetch errors and stale resonance dates are visible through per-sector provenance and `performance.degradation_reasons`.
- Every failed phase-1 sector batch is included in `performance.failed_batches`; the completed scan is `degraded` unless all attempted batches failed, in which case it is `error`.
- Markdown, HTML, and `--json` outputs expose the additive scan status without removing prior output fields.
- Focused tests and both mandatory repository gates pass without regenerating goldens.

## Plan self-review

- Coverage: Task 1 fixes the bounded sector universe; Tasks 2–4 fix each P1 visibility/resilience failure and lock the JSON contract; Task 5 verifies all required quality gates.
- Type consistency: `metrics` is always a mutable dictionary, `degradation_reasons` is a list of strings, `failed_batches` is a list of `{sectors, reason}` dictionaries, and `scan_status` is one of `complete`, `degraded`, or `error`.
- Scope control: provider selection, score formulas, and P0 recommendation gates are explicitly unchanged.
