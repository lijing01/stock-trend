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
    kline = _fetch_kline(ts_code)
    if kline:
        return float(kline[-1].get("close", 0))
    return None


def fetch_kline_with_price(ts_code: str) -> tuple[Optional[float], Optional[list]]:
    """Fetch kline data and return (current_price, kline_list)."""
    kline = _fetch_kline(ts_code)
    if kline:
        return float(kline[-1].get("close", 0)), kline
    return None, None


def _fetch_kline(ts_code: str) -> Optional[list]:
    """Fetch raw kline data from eastmoney."""
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
        records = raw if isinstance(raw, list) else raw.get("data", [])
        return records if records else None
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
    """Check all active holdings for stop-loss / target proximity / time-stop / trailing-stop alerts.

    P2 #5a: 移动止损 advisory (price up 5% → stop to MA20, up 10% → trailing 8%)
    P2 #5b: 分批止盈 tp1/tp2 separate alerts with partial/full sell advice
    P2 #5c: 时间止损 (hold > 90d + loss > 5%, hold > 30d + profit < 2%)
    """
    alerts: list[dict] = []
    threshold = float(settings.get("alert_threshold_pct", 3.0))
    today = date.today()
    for h in holdings:
        if h.get("status") != "active":
            continue
        code = h["code"]
        ts_code = code_to_ts_code(code)
        current, kline = fetch_kline_with_price(ts_code)
        if current is None:
            continue
        buy_price = float(h.get("buy_price", 0))
        pnl_pct = round((current - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0.0
        buy_date_str = h.get("buy_date", "")
        hold_days = (today - date.fromisoformat(buy_date_str)).days if buy_date_str else 0

        # ── 5c: 时间止损 ──────────────────────────────────────────────
        if hold_days > 90 and pnl_pct < -5:
            alerts.append({
                "code": code, "name": h.get("name", ""),
                "type": "time_stop",
                "detail": f"持仓{hold_days}天浮亏{pnl_pct:.1f}%，时间成本过高，建议止损",
                "severity": "warning",
            })
        elif hold_days > 30 and pnl_pct < 2:
            alerts.append({
                "code": code, "name": h.get("name", ""),
                "type": "low_efficiency",
                "detail": f"持仓{hold_days}天仅浮盈{pnl_pct:.1f}%，资金效率低下，关注机会成本",
                "severity": "info",
            })

        # ── Static stop-loss ────────────────────────────────────────────
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

        # ── 5a: 移动止损 advisory (requires kline for MA20) ──────────
        if kline and len(kline) >= 20 and buy_price > 0:
            closes = [r["close"] for r in kline]
            ma20 = sum(closes[-20:]) / 20
            # Price up 5% from cost: suggest trailing stop to MA20
            if current >= buy_price * 1.05:
                trailing_ma = ma20
                if current < trailing_ma:
                    # Price already below trailing stop level → alert
                    alerts.append({
                        "code": code, "name": h.get("name", ""),
                        "type": "trailing_stop_hit",
                        "detail": f"盈利{pnl_pct:.1f}%但已跌破MA20{trailing_ma:.4f}，建议止盈",
                        "severity": "warning",
                    })
                else:
                    alerts.append({
                        "code": code, "name": h.get("name", ""),
                        "type": "trailing_stop_ready",
                        "detail": f"盈利{pnl_pct:.1f}%，建议将止损上移至MA20({trailing_ma:.4f})",
                        "severity": "info",
                    })
            # Price up 10% from cost: suggest trailing 8% from high
            if current >= buy_price * 1.10:
                high_since_buy = max(r.get("high", r["close"]) for r in kline)
                trail_price = high_since_buy * 0.92
                if current < trail_price:
                    alerts.append({
                        "code": code, "name": h.get("name", ""),
                        "type": "profit_trailing_hit",
                        "detail": f"最高回撤已超8%，建议止盈锁定利润",
                        "severity": "warning",
                    })
                else:
                    alerts.append({
                        "code": code, "name": h.get("name", ""),
                        "type": "profit_trailing_ready",
                        "detail": f"盈利{pnl_pct:.1f}%超过10%，建议设置回撤8%止盈({trail_price:.4f})",
                        "severity": "info",
                    })

        # ── 5b: 分批止盈 tp1/tp2 ─────────────────────────────────────
        targets = h.get("targets", [])
        if targets:
            t1 = float(targets[0]) if isinstance(targets[0], (int, float)) else float(targets[0])
            dist_to_t1 = (t1 - current) / current * 100
            if current >= t1:
                alerts.append({
                    "code": code, "name": h.get("name", ""),
                    "type": "tp1_hit",
                    "detail": f"已到达目标1({t1:.4f})，建议卖出1/3仓位锁定利润",
                    "severity": "info",
                })
                # Check tp2
                if len(targets) >= 2:
                    t2 = float(targets[1]) if isinstance(targets[1], (int, float)) else float(targets[1])
                    if current >= t2:
                        alerts.append({
                            "code": code, "name": h.get("name", ""),
                            "type": "tp2_hit",
                            "detail": f"已到达目标2({t2:.4f})，建议全部卖出或移动止损持有",
                            "severity": "success" if hasattr(str, "success") else "info",
                        })
                    else:
                        dist_to_t2 = (t2 - current) / current * 100
                        if dist_to_t2 < threshold:
                            alerts.append({
                                "code": code, "name": h.get("name", ""),
                                "type": "tp2_approaching",
                                "detail": f"距目标2({t2:.4f})仅{dist_to_t2:.1f}%，剩余仓位可继续持有",
                                "severity": "info",
                            })
            elif dist_to_t1 < threshold and dist_to_t1 > 0:
                alerts.append({"code": code, "name": h.get("name", ""), "type": "target_approaching",
                               "detail": f"现价{current:.4f}距目标{t1:.4f}仅{dist_to_t1:.1f}%", "severity": "info"})
    return alerts


# ── P3 #12: Portfolio-level risk control ──────────────────────────────────


def portfolio_drawdown_alert(total_cost: float, total_value: float) -> list[dict]:
    """Check portfolio-level drawdown > 10%."""
    if total_cost <= 0:
        return []
    dd_pct = (total_value - total_cost) / total_cost * 100
    if dd_pct < -15:
        return [{
            "code": "__portfolio__", "name": "组合",
            "type": "portfolio_drawdown_critical",
            "detail": f"组合整体回撤{dd_pct:.1f}%，超过15%警戒线，建议强制减仓至50%以下",
            "severity": "critical",
        }]
    if dd_pct < -10:
        return [{
            "code": "__portfolio__", "name": "组合",
            "type": "portfolio_drawdown_warning",
            "detail": f"组合整体回撤{dd_pct:.1f}%，超过10%，建议减仓并暂停新开仓",
            "severity": "warning",
        }]
    return []


def portfolio_overlap_check(holdings: list[dict]) -> list[dict]:
    """Detect ETFs tracking the same index."""
    alerts = []
    for i, a in enumerate(holdings):
        if a.get("status") != "active":
            continue
        name_a = (a.get("name") or a["code"]).lower()
        # Group by index keywords
        index_groups = {
            "沪深300": ["300", "hs300", "沪深300"],
            "中证500": ["500", "zz500", "中证500"],
            "创业板": ["创业板", "gem", "159915", "159949"],
            "科创50": ["科创50", "588000", "588080", "588060"],
            "恒生科技": ["恒生科技", "513180", "513130", "159740"],
            "纳指": ["纳指", "纳斯达克", "513100", "159941"],
            "标普500": ["标普500", "513500", "159655"],
            "半导体/芯片": ["半导体", "芯片", "512760", "512480"],
            "证券": ["证券", "512880", "159841"],
            "军工": ["军工", "512660", "512710"],
            "新能源": ["新能源", "515030", "516160", "515700"],
            "医药": ["医药", "医疗", "512010", "512170"],
            "黄金": ["黄金", "518880", "159812"],
            "红利": ["红利", "股息", "510880", "563180"],
        }
        for group_name, keywords in index_groups.items():
            matched = False
            for kw in keywords:
                if kw in name_a or kw in a["code"]:
                    matched = True
                    break
            if not matched:
                continue
            for j, b in enumerate(holdings):
                if j <= i or b.get("status") != "active":
                    continue
                name_b = (b.get("name") or b["code"]).lower()
                for kw in keywords:
                    if kw in name_b or kw in b["code"]:
                        alerts.append({
                            "code": f"{a['code']}+{b['code']}",
                            "name": f"{a.get('name','')}/{b.get('name','')}",
                            "type": "index_overlap",
                            "detail": f"{a.get('name','') or a['code']}与{b.get('name','') or b['code']}跟踪同一指数({group_name})，建议合并",
                            "severity": "info",
                        })
                        break
                break
            break
    return alerts


def cash_ratio_suggestion(regime: str, total_value: float, total_cost: float) -> dict | None:
    """Suggest cash ratio based on market regime."""
    pnl = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
    if regime == "bear":
        suggested = "60-80%"
        reason = "熊市建议大量保留现金"
    elif regime == "oscillate":
        suggested = "30-50%"
        reason = "震荡市建议中等仓位保留现金"
    else:
        suggested = "10-20%"
        reason = "牛市建议少量保留现金"
    # If already in loss, tighten further
    if pnl < -5:
        suggested = "70-90%"
        reason += "，且当前浮亏，建议进一步减仓"
    return {
        "suggested_cash_ratio": suggested,
        "reason": reason,
        "current_exposure_pct": round(100 * (1 - max(0, pnl) / 100), 1) if pnl > 0 else 100,
    }


# ── Kelly position sizing (mirrors etf_scanner.py P2 #8) ──────────────────


def calc_kelly_position_pct(
    combined_score: float,
    volatility: float,
    regime_coef: float,
    trend_stage: str = "mid",
) -> dict:
    """Kelly-optimal position % for a single holding.

    Mirrors etf_scanner.py Phase 2 #8 logic:
      half-Kelly baseline f=25% → score/vol/regime/trend multipliers.
    """
    # Half-Kelly baseline
    win_rate = 0.55
    avg_win_loss = 1.5
    kelly_f = (avg_win_loss * win_rate - (1 - win_rate)) / avg_win_loss
    f_val = max(0.05, min(0.4, kelly_f / 2))
    base_kelly_pct = f_val * 100

    score_mult = 1.2 if combined_score >= 80 else (1.0 if combined_score >= 65 else 0.8)
    vol_mult = 1.1 if volatility < 0.1 else (1.0 if volatility < 0.2 else 0.8)
    anti_martingale_mult = 1.2 if trend_stage == "early" else (1.0 if trend_stage == "mid" else 0.6)

    position_pct = base_kelly_pct * score_mult * vol_mult * regime_coef * anti_martingale_mult
    position_pct = round(max(5, min(40, position_pct)), 0)

    return {
        "kelly_pct": int(position_pct),
        "kelly_range": [int(max(5, position_pct - 4)), int(min(40, position_pct + 4))],
        "base_kelly": round(base_kelly_pct, 1),
        "score_mult": score_mult,
        "vol_mult": vol_mult,
        "regime_coef": regime_coef,
        "trend_mult": anti_martingale_mult,
    }


def _estimate_volatility(code: str) -> Optional[float]:
    """Quick vol estimate from 20-day kline for Kelly input.

    Uses 85th-15th percentile return spread as vol proxy.
    """
    ts_code = code_to_ts_code(code)
    kline = _fetch_kline(ts_code)
    if kline and len(kline) >= 15:
        closes = [float(r["close"]) for r in kline[-20:]]
        if len(closes) >= 10:
            returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
            idx85 = int(len(returns) * 0.85)
            idx15 = int(len(returns) * 0.15)
            spread = sorted(returns)[idx85] - sorted(returns)[idx15]
            return round(max(0.05, min(0.4, spread)), 2)
    return 0.15  # default moderate vol


def portfolio_kelly_analysis(
    holdings: list[dict],
    scan_compare: list[dict],
    total_value: float,
    regime_coef: float,
) -> dict:
    """Compare current allocation vs Kelly-optimal for each holding.

    Uses scanner combined_score as win-probability proxy, kline vol as risk.
    Returns per-holding action (reduce/hold/increase) + total exposure guidance.
    """
    if not holdings or total_value <= 0:
        return {"holdings": [], "summary": "no_data"}

    scan_map: dict[str, dict] = {}
    for r in scan_compare:
        if "code" in r:
            scan_map[r["code"]] = r

    results: list[dict] = []
    total_kelly_pct = 0.0

    for h in holdings:
        if h.get("status") != "active":
            continue
        code = h["code"]
        qty = float(h.get("quantity", 0))
        price = float(h.get("current_price", 0) or 0)
        current_value = qty * price
        current_alloc_pct = round(current_value / total_value * 100, 1) if total_value > 0 else 0.0

        scan = scan_map.get(code, {})
        score = scan.get("combined_score") or scan.get("scan_score") or 50

        vol = _estimate_volatility(code)

        # Infer trend stage from PnL direction + scan direction
        pnl = h.get("pnl_pct", 0) or 0
        direction = scan.get("score_direction", "")
        if direction == "down":
            trend = "decline"
        elif pnl < 3:
            trend = "early" if direction in ("up", "") else "mid"
        elif pnl < 15:
            trend = "mid"
        else:
            trend = "late"

        kelly = calc_kelly_position_pct(score, vol, regime_coef, trend)

        diff = round(current_alloc_pct - kelly["kelly_pct"], 1)
        abs_diff_pct = abs(diff) / max(kelly["kelly_pct"], 1) * 100 if kelly["kelly_pct"] > 0 else 0

        if diff > 5 and abs_diff_pct > 30:
            action = "reduce"
            note = f"仓位{current_alloc_pct}%远超凯利{kelly['kelly_pct']}%，建议减仓"
        elif diff < -3 and abs_diff_pct > 30:
            action = "increase"
            note = f"仓位{current_alloc_pct}%低于凯利{kelly['kelly_pct']}%，可适当加仓"
        else:
            action = "hold"
            note = f"仓位{current_alloc_pct}%接近凯利{kelly['kelly_pct']}%，维持"

        total_kelly_pct += kelly["kelly_pct"]

        results.append({
            "code": code,
            "name": h.get("name", ""),
            "score": score,
            "current_alloc_pct": current_alloc_pct,
            "optimal_pct": kelly["kelly_pct"],
            "optimal_range": kelly["kelly_range"],
            "diff_pct": diff,
            "action": action,
            "note": note,
            "kelly_detail": kelly,
        })

    # Scale total if sum exceeds position limit (keep ~10% cash)
    position_limit = 100 - 10
    if total_kelly_pct > position_limit:
        scale = position_limit / total_kelly_pct
        for r in results:
            r["scaled_pct"] = round(r["optimal_pct"] * scale, 1)
            r["scaled_range"] = [
                round(r["optimal_range"][0] * scale, 1),
                round(r["optimal_range"][1] * scale, 1),
            ]
    else:
        for r in results:
            r["scaled_pct"] = r["optimal_pct"]
            r["scaled_range"] = r["optimal_range"]

    total_scaled = sum(r["scaled_pct"] for r in results)
    return {
        "holdings": results,
        "total_optimal_pct": round(total_kelly_pct, 1),
        "total_scaled_pct": round(total_scaled, 1),
        "cash_reserve_pct": round(100 - total_scaled, 1),
        "total_over_limit": total_kelly_pct > position_limit,
    }


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
    """Output alerts for all active holdings (incl. portfolio risk)."""
    portfolio = load_portfolio()
    holdings = portfolio.get("holdings", [])
    settings = portfolio.get("settings", {})
    alerts = check_alerts(holdings, settings)
    # Portfolio-level risk
    active = [h for h in holdings if h.get("status") == "active"]
    total_cost = sum(float(h.get("buy_price", 0)) * float(h.get("quantity", 0)) for h in active)
    total_value = 0.0
    for h in active:
        p = fetch_current_price(code_to_ts_code(h["code"]))
        if p is not None:
            total_value += p * float(h.get("quantity", 0))
    alerts.extend(portfolio_drawdown_alert(total_cost, total_value))
    alerts.extend(portfolio_overlap_check(active))
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
    regime_info = {"regime": "unknown", "coefficient": 1.0}
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
                            "combined_score": r.get("combined_score") or score,
                            "recommendation": rec,
                        })
        except Exception:
            scan_compare = [{"note": "etf-scan 执行失败，跳过排名对比"}]

    closed = [h for h in holdings if h.get("status") == "closed"]

    # ── P3 #12: Portfolio-level risk checks ──
    portfolio_dd = portfolio_drawdown_alert(total_cost, total_value)
    alerts.extend(portfolio_dd)
    overlap = portfolio_overlap_check(active)
    alerts.extend(overlap)
    cash_advice = cash_ratio_suggestion(
        regime_info["regime"], total_value, total_cost,
    )

    # ── Kelly portfolio analysis ──
    kelly_analysis = {}
    if scan_compare and isinstance(scan_compare, list) and "note" not in scan_compare[0]:
        kelly_analysis = portfolio_kelly_analysis(
            enriched, scan_compare, total_value,
            regime_info.get("coefficient", 1.0),
        )
        # Add Kelly-based alerts for over-weighted positions
        for kh in kelly_analysis.get("holdings", []):
            if kh["action"] == "reduce":
                alerts.append({
                    "code": kh["code"], "name": kh["name"],
                    "type": "kelly_overweight",
                    "detail": kh["note"],
                    "severity": "info",
                })

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
        "portfolio_risk": {
            "drawdown_alert": portfolio_dd,
            "overlap_warnings": overlap,
            "cash_advice": cash_advice,
            "kelly_analysis": kelly_analysis,
        },
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_kelly(args):
    """Standalone Kelly portfolio analysis (runs scan, outputs optimal allocation)."""
    portfolio = load_portfolio()
    holdings = portfolio.get("holdings", [])
    active = [h for h in holdings if h.get("status") == "active"]

    if not active:
        result = {"meta": {"command": "kelly"}, "error": "无活跃持仓", "holdings": []}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    # Fetch current prices
    enriched = []
    total_value = 0.0
    for h in active:
        current = fetch_current_price(code_to_ts_code(h["code"]))
        if current is not None:
            value = float(h["buy_price"]) * float(h["quantity"])
            total_value += value
            enriched.append({
                "code": h["code"], "name": h.get("name", ""),
                "buy_price": float(h["buy_price"]), "current_price": current,
                "pnl_pct": round((current - float(h["buy_price"])) / float(h["buy_price"]) * 100, 2),
                "quantity": float(h["quantity"]),
                "current_value": round(current * float(h["quantity"]), 2),
                "status": "active",
            })

    # Run scanner for scores + regime
    scan_compare = []
    regime_coef = 1.0
    try:
        rc, stdout, stderr = _run_script("etf_scanner.py", "--output", "compact", timeout=120)
        if rc == 0:
            scan_data = json.loads(stdout)
            regime_coef = scan_data.get("meta", {}).get("regime_coefficient", 1.0)
            rankings = scan_data.get("combined_ranking", [])
            ranking_map = {r["code"]: r for r in rankings}
            for h in active:
                code = h["code"]
                if code in ranking_map:
                    r = ranking_map[code]
                    scan_compare.append({
                        "code": code, "name": h.get("name", ""),
                        "scan_score": r.get("quick_score", 0),
                        "combined_score": r.get("combined_score") or r.get("quick_score", 0),
                        "score_direction": r.get("score_direction", ""),
                    })
    except Exception:
        pass

    kelly_analysis = portfolio_kelly_analysis(
        enriched, scan_compare, total_value, regime_coef,
    )

    result = {
        "meta": {
            "command": "kelly",
            "timestamp": datetime.now().isoformat(),
            "regime_coefficient": regime_coef,
        },
        "holdings": kelly_analysis.get("holdings", []),
        "summary": {
            "total_value": round(total_value, 2),
            "total_optimal_pct": kelly_analysis.get("total_optimal_pct", 0),
            "total_scaled_pct": kelly_analysis.get("total_scaled_pct", 0),
            "cash_reserve_pct": kelly_analysis.get("cash_reserve_pct", 0),
            "over_limit": kelly_analysis.get("total_over_limit", False),
        },
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

    # kelly
    sub.add_parser("kelly", help="凯利公式仓位分析")

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "add": cmd_add,
        "remove": cmd_remove,
        "update": cmd_update,
        "alerts": cmd_alerts,
        "status": cmd_status,
        "kelly": cmd_kelly,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
