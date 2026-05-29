"""Tests for portfolio_manager.py"""
import sys
import json
import os
import tempfile
import yaml
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from portfolio.manager import (
    load_portfolio, save_portfolio, find_holding,
    code_to_ts_code, check_alerts,
    calc_kelly_position_pct, portfolio_kelly_analysis,
)

# Test result tracking
PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def test(name, condition, detail="", category="portfolio"):
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


def _make_test_portfolio():
    """Return a portfolio dict with one test holding."""
    return {
        "holdings": [
            {
                "code": "513180",
                "ts_code": "513180.SH",
                "name": "恒生科技ETF华夏",
                "buy_price": 1.025,
                "buy_date": "2026-04-15",
                "quantity": 2000,
                "stop_loss": 0.95,
                "targets": [1.15, 1.25],
                "notes": "test",
                "status": "active",
                "close_price": None,
                "close_date": None,
            }
        ],
        "settings": {"alert_threshold_pct": 3.0, "default_stop_loss_pct": 5.0},
    }


# ── Unit tests (direct import) ─────────────────────────────────


def test_find_holding():
    """Find active holding by code."""
    portfolio = _make_test_portfolio()
    h = find_holding(portfolio["holdings"], "513180")
    test("TPF-01: find active holding", h is not None and h["code"] == "513180")
    h2 = find_holding(portfolio["holdings"], "999999")
    test("TPF-01b: find nonexistent holding", h2 is None)


def test_find_holding_skips_closed():
    """Closed holdings should not be found by find_holding."""
    portfolio = _make_test_portfolio()
    portfolio["holdings"][0]["status"] = "closed"
    h = find_holding(portfolio["holdings"], "513180")
    test("TPF-02: skip closed holding", h is None)


def test_code_to_ts_code():
    """Code mapping to ts_code."""
    test("TPF-03: 513180 -> .SH", code_to_ts_code("513180") == "513180.SH")
    test("TPF-03b: 159915 -> .SZ", code_to_ts_code("159915") == "159915.SZ")
    test("TPF-03c: 588000 -> .SH", code_to_ts_code("588000") == "588000.SH")
    test("TPF-03d: already suffixed", code_to_ts_code("513180.SH") == "513180.SH")


def test_save_and_load():
    """Save then load roundtrip preserves data."""
    portfolio = _make_test_portfolio()
    orig_path = Path(tempfile.mktemp(suffix=".yaml"))
    try:
        # Monkey-patch the path
        from portfolio import manager as pm
        orig = pm.PORTFOLIO_PATH
        pm.PORTFOLIO_PATH = orig_path
        save_portfolio(portfolio)
        loaded = load_portfolio()
        test("TPF-04: save/load roundtrip",
             loaded["holdings"][0]["code"] == "513180" and
             loaded["holdings"][0]["buy_price"] == 1.025)
        pm.PORTFOLIO_PATH = orig
    finally:
        if orig_path.exists():
            orig_path.unlink()


def test_alert_stop_loss_approaching():
    """Alert when price approaches stop loss."""
    portfolio = _make_test_portfolio()
    from portfolio import manager as pm
    orig_fetch = pm.fetch_kline_with_price
    pm.fetch_kline_with_price = lambda _: (0.97, [{"close": 0.97}])  # close to 0.95 stop loss
    alerts = check_alerts(portfolio["holdings"], portfolio["settings"])
    pm.fetch_kline_with_price = orig_fetch
    test("TPF-05: stop loss approaching alert",
         any(a["type"] == "stop_loss_approaching" for a in alerts))


def test_alert_stop_loss_hit():
    """Critical alert when price breaks stop loss."""
    portfolio = _make_test_portfolio()
    from portfolio import manager as pm
    orig_fetch = pm.fetch_kline_with_price
    pm.fetch_kline_with_price = lambda _: (0.94, [{"close": 0.94}])  # below 0.95
    alerts = check_alerts(portfolio["holdings"], portfolio["settings"])
    pm.fetch_kline_with_price = orig_fetch
    test("TPF-06: stop loss hit alert",
         any(a["type"] == "stop_loss_hit" and a["severity"] == "critical" for a in alerts))


def test_alert_target_approaching():
    """Info alert when price approaches target."""
    portfolio = _make_test_portfolio()
    from portfolio import manager as pm
    orig_fetch = pm.fetch_kline_with_price
    pm.fetch_kline_with_price = lambda _: (1.13, [{"close": 1.13}])  # close to 1.15 target
    alerts = check_alerts(portfolio["holdings"], portfolio["settings"])
    pm.fetch_kline_with_price = orig_fetch
    test("TPF-07: target approaching alert",
         any(a["type"] == "target_approaching" for a in alerts))


def test_alert_target_hit():
    """Info alert when price reaches first target."""
    portfolio = _make_test_portfolio()
    from portfolio import manager as pm
    orig_fetch = pm.fetch_kline_with_price
    pm.fetch_kline_with_price = lambda _: (1.16, [{"close": 1.16}])  # above 1.15 target
    alerts = check_alerts(portfolio["holdings"], portfolio["settings"])
    pm.fetch_kline_with_price = orig_fetch
    test("TPF-08: target1 hit alert",
         any(a["type"] == "tp1_hit" for a in alerts))


def test_empty_portfolio_load():
    """Empty file should return empty holdings."""
    from portfolio import manager as pm
    orig_path = pm.PORTFOLIO_PATH
    tmp = Path(tempfile.mktemp(suffix=".yaml"))
    pm.PORTFOLIO_PATH = tmp
    p = load_portfolio()
    pm.PORTFOLIO_PATH = orig_path
    if tmp.exists():
        tmp.unlink()
    test("TPF-09: empty portfolio", p.get("holdings") == [])


def test_no_alerts_no_holdings():
    """No alerts when portfolio is empty."""
    alerts = check_alerts([], {})
    test("TPF-10: no alerts empty portfolio", len(alerts) == 0)


# ── Kelly unit tests ───────────────────────────────────────────


def test_kelly_default():
    """Baseline: 55% win, 1.5:1, half-Kelly base=12.5% → ~13%."""
    k = calc_kelly_position_pct(combined_score=65, volatility=0.15, regime_coef=1.0, trend_stage="mid")
    test("KLY-01: baseline kelly_pct ~13",
         12 <= k["kelly_pct"] <= 15,
         detail=f"got {k['kelly_pct']}")


def test_kelly_high_score_low_vol():
    """High score + low vol + early trend → ~20%."""
    k = calc_kelly_position_pct(combined_score=85, volatility=0.08, regime_coef=1.0, trend_stage="early")
    test("KLY-02: high score early trend kelly_pct >= 18",
         k["kelly_pct"] >= 18,
         detail=f"got {k['kelly_pct']}")


def test_kelly_low_score_bear():
    """Low score + bear regime → clamped at min 5%."""
    k = calc_kelly_position_pct(combined_score=45, volatility=0.25, regime_coef=0.4, trend_stage="decline")
    test("KLY-03: low score bear kelly_pct == 5",
         k["kelly_pct"] == 5,
         detail=f"got {k['kelly_pct']}")


def test_kelly_returns_all_keys():
    """calc_kelly_position_pct returns all expected keys."""
    k = calc_kelly_position_pct(50, 0.15, 1.0, "mid")
    expected = {"kelly_pct", "kelly_range", "base_kelly", "score_mult", "vol_mult", "regime_coef", "trend_mult"}
    test("KLY-04: all keys present", expected.issubset(k.keys()))


def test_portfolio_kelly_empty():
    """Empty portfolio → summary no_data."""
    result = portfolio_kelly_analysis([], [], 0, 1.0)
    test("KLY-05: empty portfolio", result.get("summary") == "no_data")


def test_portfolio_kelly_with_holdings():
    """Mock holdings get per-holding kelly analysis."""
    holdings = [
        {"code": "513180", "name": "恒生科技", "current_price": 1.05, "quantity": 2000,
         "pnl_pct": 5.0, "status": "active"},
        {"code": "518880", "name": "黄金ETF", "current_price": 5.5, "quantity": 500,
         "pnl_pct": 8.0, "status": "active"},
    ]
    scan = [
        {"code": "513180", "scan_score": 75, "combined_score": 80, "score_direction": "up"},
        {"code": "518880", "scan_score": 60, "combined_score": 62, "score_direction": "up"},
    ]
    total_value = 1.05 * 2000 + 5.5 * 500  # 2100 + 2750 = 4850
    result = portfolio_kelly_analysis(holdings, scan, total_value, 1.0)
    holdings_out = result.get("holdings", [])
    test("KLY-06: kelly analysis returns holdings", len(holdings_out) == 2)
    test("KLY-07: each holding has action", all(h.get("action") in ("reduce", "hold", "increase") for h in holdings_out))
    test("KLY-08: total_optimal_pct > 0", result.get("total_optimal_pct", 0) > 0)
    test("KLY-09: cash_reserve_pct >= 0", result.get("cash_reserve_pct", -1) >= 0)


# ── Integration tests (subprocess) ─────────────────────────────


def run_script(script_name, *args, timeout=30, portfolio_path=None):
    """Run a script, return (rc, stdout, stderr)."""
    import subprocess
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    env = os.environ.copy()
    if portfolio_path:
        env["STOCK_TREND_PORTFOLIO"] = str(portfolio_path)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return result.returncode, result.stdout, result.stderr


def _make_tmp_portfolio():
    """Create a temp portfolio path and return it."""
    return Path(tempfile.mktemp(suffix=".yaml"))


def test_integration_add():
    """Add then list should show the holding."""
    tmp = _make_tmp_portfolio()
    try:
        rc, out, err = run_script("portfolio/manager.py", "add",
                                   "--code", "513180",
                                   "--name", "恒生科技ETF华夏",
                                   "--price", "1.025",
                                   "--date", "2026-04-15",
                                   "--qty", "2000",
                                   "--stop-loss", "0.95",
                                   "--targets", "1.15,1.25",
                                   portfolio_path=tmp)
        add_result = json.loads(out)
        ok = add_result.get("status") == "ok"

        rc2, out2, err2 = run_script("portfolio/manager.py", "list", portfolio_path=tmp)
        list_result = json.loads(out2)
        has_holding = len(list_result.get("holdings", [])) > 0

        test("TPF-I01: add + list integration", ok and has_holding, detail=f"add={ok} list_holdings={has_holding}")
    finally:
        if tmp.exists():
            tmp.unlink()


def test_integration_remove():
    """Remove marks holding as closed."""
    tmp = _make_tmp_portfolio()
    try:
        run_script("portfolio/manager.py", "add",
                   "--code", "513180", "--price", "1.025",
                   "--date", "2026-04-15", "--qty", "2000",
                   portfolio_path=tmp)
        rc, out, err = run_script("portfolio/manager.py", "remove", "--code", "513180", portfolio_path=tmp)
        rm_result = json.loads(out)
        ok = rm_result.get("status") == "ok" and rm_result.get("holding", {}).get("status") == "closed"
        test("TPF-I02: remove integration", ok)
    finally:
        if tmp.exists():
            tmp.unlink()


def test_integration_update():
    """Update changes stop-loss and targets."""
    tmp = _make_tmp_portfolio()
    try:
        run_script("portfolio/manager.py", "add",
                   "--code", "513180", "--price", "1.025",
                   "--date", "2026-04-15", "--qty", "2000",
                   portfolio_path=tmp)
        rc, out, err = run_script("portfolio/manager.py", "update",
                                   "--code", "513180",
                                   "--stop-loss", "0.93",
                                   "--targets", "1.12,1.22",
                                   portfolio_path=tmp)
        up_result = json.loads(out)
        h = up_result.get("holding", {})
        ok = up_result.get("status") == "ok" and h.get("stop_loss") == 0.93 and h.get("targets") == [1.12, 1.22]
        test("TPF-I03: update integration", ok)
    finally:
        if tmp.exists():
            tmp.unlink()


# ── Runner ─────────────────────────────────────────────────────


def run_portfolio_tests():
    """Run all portfolio manager tests."""
    print("\n📁 持仓管理测试 (Portfolio)")
    print("=" * 50)

    test_find_holding()
    test_find_holding_skips_closed()
    test_code_to_ts_code()
    test_save_and_load()
    test_alert_stop_loss_approaching()
    test_alert_stop_loss_hit()
    test_alert_target_approaching()
    test_alert_target_hit()
    test_empty_portfolio_load()
    test_no_alerts_no_holdings()
    test_integration_add()
    test_integration_remove()
    test_integration_update()
    # Kelly tests
    test_kelly_default()
    test_kelly_high_score_low_vol()
    test_kelly_low_score_bear()
    test_kelly_returns_all_keys()
    test_portfolio_kelly_empty()
    test_portfolio_kelly_with_holdings()

    print(f"\nPortfolio 结果: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return PASSED, FAILED


if __name__ == "__main__":
    run_portfolio_tests()
    if FAILED > 0:
        sys.exit(1)
