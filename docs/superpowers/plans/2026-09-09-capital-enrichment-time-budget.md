# Daily Candidates: Capital Enrichment Time-Budget Remediation Plan

## Goal

Keep the daily candidate scan within its fixed 180-second wall-clock budget
while ensuring that final report candidates receive a real opportunity for
current capital-flow enrichment. A candidate without valid current capital
evidence must remain non-promotable, and scheduler omissions must remain
distinct from provider failures.

This plan follows the existing prioritized-enrichment work in
`2026-08-27-candidates-prioritized-capital-enrichment.md` and the fixed-budget
provenance work in `2026-08-28-capital-fetch-provenance-fixed-budget.md`.

## Observed incident

The 2026-09-09 run recorded:

| Metric | Value |
|---|---:|
| Total scan time | 175.8s |
| K-line phase | 114.8s |
| Eligible phase-2 population | 163 |
| Initial capital live starts | 44 |
| Initial capital valid results | 20 |
| Capital failures classified as `stale_data` | 24 |
| Top-up candidates selected | 10 |
| Top-up capital live starts | 0 |
| Top-up candidates skipped by deadline | 10 |

The scanner creates a 170-second live deadline from a 180-second total budget,
reserving 10 seconds for finalization. The top-up guard requires at least one
25-second capital attempt budget. After K-line work and initial enrichment,
the remaining budget was below that threshold, so the scheduler deliberately
marked the ten selected candidates as `not_started_deadline`.

The status is therefore a valid safety outcome. The operational defect is that
the current schedule allows earlier work, including repeated per-window
enrichment, to consume the time needed by the final report frontier.

## Fixed-budget contract

Keep these values unchanged during the first implementation:

```python
SCAN_DEADLINE_SECONDS = 180
FINALIZATION_RESERVE_SECONDS = 10
KLINE_PHASE_SECONDS = 110
LIVE_ATTEMPT_TIMEOUT_SECONDS["capital"] = 25
MAX_IN_FLIGHT["capital"] = 4
MAX_IN_FLIGHT["fundamental"] = 2
```

The implementation must not extend the deadline while in-flight work is
running. It must cancel or classify late work, preserve source evidence, and
finish report serialization inside the finalization reserve.

## Target scheduling model

```text
sector ranking and membership
          |
          v
K-line / Wyckoff filtering with a phase deadline
          |
          v
global provisional ranking across all sector windows
          |
          v
one bounded initial capital queue
          |
          v
final report frontier and dynamic top-up capacity
          |
          v
quality gate, snapshot, and report finalization
```

The key change is that each scan window must not own an independent initial
capital queue. The scan needs one global queue and one global top-up decision.

## Implementation tasks

### 1. Add budget and scheduler observability

Modify:

- `.claude/skills/stock-trend/scripts/core/source_health.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

Record, per scan and per enrichment stage:

- monotonic start and end timestamps;
- absolute deadline and remaining time at stage entry;
- queue size, batch size, and maximum in-flight count;
- selected, started, succeeded, failed, cancelled, and skipped counts;
- source health state and circuit-breaker transitions;
- whether the stage ended by exhaustion, source unavailability, or deadline.

Keep the existing stable reason codes. Add diagnostic detail for the two
deadline cases:

- `deadline`: the absolute live deadline has passed;
- `budget_insufficient_for_attempt`: the deadline has not passed, but the
  remaining time is below the source's one-attempt budget.

For compatibility, keep `not_started_deadline` as the serialized candidate
status used by existing consumers, while exposing the more precise diagnostic
reason in a separate field.

### 2. Make capital enrichment global across sector windows

Modify:

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

Change the expansion flow so `scan_sectors()` collects phase-1 candidates and
K-line/Wyckoff results across its windows before applying the final capital
priority queue. Preserve the existing sector expansion stop conditions and
the 120-sector expansion cap.

The global queue must:

- deduplicate by stock code;
- use the same provisional score and stable code tie-breaker;
- exclude valid current capital caches;
- cap the initial live scope at the configured priority limit;
- submit each stock to the capital provider at most once per scan;
- retain the full research population for audit even when it is outside the
  live enrichment scope.

The existing `run_phase2()` API should remain compatible with focused callers
and tests. If the production path needs a new orchestration helper, keep the
old helper as a thin compatibility wrapper rather than changing its public
return shape.

### 3. Reserve and dynamically size the top-up stage

Modify `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`.

Before starting the initial live queue, reserve a top-up window. Start with a
configurable one-wave reserve and calculate actual capacity at the top-up
boundary:

```python
remaining = max(0.0, live_deadline - time.monotonic())
capital_capacity = MAX_IN_FLIGHT["capital"] * floor(
    remaining / LIVE_ATTEMPT_TIMEOUT_SECONDS["capital"]
)
```

Also calculate the fundamental capacity with its own concurrency and timeout
when a top-up candidate lacks a valid membership fallback. The top-up batch
must be capped by the smaller relevant capacity and by
`CAPITAL_TOPUP_LIMIT`.

If the remaining budget is below one complete attempt, do not submit the
batch. Mark candidates with `not_started_deadline` and attach the detailed
budget reason. If the capacity is positive, submit only the executable prefix
of the ranked top-up list; leave the remainder explicitly unselected for this
run.

The initial queue must stop before consuming the reserved top-up window. A
successful early-stop condition still applies, but it cannot spend the
reserved time unless no top-up candidate remains.

### 4. Reduce the two major sources of time loss

Modify after the scheduler change has measurable stage timings:

- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py`

For K-lines, identify why the bounded 110-second phase measured 114.8 seconds
and reduce subprocess/fallback tail time without allowing late work to mutate
source health.

For capital flow, reuse the already fetched current K-line when the
`kline_estimate` fallback is needed. Preserve `kline_estimate` as the source
label and retain `stale_data` when the estimated rows do not cover the expected
trading date. Do not convert an estimate into measured main-force flow.

Do not increase concurrency before measuring provider behavior; the capital
source already recorded 24 `stale_data` failures and a circuit-breaker event.

## Data and status contract

The following states remain distinct:

| State | Provider called | Promotable | Meaning |
|---|---:|---:|---|
| `live_success` | yes | potentially | Current usable capital data returned. |
| `stale_data` or classified provider error | yes | no | A live attempt failed or returned unusable data. |
| `cache_valid` | no | potentially | Current cache passed validation. |
| `not_started_deadline` | no | no | Scheduler did not start because the budget was exhausted or insufficient. |
| `not_selected_for_enrichment` | no | no | Candidate was outside the bounded live scope. |
| `source_unavailable` | no | no | Circuit breaker prevented a provider call. |

No scheduler state may be counted as a provider failure. No neutral capital
score may pass the data-quality gate.

## Tests

Add or update focused tests in:

- `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py`

Cover these cases:

1. A slow initial phase preserves the reserved top-up window.
2. Four available capital slots and 30 seconds of budget produce at most four
   top-up requests.
3. Ten selected top-up candidates with zero executable capacity produce zero
   provider calls and ten explicit deadline statuses.
4. Top-up capacity accounts for the fundamental concurrency when full
   fundamental enrichment is required.
5. Multiple sector windows produce one global, deduplicated capital queue.
6. The same stock never receives two capital requests in one run.
7. Provider failure, stale data, circuit-open, budget-insufficient, and
   absolute-deadline states remain distinguishable.
8. Missing current capital evidence never promotes a candidate.
9. Late futures cannot update source health after the deadline.

## Verification

Run the focused tests first, then the mandatory repository gates:

```bash
/Users/trace/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
/Users/trace/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/trace/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_daily_recommendation_performance.py
/Users/trace/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/trace/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Do not regenerate golden snapshots merely to hide a failure. If intended
diagnostic fields change the golden output, document the exact output change
before regenerating the snapshot.

Run one post-close smoke scan with live access after the tests. Compare it to
the 2026-09-09 baseline and verify:

- total time remains within approximately 180 seconds;
- the initial queue is global and bounded;
- top-up requests start whenever the calculated capacity is positive;
- `selected = started + skipped` is reconciled for every stage;
- all formal recommendations have current valid capital evidence;
- the report clearly distinguishes data degradation from budget omission.

## Non-goals

- Do not relax the market-regime gate or data-quality gate.
- Do not treat old capital data or K-line estimates as measured current flow.
- Do not remove the finalization reserve.
- Do not expand the complete sector universe to compensate for enrichment
  misses.
- Do not publish a new strategy policy as part of this performance change.
