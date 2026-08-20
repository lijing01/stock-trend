#!/usr/bin/env python3
"""每日候选股 /candidates — 每天自动筛出 20~30 只维科夫买点候选.

自动选热点板块(板块排行 hot_score)→ stock_scanner 维科夫漏斗扫成分股 →
批量扩展直到 ≥ min-candidates → 按综合分排序取 top ~30 → 每日候选报告。

Usage:
    python3 daily_candidates.py [--top 30] [--min-candidates 20] [--min-score 50] [--json]
    python3 daily_candidates.py --sectors BK0420,BK0897   # 手动指定板块(覆盖自动选)
"""

import os
import sys
import json
import argparse
import copy
import math
from html import escape
import time
from datetime import datetime, timedelta, time as datetime_time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = Path(os.environ.get("STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

from scans.stock_scanner import (
    apply_membership_quality,
    build_sector_membership,
    build_sector_peer_cohorts,
    composite_from_dimensions,
    gather_candidates,
    merge_sector_memberships,
    run_phase2,
    score_sector_membership,
    select_primary_sector_membership,
)
from core.source_health import (
    LIVE_ATTEMPT_TIMEOUT_SECONDS,
    MAX_PROVIDER_ATTEMPTS,
    RunSourceHealth,
    SOURCES as SOURCE_HEALTH_NAMES,
    classify_failure,
    live_attempt,
)
from core.recommendation_snapshot import save_snapshot_if_official
SIGNAL_LABELS = {
    "volume_breakout": "放量突破",
    "northbound_adding": "北向增持",
}

REASON_LABELS = {
    "kline_stale": "K线数据过期",
    "coverage_below_70pct": "数据覆盖率低于70%",
    "secondary_data_missing": "资金面和基本面数据均缺失",
    "capital_error": "资金面数据返回错误",
    "fundamental_error": "基本面数据返回错误",
    "single_day_pulse": "板块仅呈单日脉冲，持续性证据不足",
    "history_insufficient": "板块历史快照不足，尚不能验证持续性",
    "breadth_capital_divergence": "普涨但市场资金背离，需板块资金或共振确认",
    "quality_adjusted_below_min_score": "质量调整分低于最低门槛",
    "sector_unverified": "板块持续性未验证",
    "manual_unverified": "手动指定板块未经持续性验证",
    "stale_cache": "板块排行使用过期缓存",
    "partial_realtime": "板块实时排行数据不完整",
    "regime_missing": "市场环境数据缺失",
    "regime_stale": "市场环境数据过期",
    "regime_weak": "市场环境评分偏弱",
    "intraday_provisional": "盘中数据尚未收盘确认",
    "recommendation_limit": "超出当日推荐数量上限",
    "wyckoff_countertrend": "维科夫长短周期逆势，降级为观察",
    "wyckoff_retest_pending": "维科夫突破后回踩，等待重新站稳箱顶",
    "wyckoff_failed_breakout": "维科夫突破失败，等待重新构筑",
    "trade_plan_target_source_not_executable": "目标来源非结构化阻力位，仅供观察",
}

DATA_REASON_CODES = {
    "kline_stale",
    "coverage_below_70pct",
    "secondary_data_missing",
    "capital_error",
    "fundamental_error",
    "stale_cache",
    "partial_realtime",
    "regime_missing",
    "regime_stale",
    "intraday_provisional",
}


def candidate_rank_score(item):
    """Return the quality-adjusted rank score with legacy fallback."""
    return item.get("quality_adjusted_score", item.get("composite_score", 0))


def _is_final_valid_candidate(item, min_score):
    """Single eligibility predicate shared by scan stopping and final audit."""
    return bool(
        candidate_rank_score(item) >= min_score
        and item.get("data_quality", {}).get("eligible", False)
        and item.get("sector_actionable", True)
    )


_PERFORMANCE_PHASE_FIELDS = (
    "sector_ranking_seconds", "sector_membership_seconds", "kline_seconds",
    "wyckoff_seconds", "capital_seconds", "fundamental_seconds",
    "report_seconds", "total_seconds",
)
_PERFORMANCE_FUNNEL_FIELDS = (
    "batch_count", "raw_candidate_count", "unique_candidate_count",
    "wyckoff_pass_count", "final_candidate_count", "final_valid_count",
    "actionable_count",
)
_SOURCE_AUDIT_FIELDS = (
    "logical_live_requests", "provider_attempts", "cache_hits", "failures",
    "circuit_breaks", "failure_reasons", "state",
)


def _record_degradation(metrics, reason):
    """Record one stable degradation reason without duplicate noise."""
    reasons = metrics.setdefault("degradation_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _record_failed_batch(metrics, batch, exc):
    """Retain the failed batch and its exception type in the public audit."""
    metrics.setdefault("failed_batches", []).append({
        "sectors": list(batch),
        "reason": type(exc).__name__,
    })
    _record_degradation(metrics, f"batch_error:{type(exc).__name__}")


def _complete_performance(performance, source_health, candidates, buckets,
                          min_score, total_seconds):
    """Finalize the additive public performance contract from run evidence."""
    completed = performance
    for field in _PERFORMANCE_PHASE_FIELDS:
        completed.setdefault(field, 0.0)
    for field in _PERFORMANCE_FUNNEL_FIELDS:
        completed.setdefault(field, 0)
    completed["final_candidate_count"] = len(candidates)
    completed["final_valid_count"] = sum(
        _is_final_valid_candidate(item, min_score) for item in candidates)
    completed["actionable_count"] = len(buckets.get("actionable", []))
    completed["total_seconds"] = max(0.0, float(total_seconds))
    completed.setdefault("degradation_reasons", [])
    completed.setdefault("failed_batches", [])
    attempted_batches = int(completed.get("batch_count", 0))
    failed_batches = len(completed["failed_batches"])
    completed["scan_status"] = (
        "error" if attempted_batches > 0 and failed_batches == attempted_batches
        else ("degraded" if completed["degradation_reasons"] else "complete")
    )

    snapshot = (source_health.snapshot()
                if isinstance(source_health, RunSourceHealth)
                else completed.get("sources", {}))
    sources = {}
    for source in SOURCE_HEALTH_NAMES:
        state = snapshot.get(source, {})
        sources[source] = {
            field: (dict(state.get(field, {}))
                    if field == "failure_reasons"
                    else state.get(field, "healthy" if field == "state" else 0))
            for field in _SOURCE_AUDIT_FIELDS
        }
        sources[source]["requests"] = sources[source][
            "logical_live_requests"]
    completed["sources"] = sources
    # Historical alias retained for additive compatibility.
    completed["source_health"] = sources
    for field in _PERFORMANCE_PHASE_FIELDS:
        completed[field] = round(max(0.0, float(completed[field])), 3)
    return completed


def _freeze_output_envelope(performance, builders, run_started_at=None):
    """Assemble requested formats once, then freeze a shared timing snapshot."""
    assembly_started = time.monotonic()
    outputs = {name: builder() for name, builder in builders}
    assembly_finished = time.monotonic()
    assembly_seconds = max(0.0, assembly_finished - assembly_started)
    snapshot = copy.deepcopy(performance)
    snapshot["report_seconds"] = round(assembly_seconds, 3)
    prior_total = max(0.0, float(performance.get("total_seconds", 0.0)))
    total_seconds = prior_total + assembly_seconds
    if run_started_at is not None:
        total_seconds = max(
            total_seconds, max(0.0, assembly_finished - run_started_at))
    snapshot["total_seconds"] = round(total_seconds, 3)
    return outputs, snapshot


def _attach_performance_audit(output, performance, output_format):
    """Attach the already-frozen audit without rerendering the report body."""
    if output_format == "markdown":
        audit = "\n".join(_performance_markdown(performance))
        marker = "\n---\n"
        return output.replace(marker, f"\n{audit}\n{marker}", 1)
    if output_format == "html":
        return output.replace(
            "\n<footer>", f"\n{_performance_html(performance)}\n<footer>", 1)
    return output


def _performance_markdown(performance):
    if not performance:
        return []
    phase_labels = (
        ("板块排行", "sector_ranking_seconds"),
        ("板块成分", "sector_membership_seconds"),
        ("K线", "kline_seconds"), ("维科夫", "wyckoff_seconds"),
        ("资金", "capital_seconds"), ("基本面", "fundamental_seconds"),
        ("报告", "report_seconds"), ("总计", "total_seconds"),
    )
    lines = ["", "## 性能与数据源审计", "", "| 阶段 | 秒 |", "|---|---:|"]
    lines.extend(
        f"| {label} | {float(performance.get(field, 0)):.3f} |"
        for label, field in phase_labels)
    lines.extend([
        "",
        "**漏斗**: "
        f"批次 {performance.get('batch_count', 0)} → "
        f"原始 {performance.get('raw_candidate_count', 0)} → "
        f"去重 {performance.get('unique_candidate_count', 0)} → "
        f"维科夫 {performance.get('wyckoff_pass_count', 0)} → "
        f"最终 {performance.get('final_candidate_count', 0)} → "
        f"有效 {performance.get('final_valid_count', 0)} → "
        f"可执行 {performance.get('actionable_count', 0)}",
        "",
        f"**扫描状态**: {performance.get('scan_status', 'complete')} | "
        f"降级原因: {'、'.join(performance.get('degradation_reasons', [])) or '无'}",
        "",
        "| 数据源 | 逻辑请求 | Provider尝试 | 缓存命中 | 失败 | 熔断 | 状态 | 失败原因 |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ])
    for source in SOURCE_HEALTH_NAMES:
        state = performance.get("sources", {}).get(source, {})
        reasons = json.dumps(
            state.get("failure_reasons", {}), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"))
        lines.append(
            f"| {source} | {state.get('logical_live_requests', 0)} | "
            f"{state.get('provider_attempts', 0)} | "
            f"{state.get('cache_hits', 0)} | {state.get('failures', 0)} | "
            f"{state.get('circuit_breaks', 0)} | "
            f"{state.get('state', 'healthy')} | {reasons} |")
    return lines


def _performance_html(performance):
    if not performance:
        return ""
    rows = []
    for source in SOURCE_HEALTH_NAMES:
        state = performance.get("sources", {}).get(source, {})
        reasons = json.dumps(
            state.get("failure_reasons", {}), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"))
        rows.append(
            f"<tr><td>{source}</td>"
            f"<td>{state.get('logical_live_requests', 0)}</td>"
            f"<td>{state.get('provider_attempts', 0)}</td>"
            f"<td>{state.get('cache_hits', 0)}</td>"
            f"<td>{state.get('failures', 0)}</td>"
            f"<td>{state.get('circuit_breaks', 0)}</td>"
            f"<td>{state.get('state', 'healthy')}</td><td>{reasons}</td></tr>")
    phase_text = " | ".join(
        f"{field.removesuffix('_seconds')}={float(performance.get(field, 0)):.3f}s"
        for field in _PERFORMANCE_PHASE_FIELDS)
    funnel_text = " | ".join(
        f"{field}={performance.get(field, 0)}"
        for field in _PERFORMANCE_FUNNEL_FIELDS)
    scan_status = escape(str(performance.get("scan_status", "complete")))
    degradation_reasons = escape(
        "、".join(performance.get("degradation_reasons", [])) or "无")
    return (
        "<section><h2 style='font-size:18px;margin:18px 0 8px'>"
        "性能与数据源审计</h2>"
        f"<p class='dt'>{phase_text}</p><p class='dt'>{funnel_text}</p>"
        f"<p class='dt'>扫描状态={scan_status} | 降级原因={degradation_reasons}</p>"
        "<table><thead><tr><th>数据源</th><th>逻辑请求</th>"
        "<th>Provider尝试</th><th>缓存</th><th>失败</th><th>熔断</th>"
        "<th>状态</th><th>失败原因</th></tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table></section>")


def _emit_performance_summary(performance):
    """Emit one deterministic, machine-greppable stderr audit line."""
    source_text = ",".join(
        f"{source}:req={state.get('logical_live_requests', 0)}/"
        f"attempt={state.get('provider_attempts', 0)}/"
        f"cache={state.get('cache_hits', 0)}/fail={state.get('failures', 0)}/"
        f"circuit={state.get('circuit_breaks', 0)}/"
        f"state={state.get('state', 'healthy')}/"
        f"reasons={json.dumps(state.get('failure_reasons', {}), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        for source, state in sorted(performance.get("sources", {}).items()))
    phase_text = " ".join(
        f"{field.removesuffix('_seconds')}="
        f"{float(performance.get(field, 0)):.3f}s"
        for field in _PERFORMANCE_PHASE_FIELDS)
    print(
        f"[performance] {phase_text} "
        f"batches={performance.get('batch_count', 0)} "
        f"raw={performance.get('raw_candidate_count', 0)} "
        f"unique={performance.get('unique_candidate_count', 0)} "
        f"wyckoff={performance.get('wyckoff_pass_count', 0)} "
        f"final={performance.get('final_candidate_count', 0)} "
        f"final_valid={performance.get('final_valid_count', 0)} "
        f"actionable={performance.get('actionable_count', 0)} "
        f"sources=[{source_text}]",
        file=sys.stderr,
    )


def is_recommendation_session(now=None):
    """Treat the whole 09:30-15:00 window as provisional, including lunch."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return datetime_time(9, 30) <= current <= datetime_time(15, 0)


def _is_current_trading_day(last_trading_date, source, now=None):
    """Infer today's verified trading status from the shared calendar result."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    if source == "calendar_closed" and last_trading_date:
        return False
    if source == "calendar_open":
        return True
    if source in ("snapshot", "cache") \
            and last_trading_date == now.strftime("%Y-%m-%d"):
        return True
    return None


def resolve_recommendation_date(now=None, regime_date="", last_trading_date="",
                                is_trading_day=None):
    """Resolve the closing-data date that a recommendation may rely on."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if is_trading_day is False and last_trading_date:
        return last_trading_date
    # Without a trading calendar, only the immediately preceding weekday is
    # safe to treat as the last close. Holiday uncertainty therefore degrades
    # to observation through the later K-line/regime date checks.
    if now.weekday() >= 5 or now.time() < datetime_time(9, 30):
        previous = now.date() - timedelta(days=1)
        while previous.weekday() >= 5:
            previous -= timedelta(days=1)
        return previous.strftime("%Y-%m-%d")
    if is_recommendation_session(now):
        return today
    return today


def _window_average(values, size):
    if len(values) < size:
        return None
    selected = values[-size:]
    return round(sum(selected) / len(selected), 1)


def merge_sector_resonance(ranked, resonance_sectors):
    """Merge same-day ths-theme ZT/LHB evidence into EM sector rows."""
    by_name = {
        item.get("name", ""): item for item in resonance_sectors
        if item.get("name")
    }
    merged = []
    for source in ranked:
        sector = dict(source)
        resonance = by_name.get(sector.get("name", ""), {})
        for key in ("zt_score", "lhb_score", "lhb_direction"):
            if resonance.get(key) is not None:
                sector[key] = resonance[key]
        merged.append(sector)
    return merged


def enrich_sector_context(ranked, history, hs300_change=None, as_of_date=""):
    """Attach absolute strength, persistence, relative strength and action gate."""
    dates = sorted(history.keys())[-10:] if history else []
    enriched = []
    for source in ranked:
        sector = dict(source)
        code = sector.get("code", "")
        aligned_entries = []
        for date_str in dates:
            match = next(
                (row for row in history.get(date_str, [])
                 if row.get("code") == code),
                None,
            )
            aligned_entries.append(match)
        hot_values = [
            float(row.get("hot_score", 0) or 0) if row else 0.0
            for row in aligned_entries
        ]
        avg3 = _window_average(hot_values, 3)
        avg5 = _window_average(hot_values, 5)
        avg10 = _window_average(hot_values, 10)
        persistence_values = [v for v in (avg3, avg5, avg10) if v is not None]
        persistence = (
            round(sum(persistence_values) / len(persistence_values), 1)
            if persistence_values else 0.0
        )
        change = sector.get("change_pct")
        relative_strength = None
        if change is not None and hs300_change is not None:
            relative_strength = round(float(change) - float(hs300_change), 2)

        days = sum(row is not None for row in aligned_entries)
        recent_entries = aligned_entries[-3:]
        history_current = bool(dates) and (
            not as_of_date or dates[-1] == as_of_date
        )
        latest_present = bool(
            history_current and aligned_entries
            and aligned_entries[-1] is not None
        )
        short_persistence = (
            round(sum(hot_values) / len(hot_values), 1)
            if len(hot_values) >= 2 else 0.0
        )
        if not persistence_values:
            persistence = short_persistence
        classification_persistence = avg3 if avg3 is not None else short_persistence
        history_insufficient = len(dates) < 3 or days < 3
        if latest_present and len(dates) >= 3 \
                and all(row is not None for row in recent_entries) \
                and classification_persistence >= 60 \
                and (relative_strength is None or relative_strength >= 0):
            sector_type = "mainline"
        elif latest_present and len(dates) >= 2 \
                and all(row is not None for row in aligned_entries[-2:]) \
                and classification_persistence >= 45:
            sector_type = "emerging"
        else:
            sector_type = "single_day_pulse"

        relative_component = 50.0
        if relative_strength is not None:
            relative_component = max(0.0, min(100.0, 50 + relative_strength * 10))
        resonance_values = [
            float(sector[key]) for key in ("zt_score", "lhb_score")
            if sector.get(key) is not None
        ]
        resonance = (
            sum(resonance_values) / len(resonance_values)
            if resonance_values else 50.0
        )
        recent_capital_entries = aligned_entries[-5:]
        net_flows = [
            float(row["net_flow"])
            for row in recent_capital_entries
            if row and row.get("net_flow") is not None
        ]
        capital_persistence = 50.0
        capital_positive_days = 0
        capital_streak = 0
        if net_flows:
            capital_positive_days = sum(value > 0 for value in net_flows)
            denominator = max(1, len(recent_capital_entries))
            for row in reversed(recent_capital_entries):
                if not row or row.get("net_flow") is None \
                        or float(row["net_flow"]) <= 0:
                    break
                capital_streak += 1
            capital_persistence = (
                capital_positive_days / denominator * 70
                + min(capital_streak / 3, 1) * 30
            )
        # Weak-market promotion requires positive flow evidence, not merely
        # the presence of a (possibly negative or sparse) flow record.
        positive_capital_proof = (
            len(net_flows) >= 3
            and capital_positive_days >= 2
            and sum(net_flows) > 0
        )
        capital_evidence = (
            "positive_verified" if positive_capital_proof
            else ("partial" if net_flows else "unknown")
        )
        sector_score = round(
            float(sector.get("absolute_hot_score", 0)) * 0.30
            + persistence * 0.30
            + relative_component * 0.15
            + capital_persistence * 0.15
            + resonance * 0.10,
            1,
        )
        sector.update({
            "relative_hot_score": sector.get("hot_score", 0),
            "persistence_3d": avg3,
            "persistence_5d": avg5,
            "persistence_10d": avg10,
            "persistence_score": persistence,
            "persistence_days": days,
            "relative_strength": relative_strength,
            "capital_persistence": round(capital_persistence, 1),
            "capital_positive_days": capital_positive_days,
            "capital_streak": capital_streak,
            "capital_evidence": capital_evidence,
            "persistence_status": (
                "history_insufficient" if history_insufficient
                else ("verified" if sector_type in ("mainline", "emerging")
                      else "single_day_pulse")
            ),
            "resonance_score": round(resonance, 1),
            "sector_score": sector_score,
            "sector_type": sector_type,
            "sector_actionable": sector_type in ("mainline", "emerging"),
        })
        enriched.append(sector)
    enriched.sort(key=lambda item: item["sector_score"], reverse=True)
    return enriched


def _candidate_memberships(candidate, sector_context=None):
    """Return complete membership evidence for a gathered candidate."""
    memberships = candidate.get("sector_memberships")
    if memberships:
        return merge_sector_memberships(memberships)
    sector_code = candidate.get("sector_code", "")
    context = (sector_context or {}).get(sector_code, {})
    if not context:
        context = {
            "name": candidate.get("sector_name", sector_code),
            "hot_score": candidate.get("sector_hot_score", 50),
            "sector_actionable": candidate.get("sector_actionable", False),
            "sector_score": candidate.get("sector_score"),
            "persistence_status": candidate.get(
                "sector_persistence_status", ""),
            "persistence_score": candidate.get("sector_persistence"),
            "relative_strength": candidate.get("sector_relative_strength"),
            "ranking_position": candidate.get("ranking_position"),
            "ranking_source": candidate.get("ranking_source", ""),
            "ranking_data_date": candidate.get("ranking_data_date", ""),
            "ranking_quality": candidate.get("ranking_quality", ""),
            "ranking_errors": candidate.get("ranking_errors", []),
        }
    return [build_sector_membership(
        sector_code,
        candidate.get("sector_name", sector_code),
        context=context,
        stock=candidate,
    )]


def _rebind_primary_sector(item, peer_cohorts=None, as_of_date=""):
    """Rebind to the strongest membership and recompute sector-only effects."""
    rebound = copy.deepcopy(item)
    memberships = merge_sector_memberships(
        rebound.get("sector_memberships", []))
    primary = select_primary_sector_membership(memberships)
    if not primary:
        return rebound

    rebound.update({
        "sector_code": primary.get("code", ""),
        "sector_name": primary.get("name", ""),
        "sector_hot_score": primary.get(
            "hot_score", primary.get("sector_score", 50)),
        "sector_type": primary.get("sector_type", ""),
        "sector_actionable": primary.get("sector_actionable", False),
        "sector_persistence_status": primary.get("persistence_status", ""),
        "sector_capital_evidence": primary.get(
            "capital_evidence", "unknown"),
        "sector_score": primary.get("sector_score"),
        "sector_persistence": primary.get("persistence_score"),
        "sector_relative_strength": primary.get("relative_strength"),
        "ranking_position": primary.get("ranking_position"),
        "ranking_source": primary.get("ranking_source", ""),
        "ranking_data_date": primary.get("ranking_data_date", ""),
        "ranking_quality": primary.get("ranking_quality", ""),
        "ranking_errors": primary.get("ranking_errors", []),
        "membership_source": primary.get("membership_source", "realtime"),
        "membership_data_date": primary.get("membership_data_date", ""),
        "membership_quality": primary.get("membership_quality", "good"),
        "membership_cache_error": primary.get("membership_cache_error", ""),
        "sector_memberships": memberships,
    })

    dimensions = rebound.get("dimensions")
    raw_dimensions = rebound.get("raw_dimensions")
    if raw_dimensions is None and dimensions:
        raw_dimensions = dict(dimensions)
    if raw_dimensions and "sector_strength" in raw_dimensions:
        raw_dimensions["sector_strength"] = score_sector_membership(
            rebound, primary, peer_cohorts or {})
        rebound["raw_dimensions"] = raw_dimensions
        rebound["dimensions"] = {
            name: round(value, 1)
            for name, value in raw_dimensions.items()
        }
        raw_score = round(composite_from_dimensions(raw_dimensions), 1)
        rebound["composite_score"] = raw_score
        rebound["raw_composite_score"] = raw_score

    base_quality = rebound.get("base_data_quality")
    if base_quality is None:
        base_quality = rebound.get("data_quality", {})
    rebound["base_data_quality"] = copy.deepcopy(base_quality)
    rebound["data_quality"] = apply_membership_quality(
        base_quality, primary, as_of_date=as_of_date)
    raw_score = rebound.get(
        "raw_composite_score", rebound.get("composite_score", 0))
    quality = rebound["data_quality"]
    rebound["quality_adjusted_score"] = round(
        raw_score
        * quality.get("coverage_factor", 1.0)
        * quality.get("freshness_factor", 1.0),
        1,
    )
    cohort = sorted((peer_cohorts or {}).get(primary.get("code", ""), []),
                    reverse=True)
    if cohort:
        change = rebound.get("change_pct", 0)
        rebound["sector_relative_rank"] = (
            cohort.index(change) + 1 if change in cohort else len(cohort))
        rebound["sector_total"] = len(cohort)
    return rebound


def pick_hot_sectors(top_n=None, min_hot=45, min_stocks=10, regime=None,
                     as_of_date="", source_health=None, metrics=None):
    """Pick all sectors above the absolute heat floor, in rank order."""
    metrics = metrics if metrics is not None else {}
    from fetchers.sector_data import (
        append_daily_snapshot,
        get_sector_rankings,
        load_rankings_cache_full,
        load_snapshot_history,
        rank_hot_sectors,
        save_rankings_cache,
    )
    ranking_token = None
    if isinstance(source_health, RunSourceHealth) \
            and time.monotonic() < source_health.live_deadline:
        ranking_token = source_health.try_acquire_live_permit(
            "sector_ranking")
    try:
        if isinstance(source_health, RunSourceHealth) \
                and ranking_token is not None:
            wrapped = get_sector_rankings(
                timeout=LIVE_ATTEMPT_TIMEOUT_SECONDS["sector_ranking"],
                retries=max(
                    0, MAX_PROVIDER_ATTEMPTS["sector_ranking"] // 2 - 1),
                with_evidence=True,
                deadline=source_health.live_deadline)
            rankings = wrapped["payload"]
            ranking_attempt = wrapped["live_attempt"]
        elif source_health is None or not isinstance(
                source_health, RunSourceHealth):
            rankings = get_sector_rankings()
        else:
            rankings = {"meta": {}, "sectors": []}
    except Exception as exc:
        rankings = {"meta": {"errors": [str(exc)]}, "sectors": []}
        if ranking_token is not None:
            attempts = getattr(exc, "provider_attempts", 0)
            if attempts:
                source_health.mark_started(ranking_token)
                source_health.complete_failure(ranking_token, live_attempt(
                    attempted=True, provider_attempts=attempts,
                    reason=getattr(exc, "reason", "")
                    or classify_failure(exc)))
            else:
                source_health.release_unstarted(
                    ranking_token, "live_deadline")
            ranking_token = None
    live_meta = rankings.get("meta", {})
    active = sum(
        1 for sector in rankings.get("sectors", [])
        if (sector.get("up_count", 0) or 0) > 0
        or (sector.get("down_count", 0) or 0) > 0
    )
    if ranking_token is not None:
        if ranking_attempt.get("attempted"):
            source_health.mark_started(ranking_token)
        if ranking_attempt.get("reason"):
            source_health.complete_failure(ranking_token, ranking_attempt)
        elif ranking_attempt.get("attempted"):
            source_health.complete_success(ranking_token, ranking_attempt)
        else:
            source_health.release_unstarted(ranking_token, "live_deadline")
    ranking_meta = {
        "source": live_meta.get("source", "realtime"),
        "data_date": as_of_date or datetime.now().strftime("%Y-%m-%d"),
        "quality": "good",
        "errors": live_meta.get("errors", [])
        or live_meta.get("upstream_errors", []),
    }
    if active and live_meta.get("complete", False):
        if as_of_date:
            try:
                save_rankings_cache(rankings, data_date=as_of_date)
            except (OSError, TypeError, ValueError) as exc:
                _record_degradation(
                    metrics, f"ranking_cache_write_error:{type(exc).__name__}")
            try:
                append_daily_snapshot(rankings, override_date=as_of_date)
            except (OSError, TypeError, ValueError) as exc:
                _record_degradation(
                    metrics, f"sector_snapshot_write_error:{type(exc).__name__}")
    else:
        cached = load_rankings_cache_full()
        cached_rankings = (cached or {}).get("rankings", {})
        cache_usable = (
            bool(cached_rankings.get("sectors"))
            and cached_rankings.get("meta", {}).get("complete") is not False
        )
        if cache_usable:
            rankings = cached["rankings"]
            if isinstance(source_health, RunSourceHealth):
                source_health.record_cache_hit(
                    "sector_ranking", stale=True, reason="cache_only")
            ranking_meta = {
                "source": "cache",
                "data_date": cached.get("data_date", ""),
                "quality": "degraded",
                "errors": live_meta.get("errors", [])
                or live_meta.get("upstream_errors", []),
            }
        elif active:
            ranking_meta = {
                "source": "realtime_partial",
                "data_date": as_of_date,
                "quality": "partial",
                "errors": live_meta.get("errors", []),
            }
        else:
            ranking_meta = {
                "source": "error", "data_date": "", "quality": "error",
                "errors": live_meta.get("errors", []),
            }
    ranked = rank_hot_sectors(rankings, top_n=top_n, min_stocks=min_stocks)
    for sector in ranked:
        sector.update({
            "ranking_source": ranking_meta["source"],
            "ranking_data_date": ranking_meta["data_date"],
            "ranking_quality": ranking_meta["quality"],
            "ranking_errors": ranking_meta["errors"],
        })
    qualified = [
        sector for sector in ranked
        if sector.get("absolute_hot_score", 0) >= min_hot
    ]
    expected_date = as_of_date or (regime or {}).get("data_date", "")
    resonance_quality = "not_available"
    resonance_reason = ""
    if expected_date:
        try:
            from bridge.sector_feeder import load_qualified_sectors
            resonance = load_qualified_sectors()
            if resonance.date == expected_date:
                qualified = merge_sector_resonance(
                    qualified, resonance.sectors)
                resonance_quality = "good"
            else:
                resonance_quality = "stale"
                resonance_reason = "date_mismatch"
                _record_degradation(metrics, "resonance_stale:date_mismatch")
        except Exception as exc:
            resonance_quality = "error"
            resonance_reason = type(exc).__name__
            _record_degradation(
                metrics, f"resonance_error:{type(exc).__name__}")
    for sector in qualified:
        sector["resonance_quality"] = resonance_quality
        sector["resonance_reason"] = resonance_reason
    hs300_change = (regime or {}).get("hs300_change")
    enriched = enrich_sector_context(
        qualified,
        load_snapshot_history(days=10),
        hs300_change=hs300_change,
        as_of_date=expected_date,
    )
    for sector in enriched:
        if sector.get("ranking_source") == "cache" \
                and expected_date \
                and sector.get("ranking_data_date") != expected_date:
            sector["sector_type"] = "stale_cache"
            sector["sector_actionable"] = False
        elif sector.get("ranking_quality") in ("partial", "error"):
            sector["sector_type"] = "partial_realtime"
            sector["sector_actionable"] = False
    return enriched


def scan_sectors(sector_codes, batch_size=4, per_sector=25,
                 min_candidates=20, min_score=50, as_of_date="",
                 sector_context=None, source_health=None, metrics=None,
                 trade_plan_policy=None):
    """Expand until enough score-qualified, data-eligible candidates exist."""
    metrics = metrics if metrics is not None else {}
    if sector_context is None:
        from fetchers.sector_data import get_sector_rankings, rank_hot_sectors
        try:
            rankings = get_sector_rankings()
            ranked = rank_hot_sectors(
                rankings, top_n=len(rankings.get("sectors", [])))
            meta = rankings.get("meta", {})
            metrics["ranking_snapshot_source"] = meta.get(
                "source", "realtime")
            metrics["ranking_snapshot_errors"] = meta.get(
                "errors", meta.get("upstream_errors", []))
            sector_context = {}
            for position, sector in enumerate(ranked, start=1):
                sector_context[sector.get("code", "")] = {
                    **sector,
                    "ranking_position": sector.get(
                        "ranking_position", position),
                    "ranking_source": sector.get(
                        "ranking_source", meta.get("source", "realtime")),
                    "ranking_data_date": sector.get(
                        "ranking_data_date", meta.get("data_date", "")),
                    "ranking_quality": sector.get(
                        "ranking_quality", meta.get("quality", "")),
                }
        except Exception as exc:
            metrics["ranking_snapshot_source"] = "error"
            metrics["ranking_snapshot_errors"] = [str(exc)]
            sector_context = {}

    def sector_order_key(code):
        position = (sector_context or {}).get(code, {}).get(
            "ranking_position")
        try:
            position = float(position)
        except (TypeError, ValueError):
            position = float("inf")
        return position, str(code)

    ordered_sector_codes = sorted(
        dict.fromkeys(sector_codes), key=sector_order_key)
    all_scored = {}
    phase1_candidates = {}
    analyzed_codes = set()
    metrics.setdefault("batch_count", 0)
    metrics.setdefault("raw_candidate_count", 0)
    metrics.setdefault("unique_candidate_count", 0)
    for i in range(0, len(ordered_sector_codes), batch_size):
        batch = ordered_sector_codes[i:i + batch_size]
        metrics["batch_count"] += 1
        membership_started = time.monotonic()
        try:
            try:
                phase1 = gather_candidates(
                    batch, top_n_per_sector=per_sector,
                    sector_context=sector_context,
                    source_health=source_health, metrics=metrics)
            except TypeError as exc:
                # Preserve compatibility with callers/tests that inject the
                # historical two-argument gather function.
                if not any(name in str(exc) for name in (
                        "sector_context", "source_health", "metrics")):
                    raise
                phase1 = gather_candidates(
                    batch, top_n_per_sector=per_sector)
        except Exception as e:
            print(f"  ⚠️ 板块 {batch} 汇聚失败: {e}", file=sys.stderr)
            _record_failed_batch(metrics, batch, e)
            continue
        finally:
            metrics["sector_membership_seconds"] = metrics.get(
                "sector_membership_seconds", 0.0) + (
                    time.monotonic() - membership_started)
        raw_candidates = phase1.get("candidates", [])
        metrics["raw_candidate_count"] += len(raw_candidates)
        for candidate in raw_candidates:
            code = candidate.get("code", "")
            candidate["sector_memberships"] = _candidate_memberships(
                candidate, sector_context)
            existing = phase1_candidates.get(code)
            if existing is None:
                phase1_candidates[code] = candidate
            else:
                existing["sector_memberships"] = merge_sector_memberships(
                    existing.get("sector_memberships", []),
                    candidate.get("sector_memberships", []),
                )
        new_candidates = [
            phase1_candidates[candidate.get("code")]
            for candidate in raw_candidates
            if candidate.get("code") not in analyzed_codes
        ]
        if new_candidates:
            analyzed_codes.update(
                candidate.get("code") for candidate in new_candidates)
            metrics["unique_candidate_count"] = len(analyzed_codes)
            try:
                scored = run_phase2(
                    new_candidates, enable_wyckoff=True,
                    as_of_date=as_of_date,
                    source_health=source_health, metrics=metrics,
                    trade_plan_policy=trade_plan_policy)
            except TypeError as exc:
                if not any(name in str(exc) for name in (
                        "source_health", "metrics", "trade_plan_policy")):
                    raise
                scored = run_phase2(
                    new_candidates, enable_wyckoff=True,
                    as_of_date=as_of_date)
            for scored_item in scored:
                code = scored_item.get("code", "")
                raw = phase1_candidates.get(code, {})
                scored_item["sector_memberships"] = merge_sector_memberships(
                    scored_item.get("sector_memberships", [])
                    or _candidate_memberships(scored_item, sector_context),
                    raw.get("sector_memberships", []),
                )
                all_scored[code] = scored_item

        peer_cohorts = build_sector_peer_cohorts(
            list(phase1_candidates.values()))
        for code, scored_item in list(all_scored.items()):
            raw = phase1_candidates.get(code, {})
            scored_item["sector_memberships"] = merge_sector_memberships(
                scored_item.get("sector_memberships", []),
                raw.get("sector_memberships", []),
            )
            all_scored[code] = _rebind_primary_sector(
                scored_item, peer_cohorts=peer_cohorts,
                as_of_date=as_of_date)
        eligible_count = sum(
            _is_final_valid_candidate(item, min_score)
            for item in all_scored.values())
        print(
            f"  批次完成,候选 {len(all_scored)} 只,有效 {eligible_count} 只",
            file=sys.stderr,
        )
        if eligible_count >= min_candidates:
            break
    return [all_scored[code] for code in sorted(all_scored)]


def select_candidate_pool(scored, top, min_score):
    """Keep promotable candidates ahead of observation-only high scorers."""
    candidates = [
        item for item in scored
        if item.get("composite_score", 0) >= min_score
    ]
    for item in candidates:
        item["score_eligible"] = candidate_rank_score(item) >= min_score

    def selection_key(item):
        promotable = (
            item["score_eligible"]
            and item.get("data_quality", {}).get("eligible", False)
            and item.get("sector_actionable", True)
        )
        return promotable, candidate_rank_score(item)

    candidates.sort(key=selection_key, reverse=True)
    return candidates[:top]


def _signal_text(signals):
    """Render signals dict to Chinese-readable string (bools → labels)."""
    parts = []
    for k, v in signals.items():
        if k in SIGNAL_LABELS:
            parts.append(SIGNAL_LABELS[k])
        elif isinstance(v, bool):
            parts.append(k)
        else:
            parts.append(str(v))
    return "、".join(parts) or "-"


def _reason_detail(code, item):
    """Translate an internal reason code and attach the available cause data."""
    quality = item.get("data_quality", {})
    dimensions = quality.get("dimensions", {})
    expected = quality.get("as_of_date", "") or "未知"
    if code == "coverage_below_70pct":
        return f"数据覆盖率{quality.get('coverage', 0):.0%}，低于70%门槛"
    dimension_name = {
        "kline_stale": "kline",
        "capital_error": "capital",
        "fundamental_error": "fundamental",
    }.get(code)
    if dimension_name:
        dimension = dimensions.get(dimension_name, {})
        details = []
        if dimension.get("data_date"):
            details.append(f"数据日期{dimension['data_date']}")
        if code == "kline_stale":
            details.append(f"要求覆盖至{expected}")
        if dimension.get("source"):
            details.append(f"来源{dimension['source']}")
        if dimension.get("stale_reason"):
            details.append(f"原因码{dimension['stale_reason']}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return f"{REASON_LABELS[code]}{suffix}"
    return REASON_LABELS.get(code, str(code))


def _candidate_diagnostic_text(item):
    """Render data problems and other demotion causes in the final column."""
    quality = item.get("data_quality", {})
    reasons = list(item.get("observation_reasons", [])) \
        or list(quality.get("reasons", []))
    data_reasons = []
    other_reasons = []
    for code in reasons:
        target = data_reasons if code in DATA_REASON_CODES else other_reasons
        target.append(_reason_detail(code, item))

    for label, prefix in (("板块排行", "ranking"), ("板块成分", "membership")):
        quality_value = item.get(f"{prefix}_quality", "")
        source = item.get(f"{prefix}_source", "")
        errors = item.get(f"{prefix}_errors", []) or []
        if quality_value and quality_value != "good":
            cause = "、".join(str(error) for error in errors if error)
            detail = f"{label}数据质量{quality_value}"
            if source:
                detail += f"（来源{source}）"
            if cause:
                detail += f"：{cause}"
            data_reasons.append(detail)

    parts = []
    if data_reasons:
        parts.append("数据问题/异常：" + "、".join(dict.fromkeys(data_reasons)))
    else:
        parts.append("数据问题/异常：无")
    if other_reasons:
        parts.append("其他原因：" + "、".join(dict.fromkeys(other_reasons)))
    elif not data_reasons:
        parts.append("信号：" + _signal_text(item.get("signals", {})))
    wyckoff = item.get("wyckoff", {})
    signal_status = wyckoff.get("signal_status") or wyckoff.get("short_term", {}).get("signal_status", "")
    sub_phase = str(wyckoff.get("sub_phase", "")).lower()
    if sub_phase == "backup" and signal_status == "candidate":
        parts.append("维科夫状态：BU回踩待确认")
    elif sub_phase == "lps" and signal_status == "confirmed":
        parts.append("维科夫状态：LPS已确认")
    elif signal_status == "retest_pending":
        parts.append("维科夫状态：突破后回踩待确认")
    elif signal_status == "failed_breakout":
        parts.append("维科夫状态：突破失败")
    return "；".join(parts)


def _sector_text(item):
    text = item.get("sector_name", "")
    provenance = []
    for label, prefix in (("排行", "ranking"), ("成分", "membership")):
        source = item.get(f"{prefix}_source", "")
        if not source:
            continue
        data_date = item.get(f"{prefix}_data_date", "") or "未知"
        quality = item.get(f"{prefix}_quality", "") or "未知"
        provenance.append(
            f"{label} 来源 {source}｜日期 {data_date}｜质量 {quality}"
        )
    if provenance:
        return f"{text}（{'；'.join(provenance)}）"
    return text


def _long_term_structure_text(wyckoff):
    """Render the long-term phase together with a precise unavailable cause."""
    long_term = wyckoff.get("long_term", {})
    phase_name = long_term.get("phase_name", "未确认")
    reason = long_term.get("reason", "")
    return f"{phase_name}（{reason}）" if reason else phase_name


def _minor_phase_text(wyckoff):
    """Render the short-term Wyckoff A–E phase with its Chinese meaning.

    When the phase carries a trigger K-line, its date and low/close prices
    are appended so readers can see which bar satisfied the phase condition.
    """
    minor = wyckoff.get("minor_phase", {})
    name = minor.get("name", "小级别阶段未确认")
    description = minor.get("description", "")
    text = f"{name}（{description}）" if description else name
    trigger = minor.get("trigger")
    if trigger and trigger.get("date"):
        text += (
            f"；触发K线 {trigger['date']} "
            f"低{trigger['low']} 收{trigger['close']}"
        )
    return text


def _minor_phase_html(wyckoff):
    text = _minor_phase_text(wyckoff)
    minor = wyckoff.get("minor_phase", {})
    if (minor.get("code") == "D"
            and str(wyckoff.get("sub_phase", "")).lower() == "lps"
            and wyckoff.get("signal_status") == "confirmed"):
        return f"<span style='background:#fef3c7;color:#92400e;padding:2px 6px;border-radius:4px'>{text}</span>"
    return text


def _long_term_confidence_text(wyckoff):
    """Avoid presenting 0% as evidence when the long-term phase is unavailable."""
    long_term = wyckoff.get("long_term", {})
    if long_term.get("reason_code") or not long_term.get("eligible", False):
        return "-"
    confidence = long_term.get("confidence")
    return f"{confidence:.0%}" if confidence is not None else "-"


def _kline_depth_text(wyckoff):
    long_term = wyckoff.get("long_term", {})
    available = long_term.get("bars_available")
    minimum = long_term.get("minimum_bars", 250)
    return f"{available}/{minimum}" if available is not None else "未知"


TARGET_SOURCE_LABELS = {
    "resistance": "阻力位",
    "atr_projection": "ATR投射（仅观察）",
    "unavailable": "目标不可用",
}


def _target_source_audit(items):
    counts = {source: 0 for source in TARGET_SOURCE_LABELS}
    for item in items:
        source = (item.get("trade_plan") or {}).get("target_source")
        if source not in counts:
            source = "unavailable"
        counts[source] += 1
    return (
        f"目标来源审计：阻力位 {counts['resistance']}｜"
        f"ATR投射（仅观察） {counts['atr_projection']}｜"
        f"目标不可用 {counts['unavailable']}"
    )


def _trade_plan_text(item):
    """Render the additive compact trade-plan fields consistently."""
    plan = item.get("trade_plan") or {}
    entry = plan.get("entry") or {}
    stop = plan.get("stop_loss") or {}
    targets = plan.get("targets") or {}
    rr = plan.get("risk_reward") or {}
    position = plan.get("position") or {}
    if not plan:
        return "交易计划：未生成"
    source = plan.get("target_source")
    if source not in TARGET_SOURCE_LABELS:
        source = "unavailable"
    source_text = TARGET_SOURCE_LABELS[source]
    target_values = [targets.get(key) for key in
                     ("conservative", "primary", "aggressive")]
    valid_target_ladder = (
        source in {"resistance", "atr_projection"}
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in target_values
        )
        and target_values[0] < target_values[1] < target_values[2]
    )
    rr_value = rr.get("recomputed") if valid_target_ladder else None
    rr_text = (
        f"{rr_value:.2f}" if isinstance(rr_value, (int, float))
        and not isinstance(rr_value, bool)
        and math.isfinite(rr_value) else "—"
    )
    target_text = (
        "/".join(str(value) for value in target_values)
        if valid_target_ladder else "—/—/—"
    )
    reason = plan.get("target_reason")
    reason_text = f"（{reason}）" if source == "unavailable" and reason else ""
    return (
        f"交易计划：入场{entry.get('low', '-')}~{entry.get('high', '-')} | "
        f"止损{stop.get('price', '-')} | "
        f"目标{target_text} | 目标来源 {source_text}{reason_text} | "
        f"R:R {rr_text} | "
        f"仓位≤{position.get('max_portfolio_pct', '-')}% | "
        f"有效{(plan.get('validity') or {}).get('trading_sessions', '-')}个交易日"
    )


def _markdown_cell(value):
    """Escape content embedded in one Markdown table cell."""
    return str(value).replace("|", r"\|").replace("\n", " ")


def _append_candidate_table(lines, title, items, empty_text):
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append(f"> {empty_text}")
        return
    lines.extend([
        "| # | 名称(代码) | 板块 | 小级别维科夫阶段 | 短线买点 | 中线结构 | 周期结论 | 短线置信度 | 中线置信度 | K线根数/要求 | 原始分 | 质量分 | 数据维度覆盖率 | 数据问题/异常及原因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        detail = _markdown_cell(_candidate_diagnostic_text(item))
        plan_text = _markdown_cell(_trade_plan_text(item))
        lines.append(
            f"| {index} | {item['name']}({item['code']}) | "
            f"{_sector_text(item)} | {_minor_phase_text(wyckoff)} | "
            f"{wyckoff.get('sub_phase', '-')} | "
            f"{_long_term_structure_text(wyckoff)} | "
            f"{wyckoff.get('alignment', {}).get('label', '未确认')} | "
            f"{wyckoff.get('confidence', 0):.0%} | "
            f"{_long_term_confidence_text(wyckoff)} | "
            f"{_kline_depth_text(wyckoff)} | "
            f"{item['composite_score']:.1f} | "
            f"{candidate_rank_score(item):.1f} | "
            f"{quality.get('coverage', 0):.0%} | "
            f"{detail}；{plan_text} |"
        )


def load_regime_context():
    """Return market-regime summary line if today's context exists."""
    try:
        p = CACHE_DIR / "market_regime.json"
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        r = d.get("regime", {})
        return {
            "score": r.get("score"),
            "label": r.get("label", ""),
            "data_date": d.get("data_date", ""),
            "advice": r.get("advice", ""),
            "hs300_change": (
                d.get("indices", {}).get("000300.SH", {}).get("pct_chg")
            ),
            "capital_score": d.get("components", {}).get("capital", {}).get("score"),
        }
    except Exception:
        return None


def build_recommendation_policy(regime, expected_date, market_open=False):
    if not regime or regime.get("score") is None:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_missing"],
        }
    if regime.get("data_date") != expected_date:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_stale"],
        }
    score = float(regime["score"])
    capital_score = regime.get("capital_score")
    divergence = capital_score is not None and float(capital_score) < 35
    if score < 60:
        policy = {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_weak"],
        }
    elif score < 80:
        policy = {
            "mode": "waiting_trigger", "max_recommendations": 2,
            "max_portfolio_pct": 30, "reasons": [],
            "requires_sector_capital_proof": divergence,
        }
    else:
        policy = {
            "mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "reasons": [],
            "requires_sector_capital_proof": divergence,
        }
    # 盘中结果只用于观察，不能产生正式推荐或仓位建议。
    if market_open:
        previous_mode = policy.get("mode", "observation")
        policy.update({
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "provisional": True,
            "provisional_target_mode": previous_mode,
        })
        policy["reasons"] = (policy.get("reasons") or []) + ["intraday_provisional"]
    return policy


def _trade_plan_promotable(item):
    """Only a complete resistance-backed v1 buy plan is recommendable."""
    plan = item.get("trade_plan")
    targets = (plan or {}).get("targets") or {}
    target_values = [targets.get(key) for key in
                     ("conservative", "primary", "aggressive")]
    risk_reward = (plan or {}).get("risk_reward") or {}
    rr_value = risk_reward.get("recomputed")
    return (
        item.get("trade_plan_status") == "complete"
        and isinstance(plan, dict)
        and plan.get("action") == "buy"
        and plan.get("target_source") == "resistance"
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in target_values
        )
        and target_values[0] < target_values[1] < target_values[2]
        and isinstance(rr_value, (int, float))
        and not isinstance(rr_value, bool)
        and math.isfinite(rr_value)
    )


def classify_candidates(candidates, policy):
    eligible = [
        item for item in candidates
        if item.get("data_quality", {}).get("eligible", False)
        and item.get("sector_actionable", True)
        and item.get("score_eligible", True)
        and (not policy.get("requires_sector_capital_proof", False)
             or item.get("sector_capital_evidence") == "positive_verified")
        and item.get("wyckoff", {}).get("alignment", {}).get(
            "recommendation_gate", "short_term_only") != "observation"
        and (_trade_plan_promotable(item)
             if policy.get("mode") in {"actionable", "waiting_trigger"}
             else True)
    ]
    limit = policy.get("max_recommendations", 0)
    actionable = eligible[:limit] if policy.get("mode") == "actionable" else []
    waiting = eligible[:limit] if policy.get("mode") == "waiting_trigger" else []
    promoted = {item["code"] for item in actionable + waiting}
    confirmations = []
    if policy.get("mode") == "waiting_trigger":
        confirmations = [item for item in candidates
                         if item.get("data_quality", {}).get("eligible", False)
                         and item.get("score_eligible", True)
                         and item.get("wyckoff")
                         and _trade_plan_promotable(item)
                         and item.get("code") not in promoted][:2]
        confirmations = [
            dict(item, confirmation_conditions=(
                "次日板块跑赢沪深300、守住当日低点，且放量或资金/共振确认"))
            for item in confirmations
        ]
    confirmation_codes = {item["code"] for item in confirmations}
    observation = []
    for item in candidates:
        if (item.get("code") in promoted
                or item.get("code") in confirmation_codes):
            continue
        copy = dict(item)
        reasons = list(item.get("data_quality", {}).get("reasons", []))
        if not item.get("sector_actionable", True):
            reasons.append(item.get("sector_persistence_status")
                           or item.get("sector_type") or "sector_unverified")
        if policy.get("requires_sector_capital_proof", False) \
                and item.get("sector_capital_evidence") != "positive_verified":
            reasons.append("breadth_capital_divergence")
        if not item.get("score_eligible", True):
            reasons.append("quality_adjusted_below_min_score")
        if item.get("wyckoff", {}).get("alignment", {}).get(
                "recommendation_gate") == "observation":
            alignment_status = item.get("wyckoff", {}).get("alignment", {}).get("status")
            signal_status = item.get("wyckoff", {}).get("short_term", {}).get("signal_status")
            if signal_status == "retest_pending":
                reasons.append("wyckoff_retest_pending")
            elif signal_status == "failed_breakout":
                reasons.append("wyckoff_failed_breakout")
            elif alignment_status != "short_term_pending":
                reasons.append("wyckoff_countertrend")
        if (policy.get("mode") in {"actionable", "waiting_trigger"}
                and not _trade_plan_promotable(item)):
            reasons.extend(item.get("trade_plan_reasons") or [])
            if not item.get("trade_plan"):
                reasons.append("trade_plan_missing")
            elif item.get("trade_plan", {}).get("target_source") != "resistance":
                reasons.append("trade_plan_target_source_not_executable")
        if not reasons and policy.get("reasons"):
            reasons.extend(policy["reasons"])
        if not reasons:
            reasons.append("recommendation_limit")
        if policy.get("provisional") and "intraday_provisional" not in reasons:
            reasons.insert(0, "intraday_provisional")
        copy["observation_reasons"] = list(dict.fromkeys(reasons))
        observation.append(copy)
    return {
        "actionable": actionable,
        "waiting_trigger": waiting,
        "next_day_confirmation": confirmations,
        "observation": observation,
    }


def _save_recommendation_snapshot(candidates, sector_codes, policy, buckets,
                                  recommendation_date, performance=None):
    """Persist one official snapshot while keeping report generation resilient."""
    source = {
        "recommendation_date": recommendation_date,
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot_type": "provisional" if policy.get("provisional") else "formal",
        "model_version": "daily-candidates/v1",
        "policy": copy.deepcopy(policy),
        "market_regime": load_regime_context() or {},
        "sectors": copy.deepcopy(sector_codes),
        "candidates": copy.deepcopy(candidates),
        "buckets": copy.deepcopy(buckets),
        "scan_status": (performance or {}).get("scan_status", "complete"),
    }
    try:
        result = save_snapshot_if_official(source)
        return {
            "status": result.status,
            "path": str(result.path) if result.path else None,
            "content_sha256": getattr(result, "content_sha256", None),
            "reason": None,
        }
    except Exception as exc:
        return {
            "status": "save_failed",
            "path": None,
            "content_sha256": None,
            "reason": type(exc).__name__,
        }


def generate_report(candidates, sector_codes, elapsed, policy, buckets,
                    performance=None, tracking=None):
    funnel = (
        f"板块 {len(sector_codes)} → 候选 {len(candidates)} → "
        f"维科夫买点 {sum(1 for item in candidates if item.get('wyckoff'))} → "
        f"数据合格 {sum(1 for item in candidates if item.get('data_quality', {}).get('eligible'))} → "
        f"可执行 {len(buckets['actionable'])}/等待 {len(buckets['waiting_trigger'])}"
    )
    lines = [
        "# 每日候选股",
        "",
        f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | "
        f"耗时 {elapsed:.0f}s",
        "",
        f"**推荐模式**: {policy['mode']} | "
        f"推荐上限 {policy['max_recommendations']} 只 | "
        f"组合仓位上限 {policy['max_portfolio_pct']}%",
        "",
        f"**筛选漏斗**: {funnel}",
        "",
        f"**{_target_source_audit(candidates)}**",
    ]
    regime = load_regime_context()
    if regime and regime.get("score") is not None:
        lines.extend([
            "",
            f"**市场环境**: {regime['score']} {regime['label']} "
            f"(数据 {regime['data_date']}) — {regime.get('advice', '')}",
        ])
    if policy.get("reasons"):
        lines.extend(["", f"> ⚠️ 推荐降级: {', '.join(policy['reasons'])}"])
    if policy.get("provisional"):
        lines.extend([
            "",
            "> ⚠️ **盘中临时(未收盘确认)**: 当前为盘中快照,收盘后请复跑 "
            "`/daily-review` 与 `/candidates` 确认最终结论。",
        ])
    if tracking:
        lines.extend(["", f"**推荐快照追踪**: {tracking.get('status')}"
                      + (f" | {tracking.get('path')}"
                         if tracking.get("path") else "")])
    suffix = "(盘中临时,收盘确认)" if policy.get("provisional") else ""
    _append_candidate_table(
        lines, f"今日可执行{suffix}", buckets["actionable"], "今日无可执行推荐。")
    _append_candidate_table(
        lines, f"等待触发{suffix}", buckets["waiting_trigger"], "暂无等待触发标的。")
    _append_candidate_table(
        lines, "次日确认观察（非推荐）", buckets.get("next_day_confirmation", []),
        "暂无可供次日确认的观察标的。")
    _append_candidate_table(
        lines, "观察池", buckets["observation"], "观察池为空。")
    lines.extend(_performance_markdown(performance))
    lines.extend([
        "", "---", "",
        "*候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。*",
        "",
        "**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**",
    ])
    return "\n".join(lines)


def _html_candidate_rows(items):
    if not items:
        return '<tr><td colspan="14">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        detail = _candidate_diagnostic_text(item)
        plan_text = escape(_trade_plan_text(item))
        rows.append(
            f"<tr><td>{index}</td><td><strong>{item['name']}</strong><br>"
            f"<span style='color:#86868b;font-size:12px'>{item['code']}</span></td>"
            f"<td>{_sector_text(item)}</td>"
            f"<td>{_minor_phase_html(wyckoff)}</td>"
            f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
            f"<td>{_long_term_structure_text(wyckoff)}</td>"
            f"<td>{wyckoff.get('alignment', {}).get('label', '未确认')}</td>"
            f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
            f"<td>{_long_term_confidence_text(wyckoff)}</td>"
            f"<td>{_kline_depth_text(wyckoff)}</td>"
            f"<td><strong>{item['composite_score']:.1f}</strong></td>"
            f"<td><strong>{candidate_rank_score(item):.1f}</strong></td>"
            f"<td>{quality.get('coverage', 0):.0%}</td>"
            f"<td>{escape(detail)}；{plan_text}</td></tr>"
        )
    return "".join(rows)


def _generate_html(candidates, sector_codes, elapsed, ts, policy, buckets,
                   performance=None, tracking=None):
    """Lightweight HTML mirror of the MD report."""
    regime = load_regime_context()
    weak = bool(regime and regime["score"] is not None and regime["score"] < 60)
    actionable_rows = _html_candidate_rows(buckets["actionable"])
    waiting_rows = _html_candidate_rows(buckets["waiting_trigger"])
    confirmation_rows = _html_candidate_rows(buckets.get("next_day_confirmation", []))
    observation_rows = _html_candidate_rows(buckets["observation"])
    policy_note = (
        f"推荐模式 {policy['mode']} | 推荐上限 "
        f"{policy['max_recommendations']}只 | 组合仓位上限 "
        f"{policy['max_portfolio_pct']}%"
    )
    funnel_note = (
        f"筛选漏斗：板块 {len(sector_codes)} → 候选 {len(candidates)} → "
        f"维科夫买点 {sum(1 for item in candidates if item.get('wyckoff'))} → "
        f"数据合格 {sum(1 for item in candidates if item.get('data_quality', {}).get('eligible'))} → "
        f"可执行 {len(buckets['actionable'])}/等待 {len(buckets['waiting_trigger'])}"
    )
    target_audit = _target_source_audit(candidates)

    regime_html = ""
    if regime and regime["score"] is not None:
        color = {"强势": "#dc2626", "中性": "#d97706", "弱势": "#16a34a"}.get(regime["label"], "#86868b")
        weak_note = ('<p style="color:#b45309;font-weight:600">⚠️ 弱势市:候选仅作观察,'
                     '不宜建仓,等大盘站回 MA20。</p>' if weak else "")
        regime_html = (
            f"<div class='score' style='color:{color}'>市场环境 {regime['score']} {regime['label']}"
            f"<span style='font-size:14px;color:#86868b'> (数据 {regime['data_date']})</span></div>"
            f"{weak_note}"
        )

    performance_html = _performance_html(performance)
    tracking_html = (
        f"<p class='dt'>推荐快照追踪：{escape(str(tracking.get('status')))}"
        f"{(' | ' + escape(str(tracking.get('path')))) if tracking.get('path') else ''}</p>"
        if tracking else ""
    )

    provisional_banner = (
        '<p style="color:#b45309;font-weight:600;margin:8px 0">⚠️ 盘中临时(未收盘确认): '
        '当前为盘中快照,收盘后请复跑 /daily-review 与 /candidates 确认最终结论。</p>'
        if policy.get("provisional") else ""
    )
    tier_suffix = "(盘中临时,收盘确认)" if policy.get("provisional") else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日候选股 {ts}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1000px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:32px 36px}}
h1{{font-size:24px}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.score{{font-size:26px;font-weight:800;margin:14px 0}}
table{{width:100%;border-collapse:collapse;margin:12px 0;border-radius:8px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-size:13px}}
.buy{{color:#dc2626;font-weight:600}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:28px}}
</style></head><body><div class="w">
<h1>📋 每日候选股 {datetime.now().strftime('%Y-%m-%d')}</h1>
<p class="dt">生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | 耗时 {elapsed:.0f}s</p>
{regime_html}

<p class="dt">{policy_note}</p>
<p class="dt">{funnel_note}</p>
<p class="dt">{escape(target_audit)}</p>
{tracking_html}
{provisional_banner}
<h2 style="font-size:18px;margin:18px 0 8px">今日可执行{tier_suffix}</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>中线结构</th><th>周期结论</th><th>短线置信度</th><th>中线置信度</th><th>K线根数/要求</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{actionable_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">等待触发{tier_suffix}</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>中线结构</th><th>周期结论</th><th>短线置信度</th><th>中线置信度</th><th>K线根数/要求</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{waiting_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">次日确认观察（非推荐）</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>中线结构</th><th>周期结论</th><th>短线置信度</th><th>中线置信度</th><th>K线根数/要求</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{confirmation_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>中线结构</th><th>周期结论</th><th>短线置信度</th><th>中线置信度</th><th>K线根数/要求</th><th>原始分</th><th>质量分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{observation_rows}</tbody></table>
{performance_html}

<footer><p class="disc">候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。<br><strong>本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。</strong></p></footer>
</div></body></html>"""


def build_json_output(candidates, sector_codes, elapsed, policy, buckets,
                      performance=None, tracking=None):
    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "sector_count": len(sector_codes),
            "candidate_count": len(candidates),
            "elapsed_seconds": round(elapsed, 1),
            "performance": performance or {},
            "tracking": tracking or {},
        },
        "policy": policy,
        "sectors": sector_codes,
        "candidates": candidates,
        "recommendations": buckets["actionable"],
        "waiting_trigger": buckets["waiting_trigger"],
        "next_day_confirmation": buckets.get("next_day_confirmation", []),
        "observation": buckets["observation"],
    }


def main():
    parser = argparse.ArgumentParser(description="每日候选股 — 自动筛出维科夫买点候选")
    parser.add_argument("--top", type=int, default=30, help="输出上限(默认30)")
    parser.add_argument("--min-candidates", type=int, default=20,
                        help="候选数量下限,不足则扩展板块(默认20)")
    parser.add_argument("--min-score", type=float, default=50, help="最低综合分(默认50)")
    parser.add_argument("--sectors", type=str,
                        help="手动指定板块,逗号分隔(覆盖自动选板块)")
    parser.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    parser.add_argument("--html", dest="html", action="store_true", default=True,
                        help="(默认) 生成 HTML 报告")
    parser.add_argument("--no-html", dest="html", action="store_false",
                        help="不生成 HTML(仅 MD)")
    args = parser.parse_args()

    start = time.time()
    monotonic_start = time.monotonic()
    performance = {}
    source_health = RunSourceHealth()
    regime = load_regime_context()
    from fetchers.sector_data import get_last_trading_day
    current_time = datetime.now()
    last_trading_date, trading_date_source = get_last_trading_day(
        now=current_time)
    is_trading_day = _is_current_trading_day(
        last_trading_date, trading_date_source, now=current_time)
    expected_date = resolve_recommendation_date(
        now=current_time,
        regime_date=(regime or {}).get("data_date", ""),
        last_trading_date=last_trading_date or "",
        is_trading_day=is_trading_day,
    )
    policy = build_recommendation_policy(
        regime, expected_date, market_open=is_recommendation_session())

    # 板块来源
    if args.sectors:
        sector_codes = [{
            "code": c.strip(),
            "name": c.strip(),
            "absolute_hot_score": 0,
            "relative_hot_score": 0,
            "sector_type": "manual_unverified",
            "sector_actionable": False,
        } for c in args.sectors.split(",") if c.strip()]
        print(f"手动板块 {len(sector_codes)} 个: {[c['code'] for c in sector_codes]}",
              file=sys.stderr)
    else:
        print("[1/3] 拉取热点板块...", file=sys.stderr)
        ranking_start = time.monotonic()
        sector_codes = pick_hot_sectors(
            regime=regime, as_of_date=expected_date,
            source_health=source_health, metrics=performance)
        performance["sector_ranking_seconds"] = round(
            time.monotonic() - ranking_start, 3)
        performance["sector_ranking_requests"] = 1
        if not sector_codes:
            print("⚠️ 无热点板块,候选为空", file=sys.stderr)
            sys.exit(1)
        sector_preview = [
            f"{sector['name']}({sector['sector_score']:.0f})"
            for sector in sector_codes[:5]
        ]
        print(f"  热点板块 {len(sector_codes)} 个: "
              f"{sector_preview}...",
              file=sys.stderr)

    # 漏斗扫描
    print("[2/3] 维科夫漏斗扫描成分股...", file=sys.stderr)
    scored = scan_sectors(
        [c["code"] for c in sector_codes],
        min_candidates=args.min_candidates,
        min_score=args.min_score,
        as_of_date=expected_date,
        sector_context={c["code"]: c for c in sector_codes},
        source_health=source_health,
        metrics=performance,
        trade_plan_policy=policy,
    )

    # 过滤 + 排序 + 归一化到 top
    candidates = select_candidate_pool(scored, args.top, args.min_score)
    buckets = classify_candidates(candidates, policy)

    elapsed = time.time() - start
    performance = _complete_performance(
        performance, source_health, candidates, buckets, args.min_score,
        time.monotonic() - monotonic_start)
    tracking = _save_recommendation_snapshot(
        candidates, sector_codes, policy, buckets, expected_date, performance)

    if args.json:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        builders = [("json", lambda: build_json_output(
            candidates, sector_codes, elapsed, policy, buckets,
            tracking=tracking))]
        if args.html:
            builders.append(("html", lambda: _generate_html(
                candidates, sector_codes, elapsed, ts, policy, buckets,
                tracking=tracking)))
        outputs, performance = _freeze_output_envelope(
            performance, builders, run_started_at=monotonic_start)
        out = outputs["json"]
        out["meta"]["performance"] = performance
        if args.html:
            try:
                REPORTS_DIR.mkdir(parents=True, exist_ok=True)
                html = _attach_performance_audit(
                    outputs["html"], performance, "html")
                html_path = REPORTS_DIR / f"candidates-{ts}.html"
                html_path.write_text(html, encoding="utf-8")
                print(f"HTML: {html_path}", file=sys.stderr)
            except Exception as e:
                print(f"⚠️ HTML 生成失败: {e}", file=sys.stderr)
        _emit_performance_summary(performance)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # Assemble every requested format exactly once before freezing one shared
    # performance envelope.  Serialization and file writes are outside it.
    builders = [("markdown", lambda: generate_report(
        candidates, sector_codes, elapsed, policy, buckets,
        tracking=tracking))]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.html:
        builders.append(("html", lambda: _generate_html(
            candidates, sector_codes, elapsed, ts, policy, buckets,
            tracking=tracking)))
    outputs, performance = _freeze_output_envelope(
        performance, builders, run_started_at=monotonic_start)
    report = _attach_performance_audit(
        outputs["markdown"], performance, "markdown")
    print(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"candidates-{ts}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n候选报告: {path}")

    if args.html:
        try:
            html = _attach_performance_audit(
                outputs["html"], performance, "html")
            html_path = REPORTS_DIR / f"candidates-{ts}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"HTML: {html_path}")
        except Exception as e:
            print(f"⚠️ HTML 生成失败: {e}")

    _emit_performance_summary(performance)
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
