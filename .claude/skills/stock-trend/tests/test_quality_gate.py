#!/usr/bin/env python3
"""Quality gate tests for signal consistency checks."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

PASSED = 0
FAILED = 0
RESULTS = []


def test(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def test_signal_consistency_detects_conflict():
    """Direction=bullish but indicators all bearish -> flagged as conflict."""
    from analysis.quality_gate import check_signal_consistency

    risks = [
        "空头排列",
        "MA5下穿MA10死叉",
        "MACD死叉；绿柱放大",
        "RSI=70.1，超买",
    ]
    result = check_signal_consistency(direction="震荡偏多", risks=risks)
    test("signal_conflict_detected",
         result["has_conflict"] is True,
         f"conflict={result['has_conflict']}, bearish_count={result['bearish_signal_count']}")
    test("signal_conflict_penalty_applied",
         result["penalty"] >= 0.10,
         f"penalty={result['penalty']}")


def test_signal_consistency_no_conflict():
    """Direction=bullish with supporting indicators -> no conflict."""
    from analysis.quality_gate import check_signal_consistency

    risks = ["RSI=55.0，中性区间"]
    result = check_signal_consistency(direction="震荡偏多", risks=risks)
    test("signal_no_conflict",
         result["has_conflict"] is False,
         f"conflict={result['has_conflict']}")
    test("signal_no_penalty",
         result["penalty"] == 0,
         f"penalty={result['penalty']}")


def test_signal_consistency_bearish_direction():
    """Direction=bearish with bearish indicators -> no conflict (consistent)."""
    from analysis.quality_gate import check_signal_consistency

    risks = ["空头排列", "MACD死叉；绿柱放大"]
    result = check_signal_consistency(direction="震荡偏空", risks=risks)
    test("bearish_consistent_no_conflict",
         result["has_conflict"] is False,
         f"conflict={result['has_conflict']}")


def test_overbought_with_bearish():
    """Overbought + bearish signal in bullish direction -> conflict."""
    from analysis.quality_gate import check_signal_consistency

    risks = ["RSI=75.0，超买", "OBV在20日均线下方，资金净流出"]
    result = check_signal_consistency(direction="震荡偏多", risks=risks)
    test("overbought_bearish_conflict",
         result["has_conflict"] is True,
         f"conflict={result['has_conflict']}, penalty={result['penalty']}")


def test_bearish_direction_with_bullish_signals():
    """Bearish direction with many bullish signals -> conflict."""
    from analysis.quality_gate import check_signal_consistency

    risks = ["多头排列", "MACD金叉", "红柱放大", "放量上涨"]
    result = check_signal_consistency(direction="偏空", risks=risks)
    test("bearish_dir_bullish_signals_conflict",
         result["has_conflict"] is True,
         f"conflict={result['has_conflict']}, bullish_count={result['bullish_signal_count']}")
    test("bearish_dir_bullish_signals_penalty",
         result["penalty"] >= 0.15,
         f"penalty={result['penalty']}")


if __name__ == "__main__":
    print("=== Quality Gate Tests ===")
    test_signal_consistency_detects_conflict()
    test_signal_consistency_no_conflict()
    test_signal_consistency_bearish_direction()
    test_overbought_with_bearish()
    test_bearish_direction_with_bullish_signals()
    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED > 0 else 0)
