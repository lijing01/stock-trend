# Daily Recommendation Performance Gap Fix — Context

## Task statement

Create an executable repair plan for the critical gaps remaining from `docs/2026-08-12-daily-recommendation-performance-fix-plan.md`.

## Desired outcome

A bounded implementation and test plan that closes the verified P0-2, P1-1, P1-2, P1-3, and completion-evidence gaps without changing recommendation thresholds or beginning the deferred P2 fetcher de-subprocess refactor.

## Known evidence

- P0-1 ranking-context reuse and P0-3 K-line/Wyckoff funnel are functionally present.
- P0-2 deduplicates deep analysis but does not reselect a duplicate stock's primary sector when a later, stronger sector appears; memberships retain only code/name.
- P1-1 has a simple per-source three-state failure counter, but all work is submitted concurrently, so it does not provide bounded probes; failure reasons and circuit/cache-only/stale audit events are absent. Ranking health is outside this context.
- P1-2 fast paths exist, but K-line cache validation omits source/error checks and fundamental cache validation omits source/data-quality checks.
- P1-3 emits partial `meta.performance`; sector-membership/report timings, complete counters, stderr summary, report audit, and final valid count are absent.
- The deterministic 20-sector/~250-stock performance fixture is absent. The recorded outage run is 45.36s against a stated 45s upper bound; warm/cold normal-network baselines are not documented.
- Fresh verification on 2026-08-13: targeted suites passed 53/53 and 34/34; `test_stock_trend.py` and `test_golden.py --diff` exited successfully.
- The documented `python3 -m unittest .claude/...` command is invalid because the hidden path is not an importable module name.

## Constraints

- Preserve recommendation scoring thresholds, date freshness, market/sector/data-quality gates, JSON compatibility, and observation-only degradation semantics.
- No new dependencies.
- Do not read or modify `reports/` during planning.
- Do not implement P2 unless post-fix deterministic and real benchmarks show it is necessary.
- Any later Python changes under `.claude/skills/stock-trend/scripts/` require the two repository quality gates.

## Likely touchpoints

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`
- `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`
- Potential new `.claude/skills/stock-trend/scripts/core/source_health.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Potential new deterministic performance test module
- `docs/2026-08-12-daily-recommendation-performance-fix-plan.md`

## Open questions resolved by planning assumption

- Treat P2 as explicitly out of scope and gate it behind evidence.
- Prefer one shared run-scoped source-health component over duplicated dictionaries.
- Use deterministic mocked-time/request-count tests as the primary performance acceptance evidence; retain one actual-environment benchmark as a manual release check.
