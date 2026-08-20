# Recommendation Snapshot and Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Persist one immutable official recommendation snapshot per recommendation date and automatically attribute actionable recommendations over 5/10/20/60 market sessions without future-data leakage.

**Architecture:** Add a write-once snapshot store under .cache/stock-trend/recommendation_history/. Add a separate attribution engine and atomic mutable sidecar under .cache/stock-trend/recommendation_attribution/; it reads but never changes official snapshots. Separate pure evaluation from live fetch orchestration so dates, fills, returns, MFE/MAE, exits, and benchmark alpha are deterministic and testable.

**Tech Stack:** Python 3.10 standard library, unittest, existing qfq stock K-line path, market_regime.fetch_index_kline() for HS300 sessions, sector_kline.fetch_single_kline() for BK benchmarks.

---

## Dependency and fixed decisions

- Execute after 2026-08-20-daily-recommendation-trade-plan-gate.md. Snapshots consume candidate-trade-plan/v1 but loaders tolerate older non-actionable candidates.
- Official headline uses only recommendations from non-provisional official snapshots.
- Snapshot all buckets so later calibration can compare promoted and rejected candidates without reconstructing history.
- Recommendation date T is the final data basis date. Entry is evaluated on the next market session T+1.
- Window N ends on the Nth market session after T; T+1 is session 1. Never use natural days or sparse stock bars as the calendar.
- Executable requires a T+1 qfq bar intersecting the plan entry zone. T+1 suspension, open below stop, zone not reached, or one-price limit-up is unexecutable; do not delay entry.
- Fill is T+1 open when inside the zone, otherwise the crossed zone boundary.
- If one daily bar touches stop and target, apply the conservative stop-first rule.
- Output both mark-to-market return and plan-path return. Terminal windows freeze; later runs only mature pending windows.
- Costs are explicit inputs. Defaults are zero/gross so mutable fee/tax law is not silently hard-coded.
- v1 evaluates persisted official snapshots only; it does not reconstruct historical recommendations.

## File map

- Create: .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py — canonical payload, validation, hashing, atomic write-once store, loader.
- Create: .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py — evaluation, fetch adapters, sidecar merge, summaries, CLI.
- Create: .claude/skills/stock-trend/tests/test_recommendation_snapshot.py — immutability/idempotency/conflict/guards.
- Create: .claude/skills/stock-trend/tests/test_recommendation_attribution.py — windows/execution/path/benchmarks/cutoff.
- Create: .claude/skills/stock-trend/tests/test_recommendation_lifecycle.py — candidate → snapshot → staged maturity.
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:1445-1607 and .claude/skills/stock-trend/tests/test_daily_candidates.py:1791-1850 — save once and expose tracking.
- Modify: .claude/skills/stock-trend/tests/test_stock_trend.py:1420-1440 — register runners.
- Modify: .claude/skills/stock-trend/SKILL.md:251-291 and docs/daily-recommendation-optimization.md:300-335,365-382 — lifecycle contract.

### Task 1: Lock the immutable snapshot contract

**Files:**
- Create: .claude/skills/stock-trend/tests/test_recommendation_snapshot.py

- [ ] **Step 1: Add a canonical source fixture**

~~~python
def snapshot_input():
    return {
        "recommendation_date": "2026-08-20",
        "generated_at": "2026-08-20T15:20:00+08:00",
        "snapshot_type": "formal",
        "model_version": "daily-candidates/v1",
        "policy": {"mode": "actionable", "provisional": False},
        "market_regime": {"score": 85, "data_date": "2026-08-20"},
        "sectors": [{"code": "BK1", "ranking_data_date": "2026-08-20"}],
        "candidates": [candidate_with_plan("600000", "2026-08-20")],
        "buckets": {
            "actionable": [candidate_with_plan("600000", "2026-08-20")],
            "waiting_trigger": [], "next_day_confirmation": [],
            "observation": [],
        },
        "scan_status": "complete",
    }
~~~

- [ ] **Step 2: Test canonicalization, deep copy, and future guards**

~~~python
def test_build_snapshot_is_canonical_and_detached():
    source = snapshot_input()
    snapshot = build_snapshot(source)
    source["candidates"][0]["quality_adjusted_score"] = 0
    assert snapshot["schema_version"] == "recommendation-snapshot/v1"
    assert snapshot["content"]["candidates"][0]["quality_adjusted_score"] == 80
    assert snapshot["content_sha256"] == content_sha256(snapshot["content"])


def test_snapshot_rejects_future_dated_evidence():
    source = snapshot_input()
    source["candidates"][0]["data_quality"]["dimensions"]["kline"]["data_date"] = "2026-08-21"
    with self.assertRaises(SnapshotValidationError):
        build_snapshot(source)
~~~

Check dates from regime, sector ranking/membership, every quality dimension, and trade-plan basis. Formal actionable rows must have trade_plan_status=complete.

- [ ] **Step 3: Test write-once idempotency and conflict**

~~~python
def test_same_content_is_noop_and_different_content_conflicts(self):
    first = build_snapshot(snapshot_input())
    result1 = save_official_snapshot(first, root=self.root)
    before, mtime = result1.path.read_bytes(), result1.path.stat().st_mtime_ns
    result2 = save_official_snapshot(first, root=self.root)
    self.assertEqual(result2.status, "unchanged")
    self.assertEqual(result1.path.read_bytes(), before)
    self.assertEqual(result1.path.stat().st_mtime_ns, mtime)

    changed = copy.deepcopy(first)
    changed["content"]["policy"]["mode"] = "observation"
    changed["content_sha256"] = content_sha256(changed["content"])
    with self.assertRaises(SnapshotConflict):
        save_official_snapshot(changed, root=self.root)
    self.assertEqual(result1.path.read_bytes(), before)
~~~

- [ ] **Step 4: Test provisional skip and failed atomic write**

~~~python
def test_provisional_is_not_official(self):
    source = snapshot_input()
    source["snapshot_type"] = "provisional"
    source["policy"]["provisional"] = True
    result = save_snapshot_if_official(source, root=self.root)
    self.assertEqual(result.status, "skipped_provisional")
    self.assertEqual(list(self.root.rglob("*.json")), [])


def test_link_failure_leaves_no_partial_file(self):
    snapshot = build_snapshot(snapshot_input())
    with patch("core.recommendation_snapshot.os.link", side_effect=OSError("disk")):
        with self.assertRaises(OSError):
            save_official_snapshot(snapshot, root=self.root)
    self.assertEqual(list(self.root.rglob("*.json")), [])
~~~

- [ ] **Step 5: Add runner and verify RED**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
~~~

Expected: FAIL with ModuleNotFoundError: core.recommendation_snapshot.

- [ ] **Step 6: Commit tests**

~~~bash
git add .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
git commit -m "test: define immutable recommendation snapshots"
~~~

### Task 2: Implement the atomic write-once store

**Files:**
- Create: .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py
- Test: .claude/skills/stock-trend/tests/test_recommendation_snapshot.py

- [ ] **Step 1: Define canonical JSON and exceptions**

~~~python
SCHEMA_VERSION = "recommendation-snapshot/v1"
DEFAULT_ROOT = CACHE_DIR / "recommendation_history"


class SnapshotValidationError(ValueError):
    pass


class SnapshotConflict(RuntimeError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_sha256(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()
~~~

- [ ] **Step 2: Validate all evidence dates and formal eligibility**

`save_snapshot_if_official()` must return `skipped_provisional` before calling
`build_snapshot()`. For formal sources, reject evidence after
recommendation_date, formal+provisional combinations, malformed policy/buckets,
and actionable rows without complete trade plans. Do not reject old observation
candidates lacking trade_plan.

- [ ] **Step 3: Build a detached stable envelope**

~~~python
def build_snapshot(source):
    copied = copy.deepcopy(source)
    _validate_source(copied)
    content = {
        "recommendation_date": copied["recommendation_date"],
        "snapshot_type": copied["snapshot_type"],
        "model_version": copied["model_version"],
        "policy": copied["policy"],
        "market_regime": copied["market_regime"],
        "sectors": copied["sectors"],
        "candidates": copied["candidates"],
        "buckets": copied["buckets"],
        "scan_status": copied["scan_status"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": copied["generated_at"],
        "content_sha256": content_sha256(content),
        "content": content,
    }
~~~

- [ ] **Step 4: Atomically create YYYY-MM-DD.json without overwrite**

Write a same-directory temp file, flush, `os.fsync`, then publish it with
`os.link(temp_path, target_path)`. The hard-link operation is atomic and cannot
overwrite an existing target. On `FileExistsError`, load and compare the
existing digest: same returns `unchanged`; different raises `SnapshotConflict`.
Always unlink the temp path in `finally` and fsync the parent directory after a
successful publish.

- [ ] **Step 5: Add strict loaders**

load_official_snapshot() rejects malformed JSON, unknown schema, filename/content date mismatch, and digest mismatch. iter_official_snapshots(root, through_date) returns sorted valid snapshots plus rejected-file diagnostics.

- [ ] **Step 6: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
git add .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
git commit -m "feat: persist immutable recommendation snapshots"
~~~

### Task 3: Save exactly once from /candidates

**Files:**
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:1445-1607
- Test: .claude/skills/stock-trend/tests/test_daily_candidates.py:1791-1850

- [ ] **Step 1: Add integration tests**

~~~python
def test_main_saves_one_formal_snapshot_before_serialization():
    with fixed_main_dependencies(),          patch.object(dc, "save_snapshot_if_official") as save:
        output = run_json_main()
    self.assertEqual(save.call_count, 1)
    payload = save.call_args.args[0]
    self.assertEqual(payload["recommendation_date"], "2026-08-20")
    self.assertEqual(payload["snapshot_type"], "formal")
    self.assertEqual(payload["buckets"]["actionable"][0]["trade_plan_status"], "complete")


def test_snapshot_failure_does_not_suppress_output():
    with patch.object(dc, "save_snapshot_if_official", side_effect=OSError("disk")):
        output = run_json_main()
    self.assertIn("candidates", output)
    self.assertEqual(output["meta"]["tracking"]["status"], "save_failed")
~~~

Also assert provisional runs report skipped_provisional and do not create an official file.

- [ ] **Step 2: Save after classification and before format builders**

Immediately after `buckets = classify_candidates(candidates, policy)`, call
`_save_recommendation_snapshot(candidates, sector_codes, policy, buckets,
expected_date, performance)` exactly once. Do not save inside
JSON/Markdown/HTML builders.

- [ ] **Step 3: Add tracking metadata to all formats**

JSON meta.tracking contains status/path/content_sha256/reason. Markdown/HTML show one compact audit line. save_failed is evidence-tracking degradation, not recommendation failure.

- [ ] **Step 4: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: snapshot formal daily recommendations"
~~~

### Task 4: Define exact attribution behavior

**Files:**
- Create: .claude/skills/stock-trend/tests/test_recommendation_attribution.py

- [ ] **Step 1: Add fixed market, stock, HS300, and sector fixtures**

Create 65 market sessions after 2026-08-20. Give the stock deterministic qfq OHLCV, one suspension, a one-price limit-up case, and exact 5/10/20/60 values. Give benchmarks different slopes so alpha is exact.

- [ ] **Step 2: Test exact maturity and future cutoff**

~~~python
def test_windows_mature_only_on_exact_session():
    at_4 = evaluate_recommendation(fixture(), evaluation_as_of=session(4), **series())
    at_5 = evaluate_recommendation(fixture(), evaluation_as_of=session(5), **series())
    assert at_4["windows"]["5"]["status"] == "pending"
    assert at_5["windows"]["5"]["status"] == "complete"
    assert at_5["windows"]["5"]["mark_to_market_return"] == 0.05


def test_rows_after_cutoff_are_ignored():
    baseline = evaluate_at_session(10)
    poisoned = copy.deepcopy(series())
    poisoned["stock_rows"].append(future_spike_row())
    assert evaluate_at_session(10, **poisoned) == baseline
~~~

- [ ] **Step 3: Test execution**

Cover: open inside zone, boundary-cross fill, open below stop, T+1 suspension, one-price limit-up (open==high==low==close and pct_chg>=9.5), normal limit-up with a tradable range, and zone not reached.

- [ ] **Step 4: Test path and benchmarks**

Assert MFE, MAE, first stop/target dates, stop-first on ambiguous bars, gross/net returns with explicit costs, HS300 alpha, sector alpha, and independent benchmark degradation where alpha is null but absolute return remains complete.

- [ ] **Step 5: Test qfq and post-entry suspension**

Reject missing/wrong adjustment metadata. During suspension, carry prior close for mark-to-market, set carried_suspension=true, and do not trigger stop/target.

- [ ] **Step 6: Add runner and verify RED**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_attribution.py
~~~

Expected: FAIL with ModuleNotFoundError: analysis.recommendation_attribution.

- [ ] **Step 7: Commit tests**

~~~bash
git add .claude/skills/stock-trend/tests/test_recommendation_attribution.py
git commit -m "test: define recommendation attribution rules"
~~~

### Task 5: Implement the pure attribution engine

**Files:**
- Create: .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py
- Test: .claude/skills/stock-trend/tests/test_recommendation_attribution.py

- [ ] **Step 1: Define stable states and cost model**

~~~python
WINDOWS = (5, 10, 20, 60)
EVALUATOR_VERSION = "recommendation-attribution/v1"


@dataclass(frozen=True)
class CostModel:
    buy_commission_bps: float = 0.0
    sell_commission_bps: float = 0.0
    buy_slippage_bps: float = 0.0
    sell_slippage_bps: float = 0.0
    sell_tax_bps: float = 0.0

    def __post_init__(self):
        values = dataclasses.astuple(self)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("cost bps must be finite and non-negative")

    @property
    def mode(self):
        return "gross" if not any(dataclasses.astuple(self)) else "explicit_cost"
~~~

- [ ] **Step 2: Filter every series by evaluation_as_of before calculation**

Build market sessions exclusively from HS300 rows after T. If calendar rows are unavailable, return data_error/market_calendar_missing rather than counting stock bars.

- [ ] **Step 3: Resolve T+1 execution**

~~~python
def resolve_entry(plan, recommendation_date, market_sessions, stock_rows):
    entry_date = first_session_after(market_sessions, recommendation_date)
    row = row_by_date(stock_rows).get(entry_date)
    if row is None or float(row.get("vol") or 0) <= 0:
        return {"status": "unexecutable", "reason": "t1_suspended"}
    if _one_price_limit_up(row):
        return {"status": "unexecutable", "reason": "t1_one_price_limit_up"}
    low, high = plan["entry"]["low"], plan["entry"]["high"]
    if float(row["open"]) < float(plan["stop_loss"]["price"]):
        return {"status": "unexecutable", "reason": "t1_open_below_stop"}
    if float(row["low"]) > high or float(row["high"]) < low:
        return {"status": "unexecutable", "reason": "t1_entry_zone_not_reached"}
    fill = min(high, max(low, float(row["open"])))
    return {"status": "executable", "date": entry_date, "price": fill}
~~~

- [ ] **Step 4: Evaluate each window**

For entry through N: use qfq bars; carry prior close only for suspension valuation; calculate MFE/MAE; scan stop before targets; freeze plan-path after first exit; calculate Nth-session mark-to-market; calculate HS300/sector returns on the same dates; apply explicit costs; record all assumptions.

- [ ] **Step 5: Return a stable envelope**

~~~python
return {
    "evaluator_version": EVALUATOR_VERSION,
    "recommendation_date": recommendation_date,
    "code": recommendation["code"],
    "evaluation_as_of": evaluation_as_of,
    "execution": execution,
    "cost_model": dataclasses.asdict(cost_model),
    "windows": windows,
}
~~~

- [ ] **Step 6: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_attribution.py
git add .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py .claude/skills/stock-trend/tests/test_recommendation_attribution.py
git commit -m "feat: attribute recommendation outcomes"
~~~

### Task 6: Add fetch adapters, sidecar maturity, and CLI

**Files:**
- Modify: .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py
- Test: .claude/skills/stock-trend/tests/test_recommendation_attribution.py

- [ ] **Step 1: Separate fetch from pure evaluation**

Use existing stock qfq fetch path with metadata validation; market_regime.fetch_index_kline("000300.SH", lmt=180); sector_kline.fetch_single_kline(sector_code, min_records=180). One item failure must not block others.

- [ ] **Step 2: Implement atomic sidecars**

Path: .cache/stock-trend/recommendation_attribution/YYYY-MM-DD.json.
`merge_attribution(existing, incoming)` may mature `pending` windows and retry
`data_error` windows. Existing `complete` and `unexecutable` windows remain
unchanged. Use same-directory temp, fsync, and os.replace because sidecars are
intentionally mutable.

- [ ] **Step 3: Test idempotency and staged maturity**

~~~python
def test_repeat_is_identical_and_later_run_only_matures_pending():
    first = run_tracker(as_of=session(5))
    first_bytes = sidecar.read_bytes()
    run_tracker(as_of=session(5))
    assert sidecar.read_bytes() == first_bytes
    run_tracker(as_of=session(20))
    later = json.loads(sidecar.read_text())
    assert later["items"][0]["windows"]["5"] == first["items"][0]["windows"]["5"]
    assert later["items"][0]["windows"]["20"]["status"] == "complete"
~~~

- [ ] **Step 4: Add CLI**

~~~text
recommendation_attribution.py
  --through YYYY-MM-DD
  --history 120
  --windows 5,10,20,60
  --buy-commission-bps 0
  --sell-commission-bps 0
  --buy-slippage-bps 0
  --sell-slippage-bps 0
  --sell-tax-bps 0
  --json
~~~

Summary discloses snapshot/recommendation/complete/pending/unexecutable/error counts and groups completed executable results by Top1/3/5, regime, sector, and Wyckoff sub-phase. Emit evidence_insufficient until at least 20 official dates and 100 mature observations exist.

- [ ] **Step 5: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_attribution.py
git add .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py .claude/skills/stock-trend/tests/test_recommendation_attribution.py
git commit -m "feat: mature recommendation attribution sidecars"
~~~

### Task 7: Add end-to-end lifecycle proof and docs

**Files:**
- Create: .claude/skills/stock-trend/tests/test_recommendation_lifecycle.py
- Modify: .claude/skills/stock-trend/tests/test_stock_trend.py:1420-1440
- Modify: .claude/skills/stock-trend/SKILL.md:251-291
- Modify: docs/daily-recommendation-optimization.md:300-335,365-382

- [ ] **Step 1: Test formal run → immutable snapshot → day 4/5/20/60 maturity**

Use temporary roots and fixed rows. Assert formal save once, repeat unchanged, provisional no save, day 4 pending, day 5 only 5 complete, day 20 matures 10/20, day 60 all complete, official bytes unchanged.

- [ ] **Step 2: Register snapshot, attribution, and lifecycle runners in the main daily-recommendation gate**

- [ ] **Step 3: Document paths, write-once conflict behavior, provisional exclusion, T+1/window definitions, qfq/suspension/limit-up/stop-first rules, benchmark degradation, costs, and evidence minimums**

- [ ] **Step 4: Run targeted and mandatory gates**

~~~bash
python3 .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
python3 .claude/skills/stock-trend/tests/test_recommendation_attribution.py
python3 .claude/skills/stock-trend/tests/test_recommendation_lifecycle.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
python3 -m py_compile .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py .claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py
git diff --check
~~~

Expected: all exit 0. Do not regenerate golden snapshots unless every delta is intentional and reviewed.

- [ ] **Step 5: Commit lifecycle and docs**

~~~bash
git add .claude/skills/stock-trend/tests/test_recommendation_lifecycle.py .claude/skills/stock-trend/tests/test_stock_trend.py .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define recommendation evidence lifecycle"
~~~

## Completion criteria

- One official snapshot per recommendation date; same content is no-op and different content conflicts without overwrite.
- Provisional runs never produce official evidence.
- Snapshot failure degrades tracking status without suppressing output.
- Evaluation uses market sessions, qfq, explicit T+1 execution, and evaluation_as_of cutoff.
- 5/10/20/60 windows include return, alpha, MFE/MAE, stop/target, execution, and data status.
- Sidecars mature pending windows without changing completed results or official bytes.
- Summary excludes pending/unexecutable/error from return denominators while disclosing counts.
- New tests are in the main gate; targeted, main, golden, compile, and diff checks pass.
