# Capital Flow Timeout Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent optional capital-data enrichment from consuming the daily candidate scanner's 25-second capital budget and incorrectly downgrading an otherwise usable candidate to `capital_error`.

**Architecture:** Preserve the existing full `capital_flow.py` behavior for the portfolio and single-stock pipeline. Add an explicit `--skip-extended` mode that stops after the primary five-day capital-flow result; daily candidate scanning will always request that mode. The primary Eastmoney → Tushare → K-line-estimate fallback chain, expected-date validation, and candidate quality gate remain unchanged. Keep full and primary-only cache semantics isolated so one caller cannot reuse the other caller's enrichment contract.

**Tech Stack:** Python 3.10, `argparse`, `unittest`, `unittest.mock`, existing stock-trend fetcher/scanner scripts.

---

## Scope and non-goals

- The affected area is `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py` and the candidate-facing call in `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`.
- Do not modify `NO_PROXY`: it already bypasses every `push2*.eastmoney.com` host, and direct/proxy probes both returned HTTP 200 outside the execution sandbox.
- Do not change `is_valid_capital_result`, fallback precedence, cache TTLs, recommendation thresholds, or the treatment of `kline_estimate`; those are separate policy decisions.
- Do not regenerate golden snapshots unless the final output contract changes intentionally. This patch changes subprocess arguments and fetch timing only, so golden output is expected to remain unchanged.

## Files

- Modify: `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py`
  - Add the candidate-only switch, record whether extended enrichment was skipped,
    and prevent reduced candidate results from contaminating the shared full-fetch cache.
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
  - Pass the switch only from the daily candidate scanner path.
- Modify: `.claude/skills/stock-trend/tests/test_capital_flow.py`
  - Lock the CLI switch's behavior and backward compatibility.
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
  - Lock the scanner-to-fetcher command contract.

### Task 1: Lock the fetcher contract with failing tests

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_capital_flow.py`

- [x] **Step 1: Add a failing test proving `--skip-extended` returns valid primary data without calling optional sources**

  Add this method to `TestCapitalFlowCacheValidation`:

  ```python
  def test_skip_extended_omits_optional_enrichment(self):
      outputs = []
      fetched = {
          "meta": {"data_source": "eastmoney", "record_count": 1},
          "data": [VALID_FLOW],
      }
      with patch.object(sys, "argv", [
              "capital_flow.py", "600519.SH", "--skip-extended",
          ]), \
              patch.object(capital_flow, "load_cache", return_value=None), \
              patch.object(capital_flow, "resolve_secid", return_value="1.600519"), \
              patch.object(capital_flow,
                           "fetch_stock_capital_flow_with_fallbacks",
                           return_value=fetched), \
              patch.object(capital_flow, "fetch_northbound_flow") as northbound, \
              patch.object(capital_flow,
                           "fetch_individual_northbound") as individual, \
              patch.object(capital_flow, "fetch_margin_detail") as margin, \
              patch.object(capital_flow, "fetch_longhubang") as lhb, \
              patch.object(capital_flow, "output_json",
                           side_effect=lambda value, **_: outputs.append(value)), \
              patch.object(capital_flow, "save_cache"):
          capital_flow.main()

      result = outputs[0]
      self.assertEqual(result["data"], [VALID_FLOW])
      self.assertEqual(result["meta"]["enrichment"], "skipped")
      self.assertNotIn("northbound_market", result["data_extended"])
      self.assertNotIn("northbound_individual", result["data_extended"])
      self.assertNotIn("margin", result["data_extended"])
      self.assertNotIn("longhubang", result["data_extended"])
      self.assertIn("individual_streak", result["data_extended"])
      northbound.assert_not_called()
      individual.assert_not_called()
      margin.assert_not_called()
      lhb.assert_not_called()
  ```

- [x] **Step 2: Add a failing backward-compatibility test for the default CLI behavior**

  Add this method to the same class:

  ```python
  def test_default_mode_keeps_optional_enrichment(self):
      result, _, _ = self._run_main(None)
      self.assertEqual(result["meta"]["enrichment"], "attempted")
  ```

  Update `_run_main` to keep its existing mocks and make each optional source return `None`; this ensures the test measures control flow rather than network behavior.

- [x] **Step 3: Run the focused tests and confirm they fail for the missing argument/metadata**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  ```

  Expected: the new `--skip-extended` test fails because `argparse` does not yet recognize the flag, and the default-mode test fails because `meta.enrichment` does not yet exist.

### Task 2: Implement bounded candidate-only capital retrieval

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py:450-576`
- Test: `.claude/skills/stock-trend/tests/test_capital_flow.py`

- [x] **Step 1: Add the CLI switch with safe default behavior**

  After the existing `--expected-date` argument, add:

  ```python
  parser.add_argument(
      "--skip-extended", action="store_true",
      help="Skip optional northbound, margin, and 龙虎榜 enrichment",
  )
  ```

  The default must remain `False`, preserving all existing non-candidate callers.

- [x] **Step 2: Gate only the optional enrichment block**

  Replace the current unconditional block beginning with `if asset == "E" and not is_hk:` with:

  ```python
  if asset == "E" and not is_hk and not args.skip_extended:
      result["meta"]["enrichment"] = "attempted"
      try:
          nb_market = fetch_northbound_flow()
          if nb_market:
              result["data_extended"]["northbound_market"] = nb_market
      except Exception as e:
          errors.append(f"北向资金: {e}")
      try:
          nb_individual = fetch_individual_northbound(code)
          if nb_individual:
              result["data_extended"]["northbound_individual"] = nb_individual
      except Exception as e:
          errors.append(f"个股北向: {e}")
      try:
          exchange = "SH" if suffix == ".SH" else "SZ"
          margin = fetch_margin_detail(code, exchange)
          if margin:
              result["data_extended"]["margin"] = margin
      except Exception as e:
          errors.append(f"融资融券: {e}")
      try:
          lhb = fetch_longhubang(code)
          if lhb:
              result["data_extended"]["longhubang"] = lhb
      except Exception as e:
          errors.append(f"龙虎榜: {e}")
  elif asset == "E" and not is_hk:
      result["meta"]["enrichment"] = "skipped"
  ```

  Do not move or alter the subsequent `individual_streak` calculation: it uses the required primary `result["data"]`, not `data_extended`.

  When skip mode is active, write the requested `-o` output (the candidate scanner's
  private cache) but do not write the shared `capital_flow_<ts_code>` cache. If the
  default full mode encounters a shared cache marked `enrichment: "skipped"`, it
  must refetch rather than treating the reduced payload as a full-fetch result.

- [x] **Step 3: Run the focused fetcher tests and confirm they pass**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  ```

  Expected: all tests pass; the switch leaves valid primary rows and the candidate-private output intact, calls no optional source in skip mode, and does not write the shared full-fetch cache.

### Task 3: Make daily candidate scanning opt into bounded mode

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:927-976`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [x] **Step 1: Add a failing scanner command-contract test**

  Add this method to `TestMetadata`:

  ```python
  def test_capital_subprocess_skips_optional_enrichment(self):
      refreshed = self._valid_capital("20260813")
      with tempfile.TemporaryDirectory() as tmpdir, \
              patch.object(sc, "CACHE_DIR", tmpdir), \
              patch.object(sc, "_read_json", side_effect=[None, refreshed]), \
              patch.object(sc, "run_script",
                           return_value={"success": True}) as run:
          sc._fetch_capital_flow(
              "600001.SH", expected_trading_date="2026-08-13")

      cmd = run.call_args.args[0]
      self.assertIn("--skip-extended", cmd)
      self.assertEqual(cmd[cmd.index("--expected-date") + 1], "2026-08-13")
  ```

- [x] **Step 2: Run the focused scanner tests and confirm the new assertion fails**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  ```

  Expected: the new test fails because the command does not yet include `--skip-extended`.

- [x] **Step 3: Add the switch to the scanner-owned subprocess command**

  In `_fetch_capital_flow`, construct the command as:

  ```python
  cmd = [
      sys.executable, str(SCRIPT_DIR / "fetchers/capital_flow.py"),
      ts_code, "--asset", "E", "--skip-extended",
      "-o", str(cache_path),
  ]
  ```

  Retain the existing conditional append of `--expected-date`; it is required for same-day freshness validation.

- [x] **Step 4: Run the focused scanner tests and confirm they pass**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
  ```

  Expected: all tests pass, including existing `capital_error` and source-evidence regression cases.

### Task 4: Verify timing behavior and the repository quality gates

**Files:**

- Verify only; no additional production files.

- [x] **Step 1: Verify the exact CLI output in an isolated cache directory**

  Run:

  ```bash
  probe_cache=$(mktemp -d)
  STOCK_TREND_CACHE_DIR="$probe_cache" \
    python3 .claude/skills/stock-trend/scripts/fetchers/capital_flow.py \
    603228.SH --asset E --expected-date 2026-08-27 --skip-extended \
    -o /private/tmp/capital-flow-skip-extended.json
  ```

  Expected: the JSON has a non-error `meta.data_source`, `meta.enrichment == "skipped"`, no optional enrichment keys (`northbound_market`, `northbound_individual`, `margin`, or `longhubang`), and valid rows through `20260827`. The existing primary-flow-derived `individual_streak` key may remain. If the upstream primary feed is unavailable, `kline_estimate` is acceptable because its existing fallback policy is intentionally unchanged.

  Executed with the existing 603228 K-line cache as the permitted fallback: output was
  `data_source=kline_estimate`, `enrichment=skipped`, `individual_streak` present,
  no optional keys, and the shared cache was not written.

- [x] **Step 2: Run the mandatory project quality gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both exit 0. Investigate any golden diff; do not regenerate snapshots merely to hide a failure.

- [x] **Step 3: Run an available recommendation smoke test**

  Run after market close:

  ```bash
  python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py --no-html
  python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
    --top 30 --min-candidates 20 --no-html
  ```

  Expected: source-health audit reports zero capital subprocess timeouts under normal provider conditions. A genuine primary-data failure must still be recorded as `capital_error`; the change only removes optional enrichment as a cause of that failure.

  Executed an intraday candidate scan on 2026-08-27: capital phase completed in
  40.156s with 52 requests, 0 failures, 0 circuit trips, and no capital timeout;
  the full scan exited 0. A post-close run remains a recommended operational follow-up.

### Task 5: Review and commit

**Files:**

- Modify: the four files listed above.

- [x] **Step 1: Review the diff for scope containment**

  Run:

  ```bash
  git diff --check
  git diff -- .claude/skills/stock-trend/scripts/fetchers/capital_flow.py \
    .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
    .claude/skills/stock-trend/tests/test_capital_flow.py \
    .claude/skills/stock-trend/tests/test_stock_scanner.py
  ```

  Expected: no formatting errors; no changes to proxy configuration, scoring, quality thresholds, or golden fixtures.

- [ ] **Step 2: Commit the focused repair**

  Run:

  ```bash
  git add .claude/skills/stock-trend/scripts/fetchers/capital_flow.py \
    .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
    .claude/skills/stock-trend/tests/test_capital_flow.py \
    .claude/skills/stock-trend/tests/test_stock_scanner.py
  git commit -m "fix(scan): isolate optional capital enrichment"
  ```

  Commit body, if used, must explain that the candidate scanner retains the primary source/fallback chain but skips non-essential AKShare enrichment to preserve its bounded source deadline.

  Not run: the current workspace is on `main`, and the sandbox exposes `.git` as
  read-only, so branch creation and commit creation are unavailable. The code and
  tests remain as uncommitted working-tree changes for handoff.

## Self-review

- **Coverage:** Tasks 1-2 prevent optional enrichment from consuming the capital budget and isolate cache contracts; Task 3 applies the behavior only to candidate scanning; Task 4 validates behavior, mandatory quality gates, and an intraday live scan; Task 5 keeps the diff reviewable.
- **No placeholders:** Every production/test change includes exact paths, symbols, commands, assertions, and expected behavior.
- **Consistency:** The flag is named `--skip-extended` in fetcher parsing, scanner command construction, tests, and validation. `meta.enrichment` has only `attempted` or `skipped` values for A-share stock requests.
