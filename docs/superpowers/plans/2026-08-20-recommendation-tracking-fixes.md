# Recommendation Tracking Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daily recommendation plans and historical attribution use production-shaped dates and adjustment metadata, preserve auditable sidecar state, and expose consistent execution returns.

**Architecture:** Normalize trade dates at the attribution boundary and keep provider metadata alongside rows. Historical evaluation receives the snapshot recommendation date and cutoff explicitly, while sidecar merges are identity-aware and never overwrite unreadable evidence. Trade-plan construction will consume the existing technical target fields, calculate a real validity date from the available session calendar when supplied, and validate the full contract.

**Tech Stack:** Python 3, `unittest`/plain assertion tests, existing stock-trend adapters, JSON sidecars, pandas-based technical calculations.

---

### Task 1: Lock production-shaped attribution contracts with failing tests

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_recommendation_attribution.py`
- Modify: `.claude/skills/stock-trend/tests/test_recommendation_snapshot.py`
- Modify: `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py`

- [x] **Step 1: Add tests for `YYYYMMDD` stock/calendar/benchmark rows.**

```python
def test_production_trade_dates_mature_and_match_benchmarks():
    plan = {"entry": {"low": 10, "high": 12}, "stop_loss": {"price": 8}, "targets": {"primary": 15}}
    days = ["20260820", "20260821", "20260822", "20260825", "20260826", "20260827"]
    rows = [{"trade_date": d, "open": 11, "high": 12, "low": 10, "close": 11, "vol": 1} for d in days]
    result = evaluate_recommendation(
        {"recommendation_date": "2026-08-20", "code": "X", "trade_plan": plan},
        "2026-08-27", days, rows,
        hs300_rows=[{"trade_date": d, "close": 100 + i} for i, d in enumerate(days)],
        windows=(5,),
    )
    assert result["windows"]["5"]["status"] == "complete"
    assert result["windows"]["5"]["hs300_return"] is not None
```

- [x] **Step 2: Add tests requiring qfq metadata and preserving evaluation cutoffs.**

```python
def test_loader_contract_rejects_missing_or_wrong_adjustment():
    with pytest.raises(ValueError, match="qfq"):
        validate_series_metadata({"adj": "hfq"})
    with pytest.raises(ValueError, match="qfq"):
        validate_series_metadata({})
```

- [x] **Step 3: Add tests for sidecar identity, corrupted-file preservation, and retry metadata.**

```python
def test_corrupt_sidecar_is_not_replaced(tmp_path):
    path = tmp_path / "2026-08-20.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        read_sidecar(path)
    assert path.read_text(encoding="utf-8") == "{bad"
```

- [x] **Step 4: Add tests for technical targets, non-empty counterargument, and future validity.**

```python
from unittest.mock import patch

def test_builder_uses_technical_targets_and_validity_window():
    risk = {
        "stop_loss": 95,
        "target_conservative": 110,
        "target_moderate": 115,
        "target_aggressive": 120,
        "risk_reward_ratio": 3.0,
    }
    with patch("core.candidate_trade_plan.calc_risk_reward", return_value=risk), \
         patch("core.candidate_trade_plan.calc_entry_signals", return_value={"verdict": "ready", "signals": []}):
        plan = build_candidate_trade_plan(
            "600000", make_kline(), {}, policy(), "2026-08-20",
            "若量价确认失败则逻辑失效",
            market_sessions=["2026-08-21", "2026-08-24", "2026-08-25"],
        )
    assert plan["targets"] == {"conservative": 110, "primary": 115, "aggressive": 120}
    assert plan["validity"]["valid_until"] == "2026-08-25"
```

- [x] **Step 5: Run only the changed tests and confirm each new test fails for the reported reason.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 .claude/skills/stock-trend/tests/test_recommendation_attribution.py
PYTHONDONTWRITEBYTECODE=1 python3 .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
PYTHONDONTWRITEBYTECODE=1 python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
```

Expected: failures identify date normalization, qfq validation, corrupted sidecar handling, technical target usage, or validity/counterargument behavior—not test collection errors.

### Task 2: Repair attribution normalization, history loading, returns, and sidecars

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py`
- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py`

- [x] **Step 1: Implement one strict date normalizer and use it for all row/calendar comparisons.**

```python
def normalize_trade_date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
    return date.fromisoformat(text[:10]).isoformat()
```

Use it in `_dt`, `_row_date`, `resolve_entry`, `evaluate_recommendation`, and benchmark maps; malformed dates must produce `data_error` rather than silently compare strings.

- [x] **Step 2: Preserve and validate series metadata.**

```python
def validate_series_metadata(series):
    meta = (series or {}).get("meta") or {}
    if meta.get("adj") != "qfq":
        raise ValueError("qfq adjustment metadata required")
```

The default loader returns metadata with rows, passes the snapshot recommendation date into `_fetch_kline`, and requests only data through the evaluation cutoff. Missing historical rows remain retryable `data_error`; only explicit suspension evidence becomes `unexecutable`.

- [x] **Step 3: Make gross/net use the same plan path and expose MTM separately.**

```python
gross = path_return if path_return is not None else mtm
net = gross - cost_bps / 10000
item["gross_return"] = gross
item["mark_to_market_return"] = mtm
item["plan_path_return"] = path_return
```

- [x] **Step 4: Make sidecar merges identity-aware and corruption-safe.**

Persist snapshot digest, evaluator version, evaluation cutoff, and cost model. Read errors other than `FileNotFoundError` must raise without replacing the original file. Merge only compatible identities and update run-level metadata when a newer evaluation advances the cutoff.

- [x] **Step 5: Make `--history` filter snapshot dates and pass the snapshot date through the loader.**

Use the most recent `N` official snapshots after `iter_official_snapshots`, and ensure `evaluation_as_of` and `through_date` are normalized before loading.

- [x] **Step 6: Re-run the new attribution and snapshot tests and confirm green.**

### Task 3: Repair trade-plan construction and validation

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/core/candidate_trade_plan.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- [x] **Step 1: Guard empty K-line frames before indexing the last close.**

```python
df = _normalized_frame(kline)
if len(df) < 2:
    return {"schema_version": SCHEMA_VERSION, "code": code, "basis_date": basis_date,
            "action": "avoid", "event_check": {"status": "not_implemented"}}
close = _finite_positive(df["close"].iloc[-1])
```

- [x] **Step 2: Consume `target_conservative`, `target_moderate`, and `target_aggressive`; only synthesize fallback targets when the adapter has no complete ordered set.**

Recompute R:R from the selected entry high, stop, and primary target; incomplete or below-threshold plans stay non-actionable.

- [x] **Step 3: Generate a stable non-empty counterargument and pass it from the scanner.**

```python
def _candidate_counterargument(item):
    warnings = item.get("warnings") or []
    if warnings:
        return "；".join(str(x) for x in warnings)
    return "若量价确认失败或收盘跌破结构支撑，则交易逻辑失效"
```

- [x] **Step 4: Validate schema version, horizon, validity sessions, event status allow-list, and `wait`/`watch` action semantics.**

Calculate `valid_until` from an optional market-session list; otherwise retain an explicit `validity_sessions` contract and do not claim the recommendation is valid only on the basis date.

- [x] **Step 5: Run candidate trade-plan and scanner integration tests and confirm green.**

### Task 4: Full verification and handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-08-20-recommendation-tracking-fixes.md`

- [x] **Step 1: Run the required stock-trend quality gates.**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 .claude/skills/stock-trend/tests/test_stock_trend.py
PYTHONDONTWRITEBYTECODE=1 python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

- [x] **Step 2: Run focused attribution, snapshot, candidate, and lifecycle tests again.**

- [x] **Step 3: Run `git diff --check` and inspect `git status --short`.**

- [x] **Step 4: Mark completed steps in this plan only after command output confirms them, then report changed files, tests, and any remaining limitations.**
