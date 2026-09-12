# Daily Candidates: 330-Second Capital-Enrichment Remediation Plan

## Goal

Give the final candidate frontier enough time to receive current capital and
fundamental enrichment while keeping the data-quality gate intact. The scan
has a 330-second hard wall-clock budget, including report finalization.
Missing or stale evidence remains non-promotable; a longer budget must never
be used to relabel stale data as current.

## Incident basis

The 2026-09-12 09:23:01 report completed in about 156 seconds. Its audit
record showed 12 top-up candidates selected, 6 started and valid, and 6
skipped because the remaining time was insufficient for a complete request
window. The top-up limit was 12; capital concurrency was 4, fundamental
concurrency was 2, and each provider attempt had a 25-second timeout. Thus,
within a 75-second remainder, fundamental capacity was only
`floor(75 / 25) * 2 = 6` candidates.

The same run recorded 21 capital results as `stale_data`. The larger budget
addresses requests that were not started; it does not correct stale provider
results. That data-path issue remains a separate diagnostic item.

## Runtime budget contract

| Boundary | Value | Purpose |
|---|---:|---|
| Total scan deadline | 330s | Hard end-to-end cap |
| Finalization reserve | 10s | Scoring, persistence, and report output |
| Live-data deadline | 320s | No new live work after this point |
| K-line phase boundary | 110s | Preserve current K-line/Wyckoff phase cap |
| Top-up limit | 12 candidates | Preserve current bounded report frontier |
| Per-attempt timeout | 25s | Preserve current provider timeout |
| Capital concurrency | 4 | Preserve current limit |
| Fundamental concurrency | 4 | Increase from 2 to 4 |
| Top-up safety margin | 2s | Scheduling overhead allowance |
| Protected top-up window | 77s | Three 25s waves at concurrency 4, plus 2s |
| Latest initial-enrichment admission | 243s | `320s - 77s` from scan start |

The protected window must be derived from configuration rather than copied as
a separate magic number:

```text
topup_waves = ceil(CAPITAL_TOPUP_LIMIT / min(capital_workers, fundamental_workers))
topup_reserve = topup_waves * max(capital_timeout, fundamental_timeout) + safety_margin
initial_deadline = live_deadline - topup_reserve
```

With a 12-candidate top-up, concurrency 4 for both sources, 25-second
timeouts, and a 2-second margin, the reserve is 77 seconds. The formula must
be covered for differing source concurrency and timeouts. Capacity at the
top-up boundary remains calculated from actual remaining time using complete
provider windows; the reserve is not permission to overrun the live deadline.

## Implementation scope

### 1. Set the 330-second run budget

Modify `scripts/core/source_health.py`:

- Set `SCAN_DEADLINE_SECONDS = 330` and retain the 10-second finalization
  reserve.
- Keep `KLINE_PHASE_SECONDS = 110` and the 25-second provider timeouts.
- Raise `MAX_IN_FLIGHT["fundamental"]` from 2 to 4; keep capital concurrency
  at 4 and provider retry counts unchanged.
- Compute `CAPITAL_TOPUP_RESERVE_SECONDS` and
  `capital_initial_deadline` from the queue limit, concurrency, timeout, and
  safety margin.
- Preserve source-health circuit breakers and live-deadline enforcement.

### 2. Decouple top-up scheduling by data dimension

Modify `scripts/scans/stock_scanner.py`:

- Build capital and fundamental top-up batches independently.
- A candidate with capital capacity must be allowed a capital request even
  when the fundamental batch has less capacity.
- Keep separate processed sets, capacities, and evidence for capital and
  fundamental requests.
- Mark each dimension's unstarted work with the correct scheduler reason;
  do not copy a fundamental skip onto capital evidence or vice versa.
- Preserve queue ranking, deduplication, source-health admission, and the
  maximum top-up population of 12.

This means valid K-line plus valid capital evidence can reach 80% weighted
coverage even if fundamental enrichment cannot start. The existing 70%
coverage threshold and all fresh-data requirements stay unchanged.

### 3. Clarify scheduler diagnostics

Modify `scripts/scans/daily_candidates.py` and the audit assembly in
`scripts/scans/stock_scanner.py`:

- Render `deadline` as “已达到实时请求截止时间，未启动”.
- Render `budget_insufficient_for_attempt` as “剩余预算不足一个完整请求窗口，未启动”.
- Keep stable machine-readable status and reason codes.
- Report total budget, live deadline, remaining seconds at top-up, computed
  reserve, and per-source top-up selected/started/valid/skipped counts.

### 4. Diagnose stale capital results separately

Record expected trading date, returned latest data date, provider, fetch time,
cache timestamp, and stale validation reason for capital results. Confirm that
weekend runs use the most recent completed trading date. If a supported
fallback provider exists, try it only after the primary returns stale data;
do not repeatedly call the same source without a new freshness expectation.
Never promote an estimated or stale result to current measured capital flow.

This is diagnostic and data-source work; it must not weaken the scheduler's
budget protections or candidate quality gate.

## Tests

Add or update focused tests in:

- `tests/test_stock_scanner.py`
- `tests/test_daily_candidates.py`
- `tests/test_daily_recommendation_performance.py`

Cover these contracts:

1. Total deadline is 330 seconds, live deadline is 320 seconds, the K-line
   boundary remains 110 seconds, and finalization retains 10 seconds.
2. With a 12-candidate limit, concurrency 4 for both sources, 25-second
   timeouts, and 2-second safety margin, the dynamic reserve is 77 seconds
   and initial enrichment closes at 243 seconds.
3. Dynamic reserve and capacity remain correct when fundamental concurrency,
   source timeouts, or top-up limit differ.
4. With enough budget, all 12 selected candidates can be admitted to both
   top-up dimensions.
5. If fundamental capacity is 6 but capital capacity is 12, capital still
   starts for 12 and fundamental starts for 6; evidence is recorded
   independently.
6. Less than one complete request window starts no provider work and reports
   `budget_insufficient_for_attempt`; an elapsed live deadline reports
   `deadline`.
7. No new provider work can consume the finalization reserve or exceed the
   live deadline.
8. Stale capital, provider failures, scheduler omissions, and source
   unavailability remain distinct; none can pass the fresh-capital gate.
9. HTML/JSON diagnostics expose the configured budget and reconcile selected
   work as started plus skipped for each source.

After Python changes, run the mandatory repository quality gates:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Also run the three focused test files above. Do not regenerate golden output
unless an intended output change is reviewed and explained.

## Rollout and acceptance

First run a simulated test with 12 candidates and controlled provider delays.
Then run one post-close live scan and compare its source audit with the
2026-09-12 baseline. The change is accepted when:

- The scan completes within 330 seconds and the report finalizes inside the
  reserved 10 seconds.
- The top-up queue is still bounded at 12 and its capacity is calculated
  from actual remaining time.
- When sufficient capacity exists, both top-up dimensions start for all 12;
  if one source has less capacity, the other source proceeds independently.
- There are no 55%-coverage candidates caused solely by a coupled scheduler
  skip when current K-line and capital evidence are both available.
- No stale or failed data becomes eligible, and no source starts work after
  the live deadline.
- Source failure rates and circuit-breaker events do not materially worsen
  after fundamental concurrency increases.

If fundamental errors or rate limits rise materially, revert its concurrency
to 2 and let the dynamic reserve expand accordingly. Keep the 330-second
hard cap and independent dimension scheduling. If end-to-end time still
exceeds the cap, optimize or reduce upstream work; do not remove the
finalization reserve or bypass freshness checks.

## Non-goals

- Do not lower the 70% data-coverage threshold.
- Do not make missing or stale capital/fundamental data promotable.
- Do not increase provider retry counts as part of this change.
- Do not expand the sector universe to compensate for enrichment misses.
- Do not change scoring weights or recommendation policy.
