# Individual Stock Trend Reliability Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/stock-trend` individual-stock conclusions fail closed on invalid or stale data, publish only artifacts produced and validated by the current pipeline run, and keep action labels consistent with the actual target and risk/reward evidence.

**Architecture:** Deliver the work in two milestones. Milestone A repairs contracts and freshness without intentionally changing valid-input scores. Milestone B fixes two bounded decision-quality defects: missing indicators diluting the technical score and ATR-projected targets being presented as actionable. Reuse existing cache, recommendation-quality, and report structures; add no dependencies and do not redesign the scoring model until historical calibration evidence exists.

**Tech Stack:** Python 3.10, standard library, existing standalone `unittest`-style test runners, JSON file contracts, Markdown skill documentation.

---

## Delivery strategy

### Milestone A — correctness and freshness

Tasks 1–3 are release-blocking repairs. They must preserve the numerical output for a complete, fresh, valid fixture while preventing invalid or stale dimensions from influencing a report.

### Milestone B — bounded decision-quality repair

Tasks 4–5 may change output for incomplete data or weak target evidence. Review every golden diff as a semantic change. Do not regenerate snapshots merely to obtain a green test run.

### Explicitly deferred

- Do not replace the current composite weights with guessed “better” weights.
- Do not introduce factor-cluster weights for MA/MACD/ADX versus RSI/KDJ until a historical replay evaluator exists.
- Do not add brokerage-cost or slippage defaults to production recommendations until the attribution data records an explicit cost model.
- Do not refactor the large analysis files merely for style; this plan is a behavioral repair.

Those items require a separate calibration plan after the repaired pipeline has accumulated trustworthy recommendation snapshots. Task 6 makes the current heuristic model explicit so it can be calibrated later.

## File map

| File | Responsibility in this repair |
| --- | --- |
| `.claude/skills/stock-trend/scripts/analysis/technical.py` | Emit one consistent technical schema; exclude unavailable indicators from the technical-score denominator; expose honest target provenance. |
| `.claude/skills/stock-trend/scripts/analysis/scores.py` | Validate the schema actually emitted by `technical.py`; fail closed on `data_quality=error`; carry target provenance into reports. |
| `.claude/skills/stock-trend/scripts/pipeline/runner.py` | Resolve the expected trading date, pass freshness requirements to fetchers, validate current-run outputs, and publish only usable artifacts. |
| `.claude/skills/stock-trend/scripts/fetchers/kline.py` | Apply stale-by-date validation to Tushare cache hits and fetched K-line results. |
| `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py` | Preserve its existing date validation metadata and ensure stale fetched results are explicitly unusable. |
| `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py` | Consume the expected trading date already supported by its CLI; no scoring change. |
| `.claude/skills/stock-trend/scripts/reporting/report.py` | Gate action labels on target provenance as well as direction, confidence, and R:R. |
| `.claude/skills/stock-trend/tests/test_stock_trend.py` | Contract, pipeline freshness, stale-artifact, technical-score, and report-action regressions. |
| `.claude/skills/stock-trend/tests/test_capital_flow.py` | Verify the runner/fetcher expected-date contract remains compatible with capital-flow validation. |
| `.claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py` | Lock the six-dimension composite weight contract and its total. |
| `.claude/skills/stock-trend/SKILL.md` | Match user-facing scoring and freshness documentation to the implemented six-dimension model. |

## Task 0: Establish a clean behavioral baseline

**Files:**

- Verify only: `.claude/skills/stock-trend/tests/test_stock_trend.py`
- Verify only: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Confirm the worktree state before implementation**

  Run:

  ```bash
  git status --short
  ```

  Expected: record the existing output. Preserve all user-owned changes and do not mix unrelated files into later commits.

- [ ] **Step 2: Run the two mandatory baseline gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0. If either fails before implementation, record the exact pre-existing failure and do not attribute it to this repair.

- [ ] **Step 3: Do not commit the baseline**

  This task is evidence collection only. It must leave the worktree unchanged.

## Task 1: Align the technical-analysis schema and fail closed on technical errors

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py:1698-1799`
- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py:1825-1854`
- Modify: `.claude/skills/stock-trend/scripts/analysis/scores.py:569-650`
- Modify: `.claude/skills/stock-trend/scripts/analysis/scores.py:858-894`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:383-399`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:1316-1370`

- [ ] **Step 1: Add failing schema-contract tests**

  First migrate the existing `VI-valid-tech` and `VI-bad-quality` fixtures to the canonical emitted layout so the suite no longer preserves the old, incorrect contract:

  ```python
  valid_tech = {
      "summary": {
          "total_score": 1.5,
          "direction": "neutral",
          "confidence": "low",
          "data_quality": "good",
      },
  }
  bad_quality_tech = {
      "summary": {
          "total_score": 1.5,
          "direction": "neutral",
          "confidence": "low",
          "data_quality": "invalid_quality",
      },
  }
  ```

  Extend `run_validate_tests()` with a fixture matching the real `technical.py` output:

  ```python
  emitted_technical = {
      "meta": {"ts_code": "600519.SH", "data_points": 120},
      "summary": {
          "total_score": 1.5,
          "direction": "neutral",
          "confidence": "low",
          "consistency": 0.5,
          "data_quality": "good",
      },
  }
  errors = validate_input(emitted_technical, valid_scores)
  test(
      "VI-emitted-schema: technical.py真实输出通过校验",
      errors == [],
      f"errors={errors}",
      "validate",
  )
  ```

  Add an error-quality test:

  ```python
  error_technical = {
      "meta": {"ts_code": "600519.SH", "error": "no data"},
      "summary": {
          "total_score": 0,
          "direction": "neutral",
          "confidence": "low",
          "consistency": 0.0,
          "data_quality": "error",
      },
  }
  errors = validate_input(error_technical, valid_scores)
  test(
      "VI-error-quality: error是合法枚举但不可用于评分",
      not any("data_quality" in value and "must be one of" in value for value in errors),
      f"errors={errors}",
      "validate",
  )
  ```

  Extend TA-04 so an error input must expose a complete non-actionable schema:

  ```python
  test("TA-04b: 空数据quality=error",
       summary.get("data_quality") == "error",
       f"summary={summary}", "analyze")
  test("TA-04c: 空数据无操作价位",
       summary.get("stop_loss") is None
       and not summary.get("target_moderate")
       and summary.get("entry_signals", {}).get("verdict") == "wait",
       f"summary={summary}", "analyze")
  ```

- [ ] **Step 2: Run the focused suite and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  ```

  Expected: the real emitted schema fails because `scores.validate_input()` expects numeric confidence and reads quality at the root; TA-04 fails because the error summary lacks `data_quality` and the non-actionable fields.

- [ ] **Step 3: Make `technical.py` emit one complete summary shape**

  Add a small helper beside `build_summary()` and use it in both error branches:

  ```python
  def build_unavailable_summary(message):
      return {
          "total_score": 0,
          "direction": "neutral",
          "confidence": "low",
          "consistency": 0.0,
          "data_quality": "error",
          "key_signals": [message],
          "dimension_scores": {},
          "support_levels": [],
          "resistance_levels": [],
          "stop_loss": None,
          "target_conservative": None,
          "target_moderate": None,
          "target_aggressive": None,
          "target": None,
          "risk_reward_ratio": None,
          "favorable_rr": False,
          "position_sizing": "不建议建仓",
          "position_tier": 0,
          "risk_reward_warning": message,
          "entry_signals": {
              "signal_count": 0,
              "signals": [],
              "verdict": "wait",
          },
      }
  ```

  Preserve the original provider error under `meta.error`. Use `build_unavailable_summary("无K线数据，技术面无法分析")` for a provider error and `build_unavailable_summary("无数据记录，技术面无法分析")` for an empty `data` array.

- [ ] **Step 4: Make `scores.py` validate the actual schema location and types**

  Replace the mismatched contract with:

  ```python
  required_summary_keys = {
      "total_score": (int, float),
      "direction": str,
      "confidence": str,
      "data_quality": str,
  }
  valid_confidence = {"low", "medium", "high"}
  valid_qualities = {"good", "limited", "insufficient", "partial", "error"}
  ```

  Validate `summary["confidence"]` against `valid_confidence` and read quality from the canonical location:

  ```python
  dq = summary.get("data_quality") if isinstance(summary, dict) else None
  if dq not in valid_qualities:
      errors.append(
          "technical_data['summary']['data_quality'] must be one of "
          f"{sorted(valid_qualities)}, got {dq!r}"
      )
  ```

  Do not keep the root-level `technical_data["data_quality"]` as a second source of truth.

- [ ] **Step 5: Stop composite scoring when technical quality is `error`**

  Immediately after loading `summary` in `scores.main()`, add:

  ```python
  data_quality = summary.get("data_quality")
  if data_quality == "error":
      print("Error: technical data quality is error; composite scoring disabled",
            file=sys.stderr)
      raise SystemExit(1)
  ```

  Keep `limited` and `insufficient` as supported degraded modes; their existing weight redistribution remains active.

- [ ] **Step 6: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0. Valid fixtures retain their scores. Error fixtures are explicitly non-actionable. No golden regeneration is permitted.

- [ ] **Step 7: Commit the schema repair**

  ```bash
  git add .claude/skills/stock-trend/scripts/analysis/technical.py .claude/skills/stock-trend/scripts/analysis/scores.py .claude/skills/stock-trend/tests/test_stock_trend.py
  git commit -m "fix(stock-trend): align technical quality contract"
  ```

## Task 2: Enforce expected trading dates for daily K-line and capital data

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/pipeline/runner.py:115-250`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/kline.py:241-340`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py:321-346`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py:459-489`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:1035-1130`
- Test: `.claude/skills/stock-trend/tests/test_capital_flow.py`

- [ ] **Step 1: Add failing command-construction tests**

  Add a runner test that freezes the expected date through a new CLI override and captures subprocess commands:

  ```python
  captured = {}

  def fake_run_script(cmd, label="", timeout=30):
      captured[label] = list(cmd)
      # Reuse the existing valid fixture writer for the requested -o path.
      return {
          "success": True, "label": label, "returncode": 0,
          "stdout": "", "stderr": "",
      }
  ```

  Invoke `runner.main()` with `--expected-date 2026-08-26` and assert:

  ```python
  for label in ("fetch_kline_tushare", "fetch_capital_flow"):
      test(
          f"TP-DATE-{label}: 传递expected-date",
          captured[label][-2:] == ["--expected-date", "2026-08-26"],
          f"cmd={captured[label]}",
          "pipeline",
      )
  ```

  Add the same assertion for `fetch_kline_eastmoney` in the fallback branch. Restrict the test to daily frequency; weekly bars are outside this exact-date contract.

- [ ] **Step 2: Add failing Tushare cache-freshness tests**

  Extract or import a public `latest_kline_date()` helper and test two payloads:

  ```python
  self.assertFalse(kline_covers_date(
      {"data": [{"trade_date": "20260825"}]}, "2026-08-26"))
  self.assertTrue(kline_covers_date(
      {"data": [{"trade_date": "20260826"}]}, "2026-08-26"))
  ```

  Add a CLI-level test showing a TTL-valid Tushare cache dated 2026-08-25 is ignored when `--expected-date 2026-08-26` is supplied.

- [ ] **Step 3: Run focused tests and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  ```

  Expected: runner commands lack the date argument and `kline.py` accepts the stale TTL cache.

- [ ] **Step 4: Add a deterministic expected-date entry point to the runner**

  Add the CLI option:

  ```python
  parser.add_argument(
      "--expected-date",
      help="Expected latest daily trading date (YYYY-MM-DD); defaults to the best available trading calendar",
  )
  ```

  Resolve it once after parsing:

  ```python
  def resolve_expected_date(explicit=""):
      if explicit:
          return explicit, "cli"
      from fetchers.sector_data import get_last_trading_day
      return get_last_trading_day(now=datetime.now())

  expected_date, expected_date_source = resolve_expected_date(args.expected_date)
  daily_expected_date = expected_date if args.freq == "D" else ""
  ```

  Record both values in `pipeline_output["meta"]`. If no calendar source is available, leave `daily_expected_date` empty and append a pipeline warning; do not invent a date.

- [ ] **Step 5: Pass the expected date to every daily freshness-aware fetcher**

  Append the same pair to the Tushare K-line, EastMoney fallback, and capital commands when `daily_expected_date` is non-empty:

  ```python
  cmd.extend(["--expected-date", daily_expected_date])
  ```

  Do not pass an exact expected date to `--freq W`; weekly bar labelling differs by provider.

- [ ] **Step 6: Give `kline.py` the same stale-by-date behavior as the fallback**

  Add `--expected-date` and a shared predicate:

  ```python
  def kline_covers_date(payload, expected_date=""):
      if not expected_date:
          return True
      return latest_kline_date(payload) >= expected_date
  ```

  A cache hit is valid only when both TTL and date coverage pass. After a fresh fetch, set explicit validation metadata:

  ```python
  result["meta"]["cache_validation"] = {
      "valid": kline_covers_date(result, args.expected_date),
      "expected_date": args.expected_date or "",
      "latest_data_date": latest_kline_date(result),
  }
  ```

  Do not save a result whose `cache_validation.valid` is false. Emit it for diagnostics so the runner can report the actual source and lag.

  Apply the same metadata shape in `kline_eastmoney.py`; its existing warning may remain, but a stale fetched payload must set `valid: false` and must not be cached.

- [ ] **Step 7: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: all commands exit 0. Daily commands carry the expected date, stale cache fixtures are bypassed, and weekly commands remain unchanged. Do not regenerate golden files.

- [ ] **Step 8: Commit the freshness contract**

  ```bash
  git add .claude/skills/stock-trend/scripts/pipeline/runner.py .claude/skills/stock-trend/scripts/fetchers/kline.py .claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py .claude/skills/stock-trend/tests/test_stock_trend.py .claude/skills/stock-trend/tests/test_capital_flow.py
  git commit -m "fix(stock-trend): enforce daily data freshness"
  ```

## Task 3: Publish only supplementary artifacts validated in the current run

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/pipeline/runner.py:53-105`
- Modify: `.claude/skills/stock-trend/scripts/pipeline/runner.py:370-570`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:1035-1130`

- [ ] **Step 1: Add stale-artifact regression fixtures**

  Create stale files in the existing temporary pipeline directory before invoking the runner:

  ```python
  stale_payloads = {
      "capital_flow.json": {
          "meta": {"data_source": "eastmoney", "record_count": 1},
          "data": [{"date": "20260825", "main_net_inflow": 1}],
      },
      "fundamental.json": {
          "meta": {"data_source": "akshare"},
          "summary": {"data_quality": "good", "pe_ttm": 20},
      },
      "macro_snapshot.json": {
          "meta": {"data_source": "akshare"},
          "summary": {"data_quality": "good"},
      },
  }
  for name, payload in stale_payloads.items():
      _write_json(str(stale_flow_dir / name), payload)
  ```

  Make the fake subprocess fail each supplementary fetch. Assert after `runner.main()`:

  ```python
  for key, filename in (
      ("capital_flow", "capital_flow.json"),
      ("fundamental", "fundamental.json"),
      ("macro_snapshot", "macro_snapshot.json"),
  ):
      test(
          f"TP-STALE-{key}: 失败后不发布旧文件",
          output_files.get(key) is None
          and not (stale_flow_dir / filename).exists(),
          f"output_files={output_files}",
          "pipeline",
      )
  ```

- [ ] **Step 2: Add semantic-failure tests**

  Add fixtures where the subprocess exits 0 but writes:

  ```python
  capital_error = {"meta": {"data_source": "error", "error": "failed"}, "data": []}
  fundamental_error = {"summary": {"data_quality": "error"}, "data": {}}
  macro_error = {"summary": {"data_quality": "error"}, "data": {}}
  ```

  Assert those dimensions are absent from `output_files` and represented in `results` with `available: false`, `quality: error`, and a stable reason code.

- [ ] **Step 3: Run the focused suite and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  ```

  Expected: old supplementary files remain present or their paths are still published.

- [ ] **Step 4: Replace flag-only publication with current-run availability**

  Initialize a current-run map before starting work:

  ```python
  available_outputs = {
      "kline": False,
      "technical": False,
      "etf_data": False,
      "capital_flow": False,
      "fundamental": False,
      "macro_snapshot": False,
      "futures_data": False,
      "index_valuation": False,
      "chip_distribution": False,
      "wyckoff": False,
  }
  ```

  Change `build_output_files()` to accept this mapping and return a path only when `available_outputs[key] is True`. Keep the existing asset and CLI-skip checks as additional constraints.

- [ ] **Step 5: Validate payload semantics before marking an output available**

  Add a local helper with stable output:

  ```python
  def assess_pipeline_payload(label, payload, expected_date=""):
      if not isinstance(payload, dict):
          return False, "error", f"{label}_malformed"
      meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
      summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
      quality = summary.get("data_quality") or payload.get("data_quality") or "good"
      if meta.get("data_source") == "error" or meta.get("error") or quality == "error":
          return False, "error", f"{label}_error"
      validation = meta.get("cache_validation") if isinstance(meta.get("cache_validation"), dict) else {}
      if validation.get("valid") is False:
          return False, "stale", f"{label}_stale"
      if label == "capital_flow" and expected_date:
          from fetchers.capital_flow import latest_capital_date
          if latest_capital_date(payload) < expected_date:
              return False, "stale", "capital_flow_stale"
      return True, quality, ""
  ```

  Mark availability only after this helper succeeds. Write a normalized result for every attempted dimension:

  ```python
  results[key] = {
      "available": usable,
      "quality": quality,
      "reason": reason,
  }
  ```

  Preserve existing dimension-specific metrics such as `record_count` and `pe_ttm` alongside these fields.

- [ ] **Step 6: Remove stale per-code artifacts after failure or semantic rejection**

  Call the existing `remove_stale_file(path, label, errors)` for each attempted supplementary dimension which fails. Do not delete the shared TTL cache file; only remove the per-code artifact that the report loader could otherwise reuse.

  Also require semantic success before setting `technical_available=True`; a technical payload with `summary.data_quality == "error"` must be removed and excluded.

- [ ] **Step 7: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0; stale per-code supplementary files disappear; `pipeline_output.json` never advertises an invalid artifact. Do not regenerate golden files.

- [ ] **Step 8: Commit the artifact-publication repair**

  ```bash
  git add .claude/skills/stock-trend/scripts/pipeline/runner.py .claude/skills/stock-trend/tests/test_stock_trend.py
  git commit -m "fix(stock-trend): publish only current pipeline data"
  ```

## Task 4: Stop unavailable indicators from diluting the technical score

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py:1698-1744`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:357-428`

- [ ] **Step 1: Add a direct aggregation regression**

  Add a unit-style test that calls `build_summary()` with one valid bullish signal and one unavailable indicator:

  ```python
  indicators = {
      "ma": {
          "signal": {
              "type": "bullish_align",
              "description": "多头排列",
              "score": 2,
          },
      },
      "macd": {
          "signal": {
              "type": "insufficient_data",
              "description": "MACD数据不足",
              "score": 0,
          },
      },
  }
  summary = build_summary(indicators, [], data_points=20)
  test(
      "TA-SCORE-AVAILABLE: 无效指标不进入分母",
      summary["total_score"] == 2.0,
      f"summary={summary}",
      "analyze",
  )
  test(
      "TA-SCORE-COVERAGE: 输出指标覆盖率",
      summary["indicator_coverage"] == 0.5,
      f"summary={summary}",
      "analyze",
  )
  ```

  Add an all-unavailable case and assert `total_score == 0`, `indicator_coverage == 0`, and `confidence == "low"`.

- [ ] **Step 2: Run the focused suite and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  ```

  Expected: the first score is diluted because the unavailable MACD weight is currently included.

- [ ] **Step 3: Restrict the weighted denominator to valid indicators**

  In `build_summary()`, move weighted accumulation into the validity branch:

  ```python
  expected_weight = 0.0
  available_weight = 0.0
  for name, result in indicator_results.items():
      signal = result.get("signal", {})
      score = signal.get("score", 0)
      weight = SUB_WEIGHTS.get(name, 1.0)
      expected_weight += weight
      if signal.get("type") == "insufficient_data":
          continue
      weighted_sum += score * weight
      weight_total += weight
      available_weight += weight
      valid_scores.append(score)
  ```

  Add pattern weight to both expected and available weight only when a pattern calculation was actually attempted from valid OHLC data. Expose:

  ```python
  result["indicator_coverage"] = round(
      available_weight / expected_weight, 2
  ) if expected_weight else 0.0
  ```

  Do not add factor-cluster weights in this task. Preserve all existing sub-weights for available indicators.

- [ ] **Step 4: Make coverage constrain confidence**

  After the existing confidence calculation, apply:

  ```python
  if result_coverage < 0.5:
      confidence = "low"
  elif result_coverage < 0.75 and confidence == "high":
      confidence = "medium"
  ```

  Use a local `result_coverage` value before constructing the result dictionary so `confidence` and `indicator_coverage` cannot disagree.

- [ ] **Step 5: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: complete golden fixtures remain numerically unchanged; incomplete fixtures no longer drift toward neutral solely because an unavailable indicator contributed a zero with positive weight. Do not regenerate golden files unless each diff is independently justified and reviewed.

- [ ] **Step 6: Commit the aggregation repair**

  ```bash
  git add .claude/skills/stock-trend/scripts/analysis/technical.py .claude/skills/stock-trend/tests/test_stock_trend.py
  git commit -m "fix(stock-trend): exclude unavailable indicator weights"
  ```

## Task 5: Make individual-stock action labels respect target provenance

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py:1393-1476`
- Modify: `.claude/skills/stock-trend/scripts/analysis/technical.py:1678-1692`
- Modify: `.claude/skills/stock-trend/scripts/analysis/scores.py:1412-1434`
- Modify: `.claude/skills/stock-trend/scripts/reporting/report.py:222-336`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:485-562`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py:618-875`

- [ ] **Step 1: Add failing target-provenance tests**

  Extend the report fixture helper to accept `target_source` and place it in `scores["report_params"]`:

  ```python
  def _write_report_fixture(
      tmpdir, name, *, confidence="中", rr_ratio=2.2,
      latest_close=1260.0, target_source="resistance",
  ):
      # existing fixture body
      scores["report_params"]["target_source"] = target_source
  ```

  Add direct `build_action_plan()` assertions:

  ```python
  atr_plan = build_action_plan(
      "看多", "高", 100.0,
      {
          "support_levels": [99.0],
          "resistance_levels": [105.0],
          "stop_loss": 97.0,
          "target_conservative": 104.0,
          "target_moderate": 108.0,
          "risk_reward_ratio": 2.67,
          "target_source": "atr_projection",
      },
      {},
  )
  test("TF-RPT-ATR: ATR目标只能观察",
       atr_plan["今日动作标签"] == "只观察",
       f"plan={atr_plan}", "report")
  ```

  Add a `resistance` case with the same numbers and assert it may reach `可低吸` or `等回踩` depending on price proximity.

- [ ] **Step 2: Add a target-source calculator regression**

  For legacy/current-close calculations, assert:

  ```python
  self.assertEqual(with_resistance["target_source"], "resistance")
  self.assertEqual(with_atr_only["target_source"], "atr_projection")
  self.assertEqual(without_targets["target_source"], "unavailable")
  ```

  The current legacy path leaves the source as `legacy`, so these assertions must fail before implementation.

- [ ] **Step 3: Run the focused suite and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  ```

  Expected: ATR-projected targets can currently receive a non-observation action and legacy calculations do not expose honest provenance.

- [ ] **Step 4: Set target provenance in every `calc_risk_reward()` branch**

  Replace the `legacy` default with `unavailable`. Set:

  ```python
  target_source = "resistance"
  ```

  only when the selected target ladder is derived from resistance prices. Set:

  ```python
  target_source = "atr_projection"
  ```

  when ATR constructs the targets because usable resistance is absent. Leave it `unavailable` when neither source can produce a valid target.

  Keep the current numerical R:R calculation for display; provenance controls actionability.

- [ ] **Step 5: Align the entry-verdict R:R threshold with the report gate**

  Change the individual-stock threshold in `calc_entry_signals()` from `1.0` to `1.5`:

  ```python
  rr_entry_threshold = 2.0 if is_etf else 1.5
  ```

  Recalculate `signal_count` after appending the `R:R偏低` signal so the count matches the returned list:

  ```python
  return {
      "signal_count": len(signals_found),
      "signals": signals_found,
      "verdict": verdict,
  }
  ```

- [ ] **Step 6: Propagate target provenance into the report action gate**

  Add this field to `scores.py` report parameters:

  ```python
  "target_source": summary.get("target_source", "unavailable"),
  ```

  In `build_action_plan()` read it and include it in the observation condition:

  ```python
  target_source = str(report_params.get("target_source") or "unavailable")
  executable_target = target_source == "resistance"
  if (is_bearish or low_confidence or incomplete_decision_levels
          or rr_ratio < 1.5 or not executable_target):
      action_label = "只观察"
  ```

  Add a concise reason to the observation detail when `target_source == "atr_projection"`: `"目标来自ATR投射，仅作情景参考。"`

- [ ] **Step 7: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: both commands exit 0; ATR-only targets remain visible but cannot produce `可低吸` or `等回踩`; resistance-based fixtures preserve actionable behavior. Review any golden changes line by line and do not regenerate snapshots automatically.

- [ ] **Step 8: Commit the actionability repair**

  ```bash
  git add .claude/skills/stock-trend/scripts/analysis/technical.py .claude/skills/stock-trend/scripts/analysis/scores.py .claude/skills/stock-trend/scripts/reporting/report.py .claude/skills/stock-trend/tests/test_stock_trend.py
  git commit -m "fix(stock-trend): gate actions by target evidence"
  ```

## Task 6: Version and document the current heuristic scoring model

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/scores.py:44-76`
- Modify: `.claude/skills/stock-trend/scripts/analysis/scores.py:1383-1401`
- Modify: `.claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py`
- Modify: `.claude/skills/stock-trend/SKILL.md:389-430`

- [ ] **Step 1: Add a failing scoring-contract test**

  Add to `test_scores_wyckoff_mode.py`:

  ```python
  class TestCompositeWeightContract(unittest.TestCase):
      def test_default_weights_are_versioned_and_normalized(self):
          self.assertEqual(sc.SCORING_VERSION, "composite-v2-wyckoff")
          self.assertEqual(
              set(sc.DEFAULT_WEIGHTS),
              {"technical", "capital_flow", "fundamental",
               "sentiment", "macro", "wyckoff"},
          )
          self.assertAlmostEqual(sum(sc.DEFAULT_WEIGHTS.values()), 1.0)

      def test_every_focus_weight_set_is_normalized(self):
          for name, weights in sc.FOCUS_WEIGHTS.items():
              with self.subTest(name=name):
                  self.assertEqual(set(weights), set(sc.DEFAULT_WEIGHTS))
                  self.assertAlmostEqual(sum(weights.values()), 1.0)
  ```

- [ ] **Step 2: Run the focused test and confirm RED**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
  ```

  Expected: the version assertion fails because no scoring-version constant exists.

- [ ] **Step 3: Add explicit model-version metadata without changing weights**

  Add beside the weight constants:

  ```python
  SCORING_VERSION = "composite-v2-wyckoff"
  SCORING_STATUS = "heuristic_unvalidated"
  ```

  Include both in `scores.json`:

  ```python
  "scoring_version": SCORING_VERSION,
  "scoring_status": SCORING_STATUS,
  ```

  Do not change `DEFAULT_WEIGHTS` or `FOCUS_WEIGHTS` in this task.

- [ ] **Step 4: Correct the `/stock-trend` documentation**

  Update the scoring table in `SKILL.md` to match the implemented model:

  ```text
  默认权重：技术28% / 资金23% / 基本14% / 情绪14% / 宏观9% / 维科夫12%
  technical focus：技术45% / 资金15% / 基本8% / 情绪8% / 宏观8% / 维科夫16%
  capital_flow focus：资金40% / 技术15% / 基本8% / 情绪8% / 宏观8% / 维科夫21%
  fundamental focus：基本35% / 宏观15% / 技术10% / 资金10% / 情绪10% / 维科夫20%
  sentiment focus：情绪35% / 技术20% / 资金8% / 基本8% / 宏观8% / 维科夫21%
  ```

  Label these weights as heuristic and not yet calibrated against mature individual-stock attribution samples. Remove the outdated five-dimension percentages and claims that focus weights are 55%/50%/45%.

  Document the new operational guarantees:

  ```text
  - `pipeline_output.meta.expected_date` records the daily freshness basis.
  - `output_files` contains only artifacts validated in the current run.
  - `target_source=atr_projection` is observation-only.
  - `data_quality=error` disables composite scoring.
  ```

- [ ] **Step 5: Run focused and mandatory gates**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: all commands exit 0. A golden diff may show only the two new scoring metadata fields; inspect and justify that contract change instead of regenerating unrelated snapshots.

- [ ] **Step 6: Commit the versioned documentation contract**

  ```bash
  git add .claude/skills/stock-trend/scripts/analysis/scores.py .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py .claude/skills/stock-trend/SKILL.md
  git commit -m "docs(stock-trend): version composite score contract"
  ```

## Task 7: Final integration verification and stop/go review

**Files:**

- Verify only: all files listed in Tasks 1–6

- [ ] **Step 1: Run all targeted suites touched by the repair**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_capital_flow.py
  python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
  python3 .claude/skills/stock-trend/tests/test_stock_trend.py
  ```

  Expected: all commands exit 0.

- [ ] **Step 2: Run the mandatory golden gate**

  Run:

  ```bash
  python3 .claude/skills/stock-trend/tests/test_golden.py --diff
  ```

  Expected: exit 0. Do not regenerate snapshots. If the diff fails, classify each change as intended or unintended, repair unintended changes, and update snapshots only after explicit review of every intended numerical or output-contract change.

- [ ] **Step 3: Run static repository checks**

  Run:

  ```bash
  git diff --check
  git status --short
  ```

  Expected: no whitespace errors; only the planned files are modified or committed.

- [ ] **Step 4: Verify the failure matrix with deterministic fixtures**

  Confirm all six cases:

  ```text
  1. Fresh valid K-line + valid supplementary data: scores and report generate normally.
  2. Technical quality=error: scores.py exits non-zero; no action plan is produced.
  3. TTL-valid but date-stale daily K-line: cache is ignored; stale fresh-fetch result is not published.
  4. Supplementary subprocess failure with an old per-code file: old file is removed and output_files is null for that dimension.
  5. Missing indicator: it lowers coverage/confidence but does not enter the weighted denominator as a zero score.
  6. ATR-projected targets with attractive numeric R:R: report action remains 只观察 and states the provenance reason.
  ```

- [ ] **Step 5: Apply the milestone stop/go rule**

  - Milestone A may ship when Tasks 1–3 and the final gates pass, even if Milestone B is deferred.
  - Milestone B may ship only when complete-data golden fixtures remain stable and every incomplete-data difference is explained.
  - Weight recalibration remains blocked until a separate historical replay plan defines sample size, cost model, benchmark, IC/hit-rate criteria, and rollback thresholds.

- [ ] **Step 6: Confirm the final worktree**

  Run:

  ```bash
  git status --short
  git log -6 --oneline
  ```

  Expected: the planned commits are present and no unrelated files remain staged or modified.

## Acceptance criteria

- `technical.py` and `scores.py` agree on the location and type of `confidence` and `data_quality`.
- A technical error cannot be converted into a bullish composite score by non-technical dimensions.
- Daily K-line and capital fetches share one explicit expected trading date; TTL alone is insufficient.
- `pipeline_output.output_files` never points to a stale artifact left by an earlier failed run.
- Every attempted dimension exposes `available`, `quality`, and a stable failure/staleness reason.
- Unavailable technical indicators do not contribute weight or a synthetic neutral score; coverage constrains confidence.
- ATR-projected targets remain visible for scenarios but never produce an actionable individual-stock label.
- The documented scoring weights exactly match code and are labelled heuristic until calibrated.
- All targeted tests, `test_stock_trend.py`, and `test_golden.py --diff` pass without automatic golden regeneration.

## Plan self-review

- **Spec coverage:** Tasks 1–3 address schema mismatch, freshness, and stale artifact publication. Task 4 addresses missing-indicator dilution. Task 5 addresses actionability and target provenance. Task 6 resolves weight/document drift without guessing new weights. Task 7 defines release evidence.
- **Scope control:** Factor-cluster redesign, new dependencies, cost assumptions, report redesign, and empirical weight optimization are explicitly deferred.
- **Type consistency:** `summary.data_quality` is the sole technical-quality location; confidence is `low|medium|high` in `technical.json` and is recomputed as `低|中|高` in `scores.json`; `target_source` is `resistance|atr_projection|unavailable` throughout.
- **Behavior preservation:** Milestone A does not intentionally change valid fresh scores. Milestone B changes only incomplete-indicator aggregation and actionability under non-structural targets.
- **Verification:** Every Python task repeats the repository-required `test_stock_trend.py` and `test_golden.py --diff` gates before commit.
