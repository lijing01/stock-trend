#!/usr/bin/env python3
"""Market theme (/market-theme) test suite.

Tests for analyze_market_theme.py covering:
  - window truncation via --days / lookback_days
  - min_score default removal (no invisible gap)
  - hot_threshold stability (double threshold)
  - edge cases: short/empty klines, boundary scores

Usage:
    python3 test_market_theme.py              # Run all tests
    python3 test_market_theme.py -v           # Verbose
"""

import argparse
import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import analyze_market_theme as amt

PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="market_theme"):
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


def make_kline(pct_chgs):
    """Build fake kline records from a list of pct_chg values."""
    return [{"pct_chg": c} for c in pct_chgs]


def make_sector(code="BK0001", name="测试板块", hot_score=80, change_pct=1.5, total_count=20):
    return {
        "code": code,
        "name": name,
        "hot_score": hot_score,
        "change_pct": change_pct,
        "total_count": total_count,
    }


# ──────────────────────── Test: window truncation (P0 #1) ────────────────────────


def test_window_truncation_uses_only_lookback_days():
    """compute_persistence with lookback_days should truncate data window.

    Build 20 records: first 10 are flat (0%), last 10 are strong up (2% each).
    With lookback=5 → takes last 5 (all strong 2%) → mom5=10.0, up_ratio=1.0
    With lookback=15 → takes last 15 (5 flat + 10 strong) → mom5=10.0, up_ratio=10/15≈0.67
    Default lookback=10 → takes last 10 (all strong 2%) → mom5=10.0, up_ratio=1.0
    """
    flat = [0.0] * 10
    strong = [2.0] * 10
    kline = make_kline(flat + strong)  # 20 records total

    sector = make_sector()

    # lookback=5: last 5 records = all 2% strong
    r5 = amt.compute_persistence(sector, kline, lookback_days=5)
    test(
        "lookback=5: momentum_5d ~10.0",
        r5 and abs(r5["momentum_5d"] - 10.0) < 0.01,
        f"got {r5['momentum_5d'] if r5 else 'None'}",
    )
    test(
        "lookback=5: up_days_ratio = 1.0 (5/5 up)",
        r5 and r5["up_days_ratio"] == 1.0,
        f"got {r5['up_days_ratio'] if r5 else 'None'}",
    )

    # lookback=15: last 15 = 5 flat + 10 strong → mom5 still last 5 strong
    r15 = amt.compute_persistence(sector, kline, lookback_days=15)
    test(
        "lookback=15: momentum_5d still ~10.0",
        r15 and abs(r15["momentum_5d"] - 10.0) < 0.01,
        f"got {r15['momentum_5d'] if r15 else 'None'}",
    )
    test(
        "lookback=15: up_days_ratio = 0.67 (10/15 up)",
        r15 and abs(r15["up_days_ratio"] - 10/15) < 0.01,
        f"got {r15['up_days_ratio'] if r15 else 'None'}",
    )

    # Default lookback=10: last 10 = all strong
    r10 = amt.compute_persistence(sector, kline)
    test(
        "lookback=default(10): momentum_5d ~10.0 (strong window)",
        r10 and abs(r10["momentum_5d"] - 10.0) < 0.01,
        f"got {r10['momentum_5d'] if r10 else 'None'}",
    )
    test(
        "lookback=default(10): up_days_ratio = 1.0 (10/10 up)",
        r10 and r10["up_days_ratio"] == 1.0,
        f"got {r10['up_days_ratio'] if r10 else 'None'}",
    )


def test_window_truncation_shorter_than_requested():
    """When kline is shorter than lookback_days, use all available."""
    kline = make_kline([1.0, 2.0, 3.0, 4.0])
    sector = make_sector()
    r = amt.compute_persistence(sector, kline, lookback_days=10)
    test(
        "lookback > len(kline): uses all 4 records",
        r is not None,
    )
    test(
        "lookback > len(kline): momentum_5d = sum of 4 records",
        r and abs(r["momentum_5d"] - 10.0) < 0.01,
        f"got {r['momentum_5d'] if r else 'None'}",
    )


# ──────────────────────── Test: min_score gap removal (P0 #2) ────────────────────────


def test_min_score_default_zero():
    """Default min_score=0 means low-persistence sectors aren't silently dropped.

    Verify persistence < 30 results flow through compute + classify correctly.
    """
    # Weak kline: 5 flat days → persistence should be low
    kline = make_kline([0.0, 0.0, 0.0, 0.0, 0.0])
    sector = make_sector(hot_score=30)
    r = amt.compute_persistence(sector, kline, lookback_days=5)
    classified = amt.classify_themes([r]) if r else {"fading": []}

    test(
        "low-persistence sector (< 30) is NOT dropped",
        r is not None,
        f"got {'None' if r is None else r['persistence']}",
    )
    test(
        "low-persistence sector appears in fading list",
        r is not None and r in classified.get("fading", []),
        f"persistence={r['persistence'] if r else 'N/A'}",
    )


# ──────────────────────── Test: hot_threshold stability (P0 #3) ────────────────────────


def test_hot_threshold_stable_in_weak_market():
    """In weak market where all hot_scores are low, no one-day wonders flagged."""
    results = [
        {"persistence": 30, "hot_score": 25},
        {"persistence": 35, "hot_score": 20},
    ]
    classified = amt.classify_themes(results)
    test(
        "weak market: no one-day wonders (all hot_score < 60)",
        len(classified["one_day_wonders"]) == 0,
        f"got {len(classified['one_day_wonders'])}",
    )


def test_hot_threshold_catches_one_day_wonder():
    """Sector with high hot_score but low persistence gets flagged."""
    results = [
        {"persistence": 80, "hot_score": 85},  # strong theme, not a wonder
        {"persistence": 30, "hot_score": 90},  # hot today, weak persistence
    ]
    classified = amt.classify_themes(results)
    test(
        "strong sector not flagged as one-day wonder",
        results[0] not in classified["one_day_wonders"],
    )
    test(
        "hot+weak sector flagged as one-day wonder",
        results[1] in classified["one_day_wonders"],
        f"wonders: {[r['hot_score'] for r in classified['one_day_wonders']]}",
    )


def test_hot_threshold_single_outlier_not_dragging():
    """Single outlier with extreme hot_score shouldn't drag normal sectors into one-day."""
    results = [
        {"persistence": 80, "hot_score": 99},   # outlier, but strong
        {"persistence": 45, "hot_score": 65},   # moderate hot, should NOT be wonder
        {"persistence": 45, "hot_score": 30},   # low hot, not a wonder
    ]
    classified = amt.classify_themes(results)
    one_day_codes = [r["hot_score"] for r in classified["one_day_wonders"]]
    test(
        "outlier scenario: only hot_score 65 might be wonder (threshold >= 60)",
        all(s >= 60 for s in one_day_codes),
        f"one-day scores: {one_day_codes}",
    )


# ──────────────────────── Test: edge cases ────────────────────────


def test_empty_kline():
    """Empty kline returns None."""
    r = amt.compute_persistence(make_sector(), [])
    test("empty kline returns None", r is None)


def test_short_kline():
    """Kline with < 3 records returns None."""
    r = amt.compute_persistence(make_sector(), make_kline([1.0, 2.0]))
    test("kline < 3 returns None", r is None)


def test_kline_exactly_3():
    """Kline with exactly 3 records computes without error."""
    r = amt.compute_persistence(make_sector(), make_kline([1.0, 2.0, 3.0]))
    test("kline=3 returns result", r is not None)
    test("kline=3: momentum_5d uses all 3 records", r and abs(r["momentum_5d"] - 6.0) < 0.01)
    test("kline=3: acceleration = 0 (needs 10)", r and r["acceleration"] == 0.0)


def test_classification_boundaries():
    """Verify classify_themes thresholds at boundaries."""
    results = [
        {"persistence": 70, "hot_score": 50},
        {"persistence": 50, "hot_score": 50},
        {"persistence": 40, "hot_score": 50},
        {"persistence": 39, "hot_score": 50},
    ]
    classified = amt.classify_themes(results)
    test(">= 70 → strong", len(classified["strong"]) == 1)
    test("50-69 → moderate", len(classified["moderate"]) == 1)
    test("40-49 → emerging", len(classified["emerging"]) == 1)
    test("< 40 → fading", len(classified["fading"]) == 1)


def test_all_sectors_fading():
    """When all sectors are fading, min_score=0 means they all appear."""
    results = [
        {"persistence": 10, "hot_score": 30},
        {"persistence": 20, "hot_score": 25},
    ]
    classified = amt.classify_themes(results)
    test("all fading: strong empty", len(classified["strong"]) == 0)
    test("all fading: moderate empty", len(classified["moderate"]) == 0)
    test("all fading: emerging empty", len(classified["emerging"]) == 0)
    test("all fading: fading has all", len(classified["fading"]) == 2)


def test_generate_report_uses_lookback_days_for_up_day_count():
    """Markdown report should not hardcode 10 when rendering up-day count."""
    classified = {
        "strong": [{
            "name": "主题A",
            "hot_score": 80,
            "momentum_5d": 5.0,
            "momentum_10d": 8.0,
            "up_days_ratio": round(10 / 15, 2),
            "persistence": 72.3,
            "trend_label": "↗ (延续)",
        }],
        "moderate": [{
            "name": "主题B",
            "hot_score": 65,
            "momentum_5d": 3.0,
            "momentum_10d": 4.5,
            "up_days_ratio": round(10 / 15, 2),
            "persistence": 55.0,
            "trend_label": "→ (走平)",
        }],
        "emerging": [],
        "fading": [],
        "one_day_wonders": [],
    }
    report = amt.generate_report(classified, {"scan_time": "20260528-160000"}, 15)
    test("report uses 10/15 for strong up-day count", "| 主题A | 80 | +5.0% | +8.0% | 10/15 | **72.3** | ↗ (延续) |" in report)
    test("report uses 10/15 for moderate up-day count", "| 主题B | 65 | +3.0% | 10/15 | 55.0 | → (走平) |" in report)


def test_generate_html_report_persistence_cell_has_closed_class_quote():
    """HTML report should render valid class attribute for persistence cell."""
    cell = amt._COL_RENDERERS["persistence"]({"persistence": 72.3})
    test("persistence cell has valid class attribute", cell == '<td class="ml-strong">72.3</td>', f"got {cell}")


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Market Theme test suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("Market Theme Test Suite")
    print("=" * 60)

    # ── Window truncation ──
    print("\n--- Window truncation (P0 #1) ---")
    test_window_truncation_uses_only_lookback_days()
    test_window_truncation_shorter_than_requested()

    # ── min_score gap ──
    print("\n--- min_score gap (P0 #2) ---")
    test_min_score_default_zero()

    # ── hot_threshold ──
    print("\n--- hot_threshold stability (P0 #3) ---")
    test_hot_threshold_stable_in_weak_market()
    test_hot_threshold_catches_one_day_wonder()
    test_hot_threshold_single_outlier_not_dragging()

    # ── Edge cases ──
    print("\n--- Edge cases ---")
    test_empty_kline()
    test_short_kline()
    test_kline_exactly_3()
    test_classification_boundaries()
    test_all_sectors_fading()

    # ── Report rendering ──
    print("\n--- Report rendering (P0) ---")
    test_generate_report_uses_lookback_days_for_up_day_count()
    test_generate_html_report_persistence_cell_has_closed_class_quote()

    # ── Summary ──
    total = PASSED + FAILED + SKIPPED
    print(f"\n{'=' * 60}")
    print(f"Results: {PASSED}/{total} passed, {FAILED} failed, {SKIPPED} skipped")
    print(f"{'=' * 60}")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
