# Candidate Market-Performance Fix Plan

> **Status:** Proposed — no implementation has been made under this plan.
>
> **Scope:** Address the issues identified from the 2026-08-10 daily-candidates
> report and the same-day market review. The priority is data correctness first,
> then actionable yet conservative candidate presentation.

## Background

The 2026-08-10 market was a broad advance but with a material capital-flow
divergence: 4,062 stocks rose, 99 stocks hit limit-up and turnover was
252.31 billion yuan, while aggregate main-force flow was -41.02 billion yuan.
The market-regime score of 63.4 (neutral) and the resulting light-position,
no-chasing stance were appropriate.

The candidate report nevertheless exposed four operational gaps:

1. 16 of 28 candidates had stale K-line data or failed secondary dimensions,
   which set their quality-adjusted score to zero.
2. Data-quality-valid candidates were all demoted as single-day sector pulses;
   the sector snapshot history was too sparse to distinguish insufficient
   history from genuinely one-day momentum.
3. The neutral-market policy allowed two waiting-for-trigger names, but the
   report did not provide a non-recommendation next-day confirmation list.
4. Missing sector-flow data received a neutral default, which can be unsafe
   when broad market capital flow is strongly negative.

Existing sector-ranking and constituent-cache fallback work remains in effect;
this plan does not replace it.

## P0 — Fresh closing-data retrieval and retry path

**Goal:** A post-close candidate scan must not silently reuse K-line data that
does not cover its `as_of_date`.

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`
- Test: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

### Work

1. Change `_fetch_kline` to validate the cached last trading date before
   accepting a cache hit. If it precedes `as_of_date`, force a refresh.
2. Implement a bounded `primary -> fallback -> retry` route for K lines and
   retain source, returned date and error details in metadata.
3. Apply equivalent provenance to capital-flow and fundamental fetches so an
   API error and data not-yet-published condition are distinguishable.
4. Report per-dimension retrieval success/failure counts after the scan.

### Acceptance criteria

- A T-1 cache plus a successful fallback produces a T-date K line.
- All-source failure keeps the candidate observation-only with a readable
  source/error reason.
- The stale-candidate rate improves because current data is fetched, not by
  relaxing the quality threshold.

## P1 — Reliable daily sector persistence snapshots

**Goal:** Separate a true single-day pulse from unavailable sector history.

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`
- Modify: `.claude/skills/stock-trend/scripts/analysis/market_theme.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

### Work

1. Persist one complete, validated sector snapshot per completed trading day;
   same-day reruns replace the snapshot rather than creating duplicate samples.
2. Add snapshot date, source, completeness and collection time metadata.
3. Compute persistence only over continuous trading sessions. Weekends,
   holidays and missing trading days must not be counted as observations.
4. Render `history_insufficient` separately from `single_day_pulse`.

### Acceptance criteria

- Three consecutive qualifying trading-day snapshots can classify a sector as
  mainline or emerging.
- One available snapshot is reported as insufficient history, not as evidence
  that the sector failed persistence.
- Missing dates cannot create a false three-day persistence result.

## P1 — Next-day confirmation watchlist in neutral markets

**Goal:** Retain no-chasing risk controls while making a fully observation-only
report useful on the next trading day.

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

### Work

1. Add a `next_day_confirmation` bucket of at most two names. It is not a
   recommendation and does not consume recommendation or portfolio limits.
2. Populate it only from data-eligible, high raw-score, valid-Wyckoff
   candidates from strong same-day sectors.
3. Attach explicit promotion rules: sector relative strength versus HS300,
   price-level validity, volume confirmation and positive capital-flow or
   corroborating resonance evidence.
4. Automatically promote only after the next-day data qualifies; otherwise
   retain observation status or remove the name.

### Acceptance criteria

- A neutral market can show a conditional next-day watchlist without labeling
  it buyable.
- A stale-data name or a weakening sector cannot appear in that watchlist.

## P2 — Broad-advance/capital-divergence gate

**Goal:** Avoid treating unknown sector capital flow as neutral when aggregate
market flow is materially negative.

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

### Work

1. Represent missing sector-capital data as `unknown`, not a default score of
   50.
2. When the market capital component is below an agreed threshold (initial
   proposal: `<35`), require a promotable candidate to have verified sector
   capital persistence or alternative confirmation from limit-up/LHB resonance.
3. Add a `breadth_capital_divergence` market label that tightens promotion and
   position limits without altering the raw stock-selection score.

### Acceptance criteria

- Strong negative broad-market flow plus unknown sector flow cannot produce an
  actionable or waiting-for-trigger candidate.
- Verified sector inflow/resonance can support a limited next-day confirmation
  candidate, subject to all other gates.

## P2 — Report audit trail and exposure controls

**Goal:** Make the sector-to-candidate funnel and correlated-theme risk visible.

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

### Work

1. Add a sector funnel: hot sectors -> constituents scanned -> Wyckoff passes
   -> data-eligible names -> each recommendation bucket.
2. For each leading market sector, show whether it had no candidate because of
   missing constituents, failed Wyckoff criteria, data quality or exposure cap.
3. Cluster highly correlated sectors and cap the number of retained candidates
   per theme.
4. Add sector change, relative strength, persistence state and capital/resonance
   proof to candidate rows.

### Acceptance criteria

- Readers can trace every candidate and every omission to a specific gate.
- The report explains why a strong market sector, such as tungsten on the
  reviewed day, did not yield a candidate.
- Correlated energy-metal, film/media or similar exposures are visibly capped.

## Delivery order and validation

1. Create fixed 2026-08-10 fixtures and failing regression tests.
2. Implement and verify P0 before changing scoring or classification.
3. Implement P1 and collect at least three live trading-day snapshots before
   evaluating persistence behaviour in production.
4. Implement P2 last, after reliable provenance and persistence data exist.
5. For every Python change, run the required quality gates:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Also run the targeted daily-candidate and stock-scanner test modules. Do not
regenerate golden snapshots unless every intended output change is reviewed.

---

This plan is for system reliability and learning/reference use only; it is not
investment advice.
