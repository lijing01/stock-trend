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
import time
from datetime import datetime, timedelta, time as datetime_time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = Path(os.environ.get("STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"

sys.path.insert(0, str(SCRIPT_DIR))

from scans.stock_scanner import gather_candidates, run_phase2
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


def is_recommendation_session(now=None):
    """Treat the whole 09:30-15:00 window as provisional, including lunch."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return datetime_time(9, 30) <= current <= datetime_time(15, 0)


def resolve_recommendation_date(now=None, regime_date="", last_trading_date=""):
    """Resolve the closing-data date that a recommendation may rely on."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
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
        capital_evidence = "verified" if net_flows else "unknown"
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


def pick_hot_sectors(top_n=20, min_hot=45, min_stocks=10, regime=None,
                     as_of_date=""):
    """Pick sectors above an absolute heat floor, in relative-rank order."""
    from fetchers.sector_data import (
        append_daily_snapshot,
        get_sector_rankings,
        load_rankings_cache_full,
        load_snapshot_history,
        rank_hot_sectors,
        save_rankings_cache,
    )
    rankings = get_sector_rankings()
    live_meta = rankings.get("meta", {})
    active = sum(
        1 for sector in rankings.get("sectors", [])
        if (sector.get("up_count", 0) or 0) > 0
        or (sector.get("down_count", 0) or 0) > 0
    )
    ranking_meta = {
        "source": live_meta.get("source", "realtime"),
        "data_date": as_of_date or datetime.now().strftime("%Y-%m-%d"),
        "quality": "good",
        "errors": live_meta.get("errors", [])
        or live_meta.get("upstream_errors", []),
    }
    if active and live_meta.get("complete", False):
        if as_of_date:
            save_rankings_cache(rankings, data_date=as_of_date)
            append_daily_snapshot(rankings, override_date=as_of_date)
    else:
        cached = load_rankings_cache_full()
        cached_rankings = (cached or {}).get("rankings", {})
        cache_usable = (
            bool(cached_rankings.get("sectors"))
            and cached_rankings.get("meta", {}).get("complete") is not False
        )
        if cache_usable:
            rankings = cached["rankings"]
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
    if expected_date:
        try:
            from bridge.sector_feeder import load_qualified_sectors
            resonance = load_qualified_sectors()
            if resonance.date == expected_date:
                qualified = merge_sector_resonance(
                    qualified, resonance.sectors)
        except Exception:
            pass
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
                 sector_context=None):
    """Expand until enough score-qualified, data-eligible candidates exist."""
    all_scored = {}
    for i in range(0, len(sector_codes), batch_size):
        batch = sector_codes[i:i + batch_size]
        try:
            phase1 = gather_candidates(batch, top_n_per_sector=per_sector)
        except Exception as e:
            print(f"  ⚠️ 板块 {batch} 汇聚失败: {e}", file=sys.stderr)
            continue
        if not phase1["candidates"]:
            continue
        scored = run_phase2(
            phase1["candidates"], enable_wyckoff=True, as_of_date=as_of_date)
        for s in scored:
            context = (sector_context or {}).get(s.get("sector_code", ""), {})
            if context:
                s.update({
                    "sector_type": context.get("sector_type", ""),
                    "sector_actionable": context.get("sector_actionable", False),
                    "sector_persistence_status": context.get("persistence_status", ""),
                    "sector_capital_evidence": context.get("capital_evidence", "unknown"),
                    "sector_score": context.get("sector_score"),
                    "sector_persistence": context.get("persistence_score"),
                    "sector_relative_strength": context.get("relative_strength"),
                    "ranking_source": context.get("ranking_source", ""),
                    "ranking_data_date": context.get(
                        "ranking_data_date", ""),
                    "ranking_quality": context.get("ranking_quality", ""),
                    "ranking_errors": context.get("ranking_errors", []),
                })
            all_scored[s["code"]] = s
        eligible_count = sum(
            1 for item in all_scored.values()
            if candidate_rank_score(item) >= min_score
            and item.get("data_quality", {}).get("eligible", False)
            and item.get("sector_actionable", True)
        )
        print(
            f"  批次完成,候选 {len(all_scored)} 只,有效 {eligible_count} 只",
            file=sys.stderr,
        )
        if eligible_count >= min_candidates:
            break
    return list(all_scored.values())


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


def _append_candidate_table(lines, title, items, empty_text):
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append(f"> {empty_text}")
        return
    lines.extend([
        "| # | 名称(代码) | 板块 | 买点 | 置信度 | 原始分 | 质量分 | 覆盖率 | 数据问题/异常及原因 |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        detail = _candidate_diagnostic_text(item)
        lines.append(
            f"| {index} | {item['name']}({item['code']}) | "
            f"{_sector_text(item)} | {wyckoff.get('sub_phase', '-')} | "
            f"{wyckoff.get('confidence', 0):.0%} | "
            f"{item['composite_score']:.1f} | "
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
    if market_open:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["intraday_provisional"],
        }
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
    if score < 60:
        return {
            "mode": "observation", "max_recommendations": 0,
            "max_portfolio_pct": 0, "reasons": ["regime_weak"],
        }
    capital_score = regime.get("capital_score")
    divergence = capital_score is not None and float(capital_score) < 35
    if score < 80:
        return {
            "mode": "waiting_trigger", "max_recommendations": 2,
            "max_portfolio_pct": 30, "reasons": [],
            "requires_sector_capital_proof": divergence,
        }
    return {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
        "requires_sector_capital_proof": divergence,
    }


def classify_candidates(candidates, policy):
    eligible = [
        item for item in candidates
        if item.get("data_quality", {}).get("eligible", False)
        and item.get("sector_actionable", True)
        and item.get("score_eligible", True)
        and (not policy.get("requires_sector_capital_proof", False)
             or item.get("sector_capital_evidence") == "verified")
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
                         and item.get("code") not in promoted][:2]
        confirmations = [
            dict(item, confirmation_conditions=(
                "次日板块跑赢沪深300、守住当日低点，且放量或资金/共振确认"))
            for item in confirmations
        ]
    observation = []
    for item in candidates:
        if item.get("code") in promoted:
            continue
        copy = dict(item)
        reasons = list(item.get("data_quality", {}).get("reasons", []))
        if not item.get("sector_actionable", True):
            reasons.append(item.get("sector_persistence_status")
                           or item.get("sector_type") or "sector_unverified")
        if policy.get("requires_sector_capital_proof", False) \
                and item.get("sector_capital_evidence") != "verified":
            reasons.append("breadth_capital_divergence")
        if not item.get("score_eligible", True):
            reasons.append("quality_adjusted_below_min_score")
        if not reasons and policy.get("reasons"):
            reasons.extend(policy["reasons"])
        if not reasons:
            reasons.append("recommendation_limit")
        copy["observation_reasons"] = list(dict.fromkeys(reasons))
        observation.append(copy)
    return {
        "actionable": actionable,
        "waiting_trigger": waiting,
        "next_day_confirmation": confirmations,
        "observation": observation,
    }


def generate_report(candidates, sector_codes, elapsed, policy, buckets):
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
    _append_candidate_table(
        lines, "今日可执行", buckets["actionable"], "今日无可执行推荐。")
    _append_candidate_table(
        lines, "等待触发", buckets["waiting_trigger"], "暂无等待触发标的。")
    _append_candidate_table(
        lines, "次日确认观察（非推荐）", buckets.get("next_day_confirmation", []),
        "暂无可供次日确认的观察标的。")
    _append_candidate_table(
        lines, "观察池", buckets["observation"], "观察池为空。")
    lines.extend([
        "", "---", "",
        "*候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。*",
        "",
        "**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**",
    ])
    return "\n".join(lines)


def _html_candidate_rows(items):
    if not items:
        return '<tr><td colspan="9">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        detail = _candidate_diagnostic_text(item)
        rows.append(
            f"<tr><td>{index}</td><td><strong>{item['name']}</strong><br>"
            f"<span style='color:#86868b;font-size:12px'>{item['code']}</span></td>"
            f"<td>{_sector_text(item)}</td>"
            f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
            f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
            f"<td><strong>{item['composite_score']:.1f}</strong></td>"
            f"<td><strong>{candidate_rank_score(item):.1f}</strong></td>"
            f"<td>{quality.get('coverage', 0):.0%}</td>"
            f"<td>{detail}</td></tr>"
        )
    return "".join(rows)


def _generate_html(candidates, sector_codes, elapsed, ts, policy, buckets):
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
<h2 style="font-size:18px;margin:18px 0 8px">今日可执行</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>原始分</th><th>质量分</th><th>覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{actionable_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">等待触发</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>原始分</th><th>质量分</th><th>覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{waiting_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">次日确认观察（非推荐）</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>原始分</th><th>质量分</th><th>覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{confirmation_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>原始分</th><th>质量分</th><th>覆盖率</th><th>数据问题/异常及原因</th></tr></thead><tbody>{observation_rows}</tbody></table>

<footer><p class="disc">候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。<br><strong>本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。</strong></p></footer>
</div></body></html>"""


def build_json_output(candidates, sector_codes, elapsed, policy, buckets):
    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "sector_count": len(sector_codes),
            "candidate_count": len(candidates),
            "elapsed_seconds": round(elapsed, 1),
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
    regime = load_regime_context()
    from fetchers.sector_data import get_last_trading_day
    last_trading_date, _ = get_last_trading_day()
    expected_date = resolve_recommendation_date(
        regime_date=(regime or {}).get("data_date", ""),
        last_trading_date=last_trading_date or "",
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
        sector_codes = pick_hot_sectors(
            regime=regime, as_of_date=expected_date)
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
    )

    # 过滤 + 排序 + 归一化到 top
    candidates = select_candidate_pool(scored, args.top, args.min_score)
    buckets = classify_candidates(candidates, policy)

    elapsed = time.time() - start

    if args.json:
        out = build_json_output(candidates, sector_codes, elapsed, policy, buckets)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 报告
    report = generate_report(candidates, sector_codes, elapsed, policy, buckets)
    print(report)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"candidates-{ts}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n候选报告: {path}")

    if args.html:
        try:
            html = _generate_html(
                candidates, sector_codes, elapsed, ts, policy, buckets)
            html_path = REPORTS_DIR / f"candidates-{ts}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"HTML: {html_path}")
        except Exception as e:
            print(f"⚠️ HTML 生成失败: {e}")

    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
