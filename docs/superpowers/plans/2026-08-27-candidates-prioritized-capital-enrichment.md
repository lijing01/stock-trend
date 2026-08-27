# Daily Candidates: 180-Second Budget and Prioritized Capital Enrichment

## Goal

Prevent a slow K-line stage from exhausting the daily candidate scanner's
shared deadline before individual capital-flow data is requested. Preserve the
existing data-quality gate: a candidate without current, valid capital-flow
data cannot be promoted to a formal recommendation.

This plan complements `2026-08-27-capital-flow-timeout-isolation.md`. That
plan removes optional enrichment work from the candidate-specific capital
fetch. This plan schedules the remaining primary capital-flow requests so they
receive a guaranteed execution window.

## Fixed operating budget

The daily candidate scan has a wall-clock budget of 180 seconds.

| Window | Deadline from start | Purpose |
|---|---:|---|
| K-line / Wyckoff phase | 110s | Board scan, membership expansion, K-line refresh, and Wyckoff filtering. |
| Capital enrichment phase | 170s | Prioritized primary capital-flow retrieval and final quality scoring. |
| Finalization reserve | 180s | Ranking, report serialization, audit, and recommendation snapshot. |

Implementation constants:

```python
SCAN_DEADLINE_SECONDS = 180
FINALIZATION_RESERVE_SECONDS = 10
KLINE_PHASE_SECONDS = 110
CAPITAL_PREFETCH_LIMIT = 36
CAPITAL_PREFETCH_BATCH_SIZE = 12
```

The live deadline remains `started_at + 170 seconds`. K-line scheduling must
instead use `min(live_deadline, started_at + KLINE_PHASE_SECONDS)`.

## Target flow

```text
hot sectors / memberships
          |
          v
K-line + Wyckoff, bounded to T+110s
          |
          v
provisional ranking without capital promotion
          |
          v
valid capital cache hits for all eligible candidates
          |
          v
live primary capital fetch for priority batches of 12 (up to 36 initially)
          |
          v
re-score + data-quality gate + final report
```

## Design decisions

1. Do not request live capital data for every Wyckoff-qualified stock.
   The 2026-08-27 scan had 135 qualified stocks; fetching them all cannot fit
   a bounded post-close report run.

2. Perform a preliminary, non-promotable ranking after the K-line/Wyckoff
   stage. It uses momentum, volume-price, sector strength, Wyckoff, and any
   valid fundamental fallback. Capital is neutral only for queue priority;
   it cannot satisfy the formal recommendation gate.

3. Check every qualified candidate for a valid same-day capital cache first.
   Cache hits cost no provider budget and remain eligible for final ranking.

4. For candidates lacking a valid cache, request live capital data only for
   the highest-priority queue entries. Start with 36 (`max(36, ceil(top *
   1.2))`) and process them in batches of 12. Use four in-flight capital
   requests; retain the existing source-health circuit breaker so repeated
   failures reduce effective concurrency.

5. After each completed batch, recompute data quality. Stop when at least
   `min_candidates` valid candidates exist and no higher-priority unprocessed
   candidate can displace the provisional cutoff. Otherwise start the next
   batch while before the 170-second live deadline.

6. Candidates omitted from the live enrichment queue are not `capital_error`.
   They must carry `not_selected_for_enrichment` or
   `not_started_deadline`, remain non-promotable, and be reported separately
   from genuine provider failures.

## Production changes

### 1. Phase-specific scheduling

**Modify:** `.claude/skills/stock-trend/scripts/core/source_health.py`

- Raise `SCAN_DEADLINE_SECONDS` to 180 and reserve 10 seconds for finalization.
- Expose a helper or immutable property for the 110-second K-line deadline.
- Set capital `MAX_IN_FLIGHT` to 4. Existing failure/degraded handling remains
  authoritative; do not add unbounded retries.

**Modify:** `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- Pass the K-line deadline only to the K-line `bounded_source_map` call.
- Pass the full 170-second live deadline to capital and fundamental work.
- Preserve the existing `--skip-extended` capital fetch command.

### 2. Preliminary ranking and prioritized enrichment queue

**Modify:** `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- Add a pure helper such as `rank_capital_enrichment_candidates(...)`.
- The helper must:
  - retain all candidates with a valid same-day capital cache;
  - sort cache-missing candidates by provisional score with a stable code
    tiebreaker;
  - return the initial priority queue and subsequent batches;
  - not alter final `composite_score` or `quality_adjusted_score`.
- Fetch fundamental live data only for the same priority scope. Existing
  membership-based fundamental fallback remains available for the rest.
- Recompute dimensions, trade plan, and `assess_candidate_data()` after each
  capital batch. Final ranking always uses the existing quality-adjusted score.

### 3. Correct source-evidence and cache-status reporting

**Modify:** `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- Do not make an invalid/missing cache appear as `cache_used=True` merely
  because it is wrapped in a diagnostic dictionary.
- Emit explicit source evidence with one of:
  `live_success`, `cache_valid`, `cache_miss`, `cache_stale`,
  `not_selected_for_enrichment`, `not_started_deadline`, or a classified
  provider error.

**Modify:** `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`

- Map genuine invalid capital payloads to `capital_error`.
- Map a skipped low-priority enrichment request to its explicit non-provider
  reason, while keeping `available=false` and `eligible=false`.

**Modify:** `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

- Render the explicit queue/deadline states in the data-quality reason text.
- Add report audit fields: `capital_priority_count`, `capital_live_started`,
  `capital_valid_count`, `capital_cache_valid_count`, and
  `capital_skipped_by_budget`.

## Tests

**Modify:** `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- K-line work consuming 104 seconds still allows capital requests to start
  before the 170-second deadline.
- A priority queue of 36 candidates is processed in deterministic batches of
  12, with valid cache hits excluded from provider work.
- Final ranking cannot promote an un-enriched candidate.
- Capital concurrency is capped at four and source-health degradation remains
  effective.

**Modify:** `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- Missing capital cache reports `cache_miss`, not “已回退缓存”.
- A deadline-skipped candidate reports `not_started_deadline`, not
  `capital_error`.
- A genuine capital fetch failure still reports `capital_error`.
- Audit fields reconcile: valid cache + live successes + skipped candidates
  equals the enrichment population.

**Modify:** `.claude/skills/stock-trend/tests/test_stock_trend.py`

- Add an end-to-end deterministic timing case covering the 180-second budget
  and zero false formal recommendations when capital data is unavailable.

## Verification

1. Run focused tests for source health, scanner, data quality, and report
   rendering.
2. Run the mandatory quality gates:

   ```bash
   python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

3. Run a post-close smoke scan:

   ```bash
   python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py --no-html
   python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
     --top 30 --min-candidates 20 --no-html
   ```

4. Confirm all of the following in the report audit:

   - total wall-clock time is at most approximately 180 seconds;
   - `capital_live_started > 0` whenever cache-missing priority candidates exist;
   - every formal recommendation has fresh capital data through the
     recommendation date;
   - a no-request deadline condition is distinct from a provider failure;
   - weak-market policy gating still overrides otherwise complete candidates.

## Non-goals

- Do not relax the market-regime gate, quality threshold, expected-date check,
  or recommendation risk controls.
- Do not treat an old cache as current capital data.
- Do not expand the entire sector universe merely to compensate for a missing
  candidate; the existing sector-expansion cap remains a separate policy.
