#!/usr/bin/env python3
"""Portfolio manager — track ETF holdings with P&L, alerts, scan comparison.

Usage:
    python3 portfolio_manager.py list                    # all holdings + P&L
    python3 portfolio_manager.py add --code <code> --price <buy_price> --date <buy_date> --qty <quantity>
    python3 portfolio_manager.py remove --code <code> [--close-price <price>] [--close-date <date>]
    python3 portfolio_manager.py update --code <code> [--stop-loss <price>] [--targets <p1,p2>]
    python3 portfolio_manager.py status                  # P&L + alerts + scan-compare
    python3 portfolio_manager.py alerts                  # alerts only

Outputs JSON to stdout.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import math
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional
from resolve_code import code_to_ts_code

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
DATA_DIR = SCRIPT_DIR.parent / "data"
PORTFOLIO_PATH = Path(os.environ.get("STOCK_TREND_PORTFOLIO", str(DATA_DIR / "portfolio.yaml")))
EXAMPLE_PATH = DATA_DIR / "portfolio.example.yaml"


# ── Data helpers ──────────────────────────────────────────────────────────


def load_portfolio() -> dict:
    """Load portfolio YAML, return empty dict if missing."""
    if not PORTFOLIO_PATH.exists():
        return {"holdings": [], "settings": {"alert_threshold_pct": 3.0, "default_stop_loss_pct": 5.0}}
    with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"holdings": [], "settings": {}}


def save_portfolio(portfolio: dict):
    """Write portfolio dict to YAML."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        yaml.dump(portfolio, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def find_holding(holdings: list, code: str) -> Optional[dict]:
    """Find active holding by code."""
    for h in holdings:
        if h.get("code") == code and h.get("status") == "active":
            return h
    return None


# ── Fetch helpers ─────────────────────────────────────────────────────


def fetch_current_price(ts_code: str) -> Optional[float]:
    """Get latest close price via fetch_kline_eastmoney.py subprocess."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            out_path = f.name
        rc, stdout, stderr = _run_script(
            "fetch_kline_eastmoney.py", ts_code, "-o", out_path, timeout=20
        )
        if rc != 0:
            return None
        with open(out_path, "r") as f:
            raw = json.load(f)
        os.unlink(out_path)
        # fetch_kline_eastmoney.py -o wraps in {"meta":..., "data":[...]}
        records = raw if isinstance(raw, list) else raw.get("data", [])
        if records:
            return float(records[-1].get("close", 0))
        return None
    except Exception:
        return None


def _run_script(script_name: str, *args, timeout: int = 20):
    """Run a script from SCRIPT_DIR, return (rc, stdout, stderr)."""
    script_path = SCRIPT_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# ── P&L calculation ────────────────────────────────────────────────────────


def calc_pnl(holding: dict, current_price: float) -> dict:
    """Calculate P&L for a holding given current price."""
    buy_price = float(holding.get("buy_price", 0))
    qty = float(holding.get("quantity", 0))
    cost = buy_price * qty
    value = current_price * qty
    pnl_amount = round(value - cost, 2)
    pnl_pct = round((current_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0.0
    return {"cost": round(cost, 2), "market_value": round(value, 2), "pnl_amount": pnl_pct, "pnl_pct": pnl_pct}


# ── Alert logic ────────────────────────────────────────────────────────────


def check_alerts(holdings: list[dict], settings: dict) -> list[dict]:
    """Check all active holdings for stop-loss / target proximity alerts."""
    alerts: list[dict] = []
    threshold = float(settings.get("alert_threshold_pct", 3.0))
    for h in holdings:
        if h.get("status") != "active":
            continue
        code = h["code"]
        current = fetch_current_price(code_to_ts_code(code))
        if current is None:
            continue
        sl = h.get("stop_loss")
        if sl:
            sl = float(sl)
            dist_pct = (current - sl) / sl * 100
            if current <= sl:
                alerts.append({"code": code, "name": h.get("name", ""), "type": "stop_loss_hit",
                               "detail": f"现价{current:.4f}已跌破止损{sl:.4f}", "severity": "critical"})
            elif dist_pct < threshold:
                alerts.append({"code": code, "name": h.get("name", ""), "type": "stop_loss_approaching",
                               "detail": f"现价{current:.4f}距止损{sl:.4f}仅{dist_pct:.1f}%", "severity": "warning"})
        targets = h.get("targets", [])
        if targets:
            t1 = float(targets[0]) if isinstance(targets[0], (int, float)) else float(targets[0])
            dist_to_target = (t1 - current) / current * 100
            if dist_to_target < threshold and dist_to_target > 0:
                alerts.append({"code": code, "name": h.get("name", ""), "type": "target_approaching",
                               "detail": f"现价{current:.4f}距目标{t1:.4f}仅{dist_to_target:.1f}%", "severity": "info"})
            elif current >= t1:
                alerts.append({"code": code, "name": h.get("name", ""), "type": "target_hit",
                               "detail": f"现价{current:.4f}已达目标{t1:.4f}", "severity": "info"})
    return alerts


# ── Commands ────────────────────────────────────────────────────────────────


def cmd_list(args):
    """List all holdings with current P&L."""
    portfolio = load_portfolio()
    holdings = portfolio.get("holdings", [])
    settings = portfolio.get("settings", {})
    active = [h for h in holdings if h.get("status") == "active"]
    closed = [h for h in holdings if h.get("status") == "closed"]

    # Enrich active holdings with current price
    enriched = []
    total_cost = 0.0
    total_value = 0.0
    for h in active:
        current = fetch_current_price(code_to_ts_code(h["code"]))
        if current is not None:
            cost = float(h["buy_price"]) * float(h["quantity"])
            value = current * float(h["quantity"])
            total_cost += cost
            total_value += value
            enriched.append({
                "code": h["code"],
                "name": h.get("name", ""),
                "buy_price": float(h["buy_price"]),
                "current_price": current,
                "pnl_pct": round((current - float(h["buy_price"])) / float(h["buy_price"]) * 100, 2),
                "pnl_amount": round(value - cost, 2),
                "buy_date": h.get("buy_date", ""),
                "hold_days": (date.today() - date.fromisoformat(h["buy_date"])).days if h.get("buy_date") else 0,
                "stop_loss": float(h["stop_loss"]) if h.get("stop_loss") else None,
                "targets": [float(t) for t in h.get("targets", [])] if h.get("targets") else [],
                "quantity": float(h["quantity"]),
                "status": "active",
                "notes": h.get("notes", ""),
            })
        else:
            enriched.append({
                "code": h["code"],
                "name": h.get("name", ""),
                "buy_price": float(h["buy_price"]),
                "current_price": None,
                "pnl_pct": None,
                "pnl_amount": None,
                "buy_date": h.get("buy_date", ""),
                "hold_days": (date.today() - date.fromisoformat(h["buy_date"])).days if h.get("buy_date") else 0,
                "stop_loss": h.get("stop_loss"),
                "targets": h.get("targets", []),
                "quantity": float(h["quantity"]),
                "status": "active",
                "notes": h.get("notes", ""),
            })

    result = {
        "meta": {"command": "list", "timestamp": datetime.now().isoformat()},
        "holdings": enriched,
        "summary": {
            "total_active": len(active),
            "total_closed": len(closed),
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0.0,
            "total_pnl_amount": round(total_value - total_cost, 2),
        }
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_add(args):
    """Add a new holding."""
    portfolio = load_portfolio()
    # Resolve code to ts_code
    ts_code = code_to_ts_code(args.code)
    code = ts_code.split(".")[0]

    # Check duplicate active
    if find_holding(portfolio.get("holdings", []), code):
        result = {"error": f"持仓 {code} 已存在", "code": code}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    holding = {
        "code": code,
        "ts_code": ts_code,
        "name": args.name or "",
        "buy_price": float(args.price),
        "buy_date": args.date,
        "quantity": float(args.qty),
        "stop_loss": float(args.stop_loss) if args.stop_loss else None,
        "targets": [float(t) for t in args.targets.split(",")] if args.targets else [],
        "notes": args.notes or "",
        "status": "active",
        "close_price": None,
        "close_date": None,
    }
    if "holdings" not in portfolio:
        portfolio["holdings"] = []
    portfolio["holdings"].append(holding)
    save_portfolio(portfolio)

    result = {"status": "ok", "message": f"已添加 {name_or_fallback(holding)}", "holding": holding}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def name_or_fallback(holding: dict) -> str:
    return f"{holding.get('name', '')}({holding['code']})".strip()


def cmd_remove(args):
    """Mark a holding as closed."""
    portfolio = load_portfolio()
    code = args.code
    holding = find_holding(portfolio.get("holdings", []), code)
    if not holding:
        result = {"error": f"未找到活跃持仓 {code}"}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    holding["status"] = "closed"
    holding["close_price"] = float(args.close_price) if args.close_price else None
    holding["close_date"] = args.close_date or date.today().isoformat()
    save_portfolio(portfolio)

    result = {"status": "ok", "message": f"已平仓 {name_or_fallback(holding)}", "holding": holding}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_update(args):
    """Update stop-loss or targets for a holding."""
    portfolio = load_portfolio()
    code = args.code
    holding = find_holding(portfolio.get("holdings", []), code)
    if not holding:
        result = {"error": f"未找到活跃持仓 {code}"}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    if args.stop_loss:
        holding["stop_loss"] = float(args.stop_loss)
    if args.targets:
        holding["targets"] = [float(t) for t in args.targets.split(",")]
    if args.notes is not None:
        holding["notes"] = args.notes
    if args.name:
        holding["name"] = args.name
    save_portfolio(portfolio)

    result = {"status": "ok", "message": f"已更新 {name_or_fallback(holding)}", "holding": holding}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_alerts(args):
    """Output alerts for all active holdings."""
    portfolio = load_portfolio()
    alerts = check_alerts(portfolio.get("holdings", []), portfolio.get("settings", {}))
    result = {"meta": {"command": "alerts", "timestamp": datetime.now().isoformat()}, "alerts": alerts}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_status(args):
    """Full status: holdings + P&L + alerts + scan comparison."""
    portfolio = load_portfolio()
    holdings = portfolio.get("holdings", [])
    settings = portfolio.get("settings", {})
    active = [h for h in holdings if h.get("status") == "active"]

    # P&L for active holdings
    enriched = []
    total_cost = 0.0
    total_value = 0.0
    for h in active:
        current = fetch_current_price(code_to_ts_code(h["code"]))
        if current is not None:
            cost = float(h["buy_price"]) * float(h["quantity"])
            value = current * float(h["quantity"])
            total_cost += cost
            total_value += value
            enriched.append({
                "code": h["code"], "name": h.get("name", ""),
                "buy_price": float(h["buy_price"]), "current_price": current,
                "pnl_pct": round((current - float(h["buy_price"])) / float(h["buy_price"]) * 100, 2),
                "pnl_amount": round(value - cost, 2),
                "buy_date": h.get("buy_date", ""),
                "hold_days": (date.today() - date.fromisoformat(h["buy_date"])).days if h.get("buy_date") else 0,
                "stop_loss": float(h["stop_loss"]) if h.get("stop_loss") else None,
                "targets": [float(t) for t in h["targets"]] if h.get("targets") else [],
                "quantity": float(h["quantity"]), "status": "active",
            })
        else:
            enriched.append({
                "code": h["code"], "name": h.get("name", ""),
                "buy_price": float(h["buy_price"]), "current_price": None,
                "pnl_pct": None, "pnl_amount": None, "buy_date": h.get("buy_date", ""),
                "hold_days": (date.today() - date.fromisoformat(h["buy_date"])).days if h.get("buy_date") else 0,
                "stop_loss": h.get("stop_loss"), "targets": h.get("targets", []),
                "quantity": float(h["quantity"]), "status": "active",
            })

    # Alerts
    alerts = check_alerts(holdings, settings)

    # Scan comparison — run etf_scanner quick phase only
    scan_compare = []
    if getattr(args, 'skip_scan', False):
        scan_compare = [{"note": "已跳过 scan 对比 (--skip-scan)"}]
    else:
        try:
            rc, stdout, stderr = _run_script(
                "etf_scanner.py", "--output", "compact", timeout=120
            )
            if rc == 0:
                scan_data = json.loads(stdout)
                # Extract market regime from scanner meta
                scan_meta = scan_data.get("meta", {})
                regime_info = {
                    "regime": scan_meta.get("market_regime", "unknown"),
                    "coefficient": scan_meta.get("regime_coefficient", 1.0),
                }
                # Add regime warning alert
                if regime_info["regime"] == "bear":
                    alerts.append({
                        "code": "__market__", "name": "大盘",
                        "type": "regime_warning",
                        "detail": f"市场处于熊市(系数{regime_info['coefficient']})，建议减仓至40%以下",
                        "severity": "warning",
                    })
                rankings = scan_data.get("combined_ranking", [])
                ranking_map = {r["code"]: r for r in rankings}
                for h in active:
                    code = h["code"]
                    if code in ranking_map:
                        r = ranking_map[code]
                        rank = r.get("rank", 999)
                        score = r.get("quick_score", 0)
                        is_top_pick = rank <= int(scan_data.get("meta", {}).get("top_n", 10))
                        score_direction = r.get("score_direction", "")
                        if is_top_pick and score >= 70:
                            rec = "持仓仍在强势区，可继续持有"
                        elif score >= 55:
                            rec = "评分中等，关注变化"
                        else:
                            rec = "排名靠后/评分偏低，考虑减仓"
                        scan_compare.append({
                            "code": code, "name": h.get("name", ""),
                            "scan_rank": rank, "scan_score": score,
                            "score_direction": score_direction,
                            "is_top_pick": is_top_pick,
                            "recommendation": rec,
                        })
        except Exception:
            scan_compare = [{"note": "etf-scan 执行失败，跳过排名对比"}]

    closed = [h for h in holdings if h.get("status") == "closed"]
    result = {
        "meta": {
            "command": "status",
            "timestamp": datetime.now().isoformat(),
            "market_regime": regime_info["regime"],
            "regime_coefficient": regime_info["coefficient"],
        },
        "holdings": enriched,
        "summary": {
            "total_active": len(active),
            "total_closed": len(closed),
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0.0,
            "total_pnl_amount": round(total_value - total_cost, 2),
        },
        "alerts": alerts,
        "scan_compare": scan_compare,
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# ── CLI entry ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="ETF 持仓管理")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="列出全部持仓（含浮动盈亏）")

    # add
    p_add = sub.add_parser("add", help="新增持仓")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--name", default="")
    p_add.add_argument("--price", required=True, help="买入价")
    p_add.add_argument("--date", required=True, help="买入日期 YYYY-MM-DD")
    p_add.add_argument("--qty", required=True, help="持仓数量")
    p_add.add_argument("--stop-loss", help="止损价")
    p_add.add_argument("--targets", help="目标价，逗号分隔")
    p_add.add_argument("--notes", default="")

    # remove
    p_rm = sub.add_parser("remove", help="平仓")
    p_rm.add_argument("--code", required=True)
    p_rm.add_argument("--close-price")
    p_rm.add_argument("--close-date")

    # update
    p_up = sub.add_parser("update", help="更新止损/目标")
    p_up.add_argument("--code", required=True)
    p_up.add_argument("--stop-loss")
    p_up.add_argument("--targets")
    p_up.add_argument("--notes", nargs="?")
    p_up.add_argument("--name")

    # alerts
    sub.add_parser("alerts", help="仅输出预警信息")

    # status
    p_st = sub.add_parser("status", help="持仓总览 + 预警 + etf-scan 对比")
    p_st.add_argument("--skip-scan", action="store_true", help="跳过 etf-scan 排名对比（省时）")

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "add": cmd_add,
        "remove": cmd_remove,
        "update": cmd_update,
        "alerts": cmd_alerts,
        "status": cmd_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
