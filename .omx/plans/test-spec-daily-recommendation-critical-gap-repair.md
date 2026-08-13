# Test Spec — Daily Recommendation Critical Gap Repair

## Required suites

1. `test_daily_candidates.py`: ranking reuse, duplicate-sector merge, primary-sector rebinding, output compatibility.
2. `test_stock_scanner.py`: bounded source health, failure classification, cache validators, expensive-fetch funnel, scoring equivalence.
3. `test_daily_recommendation_performance.py`: deterministic 20-sector/~250-row/30%-duplicate fixture with normal, slow, and unavailable sources.
4. `test_stock_trend.py`: repository regression gate.
5. `test_golden.py --diff`: output regression gate.

## Behavioral assertions

- Ranking fetch count is exactly one per run.
- Each unique stock has at most one K-line, capital, and fundamental acquisition.
- Sector traversal order does not change primary sector, score, order, or recommendation bucket.
- A later better sector rebinds all sector-derived fields without new stock-data I/O.
- `ranking_position` comes from the shared full ranking snapshot; mixed fresh/stale memberships prefer the actionable sector even when stale raw heat is higher.
- Cross-batch rebinding with different peer cohorts produces the same sector score and order regardless of traversal order.
- With early stopping actually triggered, shuffled sector input is normalized by the shared ranking snapshot and processes the same complete batch frontier, candidate set, and peer cohorts.
- Fresh-to-stale and stale-to-fresh rebinding recompute membership-overlay quality from immutable base data quality, including eligibility, reasons, freshness, and adjusted score.
- Failure reasons distinguish DNS, timeout, HTTP, empty, parse, subprocess, and unknown.
- All five source families expose compatible internal `{payload, live_attempt}` evidence without changing public payload/CLI contracts.
- No new live request is submitted after a source circuit opens.
- Live admission and request counting occur atomically through a permit; no separate check/record race is allowed.
- With `failure_threshold=2` and `max_in_flight=2`, the exact failed-source live-request upper bound is `2 + 2 - 1 = 3`.
- Invalid cache is diagnostic-only in cache-only mode and cannot become actionable.
- Valid warm cache bypasses subprocesses.
- Intraday/post-close/weekend/holiday/last-preholiday cases use the single main-flow `expected_trading_date`; validators never compare against a raw natural date.
- JSON top-level compatibility remains intact and performance fields are additive.
- Metrics collection does not change scores, ordering, or buckets.

## Performance assertions

- Use mock time and fixed delays; do not use flaky wall-clock assertions in automated tests.
- Import production `SCAN_DEADLINE_SECONDS`, `FINALIZATION_RESERVE_SECONDS`, per-source timeout caps, and provider-attempt limits; compute the worst serial/parallel path and assert `live critical path + cache-only/rebind/score/output reserve <= 45 seconds`.
- Slow sources do not produce candidate-count-times-timeout behavior.
- Metrics exactly reconcile with mock call logs; `report_seconds` excludes final serialization/output/write time.
- `logical_live_requests` reconcile with permits; `provider_attempts` reconcile with inner fallback attempts; circuit limits use only logical requests.
- Success/failure completion atomically records `live_attempt.provider_attempts`; submit failure and pre-start cancellation leave `logical_live_requests`, failures, and provider attempts unchanged and return in-flight count to zero. Only `mark_started(token)` increments logical live requests.
- Deadline tests cover normal completion, per-source timeout, sequential source failures, unstarted-token release, started-task timeout, cache-only remainder, and ignored late results.
- A late future cannot block scanner return beyond the total deadline; finalization reserve covers cache-only collection, peer rebinding, scoring, classification, and output-model assembly.
- Manual release evidence includes warm cache ≤30s, outage ≤45s, and a recorded cold-cache baseline.

## Verification order

1. New failing tests demonstrate each known gap.
2. Targeted suites pass after each implementation phase.
3. Repository regression gate passes.
4. Golden diff passes without snapshot regeneration unless every output difference is intentional and documented.
5. Manual benchmarks run last and are recorded in the source plan.

## Stop conditions

- Stop when all assertions and benchmarks pass; keep P2 deferred.
- If a benchmark fails, use audit metrics to identify the phase and continue only in the responsible P0/P1 area.
- Open a separate P2 plan only if evidence isolates subprocess startup as the remaining material bottleneck.
