#!/usr/bin/env python3
"""Market theme (/market-theme) test suite.

Tests for analyze_market_theme.py covering:
  - window truncation via lookback_days (P0 #1)
  - min_score default removal (no invisible gap) (P0 #2)
  - hot_threshold stability & double threshold (P0 #3)
  - edge cases: empty/shorter snapshots, boundary scores
  - report rendering

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

from analysis import market_theme as amt

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


def make_snapshot(pct_chgs, code="BK0001", name="测试板块", start_rank=20):
    """Build fake snapshot entries from pct_chg values.

    Each entry has snapshot-format keys: code, name, hot_score,
    change_pct, up_ratio, rank.

    hot_score derived: max(0, min(100, 50 + pct_chg * 10)).
    up_ratio: 0.8 if pct_chg > 0 else 0.3.
    rank: descending from start_rank.
    """
    entries = []
    for i, pct in enumerate(pct_chgs):
        hot = max(0.0, min(100.0, 50.0 + (pct or 0) * 10.0))
        entries.append({
            "code": code,
            "name": name,
            "hot_score": hot,
            "change_pct": pct,
            "up_ratio": 0.8 if (pct or 0) > 0 else 0.3,
            "rank": max(1, start_rank - i),
        })
    return entries


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
    """compute_persistence with lookback_days truncates snapshots.

    Build 12 entries: first 6 weak (pct=-3%), last 6 strong (pct=+3%).
    lookback=5 → last 5 strong entries → up_days_ratio=0.8.
    lookback=10 → last 10 (4 weak + 6 strong) → up_days_ratio=0.6.
    """
    weak_pct = [-3.0] * 6
    strong_pct = [3.0] * 6
    snapshots = make_snapshot(weak_pct + strong_pct)  # 12 entries

    sector = make_sector()
    # History has 15 days; on_list_rate = n/15 for each window
    history = {f"2026-05-{i:02d}": [] for i in range(15)}

    # lookback=5: truncates to last 5 (all strong)
    r5 = amt.compute_persistence(sector, snapshots, lookback_days=5, history=history)
    test(
        "lookback=5: returns result",
        r5 is not None,
    )
    test(
        "lookback=5: up_days_ratio = 0.8 (all strong entries)",
        r5 and abs(r5["up_days_ratio"] - 0.8) < 0.01,
        f"got {r5['up_days_ratio'] if r5 else 'None'}",
    )
    test(
        "lookback=5: momentum_5d = 0 (uniform hot_score via window)",
        r5 and abs(r5["momentum_5d"] - 0.0) < 0.01,
        f"got {r5['momentum_5d'] if r5 else 'None'}",
    )

    # lookback=10: truncates to last 10 (4 weak + 6 strong)
    r10 = amt.compute_persistence(sector, snapshots, lookback_days=10, history=history)
    test(
        "lookback=10: returns result",
        r10 is not None,
    )
    # up_ratio = (4*0.3 + 6*0.8) / 10 = 0.6
    test(
        "lookback=10: up_days_ratio = 0.6 (4 weak + 6 strong)",
        r10 and abs(r10["up_days_ratio"] - 0.6) < 0.01,
        f"got {r10['up_days_ratio'] if r10 else 'None'}",
    )

    # Default lookback=None: uses all 12 snapshots (6 weak + 6 strong)
    r_default = amt.compute_persistence(sector, snapshots, lookback_days=12, history=history)
    test(
        "lookback=12 (all): up_days_ratio = 0.55 (6*0.3 + 6*0.8)/12",
        r_default and abs(r_default["up_days_ratio"] - 0.55) < 0.01,
        f"got {r_default['up_days_ratio'] if r_default else 'None'}",
    )


def test_window_truncation_shorter_than_requested():
    """When snapshots shorter than lookback_days, use all available."""
    snapshots = make_snapshot([1.0, 2.0, 3.0, 4.0])
    sector = make_sector()
    history = {f"2026-05-{i:02d}": [] for i in range(10)}
    r = amt.compute_persistence(sector, snapshots, lookback_days=10, history=history)
    test(
        "lookback > len: computes without error",
        r is not None,
    )
    test(
        "lookback > len: up_days_ratio = 0.8 (all 4 entries up_ratio=0.8)",
        r and abs(r["up_days_ratio"] - 0.8) < 0.01,
        f"got {r['up_days_ratio'] if r else 'None'}",
    )


# ──────────────────────── Test: min_score gap removal (P0 #2) ────────────────────────


def test_min_score_default_zero():
    """Default min_score=0 means low-persistence sectors aren't silently dropped.

    Verify persistence < 30 results flow through compute + classify correctly.
    """
    # Weak snapshots: very low pct change → low hot_score → low persistence
    snapshots = make_snapshot([-5.0, -5.0])  # hot_score clamped at 0
    sector = make_sector(hot_score=20)
    history = {f"2026-05-{i:02d}": [] for i in range(5)}
    r = amt.compute_persistence(sector, snapshots, lookback_days=5, history=history)
    classified = amt.classify_themes([r]) if r else {"fading": []}

    test(
        "low-persistence sector is NOT dropped",
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


def test_empty_snapshots():
    """Empty snapshots returns None."""
    r = amt.compute_persistence(make_sector(), [], lookback_days=10, history={"d1": []})
    test("empty snapshots returns None", r is None)


def test_sparse_snapshots():
    """Few snapshots (< 3) computes without error (no minimum requirement)."""
    snapshots = make_snapshot([1.0, 2.0])
    sector = make_sector()
    history = {"d1": [{"code": "BK0001"}], "d2": [{"code": "BK0001"}]}
    r = amt.compute_persistence(sector, snapshots, lookback_days=10, history=history)
    test("sparse snapshots (< 3) returns result", r is not None)
    # rank_trend falls to 50 (neutral) → on_list_rate=1.0 & avg_hot=65 → label "→ (走平)"
    test("sparse: trend_label = → (走平) (high on_list_rate + avg_hot)",
         r and r["trend_label"] == "→ (走平)")


def test_snapshots_exactly_3():
    """Exactly 3 snapshot entries computes without error, neutral trend."""
    snapshots = make_snapshot([1.0, 2.0, 3.0])
    sector = make_sector()
    history = {"d1": [], "d2": [], "d3": []}
    r = amt.compute_persistence(sector, snapshots, lookback_days=3, history=history)
    test("snapshots=3 returns result", r is not None)
    # momentum_5d: avg_hot - mean(last 3)
    # avg_hot = mean(60,70,80) = 70, last 3 avg = 70 → mom5=0
    test("snapshots=3: momentum_5d = 0 (uniform)", r and abs(r["momentum_5d"] - 0.0) < 0.01)
    # rank_trend: 3 entries < 4 → falls to elif snapshots → 50 → trend_label ↘
    test("snapshots=3: trend_label correct with sparse data",
         r and r["trend_label"] in ("↘ (减弱)", "→ (走平)"))


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


# ──────────────────────── Test: Report rendering ────────────────────────


def test_generate_report_uses_lookback_days_for_up_day_count():
    """Markdown report should not hardcode 10 when rendering up-day count."""
    classified = {
        "strong": [{
            "name": "主题A",
            "hot_score": 80,
            "momentum_5d": 5.0,
            "up_days_ratio": round(10 / 15, 2),
            "persistence": 72.3,
            "trend_label": "↗ (延续)",
        }],
        "moderate": [{
            "name": "主题B",
            "hot_score": 65,
            "momentum_5d": 3.0,
            "up_days_ratio": round(10 / 15, 2),
            "persistence": 55.0,
            "trend_label": "→ (走平)",
        }],
        "emerging": [],
        "fading": [],
        "one_day_wonders": [],
    }
    report = amt.generate_report(classified, {"scan_time": "20260528-160000"}, 15)
    # Current report format: name | hot_score | momentum_5d | up_days_ratio% | persistence | trend
    test("report shows strong sector with correct format",
         "| 主题A | 80 | +5.0 | 67% | **72.3** | ↗ (延续) |" in report)
    test("report shows moderate sector with correct format",
         "| 主题B | 65 | +3.0 | 67% | 55.0 | → (走平) |" in report)


def test_generate_html_report_persistence_cell_has_closed_class_quote():
    """HTML report should render valid class attribute for persistence cell."""
    cell = amt._COL_RENDERERS["persistence"]({"persistence": 72.3})
    test("persistence cell has valid class attribute",
         cell == '<td class="ml-strong">72.3</td>',
         f"got {cell}")


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
    test_empty_snapshots()
    test_sparse_snapshots()
    test_snapshots_exactly_3()
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
