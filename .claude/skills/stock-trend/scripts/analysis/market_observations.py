"""Pure contract for separating frozen market observations from model inputs.

This module does not fetch data, mutate a report context, or reconstruct facts
from human-readable component details.  Missing frozen values stay missing.
"""

from __future__ import annotations

import copy
import math
from datetime import date


_OBSERVED_FIELDS = {
    "volume": ("amount_yi",),
    "breadth": (
        "up", "down", "flat_count", "total_count",
        "up_down_sample_count", "suspended_count", "uncovered_count",
    ),
    "zt_emotion": ("count", "streak_count", "max_streak"),
    "capital": ("main_force_yi",),
}

_EVIDENCE_KEYS = {
    "volume": "amount_evidence",
    "breadth": "activity_evidence",
    "zt_emotion": "zt_evidence",
    "capital": "activity_evidence",
}


def _number(value):
    """Return a finite numeric value while rejecting booleans and sentinels."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _iso_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _source(ctx, key):
    evidence = ctx.get(_EVIDENCE_KEYS[key]) or {}
    if not isinstance(evidence, dict):
        evidence = {}
    origin = evidence.get("date_origin") or "unknown"
    # A request date describes the caller's intent.  It cannot establish the
    # provider's trading date, even when the two strings happen to match.
    provider_date = (
        _iso_date(evidence.get("data_date"))
        if origin == "provider"
        else None
    )
    basis_date = _iso_date(ctx.get("data_date"))
    alignment = (
        "matched" if provider_date and basis_date and provider_date == basis_date
        else "mismatched" if provider_date and basis_date
        else "unknown"
    )
    return {
        "status": evidence.get("completeness")
        or ((ctx.get("components") or {}).get(key) or {}).get("data_status")
        or "unknown",
        "provider": evidence.get("provider") or "unknown",
        "provider_date": provider_date,
        "alignment": alignment,
        "date_origin": origin,
        "requested_date": _iso_date(evidence.get("requested_date")),
        "event_time": evidence.get("source_timestamp") or None,
        "fetched_at": evidence.get("fetched_at") or None,
    }


def _raw_inputs(ctx):
    detail_inputs = ctx.get("detail_inputs") or {}
    if not isinstance(detail_inputs, dict):
        detail_inputs = {}
    result = {}
    for key, fields in _OBSERVED_FIELDS.items():
        frozen = detail_inputs.get(key) or {}
        if not isinstance(frozen, dict):
            frozen = {}
        if key == "breadth":
            # Accept old frozen caches but publish one stable v1 field shape.
            frozen = dict(frozen)
            if "flat_count" not in frozen and "flat" in frozen:
                frozen["flat_count"] = frozen.get("flat")
            if "total_count" not in frozen and "total" in frozen:
                frozen["total_count"] = frozen.get("total")
        source = _source(ctx, key)
        group = {
            field: None if source["status"] == "missing"
            else _number(frozen.get(field))
            for field in fields
        }
        group["source"] = source
        result[key] = group
    return result


def _legacy_model_inputs(ctx):
    intraday = bool(ctx.get("intraday"))
    components = ctx.get("components") or {}
    breadth = components.get("breadth") or {}
    zt = ctx.get("zt") or {}
    return {
        "kind": "legacy_intraday" if intraday else "legacy_close",
        "volume": {"amount_yi": _number(ctx.get("amount_yi"))},
        "breadth": {
            "up": _number(breadth.get("up")),
            "down": _number(breadth.get("down")),
        },
        "zt_emotion": {
            "count": _number(zt.get("count")),
            "streak_count": _number(zt.get("streak_count")),
            "max_streak": _number(zt.get("max_streak")),
        },
        # Legacy contexts did not freeze the capital model input separately.
        # Component prose such as “-131.2亿” is intentionally not parsed.
        "capital": {"main_force_yi": None},
    }


def build_market_observations(ctx):
    """Build a detached v1 observation/model-input audit contract."""
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")

    explicit_model_inputs = ctx.get("model_inputs")
    if isinstance(explicit_model_inputs, dict):
        model_inputs = copy.deepcopy(explicit_model_inputs)
    else:
        model_inputs = _legacy_model_inputs(ctx)

    intraday_evidence = ctx.get("intraday_evidence") or {}
    if not isinstance(intraday_evidence, dict):
        intraday_evidence = {}
    regime = ctx.get("regime") or {}
    if not isinstance(regime, dict):
        regime = {}

    return {
        "schema_version": "market-observations/v1",
        "basis_date": _iso_date(ctx.get("data_date")),
        "generated_at": copy.deepcopy(ctx.get("generated_at")),
        "as_of": copy.deepcopy(ctx.get("as_of")),
        "intraday": bool(ctx.get("intraday")),
        "raw_inputs": _raw_inputs(ctx),
        "model_inputs": model_inputs,
        "model_kind": model_inputs.get("kind") or "unknown",
        "raw_components": copy.deepcopy(ctx.get("raw_components"))
        if isinstance(ctx.get("raw_components"), dict) else {},
        "score_context": {
            "anchor_score": _number(intraday_evidence.get("anchor_score")),
            "projected_score": _number(intraday_evidence.get("projected_score")),
            "blend_weight": _number(intraday_evidence.get("blend_weight")),
            "reported_score": _number(regime.get("score")),
            "anchor_date": _iso_date(intraday_evidence.get("anchor_date")),
            "session_fraction": _number(
                intraday_evidence.get("session_elapsed_fraction")
                if intraday_evidence.get("session_elapsed_fraction") is not None
                else intraday_evidence.get("fraction")
            ),
        },
    }


def _display(value, suffix=""):
    return "未保存" if value is None else f"{value}{suffix}"


def component_observation_detail(ctx, key):
    """Return a concise raw/model explanation for one score component."""
    components = ctx.get("components") or {} if isinstance(ctx, dict) else {}
    component = components.get(key) or {}
    original = str(component.get("detail") or "未保存")
    if key == "index_trend":
        return original
    if key not in _OBSERVED_FIELDS:
        return original

    observations = build_market_observations(ctx)
    raw = observations["raw_inputs"][key]
    if key == "volume":
        raw_text = f"原始观测：两市成交额 {_display(raw.get('amount_yi'), '亿')}"
    elif key == "breadth":
        raw_text = (
            "原始观测：上涨 " + _display(raw.get("up"), "家")
            + "，下跌 " + _display(raw.get("down"), "家")
        )
        if raw.get("flat_count") is not None:
            raw_text += "，平盘 " + _display(raw.get("flat_count"), "家")
        else:
            raw_text += "；平盘未保存"
        if raw.get("total_count") is not None:
            raw_text += "，市场总数 " + _display(raw.get("total_count"), "家")
        else:
            raw_text += "，市场总数未保存，涨跌样本不代表市场总数"
    elif key == "zt_emotion":
        raw_text = (
            "原始观测：涨停 " + _display(raw.get("count"), "家")
            + "，连板 " + _display(raw.get("streak_count"), "家")
            + "，最高 " + _display(raw.get("max_streak"), "板")
        )
    else:
        raw_text = "原始观测：全市场主力净流入 " + _display(
            raw.get("main_force_yi"), "亿")

    kind = str((observations.get("model_inputs") or {}).get("kind") or "unknown")
    if kind == "projection":
        label = "模型外推说明"
    elif kind == "anchor":
        label = "昨收锚定说明"
    else:
        label = "模型输入说明"
    return f"{raw_text}；{label}：{original}"
