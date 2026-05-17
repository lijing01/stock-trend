"""Tests for backtest_engine.py"""
import sys
import json
import os
import tempfile
import math
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backtest_engine import (
    spearman_rank_correlation, slice_kline_to_date, find_kline_date_index,
    load_watchlist,
)

# Test result tracking
PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="backtest"):
    global PASSED, FAILED, SKIPPED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def skip(name, reason=""):
    global SKIPPED
    SKIPPED += 1
    RESULTS.append({"name": name, "status": "SKIP", "detail": reason, "category": "skip"})
    print(f"  [SKIP] {name}" + (f" — {reason}" if reason else ""))


def _make_kline(dates, prices, vols=None):
    """Build synthetic K-line list."""
    records = []
    for i, d in enumerate(dates):
        p = prices[i] if i < len(prices) else 100.0
        v = vols[i] if vols and i < len(vols) else 1000000
        records.append({"trade_date": d, "open": p, "close": p, "high": p * 1.01, "low": p * 0.99,
                        "vol": v, "amount": p * v, "pre_close": prices[i-1] if i > 0 else p,
                        "change": p - (prices[i-1] if i > 0 else p), "pct_chg": 0})
    return records


# ── Unit tests ───────────────────────────────────────


def test_spearman_perfect():
    """Perfect monotonic correlation = 1.0."""
    x = [1, 2, 3, 4, 5]
    y = [2, 4, 6, 8, 10]
    r = spearman_rank_correlation(x, y)
    test("TBT-01: perfect correlation", abs(r - 1.0) < 0.001)


def test_spearman_inverse():
    """Inverse monotonic correlation = -1.0."""
    x = [1, 2, 3, 4, 5]
    y = [10, 8, 6, 4, 2]
    r = spearman_rank_correlation(x, y)
    test("TBT-02: inverse correlation", abs(r - (-1.0)) < 0.001)


def test_spearman_no_corr():
    """Effectively no correlation ≈ 0."""
    x = [1, 2, 3, 4, 5]
    y = [3, 5, 1, 4, 2]
    r = spearman_rank_correlation(x, y)
    test("TBT-03: near-zero correlation", abs(r) < 0.5)


def test_spearman_small():
    """Fewer than 3 points returns 0."""
    r = spearman_rank_correlation([1, 2], [3, 4])
    test("TBT-04: small sample", r == 0.0)


def test_spearman_ties():
    """Ties should still produce reasonable results."""
    x = [1, 2, 2, 3, 4]
    y = [2, 3, 3, 4, 5]
    r = spearman_rank_correlation(x, y)
    test("TBT-05: ties", r > 0.8)


def test_spearman_known():
    """Known Spearman value: x=[1,2,3,4,5], y=[5,6,7,8,7] → ~0.825 (tie at 7)."""
    x = [1, 2, 3, 4, 5]
    y = [5, 6, 7, 8, 7]
    r = spearman_rank_correlation(x, y)
    # Tie at y=7 (rank 3.5), so d^2 = (3-3.5)^2 + (4-5)^2 + (5-3.5)^2 = 3.5
    # r = 1 - 6*3.5/(5*24) = 1 - 21/120 = 0.825
    test("TBT-06: known value", abs(r - 0.825) < 0.001)


def test_slice_kline_to_date():
    """Slice K-line up to target date."""
    dates = ["20260105", "20260106", "20260107", "20260108", "20260109"]
    kline = _make_kline(dates, [100] * 5)
    sliced = slice_kline_to_date(kline, "20260107")
    test("TBT-07: slice to date", len(sliced) == 3 and sliced[-1]["trade_date"] == "20260107")


def test_slice_kline_partial():
    """Slice with date in middle returns correct count."""
    dates = ["20260105", "20260106", "20260107", "20260108", "20260109"]
    kline = _make_kline(dates, [100] * 5)
    sliced = slice_kline_to_date(kline, "20260106")
    test("TBT-08: slice partial", len(sliced) == 2)


def test_slice_kline_with_hyphen():
    """Handle YYYY-MM-DD input."""
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    kline = _make_kline(dates, [100] * 5)
    sliced = slice_kline_to_date(kline, "2026-01-07")
    test("TBT-09: slice with hyphen", len(sliced) == 3)


def test_find_kline_index():
    """Find index of first record >= target date."""
    dates = ["20260105", "20260106", "20260107", "20260108", "20260109"]
    kline = _make_kline(dates, [100] * 5)
    idx = find_kline_date_index(kline, "20260107")
    test("TBT-10: find date index", idx == 2)


def test_find_kline_index_beyond():
    """Return len when target beyond last date."""
    dates = ["20260105", "20260106", "20260107", "20260108", "20260109"]
    kline = _make_kline(dates, [100] * 5)
    idx = find_kline_date_index(kline, "20260115")
    test("TBT-11: find beyond range", idx == len(kline))


def test_watchlist_load():
    """Load watchlist returns list of ETFs."""
    etfs = load_watchlist()
    test("TBT-12: watchlist loaded", len(etfs) > 50)
    test("TBT-13: watchlist has codes", all(e.get("code") for e in etfs))
    test("TBT-14: watchlist has ts_code", all(e.get("ts_code") for e in etfs))


def test_watchlist_focus():
    """Filter by focus category."""
    etfs = load_watchlist(focus="科技")
    test("TBT-15: watchlist filtered", len(etfs) > 0 and all("科技" in e["category"] for e in etfs))


def test_watchlist_single_etf():
    """Filter by single ETF code."""
    etfs = load_watchlist(etf_code="513180")
    test("TBT-16: single ETF", len(etfs) == 1 and etfs[0]["code"] == "513180")


def test_degraded_dimensions():
    """Score with capital_flow=None still works (weight redistribution)."""
    from etf_scanner import score_momentum, score_volume, compute_quick_score
    kline = _make_kline([f"202601{str(i).zfill(2)}" for i in range(5, 85)], [100 + i for i in range(80)])
    result = {
        "code": "513180",
        "ts_code": "513180.SH",
        "kline": kline,
        "capital_flow": None,
        "etf_data": None,
    }
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20, "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    test("TBT-17: degraded dims produce score",
         scored.get("quick_score") is not None and scored["quick_score"] > 0)


# ── Integration test ──────────────────────────────────


def test_integration_small_backtest():
    """Run backtest on 3 ETFs with 20-day lookback."""
    import subprocess
    script_path = SCRIPTS_DIR / "backtest_engine.py"
    cmd = [sys.executable, str(script_path),
           "--etf", "513180",
           "--lookback-days", "20",
           "--eval-windows", "5,10",
           "--sample-interval", "10"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            test("TBT-I01: small backtest", False, detail=f"exit={result.returncode} stderr={result.stderr[:200]}")
            return
        data = json.loads(result.stdout)
        has_meta = "meta" in data
        etfs_tested = data.get("meta", {}).get("total_etfs_tested", 0)
        test("TBT-I01: small backtest runs", has_meta and data["meta"].get("error") is None,
             detail=f"etfs={etfs_tested}")
    except subprocess.TimeoutExpired:
        test("TBT-I01: small backtest", False, detail="timeout")
    except json.JSONDecodeError:
        test("TBT-I01: small backtest", False, detail="invalid JSON")


# ── Runner ────────────────────────────────────────────


def run_backtest_tests():
    """Run all backtest engine tests."""
    print("\n📊 回测验证测试 (Backtest)")
    print("=" * 50)

    test_spearman_perfect()
    test_spearman_inverse()
    test_spearman_no_corr()
    test_spearman_small()
    test_spearman_ties()
    test_spearman_known()
    test_slice_kline_to_date()
    test_slice_kline_partial()
    test_slice_kline_with_hyphen()
    test_find_kline_index()
    test_find_kline_index_beyond()
    test_watchlist_load()
    test_watchlist_focus()
    test_watchlist_single_etf()
    test_degraded_dimensions()
    test_integration_small_backtest()

    print(f"\nBacktest 结果: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return PASSED, FAILED


if __name__ == "__main__":
    run_backtest_tests()
    if FAILED > 0:
        sys.exit(1)
