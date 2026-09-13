"""Temporary scanner-backed provider adapter.

This module contains the remaining live-data bridge.  Backtesting imports
this provider boundary instead of the scan workflow; it will be replaced when
the sector-membership and K-line adapters migrate in phase 4.
"""


def gather_sector_candidates(sector_codes, top_n_per_sector=25):
    from scans.stock_scanner import gather_candidates
    return gather_candidates(sector_codes, top_n_per_sector=top_n_per_sector)


def fetch_stock_kline(ts_code):
    from scans.stock_scanner import _fetch_kline
    return _fetch_kline(ts_code)
