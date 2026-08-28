#!/usr/bin/env python3
"""周主线报告 — 中期板块方向聚合分析.

聚合一周数据（行业热力 + 持续性 + 龙虎榜机构信号），
识别适合中线持仓（1-6个月）的主线方向。

数据源:
  - ths_theme: 行业板块实时热力评分
  - market_theme snapshots: 板块持续性记录
  - LHB snapshots: 机构资金信号

评分公式:
  周主线分 = 周均热度(30%) + 上榜频率(25%) + 最新热度(25%)
             + 趋势方向(10%) + LHB验证(10%)

Usage:
    python3 analysis/weekly_report.py                     # 本周报告
    python3 analysis/weekly_report.py --weeks 2           # 回溯2周
    python3 analysis/weekly_report.py --html              # HTML 报告
"""

import argparse
from html import escape
import json
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
LHB_SNAPSHOT_DIR = CACHE_DIR / "lhb_snapshots"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

from core.source_health import classify_failure, live_attempt

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


def _safe_float(v) -> float:
    if v is None: return 0.0
    try: return float(v)
    except: return 0.0


# ──────────────── 数据收集 ────────────────


def load_market_snapshots(days: int = 10) -> dict[str, list[dict]]:
    """Load market-theme sector snapshots."""
    try:
        from fetchers.sector_data import load_snapshot_history
        return load_snapshot_history(days=days)
    except Exception:
        return {}


def load_lhb_snapshots(days: int = 10) -> list[dict]:
    """Compatibility wrapper returning only loaded LHB snapshots."""
    return load_lhb_snapshot_bundle(days=days).get("snapshots", [])


def _read_json_file(filepath: Path) -> Optional[dict]:
    """Read a JSON object, treating malformed files as unavailable."""
    try:
        payload = json.loads(filepath.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def load_lhb_snapshot_bundle(days: int = 10) -> dict:
    """Load LHB snapshots together with per-day collection evidence.

    A snapshot without a status sidecar is intentionally labelled
    ``legacy_snapshot``.  It remains usable for historical aggregation, but
    it is not counted as a current collection attempt.
    """
    snapshots = []
    available_days = 0
    attempted_days = 0
    status_days = {}
    failure_reasons = []
    mapping_stale_days = []
    status_dir = LHB_SNAPSHOT_DIR / "status"
    today = date.today()

    for i in range(max(0, days)):
        day = (today - timedelta(days=i)).isoformat()
        snapshot = _read_json_file(LHB_SNAPSHOT_DIR / f"{day}.json")
        sidecar = _read_json_file(status_dir / f"{day}.json")
        sidecar_status = str(sidecar.get("status", "")) if sidecar else ""

        if sidecar is not None:
            attempted_days += 1
            status = sidecar_status or "error"
            status_days[day] = status
            if sidecar.get("mapping_stale"):
                mapping_stale_days.append(day)
            reasons = sidecar.get("failure_reasons", []) or []
            if isinstance(reasons, str):
                reasons = [reasons]
            failure_reasons.extend(str(reason) for reason in reasons if reason)
            if status == "live_success" and snapshot and snapshot.get("sectors"):
                snapshots.append(snapshot)
                available_days += 1
            continue

        if snapshot and snapshot.get("sectors"):
            status_days[day] = "legacy_snapshot"
            snapshots.append(snapshot)
            available_days += 1
        else:
            status_days[day] = "not_run"

    status_counts = Counter(status_days.values())
    if available_days:
        overall_status = "live_success" if status_counts.get("live_success") else "legacy_snapshot"
    elif attempted_days and any(
            status_counts.get(status, 0) for status in ("error", "mapping_error")):
        overall_status = "error" if status_counts.get("error") else "mapping_error"
    elif attempted_days:
        overall_status = "no_data"
    else:
        overall_status = "not_run"

    return {
        "snapshots": sorted(snapshots, key=lambda s: s.get("date", "")),
        "available_days": available_days,
        "attempted_days": attempted_days,
        "status_days": status_days,
        "failure_reasons": sorted(set(failure_reasons)),
        "mapping_stale_days": sorted(mapping_stale_days),
        "status": overall_status,
    }


def _unpack_source_result(result) -> tuple[dict, Optional[dict]]:
    """Accept source_health wrappers and legacy payloads."""
    if (isinstance(result, dict)
            and isinstance(result.get("payload"), dict)
            and isinstance(result.get("live_attempt"), dict)):
        return result["payload"], result["live_attempt"]
    return result if isinstance(result, dict) else {}, None


def _fallback_industry_rows(payload: dict) -> list[dict]:
    """Convert East Money ranking rows to the THS scoring input contract."""
    rows = []
    for sector in payload.get("sectors", []) or []:
        if sector.get("type") != "industry":
            continue
        name = str(sector.get("name", "") or "").strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "code": str(sector.get("code", "") or ""),
            "change_pct": _safe_float(sector.get("change_pct")),
            "net_flow": _safe_float(sector.get("main_force_net")),
            "total_amount": _safe_float(sector.get("amount")),
            "total_volume": 0,
            "up_count": int(_safe_float(sector.get("up_count"))),
            "down_count": int(_safe_float(sector.get("down_count"))),
            "leader_name": "",
            "leader_change": 0.0,
        })
    return rows


def fetch_current_industry_data() -> dict:
    """Fetch and score today's industries with THS/EM evidence.

    THS remains the preferred source.  East Money is an independent fallback
    and is only labelled successful when it returns valid industry rows.
    """
    from analysis.ths_theme import (
        fetch_industry_data_with_evidence,
        score_industries,
    )

    ths = fetch_industry_data_with_evidence()
    ths_attempt = ths.get("live_attempt", {}) or {}
    errors = list(ths.get("errors", []) or [])
    if ths.get("status") == "live_success" and ths.get("data"):
        return {
            **ths,
            "data": score_industries(list(ths["data"])),
        }

    em_attempt = {}
    em_payload = {}
    try:
        from fetchers.sector_data import get_sector_rankings
        fetched = get_sector_rankings(with_evidence=True)
        em_payload, em_attempt = _unpack_source_result(fetched)
        em_attempt = em_attempt or {}
        em_meta = em_payload.get("meta", {}) or {}
        em_errors = list(em_meta.get("errors", []) or [])
        for error in em_errors:
            text = f"eastmoney_push2: {error}"
            if text not in errors:
                errors.append(text)
        # get_sector_rankings has an historical AKShare fallback when its
        # response is empty.  Reject that path here: this boundary must not
        # label THS data as an independent East Money success.
        em_used_internal_fallback = bool(em_meta.get("upstream_errors"))
        fallback_rows = [] if em_used_internal_fallback else _fallback_industry_rows(em_payload)
        if em_used_internal_fallback:
            errors.append("eastmoney_push2: rejected internal AKShare fallback")
        if fallback_rows:
            provider_attempts = int(ths_attempt.get("provider_attempts", 0) or 0)
            provider_attempts += int(em_attempt.get("provider_attempts", 0) or 0)
            return {
                "data": score_industries(fallback_rows),
                "status": "live_success",
                "source": "eastmoney_push2",
                "live_attempt": live_attempt(
                    attempted=bool(ths_attempt.get("attempted")
                                   or em_attempt.get("attempted")),
                    provider_attempts=provider_attempts,
                    status="success", failure_chain=[
                        ths_attempt.get("reason", "")
                    ] if ths_attempt.get("reason") else None,
                ),
                "errors": errors,
            }
    except Exception as exc:
        reason = classify_failure(exc)
        errors.append(f"eastmoney_push2: {reason}: {exc}")
        em_attempt = live_attempt(
            attempted=True, provider_attempts=1, reason=reason,
            status="error", error_type=type(exc).__name__,
            failure_detail=str(exc),
        )

    provider_attempts = int(ths_attempt.get("provider_attempts", 0) or 0)
    provider_attempts += int(em_attempt.get("provider_attempts", 0) or 0)
    reasons = [
        str(ths_attempt.get("reason", "") or ""),
        str(em_attempt.get("reason", "") or ""),
    ]
    reason = next((item for item in reasons if item and item != "empty"), "empty")
    status = "error" if reason != "empty" else "no_data"
    return {
        "data": [],
        "status": status,
        "source": "none",
        "live_attempt": live_attempt(
            attempted=bool(ths_attempt.get("attempted")
                           or em_attempt.get("attempted")),
            provider_attempts=provider_attempts,
            reason=reason,
            status="error" if status == "error" else "empty",
        ),
        "errors": errors,
    }


def fetch_industry_data() -> list[dict]:
    """Compatibility wrapper returning today's scored industry rows."""
    return fetch_current_industry_data().get("data", [])


# ──────────────── 聚合评分 ────────────────


def aggregate_sectors(market_snapshots: dict[str, list[dict]],
                       lhb_snapshots: list[dict],
                       today_industries: list[dict],
                       lhb_meta: Optional[dict] = None) -> list[dict]:
    """Aggregate sector data across all sources.

    Returns scored sectors with weekly_score (0-100).
    """
    # An explicit bundle controls whether LHB evidence is usable.  The
    # legacy three-argument call infers availability from its snapshot list.
    if lhb_meta is None:
        lhb_available_days = len(lhb_snapshots)
        lhb_status = "live_success" if lhb_available_days else "not_run"
    else:
        lhb_available_days = int(lhb_meta.get("available_days", 0) or 0)
        lhb_status = str(lhb_meta.get("status", "not_run") or "not_run")
    lhb_enabled = lhb_available_days > 0
    score_weights = {
        "avg_hot": 0.30,
        "frequency": 0.25,
        "latest_hot": 0.25,
        "trend": 0.10,
        "lhb": 0.10 if lhb_enabled else 0,
        "base_total": 1.0 if lhb_enabled else 0.90,
    }
    usable_lhb_snapshots = lhb_snapshots if lhb_enabled else []

    # Track all sectors and their daily data
    sector_days = defaultdict(list)  # sector_name → list of {date, hot_score, ...}
    sector_codes = {}

    # From market-theme snapshots
    for date_str, sectors in market_snapshots.items():
        for s in sectors:
            name = s.get("name", "")
            if not name:
                continue
            sector_days[name].append({
                "date": date_str,
                "hot_score": _safe_float(s.get("hot_score", 0)),
                "change_pct": _safe_float(s.get("change_pct", 0)),
                "up_ratio": _safe_float(s.get("up_ratio", 0)),
                "source": "market_theme",
            })
            # Store code from first encounter
            if name not in sector_codes:
                sector_codes[name] = s.get("code", "")

    # From today's industry data
    for s in today_industries:
        name = s.get("name", "")
        if not name:
            continue
        sector_days[name].append({
            "date": "today",
            "hot_score": s.get("hot_score", 0),
            "change_pct": _safe_float(s.get("change_pct", 0)),
            "up_ratio": _safe_float(s.get("up_ratio", 0)),
            "net_flow": s.get("net_flow", 0),
            "leader": s.get("leader_name", ""),
            "source": "ths_theme",
        })
        if name not in sector_codes:
            sector_codes[name] = ""

    if not sector_days:
        return []

    # Count how many distinct dates we have
    all_dates = set()
    for entries in sector_days.values():
        for e in entries:
            if e["date"] != "today":
                all_dates.add(e["date"])
    total_dates = len(all_dates) + (1 if any(
        any(e["date"] == "today" for e in entries)
        for entries in sector_days.values()) else 0)

    # Score each sector
    results = []
    for name, days in sector_days.items():
        hot_scores = [d["hot_score"] for d in days if d["hot_score"] > 0]
        appearance_days = len(set(d["date"] for d in days if d["hot_score"] > 0))

        # Weekly avg hot_score
        avg_hot = mean(hot_scores) if hot_scores else 0

        # Frequency
        freq = appearance_days / max(total_dates, 1)

        # Latest hot_score
        latest_entry = max(days, key=lambda d: (
            9999 if d["date"] == "today" else 0,
            d["date"]
        ))
        latest_hot = latest_entry.get("hot_score", 0)

        # Trend: compare first half vs second half
        sorted_days = sorted([d for d in days if d["date"] != "today"],
                             key=lambda d: d["date"])
        mid = len(sorted_days) // 2
        if mid > 0 and len(sorted_days) >= 2:
            first_avg = mean(d["hot_score"] for d in sorted_days[:mid] if d["hot_score"] > 0) if mid else 0
            second_avg = mean(d["hot_score"] for d in sorted_days[mid:] if d["hot_score"] > 0) if len(sorted_days) > mid else 0
            trend = "up" if second_avg > first_avg else "down" if second_avg < first_avg else "flat"
            trend_score = 80 if trend == "up" else 40 if trend == "down" else 60
        else:
            trend = "flat"
            trend_score = 50

        # LHB cross-ref
        lhb_net = 0
        lhb_direction = ""
        for snap in usable_lhb_snapshots:
            for sec in snap.get("sectors", []):
                if sec.get("sector_name") == name or sec.get("matched_industry") == name:
                    lhb_net += _safe_float(sec.get("inst_net_yi", 0))
        if lhb_net > 0.5:
            lhb_direction = "净买"
            lhb_score = 80
        elif lhb_net < -0.5:
            lhb_direction = "净卖"
            lhb_score = 20
        else:
            lhb_score = 50 if lhb_enabled else None

        # Composite weekly score
        base_weekly = (avg_hot * score_weights["avg_hot"]
                       + freq * 100 * score_weights["frequency"]
                       + latest_hot * score_weights["latest_hot"]
                       + trend_score * score_weights["trend"])
        weekly = base_weekly
        if lhb_enabled:
            weekly += (lhb_score or 0) * score_weights["lhb"]
        else:
            weekly = base_weekly / score_weights["base_total"]
        weekly = round(max(0, min(100, weekly)), 1)

        net_flow = latest_entry.get("net_flow", 0) or 0
        leader = latest_entry.get("leader", "") or latest_entry.get("leader_name", "")
        change = latest_entry.get("change_pct", 0) or 0

        results.append({
            "name": name,
            "code": sector_codes.get(name, ""),
            "weekly_score": weekly,
            "avg_hot": round(avg_hot, 1),
            "appearance_days": appearance_days,
            "total_dates": total_dates,
            "frequency": round(freq, 2),
            "latest_hot": round(latest_hot, 1),
            "latest_change": round(change, 2),
            "net_flow": net_flow,
            "trend": trend,
            "trend_score": trend_score,
            "lhb_net_yi": round(lhb_net, 2),
            "lhb_direction": lhb_direction,
            "lhb_status": lhb_status,
            "score_weights": dict(score_weights),
            "leader": leader,
        })

    results.sort(key=lambda r: r["weekly_score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def classify_weekly(scored: list[dict]) -> dict:
    """Classify into weekly tiers."""
    strong = [s for s in scored if s["weekly_score"] >= 65]
    active = [s for s in scored if 45 <= s["weekly_score"] < 65]
    normal = [s for s in scored if 30 <= s["weekly_score"] < 45]
    weak = [s for s in scored if s["weekly_score"] < 30]
    return {"strong": strong, "active": active, "normal": normal, "weak": weak}


# ──────────────── 报告 ────────────────


def _coverage_lines(meta: dict) -> list[str]:
    """Render data-source coverage and failure evidence for Markdown."""
    lines = []
    industry_status = meta.get("industry_status")
    if industry_status:
        source = meta.get("industry_source") or "none"
        lines.append(f"▸ 行业热力: {industry_status} (source={source})")
        if industry_status != "live_success":
            lines.append("⚠️ 行业数据不可用，周报仅使用已有历史数据（如有）")
        industry_errors = meta.get("industry_errors", []) or []
        if industry_errors:
            lines.append(f"  行业失败原因: {'; '.join(map(str, industry_errors[:3]))}")

    if "lhb_available_days" in meta:
        available = int(meta.get("lhb_available_days", 0) or 0)
        attempted = int(meta.get("lhb_attempted_days", 0) or 0)
        status_days = meta.get("lhb_status_days", {}) or {}
        not_run = sum(1 for status in status_days.values() if status == "not_run")
        lines.append(
            f"▸ 龙虎榜覆盖: 有效{available}天 / 尝试{attempted}天 / 未运行{not_run}天")
        if meta.get("mapping_stale_days"):
            lines.append(
                f"  映射过期但显式使用: {len(meta['mapping_stale_days'])}天")
        reasons = meta.get("lhb_failure_reasons", []) or []
        if reasons:
            lines.append(f"  龙虎榜失败原因: {'; '.join(map(str, reasons[:3]))}")
        if available == 0:
            lines.append(
                "⚠️ 本周无有效龙虎榜验证，周分已重归一，不代表机构资金确认")
    warnings = meta.get("warnings", []) or []
    if warnings:
        lines.append(f"⚠️ 数据提示: {'; '.join(map(str, warnings[:5]))}")
    return lines


def _coverage_html(meta: dict) -> str:
    """Render the same coverage evidence as a compact HTML block."""
    lines = _coverage_lines(meta)
    if not lines:
        return ""
    rendered = "<br>".join(escape(str(line)) for line in lines)
    return f'<div class="coverage">{rendered}</div>'


def generate_report(scored: list[dict], classified: dict,
                    meta: dict) -> str:
    """Generate MD report."""
    lines = []
    lines.append("## 📅 周主线报告")
    lines.append("")
    lines.append(f"▸ 生成时间: {meta.get('time', '')}")
    lines.append(f"▸ 数据区间: {meta.get('period', '')}")
    lines.append(f"▸ 覆盖板块: {meta.get('total_sectors', 0)} 个")
    lines.append(f"▸ 数据天数: {meta.get('total_dates', 0)} 天")
    coverage = _coverage_lines(meta)
    if coverage:
        lines.append("")
        lines.extend(coverage)
    lines.append("")

    strong = classified["strong"]
    active = classified["active"]
    weak = classified["weak"]

    if strong:
        lines.append("### 🔥 中期主线（周分≥65）")
        lines.append("")
        lines.append("| 排名 | 板块 | 周分 | 均热度 | 上榜天数 | 趋势 | LHB验证 | 领涨股 |")
        lines.append("|------|------|------|--------|---------|------|---------|--------|")
        for s in strong[:10]:
            lhb = f"{s['lhb_direction']}({s['lhb_net_yi']:+.1f}亿)" if s['lhb_direction'] else "-"
            lines.append(
                f"| {s['rank']} | {s['name']} | **{s['weekly_score']:.0f}** | "
                f"{s['avg_hot']:.0f} | {s['appearance_days']}/{s['total_dates']} | "
                f"{'↑' if s['trend']=='up' else '↓' if s['trend']=='down' else '→'} | "
                f"{lhb} | {s['leader'] or '-'} |")
        lines.append("")

    if active:
        lines.append("### 👀 关注方向（45-64）")
        lines.append("")
        lines.append("| 排名 | 板块 | 周分 | 均热度 | 趋势 | LHB |")
        lines.append("|------|------|------|--------|------|-----|")
        for s in active[:8]:
            lines.append(
                f"| {s['rank']} | {s['name']} | {s['weekly_score']:.0f} | "
                f"{s['avg_hot']:.0f} | "
                f"{'↑' if s['trend']=='up' else '↓' if s['trend']=='down' else '→'} | "
                f"{s['lhb_direction'] or '-'} |")
        lines.append("")

    if weak:
        lines.append("### ❄️ 退潮方向（<30）")
        lines.append("")
        for s in weak[:5]:
            lines.append(f"- {s['name']} 周分{s['weekly_score']:.0f} 均热度{s['avg_hot']:.0f}")
        lines.append("")

    # LHB highlights
    lhb_buy = [s for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1]
    if lhb_buy:
        lines.append("### 🏛️ 机构资金动向")
        lines.append("")
        lines.append("**机构本周净买入板块**:")
        for s in lhb_buy[:5]:
            lines.append(f"- {s['name']} 净买{s['lhb_net_yi']:+.1f}亿 周分{s['weekly_score']:.0f}")
        lines.append("")

    lines.append("---")
    lines.append("> *数据来源: 同花顺热力 + 东方财富持续性 + 龙虎榜 (AKShare)*")
    return "\n".join(lines)


# ──────────────── HTML 报告 ────────────────


def _generate_html_report(scored: list[dict], classified: dict,
                          meta: dict) -> str:
    """Generate HTML report."""
    strong = classified["strong"][:10]
    active = classified["active"][:8]
    weak = classified["weak"][:5]
    lhb_buy = [s for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1][:5]

    def _rows(items, cols):
        rows = ""
        for s in items:
            cells = ""
            for c in cols:
                if c == "rank":
                    cells += f"<td>{s['rank']}</td>"
                elif c == "name":
                    cells += f"<td><strong>{s['name']}</strong></td>"
                elif c == "score":
                    cls = "s-strong" if s['weekly_score'] >= 65 else "s-active" if s['weekly_score'] >= 45 else ""
                    cells += f'<td class="{cls}">{s["weekly_score"]:.0f}</td>'
                elif c == "avg":
                    cells += f"<td>{s['avg_hot']:.0f}</td>"
                elif c == "days":
                    cells += f"<td>{s['appearance_days']}/{s['total_dates']}</td>"
                elif c == "trend":
                    arr = "↑" if s['trend']=='up' else "↓" if s['trend']=='down' else "→"
                    cls_t = "sp" if s['trend']=='up' else "sn" if s['trend']=='down' else ""
                    cells += f'<td class="{cls_t}">{arr}</td>'
                elif c == "lhb":
                    lhb_str = f"{s['lhb_direction']}({s['lhb_net_yi']:+.1f}亿)" if s['lhb_direction'] else "-"
                    cells += f"<td>{lhb_str}</td>"
                elif c == "leader":
                    cells += f"<td>{s.get('leader','-')}</td>"
                elif c == "hot":
                    cells += f"<td>{s['latest_hot']:.0f}</td>"
            rows += f"<tr>{cells}</tr>"
        return rows

    strong_cols = ["rank", "name", "score", "avg", "days", "trend", "lhb", "leader"]
    active_cols = ["rank", "name", "score", "avg", "trend", "lhb"]

    lhb_rows = ""
    for s in lhb_buy:
        lhb_rows += f"<tr><td>{s['name']}</td><td class='sp'>+{s['lhb_net_yi']:.1f}亿</td><td>{s['weekly_score']:.0f}</td><td>{s['latest_hot']:.0f}</td><td>{s.get('leader','-')}</td></tr>"

    weak_list = "".join(f"<li>{s['name']} 周分{s['weekly_score']:.0f}</li>" for s in weak)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>周主线报告 {meta.get('time','')}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1100px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:36px 40px}}
h1{{font-size:24px;color:#1a1a1a}} h2{{font-size:18px;margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.sec{{background:#fafafa;border-radius:8px;padding:16px 20px;margin:20px 0;border:1px solid #e5e7eb}}
.sec-strong{{border-left:4px solid #dc2626}}
.sec-active{{border-left:4px solid #1d4ed8}}
.sec-lhb{{border-left:4px solid #7c3aed}}
h2{{margin-top:0}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-weight:600;font-size:13px}}
.s-strong{{color:#dc2626;font-weight:700}} .s-active{{color:#1d4ed8;font-weight:600}}
.sp{{color:#dc2626;font-weight:600}} .sn{{color:#16a34a;font-weight:600}}
.summary-cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#f9fafb;border-radius:8px;padding:12px 16px;flex:1;min-width:100px;text-align:center}}
.card .num{{font-size:22px;font-weight:700;color:#1d4ed8}}
.card .lbl{{font-size:12px;color:#86868b;margin-top:2px}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:32px}}
.coverage{{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px 16px;margin:16px 0;color:#9a3412;font-size:13px;line-height:1.7}}
</style></head><body><div class="w">
<h1>📅 周主线报告</h1>
<p class="dt">{meta.get('time','')} | {meta.get('period','')} | {meta.get('total_sectors',0)} 板块 | {meta.get('total_dates',0)} 天数据</p>

{_coverage_html(meta)}

<div class="summary-cards">
<div class="card"><div class="num" style="color:#dc2626">{len(strong)}</div><div class="lbl">中期主线</div></div>
<div class="card"><div class="num" style="color:#1d4ed8">{len(active)}</div><div class="lbl">关注方向</div></div>
<div class="card"><div class="num" style="color:#a1a1a6">{len(weak)}</div><div class="lbl">退潮方向</div></div>
<div class="card"><div class="num">{len(lhb_buy)}</div><div class="lbl">机构净买板块</div></div>
</div>

{'<div class="sec sec-strong"><h2>🔥 中期主线（周分≥65）</h2><table><thead><tr><th>#</th><th>板块</th><th>周分</th><th>均热度</th><th>上榜</th><th>趋势</th><th>LHB</th><th>领涨</th></tr></thead><tbody>' + _rows(strong[:10], strong_cols) + '</tbody></table></div>' if strong else ''}

{'<div class="sec sec-active"><h2>👀 关注方向（45-64）</h2><table><thead><tr><th>#</th><th>板块</th><th>周分</th><th>均热度</th><th>趋势</th><th>LHB</th></tr></thead><tbody>' + _rows(active[:8], active_cols) + '</tbody></table></div>' if active else ''}

{'<div class="sec sec-lhb"><h2>🏛️ 机构资金动向</h2><table><thead><tr><th>板块</th><th>净买额</th><th>周分</th><th>最新热度</th><th>领涨</th></tr></thead><tbody>' + lhb_rows + '</tbody></table></div>' if lhb_buy else ''}

{'<div class="sec"><h2>❄️ 退潮方向</h2><ul>' + weak_list + '</ul></div>' if weak else ''}

<footer><p class="disc">数据来源: 同花顺热力 + 东方财富持续性 + 龙虎榜 (AKShare) | 仅供学习参考</p></footer>
</div></body></html>"""


# ──────────────── 主流程 ────────────────


def main():
    parser = argparse.ArgumentParser(description="周主线报告")
    parser.add_argument("--weeks", type=int, default=1, help="回溯周数, 默认1周")
    parser.add_argument("--html", action="store_true", help="生成 HTML 报告")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    days = args.weeks * 7
    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    period_start = (date.today() - timedelta(days=days)).isoformat()
    period_end = date.today().isoformat()

    print(f"[1/4] 加载市场持续性快照 ({days}天)...")
    market_snapshots = load_market_snapshots(days=days)
    print(f"  {len(market_snapshots)} 天快照")

    print(f"[2/4] 加载龙虎榜快照 ({days}天)...")
    lhb_bundle = load_lhb_snapshot_bundle(days=days)
    lhb_snapshots = lhb_bundle["snapshots"]
    print(
        f"  有效 {lhb_bundle['available_days']} 天 / "
        f"尝试 {lhb_bundle['attempted_days']} 天 / "
        f"状态 {lhb_bundle['status']}"
    )

    print("[3/4] 获取今日行业热力...")
    industry_evidence = fetch_current_industry_data()
    today_industries = industry_evidence.get("data", [])
    if today_industries:
        print(
            f"  {len(today_industries)} 个行业 "
            f"(source={industry_evidence.get('source', 'none')})"
        )
    else:
        print(
            f"  ⚠️ 行业数据不可用 "
            f"(status={industry_evidence.get('status', 'error')})"
        )

    print("[4/4] 聚合评分...")
    scored = aggregate_sectors(
        market_snapshots, lhb_snapshots, today_industries,
        lhb_meta=lhb_bundle,
    )
    warnings = []
    if industry_evidence.get("status") != "live_success":
        warnings.append("industry unavailable")
    if lhb_bundle.get("available_days", 0) == 0:
        warnings.append("no valid LHB verification")
    meta = {
        "time": now,
        "period": f"{period_start} ~ {period_end}",
        "total_sectors": len(scored),
        "total_dates": len(market_snapshots) + (1 if today_industries else 0),
        "industry_status": industry_evidence.get("status", "error"),
        "industry_source": industry_evidence.get("source", "none"),
        "industry_live_attempt": industry_evidence.get("live_attempt", {}),
        "industry_errors": industry_evidence.get("errors", []),
        "lhb_available_days": lhb_bundle.get("available_days", 0),
        "lhb_attempted_days": lhb_bundle.get("attempted_days", 0),
        "lhb_status_days": lhb_bundle.get("status_days", {}),
        "lhb_failure_reasons": lhb_bundle.get("failure_reasons", []),
        "mapping_stale_days": lhb_bundle.get("mapping_stale_days", []),
        "warnings": warnings,
        "score_weights": (
            scored[0].get("score_weights", {}) if scored else {
                "avg_hot": 0.30, "frequency": 0.25, "latest_hot": 0.25,
                "trend": 0.10, "lhb": 0,
                "base_total": 0.90 if not lhb_bundle.get("available_days") else 1.0,
            }
        ),
    }
    if not scored:
        print("⚠️ 无数据")
        if args.json:
            print(json.dumps({
                "meta": meta, "strong": [], "active": [], "lhb_buy": [],
            }, ensure_ascii=False, indent=2))
        else:
            for line in _coverage_lines(meta):
                print(line)
        return

    classified = classify_weekly(scored)

    if args.json:
        output = {
            "meta": meta,
            "strong": [{"name": s["name"], "weekly_score": s["weekly_score"],
                        "avg_hot": s["avg_hot"], "trend": s["trend"],
                        "lhb_direction": s["lhb_direction"]}
                       for s in classified["strong"]],
            "active": [{"name": s["name"], "weekly_score": s["weekly_score"]}
                       for s in classified["active"]],
            "lhb_buy": [{"name": s["name"], "net_yi": s["lhb_net_yi"],
                         "weekly_score": s["weekly_score"]}
                        for s in scored if s["lhb_direction"] == "净买" and s["lhb_net_yi"] > 1],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report = generate_report(scored, classified, meta)
        print(report)

    if args.html:
        html = _generate_html_report(scored, classified, meta)
        html_path = REPORTS_DIR / f"weekly-{now_ts}.html"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"\nHTML: {html_path}")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
