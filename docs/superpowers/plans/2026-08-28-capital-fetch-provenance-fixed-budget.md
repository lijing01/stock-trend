# Capital Fetch Provenance and Fixed-Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every failed live capital-flow request attributable to its actual fallback stage while keeping the daily scan's 180-second deadline, 36-item capital queue, batch size of 12, and maximum concurrency of 4 unchanged.

**Architecture:** `capital_flow.py` owns the Eastmoney → Tushare → K-line-estimate fallback chain, so it will emit a small structured `failure_chain` when all stages fail. `stock_scanner.py` will preserve that payload evidence instead of rewriting it to the generic `empty` reason after a successful subprocess. `source_health.py` continues to consume a stable low-cardinality reason code, preserving the existing eight-consecutive-failure circuit breaker and its fixed scheduling budget.

**Tech Stack:** Python 3.10, `argparse`, `unittest`, `unittest.mock`, existing stock-trend fetcher/scanner/report scripts.

---

## Problem statement and acceptance criteria

### P0 problem

The 2026-08-28 candidate report recorded 45 logical capital requests, 25 valid results, and 20 failures, all reported as `empty`. That proves the 20 codes were actually called; it does **not** identify whether Eastmoney was empty/unreachable, Tushare was unavailable, the K-line estimate was missing or stale, or the child process wrote an invalid output file.

The loss occurs in `_fetch_capital_flow`: after `run_script()` succeeds, an invalid output payload is converted to `reason=status="empty"`. The fetcher itself has richer information (`meta.error_type`, `meta.stale_sources`, and its per-stage errors), but it is not preserved into candidate `source_evidence`.

### Fixed-budget contract

This plan deliberately does **not** increase any of these values:

```python
SCAN_DEADLINE_SECONDS = 180
FINALIZATION_RESERVE_SECONDS = 10
CAPITAL_PREFETCH_LIMIT = 36
CAPITAL_PREFETCH_BATCH_SIZE = 12
MAX_IN_FLIGHT["capital"] = 4
LIVE_ATTEMPT_TIMEOUT_SECONDS["capital"] = 25
```

It also adds no retry loop and no second queue pass. One scanner-selected candidate still launches at most one capital-fetch subprocess; the child process retains its existing bounded fallback chain. The existing circuit breaker still opens after eight consecutive logical capital failures, so the scan does not spend the remaining deadline on a demonstrably unavailable source.

### Acceptance criteria

1. A candidate whose child process exits zero but emits an invalid capital payload reports a stable, specific reason (for example `stale_data`, `eastmoney_empty`, `tushare_empty`, `kline_missing`, or `output_invalid`) rather than an unqualified `empty`.
2. Candidate source evidence retains a bounded `failure_chain`, `error_type`, and `stale_sources` for report/audit use; raw long error strings are retained only as a detail field and are not used as aggregation keys.
3. `RunSourceHealth` aggregates only the stable reason code and keeps its existing circuit-breaker behavior.
4. The report distinguishes: `attempted=true` provider failures, `attempted=false` queue omissions, deadline omissions, and `source_unavailable` skips.
5. Existing timing constants and the production timing contract remain unchanged; no candidate with missing current capital evidence becomes eligible.

## Files

- Modify: `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py`
  - Produce a structured, bounded fallback outcome list only when all fallback stages fail.
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
  - Derive the scanner reason from the failed payload and attach diagnostic-only details to `live_attempt`.
- Modify: `.claude/skills/stock-trend/scripts/core/source_health.py`
  - Extend the internal evidence record with optional diagnostic fields without changing health counters or scheduling constants.
- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`
  - Treat `source_unavailable` as an unrequested scheduler state, preserving the hard eligibility gate.
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
  - Render scheduling states separately from actual fetch failures and summarize stable capital failure reasons in the audit.
- Modify: `.claude/skills/stock-trend/tests/test_capital_flow.py`
  - Lock fallback-chain metadata and existing valid fallback behavior.
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
  - Lock scanner propagation, reason classification, and non-promotion on invalid capital data.
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
  - Lock report wording/counts for provider failure versus no-request scheduling states.
- Modify: `.claude/skills/stock-trend/tests/test_recommendation_quality.py`
  - Lock non-provider handling for circuit-open `source_unavailable` states.
- Modify: `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py`
  - Lock the unchanged fixed-budget constants and bounded-attempt contract.

## Data contract

Add only optional fields; do not rename existing `meta.error`, `meta.error_type`, `source_evidence.capital.reason`, or `source_evidence.capital.status` fields.

```python
# capital_flow.py: returned only when every fallback is unusable
meta["failure_chain"] = [
    {"source": "eastmoney", "reason": "empty"},
    {"source": "tushare_fallback", "reason": "empty"},
    {"source": "kline_estimate", "reason": "missing"},
]

# stock_scanner.py: internal/live source evidence
live_attempt(
    attempted=True,
    provider_attempts=1,
    reason="eastmoney_empty",
    status="eastmoney_empty",
    failure_chain=meta["failure_chain"],
    error_type=meta.get("error_type", ""),
    stale_sources=meta.get("stale_sources", []),
    failure_detail=meta.get("error", ""),
)
```

The reason-code resolver must use this precedence:

```python
def _capital_failure_reason(payload, cache_verdict):
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    if meta.get("error_type") == "stale_data":
        return "stale_data"
    chain = meta.get("failure_chain", [])
    sources = {item.get("source"): item.get("reason") for item in chain
               if isinstance(item, dict)}
    for source, prefix in (("eastmoney", "eastmoney"),
                           ("tushare_fallback", "tushare")):
        source_reason = sources.get(source)
        if source_reason in {"empty", "timeout", "dns", "http", "parse",
                             "subprocess"}:
            return f"{prefix}_{source_reason}"
    if sources.get("kline_estimate") in {"missing", "empty"}:
        return "kline_missing"
    return cache_verdict.get("reason") or "output_invalid"
```

This selects a single stable aggregate reason while retaining the complete ordered chain for diagnostics. It intentionally does not infer a provider failure for `not_selected_for_enrichment`, `not_started_deadline`, or `source_unavailable` because those are scheduler states, not failed API calls.

### Task 1: Add failing fetcher tests for structured fallback evidence

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_capital_flow.py`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py:271-350`

- [x] **Step 1: Add an all-fallbacks-failed test**

  Add this assertion block to the existing `test_all_empty_sources_return_error` test after it obtains `result`:

  ```python
  self.assertEqual(result["meta"]["data_source"], "error")
  self.assertEqual(
      result["meta"]["failure_chain"],
      [
          {"source": "eastmoney", "reason": "empty"},
          {"source": "tushare_fallback", "reason": "empty"},
          {"source": "kline_estimate", "reason": "missing"},
      ],
  )
  ```

- [x] **Step 2: Add a stale-data chain test**

  Add a test using the existing stale Eastmoney fixture:

  ```python
  def test_stale_primary_records_chain_without_losing_stale_marker(self):
      stale = [{"date": "20260825", "main_net_inflow": 1.0}]
      result, _, _ = self._fetch(stale, [], [])
      self.assertEqual(result["meta"]["error_type"], "stale_data")
      self.assertEqual(result["meta"]["failure_chain"][0], {
          "source": "eastmoney", "reason": "stale_data",
      })
      self.assertIn("eastmoney", result["meta"]["stale_sources"])
  ```

- [x] **Step 3: Run the focused test file and confirm failure**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  ```

  Expected: the new assertions fail with `KeyError: 'failure_chain'`; existing fallback tests still pass.

- [x] **Step 4: Implement a bounded fallback ledger**

  In `fetch_stock_capital_flow_with_fallbacks`, initialize and append a short record at each existing branch:

  ```python
  failure_chain = []

  # In the Eastmoney exception branch
  failure_chain.append({"source": "eastmoney", "reason": "empty"})

  # When Eastmoney rows are present but fail expected-date validation
  failure_chain.append({"source": "eastmoney", "reason": "stale_data"})

  # When Tushare has no valid rows
  failure_chain.append({"source": "tushare_fallback", "reason": "empty"})

  # When K-line estimation has no valid rows
  failure_chain.append({"source": "kline_estimate", "reason": "missing"})
  ```

  Do not append a failure record for a source whose valid result is immediately returned. Before returning the final `data_source="error"` payload, add:

  ```python
  if failure_chain:
      meta["failure_chain"] = failure_chain
  ```

  Preserve `errors`, `error_type`, and `stale_sources` exactly as they are today. Use a small local helper to classify caught Eastmoney exceptions with the existing `classify_failure` function if its result is one of `timeout`, `dns`, `http`, `parse`, or `subprocess`; otherwise use `empty`. This prevents raw messages from becoming report aggregation keys.

- [x] **Step 5: Re-run focused fetcher tests**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  ```

  Expected: all tests pass, and valid Eastmoney/Tushare/K-line fallback success paths have no `failure_chain` because they are valid results.

### Task 2: Preserve child-payload diagnostics in scanner evidence

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/core/source_health.py:62-84`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1064-1155`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [x] **Step 1: Add a failing scanner propagation test**

  Add this method near the existing `_fetch_capital_flow` tests in `TestMetadata`:

  ```python
  def test_capital_error_payload_keeps_fallback_chain_in_live_evidence(self):
      payload = {
          "meta": {
              "data_source": "error",
              "error_type": "stale_data",
              "stale_sources": ["eastmoney"],
              "failure_chain": [
                  {"source": "eastmoney", "reason": "stale_data"},
                  {"source": "tushare_fallback", "reason": "empty"},
              ],
              "error": "资金流向获取失败: 东方财富数据过期",
          },
          "data": [],
      }
      with tempfile.TemporaryDirectory() as tmpdir, \
              patch.object(sc, "CACHE_DIR", tmpdir), \
              patch.object(sc, "_read_json", side_effect=[None, payload]), \
              patch.object(sc, "run_script", return_value={"success": True}):
          wrapped = sc._fetch_capital_flow(
              "600001.SH", with_evidence=True,
              expected_trading_date="2026-08-13")

      attempt = wrapped["live_attempt"]
      self.assertEqual(attempt["reason"], "stale_data")
      self.assertEqual(attempt["status"], "stale_data")
      self.assertEqual(attempt["stale_sources"], ["eastmoney"])
      self.assertEqual(attempt["failure_chain"], payload["meta"]["failure_chain"])
      self.assertIn("东方财富", attempt["failure_detail"])
  ```

- [x] **Step 2: Add the extended optional fields to `live_attempt`**

  Change the signature and returned dictionary in `source_health.py` to:

  ```python
def live_attempt(*, attempted: bool, provider_attempts: int = 0,
                   reason: str = "", cache_used: bool = False,
                   stale: bool = False, subprocess_started: bool = False,
                   status: str = "", failure_chain: list | None = None,
                   error_type: str = "", stale_sources: list | None = None,
                   failure_detail: str = "") -> dict:
      evidence = {
          # retain existing fields unchanged
          "attempted": bool(attempted),
          "reason": reason,
          "cache_used": bool(cache_used),
          "stale": bool(stale),
          "subprocess_started": bool(subprocess_started),
          "provider_attempts": max(0, int(provider_attempts or 0)),
          "status": str(status or ""),
      }
      # Keep successful/legacy evidence schema stable; attach diagnostics only
      # when a failed payload actually provides them.
      if failure_chain:
          evidence["failure_chain"] = list(failure_chain)
      if error_type:
          evidence["error_type"] = str(error_type)
      if stale_sources:
          evidence["stale_sources"] = list(stale_sources)
      if failure_detail:
          evidence["failure_detail"] = str(failure_detail)
      return evidence
```

  Keep `RunSourceHealth._complete()` unchanged: it aggregates `attempt["reason"]` only. Do not add `failure_detail` to `failure_reasons`.

- [x] **Step 3: Implement the scanner resolver and propagation**

  Add `_capital_failure_reason(payload, cache_verdict)` beside `classify_failure` usage in `stock_scanner.py`, using the exact precedence specified in the data contract. In the `result["success"]` / invalid-verdict branch of `_fetch_capital_flow`, replace the hard-coded assignments with:

  ```python
  meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
  reason = _capital_failure_reason(payload, refreshed_verdict)
  attempt.update({
      "reason": reason,
      "status": reason,
      "failure_chain": meta.get("failure_chain", []),
      "error_type": meta.get("error_type", ""),
      "stale_sources": meta.get("stale_sources", []),
      "failure_detail": meta.get("error", ""),
  })
  ```

  Call `_with_cache_verdict` after extracting `meta`, because its wrapper may alter the payload shape. Retain the existing strict cache/date validator and the `live_success` branch exactly.

- [x] **Step 4: Run focused scanner tests**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  ```

  Expected: the new test passes; existing timeout and `not_selected_for_enrichment` tests remain green. A stale/error payload must still be ineligible under the existing recommendation-quality gate.

### Task 3: Report source failures and scheduling omissions separately

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1239-1286`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: Add failing report-rendering tests**

  Build two candidate fixtures with these evidence fragments:

  ```python
  provider_failure = {
      "attempted": True,
      "status": "eastmoney_empty",
      "reason": "eastmoney_empty",
      "failure_chain": [
          {"source": "eastmoney", "reason": "empty"},
          {"source": "tushare_fallback", "reason": "empty"},
      ],
  }
  queue_omission = {
      "attempted": False,
      "status": "not_selected_for_enrichment",
      "reason": "not_selected_for_enrichment",
  }
  ```

  Assert the rendered HTML includes `资金接口失败（已调用）：东方财富空响应` for the first fixture and `未进入资金增强优先队列（未调用）` for the second. Assert the summary exposes `capital_failure_reasons={"eastmoney_empty": 1}` and does not include the queue omission in that counter.

- [x] **Step 2: Implement explicit labels and audit aggregation**

  In the report diagnostic formatter, branch on `attempted` before choosing a label:

  ```python
  if evidence.get("attempted"):
      label = "资金接口失败（已调用）"
  else:
      label = "资金增强调度状态（未调用）"
  ```

  Add `capital_failure_reasons` to the audit only for evidence records where `attempted is True` and `status != "live_success"`. Increment by `reason`, never by `failure_detail`. Render `failure_chain` in a collapsed diagnostic/details element for the affected candidate; do not expose it as a ranking signal.

- [x] **Step 3: Run focused report tests**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
  ```

  Expected: provider failures and scheduler states have different wording and only actual calls contribute to the failure-reason aggregation.

### Task 4: Lock the no-budget-increase contract and validate end to end

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py:333-360`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [x] **Step 1: Add a fixed-budget test for failing capital calls**

  Add a deterministic scanner test with 37 cache-missing candidates, mocked capital results carrying `reason="eastmoney_empty"`, and a `RunSourceHealth` instance. Assert:

  ```python
  self.assertLessEqual(len(capital_calls), 36)
  self.assertEqual(contract.CAPITAL_PREFETCH_LIMIT, 36)
  self.assertEqual(contract.CAPITAL_PREFETCH_BATCH_SIZE, 12)
  self.assertEqual(contract.MAX_IN_FLIGHT["capital"], 4)
  self.assertEqual(contract.SCAN_DEADLINE_SECONDS, 180)
  self.assertTrue(all(
      not item["data_quality"]["eligible"] for item in scored
      if item["source_evidence"]["capital"].get("attempted")
  ))
  ```

  Use `source_result(None, live_attempt(...))` in the mock so the test exercises source-health failure accounting without sleeping or accessing the network.

- [x] **Step 2: Run performance and scanner tests**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_daily_recommendation_performance.py
  python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  ```

  Expected: all tests pass; no production timing constant, queue size, batch size, or capital concurrency is increased.

- [x] **Step 3: Run mandatory repository quality gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands pass. Do not regenerate golden snapshots unless the report text is intentionally snapshot-covered and the changed label/counter has been reviewed as the desired output contract.

- [x] **Step 4: Run a live post-close smoke scan and audit results**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
    --top 30 --min-candidates 20 --no-html
  ```

  Expected: completion remains within the 180-second envelope; the audit separately shows attempted capital failures, their stable reason breakdown, live successes, cache-valid values, and uncalled queue/deadline states. Confirm no candidate with failed/missing same-day capital data is promoted to `eligible=true`.

  Final validation on 2026-08-28 completed in 101.25s (wall time 101.94s):
  `capital_priority=62`, `capital_live_started=30`, `capital_valid=73`,
  `capital_cache_valid=61`, `capital_failure_reasons={"stale_data":18}`;
  no budget constants or eligibility gates changed. The environment reported
  DNS failures for live market providers, so this smoke result used the
  repository's cache fallbacks where available and kept the scan degraded.

## Rollout and decision gate

1. Deploy Tasks 1–4 without changing budget constants or policy thresholds.
2. Collect three post-close scans and aggregate `capital_failure_reasons` plus `failure_chain` by source.
3. Only then choose a P1 remedy:
   - predominantly `eastmoney_empty`/network errors: repair or extend the Eastmoney-host path;
   - predominantly `tushare_empty`: inspect credential/coverage behavior;
   - predominantly `kline_missing` or `stale_data`: fix same-day K-line availability/freshness;
   - predominantly `output_invalid`: inspect child-output/cache write path.
4. Do not enlarge `CAPITAL_PREFETCH_LIMIT` until the successful-data yield and live deadline are measured after P0. More candidates cannot correct a source that is currently failing; it would only trade report finalization time for more unclassified failures.

## Non-goals

- Do not relax expected trading-date validation, recommendation eligibility, regime gating, or quality scoring.
- Do not treat a stale cache or a K-line estimate with an older trading date as current capital data.
- Do not change the `not_selected_for_enrichment` scheduling policy in this patch; only make its “not called” nature unambiguous in the report.
- Do not add an unbounded retry, a second enrichment queue, or a larger capital budget.
