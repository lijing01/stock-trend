---
name: market-theme-snapshot-architecture
description: Market theme analysis now uses daily ranking snapshots instead of BK K-lines
metadata:
  type: reference
---

Market theme (`/market-theme`) no longer depends on East Money BK K-line data (`push2.eastmoney.com` for `90.BKxxxx`).

**Why**: BK K-line CDN was stale — data stuck at 2026-05-08, 22 days behind. Cache-buster `&_=timestamp` didn't help (upstream API issue).

**New architecture**:
- Phase 1: Sector rankings from East Money real-time API (unchanged)
- Phase 2: Load `sector_snapshot_history.json` — appended each trading day by `append_daily_snapshot()`
- Phase 3: Persistence score from snapshot data (on-list rate, avg hot, rank trend, today hot, up-ratio trend)

**Snapshot file**: `.cache/stock-trend/sector_snapshot_history.json` — dict of date → top-30 sector summaries. Auto-pruned to 30 days.

**Functions added** (`fetchers/sector_data.py`):
- `append_daily_snapshot(rankings)` — called on realtime data success
- `load_snapshot_history(days=N)` — loads last N snapshot days
- `_hot_ranked_sectors(rankings, top_n)` — internal ranking for archival

**Removed from `market_theme.py`**:
- `batch_fetch_kline` import and `fetch_kline_for_sectors()`
- `_get_kline_latest_trade_date()` and `_resolve_effective_date()`
- K-line persistence computation (`_compute_momentum`, `_up_days_ratio`, `_compute_volatility`, `_compute_acceleration`)
- CDN cache warnings

[[market-theme-usage]]
