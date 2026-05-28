#!/usr/bin/env python3
"""Regression tests for golden diff stability and EastMoney fallback."""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_kline_eastmoney as fke
import test_golden as tg

PASSED = 0
FAILED = 0


def test(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def base_config():
    return {
        "thresholds": {
            "score": 0.01,
            "price": 0.0001,
            "default": 0.001,
        },
        "numeric_threshold_map": {
            "total_score": "score",
            "confidence": "score",
            "close": "price",
            "open": "price",
            "high": "price",
            "low": "price",
            "volume": "default",
            "amount": "default",
        },
    }


def test_fetch_eastmoney_supports_no_proxy_fallback():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def __init__(self, payload):
            self.payload = payload

        def open(self, req, timeout=15):
            return FakeResponse(self.payload)

    payload = json.dumps({
        "rc": 0,
        "data": {
            "name": "测试ETF",
            "code": "513180",
            "klines": [
                "2026-05-27,1.00,1.01,1.02,0.99,100,1000,0,1.00,0.01,0.10",
            ],
        },
    }).encode("utf-8")

    old_urlopen = fke.urllib.request.urlopen
    old_build_opener = fke.urllib.request.build_opener
    try:
        def fail_urlopen(req, timeout=15):
            raise RuntimeError("proxy blocked")

        def fake_build_opener(*handlers):
            return FakeOpener(payload)

        fke.urllib.request.urlopen = fail_urlopen
        fke.urllib.request.build_opener = fake_build_opener

        records, name = fke.fetch_eastmoney("1.513180", "D", lmt=1)
        test("fetch_eastmoney falls back to no-proxy opener", len(records) == 1 and name == "测试ETF")
    except Exception as exc:
        test("fetch_eastmoney falls back to no-proxy opener", False, str(exc))
    finally:
        fke.urllib.request.urlopen = old_urlopen
        fke.urllib.request.build_opener = old_build_opener


def test_diff_output_ignores_volatile_error_fields():
    golden = {
        "meta": {"data_source": "akshare", "fetch_time": "20260527-232904"},
        "summary": {"data_quality": "good", "pmi": 50.3},
        "data": {"pmi": 50.3},
        "errors": ["hs300: old error text"],
    }
    current = {
        "meta": {"data_source": "akshare", "fetch_time": "20260528-141756"},
        "summary": {"data_quality": "good", "pmi": 50.3},
        "data": {"pmi": 50.3},
        "errors": ["hs300: new error text"],
    }
    diffs = tg.diff_output("macro_snapshot.json", golden, current, base_config())
    test("macro diff ignores fetch_time and error string churn", len(diffs) == 0, str(diffs[:2]))


def test_diff_output_aligns_kline_and_normalizes_volume_units():
    golden = {
        "meta": {"data_source": "baostock", "record_count": 2},
        "data": [
            {"trade_date": "20250527", "open": 1495.71, "close": 1488.87, "high": 1503.04, "low": 1488.49, "pct_chg": -0.4257, "vol": 1775213.0, "amount": 2748945675.47},
            {"trade_date": "20250528", "open": 1488.95, "close": 1482.21, "high": 1491.33, "low": 1478.35, "pct_chg": -0.4469, "vol": 1626568.0, "amount": 2504624605.12},
        ],
    }
    current = {
        "meta": {"data_source": "eastmoney", "record_count": 3, "em_host": "push2his.eastmoney.com"},
        "data": [
            {"trade_date": "20250526", "open": 1501.00, "close": 1498.94, "high": 1507.50, "low": 1498.00, "pct_chg": 0.0, "vol": 18888.0, "amount": 2800000000.0, "change": 0.0, "turnover_rate": 0.1},
            {"trade_date": "20250527", "open": 1499.44, "close": 1492.34, "high": 1507.04, "low": 1491.95, "pct_chg": -0.44, "vol": 17752.0, "amount": 2748945675.0, "change": -6.6, "turnover_rate": 0.14},
            {"trade_date": "20250528", "open": 1492.43, "close": 1485.44, "high": 1494.90, "low": 1481.44, "pct_chg": -0.46, "vol": 16266.0, "amount": 2504624605.0, "change": -6.9, "turnover_rate": 0.13},
        ],
    }
    diffs = tg.diff_output("kline.json", golden, current, base_config())
    test(
        "kline diff aligns by trade_date and tolerates source drift",
        all(d["severity"] != "fail" for d in diffs),
        str(diffs[:2]),
    )


def test_diff_output_scores_uses_stable_semantics():
    golden = {
        "composite_score": 0.097,
        "direction": "震荡偏多",
        "direction_detail": "震荡",
        "confidence": "低",
        "report_params": {"position_tier": 2},
    }
    current = {
        "composite_score": -0.099,
        "direction": "震荡偏空",
        "direction_detail": "震荡",
        "confidence": "低",
        "report_params": {"position_tier": 3},
    }
    diffs = tg.diff_output("scores.json", golden, current, base_config())
    test("scores diff focuses on stable direction semantics", len(diffs) == 0, str(diffs[:2]))


def main():
    print("=" * 60)
    print("Golden Regression Tests")
    print("=" * 60)
    test_fetch_eastmoney_supports_no_proxy_fallback()
    test_diff_output_ignores_volatile_error_fields()
    test_diff_output_aligns_kline_and_normalizes_volume_units()
    test_diff_output_scores_uses_stable_semantics()
    total = PASSED + FAILED
    print(f"\n{'=' * 60}")
    print(f"Results: {PASSED}/{total} passed, {FAILED} failed")
    print(f"{'=' * 60}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
