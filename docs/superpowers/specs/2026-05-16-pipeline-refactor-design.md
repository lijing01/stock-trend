# Pipeline Refactor Design — 2026-05-16

## Goal

Simplify Agent workflow from 10-step manual orchestration to 4-step automation, reduce code duplication across 6 fetch scripts, replace fragile CLI parameter passing with JSON files, and improve cache reliability and timeout handling.

## Priority Order

1. Pipeline orchestration (highest impact)
2. Code dedup (BaseFetcher + eastmoney_utils)
3. Data passing (JSON intermediate files)
4. Cache reliability + timeout degradation

---

## 1. Pipeline One-Command Entry

### Current State

Agent follows 332-line SKILL.md, manually calling 6+ scripts in sequence with dozens of CLI arguments. Any parameter mistake breaks the pipeline.

### Change

`run_pipeline.py` gains `--code` and `--mode full` arguments. Internal orchestration:

```
run_pipeline.py --code 513180 --mode full
```

Internally runs:
1. resolve_code (if needed)
2. fetch_kline → fetch_kline_eastmoney fallback
3. analyze_technical
4. fetch_etf_data (if ETF)
5. fetch_capital_flow
6. fetch_fundamental (skip if ETF)
7. fetch_macro_snapshot

All outputs written to `/tmp/stock-trend-cache/{code}/pipeline_output.json`.

### New Agent Flow

1. `python run_pipeline.py --code 513180 --mode full` — data in one step
2. Agent does 4-dimension web search + Step 3.5 reverse check
3. `python compute_scores.py --code 513180` — reads JSON files, no CLI params
4. `python generate_report.py --code 513180` — generates report

### SKILL.md Change

Simplify from 332-line procedural instructions to 4-step summary + error handling guidance. Keep reference docs unchanged.

---

## 2. BaseFetcher + eastmoney_utils

### Current State

6 fetch scripts each duplicate: argparse setup, JSON output formatting, error handling, cache read/write. `EM_HEADERS` and secid mapping duplicated in 3 files.

### Change

**New file: `base_fetcher.py`**

```python
class BaseFetcher:
    def __init__(self, args):
        self.code = args.code
        self.output = args.output  # optional override

    def fetch(self) -> dict:
        """Subclass implements this."""
        raise NotImplementedError

    def run(self):
        """Unified entry: cache check → fetch → cache write → JSON output."""
        ...
```

Features:
- Unified argparse (`--code`, `--output`, `--no-cache`)
- Unified JSON output with metadata (timestamp, source, code)
- Unified error handling (try/except → stderr + exit code)
- Cache integration via `cache_utils.py`

**New file: `eastmoney_utils.py`**

- `EM_HEADERS` constant
- `build_secid(code) -> str` function
- `rotate_node(session, max_retries=3)` — 3-node rotation logic
- Used by: `fetch_kline_eastmoney.py`, `fetch_capital_flow.py`, `fetch_etf_data.py`

**Refactored scripts**

Each fetch script:
- Inherits from `BaseFetcher`
- Implements only `fetch(self) -> dict`
- Removes: argparse boilerplate, JSON output formatting, error handling, cache logic
- Estimated reduction: 30-50% per file

---

## 3. JSON Intermediate Files

### Current State

`compute_scores.py` accepts 6+ CLI parameters including nested JSON strings (`--self-check`, `--signals-info`, `--analysis`). Error-prone and hard to debug.

### Change

All scripts write output to `/tmp/stock-trend-cache/{code}/`:

| File | Producer | Consumer |
|------|----------|----------|
| `pipeline_output.json` | run_pipeline | compute_scores, generate_report |
| `technical.json` | analyze_technical | compute_scores |
| `capital_flow.json` | fetch_capital_flow | compute_scores |
| `fundamental.json` | fetch_fundamental | compute_scores |
| `macro_snapshot.json` | fetch_macro_snapshot | compute_scores |
| `search_results.json` | Agent web search | compute_scores |

`compute_scores.py` changes:
- Required: `--code 513180` (locates data directory)
- Optional: `--data-dir /tmp/stock-trend-cache/513180` (default auto-inferred)
- Reads all dimension data from JSON files in the directory
- Removes: `--capital-flow-score`, `--sentiment-summary`, `--self-check`, etc.
- Backward compat: keep old CLI params as overrides (deprecated)

`generate_report.py` changes similarly:
- Required: `--code 513180`
- Reads `pipeline_output.json`, `technical.json`, `scores.json` from data dir
- Backward compat: keep old CLI params as overrides (deprecated)

### Data Flow

```
fetch scripts → write JSON files → compute_scores reads JSON → generate_report reads JSON
```

CLI parameter passing eliminated for main flow.

---

## 4. Cache Reliability + Timeout Degradation

### Current State

- Cache in `/tmp/stock-trend-cache/` — cleared on reboot
- No cache size management
- Pipeline timeout handled by Agent manually, no script-level enforcement
- Timeout kills entire pipeline

### Change

**Cache directory**: `/tmp/stock-trend-cache/` → `.cache/stock-trend/` (project-relative, survives reboot)

Update `cache_utils.py`:
- Change `DEFAULT_CACHE_DIR` to `.cache/stock-trend/`
- Add `clean_cache(max_size_mb=200)` — LRU eviction of oldest cache files when total size exceeds limit
- Called at pipeline start

**Pipeline timeout**: `run_pipeline.py` sets per-step `timeout=30s`

```python
result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
except subprocess.TimeoutExpired:
    # Mark this dimension as timeout
    output["timeouts"] = output.get("timeouts", []) + [step_name]
    output[step_name] = {"error": "timeout", "source": None}
```

Behavior:
- Timeout on single step → mark dimension as timed out, continue remaining steps
- `pipeline_output.json` includes `timeouts: ["capital_flow"]` field
- `compute_scores.py` reads timeout markers → adjust dimension weights (existing logic, enhanced)

---

## Files Changed

| File | Change |
|------|--------|
| `scripts/base_fetcher.py` | **New** — BaseFetcher class |
| `scripts/eastmoney_utils.py` | **New** — EM headers, secid, node rotation |
| `scripts/cache_utils.py` | Modify — new cache dir, add clean_cache() |
| `scripts/run_pipeline.py` | Major — add --code/--mode, orchestration logic, timeout handling |
| `scripts/compute_scores.py` | Major — read JSON files, deprecate CLI params |
| `scripts/generate_report.py` | Modify — read JSON from data dir |
| `scripts/fetch_kline.py` | Refactor — inherit BaseFetcher |
| `scripts/fetch_kline_eastmoney.py` | Refactor — inherit BaseFetcher, use eastmoney_utils |
| `scripts/fetch_capital_flow.py` | Refactor — inherit BaseFetcher, use eastmoney_utils |
| `scripts/fetch_etf_data.py` | Refactor — inherit BaseFetcher, use eastmoney_utils |
| `scripts/fetch_fundamental.py` | Refactor — inherit BaseFetcher |
| `scripts/fetch_macro_snapshot.py` | Refactor — inherit BaseFetcher |
| `SKILL.md` | Simplify — 4-step flow instead of 10-step |

## Out of Scope

- Template engine replacement (generate_report.py's Mustache-lite works for now)
- SQLite or other DB migration
- CI/CD pipeline setup
- Web search automation (remains Agent responsibility)