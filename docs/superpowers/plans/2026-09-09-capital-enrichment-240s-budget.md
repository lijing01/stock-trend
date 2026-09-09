# Daily Candidates: 240-Second Capital-Enrichment Budget

## Decision

Set the daily-candidates scan budget to 240 seconds.  This adds one minute
to the prior 180-second envelope so the capital-enrichment phase has a
meaningful opportunity to complete the global initial queue and its final
report top-up pass.

## Budget contract

| Stage | Previous | 240-second profile |
|---|---:|---:|
| Total scan deadline | 180s | 240s |
| Finalization reserve | 10s | 10s |
| Live-data deadline | 170s | 230s |
| K-line phase boundary | 110s | 110s |
| Protected top-up reserve | 52s | 52s |
| Latest initial-capital admission | 118s | 178s |

The resulting capital window after the K-line boundary grows from about 60
seconds to about 120 seconds.  The initial queue cannot consume the 52-second
top-up reserve, so the additional minute cannot remove the second-pass
protection added for `not_started_deadline` remediation.

## Scope

- Change only `SCAN_DEADLINE_SECONDS` in `core/source_health.py`.
- Keep the K-line boundary, 25-second provider attempt timeout, provider
  retry count, concurrency, initial queue limit (36), top-up limit (12), and
  recommendation/data-quality gates unchanged.
- Update the production performance contract test to assert the 240-second
  default.

## Expected effect

The three top-up candidates in the 2026-09-09 report that were marked
`not_started_deadline` because no complete 25-second attempt remained should
now receive an executable slot when upstream phases finish within their
existing bounds.  A successful fetch can change a candidate's capital score,
signals, warnings, and ordering, but cannot bypass market-regime, sector
persistence, or data-quality gates.

## Verification and rollback

Run both repository quality gates after the change.  For post-close smoke
runs, audit `capital_topup_selected_count`, `capital_topup_live_started`,
`capital_topup_valid_count`, and `capital_topup_skipped_deadline`; confirm the
report completes before 240 seconds.  If the longer window causes operational
issues, reverting the single constant restores the previous 180-second
profile without changing scheduling semantics.

### First post-change smoke run

The 2026-09-09 22:15 run completed in 127.242 seconds.  The capital source
was healthy (`18` live starts, `39` valid cache hits, `57` valid results); the
top-up queue was empty, so `capital_topup_selected=0`,
`capital_topup_budget_insufficient=0`, and `capital_topup_skipped_deadline=0`.
This confirms the 240-second envelope completes comfortably, but a later run
with missing capital caches is still required to measure how many additional
top-up calls the extra minute admits.  The report correctly remained without
actionable recommendations because the market score was 34.1 (weak).
