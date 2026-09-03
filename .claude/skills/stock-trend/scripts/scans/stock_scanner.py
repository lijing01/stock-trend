#!/usr/bin/env python3
"""A股个股筛选器 — Scan A-stock constituents of hot sectors after market_theme/market_leader.

Three-phase architecture:
  Phase 1: Gather + hard-filter A-stocks from hot sector constituents
  Phase 2: Quick multi-dimension scoring (momentum/volume/capital/fundamental/sector)
  Phase 3: Rank, assign stars, output JSON

Usage:
    python3 stock_scanner.py --sectors BK0477,BK0897 --top 10
    python3 stock_scanner.py --from-leader /path/to/leader_output.json --top 10
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import copy
import inspect
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.cache_utils import run_script, CACHE_DIR
from core.eastmoney_utils import ma, rsi, macd_direction, volume_ma
from core.recommendation_quality import (
    NON_PROVIDER_STATUSES as NON_PROVIDER_ENRICHMENT_STATUSES,
    assess_candidate_data,
    latest_data_date,
)
from core.source_health import (
    CAPITAL_PREFETCH_BATCH_SIZE,
    CAPITAL_PREFETCH_LIMIT,
    LIVE_ATTEMPT_TIMEOUT_SECONDS,
    MAX_IN_FLIGHT,
    MAX_PROVIDER_ATTEMPTS,
    RunSourceHealth,
    bounded_source_map,
    classify_failure,
    live_attempt,
    source_result,
)
from core.candidate_trade_plan import (
    build_candidate_trade_plan,
    validate_trade_plan,
)
from analysis.wyckoff import (
    analyze_kline_dict,
    BUY_PHASES, BUY_SUB_PHASES, build_period_alignment, is_buy_signal, normalize_score_100,
    SUB_LPS, SUB_PRE_MARKUP, SUB_JAC,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
# Match RunSourceHealth semantics: degrade (and keep retrying) after the first
# failures; only hard-stop after many consecutive failures.
SOURCE_FAILURE_THRESHOLD = 2
SOURCE_HARD_FAILURE_THRESHOLD = 8
SOURCE_EVIDENCE_STATUSES = frozenset({
    "live_success", "cache_valid", "cache_miss", "cache_stale",
    "not_selected_for_enrichment", "not_started_deadline",
    "source_unavailable",
})

# ──────────────────────── Helpers ────────────────────────


def _read_json(path):
    """Read JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            meta = payload.setdefault("meta", {})
            if isinstance(meta, dict) and not (
                    meta.get("fetch_time") or meta.get("fetched_at")):
                modified = datetime.fromtimestamp(Path(path).stat().st_mtime)
                meta["fetch_time"] = modified.strftime("%Y%m%d-%H%M%S")
        return payload
    except Exception:
        return None


def _safe_float(val, default=0.0):
    """Parse float safely."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _optional_number(value):
    """Return a finite ordering value, leaving missing values as None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sector_membership_sort_key(membership):
    """Deterministic primary-sector preference; missing evidence ranks last."""
    descending = (
        membership.get("sector_score"),
        membership.get("persistence_score"),
        membership.get("relative_strength"),
    )
    rank = _optional_number(membership.get("ranking_position"))
    membership_degraded = (
        membership.get("membership_source", "realtime") != "realtime"
        or membership.get("membership_quality", "good") != "good"
        or bool(membership.get("membership_cache_error"))
    )
    return (
        not bool(membership.get("sector_actionable", False)),
        membership_degraded,
        *tuple(
            -number if (number := _optional_number(value)) is not None
            else float("inf")
            for value in descending
        ),
        rank if rank is not None else float("inf"),
        str(membership.get("code", "")),
    )


def select_primary_sector_membership(memberships):
    """Return the strongest membership without mutating the evidence list."""
    usable = [membership for membership in memberships
              if membership.get("code")]
    return min(usable, key=sector_membership_sort_key) if usable else {}


def build_sector_membership(sector_code, sector_name="", context=None,
                            stock=None):
    """Build the complete sector evidence attached to one stock membership."""
    context = context or {}
    stock = stock or {}
    return {
        "code": sector_code,
        "name": context.get("name", sector_name or sector_code),
        "hot_score": context.get(
            "hot_score", context.get("relative_hot_score", 50)),
        "sector_actionable": context.get("sector_actionable", False),
        "sector_score": context.get("sector_score"),
        "persistence_status": context.get("persistence_status", ""),
        "persistence_score": context.get("persistence_score"),
        "persistence_3d": context.get("persistence_3d"),
        "persistence_5d": context.get("persistence_5d"),
        "persistence_10d": context.get("persistence_10d"),
        "history_window_days": context.get("history_window_days"),
        "history_coverage_days": context.get("history_coverage_days"),
        "sector_observed_days": context.get("sector_observed_days"),
        "hot_appearance_days": context.get("hot_appearance_days"),
        "hot_streak": context.get("hot_streak"),
        "persistence_days": context.get(
            "persistence_days", context.get("hot_appearance_days")),
        "relative_strength": context.get("relative_strength"),
        "ranking_position": context.get("ranking_position"),
        "ranking_source": context.get("ranking_source", ""),
        "ranking_data_date": context.get("ranking_data_date", ""),
        "ranking_quality": context.get("ranking_quality", ""),
        "ranking_errors": context.get("ranking_errors", []),
        "membership_source": stock.get("membership_source", "realtime"),
        "membership_data_date": stock.get("membership_data_date", ""),
        "membership_quality": stock.get("membership_quality", "good"),
        "membership_cache_error": stock.get("membership_cache_error", ""),
        "membership_cache_at": stock.get("membership_cache_at", ""),
        "membership_cache_age_hours": stock.get(
            "membership_cache_age_hours"),
        "membership_cache_tier": stock.get("membership_cache_tier", ""),
        "membership_fallback_reason": stock.get(
            "membership_fallback_reason", ""),
        "membership_provider_attempts": stock.get(
            "membership_provider_attempts", 0),
        "membership_fetch_evidence": copy.deepcopy(
            stock.get("membership_fetch_evidence", {})),
        "sector_type": context.get("sector_type", ""),
        "capital_evidence": context.get("capital_evidence", "unknown"),
    }


def _sector_membership_output_fields(membership):
    """Expose sector persistence evidence on the scored candidate itself."""
    return {
        "sector_type": membership.get("sector_type", ""),
        "sector_actionable": membership.get("sector_actionable", False),
        "sector_persistence_status": membership.get(
            "persistence_status", ""),
        "sector_capital_evidence": membership.get(
            "capital_evidence", "unknown"),
        "sector_score": membership.get("sector_score"),
        "sector_persistence": membership.get("persistence_score"),
        "sector_persistence_3d": membership.get("persistence_3d"),
        "sector_persistence_5d": membership.get("persistence_5d"),
        "sector_persistence_10d": membership.get("persistence_10d"),
        "history_window_days": membership.get("history_window_days"),
        "history_coverage_days": membership.get("history_coverage_days"),
        "sector_observed_days": membership.get("sector_observed_days"),
        "hot_appearance_days": membership.get("hot_appearance_days"),
        "hot_streak": membership.get("hot_streak"),
        "persistence_days": membership.get("persistence_days"),
        "sector_relative_strength": membership.get("relative_strength"),
        "ranking_position": membership.get("ranking_position"),
        "ranking_source": membership.get("ranking_source", ""),
        "ranking_data_date": membership.get("ranking_data_date", ""),
        "ranking_quality": membership.get("ranking_quality", ""),
        "ranking_errors": copy.deepcopy(
            membership.get("ranking_errors", [])),
    }


def merge_sector_memberships(*membership_groups):
    """Merge memberships by sector code, retaining the newest full record."""
    merged = {}
    for group in membership_groups:
        for membership in group or []:
            code = membership.get("code", "")
            if not code:
                continue
            merged[code] = {**merged.get(code, {}), **membership}
    return [merged[code] for code in sorted(merged)]


def _is_a_share(code):
    """Return True if code looks like an A-share (not ETF, not HK)."""
    if not code or not isinstance(code, str):
        return False
    if len(code) == 6 and code.isdigit():
        if code.startswith(("6", "0", "3")):
            return code[:2] not in ("50", "51", "55", "56", "58", "15", "16", "18")
        return False
    return False


def _is_st(name):
    """Return True if stock name indicates ST / delisting risk."""
    if not name:
        return False
    return any(kw in str(name) for kw in ("ST", "*ST", "退"))


def _compute_pct_rank(val, sorted_vals):
    """Percentile rank of val in sorted_vals (0-100)."""
    if not sorted_vals:
        return 50
    n = len(sorted_vals)
    rank = sum(1 for v in sorted_vals if v < val)
    return rank / n * 100


def _resolve_ts_code(code):
    """Resolve 6-digit code to ts_code suffix."""
    if len(code) != 6 or not code.isdigit():
        return code
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _piecewise_linear(val, anchors):
    """Piecewise linear map from anchors [(x0,y0), (x1,y1), ...]."""
    if not anchors:
        return 0
    if val <= anchors[0][0]:
        return anchors[0][1]
    if val >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= val <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (val - x0) / (x1 - x0)
    return 0


def _source_state(source_health, source):
    if source_health is None:
        return None
    if isinstance(source_health, RunSourceHealth):
        return source_health.snapshot()[source]
    return source_health.setdefault(source, {
        "state": "healthy", "failures": 0,
    })


def _source_unavailable(source_health, source):
    if isinstance(source_health, RunSourceHealth):
        return source_health.unavailable(source)
    state = _source_state(source_health, source)
    return bool(state and state.get("state") == "unavailable")


def _source_succeeded(source_health, source):
    if isinstance(source_health, RunSourceHealth):
        return
    state = _source_state(source_health, source)
    if state is not None:
        state.update({"state": "healthy", "failures": 0})


def _source_failed(source_health, source):
    if isinstance(source_health, RunSourceHealth):
        return
    state = _source_state(source_health, source)
    if state is None:
        return
    state["failures"] = state.get("failures", 0) + 1
    state["state"] = (
        "unavailable"
        if state["failures"] >= SOURCE_HARD_FAILURE_THRESHOLD
        else "degraded"
    )


def _evidenced_fetch(fetcher, *args, usable=None, **kwargs):
    """Call an optionally evidence-aware fetcher, including test doubles."""
    try:
        result = fetcher(*args, with_evidence=True, **kwargs)
    except TypeError as exc:
        if "with_evidence" not in str(exc):
            raise
        payload = fetcher(*args, **kwargs)
        reason = "" if usable is None or usable(payload) else "empty"
        return source_result(payload, live_attempt(
            attempted=True, provider_attempts=1, reason=reason))
    if isinstance(result, dict) and set(("payload", "live_attempt")) <= set(result):
        return result
    reason = "" if usable is None or usable(result) else "empty"
    return source_result(result, live_attempt(
        attempted=True, provider_attempts=1, reason=reason))


def _call_fetch_compat(fetcher, ts_code, kwargs):
    """Call a fetcher while keeping older test/adaptor signatures usable."""
    signature_target = getattr(fetcher, "side_effect", None)
    if not callable(signature_target):
        signature_target = fetcher
    try:
        parameters = inspect.signature(signature_target).parameters
    except (TypeError, ValueError):
        return fetcher(ts_code, **kwargs)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD
           for parameter in parameters.values()):
        return fetcher(ts_code, **kwargs)
    supported = {
        name: value for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind != inspect.Parameter.POSITIONAL_ONLY
    }
    return fetcher(ts_code, **supported)


def _unpack_source_result(result):
    """Return payload and evidence from either new or legacy fetchers."""
    if isinstance(result, dict) and set(
            ("payload", "live_attempt")) <= set(result):
        return result["payload"], result["live_attempt"]
    return result, live_attempt(
        attempted=False, cache_used=bool(result), stale=False)


def _cache_status(payload, verdict=None, cached=None):
    """Return an explicit cache state without treating diagnostics as data."""
    if isinstance(verdict, dict) and verdict.get("valid"):
        return "cache_valid"
    if cached is None:
        validation = payload.get("meta", {}).get("cache_validation", {}) \
            if isinstance(payload, dict) and isinstance(
                payload.get("meta", {}), dict) else {}
        cached = validation.get("cache_present") if isinstance(
            validation, dict) and "cache_present" in validation else payload
    return "cache_stale" if isinstance(cached, dict) and bool(cached) \
        else "cache_miss"


def _evidence_status(source, payload, attempt, *, cache_probe=False,
                     usable=None):
    """Normalize adapter/scheduler evidence to the public status vocabulary."""
    del source  # Kept in the signature so source-specific adapters can evolve.
    attempt = attempt if isinstance(attempt, dict) else {}
    status = str(attempt.get("status") or "")
    if status in SOURCE_EVIDENCE_STATUSES:
        return status
    if cache_probe or not attempt.get("attempted"):
        if usable is not None and usable(payload):
            return "cache_valid"
        validation = payload.get("meta", {}).get("cache_validation", {}) \
            if isinstance(payload, dict) and isinstance(
                payload.get("meta", {}), dict) else {}
        if isinstance(validation, dict) and validation.get("stale"):
            return "cache_stale"
        cache_present = isinstance(validation, dict) and validation.get(
            "cache_present") is True
        if cache_present or (isinstance(payload, dict) and bool(payload)):
            return "cache_stale"
        return "cache_miss"
    if attempt.get("reason"):
        return str(attempt["reason"])
    if usable is None or usable(payload):
        return "live_success"
    return "empty"


def _normalize_source_evidence(source, payload, attempt, *, cache_probe=False,
                               usable=None):
    """Copy evidence and make cache/provider status explicit for reports."""
    evidence = copy.deepcopy(attempt) if isinstance(attempt, dict) else {}
    status = _evidence_status(
        source, payload, evidence, cache_probe=cache_probe, usable=usable)
    evidence["status"] = status
    if status in NON_PROVIDER_ENRICHMENT_STATUSES:
        evidence["cache_used"] = False
    elif status == "cache_valid":
        evidence["cache_used"] = True
        evidence["stale"] = False
    return evidence


def _cache_fetch(fetcher, *args, **kwargs):
    """Call cache-only while retaining compatibility with older test doubles."""
    try:
        return fetcher(*args, cache_only=True, **kwargs)
    except TypeError as exc:
        if "cache_only" not in str(exc):
            raise
        return fetcher(*args, **kwargs)


# ──────────────────────── Phase 1: Gather + Filter ────────────────────────


def gather_candidates(sector_codes: list[str], top_n_per_sector: int = 30,
                      max_workers: int = 4, sector_context=None,
                      source_health=None, metrics=None) -> dict:
    """Phase 1: Gather constituent A-stocks from hot sectors, dedup, hard filter.

    Returns dict with:
        stocks: list of candidate stock dicts
        excluded: list of excluded stocks with reasons
        sector_map: {code: {name, hot_score}} for later reference
    """
    # Import sector_data inline to avoid circular imports
    from fetchers.sector_data import (
        get_sector_rankings, get_sector_stocks, get_sector_stocks_cached,
        rank_hot_sectors,
    )

    # Get sector rankings to enrich with hot scores
    sector_scores = {}
    hot_sectors = []
    if sector_context is not None:
        for code, context in sector_context.items():
            sector_scores[code] = context.get(
                "hot_score", context.get("relative_hot_score", 50))
            hot_sectors.append({
                "code": code,
                "name": context.get("name", code),
                "hot_score": sector_scores[code],
            })
    else:
        try:
            rankings = get_sector_rankings()
            if metrics is not None:
                metrics["sector_ranking_requests"] = (
                    metrics.get("sector_ranking_requests", 0) + 1)
            hot_sectors = rank_hot_sectors(
                rankings, top_n=len(rankings.get("sectors", [])))
            for s in hot_sectors:
                sector_scores[s["code"]] = s.get("hot_score", 50)
        except Exception:
            pass

    # Parallel fetch constituent stocks per sector
    sector_map = {}
    all_stocks = []  # list of (stock_dict, sector_info)

    def _fetch_one_sector(code):
        def _with_evidence(stocks, attempt):
            return [
                {**stock, "membership_fetch_evidence": copy.deepcopy(attempt)}
                for stock in (stocks or [])
            ]

        try:
            cache_only = _source_unavailable(source_health, "sector_membership")
            if cache_only:
                stocks = get_sector_stocks_cached(
                    code, top_n=top_n_per_sector)
                attempt = live_attempt(
                    attempted=False, cache_used=bool(stocks),
                    stale=bool(stocks), reason="cache_only" if stocks else "")
            else:
                try:
                    fetched = get_sector_stocks(
                        code, top_n=top_n_per_sector, with_evidence=True)
                except TypeError as exc:
                    if "with_evidence" not in str(exc):
                        raise
                    fetched = get_sector_stocks(
                        code, top_n=top_n_per_sector)
                if isinstance(fetched, dict) and set(
                        ("payload", "live_attempt")) <= set(fetched):
                    stocks = fetched["payload"]
                    attempt = fetched["live_attempt"]
                else:
                    stocks = fetched
                    attempt = live_attempt(
                        attempted=True, provider_attempts=1,
                        reason="" if stocks else "empty")
            stocks = _with_evidence(stocks, attempt)
            if metrics is not None:
                key = ("sector_membership_cache_hits" if cache_only or (
                    stocks and stocks[0].get("membership_source") == "cache")
                    else "sector_membership_requests")
                metrics[key] = metrics.get(key, 0) + 1
            if not stocks:
                raise RuntimeError("无可用成分股缓存")
            if stocks[0].get("membership_source") == "cache":
                if not cache_only:
                    _source_failed(source_health, "sector_membership")
            else:
                _source_succeeded(source_health, "sector_membership")
            hot_score = sector_scores.get(code, 50)
            # Try to get sector name from the first stock or rankings
            name = code  # fallback
            for s in hot_sectors if 'hot_sectors' in dir() else []:
                if s.get("code") == code:
                    name = s.get("name", code)
                    break
            return {"code": code, "name": name, "hot_score": hot_score,
                    "stocks": stocks, "error": None}
        except Exception as e:
            _source_failed(source_health, "sector_membership")
            if metrics is not None:
                metrics["sector_membership_failures"] = (
                    metrics.get("sector_membership_failures", 0) + 1)
            return {"code": code, "name": code, "hot_score": 50,
                    "stocks": [], "error": str(e)}

    def _membership_payload(code, stocks, error=None):
        name = code
        for sector in hot_sectors:
            if sector.get("code") == code:
                name = sector.get("name", code)
                break
        return {
            "code": code, "name": name,
            "hot_score": sector_scores.get(code, 50),
            "stocks": stocks or [], "error": error,
        }

    def _fetch_membership_live(code):
        try:
            wrapped = get_sector_stocks(
                code, top_n=top_n_per_sector,
                timeout=LIVE_ATTEMPT_TIMEOUT_SECONDS["sector_membership"],
                retries=MAX_PROVIDER_ATTEMPTS["sector_membership"] - 1,
                with_evidence=True, deadline=source_health.live_deadline)
            if isinstance(wrapped, dict) and set(
                    ("payload", "live_attempt")) <= set(wrapped):
                stocks = wrapped["payload"]
                attempt = wrapped["live_attempt"]
            else:
                stocks = wrapped
                attempt = live_attempt(
                    attempted=True, provider_attempts=1,
                    reason="" if stocks else "empty")
            stocks = [
                {**stock, "membership_fetch_evidence": copy.deepcopy(attempt)}
                for stock in (stocks or [])
            ]
            return source_result(_membership_payload(code, stocks), attempt)
        except Exception as exc:
            attempts = getattr(exc, "provider_attempts", 0)
            return source_result(
                _membership_payload(code, [], str(exc)),
                live_attempt(
                    attempted=attempts > 0, provider_attempts=attempts,
                    reason=getattr(exc, "reason", "")
                    or classify_failure(exc)),
            )

    def _fetch_membership_cache(code, scheduler_reason="cache_only"):
        fallback_reason = (
            "cache_only" if scheduler_reason in ("", "cache_only")
            else f"cache_only_{scheduler_reason}")
        stocks = get_sector_stocks_cached(
            code, top_n=top_n_per_sector,
            fallback_reason=fallback_reason)
        attempt = live_attempt(
            attempted=False, cache_used=bool(stocks), stale=bool(stocks),
            reason=scheduler_reason if stocks else "")
        stocks = [
            {**stock,
             "membership_fallback_reason": fallback_reason,
             "membership_fetch_evidence": copy.deepcopy(attempt)}
            for stock in (stocks or [])
        ]
        return _membership_payload(code, stocks)

    if isinstance(source_health, RunSourceHealth):
        fetched = bounded_source_map(
            "sector_membership", sector_codes, source_health,
            _fetch_membership_live, _fetch_membership_cache,
            source_health.live_deadline, max_workers=max_workers,
            cache_usable=lambda payload: bool(payload.get("stocks")),
            cache_fetch_with_reason=_fetch_membership_cache)
        results = [result for _, result in fetched]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_fetch_one_sector, c): c for c in sector_codes}
            results = [future.result() for future in as_completed(futures)]

    for result in results:
        sector_code = result["code"]
        context = (sector_context or {}).get(sector_code, {})
        sector_map[sector_code] = {
            "name": result["name"],
            "hot_score": result["hot_score"],
            **context,
        }
        for s in result["stocks"]:
            all_stocks.append((s, sector_code))

    # Dedup + filter
    stocks_by_code = {}
    memberships_by_code = {}
    excluded = []

    for s, sector_code in all_stocks:
        code = s.get("code", "")
        name = s.get("name", "")
        context = (sector_context or {}).get(sector_code, {})
        membership = build_sector_membership(
            sector_code,
            sector_name=sector_map.get(sector_code, {}).get(
                "name", sector_code),
            context=context,
            stock=s,
        )
        memberships_by_code.setdefault(code, []).append(membership)
        stocks_by_code.setdefault(code, s)

    candidates = []
    for code in sorted(stocks_by_code):
        s = stocks_by_code[code]
        name = s.get("name", "")
        memberships = merge_sector_memberships(
            memberships_by_code.get(code, []))
        primary = select_primary_sector_membership(memberships)
        sector_code = primary.get("code", "")

        # A-share filter
        if not _is_a_share(code):
            excluded.append({"code": code, "name": name, "reason": "非A股(ETF/港股)"})
            continue

        # ST filter
        if _is_st(name):
            excluded.append({"code": code, "name": name, "reason": "ST/退市风险"})
            continue

        # Market cap filter: 50-2000亿
        mcap = _safe_float(s.get("market_cap"))
        if mcap < 5e9:
            excluded.append({"code": code, "name": name, "reason": "市值过小(<50亿)"})
            continue
        if mcap > 2e11:
            excluded.append({"code": code, "name": name, "reason": "市值过大(>2000亿)"})
            continue

        candidates.append({
            "code": code,
            "ts_code": _resolve_ts_code(code),
            "name": name,
            "sector_code": sector_code,
            "sector_name": primary.get("name", sector_code),
            "sector_hot_score": primary.get("hot_score", 50),
            "change_pct": _safe_float(s.get("change_pct")),
            "amount": _safe_float(s.get("amount")),
            "market_cap": mcap,
            "pe": _safe_float(s.get("pe")),
            "membership_source": primary.get(
                "membership_source", "realtime"),
            "membership_data_date": primary.get(
                "membership_data_date", ""),
            "membership_quality": primary.get("membership_quality", "good"),
            "membership_cache_error": primary.get(
                "membership_cache_error", ""),
            "membership_cache_at": primary.get("membership_cache_at", ""),
            "membership_cache_age_hours": primary.get(
                "membership_cache_age_hours"),
            "membership_cache_tier": primary.get(
                "membership_cache_tier", ""),
            "membership_fallback_reason": primary.get(
                "membership_fallback_reason", ""),
            "membership_provider_attempts": primary.get(
                "membership_provider_attempts", 0),
            "membership_fetch_evidence": copy.deepcopy(
                primary.get("membership_fetch_evidence", {})),
            "sector_memberships": memberships,
        })

    return {
        "candidates": candidates,
        "excluded": excluded,
        "sector_map": sector_map,
    }


# ──────────────────────── Phase 2: Scoring ────────────────────────


def _remaining_timeout(source, live_deadline=None):
    timeout = LIVE_ATTEMPT_TIMEOUT_SECONDS[source]
    if live_deadline is not None:
        timeout = min(timeout, max(0.001, live_deadline - time.monotonic()))
    return timeout


def _cache_verdict(reasons):
    """Return the common, JSON-safe result for pure cache validators."""
    reasons = list(dict.fromkeys(reasons))
    error_reasons = {
        "invalid_payload", "source_missing", "source_error",
        "payload_error", "quality_missing", "quality_error",
        "insufficient_data", "flow_metrics_missing",
    }
    return {
        "valid": not reasons,
        "reasons": reasons,
        "stale": any(reason in {
            "wrong_trading_date", "cache_expired",
        } for reason in reasons),
        "error": any(reason in error_reasons for reason in reasons),
    }


def _payload_validation_reasons(payload, allow_nonfatal_errors=False):
    """Validate shared source, quality, and error metadata."""
    if not isinstance(payload, dict) or not payload:
        return ["invalid_payload"]
    meta = payload.get("meta", {})
    summary = payload.get("summary", {})
    meta = meta if isinstance(meta, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    source = (
        meta.get("data_source") or meta.get("source")
        or payload.get("source")
    )
    reasons = []
    if not source:
        reasons.append("source_missing")
    elif str(source).lower() == "error":
        reasons.append("source_error")
    if any((
        payload.get("error"),
        meta.get("error"), meta.get("errors"), meta.get("refresh_error"),
        summary.get("error"), summary.get("errors"),
    )):
        reasons.append("payload_error")
    if payload.get("errors") and not allow_nonfatal_errors:
        reasons.append("payload_error")
    quality = (
        summary.get("data_quality") or payload.get("data_quality")
        or meta.get("data_quality")
    )
    if quality == "error":
        reasons.append("quality_error")
    cache_validation = meta.get("cache_validation")
    if isinstance(cache_validation, dict) and not cache_validation.get(
            "valid", False):
        reasons.extend(cache_validation.get("reasons") or ["payload_error"])
    return reasons


def _append_ttl_reason(reasons, cache_age_seconds, ttl_seconds):
    if cache_age_seconds is not None and ttl_seconds is not None:
        if cache_age_seconds >= ttl_seconds:
            reasons.append("cache_expired")


def _validate_kline_cache(payload, expected_trading_date=""):
    """Pure validation for a K-line cache candidate."""
    reasons = _payload_validation_reasons(payload)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    required = ("open", "high", "low", "close")
    usable_rows = [
        row for row in rows
        if isinstance(row, dict)
        and latest_data_date({"data": [row]})
        and all(
            isinstance(row.get(field), (int, float))
            and not isinstance(row.get(field), bool)
            and math.isfinite(float(row[field]))
            for field in required
        )
    ] if isinstance(rows, list) else []
    if len(usable_rows) < WYCKOFF_MIN_BARS:
        reasons.append("insufficient_data")
    data_date = latest_data_date({"data": usable_rows})
    if expected_trading_date and data_date < expected_trading_date:
        reasons.append("wrong_trading_date")
    return _cache_verdict(reasons)


def _validate_capital_cache(payload, expected_trading_date="",
                            cache_age_seconds=None, ttl_seconds=None):
    """Pure validation for a capital-flow cache candidate."""
    reasons = _payload_validation_reasons(payload)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or not rows:
        reasons.append("insufficient_data")
    else:
        flow_fields = (
            "main_net_inflow", "main_inflow", "main_outflow",
            "retail_net_inflow", "retail_inflow", "retail_outflow",
            "total_net_inflow",
        )
        def _row_date(row):
            return latest_data_date({"data": [row]})

        has_numeric_flow = any(
            isinstance(row, dict)
            and (not expected_trading_date
                 or _row_date(row) >= expected_trading_date)
            and any(
                isinstance(row.get(field), (int, float))
                and not isinstance(row.get(field), bool)
                and math.isfinite(float(row[field]))
                for field in flow_fields
            )
            for row in rows
        )
        if not has_numeric_flow:
            reasons.append("flow_metrics_missing")
    data_date = latest_data_date(payload)
    if expected_trading_date and data_date < expected_trading_date:
        reasons.append("wrong_trading_date")
    _append_ttl_reason(reasons, cache_age_seconds, ttl_seconds)
    return _cache_verdict(reasons)


def _validate_fundamental_cache(payload, expected_trading_date="",
                                cache_age_seconds=None, ttl_seconds=None):
    """Pure validation for a fundamental cache candidate."""
    reasons = _payload_validation_reasons(
        payload, allow_nonfatal_errors=True)
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict) or not summary:
        reasons.append("insufficient_data")
    elif not summary.get("data_quality"):
        reasons.append("quality_missing")
    _append_ttl_reason(reasons, cache_age_seconds, ttl_seconds)
    return _cache_verdict(reasons)


def _cache_file_age_seconds(path):
    """Return cache file age, keeping wall-clock access outside validators."""
    try:
        return max(0.0, time.time() - Path(path).stat().st_mtime)
    except OSError:
        return float("inf")


def _cache_ttl_seconds(source, now=None):
    """Return session-aware TTL; lunch remains part of the trading session."""
    now = now or datetime.now()
    in_session = (
        now.weekday() < 5
        and (now.hour, now.minute) >= (9, 30)
        and (now.hour, now.minute) <= (15, 0)
    )
    if source == "fundamental":
        return 1800 if in_session else 57600
    return 300 if in_session else 57600


def _with_cache_verdict(payload, verdict):
    """Attach an invalid-cache diagnosis without mutating the loaded value."""
    cache_present = isinstance(payload, dict) and bool(payload)
    if not isinstance(payload, dict):
        payload = {"invalid_payload": payload}
    diagnosed = copy.deepcopy(payload)
    if not isinstance(diagnosed.get("meta"), dict):
        diagnosed["meta"] = {}
    diagnosed["meta"]["cache_validation"] = {
        **(verdict if isinstance(verdict, dict) else {}),
        "cache_present": cache_present,
    }
    diagnosed["meta"]["cache_present"] = cache_present
    return diagnosed


def _fetch_kline(ts_code, as_of_date="", cache_only=False,
                 with_evidence=False, live_deadline=None):
    """Fetch a current 60-day K-line, refreshing stale caches when required."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "kline.json"

    # A cache is a hit only when it covers the recommendation date.  A
    # post-close scan must not score T-1 data merely because it has enough bars.
    cached = _read_json(str(cache_path))
    cache_verdict = _validate_kline_cache(cached, as_of_date)
    if cache_verdict["valid"]:
        result = source_result(cached, live_attempt(
            attempted=False, cache_used=True, stale=False,
            status="cache_valid"))
        return result if with_evidence else cached
    if cache_only:
        cache_present = isinstance(cached, dict) and bool(cached)
        cached = _with_cache_verdict(cached, cache_verdict)
        status = _cache_status(cached, cache_verdict,
                               cached if cache_present else None)
        result = source_result(cached, live_attempt(
            attempted=False, cache_used=False,
            stale=status == "cache_stale", status=status))
        return result if with_evidence else cached

    # Fetch via subprocess. Transient EM/Tencent failures under concurrency
    # are common, so retry once before giving up to a stale cache.
    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/kline_eastmoney.py"),
        ts_code, "--asset", "E", "--freq", "D",
        "-o", str(cache_path),
    ]
    if as_of_date:
        cmd.extend(["--expected-date", as_of_date])

    def _run_kline_subprocess():
        return run_script(
            cmd, label=f"kline_{ts_code}",
            timeout=_remaining_timeout("kline", live_deadline))

    result = _run_kline_subprocess()
    attempt = live_attempt(
        attempted=True, provider_attempts=1, subprocess_started=True)
    refreshed = None
    refreshed_verdict = {"valid": False}
    if result["success"]:
        refreshed = _read_json(str(cache_path))
        refreshed_verdict = _validate_kline_cache(refreshed, as_of_date)
    # Retry only when the subprocess itself failed (timeout/crash); a
    # successful-but-stale refresh re-running would just re-read the same
    # file the first attempt already wrote.
    if not result["success"]:
        result = _run_kline_subprocess()
        attempt["provider_attempts"] = 2
        if result["success"]:
            refreshed = _read_json(str(cache_path))
            refreshed_verdict = _validate_kline_cache(refreshed, as_of_date)
    if refreshed_verdict["valid"]:
        attempt["status"] = "live_success"
        wrapped = source_result(refreshed, attempt)
        return wrapped if with_evidence else refreshed
    if refreshed:
        refreshed = _with_cache_verdict(refreshed, refreshed_verdict)
        refreshed.setdefault("meta", {})["refresh_error"] = (
            f"K线刷新后仍未覆盖{as_of_date}")
        attempt["reason"] = "empty"
        attempt["status"] = "empty"
        wrapped = source_result(refreshed, attempt)
        return wrapped if with_evidence else refreshed
    # Keep a stale cache observable to the quality gate instead of dropping it
    # and losing the source/date diagnostic.
    if cached:
        cached = _with_cache_verdict(cached, cache_verdict)
        cached.setdefault("meta", {})["refresh_error"] = (
            result.get("error") or f"K线刷新失败，未覆盖{as_of_date}")
        attempt.update({
            "reason": classify_failure(
                result.get("stderr") or result.get("error")),
            "cache_used": True, "stale": True,
        })
        attempt["status"] = attempt["reason"]
        wrapped = source_result(cached, attempt)
        return wrapped if with_evidence else cached
    attempt["reason"] = classify_failure(
        result.get("stderr") or result.get("error"))
    attempt["status"] = attempt["reason"]
    wrapped = source_result(None, attempt)
    return wrapped if with_evidence else None


def _cache_file_is_fresh(path, ttl_seconds):
    """Check output-file freshness without launching a fetcher process."""
    try:
        return _cache_file_age_seconds(path) < ttl_seconds
    except OSError:
        return False


def _usable_capital_payload(payload):
    return _validate_capital_cache(payload)["valid"]


def _usable_fundamental_payload(payload):
    return _validate_fundamental_cache(payload)["valid"]


def _probe_capital_cache(ts_code, expected_trading_date=""):
    """Read and validate a capital cache without invoking a provider."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "capital_flow.json"
    cached = _read_json(str(cache_path))
    ttl_seconds = _cache_ttl_seconds("capital")
    cache_age_seconds = (
        0 if _cache_file_is_fresh(cache_path, ttl_seconds) else ttl_seconds
    )
    verdict = _validate_capital_cache(
        cached, expected_trading_date,
        cache_age_seconds=cache_age_seconds,
        ttl_seconds=ttl_seconds)
    if verdict["valid"]:
        return source_result(cached, live_attempt(
            attempted=False, cache_used=True, stale=False,
            status="cache_valid"))
    present = isinstance(cached, dict) and bool(cached)
    diagnosed = _with_cache_verdict(cached, verdict)
    status = _cache_status(diagnosed, verdict, diagnosed if present else None)
    return source_result(diagnosed, live_attempt(
        attempted=False, cache_used=False, stale=status == "cache_stale",
        status=status))


def _probe_fundamental_cache(ts_code, expected_trading_date=""):
    """Read and validate a fundamental cache without invoking a provider."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "fundamental.json"
    cached = _read_json(str(cache_path))
    ttl_seconds = _cache_ttl_seconds("fundamental")
    cache_age_seconds = (
        0 if _cache_file_is_fresh(cache_path, ttl_seconds) else ttl_seconds
    )
    verdict = _validate_fundamental_cache(
        cached, expected_trading_date,
        cache_age_seconds=cache_age_seconds,
        ttl_seconds=ttl_seconds)
    if verdict["valid"]:
        return source_result(cached, live_attempt(
            attempted=False, cache_used=True, stale=False,
            status="cache_valid"))
    present = isinstance(cached, dict) and bool(cached)
    diagnosed = _with_cache_verdict(cached, verdict)
    status = _cache_status(diagnosed, verdict, diagnosed if present else None)
    return source_result(diagnosed, live_attempt(
        attempted=False, cache_used=False, stale=status == "cache_stale",
        status=status))


def _fundamental_failure_reason(payload, verdict=None):
    """Preserve provider/cache reason when a fetcher returned an invalid payload."""
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    provider_failures = meta.get("provider_failures", [])
    if isinstance(provider_failures, list):
        for failure in provider_failures:
            if isinstance(failure, dict) and failure.get("reason"):
                return str(failure["reason"])

    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if isinstance(errors, (list, tuple)):
        error_text = " ".join(str(error) for error in errors if error)
    else:
        error_text = str(errors or "")
    if error_text:
        classified = classify_failure(error_text)
        return classified if classified != "unknown" else "provider_error"

    reasons = verdict.get("reasons", []) if isinstance(verdict, dict) else []
    for reason in ("wrong_trading_date", "cache_expired", "quality_error",
                   "source_error", "payload_error", "insufficient_data"):
        if reason in reasons:
            return reason
    return "empty"


def _capital_failure_reason(payload, verdict=None):
    """Return a stable capital failure code and retain source-level detail."""
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("error_type") == "stale_data":
        return "stale_data"

    chain = meta.get("failure_chain", [])
    sources = {
        item.get("source"): item.get("reason")
        for item in chain
        if isinstance(item, dict) and item.get("source")
    }
    for source, prefix in (("eastmoney", "eastmoney"),
                           ("tushare_fallback", "tushare")):
        source_reason = sources.get(source)
        if source_reason in {"empty", "timeout", "dns", "http", "parse",
                             "subprocess"}:
            return f"{prefix}_{source_reason}"
        if source_reason == "stale_data":
            return "stale_data"
    if sources.get("kline_estimate") in {"missing", "empty"}:
        return "kline_missing"
    if sources.get("kline_estimate") == "stale_data":
        return "stale_data"

    error_text = meta.get("error", "")
    classified = classify_failure(error_text)
    if classified != "unknown":
        return classified
    reasons = verdict.get("reasons", []) if isinstance(verdict, dict) else []
    if "wrong_trading_date" in reasons:
        return "stale_data"
    return "output_invalid"


def _same_day_membership_fundamental_fallback(candidate, as_of_date=""):
    """Build scan-grade PE evidence from a verified live member snapshot.

    The sector constituent response already contains the current dynamic PE.
    It is safe to use that one metric only when the membership response was
    live, marked good, and covers the exact recommendation date.  Cached or
    degraded membership never crosses this boundary, so this fallback cannot
    promote the stale snapshot seen in the report.
    """
    if not as_of_date:
        return None
    if candidate.get("membership_source") != "realtime" \
            or candidate.get("membership_quality") != "good" \
            or candidate.get("membership_data_date") != as_of_date:
        return None
    pe = _optional_number(candidate.get("pe"))
    if pe is None or not math.isfinite(pe) or pe <= 0:
        return None
    market_cap = _optional_number(candidate.get("market_cap"))
    summary = {
        "data_quality": "partial",
        "pe_ttm": round(pe, 2),
        "_fallback_source": "sector_membership_quote",
    }
    if market_cap is not None and math.isfinite(market_cap) and market_cap > 0:
        summary["market_cap_billion"] = round(market_cap / 1e8, 2)
    return {
        "meta": {
            "ts_code": candidate.get("ts_code", ""),
            "data_source": "sector_membership_quote",
            "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "fetch_mode": "same_day_fallback",
        },
        "summary": summary,
        "data": {},
        "errors": [],
    }


def _fetch_capital_flow(ts_code, cache_only=False, with_evidence=False,
                        live_deadline=None, expected_trading_date=""):
    """Fetch capital flow for a stock via CLI."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "capital_flow.json"

    cached = _read_json(str(cache_path))
    ttl_seconds = _cache_ttl_seconds("capital")
    cache_age_seconds = (
        0 if _cache_file_is_fresh(cache_path, ttl_seconds) else ttl_seconds
    )
    cache_verdict = _validate_capital_cache(
        cached, expected_trading_date,
        cache_age_seconds=cache_age_seconds,
        ttl_seconds=ttl_seconds)
    if cache_verdict["valid"]:
        wrapped = source_result(cached, live_attempt(
            attempted=False, cache_used=True, stale=False,
            status="cache_valid"))
        return wrapped if with_evidence else cached
    if cache_only:
        cache_present = isinstance(cached, dict) and bool(cached)
        cached = _with_cache_verdict(cached, cache_verdict)
        status = _cache_status(cached, cache_verdict,
                               cached if cache_present else None)
        wrapped = source_result(cached, live_attempt(
            attempted=False, cache_used=False,
            stale=status == "cache_stale", status=status))
        return wrapped if with_evidence else cached

    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/capital_flow.py"),
        ts_code, "--asset", "E", "--skip-extended", "-o", str(cache_path),
    ]
    if expected_trading_date:
        cmd.extend(["--expected-date", expected_trading_date])
    result = run_script(
        cmd, label=f"cap_{ts_code}",
        timeout=_remaining_timeout("capital", live_deadline))
    attempt = live_attempt(
        attempted=True, provider_attempts=1, subprocess_started=True)
    if result["success"]:
        payload = _read_json(str(cache_path))
        refreshed_verdict = _validate_capital_cache(
            payload, expected_trading_date)
        if not refreshed_verdict["valid"]:
            meta = payload.get("meta", {}) \
                if isinstance(payload, dict) else {}
            meta = meta if isinstance(meta, dict) else {}
            reason = _capital_failure_reason(payload, refreshed_verdict)
            attempt.update({
                "reason": reason,
                "status": reason,
                "failure_chain": meta.get("failure_chain", []),
                "error_type": meta.get("error_type", ""),
                "stale_sources": meta.get("stale_sources", []),
                "failure_detail": meta.get("error", ""),
            })
            payload = _with_cache_verdict(payload, refreshed_verdict)
        else:
            attempt["status"] = "live_success"
        wrapped = source_result(payload, attempt)
        return wrapped if with_evidence else payload
    attempt["reason"] = classify_failure(
        result.get("stderr") or result.get("error"))
    if cached:
        cached = _with_cache_verdict(cached, cache_verdict)
        attempt.update({"cache_used": True, "stale": True})
    attempt["status"] = attempt["reason"]
    wrapped = source_result(cached, attempt)
    return wrapped if with_evidence else cached


def _fetch_fundamental(ts_code, cache_only=False, with_evidence=False,
                       live_deadline=None, expected_trading_date=""):
    """Fetch fundamental data, prefer cache."""
    code = ts_code.split(".")[0]
    cache_path = Path(CACHE_DIR) / code / "fundamental.json"

    cached = _read_json(str(cache_path))
    ttl_seconds = _cache_ttl_seconds("fundamental")
    cache_age_seconds = (
        0 if _cache_file_is_fresh(cache_path, ttl_seconds) else ttl_seconds
    )
    cache_verdict = _validate_fundamental_cache(
        cached, expected_trading_date,
        cache_age_seconds=cache_age_seconds,
        ttl_seconds=ttl_seconds)
    if cache_verdict["valid"]:
        wrapped = source_result(cached, live_attempt(
            attempted=False, cache_used=True, stale=False,
            status="cache_valid"))
        return wrapped if with_evidence else cached
    if cache_only:
        cache_present = isinstance(cached, dict) and bool(cached)
        cached = _with_cache_verdict(cached, cache_verdict)
        status = _cache_status(cached, cache_verdict,
                               cached if cache_present else None)
        wrapped = source_result(cached, live_attempt(
            attempted=False, cache_used=False,
            stale=status == "cache_stale", status=status))
        return wrapped if with_evidence else cached

    cmd = [
        sys.executable, str(SCRIPT_DIR / "fetchers/fundamental.py"),
        ts_code, "--asset", "E", "--fast", "-o", str(cache_path),
    ]
    result = run_script(
        cmd, label=f"fund_{ts_code}",
        timeout=_remaining_timeout("fundamental", live_deadline))
    attempt = live_attempt(
        attempted=True, provider_attempts=1, subprocess_started=True)
    if result["success"]:
        payload = _read_json(str(cache_path))
        refreshed_verdict = _validate_fundamental_cache(
            payload, expected_trading_date)
        if not refreshed_verdict["valid"]:
            attempt["reason"] = _fundamental_failure_reason(
                payload, refreshed_verdict)
            attempt["status"] = attempt["reason"]
            payload = _with_cache_verdict(payload, refreshed_verdict)
        else:
            attempt["status"] = "live_success"
        wrapped = source_result(payload, attempt)
        return wrapped if with_evidence else payload
    attempt["reason"] = classify_failure(
        result.get("stderr") or result.get("error"))
    if cached:
        cached = _with_cache_verdict(cached, cache_verdict)
        attempt.update({"cache_used": True, "stale": True})
    attempt["status"] = attempt["reason"]
    wrapped = source_result(cached, attempt)
    return wrapped if with_evidence else cached


def _compute_close_prices(kline_data):
    """Extract close price series from kline data."""
    records = kline_data.get("data", []) if kline_data else []
    return [r["close"] for r in records if r.get("close") is not None]


def score_momentum(candidate, kline_data):
    """Score momentum dimension (0-100).

    Components: MA alignment + RSI position + MACD direction + 20d return.
    """
    if not kline_data:
        return 50.0

    records = kline_data.get("data", [])
    if len(records) < 20:
        return 50.0

    closes = _compute_close_prices(kline_data)
    if len(closes) < 20:
        return 50.0

    score = 50.0

    # MA alignment (contributes ±25)
    ma5_val = ma(closes, 5)
    ma20_val = ma(closes, 20)
    ma60_val = ma(closes, 60) if len(closes) >= 60 else ma20_val
    if ma5_val and ma20_val and ma60_val:
        if ma5_val > ma20_val > ma60_val:
            score += 25
        elif ma5_val > ma20_val:
            score += 10
        elif ma5_val < ma20_val < ma60_val:
            score -= 25
        elif ma5_val < ma20_val:
            score -= 10

    # RSI position (contributes ±15)
    latest_rsi = rsi(closes, 14)
    if latest_rsi is not None:
        if 40 <= latest_rsi <= 70:
            score += 15
        elif 30 <= latest_rsi < 40:
            score += 5
        elif 70 < latest_rsi <= 80:
            score += 0
        elif latest_rsi > 80:
            score -= 10
        elif latest_rsi < 30:
            score -= 10

    # MACD direction (contributes ±10)
    macd_dir = macd_direction(closes)
    if macd_dir == "golden_cross":
        score += 10
    elif macd_dir == "death_cross":
        score -= 10

    # 20-day return (contributes ±10)
    if len(closes) >= 20:
        ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
        if ret_20d > 10:
            score += 10
        elif ret_20d > 5:
            score += 5
        elif ret_20d < -10:
            score -= 10
        elif ret_20d < -5:
            score -= 5

    return max(0.0, min(100.0, score))


def score_volume_price(candidate, kline_data):
    """Score volume-price dimension (0-100).

    Components: volume ratio + volume-price coordination + divergence detection.
    """
    if not kline_data:
        return 50.0

    records = kline_data.get("data", [])
    if len(records) < 20:
        return 50.0

    score = 50.0

    # Volume ratio (vol_ma5 / vol_ma20)
    ma5_vol = volume_ma(records, period=5)
    ma20_vol = volume_ma(records, period=20)
    if ma5_vol and ma20_vol and ma20_vol > 0:
        vol_ratio = ma5_vol / ma20_vol
        if vol_ratio > 1.5:
            score += 20
        elif vol_ratio > 1.2:
            score += 10
        elif vol_ratio < 0.5:
            score -= 10

    # Volume-price coordination (latest day)
    if len(records) >= 1:
        latest = records[-1]
        close = latest.get("close", 0)
        pre_close = latest.get("pre_close", close)
        pct_chg = (close - pre_close) / pre_close * 100 if pre_close else 0
        vol = latest.get("vol", 0)

        # 5-day average vol
        recent_vols = [r.get("vol", 0) for r in records[-5:]]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else vol

        vol_ratio_day = vol / avg_vol if avg_vol > 0 else 1.0

        if pct_chg > 1 and vol_ratio_day > 1.3:
            score += 15  # 放量上涨
        elif pct_chg > 0 and vol_ratio_day < 0.7:
            score -= 5   # 缩量上涨 (weak)
        elif pct_chg < -1 and vol_ratio_day > 1.5:
            score -= 15  # 放量下跌
        elif pct_chg < 0 and vol_ratio_day < 0.7:
            score += 5   # 缩量下跌 (selling exhausted)

    # Volume-price divergence over last 5 days
    if len(records) >= 5:
        recent = records[-5:]
        up_days_vol = sum(1 for r in recent if r.get("close", 0) > r.get("open", 0)
                          and r.get("vol", 0) > avg_vol)
        if up_days_vol >= 4:
            score += 5  # consistent volume-supported rise

    return max(0.0, min(100.0, score))


def score_capital(candidate, capital_data):
    """Score capital flow dimension (0-100).

    Components: main force net direction + northbound change + flow streak.
    """
    if not capital_data:
        return 50.0

    score = 50.0

    # Main force net inflow direction (5-day sum)
    records = capital_data.get("data", [])
    if records:
        total_main = sum(_safe_float(r.get("main_net_inflow")) for r in records[:5])
        if total_main > 1e8:
            score += 25
        elif total_main > 0:
            score += 15
        elif total_main < -1e8:
            score -= 15
        elif total_main < 0:
            score -= 10

    # Northbound individual change
    ext = capital_data.get("data_extended", {})
    nb = ext.get("northbound_individual", {})
    if nb and isinstance(nb, dict):
        chg = nb.get("change_shares")
        if chg is not None and chg > 0:
            score += 10

    # Capital flow streak
    istreak = ext.get("individual_streak", {})
    if istreak and isinstance(istreak, dict):
        ms = istreak.get("main_streak", 0)
        if ms >= 3:
            score += 10

    return max(0.0, min(100.0, score))


def score_fundamental_quick(candidate, fundamental_data):
    """Score fundamental dimension (0-100).

    Components: PE percentile + conservative absolute PE/PB fallback + ROE +
    profit growth + revenue growth.  The absolute valuation fallback is only
    used when no historical percentile is available, which is the scan-grade
    shape returned by the lightweight providers.
    """
    if not fundamental_data:
        return 50.0

    summary = fundamental_data.get("summary", {})
    if summary.get("data_quality") in ("error", None):
        return 50.0

    score = 50.0

    # PE percentile 3-year
    pe_pct = summary.get("pe_percentile_3y")
    if pe_pct is not None:
        if pe_pct < 30:
            score += 20
        elif pe_pct < 50:
            score += 10
        elif pe_pct > 80:
            score -= 15
    else:
        # Partial quote providers do not have a 3-year distribution.  Use
        # broad, deliberately capped valuation priors so PE=5 and PE=500 do
        # not collapse to the same neutral score.  This is only 15 points of
        # the 100-point dimension and is not a substitute for fundamentals.
        pe = _optional_number(summary.get("pe_ttm"))
        if pe is not None and math.isfinite(pe):
            if pe <= 0:
                score -= 10
            elif pe <= 8:
                score += 10
            elif pe <= 15:
                score += 7
            elif pe <= 25:
                score += 3
            elif pe <= 40:
                score -= 2
            elif pe <= 80:
                score -= 6
            else:
                score -= 10

        pb = _optional_number(summary.get("pb"))
        if pb is not None and math.isfinite(pb):
            if pb <= 0:
                score -= 5
            elif pb <= 1:
                score += 5
            elif pb <= 2:
                score += 3
            elif pb <= 4:
                pass
            elif pb <= 8:
                score -= 3
            else:
                score -= 5

    # ROE
    roe = _safe_float(summary.get("roe"))
    if roe > 15:
        score += 15
    elif roe > 10:
        score += 10
    elif roe < 0:
        score -= 10

    # Profit growth
    profit_g = _safe_float(summary.get("profit_growth_pct"))
    if profit_g > 20:
        score += 15
    elif profit_g > 10:
        score += 8
    elif profit_g < 0:
        score -= 10

    # Revenue growth
    revenue_g = _safe_float(summary.get("revenue_growth_pct"))
    if revenue_g > 15:
        score += 10
    elif revenue_g > 5:
        score += 5

    return max(0.0, min(100.0, score))


def score_sector_strength(candidate, sector_scores, sector_ranks):
    """Score sector strength dimension (0-100).

    Components: sector hot_score + relative rank within sector.
    """
    score = 50.0

    sector_code = candidate.get("sector_code", "")
    hot = sector_scores.get(sector_code, 50)
    # Map hot_score (0-100) to contribution
    score += (hot - 50) * 0.3  # ±15 from sector hot score

    # Within-sector relative rank: laggards in hot sectors get bonus
    change_pct = candidate.get("change_pct", 0)
    all_changes = sector_ranks.get(sector_code, [change_pct])
    pct_rank = _compute_pct_rank(change_pct, sorted(all_changes))

    if pct_rank < 50:
        # Laggard in a hot sector → rotation candidate bonus
        laggard_bonus = min(10, (50 - pct_rank) * 0.2)
        score += laggard_bonus

    return max(0.0, min(100.0, score))


def build_sector_peer_cohorts(candidates):
    """Build deterministic change cohorts from unique Phase 1 candidates."""
    cohorts = {}
    seen = set()
    for candidate in sorted(candidates, key=lambda item: str(item.get("code", ""))):
        code = candidate.get("code", "")
        if not code or code in seen:
            continue
        seen.add(code)
        memberships = candidate.get("sector_memberships") or [{
            "code": candidate.get("sector_code", ""),
        }]
        for membership in merge_sector_memberships(memberships):
            sector_code = membership.get("code", "")
            if sector_code:
                cohorts.setdefault(sector_code, []).append(
                    _safe_float(candidate.get("change_pct")))
    return {code: sorted(values) for code, values in sorted(cohorts.items())}


def score_sector_membership(candidate, membership, peer_cohorts):
    """Pure sector score used by both initial scoring and later rebinding."""
    sector_code = membership.get("code", "")
    rebound = {
        **candidate,
        "sector_code": sector_code,
        "sector_hot_score": membership.get(
            "hot_score", membership.get("sector_score", 50)),
    }
    return score_sector_strength(
        rebound,
        {sector_code: rebound["sector_hot_score"]},
        peer_cohorts,
    )


def composite_from_dimensions(dimensions):
    """Apply the existing scanner weights to dimension scores."""
    if "wyckoff" in dimensions:
        return (
            dimensions["momentum"] * 0.25
            + dimensions["volume_price"] * 0.15
            + dimensions["capital"] * 0.15
            + dimensions["fundamental"] * 0.10
            + dimensions["sector_strength"] * 0.10
            + dimensions["wyckoff"] * 0.25
        )
    return (
        dimensions["momentum"] * 0.30
        + dimensions["volume_price"] * 0.20
        + dimensions["capital"] * 0.20
        + dimensions["fundamental"] * 0.15
        + dimensions["sector_strength"] * 0.15
    )


def apply_membership_quality(base_quality, membership, as_of_date=""):
    """Overlay membership freshness on an immutable base-quality snapshot."""
    quality = copy.deepcopy(base_quality)
    quality.setdefault("reasons", [])
    quality.setdefault("freshness_factor", 1.0)
    source = membership.get("membership_source", "realtime")
    data_date = membership.get("membership_data_date", "")
    membership_quality = membership.get("membership_quality", "good")
    cache_error = membership.get("membership_cache_error", "")
    date_mismatch = bool(as_of_date and data_date != as_of_date)
    if source != "realtime" or membership_quality != "good" \
            or date_mismatch:
        quality["eligible"] = False
        if cache_error:
            reason = "sector_membership_cache_write_failed"
        elif membership_quality == "degraded" or date_mismatch:
            reason = "sector_membership_stale"
        else:
            reason = "sector_membership_cached"
        if reason not in quality["reasons"]:
            quality["reasons"].append(reason)
        quality["freshness_factor"] *= 0.8
    return quality


# ──────────────────────── Wyckoff gate (P0-2 funnel) ────────────────────────


# 买点子阶段(共享 wyckoff.BUY_* 单一事实源)
WYCKOFF_BUY_PHASES = BUY_PHASES
WYCKOFF_BUY_SUB_PHASES = BUY_SUB_PHASES
WYCKOFF_MIN_CONFIDENCE = 0.3
WYCKOFF_MIN_BARS = 60


def normalize_wyckoff_score(score):
    """Map wyckoff [-3, +3] to 0-100 (shared with scores.py)."""
    return normalize_score_100(score)


def wyckoff_gate_pass(analysis):
    """Gate: keep only buy-point sub-phases in accumulation/markup.

    Returns True when the analysis shows a candidate in 吸筹/拉升 phase
    currently at a confirmed buy-point sub-phase (Spring/LPS/ST/PRE_MARKUP
    or JAC/BU). Distribution/markdown or unconfirmed signals → False.
    """
    if not analysis:
        return False
    conf = _safe_float(analysis.get("phase", {}).get("confidence"))
    return is_buy_signal(analysis) and conf >= WYCKOFF_MIN_CONFIDENCE


def score_wyckoff(analysis):
    """Wyckoff 100-分制 dimension from analysis dict.

    Base from wyckoff_score [-3, +3] normalized to [0,100]; small bonus for
    late-accumulation buy points (LPS/PRE_MARKUP/JAC) with high confidence.
    """
    if not analysis:
        return 50.0
    score = _safe_float(analysis.get("wyckoff_score"), 0.0)
    s = normalize_wyckoff_score(score)
    conf = _safe_float(analysis.get("phase", {}).get("confidence"))
    sub = analysis.get("phase", {}).get("primary_sub_phase", "")
    if sub in (SUB_LPS, SUB_PRE_MARKUP, SUB_JAC) and conf >= 0.5:
        s = min(100.0, s + 5.0)
    return s


def rank_capital_enrichment_candidates(candidates, capital_data=None, top=30,
                                       batch_size=CAPITAL_PREFETCH_BATCH_SIZE,
                                       prefetch_limit=CAPITAL_PREFETCH_LIMIT,
                                       expected_trading_date=""):
    """Build a deterministic capital-enrichment queue from provisional scores.

    ``capital_data`` contains only cache probes at this point.  Valid cache
    hits are retained in the result but never enter the live-provider queue.
    The helper is deliberately pure: it only returns new lists and never
    changes a candidate's final score fields.
    """
    capital_data = capital_data if isinstance(capital_data, dict) else {}
    try:
        top = max(0, int(top))
    except (TypeError, ValueError):
        top = 30
    try:
        batch_size = max(1, int(batch_size))
    except (TypeError, ValueError):
        batch_size = CAPITAL_PREFETCH_BATCH_SIZE
    try:
        prefetch_limit = max(1, int(prefetch_limit))
    except (TypeError, ValueError):
        prefetch_limit = CAPITAL_PREFETCH_LIMIT
    limit = max(prefetch_limit, int(math.ceil(top * 1.2)))

    def provisional_key(candidate):
        score = _optional_number(candidate.get("provisional_score"))
        if score is None:
            score = _optional_number(candidate.get("quality_adjusted_score"))
        if score is None:
            score = _optional_number(candidate.get("composite_score"))
        if score is None:
            score = 0.0
        return -score, str(candidate.get("code", ""))

    ordered = sorted(list(candidates or []), key=provisional_key)
    cache_valid = []
    cache_missing = []
    for candidate in ordered:
        payload = capital_data.get(candidate.get("ts_code"))
        valid = candidate.get("capital_cache_valid") is True \
            or _validate_capital_cache(
                payload, expected_trading_date)["valid"]
        if valid:
            cache_valid.append(candidate)
        else:
            cache_missing.append(candidate)

    priority_queue = cache_missing[:limit]
    subsequent_batches = [
        priority_queue[index:index + batch_size]
        for index in range(0, len(priority_queue), batch_size)
    ]
    priority_scope = ordered[:limit]
    return {
        "ordered": ordered,
        "cache_valid": cache_valid,
        "cache_missing": cache_missing,
        "priority_scope": priority_scope,
        "priority_queue": priority_queue,
        "initial_priority_queue": priority_queue,
        "batches": subsequent_batches,
        "subsequent_batches": subsequent_batches,
        "omitted": cache_missing[limit:],
        "limit": limit,
    }


def _run_phase2_legacy(candidates, max_workers=4, enable_wyckoff=False,
               as_of_date="", source_health=None, metrics=None,
               trade_plan_policy=None):
    """Phase 2: Fetch data and compute multi-dimension scores for all candidates.

    enable_wyckoff: run Wyckoff gate (P0-2 funnel) — drops candidates not at a
    buy-point sub-phase and adds a wyckoff dimension to the composite score.
    """
    print(f"[Phase 2/3] Scoring {len(candidates)} candidates...", file=sys.stderr)
    if not candidates:
        return []

    peer_cohorts = build_sector_peer_cohorts(candidates)

    # Pre-fetch all K-line data in parallel
    print(f"  Fetching K-line data...", file=sys.stderr)
    kline_data = {}
    source_evidence = {
        "kline": {}, "capital": {}, "fundamental": {},
    }

    def _record_source_evidence(source, ts_code, attempt):
        if isinstance(attempt, dict):
            source_evidence[source][ts_code] = copy.deepcopy(attempt)

    stage_start = time.monotonic()

    def _fetch_one_kline(c):
        ts_code = c["ts_code"]
        cache_only = _source_unavailable(source_health, "kline")
        fetch_kwargs = {"as_of_date": as_of_date}
        if cache_only:
            fetch_kwargs["cache_only"] = True
        wrapped = _evidenced_fetch(
            _fetch_kline, ts_code, **fetch_kwargs,
            usable=lambda payload: bool(
                payload and payload.get("data")
                and (not as_of_date
                     or latest_data_date(payload) >= as_of_date)
                and not payload.get("meta", {}).get("refresh_error")))
        kline, attempt = _unpack_source_result(wrapped)
        _record_source_evidence("kline", ts_code, attempt)
        kline_current = bool(
            kline and kline.get("data")
            and (not as_of_date or latest_data_date(kline) >= as_of_date)
            and not kline.get("meta", {}).get("refresh_error")
        )
        if kline and kline.get("data"):
            if not attempt.get("attempted") and metrics is not None:
                metrics["kline_cache_hits"] = metrics.get("kline_cache_hits", 0) + 1
            if attempt.get("attempted"):
                if kline_current and not attempt.get("reason"):
                    _source_succeeded(source_health, "kline")
                else:
                    _source_failed(source_health, "kline")
        else:
            if attempt.get("attempted"):
                _source_failed(source_health, "kline")
            if metrics is not None:
                metrics["kline_failures"] = metrics.get("kline_failures", 0) + 1
        return ts_code, kline, attempt

    if isinstance(source_health, RunSourceHealth):
        def _live_kline(candidate):
            return _evidenced_fetch(
                _fetch_kline, candidate["ts_code"], as_of_date=as_of_date,
                live_deadline=source_health.live_deadline,
                usable=lambda payload: bool(
                    payload and payload.get("data")
                    and (not as_of_date
                         or latest_data_date(payload) >= as_of_date)
                    and not payload.get("meta", {}).get("refresh_error")))

        fetched = bounded_source_map(
            "kline", candidates, source_health, _live_kline,
            lambda candidate: _cache_fetch(
                _fetch_kline, candidate["ts_code"],
                as_of_date=as_of_date),
            source_health.live_deadline, max_workers=max_workers,
            include_evidence=True)
        for item, result in fetched:
            payload, attempt = _unpack_source_result(result)
            kline_data[item["ts_code"]] = payload
            _record_source_evidence("kline", item["ts_code"], attempt)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one_kline, c) for c in candidates]
            for fut in as_completed(futures):
                ts_code, kline, _ = fut.result()
                kline_data[ts_code] = kline

    if metrics is not None:
        metrics["kline_seconds"] = metrics.get("kline_seconds", 0.0) + (
            time.monotonic() - stage_start)

    # Wyckoff is a cheap in-memory gate compared with capital/fundamental I/O.
    # Apply it before requesting those dimensions.
    analysis_by_ts = {}
    eligible_candidates = []
    wyckoff_start = time.monotonic()
    for candidate in candidates:
        ts_code = candidate["ts_code"]
        kline = kline_data.get(ts_code)
        records = kline.get("data", []) if kline else []
        if len(records) < 20:
            continue
        if enable_wyckoff:
            if len(records) < WYCKOFF_MIN_BARS:
                continue
            analysis = analyze_kline_dict(kline)
            if not wyckoff_gate_pass(analysis):
                continue
            analysis_by_ts[ts_code] = analysis
        eligible_candidates.append(candidate)
    if metrics is not None:
        metrics["wyckoff_seconds"] = metrics.get("wyckoff_seconds", 0.0) + (
            time.monotonic() - wyckoff_start)
        metrics["wyckoff_pass_count"] = (
            metrics.get("wyckoff_pass_count", 0) + len(eligible_candidates))

    # Fetch capital flow in parallel (only for stocks with K-line data)
    print(f"  Fetching capital flow data...", file=sys.stderr)
    capital_data = {}
    prefetched_fundamental = None

    stage_start = time.monotonic()

    def _fetch_capital_for_run(ts_code, **kwargs):
        result = _call_fetch_compat(
            _fetch_capital_flow, ts_code,
            {"expected_trading_date": as_of_date, **kwargs})
        if isinstance(result, dict) and "payload" not in result \
                and isinstance(result.get("data"), list):
            meta = result.get("meta")
            if not isinstance(meta, dict) or not (
                    meta.get("data_source") or meta.get("source")):
                result = copy.deepcopy(result)
                result.setdefault("meta", {})["data_source"] = (
                    "legacy_adapter")
        return result

    def _fetch_fundamental_for_run(ts_code, **kwargs):
        return _call_fetch_compat(
            _fetch_fundamental, ts_code,
            {"expected_trading_date": as_of_date, **kwargs})

    def _fetch_one_cap(c):
        ts_code = c["ts_code"]
        cache_only = _source_unavailable(source_health, "capital")
        fetch_kwargs = {"cache_only": True} if cache_only else {}
        wrapped = _evidenced_fetch(
            _fetch_capital_for_run, ts_code, **fetch_kwargs,
            usable=_usable_capital_payload)
        data, attempt = _unpack_source_result(wrapped)
        _record_source_evidence("capital", ts_code, attempt)
        usable = _usable_capital_payload(data)
        if attempt.get("attempted"):
            if usable and not attempt.get("reason"):
                _source_succeeded(source_health, "capital")
            else:
                _source_failed(source_health, "capital")
        return ts_code, data, attempt

    if isinstance(source_health, RunSourceHealth):
        with ThreadPoolExecutor(max_workers=2) as dimension_pool:
            capital_future = dimension_pool.submit(
                bounded_source_map,
                "capital", eligible_candidates, source_health,
                lambda candidate: _evidenced_fetch(
                    _fetch_capital_for_run, candidate["ts_code"],
                    live_deadline=source_health.live_deadline,
                    usable=_usable_capital_payload),
                lambda candidate: _cache_fetch(
                    _fetch_capital_for_run, candidate["ts_code"]),
                source_health.live_deadline, max_workers,
                include_evidence=True)
            fundamental_future = dimension_pool.submit(
                bounded_source_map,
                "fundamental", eligible_candidates, source_health,
                lambda candidate: _evidenced_fetch(
                    _fetch_fundamental_for_run, candidate["ts_code"],
                    live_deadline=source_health.live_deadline,
                    usable=_usable_fundamental_payload),
                lambda candidate: _cache_fetch(
                    _fetch_fundamental_for_run, candidate["ts_code"]),
                source_health.live_deadline, max_workers,
                include_evidence=True)
            fetched = capital_future.result()
            prefetched_fundamental = fundamental_future.result()
        for item, result in fetched:
            payload, attempt = _unpack_source_result(result)
            capital_data[item["ts_code"]] = payload
            _record_source_evidence("capital", item["ts_code"], attempt)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one_cap, c)
                       for c in eligible_candidates]
            for fut in as_completed(futures):
                ts_code, cap, _ = fut.result()
                capital_data[ts_code] = cap
    if metrics is not None:
        metrics["capital_seconds"] = metrics.get("capital_seconds", 0.0) + (
            time.monotonic() - stage_start)
        metrics["capital_requests"] = metrics.get("capital_requests", 0) + len(
            eligible_candidates)

    # Fetch fundamental data (prefer cache, parallel for misses)
    print(f"  Fetching fundamental data...", file=sys.stderr)
    fundamental_data = {}

    stage_start = time.monotonic()

    def _fetch_one_fund(c):
        ts_code = c["ts_code"]
        cache_only = _source_unavailable(source_health, "fundamental")
        fetch_kwargs = {}
        if cache_only:
            fetch_kwargs["cache_only"] = True
        wrapped = _evidenced_fetch(
            _fetch_fundamental_for_run, ts_code, **fetch_kwargs,
            usable=_usable_fundamental_payload)
        data, attempt = _unpack_source_result(wrapped)
        _record_source_evidence("fundamental", ts_code, attempt)
        usable = _usable_fundamental_payload(data)
        if attempt.get("attempted"):
            if usable and not attempt.get("reason"):
                _source_succeeded(source_health, "fundamental")
            else:
                _source_failed(source_health, "fundamental")
        return ts_code, data, attempt

    if isinstance(source_health, RunSourceHealth):
        for item, result in prefetched_fundamental or []:
            payload, attempt = _unpack_source_result(result)
            fundamental_data[item["ts_code"]] = payload
            _record_source_evidence(
                "fundamental", item["ts_code"], attempt)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one_fund, c)
                       for c in eligible_candidates]
            for fut in as_completed(futures):
                ts_code, fund, _ = fut.result()
                fundamental_data[ts_code] = fund
    if metrics is not None:
        metrics["fundamental_seconds"] = metrics.get(
            "fundamental_seconds", 0.0) + (time.monotonic() - stage_start)
        metrics["fundamental_requests"] = metrics.get(
            "fundamental_requests", 0) + len(eligible_candidates)

    # A live sector response contains one same-day PE field.  Use it only as
    # an explicit partial fallback after the independent fundamental fetch is
    # unusable; the recommendation quality gate still sees this as partial
    # fundamental coverage rather than pretending it is a full report.
    for candidate in eligible_candidates:
        ts_code = candidate["ts_code"]
        if _usable_fundamental_payload(fundamental_data.get(ts_code)):
            continue
        fallback = _same_day_membership_fundamental_fallback(
            candidate, as_of_date=as_of_date)
        if fallback is None:
            continue
        fundamental_data[ts_code] = fallback
        attempt = source_evidence["fundamental"].get(ts_code)
        if not isinstance(attempt, dict):
            attempt = live_attempt(attempted=False)
            source_evidence["fundamental"][ts_code] = attempt
        attempt["fallback_source"] = "sector_membership_quote"

    # Compute scores
    scored = []
    for c in eligible_candidates:
        ts = c["ts_code"]
        kline = kline_data.get(ts)
        cap = capital_data.get(ts)
        fund = fundamental_data.get(ts)

        # Skip stocks with no K-line data (can't score)
        if not kline:
            continue

        # Verify K-line data has enough records
        records = kline.get("data", [])
        if len(records) < 20:
            continue

        wk = analysis_by_ts.get(ts) if enable_wyckoff else None

        dim_momentum = score_momentum(c, kline)
        dim_volume = score_volume_price(c, kline)
        dim_capital = score_capital(c, cap)
        dim_fundamental = score_fundamental_quick(c, fund)
        memberships = c.get("sector_memberships") or [
            build_sector_membership(
                c.get("sector_code", ""), c.get("sector_name", ""),
                context={
                    "hot_score": c.get("sector_hot_score", 50),
                    "sector_actionable": c.get("sector_actionable", False),
                },
                stock=c,
            )
        ]
        primary_membership = select_primary_sector_membership(memberships)
        dim_sector = score_sector_membership(
            c, primary_membership, peer_cohorts)

        raw_dimensions = {
            "momentum": dim_momentum,
            "volume_price": dim_volume,
            "capital": dim_capital,
            "fundamental": dim_fundamental,
            "sector_strength": dim_sector,
        }

        if enable_wyckoff:
            dim_wyckoff = score_wyckoff(wk)
            raw_dimensions["wyckoff"] = dim_wyckoff
            # Rebalance: wyckoff takes 25%, others shrink proportionally.
        composite = composite_from_dimensions(raw_dimensions)
        dims = {
            name: round(value, 1)
            for name, value in raw_dimensions.items()
        }

        # Detect signals
        signals = _detect_signals(c, kline, cap, fund)

        # Detect warnings
        warnings = _detect_warnings(c, kline, cap, fund, dim_momentum, dim_volume,
                                     dim_fundamental)
        base_data_quality = assess_candidate_data(
            kline=kline,
            capital=cap,
            fundamental=fund,
            as_of_date=as_of_date,
        )
        data_quality = apply_membership_quality(
            base_data_quality, primary_membership, as_of_date=as_of_date)
        membership_source = primary_membership.get(
            "membership_source", "realtime")
        membership_date = primary_membership.get("membership_data_date", "")
        membership_quality = primary_membership.get(
            "membership_quality", "good")
        membership_cache_error = primary_membership.get(
            "membership_cache_error", "")
        raw_composite = round(composite, 1)
        quality_adjusted = round(
            raw_composite
            * data_quality["coverage_factor"]
            * data_quality["freshness_factor"],
            1,
        )

        # Sector-relative rank (within sector, by change_pct)
        sector_code = primary_membership.get(
            "code", c.get("sector_code", ""))
        changes_in_sector = sorted(peer_cohorts.get(sector_code, [c["change_pct"]]),
                                   reverse=True)
        sector_rank = changes_in_sector.index(c["change_pct"]) + 1 if c["change_pct"] in changes_in_sector else len(changes_in_sector)

        item = {
            "code": c["code"],
            "ts_code": ts,
            "name": c["name"],
            "sector_code": sector_code,
            "sector_name": primary_membership.get(
                "name", c.get("sector_name", sector_code)),
            "sector_hot_score": primary_membership.get(
                "hot_score", c.get("sector_hot_score", 50)),
            **_sector_membership_output_fields(primary_membership),
            "composite_score": raw_composite,
            "raw_composite_score": raw_composite,
            "quality_adjusted_score": quality_adjusted,
            "raw_dimensions": raw_dimensions,
            "dimensions": dims,
            "signals": signals,
            "warnings": warnings,
            "base_data_quality": copy.deepcopy(base_data_quality),
            "data_quality": data_quality,
            "membership_source": membership_source,
            "membership_data_date": membership_date,
            "membership_quality": membership_quality,
            "membership_cache_error": membership_cache_error,
            "membership_cache_at": primary_membership.get(
                "membership_cache_at", ""),
            "membership_cache_age_hours": primary_membership.get(
                "membership_cache_age_hours"),
            "membership_cache_tier": primary_membership.get(
                "membership_cache_tier", ""),
            "membership_fallback_reason": primary_membership.get(
                "membership_fallback_reason", ""),
            "membership_provider_attempts": primary_membership.get(
                "membership_provider_attempts", 0),
            "membership_fetch_evidence": copy.deepcopy(
                primary_membership.get("membership_fetch_evidence", {})),
            "source_evidence": {
                source: copy.deepcopy(source_evidence.get(source, {}).get(
                    ts, {}))
                for source in ("kline", "capital", "fundamental")
            } | {
                "membership": copy.deepcopy(
                    primary_membership.get("membership_fetch_evidence", {})),
            },
            "sector_memberships": merge_sector_memberships(memberships),
            "change_pct": c.get("change_pct", 0),
            "sector_relative_rank": sector_rank,
            "sector_total": len(changes_in_sector),
        }

        if enable_wyckoff and wk:
            short_term = wk.get("short_term") or {
                "phase": wk.get("phase", {}).get("primary", ""),
                "phase_name": wk.get("phase", {}).get("primary_name", ""),
                "sub_phase": wk.get("phase", {}).get("primary_sub_phase", ""),
                "sub_phase_name": wk.get("phase", {}).get("sub_phase_name", ""),
                "confidence": wk.get("phase", {}).get("confidence", 0),
                "signal_status": wk.get("signal", {}).get("status", "confirmed"),
                "signal_age_bars": wk.get("signal", {}).get("age_bars", 0),
            }
            long_term = wk.get("long_term") or {"eligible": False}
            item["wyckoff"] = {
                "phase": wk.get("phase", {}).get("primary_name", ""),
                "sub_phase": wk.get("phase", {}).get("sub_phase_name", ""),
                "confidence": wk.get("phase", {}).get("confidence", 0),
                "score": round(dim_wyckoff, 1),
                "verdict": wk.get("wyckoff_signals", {}).get("verdict", ""),
                "trading_implication": wk.get("wyckoff_signals", {}).get("trading_implication", ""),
                "minor_phase": short_term.get("minor_phase") or wk.get("phase", {}).get("minor_phase", {}),
                "short_term": short_term,
                "long_term": long_term,
                "alignment": wk.get("alignment") or build_period_alignment(short_term, long_term),
            }

        # K-lines are already prefetched above; attach an additive plan without
        # introducing another network request.  Policy is optional for legacy callers.
        if trade_plan_policy is not None:
            try:
                item["trade_plan"] = build_candidate_trade_plan(
                    c["code"], kline_data.get(ts), item.get("wyckoff", {}),
                    trade_plan_policy, as_of_date,
                    _candidate_counterargument(item))
                verdict = validate_trade_plan(
                    item["trade_plan"], trade_plan_policy, as_of_date)
                item["trade_plan_status"] = (
                    "complete" if verdict["complete"] else "incomplete")
                item["trade_plan_reasons"] = verdict["reasons"]
                item["trade_plan_target_source"] = (
                    (item.get("trade_plan") or {}).get("target_source")
                    or "unavailable")
            except (KeyError, TypeError, ValueError):
                item["trade_plan"] = None
                item["trade_plan_status"] = "error"
                item["trade_plan_reasons"] = ["trade_plan_build_error"]
                item["trade_plan_target_source"] = "unavailable"

        scored.append(item)

    return scored


# ──────────────────────── Prioritized Phase 2 ────────────────────────


def run_phase2(candidates, max_workers=4, enable_wyckoff=False,
               as_of_date="", source_health=None, metrics=None,
               trade_plan_policy=None, top=30, min_candidates=20):
    """Score candidates with bounded K-line work and prioritized enrichment.

    K-line/Wyckoff is completed first.  Capital and fundamental cache probes
    then cover the whole qualified set without provider calls; only the
    highest provisional-score cache misses enter live enrichment batches.
    Capital remains a hard quality gate even though it is neutral in the
    provisional queue score.
    """
    candidates = list(candidates or [])
    print(f"[Phase 2/3] Scoring {len(candidates)} candidates...", file=sys.stderr)
    if not candidates:
        return []

    metrics_ref = metrics if isinstance(metrics, dict) else {}
    try:
        worker_count = max(1, int(max_workers))
    except (TypeError, ValueError):
        worker_count = 4
    try:
        requested_min_candidates = max(0, int(min_candidates))
    except (TypeError, ValueError):
        requested_min_candidates = 20

    peer_cohorts = build_sector_peer_cohorts(candidates)
    kline_data = {}
    source_evidence = {
        "kline": {}, "capital": {}, "fundamental": {},
    }

    def _record_source_evidence(source, ts_code, attempt):
        if isinstance(attempt, dict):
            source_evidence[source][ts_code] = copy.deepcopy(attempt)

    def _kline_usable(payload):
        return bool(
            payload and isinstance(payload, dict)
            and payload.get("data")
            and (not as_of_date or latest_data_date(payload) >= as_of_date)
            and not payload.get("meta", {}).get("refresh_error")
            and _validate_kline_cache(payload, as_of_date)["valid"]
        )

    print("  Fetching K-line data...", file=sys.stderr)
    kline_started = time.monotonic()

    def _fetch_one_kline(candidate):
        ts_code = candidate["ts_code"]
        cache_only = _source_unavailable(source_health, "kline")
        kwargs = {"as_of_date": as_of_date}
        if cache_only:
            kwargs["cache_only"] = True
        try:
            wrapped = _evidenced_fetch(
                _fetch_kline, ts_code, **kwargs, usable=_kline_usable)
            payload, attempt = _unpack_source_result(wrapped)
        except Exception as exc:
            payload = None
            attempt = live_attempt(
                attempted=True, provider_attempts=1,
                reason=classify_failure(exc))
        evidence = _normalize_source_evidence(
            "kline", payload, attempt,
            cache_probe=not attempt.get("attempted"), usable=_kline_usable)
        _record_source_evidence("kline", ts_code, evidence)
        if evidence.get("attempted"):
            if _kline_usable(payload) and not evidence.get("reason"):
                _source_succeeded(source_health, "kline")
            else:
                _source_failed(source_health, "kline")
        elif evidence.get("status") == "cache_valid":
            metrics_ref["kline_cache_hits"] = (
                metrics_ref.get("kline_cache_hits", 0) + 1)
        if not _kline_usable(payload):
            metrics_ref["kline_failures"] = (
                metrics_ref.get("kline_failures", 0) + 1)
        return ts_code, payload, evidence

    if isinstance(source_health, RunSourceHealth):
        def _live_kline(candidate):
            return _evidenced_fetch(
                _fetch_kline, candidate["ts_code"],
                as_of_date=as_of_date,
                live_deadline=source_health.kline_deadline,
                usable=_kline_usable)

        fetched = bounded_source_map(
            "kline", candidates, source_health, _live_kline,
            lambda candidate: _cache_fetch(
                _fetch_kline, candidate["ts_code"], as_of_date=as_of_date),
            source_health.kline_deadline,
            max_workers=min(worker_count, MAX_IN_FLIGHT["kline"]),
            cache_usable=_kline_usable,
            include_evidence=True)
        for item, result in fetched:
            payload, attempt = _unpack_source_result(result)
            evidence = _normalize_source_evidence(
                "kline", payload, attempt,
                usable=_kline_usable)
            if (not attempt.get("attempted") and
                    attempt.get("reason") == "deadline" and
                    not _kline_usable(payload)):
                evidence["status"] = "not_started_deadline"
            kline_data[item["ts_code"]] = payload
            _record_source_evidence("kline", item["ts_code"], evidence)
    else:
        with ThreadPoolExecutor(
                max_workers=min(worker_count, MAX_IN_FLIGHT["kline"])) as pool:
            futures = [pool.submit(_fetch_one_kline, candidate)
                       for candidate in candidates]
            for future in as_completed(futures):
                ts_code, payload, _ = future.result()
                kline_data[ts_code] = payload

    metrics_ref["kline_seconds"] = metrics_ref.get("kline_seconds", 0.0) + (
        time.monotonic() - kline_started)

    # Wyckoff is intentionally in-memory and follows the K-line deadline.
    analysis_by_ts = {}
    eligible_candidates = []
    wyckoff_started = time.monotonic()
    for candidate in candidates:
        ts_code = candidate["ts_code"]
        kline = kline_data.get(ts_code)
        records = kline.get("data", []) if isinstance(kline, dict) else []
        if len(records) < 20:
            continue
        if enable_wyckoff:
            if len(records) < WYCKOFF_MIN_BARS:
                continue
            analysis = analyze_kline_dict(kline)
            if not wyckoff_gate_pass(analysis):
                continue
            analysis_by_ts[ts_code] = analysis
        eligible_candidates.append(candidate)
    metrics_ref["wyckoff_seconds"] = metrics_ref.get("wyckoff_seconds", 0.0) + (
        time.monotonic() - wyckoff_started)
    metrics_ref["wyckoff_pass_count"] = (
        metrics_ref.get("wyckoff_pass_count", 0) + len(eligible_candidates))

    def _fetch_capital_for_run(ts_code, **kwargs):
        result = _call_fetch_compat(
            _fetch_capital_flow, ts_code,
            {"expected_trading_date": as_of_date, **kwargs})
        if isinstance(result, dict) and "payload" not in result \
                and isinstance(result.get("data"), list):
            meta = result.get("meta")
            if not isinstance(meta, dict) or not (
                    meta.get("data_source") or meta.get("source")):
                result = copy.deepcopy(result)
                result.setdefault("meta", {})["data_source"] = (
                    "legacy_adapter")
        return result

    def _fetch_fundamental_for_run(ts_code, **kwargs):
        return _call_fetch_compat(
            _fetch_fundamental, ts_code,
            {"expected_trading_date": as_of_date, **kwargs})

    # Probe both caches before admitting any live provider work.  These are
    # direct file reads, rather than fetcher calls, so a probe cannot consume
    # provider budget and test doubles remain one-call-per-live-candidate.
    capital_data = {}
    fundamental_data = {}
    fundamental_fallback_data = {}
    capital_cache_valid_codes = set()
    fundamental_cache_valid_codes = set()
    cache_probe_started = time.monotonic()

    def _probe(candidate, source):
        try:
            wrapped = (
                _probe_capital_cache(candidate["ts_code"], as_of_date)
                if source == "capital" else
                _probe_fundamental_cache(candidate["ts_code"], as_of_date)
            )
            payload, attempt = _unpack_source_result(wrapped)
        except Exception as exc:
            payload = None
            attempt = live_attempt(
                attempted=False, reason="cache_miss", status="cache_miss")
        usable = (_usable_capital_payload if source == "capital"
                  else _usable_fundamental_payload)
        evidence = _normalize_source_evidence(
            source, payload, attempt, cache_probe=True, usable=usable)
        return candidate["ts_code"], source, payload, evidence, usable(payload)

    probe_jobs = []
    with ThreadPoolExecutor(max_workers=min(worker_count, 8)) as pool:
        for candidate in eligible_candidates:
            probe_jobs.append(pool.submit(_probe, candidate, "capital"))
            probe_jobs.append(pool.submit(_probe, candidate, "fundamental"))
        for future in as_completed(probe_jobs):
            ts_code, source, payload, evidence, usable = future.result()
            _record_source_evidence(source, ts_code, evidence)
            if (isinstance(source_health, RunSourceHealth)
                    and evidence.get("status") == "cache_valid"):
                source_health.record_cache_result(
                    source, payload, stale=False, reason="cache_valid")
            if source == "capital":
                if usable:
                    capital_data[ts_code] = payload
                    capital_cache_valid_codes.add(ts_code)
                else:
                    capital_data[ts_code] = None
            elif usable:
                fundamental_data[ts_code] = payload
                fundamental_cache_valid_codes.add(ts_code)
            else:
                fundamental_data[ts_code] = None

    metrics_ref["capital_cache_valid_count"] = (
        metrics_ref.get("capital_cache_valid_count", 0)
        + len(capital_cache_valid_codes))
    metrics_ref["capital_enrichment_population"] = (
        metrics_ref.get("capital_enrichment_population", 0)
        + len(eligible_candidates))

    # Membership PE is a valid partial fundamental fallback for every
    # candidate, but a priority candidate still gets a live fundamental
    # request when its full fundamental cache is missing.
    for candidate in eligible_candidates:
        ts_code = candidate["ts_code"]
        if ts_code in fundamental_cache_valid_codes:
            continue
        fallback = _same_day_membership_fundamental_fallback(
            candidate, as_of_date=as_of_date)
        if fallback is not None:
            fundamental_fallback_data[ts_code] = fallback
            fundamental_data[ts_code] = fallback
            evidence = source_evidence["fundamental"].setdefault(
                ts_code, live_attempt(attempted=False))
            evidence["fallback_source"] = "sector_membership_quote"
            # This is a live, same-day membership observation, not a cache
            # hit.  It is valid partial evidence when no priority fetch is
            # requested for the candidate.
            evidence["status"] = "live_success"
            evidence["cache_used"] = False

    def _provisional_score(candidate):
        ts_code = candidate["ts_code"]
        kline = kline_data.get(ts_code)
        fund = fundamental_data.get(ts_code) \
            or fundamental_fallback_data.get(ts_code)
        memberships = candidate.get("sector_memberships") or [
            build_sector_membership(
                candidate.get("sector_code", ""),
                candidate.get("sector_name", ""),
                context={
                    "hot_score": candidate.get("sector_hot_score", 50),
                    "sector_actionable": candidate.get(
                        "sector_actionable", False),
                }, stock=candidate)
        ]
        primary = select_primary_sector_membership(memberships)
        dimensions = {
            "momentum": score_momentum(candidate, kline),
            "volume_price": score_volume_price(candidate, kline),
            # Capital is deliberately neutral for queue ordering only.
            "capital": 50.0,
            "fundamental": score_fundamental_quick(candidate, fund),
            "sector_strength": score_sector_membership(
                candidate, primary, peer_cohorts),
        }
        if enable_wyckoff:
            dimensions["wyckoff"] = score_wyckoff(
                analysis_by_ts.get(ts_code))
        return composite_from_dimensions(dimensions)

    priority_inputs = [
        {**candidate, "provisional_score": _provisional_score(candidate)}
        for candidate in eligible_candidates
    ]
    queue_info = rank_capital_enrichment_candidates(
        priority_inputs, capital_data=capital_data, top=top,
        batch_size=CAPITAL_PREFETCH_BATCH_SIZE,
        prefetch_limit=CAPITAL_PREFETCH_LIMIT,
        expected_trading_date=as_of_date)
    priority_queue = queue_info["priority_queue"]
    queue_by_code = {candidate["code"]: candidate
                     for candidate in priority_queue}
    metrics_ref["capital_priority_count"] = (
        metrics_ref.get("capital_priority_count", 0) + len(priority_queue))

    # The priority scope is the live capital queue.  A valid capital cache is
    # already complete for the hard gate and never enters provider work.
    for candidate in priority_queue:
        ts_code = candidate["ts_code"]
        if ts_code not in fundamental_cache_valid_codes:
            fundamental_data[ts_code] = None

    def _safe_live(fetcher, candidate, usable):
        try:
            return _evidenced_fetch(
                fetcher, candidate["ts_code"],
                live_deadline=(source_health.live_deadline
                               if isinstance(source_health, RunSourceHealth)
                               else None),
                usable=usable)
        except Exception as exc:
            reason = classify_failure(exc)
            return source_result(None, live_attempt(
                attempted=True, provider_attempts=1, reason=reason,
                status=reason))

    def _run_live_batch(source, batch):
        if not batch:
            return []
        usable = (_usable_capital_payload if source == "capital"
                  else _usable_fundamental_payload)
        fetcher = (_fetch_capital_for_run if source == "capital"
                   else _fetch_fundamental_for_run)
        if isinstance(source_health, RunSourceHealth):
            def live(candidate):
                return _safe_live(fetcher, candidate, usable)

            def cache(candidate):
                return _cache_fetch(fetcher, candidate["ts_code"])

            return bounded_source_map(
                source, batch, source_health, live, cache,
                source_health.live_deadline,
                max_workers=min(worker_count, MAX_IN_FLIGHT[source]),
                cache_usable=usable, include_evidence=True)
        if _source_unavailable(source_health, source):
            return [(
                candidate,
                source_result(None, live_attempt(
                    attempted=False, reason="source_unavailable",
                    status="source_unavailable")),
            ) for candidate in batch]
        results = []
        with ThreadPoolExecutor(
                max_workers=min(worker_count, MAX_IN_FLIGHT[source])) as pool:
            futures = {pool.submit(_safe_live, fetcher, candidate, usable): candidate
                       for candidate in batch}
            for future, candidate in list(futures.items()):
                try:
                    results.append((candidate, future.result()))
                except Exception as exc:
                    reason = classify_failure(exc)
                    results.append((candidate, source_result(
                        None, live_attempt(
                            attempted=True, provider_attempts=1,
                            reason=reason, status=reason))))
        return results

    def _consume_live_results(source, results):
        usable = (_usable_capital_payload if source == "capital"
                  else _usable_fundamental_payload)
        for candidate, result in results:
            ts_code = candidate["ts_code"]
            payload, attempt = _unpack_source_result(result)
            evidence = _normalize_source_evidence(
                source, payload, attempt, usable=usable)
            if not attempt.get("attempted"):
                if attempt.get("reason") == "deadline":
                    evidence["status"] = "not_started_deadline"
                    evidence["cache_used"] = False
                elif attempt.get("reason") == "source_unavailable":
                    evidence["status"] = "source_unavailable"
                    evidence["cache_used"] = False
            _record_source_evidence(source, ts_code, evidence)
            if source == "capital":
                capital_data[ts_code] = payload if usable(payload) else None
                if evidence.get("attempted") and evidence.get(
                        "status") not in ("cache_valid", "cache_miss",
                                           "cache_stale"):
                    metrics_ref["capital_live_started"] = (
                        metrics_ref.get("capital_live_started", 0) + 1)
                if evidence.get("status") == "live_success":
                    metrics_ref["capital_live_success_count"] = (
                        metrics_ref.get("capital_live_success_count", 0) + 1)
            else:
                if usable(payload):
                    fundamental_data[ts_code] = payload
                elif ts_code in fundamental_fallback_data:
                    # Keep the verified same-day membership quote usable when
                    # the optional full fundamental provider fails, while
                    # retaining the provider reason for diagnostics.
                    if evidence.get("reason"):
                        evidence["provider_reason"] = evidence["reason"]
                    evidence["fallback_source"] = "sector_membership_quote"
                    evidence["status"] = "live_success"
                    evidence["cache_used"] = False
                    fundamental_data[ts_code] = fundamental_fallback_data[ts_code]
                else:
                    fundamental_data[ts_code] = None
            _record_source_evidence(source, ts_code, evidence)
            if (not isinstance(source_health, RunSourceHealth)
                    and evidence.get("attempted")):
                if usable(payload) and not evidence.get("reason"):
                    _source_succeeded(source_health, source)
                else:
                    _source_failed(source_health, source)

    def _score_current():
        """Recompute all dimensions, plans, and quality after each batch."""
        scored = []
        for candidate in eligible_candidates:
            ts_code = candidate["ts_code"]
            kline = kline_data.get(ts_code)
            records = kline.get("data", []) if isinstance(kline, dict) else []
            if not kline or len(records) < 20:
                continue
            cap = capital_data.get(ts_code)
            fund = fundamental_data.get(ts_code)
            wk = analysis_by_ts.get(ts_code) if enable_wyckoff else None
            dim_momentum = score_momentum(candidate, kline)
            dim_volume = score_volume_price(candidate, kline)
            dim_capital = score_capital(candidate, cap)
            dim_fundamental = score_fundamental_quick(candidate, fund)
            memberships = candidate.get("sector_memberships") or [
                build_sector_membership(
                    candidate.get("sector_code", ""),
                    candidate.get("sector_name", ""),
                    context={
                        "hot_score": candidate.get("sector_hot_score", 50),
                        "sector_actionable": candidate.get(
                            "sector_actionable", False),
                    }, stock=candidate)
            ]
            primary_membership = select_primary_sector_membership(memberships)
            dim_sector = score_sector_membership(
                candidate, primary_membership, peer_cohorts)
            raw_dimensions = {
                "momentum": dim_momentum,
                "volume_price": dim_volume,
                "capital": dim_capital,
                "fundamental": dim_fundamental,
                "sector_strength": dim_sector,
            }
            if enable_wyckoff:
                raw_dimensions["wyckoff"] = score_wyckoff(wk)
            composite = composite_from_dimensions(raw_dimensions)
            dims = {name: round(value, 1)
                    for name, value in raw_dimensions.items()}
            signals = _detect_signals(candidate, kline, cap, fund)
            warnings = _detect_warnings(
                candidate, kline, cap, fund, dim_momentum, dim_volume,
                dim_fundamental)
            base_data_quality = assess_candidate_data(
                kline=kline, capital=cap, fundamental=fund,
                as_of_date=as_of_date,
                source_evidence=source_evidence | {
                    "capital": source_evidence["capital"].get(
                        ts_code, {}),
                    "fundamental": source_evidence["fundamental"].get(
                        ts_code, {}),
                })
            data_quality = apply_membership_quality(
                base_data_quality, primary_membership, as_of_date=as_of_date)
            raw_composite = round(composite, 1)
            quality_adjusted = round(
                raw_composite * data_quality["coverage_factor"]
                * data_quality["freshness_factor"], 1)
            sector_code = primary_membership.get(
                "code", candidate.get("sector_code", ""))
            changes_in_sector = sorted(
                peer_cohorts.get(sector_code, [candidate.get("change_pct", 0)]),
                reverse=True)
            change_pct = candidate.get("change_pct", 0)
            sector_rank = (
                changes_in_sector.index(change_pct) + 1
                if change_pct in changes_in_sector else len(changes_in_sector))
            item = {
                "code": candidate["code"], "ts_code": ts_code,
                "name": candidate["name"], "sector_code": sector_code,
                "sector_name": primary_membership.get(
                    "name", candidate.get("sector_name", sector_code)),
                "sector_hot_score": primary_membership.get(
                    "hot_score", candidate.get("sector_hot_score", 50)),
                **_sector_membership_output_fields(primary_membership),
                "composite_score": raw_composite,
                "raw_composite_score": raw_composite,
                "quality_adjusted_score": quality_adjusted,
                "raw_dimensions": raw_dimensions, "dimensions": dims,
                "signals": signals, "warnings": warnings,
                "base_data_quality": copy.deepcopy(base_data_quality),
                "data_quality": data_quality,
                "membership_source": primary_membership.get(
                    "membership_source", "realtime"),
                "membership_data_date": primary_membership.get(
                    "membership_data_date", ""),
                "membership_quality": primary_membership.get(
                    "membership_quality", "good"),
                "membership_cache_error": primary_membership.get(
                    "membership_cache_error", ""),
                "membership_cache_at": primary_membership.get(
                    "membership_cache_at", ""),
                "membership_cache_age_hours": primary_membership.get(
                    "membership_cache_age_hours"),
                "membership_cache_tier": primary_membership.get(
                    "membership_cache_tier", ""),
                "membership_fallback_reason": primary_membership.get(
                    "membership_fallback_reason", ""),
                "membership_provider_attempts": primary_membership.get(
                    "membership_provider_attempts", 0),
                "membership_fetch_evidence": copy.deepcopy(
                    primary_membership.get("membership_fetch_evidence", {})),
                "source_evidence": {
                    source: copy.deepcopy(source_evidence.get(source, {}).get(
                        ts_code, {}))
                    for source in ("kline", "capital", "fundamental")
                } | {
                    "membership": copy.deepcopy(primary_membership.get(
                        "membership_fetch_evidence", {})),
                },
                "sector_memberships": merge_sector_memberships(memberships),
                "change_pct": change_pct,
                "sector_relative_rank": sector_rank,
                "sector_total": len(changes_in_sector),
            }
            if enable_wyckoff and wk:
                short_term = wk.get("short_term") or {
                    "phase": wk.get("phase", {}).get("primary", ""),
                    "phase_name": wk.get("phase", {}).get("primary_name", ""),
                    "sub_phase": wk.get("phase", {}).get("primary_sub_phase", ""),
                    "sub_phase_name": wk.get("phase", {}).get("sub_phase_name", ""),
                    "confidence": wk.get("phase", {}).get("confidence", 0),
                    "signal_status": wk.get("signal", {}).get("status", "confirmed"),
                    "signal_age_bars": wk.get("signal", {}).get("age_bars", 0),
                }
                long_term = wk.get("long_term") or {"eligible": False}
                item["wyckoff"] = {
                    "phase": wk.get("phase", {}).get("primary_name", ""),
                    "sub_phase": wk.get("phase", {}).get("sub_phase_name", ""),
                    "confidence": wk.get("phase", {}).get("confidence", 0),
                    "score": round(raw_dimensions["wyckoff"], 1),
                    "verdict": wk.get("wyckoff_signals", {}).get("verdict", ""),
                    "trading_implication": wk.get("wyckoff_signals", {}).get(
                        "trading_implication", ""),
                    "minor_phase": short_term.get("minor_phase") or wk.get(
                        "phase", {}).get("minor_phase", {}),
                    "short_term": short_term, "long_term": long_term,
                    "alignment": wk.get("alignment") or build_period_alignment(
                        short_term, long_term),
                }
            if trade_plan_policy is not None:
                try:
                    item["trade_plan"] = build_candidate_trade_plan(
                        candidate["code"], kline, item.get("wyckoff", {}),
                        trade_plan_policy, as_of_date,
                        _candidate_counterargument(item))
                    verdict = validate_trade_plan(
                        item["trade_plan"], trade_plan_policy, as_of_date)
                    item["trade_plan_status"] = (
                        "complete" if verdict["complete"] else "incomplete")
                    item["trade_plan_reasons"] = verdict["reasons"]
                    item["trade_plan_target_source"] = (
                        item.get("trade_plan", {}).get("target_source")
                        or "unavailable")
                except (KeyError, TypeError, ValueError):
                    item["trade_plan"] = None
                    item["trade_plan_status"] = "error"
                    item["trade_plan_reasons"] = ["trade_plan_build_error"]
                    item["trade_plan_target_source"] = "unavailable"
            scored.append(item)
        return scored

    def _quality_valid(scored):
        return [item for item in scored
                if item.get("data_quality", {}).get("eligible", False)]

    def _can_stop(scored, remaining):
        if requested_min_candidates <= 0:
            return True
        valid = _quality_valid(scored)
        if len(valid) < requested_min_candidates:
            return False
        provisional_by_code = {
            candidate["code"]: _safe_float(
                candidate.get("provisional_score"), 0.0)
            for candidate in priority_queue
        }
        valid_scores = sorted(
            (provisional_by_code.get(item.get("code"),
                                    _safe_float(item.get("composite_score")))
             for item in valid), reverse=True)
        cutoff = valid_scores[requested_min_candidates - 1]
        return not any(
            provisional_by_code.get(item["code"], 0.0) > cutoff
            for item in remaining)

    capital_started = time.monotonic()
    processed_codes = set()
    batches = queue_info["batches"]
    # Queue ordering uses the provisional score; defer full output assembly
    # until a live/cache enrichment batch has completed.  This avoids doing
    # trade-plan work twice for the first batch while retaining the required
    # re-score after every batch.
    latest_scored = []
    for batch in batches:
        if (isinstance(source_health, RunSourceHealth)
                and time.monotonic() >= source_health.live_deadline):
            break
        # Fundamental live work is scoped to exactly the same priority
        # batches as capital.  Both dimensions share the 170s deadline.
        capital_results = []
        fundamental_results = []
        with ThreadPoolExecutor(max_workers=2) as dimension_pool:
            capital_future = dimension_pool.submit(
                _run_live_batch, "capital", batch)
            fundamental_batch = [
                candidate for candidate in batch
                if candidate["ts_code"] not in fundamental_cache_valid_codes
            ]
            fundamental_future = dimension_pool.submit(
                _run_live_batch, "fundamental", fundamental_batch)
            capital_results = capital_future.result()
            fundamental_results = fundamental_future.result()
        _consume_live_results("capital", capital_results)
        _consume_live_results("fundamental", fundamental_results)
        processed_codes.update(candidate["code"] for candidate in batch)
        latest_scored = _score_current()
        remaining = [candidate for candidate in priority_queue
                     if candidate["code"] not in processed_codes]
        if _can_stop(latest_scored, remaining):
            break

    # Explicitly distinguish budget omission from provider failure.  A
    # priority candidate whose batch never started is a deadline omission;
    # candidates outside the initial queue are simply not selected.
    deadline_reached = (
        isinstance(source_health, RunSourceHealth)
        and time.monotonic() >= source_health.live_deadline)
    status_changed = False
    for candidate in eligible_candidates:
        ts_code = candidate["ts_code"]
        if ts_code in capital_cache_valid_codes:
            continue
        evidence = source_evidence["capital"].setdefault(
            ts_code, live_attempt(attempted=False))
        current_status = evidence.get("status", "")
        if current_status in SOURCE_EVIDENCE_STATUSES \
                and current_status not in ("cache_miss", "cache_stale"):
            continue
        if candidate["code"] in processed_codes:
            continue
        status = (
            "not_started_deadline" if deadline_reached
            and candidate["code"] in queue_by_code
            else "not_selected_for_enrichment")
        if current_status == "cache_stale":
            evidence["cache_status"] = current_status
        status_changed = status_changed or current_status != status
        evidence["status"] = status
        evidence["reason"] = status
        evidence["cache_used"] = False
        evidence["stale"] = False
        source_evidence["capital"][ts_code] = evidence

    # Candidates in the live scope with no fundamental cache and no completed
    # fundamental result receive the same explicit non-provider status.  A
    # membership fallback remains usable for candidates outside that scope.
    for candidate in eligible_candidates:
        ts_code = candidate["ts_code"]
        if ts_code in fundamental_cache_valid_codes \
                or ts_code in fundamental_fallback_data \
                and candidate["code"] not in queue_by_code:
            continue
        evidence = source_evidence["fundamental"].setdefault(
            ts_code, live_attempt(attempted=False))
        if evidence.get("status") in SOURCE_EVIDENCE_STATUSES \
                and evidence.get("status") not in ("cache_miss", "cache_stale"):
            continue
        if candidate["code"] in processed_codes:
            continue
        status = (
            "not_started_deadline" if deadline_reached
            and candidate["code"] in queue_by_code
            else "not_selected_for_enrichment")
        if evidence.get("status") == "cache_stale":
            evidence["cache_status"] = evidence.get("status")
        status_changed = status_changed or evidence.get("status") != status
        evidence.update({
            "status": status, "reason": status,
            "cache_used": False, "stale": False,
        })
        source_evidence["fundamental"][ts_code] = evidence

    # Re-score after all statuses are final; this is the only result returned
    # to callers, so a provisional neutral capital score cannot be promoted.
    if not latest_scored or status_changed:
        latest_scored = _score_current()
    metrics_ref["capital_valid_count"] = (
        metrics_ref.get("capital_valid_count", 0)
        + sum(_usable_capital_payload(capital_data.get(candidate["ts_code"]))
              for candidate in eligible_candidates))
    metrics_ref["capital_skipped_by_budget"] = (
        metrics_ref.get("capital_skipped_by_budget", 0)
        + sum(
            source_evidence["capital"].get(candidate["ts_code"], {}).get(
                "status") in {"not_selected_for_enrichment",
                                "not_started_deadline"}
            for candidate in eligible_candidates))
    metrics_ref["capital_seconds"] = metrics_ref.get("capital_seconds", 0.0) + (
        time.monotonic() - capital_started +
        time.monotonic() - cache_probe_started) / 2
    metrics_ref["fundamental_seconds"] = metrics_ref.get(
        "fundamental_seconds", 0.0) + (time.monotonic() - capital_started)
    metrics_ref["capital_requests"] = metrics_ref.get(
        "capital_requests", 0) + len(priority_queue)
    metrics_ref["fundamental_requests"] = metrics_ref.get(
        "fundamental_requests", 0) + sum(
            candidate["ts_code"] not in fundamental_cache_valid_codes
            for candidate in priority_queue)
    return latest_scored


# ──────────────────────── Signal & Warning Detection ────────────────────────


def _detect_signals(candidate, kline_data, capital_data, fundamental_data):
    """Detect positive signals for a stock."""
    signals = {}

    if not kline_data:
        return signals

    closes = _compute_close_prices(kline_data)

    # MA alignment
    ma5_val = ma(closes, 5)
    ma20_val = ma(closes, 20)
    ma60_val = ma(closes, 60) if len(closes) >= 60 else ma20_val
    if ma5_val and ma20_val and ma60_val:
        if ma5_val > ma20_val > ma60_val:
            signals["ma_alignment"] = "多头排列"
        elif ma5_val > ma20_val:
            signals["ma_alignment"] = "短期偏多"
        elif ma5_val < ma20_val < ma60_val:
            signals["ma_alignment"] = "空头排列"

    # Volume breakout
    records = kline_data.get("data", [])
    if len(records) >= 5:
        recent = records[-5:]
        avg_vol = sum(r.get("vol", 0) for r in recent) / len(recent)
        latest = records[-1]
        vol_ratio = latest.get("vol", 0) / avg_vol if avg_vol > 0 else 1.0
        close = latest.get("close", 0)
        if vol_ratio > 1.3 and ma20_val and close > ma20_val:
            signals["volume_breakout"] = True

    # Capital streak
    if capital_data:
        ext = capital_data.get("data_extended", {})
        istreak = ext.get("individual_streak", {})
        if istreak:
            ms = istreak.get("main_streak", 0)
            if ms > 0:
                signals["capital_streak"] = ms

        nb = ext.get("northbound_individual", {})
        if nb and isinstance(nb, dict):
            chg = nb.get("change_shares")
            if chg is not None and chg > 0:
                signals["northbound_adding"] = True

    # Fundamental signals
    if fundamental_data:
        summary = fundamental_data.get("summary", {})
        pe_pct = summary.get("pe_percentile_3y")
        if pe_pct is not None:
            signals["pe_percentile_3y"] = pe_pct
        roe = summary.get("roe")
        if roe is not None:
            signals["roe"] = roe

    return signals


def _detect_warnings(candidate, kline_data, capital_data, fundamental_data,
                     dim_momentum, dim_volume, dim_fundamental):
    """Detect warning signals."""
    warnings = []

    # High momentum but low volume support → divergence
    if dim_momentum > 70 and dim_volume < 40:
        warnings.append("量价背离：动量强但量能不足")

    # Low fundamental but high momentum → speculation risk
    if dim_momentum > 70 and dim_fundamental < 40:
        warnings.append("短期炒作风险：基本面弱但动量强")

    return warnings


def _candidate_counterargument(item):
    """Return one stable human-readable risk statement for every candidate."""
    warnings = item.get("warnings") or []
    if warnings:
        return "；".join(str(warning) for warning in warnings)
    return "若量价确认失败或收盘跌破结构支撑，则交易逻辑失效"


# ──────────────────────── Phase 3: Rank + Output ────────────────────────


def assign_stars(composite_score):
    """Assign star rating based on composite score."""
    if composite_score >= 80:
        return 3
    elif composite_score >= 65:
        return 2
    elif composite_score >= 50:
        return 1
    return 0


def build_output(scored, candidates, excluded, sector_map, elapsed, source="market_theme"):
    """Build final output JSON."""
    # Sort by composite_score descending
    scored.sort(key=lambda x: x["composite_score"], reverse=True)

    # Assign stars
    for s in scored:
        s["stars"] = assign_stars(s["composite_score"])

    # Build sector summary
    sector_summary = {}
    for sc in sector_map:
        sector_stocks = [s for s in scored if s["sector_code"] == sc]
        if sector_stocks:
            avg_score = sum(s["composite_score"] for s in sector_stocks) / len(sector_stocks)
            sector_summary[sc] = {
                "name": sector_map[sc]["name"],
                "hot_score": sector_map[sc]["hot_score"],
                "stock_count": len(sector_stocks),
                "avg_score": round(avg_score, 1),
            }

    output = {
        "meta": {
            "scan_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "source": source,
            "input_sectors": list(sector_map.keys()),
            "candidate_count": len(candidates),
            "scored_count": len(scored),
            "elapsed_seconds": round(elapsed, 1),
        },
        "rankings": scored,
        "sector_summary": sector_summary,
        "excluded": excluded[:30],  # cap at 30 for readability
    }

    return output


# ──────────────────────── Main ────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="A股个股筛选器 — Scan A-stocks in hot sectors"
    )
    parser.add_argument("--sectors", type=str,
                        help="板块代码列表, 逗号分隔 (e.g. BK0477,BK0897)")
    parser.add_argument("--from-leader", type=str,
                        help="从 market_leader JSON 输出文件读取板块")
    parser.add_argument("--top", type=int, default=10,
                        help="输出前N只股票 (默认10)")
    parser.add_argument("--min-score", type=float, default=50,
                        help="最低综合分阈值 (默认50)")
    parser.add_argument("--wyckoff", action="store_true",
                        help="启用维科夫选股漏斗:只保留吸筹/拉升阶段买点候选(Spring/LPS/ST/JAC等)")
    args = parser.parse_args()

    # Determine sector codes
    sector_codes = []
    source = "manual"

    if args.from_leader:
        leader_data = _read_json(args.from_leader)
        if leader_data:
            source = "market_leader"
            for sec in leader_data.get("sectors_analyzed", []):
                sector_codes.append(sec.get("code", ""))
            # Also enrich sector_map from leader data
            if not sector_codes:
                print("Error: no sectors found in leader output", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"Error: cannot read leader file: {args.from_leader}", file=sys.stderr)
            sys.exit(1)

    if args.sectors:
        source = "market_theme" if not args.from_leader else source
        sector_codes.extend([s.strip() for s in args.sectors.split(",") if s.strip()])

    if not sector_codes:
        parser.error("Provide --sectors or --from-leader")

    # Remove duplicates
    sector_codes = list(dict.fromkeys(sector_codes))

    start = time.time()

    # Phase 1
    print(f"[Phase 1/3] Gathering A-stocks from {len(sector_codes)} sectors...", file=sys.stderr)
    phase1 = gather_candidates(sector_codes, top_n_per_sector=30)
    candidates = phase1["candidates"]
    excluded = phase1["excluded"]
    sector_map = phase1["sector_map"]
    print(f"  {len(candidates)} candidates, {len(excluded)} excluded", file=sys.stderr)

    if not candidates:
        elapsed = time.time() - start
        output = build_output([], candidates, excluded, sector_map, elapsed, source)
        print("<!--JSON_OUTPUT-->")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        print("<!--END_JSON_OUTPUT-->")
        return

    # Phase 2
    scored = run_phase2(candidates, enable_wyckoff=args.wyckoff)
    if args.wyckoff:
        print(f"[Wyckoff 漏斗] 过滤后 {len(scored)} 只买点候选",
              file=sys.stderr)

    # Phase 3
    print(f"[Phase 3/3] Building output...", file=sys.stderr)
    elapsed = time.time() - start

    # Filter by min score
    scored = [s for s in scored if s["composite_score"] >= args.min_score]

    output = build_output(scored, candidates, excluded, sector_map, elapsed, source)

    print(f"\nDone in {elapsed:.1f}s. Top {min(args.top, len(scored))} stocks:", file=sys.stderr)
    for i, s in enumerate(scored[:args.top]):
        dims = s["dimensions"]
        wk = s.get("wyckoff")
        wk_str = f" 维={wk['sub_phase']}@{wk['confidence']:.0%}" if wk else ""
        print(f"  {i+1}. {s['name']}({s['code']}) [{s['sector_name']}] "
              f"综合={s['composite_score']:.0f} ★{s['stars']} "
              f"动={dims['momentum']:.0f} 量={dims['volume_price']:.0f} "
              f"资={dims['capital']:.0f} 基={dims['fundamental']:.0f}"
              f"{wk_str}",
              file=sys.stderr)

    print("<!--JSON_OUTPUT-->")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print("<!--END_JSON_OUTPUT-->")


if __name__ == "__main__":
    main()
