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
from analysis.wyckoff import classify_buy_point_level
from core.recommendation_quality import (
    NON_PROVIDER_STATUSES as NON_PROVIDER_ENRICHMENT_STATUSES,
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
from core.recommendation_snapshot import SnapshotConflict, SnapshotValidationError
from core.recommendation_snapshot import _normalize_for_json
DEFAULT_INITIAL_SECTOR_WINDOW = 60
DEFAULT_SECTOR_EXPANSION_STEP = 20
DEFAULT_MAX_SECTOR_EXPANSION = 120
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
    "cache_miss": "未命中有效缓存，尚未完成增强",
    "cache_stale": "缓存存在但已过期或不覆盖目标日期",
    "not_selected_for_enrichment": "未进入资金增强优先队列（预算内未选中）",
    "not_started_deadline": "达到截止时间，资金增强尚未开始",
    "source_unavailable": "资金增强源不可用，本轮未调用",
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
    "wyckoff_retest_pending": "维科夫突破后回踩，等待重新站稳箱顶",
    "wyckoff_failed_breakout": "维科夫突破失败，等待重新构筑",
    "data_quality_ineligible": "关键数据质量不合格",
}

DATA_REASON_CODES = {
    "kline_stale",
    "coverage_below_70pct",
    "secondary_data_missing",
    "capital_error",
    "fundamental_error",
    "cache_miss",
    "cache_stale",
    "not_selected_for_enrichment",
    "not_started_deadline",
    "source_unavailable",
    "stale_cache",
    "partial_realtime",
    "regime_missing",
    "regime_stale",
    "intraday_provisional",
    "data_quality_ineligible",
}

def candidate_quality_score(item):
    """Return the score used by hard eligibility gates."""
    return float(
        item.get("quality_adjusted_score", item.get("composite_score", 0))
        or 0
    )


def apply_buy_point_priority(item):
    """Materialize an auditable within-bucket execution priority score."""
    level = classify_buy_point_level(item.get("wyckoff"))
    bonus = float(level["priority_bonus"]) if level else 0.0
    quality = candidate_quality_score(item)
    item["buy_point_level"] = level["number"] if level else None
    item["buy_point_level_name"] = level["name"] if level else ""
    item["buy_point_priority_bonus"] = bonus
    item["execution_priority_score"] = round(min(100.0, quality + bonus), 1)
    return item


def candidate_rank_score(item):
    """Return within-bucket execution priority with legacy fallback."""
    return float(
        item.get("execution_priority_score", candidate_quality_score(item))
        or 0
    )


def _is_final_valid_candidate(item, min_score):
    """Single eligibility predicate shared by scan stopping and final audit."""
    return bool(
        candidate_quality_score(item) >= min_score
        and item.get("data_quality", {}).get("eligible", False)
        and item.get("sector_actionable", True)
    )


_PERFORMANCE_PHASE_FIELDS = (
    "sector_ranking_seconds", "sector_membership_seconds", "kline_seconds",
    "wyckoff_seconds", "capital_seconds", "fundamental_seconds",
    "report_seconds", "total_seconds",
)
_PERFORMANCE_FUNNEL_FIELDS = (
    "sector_universe_count", "sector_qualified_count",
    "sector_expanded_count", "batch_count", "raw_candidate_count",
    "unique_candidate_count", "wyckoff_pass_count", "final_candidate_count",
    "output_candidate_count", "final_valid_count", "data_eligible_count",
    "data_rejected_count",
    "actionable_count",
    "capital_priority_count", "capital_live_started", "capital_valid_count",
    "capital_cache_valid_count", "capital_skipped_by_budget",
    "capital_enrichment_population",
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


def _record_advisory(metrics, reason):
    """Record non-blocking provenance advice without degrading scan health."""
    reasons = metrics.setdefault("advisory_reasons", [])
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
    supplied_fields = set(completed)
    for field in _PERFORMANCE_PHASE_FIELDS:
        completed.setdefault(field, 0.0)
    for field in _PERFORMANCE_FUNNEL_FIELDS:
        completed.setdefault(field, 0)
    completed.setdefault(
        "sector_expansion_total_count",
        completed.get("sector_qualified_count", 0),
    )
    completed["final_candidate_count"] = len(candidates)
    # Alias the historical ``final`` name with the business-facing output
    # stage used in the repair plan and downstream dashboards.
    completed["output_candidate_count"] = len(candidates)
    completed["final_valid_count"] = sum(
        _is_final_valid_candidate(item, min_score) for item in candidates)
    completed["data_eligible_count"] = sum(
        bool(item.get("data_quality", {}).get("eligible", False))
        for item in candidates)
    completed["data_rejected_count"] = len(
        buckets.get("data_rejected", []))
    expanded_codes = completed.get("sector_expanded_codes") or []
    if expanded_codes:
        completed["sector_expanded_count"] = len(set(expanded_codes))
    else:
        completed.setdefault("sector_expanded_count", 0)
    completed.pop("sector_expanded_codes", None)
    completed["actionable_count"] = len(buckets.get("actionable", []))
    # Keep a useful audit even for compatibility callers that do not pass the
    # scanner's shared metrics dictionary.  Production runs populate these
    # counters directly in run_phase2; this fallback only fills absent keys.
    candidate_capital_statuses = [
        (item.get("source_evidence", {}) or {}).get("capital", {})
        for item in candidates
    ]
    inferred_capital = {
        "capital_priority_count": sum(
            status.get("status") not in {
                "cache_valid", "not_selected_for_enrichment",
                "cache_miss", "cache_stale",
            }
            for status in candidate_capital_statuses
            if status.get("status")
        ),
        "capital_live_started": sum(
            bool(status.get("attempted")) for status in candidate_capital_statuses
        ),
        "capital_valid_count": sum(
            status.get("status") in {"live_success", "cache_valid"}
            for status in candidate_capital_statuses
        ),
        "capital_cache_valid_count": sum(
            status.get("status") == "cache_valid"
            for status in candidate_capital_statuses
        ),
        "capital_skipped_by_budget": sum(
            status.get("status") in {
                "not_selected_for_enrichment", "not_started_deadline",
            }
            for status in candidate_capital_statuses
        ),
        "capital_enrichment_population": len(candidates),
    }
    capital_failure_reasons = {}
    for status in candidate_capital_statuses:
        if not status.get("attempted") \
                or status.get("status") in {"live_success", "cache_valid"}:
            continue
        reason = status.get("reason") or status.get("status") or "unknown"
        capital_failure_reasons[reason] = (
            capital_failure_reasons.get(reason, 0) + 1)
    inferred_capital["capital_failure_reasons"] = capital_failure_reasons
    for field, value in inferred_capital.items():
        if field not in supplied_fields:
            completed[field] = (
                dict(value) if field == "capital_failure_reasons"
                else int(value))
    completed["total_seconds"] = max(0.0, float(total_seconds))
    completed.setdefault("degradation_reasons", [])
    completed.setdefault("advisory_reasons", [])
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
    if "capital_failure_reasons" not in supplied_fields \
            and isinstance(source_health, RunSourceHealth):
        capital_state = snapshot.get("capital", {})
        completed["capital_failure_reasons"] = dict(
            capital_state.get("failure_reasons", {}))
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
    expansion_total = (
        performance.get("sector_expansion_total_count")
        or performance.get("sector_qualified_count")
        or performance.get("sector_universe_count", 0)
    )
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
        "**板块漏斗**: "
        f"评估 {performance.get('sector_universe_count', 0)} → "
        f"热度合格 {performance.get('sector_qualified_count', 0)} → "
        f"实际展开 {performance.get('sector_expanded_count', 0)}",
        "",
        f"**板块覆盖率**: "
        f"{float(performance.get('sector_scan_coverage', 1.0)):.1%} | "
        f"是否截断: "
        f"{'是' if performance.get('sector_expansion_truncated') else '否'}"
        + (
            f" | 如需完整展开可复跑 `--max-sector-expansion "
            f"{expansion_total}`"
            if performance.get('sector_expansion_truncated') else ""
        ),
        "",
        "**股票漏斗**: "
        f"批次 {performance.get('batch_count', 0)} → "
        f"原始 {performance.get('raw_candidate_count', 0)} → "
        f"去重 {performance.get('unique_candidate_count', 0)} → "
        f"维科夫 {performance.get('wyckoff_pass_count', 0)} → "
        f"输出 {performance.get('output_candidate_count', performance.get('final_candidate_count', 0))} → "
        f"数据合格 {performance.get('data_eligible_count', 0)} → "
        f"有效 {performance.get('final_valid_count', 0)} → "
        f"数据失效 {performance.get('data_rejected_count', 0)} → "
        f"可执行 {performance.get('actionable_count', 0)}",
        "",
        "**资金增强审计**: "
        f"优先队列 {performance.get('capital_priority_count', 0)} → "
        f"已启动 {performance.get('capital_live_started', 0)} → "
        f"有效 {performance.get('capital_valid_count', 0)}（缓存有效 "
        f"{performance.get('capital_cache_valid_count', 0)}） → "
        f"预算跳过 {performance.get('capital_skipped_by_budget', 0)} | "
        f"增强总体 {performance.get('capital_enrichment_population', 0)} | "
        f"接口失败原因 {json.dumps(performance.get('capital_failure_reasons', {}), ensure_ascii=False, sort_keys=True)}",
        "",
        f"**扫描状态**: {performance.get('scan_status', 'complete')} | "
        f"降级原因: {'、'.join(performance.get('degradation_reasons', [])) or '无'}",
        "",
        f"**辅助提示**: "
        f"{'、'.join(performance.get('advisory_reasons', [])) or '无'}",
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
    expansion_total = (
        performance.get("sector_expansion_total_count")
        or performance.get("sector_qualified_count")
        or performance.get("sector_universe_count", 0)
    )
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
    funnel_text = (
        "sectors="
        f"{performance.get('sector_universe_count', 0)}→"
        f"{performance.get('sector_qualified_count', 0)}→"
        f"{performance.get('sector_expanded_count', 0)} | "
        "stocks="
        f"batch={performance.get('batch_count', 0)}→"
        f"raw={performance.get('raw_candidate_count', 0)}→"
        f"unique={performance.get('unique_candidate_count', 0)}→"
        f"wyckoff={performance.get('wyckoff_pass_count', 0)}→"
        f"output={performance.get('output_candidate_count', performance.get('final_candidate_count', 0))}→"
        f"eligible={performance.get('data_eligible_count', 0)}→"
        f"rejected={performance.get('data_rejected_count', 0)}→"
        f"actionable={performance.get('actionable_count', 0)}"
    )
    capital_text = (
        f"capital_priority={performance.get('capital_priority_count', 0)} "
        f"capital_live_started={performance.get('capital_live_started', 0)} "
        f"capital_valid={performance.get('capital_valid_count', 0)} "
        f"capital_cache_valid={performance.get('capital_cache_valid_count', 0)} "
        f"capital_skipped_by_budget={performance.get('capital_skipped_by_budget', 0)} "
        f"capital_enrichment_population={performance.get('capital_enrichment_population', 0)} "
        f"capital_failure_reasons={json.dumps(performance.get('capital_failure_reasons', {}), ensure_ascii=False, sort_keys=True)}"
    )
    scan_status = escape(str(performance.get("scan_status", "complete")))
    degradation_reasons = escape(
        "、".join(performance.get("degradation_reasons", [])) or "无")
    advisory_reasons = escape(
        "、".join(performance.get("advisory_reasons", [])) or "无")
    coverage_text = (
        f"板块覆盖率={float(performance.get('sector_scan_coverage', 1.0)):.1%} | "
        f"是否截断={'是' if performance.get('sector_expansion_truncated') else '否'}"
        + (
            f" | 完整展开可复跑 --max-sector-expansion "
            f"{expansion_total}"
            if performance.get('sector_expansion_truncated') else ""
        )
    )
    return (
        "<section><h2 style='font-size:18px;margin:18px 0 8px'>"
        "性能与数据源审计</h2>"
        f"<p class='dt'>{phase_text}</p><p class='dt'>{funnel_text}</p>"
        f"<p class='dt'>{escape(coverage_text)}</p>"
        f"<p class='dt'>{escape(capital_text)}</p>"
        f"<p class='dt'>扫描状态={scan_status} | 降级原因={degradation_reasons}</p>"
        f"<p class='dt'>辅助提示={advisory_reasons}</p>"
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
    capital_text = (
        f"capital_priority={performance.get('capital_priority_count', 0)} "
        f"capital_live_started={performance.get('capital_live_started', 0)} "
        f"capital_valid={performance.get('capital_valid_count', 0)} "
        f"capital_cache_valid={performance.get('capital_cache_valid_count', 0)} "
        f"capital_skipped_by_budget={performance.get('capital_skipped_by_budget', 0)} "
        f"capital_enrichment_population={performance.get('capital_enrichment_population', 0)} "
        f"capital_failure_reasons={json.dumps(performance.get('capital_failure_reasons', {}), ensure_ascii=False, sort_keys=True)}"
    )
    print(
        f"[performance] {phase_text} "
        f"sectors={performance.get('sector_universe_count', 0)}->"
        f"{performance.get('sector_qualified_count', 0)}->"
        f"{performance.get('sector_expanded_count', 0)} "
        f"batches={performance.get('batch_count', 0)} "
        f"raw={performance.get('raw_candidate_count', 0)} "
        f"unique={performance.get('unique_candidate_count', 0)} "
        f"wyckoff={performance.get('wyckoff_pass_count', 0)} "
        f"output={performance.get('output_candidate_count', performance.get('final_candidate_count', 0))} "
        f"data_eligible={performance.get('data_eligible_count', 0)} "
        f"data_rejected={performance.get('data_rejected_count', 0)} "
        f"final_valid={performance.get('final_valid_count', 0)} "
        f"actionable={performance.get('actionable_count', 0)} "
        f"{capital_text} "
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


PERSISTENCE_LOOKBACK_DAYS = 10
MIN_PERSISTENCE_COVERAGE_DAYS = 2
EMERGING_STREAK_DAYS = 2
MAINLINE_STREAK_DAYS = 3


def _valid_persistence_date(value, as_of_date=""):
    """Accept only strict weekday dates at or before the analysis date."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    if parsed.strftime("%Y-%m-%d") != value or parsed.weekday() >= 5:
        return False
    reference = as_of_date or datetime.now().strftime("%Y-%m-%d")
    return value <= reference


def _persistence_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalise_persistence_history(history, as_of_date=""):
    """Normalize legacy list snapshots and candidate snapshot records."""
    if not isinstance(history, dict):
        return []
    raw_history = history.get("snapshots")
    if not isinstance(raw_history, dict):
        raw_history = history

    records = []
    for date_key, payload in raw_history.items():
        if not _valid_persistence_date(date_key, as_of_date):
            continue
        if isinstance(payload, list):
            records.append({
                "date": date_key,
                "rows": [row for row in payload if isinstance(row, dict)],
                "complete": True,
                "quality": "good",
                "valid": True,
                "legacy": True,
                "coverage_eligible": False,
            })
            continue
        if not isinstance(payload, dict):
            continue
        rows = payload.get("sectors", payload.get("rows", []))
        if not isinstance(rows, list):
            rows = []
        data_date = payload.get("data_date", date_key)
        quality = payload.get("quality", payload.get("source_quality", "good"))
        records.append({
            "date": date_key,
            "rows": [row for row in rows if isinstance(row, dict)],
            "complete": payload.get("complete") is True,
            "quality": quality,
            "valid": data_date == date_key,
            "legacy": False,
            "coverage_eligible": True,
        })
    records.sort(key=lambda record: record["date"])
    return records[-PERSISTENCE_LOOKBACK_DAYS:]


def _normalise_current_snapshot(current_snapshot, as_of_date=""):
    """Normalize the in-memory ranking used for the current recommendation."""
    if current_snapshot is None:
        return None
    if isinstance(current_snapshot, list):
        date_key = as_of_date
        rows = current_snapshot
        complete = True
        quality = "good"
    elif isinstance(current_snapshot, dict):
        date_key = current_snapshot.get("data_date", "") or as_of_date
        rows = current_snapshot.get(
            "sectors", current_snapshot.get("rows", []))
        complete = current_snapshot.get("complete") is True
        quality = current_snapshot.get(
            "quality", current_snapshot.get("source_quality", "good"))
    else:
        return None
    if as_of_date and date_key != as_of_date:
        return None
    if not _valid_persistence_date(date_key, as_of_date):
        return None
    if not isinstance(rows, list):
        rows = []
    return {
        "date": date_key,
        "rows": [row for row in rows if isinstance(row, dict)],
        "complete": complete,
        "quality": quality,
        "valid": True,
        "legacy": False,
        "coverage_eligible": True,
    }


def _persistence_observation(record, sector_code, min_hot):
    """Return hot/cold evidence, or None when this date is unknown."""
    if not record.get("valid") or record.get("complete") is not True \
            or record.get("quality", "good") != "good":
        return None
    row = next((item for item in record.get("rows", [])
                if item.get("code") == sector_code), None)
    if row is None:
        # The legacy Top-30 file only supplies positive evidence.  Absence is
        # deliberately unknown, not a cold observation.
        return None
    relative = row.get("relative_hot_score", row.get("hot_score"))
    relative = _persistence_float(relative)
    absolute = _persistence_float(row.get("absolute_hot_score"))
    if record.get("legacy"):
        return {"state": "hot", "value": relative or 0.0, "row": row}
    if absolute is None:
        absolute = relative
    if absolute is None:
        return None
    return {
        "state": "hot" if absolute >= min_hot else "cold",
        "value": relative if relative is not None else absolute,
        "row": row,
    }


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


def enrich_sector_context(ranked, history, hs300_change=None, as_of_date="",
                          current_snapshot=None, min_hot=45):
    """Attach strength and persistence using separate coverage/hot evidence."""
    records = _normalise_persistence_history(history, as_of_date=as_of_date)
    current_record = _normalise_current_snapshot(
        current_snapshot, as_of_date=as_of_date)
    if current_record:
        records_by_date = {
            record["date"]: record for record in records
        }
        records_by_date[current_record["date"]] = current_record
        records = [records_by_date[date_key]
                   for date_key in sorted(records_by_date)[-PERSISTENCE_LOOKBACK_DAYS:]]
    enriched = []
    for position, source in enumerate(ranked, start=1):
        sector = dict(source)
        sector["ranking_position"] = (
            sector.get("ranking_position") or position)
        code = sector.get("code", "")
        observations = [
            _persistence_observation(record, code, min_hot)
            for record in records
        ]
        known_observations = [
            observation for observation in observations if observation
        ]
        hot_values = [observation["value"] for observation in known_observations]
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

        history_window_days = len(records)
        history_coverage_days = sum(
            record.get("valid")
            and record.get("complete") is True
            and record.get("quality", "good") == "good"
            and record.get("coverage_eligible") is True
            and bool(record.get("rows"))
            for record in records
        )
        sector_observed_days = len(known_observations)
        hot_appearance_days = sum(
            observation["state"] == "hot"
            for observation in known_observations
        )
        hot_streak = 0
        for observation in reversed(observations):
            if not observation or observation["state"] != "hot":
                break
            hot_streak += 1
        dates = [record["date"] for record in records]
        history_current = bool(records) and (
            not as_of_date or dates[-1] == as_of_date
        )
        latest_present = bool(
            history_current and observations
            and observations[-1] is not None
        )
        latest_hot = bool(latest_present and observations[-1]["state"] == "hot")
        short_persistence = (
            round(sum(hot_values) / len(hot_values), 1)
            if len(hot_values) >= 2 else 0.0
        )
        if not persistence_values:
            persistence = short_persistence
        classification_persistence = avg3 if avg3 is not None else short_persistence
        history_insufficient = (
            history_coverage_days < MIN_PERSISTENCE_COVERAGE_DAYS
        )
        if latest_hot and history_coverage_days >= MAINLINE_STREAK_DAYS \
                and hot_streak >= MAINLINE_STREAK_DAYS \
                and classification_persistence >= 60 \
                and (relative_strength is None or relative_strength >= 0):
            sector_type = "mainline"
        elif latest_hot and history_coverage_days >= EMERGING_STREAK_DAYS \
                and hot_streak >= EMERGING_STREAK_DAYS \
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
        recent_capital_entries = known_observations[-5:]
        net_flows = [
            float(observation["row"].get(
                "net_flow", observation["row"].get("main_force_net")))
            for observation in recent_capital_entries
            if observation["row"].get(
                "net_flow", observation["row"].get("main_force_net"))
            is not None
        ]
        capital_persistence = 50.0
        capital_positive_days = 0
        capital_streak = 0
        if net_flows:
            capital_positive_days = sum(value > 0 for value in net_flows)
            denominator = max(1, len(recent_capital_entries))
            for observation in reversed(recent_capital_entries):
                flow = observation["row"].get(
                    "net_flow", observation["row"].get("main_force_net"))
                if flow is None or float(flow) <= 0:
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
            "history_window_days": history_window_days,
            "history_coverage_days": history_coverage_days,
            "sector_observed_days": sector_observed_days,
            "hot_appearance_days": hot_appearance_days,
            "hot_streak": hot_streak,
            # Compatibility alias: this now explicitly means hot appearances.
            "persistence_days": hot_appearance_days,
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
            "persistence_3d": candidate.get("sector_persistence_3d"),
            "persistence_5d": candidate.get("sector_persistence_5d"),
            "persistence_10d": candidate.get("sector_persistence_10d"),
            "history_window_days": candidate.get("history_window_days"),
            "history_coverage_days": candidate.get("history_coverage_days"),
            "sector_observed_days": candidate.get("sector_observed_days"),
            "hot_appearance_days": candidate.get("hot_appearance_days"),
            "hot_streak": candidate.get("hot_streak"),
            "persistence_days": candidate.get("persistence_days"),
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
        "sector_persistence_3d": primary.get("persistence_3d"),
        "sector_persistence_5d": primary.get("persistence_5d"),
        "sector_persistence_10d": primary.get("persistence_10d"),
        "history_window_days": primary.get("history_window_days"),
        "history_coverage_days": primary.get("history_coverage_days"),
        "sector_observed_days": primary.get("sector_observed_days"),
        "hot_appearance_days": primary.get("hot_appearance_days"),
        "hot_streak": primary.get("hot_streak"),
        "persistence_days": primary.get("persistence_days"),
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
        "membership_cache_at": primary.get("membership_cache_at", ""),
        "membership_cache_age_hours": primary.get(
            "membership_cache_age_hours"),
        "membership_cache_tier": primary.get("membership_cache_tier", ""),
        "membership_fallback_reason": primary.get(
            "membership_fallback_reason", ""),
        "membership_provider_attempts": primary.get(
            "membership_provider_attempts", 0),
        "membership_fetch_evidence": copy.deepcopy(
            primary.get("membership_fetch_evidence", {})),
        "sector_memberships": memberships,
    })
    evidence = rebound.setdefault("source_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        rebound["source_evidence"] = evidence
    evidence["membership"] = copy.deepcopy(
        primary.get("membership_fetch_evidence", {}))

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
        commit_candidate_sector_snapshot,
        get_sector_rankings,
        load_candidate_sector_history,
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
        # Prefer a date supplied by the upstream ranking payload.  ``as_of``
        # is an explicit caller contract; never invent a date from the local
        # wall clock at this boundary.
        "data_date": live_meta.get("data_date", "") or as_of_date,
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
    universe_count = live_meta.get("total_sectors")
    try:
        universe_count = int(universe_count)
    except (TypeError, ValueError):
        universe_count = len(rankings.get("sectors", []))
    metrics["sector_universe_count"] = max(0, universe_count)
    ranked_universe = rank_hot_sectors(
        rankings, top_n=None, min_stocks=min_stocks)
    for sector in ranked_universe:
        sector.update({
            "ranking_source": ranking_meta["source"],
            "ranking_data_date": ranking_meta["data_date"],
            "ranking_quality": ranking_meta["quality"],
            "ranking_errors": ranking_meta["errors"],
        })
    if active and live_meta.get("complete", False) and as_of_date:
        try:
            commit_candidate_sector_snapshot(
                rankings,
                data_date=as_of_date,
                min_stocks=min_stocks,
                min_up_ratio=0.15,
            )
        except (OSError, TypeError, ValueError) as exc:
            _record_degradation(
                metrics,
                f"candidate_sector_snapshot_write_error:{type(exc).__name__}",
            )
    ranked = (
        ranked_universe[:top_n] if top_n is not None else ranked_universe
    )
    qualified = [
        sector for sector in ranked
        if sector.get("absolute_hot_score", 0) >= min_hot
    ]
    metrics["sector_qualified_count"] = len(qualified)
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
                _record_advisory(metrics, "resonance_stale:date_mismatch")
        except Exception as exc:
            resonance_quality = "error"
            resonance_reason = type(exc).__name__
            _record_degradation(
                metrics, f"resonance_error:{type(exc).__name__}")
    for sector in qualified:
        sector["resonance_quality"] = resonance_quality
        sector["resonance_reason"] = resonance_reason
    hs300_change = (regime or {}).get("hs300_change")
    history_load_errors = []
    candidate_history = load_candidate_sector_history(
        days=PERSISTENCE_LOOKBACK_DAYS, errors=history_load_errors)
    for reason in history_load_errors:
        _record_degradation(
            metrics, f"candidate_sector_history_load_error:{reason}")
    legacy_history = load_snapshot_history(days=PERSISTENCE_LOOKBACK_DAYS)
    persistence_history = dict(legacy_history or {})
    persistence_history.update(candidate_history or {})
    current_snapshot = None
    if (ranking_meta.get("quality") == "good"
            and ranking_meta.get("data_date") == expected_date):
        current_snapshot = {
            "data_date": ranking_meta["data_date"],
            "complete": True,
            "quality": "good",
            "sectors": ranked_universe,
        }
    enriched = enrich_sector_context(
        qualified,
        persistence_history,
        hs300_change=hs300_change,
        as_of_date=expected_date,
        current_snapshot=current_snapshot,
        min_hot=min_hot,
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
    # This is the qualified sector list handed to the stock-expansion stage;
    # actual expansion count is recorded in scan_sectors below.
    metrics["sector_qualified_count"] = len(enriched)
    return enriched


def scan_sectors(sector_codes, batch_size=4, per_sector=25,
                 min_candidates=20, min_score=50, as_of_date="",
                 sector_context=None, source_health=None, metrics=None,
                 capital_top=30,
                 initial_sector_window=DEFAULT_INITIAL_SECTOR_WINDOW,
                 sector_expansion_step=DEFAULT_SECTOR_EXPANSION_STEP,
                 max_sector_expansion=DEFAULT_MAX_SECTOR_EXPANSION):
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
    metrics.setdefault("sector_expanded_codes", [])
    if isinstance(source_health, RunSourceHealth):
        # Membership is the fan-out stage. Finish a bounded first window
        # before starting expensive per-stock phase 2 so late cache-only
        # fallback cannot starve the leading sectors. Expand only when the
        # current window has not produced enough eligible candidates.
        initial_window = max(1, int(initial_sector_window))
        expansion_step = max(1, int(sector_expansion_step))
        expansion_limit = max(1, int(max_sector_expansion))
        scan_limit = min(len(ordered_sector_codes), expansion_limit)
        total_sector_count = len(ordered_sector_codes)
        metrics["sector_expansion_total_count"] = total_sector_count
        metrics["sector_scan_coverage"] = round(
            scan_limit / max(1, total_sector_count), 4)
        metrics["sector_expansion_truncated"] = total_sector_count > scan_limit
        if len(ordered_sector_codes) > scan_limit:
            metrics["sector_expansion_limit"] = expansion_limit
            reasons = metrics.setdefault("degradation_reasons", [])
            reason = f"sector_expansion_capped:{expansion_limit}"
            if reason not in reasons:
                reasons.append(reason)
        windows = []
        cursor = 0
        window_size = min(initial_window, scan_limit)
        while cursor < scan_limit:
            windows.append((cursor, window_size))
            cursor += window_size
            window_size = min(expansion_step, scan_limit - cursor)
    else:
        metrics["sector_expansion_total_count"] = len(ordered_sector_codes)
        metrics["sector_scan_coverage"] = 1.0
        metrics["sector_expansion_truncated"] = False
        windows = [
            (i, min(batch_size, len(ordered_sector_codes) - i))
            for i in range(0, len(ordered_sector_codes), batch_size)
        ]

    for i, window_size in windows:
        batch = ordered_sector_codes[i:i + window_size]
        metrics["sector_expanded_codes"].extend(batch)
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
                    top=capital_top, min_candidates=min_candidates)
            except TypeError as exc:
                if not any(name in str(exc) for name in (
                        "source_health", "metrics",
                        "top", "min_candidates")):
                    raise
                try:
                    scored = run_phase2(
                        new_candidates, enable_wyckoff=True,
                        as_of_date=as_of_date,
                        top=capital_top, min_candidates=min_candidates)
                except TypeError as compat_exc:
                    if not any(name in str(compat_exc) for name in (
                            "top", "min_candidates")):
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
        apply_buy_point_priority(item)
        item["score_eligible"] = candidate_quality_score(item) >= min_score

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
    if dimension_name is None and code in {
        "cache_miss", "cache_stale", "not_selected_for_enrichment",
        "not_started_deadline", "source_unavailable"}:
        evidence_by_source = item.get("source_evidence", {})
        for source in ("capital", "fundamental"):
            evidence = evidence_by_source.get(source, {}) \
                if isinstance(evidence_by_source, dict) else {}
            dimension = dimensions.get(source, {})
            if (evidence.get("status") == code
                    or dimension.get("source_status") == code):
                dimension_name = source
                break
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
        evidence = item.get("source_evidence", {})
        evidence = evidence.get(dimension_name, {}) \
            if isinstance(evidence, dict) else {}
        if evidence.get("attempted"):
            details.append("接口已调用")
        elif code in NON_PROVIDER_ENRICHMENT_STATUSES \
                and evidence.get("attempted") is False:
            details.append("未调用")
        failure_chain = evidence.get("failure_chain", [])
        if isinstance(failure_chain, list):
            chain_text = "→".join(
                f"{entry.get('source', 'unknown')}:{entry.get('reason', 'unknown')}"
                for entry in failure_chain
                if isinstance(entry, dict))
            if chain_text:
                details.append(f"失败链路{chain_text}")
        if evidence.get("reason"):
            reason_label = (
                "调度原因码" if code in NON_PROVIDER_ENRICHMENT_STATUSES
                else "抓取原因码")
            details.append(f"{reason_label}{evidence['reason']}")
        if evidence.get("status") and evidence.get("status") != code:
            details.append(f"状态码{evidence['status']}")
        if evidence.get("provider_attempts"):
            details.append(f"Provider尝试{evidence['provider_attempts']}次")
        if evidence.get("cache_used"):
            details.append("已回退缓存")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return f"{REASON_LABELS[code]}{suffix}"
    return REASON_LABELS.get(code, str(code))


def _candidate_diagnostic_text(item):
    """Render data problems, transient status, and demotion causes."""
    quality = item.get("data_quality", {})
    reasons = list(item.get("observation_reasons", [])) \
        or list(quality.get("reasons", []))
    data_reasons = []
    other_reasons = []
    transient_reasons = []
    for code in reasons:
        if code == "intraday_provisional":
            transient_reasons.append(_reason_detail(code, item))
            continue
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
            evidence = item.get("source_evidence", {})
            evidence = evidence.get(prefix, {}) \
                if isinstance(evidence, dict) else {}
            fallback_reason = item.get(
                f"{prefix}_fallback_reason", "")
            reason = evidence.get("reason") or fallback_reason
            if reason and reason != "cache_only":
                detail += f"，实时回退原因码{reason}"
            age_hours = item.get(f"{prefix}_cache_age_hours")
            if source == "cache" and isinstance(age_hours, (int, float)):
                detail += f"，缓存年龄{age_hours:.1f}小时"
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
    if transient_reasons:
        parts.append("盘中临时状态：" + "、".join(dict.fromkeys(transient_reasons)))
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


def _sector_persistence_text(item):
    """Explain history coverage separately from hot-sector appearances."""
    values = (
        item.get("history_window_days"),
        item.get("history_coverage_days"),
        item.get("hot_appearance_days"),
        item.get("hot_streak"),
    )
    if all(value is None for value in values):
        return ""
    coverage = item.get("history_coverage_days")
    window = item.get("history_window_days")
    appearances = item.get("hot_appearance_days")
    if appearances is None:
        appearances = item.get("persistence_days", 0)
    streak = item.get("hot_streak", 0)
    if coverage is None:
        coverage = 0
    if window is None:
        window = coverage
    status = (
        item.get("sector_persistence_status")
        or item.get("persistence_status")
        or item.get("sector_type", "")
    )
    status_label = {
        "history_insufficient": "历史不足",
        "single_day_pulse": "单日脉冲",
        "verified": "已验证",
        "mainline": "主线",
        "emerging": "新兴",
    }.get(status, status or "未标记")
    return (
        f"持续性：快照覆盖 {coverage}/{window}｜"
        f"热点出现 {appearances}/{window}｜连续 {streak} 日｜{status_label}"
    )


def _sector_text(item):
    text = item.get("sector_name", "")
    persistence = _sector_persistence_text(item)
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
    details = ([persistence] if persistence else []) + provenance
    if details:
        return f"{text}（{'；'.join(details)}）"
    return text


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
    return _minor_phase_text(wyckoff)


def _wyckoff_buy_level(wyckoff):
    """Return the confirmed execution level used by the actionable HTML table."""
    level = classify_buy_point_level(wyckoff)
    if level is None:
        return None
    return {
        **level,
        "css_class": f"wyckoff-buy-level-{level['number']}",
    }


def _markdown_cell(value):
    """Escape content embedded in one Markdown table cell."""
    return str(value).replace("|", r"\|").replace("\n", " ")


def _append_candidate_table(lines, title, items, empty_text):
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append(f"> {empty_text}")
        return
    lines.extend([
        "| # | 名称(代码) | 板块 | 小级别维科夫阶段 | 短线买点 | 短线置信度 | 原始分 | 质量分 | 优先分 | 数据维度覆盖率 | 数据问题/异常及原因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        detail = _markdown_cell(_candidate_diagnostic_text(item))
        lines.append(
            f"| {index} | {item['name']}({item['code']}) | "
            f"{_sector_text(item)} | {_minor_phase_text(wyckoff)} | "
            f"{wyckoff.get('sub_phase', '-')} | "
            f"{wyckoff.get('confidence', 0):.0%} | "
            f"{item['composite_score']:.1f} | "
            f"{candidate_quality_score(item):.1f} | "
            f"{candidate_rank_score(item):.1f} | "
            f"{quality.get('coverage', 0):.0%} | "
            f"{detail} |"
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
        policy = {
            "mode": "observation", "max_recommendations": 0,
            "reasons": ["regime_missing"],
        }
    elif regime.get("data_date") != expected_date:
        policy = {
            "mode": "observation", "max_recommendations": 0,
            "reasons": ["regime_stale"],
        }
    else:
        score = float(regime["score"])
        capital_score = regime.get("capital_score")
        divergence = capital_score is not None and float(capital_score) < 35
        if score < 60:
            policy = {
                "mode": "observation", "max_recommendations": 0,
                "reasons": ["regime_weak"],
            }
        elif score < 80:
            policy = {
                "mode": "waiting_trigger", "max_recommendations": 2,
                "reasons": [],
                "requires_sector_capital_proof": divergence,
            }
        else:
            policy = {
                "mode": "actionable", "max_recommendations": 5,
                "reasons": [],
                "requires_sector_capital_proof": divergence,
            }
    # 盘中结果仍标记为临时，不写入正式推荐历史；是否可执行由市场层级和候选资格决定。
    if market_open:
        previous_mode = policy.get("mode", "observation")
        policy.update({
            "provisional": True,
            "provisional_target_mode": previous_mode,
        })
        policy["reasons"] = (policy.get("reasons") or []) + ["intraday_provisional"]
    return policy


def _short_term_observation_reason(item):
    signal_status = item.get("wyckoff", {}).get("short_term", {}).get(
        "signal_status")
    return {
        "retest_pending": "wyckoff_retest_pending",
        "failed_breakout": "wyckoff_failed_breakout",
    }.get(signal_status)


def classify_candidates(candidates, policy):
    data_rejected = []
    eligible_candidates = []
    for item in candidates:
        quality = item.get("data_quality", {})
        if quality.get("eligible", False):
            eligible_candidates.append(item)
            continue
        rejected = dict(item)
        reasons = list(quality.get("reasons", []))
        if not reasons:
            reasons.append("data_quality_ineligible")
        rejected["observation_reasons"] = list(dict.fromkeys(reasons))
        data_rejected.append(rejected)

    eligible = [
        item for item in eligible_candidates
        if item.get("sector_actionable", True)
        and item.get("score_eligible", True)
        and (not policy.get("requires_sector_capital_proof", False)
             or item.get("sector_capital_evidence") == "positive_verified")
        and _short_term_observation_reason(item) is None
    ]
    limit = policy.get("max_recommendations", 0)
    actionable = eligible[:limit] if policy.get("mode") == "actionable" else []
    waiting = eligible[:limit] if policy.get("mode") == "waiting_trigger" else []
    promoted = {item["code"] for item in actionable + waiting}
    confirmations = []
    if policy.get("mode") == "waiting_trigger":
        confirmations = [item for item in eligible_candidates
                         if item.get("data_quality", {}).get("eligible", False)
                         and item.get("score_eligible", True)
                         and item.get("wyckoff")
                         and _short_term_observation_reason(item) is None
                         and item.get("code") not in promoted][:2]
        confirmations = [
            dict(item, confirmation_conditions=(
                "次日板块跑赢沪深300、守住当日低点，且放量或资金/共振确认"))
            for item in confirmations
        ]
    confirmation_codes = {item["code"] for item in confirmations}
    observation = []
    for item in eligible_candidates:
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
        short_term_reason = _short_term_observation_reason(item)
        if short_term_reason:
            reasons.append(short_term_reason)
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
        "data_rejected": data_rejected,
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
            "reason": getattr(result, "reason", None),
            "normalization_warnings": getattr(
                result, "normalization_warnings", []),
        }
    except SnapshotConflict as exc:
        return {
            "status": "conflict",
            "path": None,
            "content_sha256": None,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    except SnapshotValidationError as exc:
        return {
            "status": "validation_failed",
            "path": None,
            "content_sha256": None,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    except OSError as exc:
        return {
            "status": "write_failed",
            "path": None,
            "content_sha256": None,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    except Exception as exc:
        return {
            "status": "save_failed",
            "path": None,
            "content_sha256": None,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }


def generate_report(candidates, sector_codes, elapsed, policy, buckets,
                    performance=None, tracking=None):
    performance = performance or {}
    sector_universe = performance.get("sector_universe_count",
                                      len(sector_codes))
    sector_qualified = performance.get("sector_qualified_count",
                                       len(sector_codes))
    sector_expanded = performance.get("sector_expanded_count",
                                      len(sector_codes))
    funnel = (
        f"板块评估 {sector_universe} → 热度合格 {sector_qualified} → "
        f"实际展开 {sector_expanded} → 候选 {len(candidates)} → "
        f"维科夫买点 {sum(1 for item in candidates if item.get('wyckoff'))} → "
        f"数据合格 {sum(1 for item in candidates if item.get('data_quality', {}).get('eligible'))} → "
        f"可执行 {len(buckets['actionable'])}/等待 {len(buckets['waiting_trigger'])}"
    )
    lines = [
        "# 每日候选股",
        "",
        f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"实际展开板块 {sector_expanded} 个 | 候选 {len(candidates)} 只 | "
        f"耗时 {elapsed:.0f}s",
        "",
        f"**推荐模式**: {policy['mode']} | "
        f"推荐上限 {policy['max_recommendations']} 只 | "
        "候选资格仅依据市场、数据、板块和维科夫筛选",
        "",
        "优先分 = 质量分 + 严格买点奖励（一级 +1、二级 +3、三级 +2）；"
        "仅用于同一推荐层级内排序，不改变硬门槛。",
        "",
        f"**筛选漏斗**: {funnel}",
    ]
    regime = load_regime_context()
    if regime and regime.get("score") is not None:
        lines.extend([
            "",
            f"**市场环境**: {regime['score']} {regime['label']} "
            f"(数据 {regime['data_date']})",
        ])
    downgrade_reasons = [
        reason for reason in policy.get("reasons", [])
        if reason != "intraday_provisional"
    ]
    if downgrade_reasons:
        lines.extend(["", f"> ⚠️ 推荐降级: {', '.join(downgrade_reasons)}"])
    if policy.get("provisional"):
        lines.extend([
            "",
            "> ⚠️ **盘中临时(未收盘确认)**: 当前为盘中快照,收盘后请复跑 "
            "`/daily-review` 与 `/candidates` 确认最终结论。",
        ])
    if tracking:
        tracking_text = f"**推荐快照追踪**: {tracking.get('status')}"
        if tracking.get("path"):
            tracking_text += f" | {tracking.get('path')}"
        if tracking.get("error_type") or tracking.get("reason"):
            tracking_text += (
                f" | {tracking.get('error_type', '')}: "
                f"{tracking.get('reason', '')}"
            )
        if tracking.get("normalization_warnings"):
            tracking_text += (
                f" | 规范化字段 {len(tracking['normalization_warnings'])}"
            )
        lines.extend(["", tracking_text])
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
    _append_candidate_table(
        lines, "数据失效/待修复", buckets.get("data_rejected", []),
        "无数据失效候选。")
    lines.extend(_performance_markdown(performance))
    lines.extend([
        "", "---", "",
        "*候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。*",
        "",
        "**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**",
    ])
    return "\n".join(lines)


_BUY_LEVEL_DISPLAY_CONTEXTS = {"none", "actionable", "observation"}


def _html_candidate_rows(items, buy_level_display="none"):
    if buy_level_display not in _BUY_LEVEL_DISPLAY_CONTEXTS:
        raise ValueError(
            f"unsupported buy level display: {buy_level_display}")
    if not items:
        return '<tr><td colspan="11">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        buy_level = (
            _wyckoff_buy_level(wyckoff)
            if buy_level_display != "none"
            else None
        )
        if not buy_level:
            row_class = ""
            buy_level_badge = ""
        elif buy_level_display == "observation":
            row_class = (
                " class='wyckoff-observation-buy-level-"
                f"{buy_level['number']}'"
            )
            buy_level_badge = (
                "<br><span class='wyckoff-buy-level-badge observation'>"
                f"潜在{buy_level['name']} · {buy_level['label']} · "
                "观察｜不可执行</span>"
            )
        else:
            row_class = f" class='{buy_level['css_class']}'"
            buy_level_badge = (
                "<br><span class='wyckoff-buy-level-badge'>"
                f"{buy_level['name']} · {buy_level['label']} · "
                "已确认</span>"
            )
        quality = item.get("data_quality", {})
        detail = _candidate_diagnostic_text(item)
        rows.append(
            f"<tr{row_class}><td>{index}</td><td><strong>{item['name']}</strong><br>"
            f"<span style='color:#86868b;font-size:12px'>{item['code']}</span>"
            f"{buy_level_badge}</td>"
            f"<td>{_sector_text(item)}</td>"
            f"<td>{_minor_phase_html(wyckoff)}</td>"
            f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
            f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
            f"<td><strong>{item['composite_score']:.1f}</strong></td>"
            f"<td><strong>{candidate_quality_score(item):.1f}</strong></td>"
            f"<td><strong>{candidate_rank_score(item):.1f}</strong></td>"
            f"<td>{quality.get('coverage', 0):.0%}</td>"
            f"<td>{escape(detail)}</td></tr>"
        )
    return "".join(rows)


def _generate_html(candidates, sector_codes, elapsed, ts, policy, buckets,
                   performance=None, tracking=None):
    """Lightweight HTML mirror of the MD report."""
    performance = performance or {}
    regime = load_regime_context()
    weak = bool(regime and regime["score"] is not None and regime["score"] < 60)
    actionable_rows = _html_candidate_rows(
        buckets["actionable"], buy_level_display="actionable")
    waiting_rows = _html_candidate_rows(buckets["waiting_trigger"])
    confirmation_rows = _html_candidate_rows(buckets.get("next_day_confirmation", []))
    observation_rows = _html_candidate_rows(
        buckets["observation"], buy_level_display="observation")
    rejected_rows = _html_candidate_rows(buckets.get("data_rejected", []))
    sector_universe = performance.get("sector_universe_count",
                                      len(sector_codes))
    sector_qualified = performance.get("sector_qualified_count",
                                       len(sector_codes))
    sector_expanded = performance.get("sector_expanded_count",
                                      len(sector_codes))
    policy_note = (
        f"推荐模式 {policy['mode']} | 推荐上限 "
        f"{policy['max_recommendations']}只 | 候选资格由筛选门槛决定"
    )
    priority_note = (
        "优先分 = 质量分 + 严格买点奖励（一级 +1、二级 +3、三级 +2）；"
        "仅用于同一推荐层级内排序，不改变硬门槛。"
    )
    funnel_note = (
        f"筛选漏斗：板块评估 {sector_universe} → 热度合格 {sector_qualified} "
        f"→ 实际展开 {sector_expanded} → 候选 {len(candidates)} → "
        f"维科夫买点 {sum(1 for item in candidates if item.get('wyckoff'))} → "
        f"数据合格 {sum(1 for item in candidates if item.get('data_quality', {}).get('eligible'))} → "
        f"可执行 {len(buckets['actionable'])}/等待 {len(buckets['waiting_trigger'])}"
    )
    regime_html = ""
    if regime and regime["score"] is not None:
        color = {"强势": "#dc2626", "中性": "#d97706", "弱势": "#16a34a"}.get(regime["label"], "#86868b")
        weak_note = ('<p style="color:#b45309;font-weight:600">⚠️ 弱势市:候选仅作观察,'
                     '等大盘站回 MA20。</p>' if weak else "")
        regime_html = (
            f"<div class='score' style='color:{color}'>市场环境 {regime['score']} {regime['label']}"
            f"<span style='font-size:14px;color:#86868b'> (数据 {regime['data_date']})</span></div>"
            f"{weak_note}"
        )

    performance_html = _performance_html(performance)
    tracking_error = ""
    tracking_warnings = ""
    if tracking:
        if tracking.get("error_type") or tracking.get("reason"):
            tracking_error = (
                f" | {escape(str(tracking.get('error_type')))}: "
                f"{escape(str(tracking.get('reason')))}"
            )
        if tracking.get("normalization_warnings"):
            tracking_warnings = (
                " | 规范化字段 "
                f"{len(tracking.get('normalization_warnings', []))}"
            )
    tracking_html = (
        f"<p class='dt'>推荐快照追踪：{escape(str(tracking.get('status')))}"
        f"{(' | ' + escape(str(tracking.get('path')))) if tracking.get('path') else ''}"
        f"{tracking_error}{tracking_warnings}</p>"
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
.candidate-table{{table-layout:fixed}}
.candidate-table th:nth-child(1),.candidate-table td:nth-child(1){{width:3%}}
.candidate-table th:nth-child(2),.candidate-table td:nth-child(2){{width:8%}}
.candidate-table th:nth-child(3),.candidate-table td:nth-child(3){{width:18%}}
.candidate-table th:nth-child(4),.candidate-table td:nth-child(4){{width:24%}}
.candidate-table th:nth-child(5),.candidate-table td:nth-child(5){{width:9%}}
.candidate-table th:nth-child(6),.candidate-table td:nth-child(6){{width:7%}}
.candidate-table th:nth-child(7),.candidate-table td:nth-child(7){{width:6%}}
.candidate-table th:nth-child(8),.candidate-table td:nth-child(8){{width:6%}}
.candidate-table th:nth-child(9),.candidate-table td:nth-child(9){{width:6%}}
.candidate-table th:nth-child(10),.candidate-table td:nth-child(10){{width:8%}}
.candidate-table th:nth-child(11),.candidate-table td:nth-child(11){{width:5%}}
.candidate-table th,.candidate-table td{{overflow-wrap:anywhere}}
.candidate-table .wyckoff-buy-level-badge{{white-space:normal}}
.buy{{color:#dc2626;font-weight:600}}
.wyckoff-buy-level-1>td{{background:#fff7d6}}
.wyckoff-buy-level-2>td{{background:#dcfce7}}
.wyckoff-buy-level-3>td{{background:#dbeafe}}
.wyckoff-buy-level-badge{{display:inline-block;margin-top:4px;padding:2px 6px;border-radius:999px;background:rgba(255,255,255,.72);font-size:11px;font-weight:700;color:#374151;white-space:nowrap}}
.buy-level-legend{{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px;font-size:12px}}
.buy-level-legend span{{padding:4px 8px;border-radius:999px;color:#374151}}
.buy-level-legend .level-1{{background:#fff7d6}}
.buy-level-legend .level-2{{background:#dcfce7}}
.buy-level-legend .level-3{{background:#dbeafe}}
.wyckoff-observation-buy-level-1>td{{background:#fffdf4}}
.wyckoff-observation-buy-level-2>td{{background:#f3fcf6}}
.wyckoff-observation-buy-level-3>td{{background:#f4f8ff}}
.wyckoff-buy-level-badge.observation{{background:transparent;border:1px dashed #9ca3af;color:#6b7280}}
.observation-buy-level-note{{margin:8px 0;padding:8px 10px;border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;color:#4b5563;font-size:12px}}
.observation-buy-level-legend .level-1{{background:#fffdf4}}
.observation-buy-level-legend .level-2{{background:#f3fcf6}}
.observation-buy-level-legend .level-3{{background:#f4f8ff}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:28px}}
</style></head><body><div class="w">
<h1>📋 每日候选股 {datetime.now().strftime('%Y-%m-%d')}</h1>
<p class="dt">生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 实际展开板块 {sector_expanded} 个 | 候选 {len(candidates)} 只 | 耗时 {elapsed:.0f}s</p>
{regime_html}

<p class="dt">{policy_note}</p>
<p class="dt">{priority_note}</p>
<p class="dt">{funnel_note}</p>
{tracking_html}
{provisional_banner}
<h2 style="font-size:18px;margin:18px 0 8px">今日可执行{tier_suffix}</h2>
<div class="buy-level-legend" aria-label="维科夫买点分级图例">
<span class="level-1">一级 · Spring/Test</span>
<span class="level-2">二级 · SOS 后 LPS</span>
<span class="level-3">三级 · JAC/BU 后再确认</span>
</div>
<table class="candidate-table"><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>优先分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{actionable_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">等待触发{tier_suffix}</h2>
<table class="candidate-table"><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>优先分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{waiting_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">次日确认观察（非推荐）</h2>
<table class="candidate-table"><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>优先分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{confirmation_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<div class="observation-buy-level-note" role="note">
<strong>观察池分级仅表示维科夫结构成熟度，不是买入建议。</strong>
市场环境、数据质量、板块持续性和维科夫筛选仍是硬门槛；
只有“今日可执行”区域具备推荐资格。
</div>
<div class="buy-level-legend observation-buy-level-legend" aria-label="观察池潜在维科夫买点分级图例">
<span class="level-1">潜在一级 · Spring/Test</span>
<span class="level-2">潜在二级 · SOS 后 LPS</span>
<span class="level-3">潜在三级 · JAC/BU 后再确认</span>
</div>
<table class="candidate-table"><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>优先分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{observation_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">数据失效/待修复</h2>
<table class="candidate-table"><thead><tr><th>#</th><th>名称</th><th>板块</th><th>小级别维科夫阶段</th><th>短线买点</th><th>短线置信度</th><th>原始分</th><th>质量分</th><th>优先分</th><th>数据维度覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{rejected_rows}</tbody></table>
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
        "data_rejected": buckets.get("data_rejected", []),
    }


def main():
    parser = argparse.ArgumentParser(description="每日候选股 — 自动筛出维科夫买点候选")
    parser.add_argument("--top", type=int, default=30, help="输出上限(默认30)")
    parser.add_argument("--min-candidates", type=int, default=20,
                        help="候选数量下限,不足则扩展板块(默认20)")
    parser.add_argument(
        "--max-sector-expansion", type=int,
        default=DEFAULT_MAX_SECTOR_EXPANSION,
        help="单次运行最多展开的热点板块数(默认120)")
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
        capital_top=args.top,
        max_sector_expansion=getattr(
            args, "max_sector_expansion", DEFAULT_MAX_SECTOR_EXPANSION),
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
        print(json.dumps(_normalize_for_json(out),
                         ensure_ascii=False, indent=2))
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
