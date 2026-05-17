# Plan: ETF指数PE分位评分 (Index PE Percentile Scoring for ETFs)

## Context

ETF fundamental dimension is currently **always 0** because `fetch_fundamental.py` skips ETFs entirely (`asset == "FD"` → `data_quality: "skip"`). The 15% fundamental weight is wasted. Per trend-dimensions.md, "跟踪指数PE分位" is P0 for ETF investing — it's the core valuation anchor for medium-term ETF decisions.

## Approach

### 1. New script: `fetch_index_valuation.py`

Fetch index PE percentile for ETFs' tracking indices. Tiered data sources:

- **Tier 1** — `stock_index_pe_lg` (乐咕乐股): Historical PE time series for ~4 major A-share indices (上证50, 沪深300, 上证380, 深证100). Computes 3-year PE percentile from ~5000 data points. Reliable for the most common ETFs.
- **Tier 2** — `stock_zh_index_value_csindex` (中证指数): Current PE + dividend yield for all CSIndex indices (000300, 000905, 000852, etc.). Only 20 data points — can compute short-term percentile but less precise.
- **Tier 3** — HK indices: No reliable automated API. Falls back to `data_quality: "skip"` with a note. Future: can add East Money scraping or manual data.

**Output format** (index_valuation.json):
```json
{
  "meta": {
    "etf_code": "513180",
    "index_name": "恒生科技",
    "data_source": "legulegu|csindex|skip",
    "data_quality": "good|partial|skip"
  },
  "summary": {
    "pe_ttm": 22.9,
    "pe_percentile_3y": 32.0,
    "dividend_yield_pct": 1.5,
    "data_quality": "good"
  },
  "data": { "...": "raw PE series last 5 entries" },
  "errors": []
}
```

**ETF → Index mapping**: Hardcoded dict for common ETFs, expandable. Example:
```python
ETF_INDEX_MAP = {
    "510300": {"index_name": "沪深300", "lg_name": "沪深300", "csindex_code": "000300"},
    "510500": {"index_name": "中证500", "lg_name": "中证500", "csindex_code": "000905"},
    "513180": {"index_name": "恒生科技", "lg_name": None, "csindex_code": None},  # HK, no API
    "159915": {"index_name": "创业板指", "lg_name": "创业板50", "csindex_code": None},
}
```

### 2. Modify `run_pipeline.py`

Add `fetch_index_valuation.py` step for ETFs (supplements `fetch_fundamental.py` which skips ETFs):
- Run in parallel with other Step 4 tasks
- Store output as `index_valuation.json` in data directory
- Pass path to `compute_scores.py`

### 3. Modify `compute_scores.py`

Add ETF-specific fundamental scoring block after the existing `args.fundamental_data` block:

```python
# ETF index valuation scoring (supplements fundamental for ETFs)
if args.asset_type == "etf" and args.fundamental_score is None:
    # Load index_valuation.json
    # Use pe_percentile_3y for scoring (same logic as stocks)
    # Use dividend_yield_pct for ETF dividend bonus
    # Track in automated_sources
```

Scoring rules (same as stock fundamentals):
- PE < 30th percentile → +1
- PE > 70th percentile → -1
- Dividend yield > 3% → +1
- Max contribution: [-2, +2] within the [-3, +3] fundamental dimension

### 4. Keep `fetch_fundamental.py` unchanged for ETFs

`fundamental.json` for ETFs stays `data_quality: "skip"`. Index valuation is a separate data source (`index_valuation.json`) that feeds into the same fundamental dimension score.

## Files to Modify

| File | Change |
|------|--------|
| `scripts/fetch_index_valuation.py` | **NEW** — Index PE percentile fetcher |
| `scripts/run_pipeline.py` | Add index_valuation step for ETFs |
| `scripts/compute_scores.py` | Add ETF fundamental scoring from index_valuation |
| `tests/golden/513180/index_valuation.json` | **NEW** — golden test data |
| `tests/golden/513180/scores.json` | Update fundamental score |

## Key Decisions

1. **Separate file vs extending fundamental.py**: Separate `index_valuation.json` keeps architecture clean. ETF fundamental stays `skip` in fundamental.json; index valuation is a new data source that contributes to the fundamental dimension score.

2. **HK indices**: No automated API available. For 513180 (恒生科技), the golden test will show `data_quality: "skip"` with a note about HK index limitation. Future enhancement can add scraping or manual data feeds.

3. **Scoring scale**: Same as stock fundamentals — PE < 30th percentile → +1, PE > 70th percentile → -1. Dividend yield > 3% → +1 for ETFs. Max contribution: [-2, +2] within the [-3, +3] fundamental dimension.

## Verification

1. `python3 scripts/test_stock_trend.py` — existing tests pass
2. `python3 tests/test_golden.py --diff` — golden snapshot diff
3. Manual test: `python3 scripts/run_pipeline.py --code 510300` — 沪深300ETF should show index PE percentile
4. Manual test: `python3 scripts/run_pipeline.py --code 513180` — 恒生科技ETF should show `data_quality: "skip"` for index valuation (HK index, no API)
5. If golden changes are reasonable (new `index_valuation.json` file, updated `scores.json`), use `--regenerate`