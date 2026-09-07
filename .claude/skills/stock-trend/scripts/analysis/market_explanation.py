"""Pure explanation contract for the formal market-regime score.

This module deliberately does not fetch data or read files.  It translates
the frozen market-regime context into an auditable explanation while keeping
the existing score, weights, and recommendation gates authoritative.
"""

import math
from datetime import date

from analysis.market_regime import (
    REGIME_COMPONENT_ORDER,
    REGIME_WEIGHTS,
    TREND_INDEX_CODES,
)


_COMPONENT_NAMES = {
    "index_trend": "大盘趋势",
    "volume": "成交额",
    "breadth": "赚钱效应",
    "zt_emotion": "涨停情绪",
    "capital": "资金",
}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_iso_date(value, field):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return value


def _dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def _source_metadata(ctx, component_id):
    component = (ctx.get("components") or {}).get(component_id) or {}
    explicit = component.get("source_kind")
    provider = component.get("provider")
    data_date = component.get("data_date")
    fetched_at = component.get("fetched_at")
    reasons = []

    if component_id == "capital":
        capital_context = ctx.get("capital_context") or {}
        detail = str(component.get("detail") or "")
        if capital_context.get("metric"):
            metric = capital_context["metric"]
            source_kind = capital_context.get("source_kind") or explicit or "unknown"
            provider = capital_context.get("provider") or provider
            data_date = capital_context.get("data_date") or data_date
            fetched_at = capital_context.get("fetched_at") or fetched_at
        elif "主力" in detail or "降级" in detail:
            metric = "market_main_force_net_inflow"
            source_kind = explicit or "alternative"
        elif "北向" in detail:
            metric = "northbound_net_buy"
            source_kind = explicit or "primary"
        else:
            metric = component.get("metric") or "capital_flow"
            source_kind = explicit or "unknown"
    else:
        metric = component.get("metric") or {
            "index_trend": "index_ma20_state",
            "volume": "turnover_vs_20d_avg",
            "breadth": "market_breadth",
            "zt_emotion": "limit_up_emotion",
        }.get(component_id, component_id)
        source_kind = explicit or "unknown"

    if component_id == "index_trend":
        diagnostics = ctx.get("index_data_quality") or {}
        sources = sorted({
            str(item.get("source")) for item in diagnostics.values()
            if isinstance(item, dict) and item.get("source")
        })
        if sources:
            provider = provider or "+".join(sources)
            if source_kind == "unknown":
                source_kind = (
                    "alternative" if any(
                        source not in ("eastmoney", "akshare")
                        for source in sources
                    ) else "primary"
                )
        elif not data_date:
            reasons.append("legacy_context_evidence_unknown")

    if not data_date:
        data_date = ctx.get("data_date") or None
    if not fetched_at:
        reasons.append("source_timestamp_missing")

    return {
        "metric": metric,
        "provider": provider or "unknown",
        "data_date": data_date,
        "fetched_at": fetched_at,
        "source_kind": source_kind,
        "reasons": reasons,
    }


def _freshness(meta, expected_date):
    if not meta.get("fetched_at"):
        return "unknown"
    if meta.get("data_date") == expected_date:
        return "fresh"
    return "stale"


def _index_counts(ctx):
    indices = ctx.get("indices")
    if not isinstance(indices, dict) or not indices:
        return None, None
    expected = len(TREND_INDEX_CODES)
    available = 0
    for code in TREND_INDEX_CODES:
        item = indices.get(code)
        if not isinstance(item, dict):
            continue
        if item.get("ok") is True or _finite(item.get("close")) is not None:
            available += 1
    return expected, available


def _component_explanation(ctx, component_id, expected_date, used_weight):
    component = (ctx.get("components") or {}).get(component_id) or {}
    raw_score = _finite(component.get("score"))
    status = component.get("data_status", "unknown")
    meta = _source_metadata(ctx, component_id)
    expected_count, available_count = _index_counts(ctx) \
        if component_id == "index_trend" else (None, None)

    reasons = list(meta.pop("reasons", []))
    if raw_score is None:
        reasons.append("non_finite_score" if component.get("score") is not None
                       else "score_missing")
    if status == "partial":
        reasons.append("component_partial")
    elif status == "missing":
        reasons.append("component_missing")

    completeness = {
        "good": "complete",
        "partial": "partial",
        "missing": "missing",
    }.get(status, "partial" if raw_score is not None else "missing")
    if component_id == "index_trend" and expected_count is not None:
        if available_count == 0:
            completeness = "missing"
        elif available_count < expected_count:
            completeness = "partial"
            reasons.append(
                f"index_coverage_{available_count}/{expected_count}")

    if raw_score is None or completeness == "missing":
        usage = "unavailable"
    elif completeness == "partial":
        usage = "reference_only"
    else:
        usage = "scorable"

    contribution = 0.0
    if raw_score is not None and used_weight > 0:
        contribution = raw_score * REGIME_WEIGHTS[component_id] / used_weight

    evidence = {
        "completeness": completeness,
        "freshness": _freshness(meta, expected_date),
        "source_kind": meta["source_kind"],
        "metric": meta["metric"],
        "provider": meta["provider"],
        "data_date": meta.get("data_date"),
        "fetched_at": meta.get("fetched_at"),
        "usage": usage,
        "reasons": _dedupe(reasons),
    }
    if expected_count is not None:
        evidence["expected_count"] = expected_count
        evidence["available_count"] = available_count
        evidence["index_codes"] = list(TREND_INDEX_CODES)
        evidence["indices"] = [
            {
                "code": code,
                "above_ma20": item.get("above_ma20") if isinstance(item, dict) else None,
                "ma20_rising": item.get("ma20_rising") if isinstance(item, dict) else None,
                "provider": (
                    ((ctx.get("index_data_quality") or {}).get(code) or {}).get("source")
                    or (item.get("source") if isinstance(item, dict) else None)
                    or "unknown"
                ),
                "data_date": (
                    (item.get("data_date") if isinstance(item, dict) else None)
                    or ((ctx.get("index_data_quality") or {}).get(code) or {}).get("data_date")
                    or None
                ),
                "usage": "scorable" if (
                    isinstance(item, dict)
                    and (item.get("ok") is True
                         or _finite(item.get("close")) is not None)
                ) else "unavailable",
            }
            for code in TREND_INDEX_CODES
            for item in [(ctx.get("indices") or {}).get(code)]
        ]
        if available_count < expected_count:
            evidence["reasons"] = _dedupe(
                list(evidence["reasons"]) + ["index_data_partial"])

    return {
        "id": component_id,
        "name": _COMPONENT_NAMES[component_id],
        "score": round(raw_score, 1) if raw_score is not None else None,
        "weight": REGIME_WEIGHTS[component_id],
        "contribution": round(contribution, 3),
        "detail": component.get("detail", ""),
        "evidence": evidence,
    }


def _blocking_reasons(ctx, expected_date, score, expected_date_valid=True):
    regime = ctx.get("regime") or {}
    reasons = []
    basis_date = ctx.get("data_date") or expected_date
    basis_date_valid = _valid_iso_date(basis_date, "basis_date") is not None
    if not expected_date_valid or not basis_date_valid:
        reasons.append("regime_date_invalid")
    elif basis_date != expected_date:
        reasons.append("regime_stale")
    quality = regime.get("data_quality")
    components = [
        component for component in (ctx.get("components") or {}).values()
        if isinstance(component, dict)
    ]
    component_statuses = [component.get("data_status") for component in components]
    has_non_finite = any(
        component.get("score") is not None
        and _finite(component.get("score")) is None
        for component in components
    )
    if quality == "missing" or "missing" in component_statuses or has_non_finite:
        reasons.append("regime_data_missing")
    if quality == "unknown":
        reasons.append("regime_data_quality_unknown")
    if quality == "partial" or "partial" in component_statuses:
        reasons.append("regime_data_partial")
    if score is None:
        reasons.append("regime_score_missing")
    elif score < 60:
        reasons.append("regime_weak")
    return _dedupe(reasons)


def _intraday_reconciliation(ctx, score):
    data = ctx.get("intraday_evidence") or \
        (ctx.get("regime") or {}).get("intraday_evidence")
    if not isinstance(data, dict):
        return None, "unavailable", ["intraday_anchor_missing"]
    anchor = _finite(data.get("anchor_score"))
    weight = _finite(data.get("blend_weight"))
    projected = _finite(data.get("projected_score"))
    if anchor is None or weight is None or projected is None:
        return None, "unavailable", ["intraday_anchor_missing"]
    formula_value = (1.0 - weight) * anchor + weight * projected
    result = {
        "anchor_score": anchor,
        "blend_weight": weight,
        "projected_score": projected,
        "formula": "(1-blend_weight)*anchor_score+blend_weight*projected_score",
        "formula_value": round(formula_value, 1),
        "anchor_date": data.get("anchor_date"),
    }
    if abs(formula_value - (score if score is not None else formula_value)) <= 0.1:
        return result, "matched", []
    return result, "mismatch", ["intraday_blend_mismatch"]


def build_market_explanation(ctx, expected_date):
    """Build a detached, deterministic explanation from a frozen context."""
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")
    expected_date_valid = _valid_iso_date(expected_date, "expected_date") is not None
    if not expected_date_valid:
        expected_date = ctx.get("data_date") or ""

    regime = ctx.get("regime") or {}
    official_score = _finite(regime.get("score"))
    components = []
    used_weight = 0.0
    for component_id in REGIME_COMPONENT_ORDER:
        component = (ctx.get("components") or {}).get(component_id) or {}
        if _finite(component.get("score")) is not None:
            used_weight += REGIME_WEIGHTS[component_id]
    if used_weight <= 0:
        used_weight = 0.0

    for component_id in REGIME_COMPONENT_ORDER:
        components.append(_component_explanation(
            ctx, component_id, expected_date, used_weight))

    raw_total = sum(
        (item["score"] or 0.0) * item["weight"]
        for item in components
        if item["score"] is not None
    )
    computed_score = raw_total / used_weight if used_weight else None
    score = official_score if official_score is not None else (
        round(computed_score, 1) if computed_score is not None else None)

    mode = "intraday" if ctx.get("intraday") or regime.get("intraday") else "close"
    quality_notes = []
    if any(item["evidence"]["source_kind"] == "alternative"
           for item in components if item["id"] == "capital"):
        quality_notes.append("capital_alternative_metric")
    if any("source_timestamp_missing" in item["evidence"]["reasons"]
           for item in components):
        quality_notes.append("source_timestamp_unknown")
    if not ctx.get("indices") or not ctx.get("index_data_quality"):
        quality_notes.extend([
            "legacy_context_evidence_unknown", "legacy_evidence_unknown",
        ])

    intraday = None
    if mode == "intraday":
        intraday, reconciliation, intraday_notes = _intraday_reconciliation(
            ctx, score)
        quality_notes.extend(intraday_notes)
    else:
        has_invalid_component = any(
            item["score"] is None and (
                (ctx.get("components") or {}).get(item["id"], {}).get("score")
                is not None
            )
            for item in components
        )
        basis_date = ctx.get("data_date")
        has_date_mismatch = bool(
            not _valid_iso_date(basis_date, "basis_date")
            or basis_date != expected_date)
        reconciliation = (
            "unavailable" if not expected_date_valid or has_invalid_component
            or has_date_mismatch
            else
            "matched" if score is not None and computed_score is not None
            and abs(score - computed_score) <= 0.1 else "unavailable"
            if score is None or computed_score is None else "mismatch"
        )

    result = {
        "schema_version": "market-explanation/v1",
        "basis_date": ctx.get("data_date") or expected_date or None,
        "basis_generated_at": ctx.get("generated_at"),
        "mode": mode,
        "score": round(score, 1) if score is not None else None,
        "components": components,
        "normalization_denominator": round(used_weight, 3),
        "raw_weighted_total": round(raw_total, 3),
        "blocking_reasons": _blocking_reasons(
            ctx, expected_date, score, expected_date_valid),
        "quality_notes": _dedupe(quality_notes),
        "reconciliation": reconciliation,
    }
    if intraday is not None:
        result["intraday"] = intraday
        result["intraday_mix"] = {
            "anchor_score": intraday.get("anchor_score"),
            "blend_weight": intraday.get("blend_weight"),
            "projected_score": intraday.get("projected_score"),
            "session_elapsed_fraction": (
                (ctx.get("intraday_evidence") or {}).get(
                    "session_elapsed_fraction",
                    (ctx.get("intraday_evidence") or {}).get("fraction"),
                )
            ),
            "blended_score": intraday.get("formula_value"),
        }
    return result
