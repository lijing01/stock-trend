# K-line cache cutover cleanup pass

Scope: `core/kline_cache.py`, the three K-line/capital-flow consumers, scanner,
pipeline runner, and `test_kline_cache.py`.

Behavior lock: run the cache, scanner, capital-flow, golden, and repository
regression suites before and after the pass.

Findings and classifications:

- Legacy flat-cache reads and per-run `kline.json` exports are grounded
  compatibility/output boundaries. Keep them while the managed cache is the
  first read path; they preserve existing CLI/report contracts and have
  regression coverage.
- Provider retry/fallback branches are grounded fail-safe behavior because
  source identity is recorded and each provider is searched in a fixed order.
- Successful K-line writes through `save_cache` were duplicate storage. Replace
  them with `publish_kline_cache`; retain only compatibility imports required by
  existing test seams.
- Pipeline startup must invoke managed cleanup so the authoritative namespace
  receives the same bounded lifecycle as legacy cache files.

Pass order:

1. Repair boundary conditions and cleanup invocation.
2. Remove duplicate successful K-line writes.
3. Re-run targeted regression tests and quality gates.
4. Perform a final diff and fallback inventory review.
