#!/usr/bin/env python3
"""Pure, bounded U.S.-to-A-share observation-list cross references."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "cross-market-observation/v1"
MAPPING_SCHEMA = "us-a-share-observation-mapping/v1"
SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAPPING = SKILL_ROOT / "data" / "us_a_observation_mapping.json"
RELATION_TYPES = {
    "direct_industry", "supply_chain", "demand", "demand_link",
}
VERIFIED_MEMBERSHIP_QUALITY = {
    "same_day_verified", "historical_verified",
}
VALID_STRUCTURE_STATES = {
    "valid", "confirmed_holding", "structure_valid", "structure_restored",
}


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(value)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except (TypeError, ValueError):
        return None


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _normalize_source(value: Any) -> dict | None:
    source = _mapping(value)
    title = str(source.get("title") or "").strip()
    url = str(source.get("url") or "").strip()
    if not title or not url.startswith(("https://", "http://")):
        return None
    return {"title": title, "url": url}


def _validate_mapping_document(value: Any) -> dict:
    document = _mapping(value)
    entries = _sequence(document.get("mappings"))
    if document.get("schema_version") != MAPPING_SCHEMA:
        raise ValueError("mapping_schema_invalid")
    version = str(document.get("version") or "").strip()
    verified_at = _iso_date(document.get("verified_at"))
    if not version or not verified_at or not entries:
        raise ValueError("mapping_metadata_invalid")
    return {
        "schema_version": MAPPING_SCHEMA,
        "version": version,
        "verified_at": verified_at,
        "mappings": [dict(entry) for entry in entries if isinstance(entry, Mapping)],
    }


def load_mapping(path: Any = None) -> dict:
    """Load and validate the complete versioned static mapping document."""
    raw = json.loads(Path(path or DEFAULT_MAPPING).read_text(encoding="utf-8"))
    return _validate_mapping_document(raw)


def _summary_rows(summary: Mapping) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    groups = _mapping(summary.get("groups"))
    for group_name in ("indices", "sectors", "stocks"):
        for raw in _sequence(groups.get(group_name)):
            row = _mapping(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol and symbol not in rows:
                rows[symbol] = dict(row)
    return rows


def _us_direction(daily: float | None, interval: float | None) -> str:
    value = daily if daily is not None else interval
    if value is None:
        return "不可用"
    if value > 0:
        return "上涨"
    if value < 0:
        return "下跌"
    return "持平"


def _sector_evidence(sector_state: Mapping, names: set[str], basis: str) -> list[dict]:
    if sector_state.get("schema_version") != "sector-persistence-analysis/v1":
        return []
    if _iso_date(sector_state.get("basis_date")) != basis:
        return []
    evidence = []
    for raw in _sequence(sector_state.get("items")):
        item = _mapping(raw)
        if str(item.get("name") or "").strip() not in names:
            continue
        if str(item.get("type") or "") != "industry":
            continue
        if _iso_date(item.get("basis_date")) != basis:
            continue
        if any(str(reason).startswith(("mixed_", "authority_calendar_unverified",
                "duplicate_trade_date", "window_5d_invalid", "window_20d_invalid"))
                for reason in _sequence(item.get("reasons"))):
            continue
        return_5d = _finite(item.get("return_5d"))
        return_20d = _finite(item.get("return_20d"))
        streak = _finite(item.get("consecutive_up_days"))
        if return_5d is None and return_20d is None and streak is None:
            continue
        evidence.append({
            "sector_name": str(item.get("name")),
            "status": "verified",
            "return_5d": return_5d,
            "return_20d": return_20d,
            "consecutive_up_days": int(streak) if streak is not None else None,
            "consecutive_up_days_lower_bound": bool(
                item.get("consecutive_up_days_lower_bound")),
            "basis_date": basis,
        })
    return evidence


def _observation_is_usable(observation: Mapping, basis: str,
                           report_cutoff: datetime | None) -> tuple[bool, str]:
    if observation.get("status") not in {"ready", "degraded"}:
        return False, "observation_unavailable"
    if observation.get("schema") != "yaml-observation-analysis/v2":
        return False, "observation_schema_unverified"
    if _iso_date(observation.get("data_date")) != basis \
            or _iso_date(observation.get("evidence_cutoff_date")) != basis:
        return False, "observation_cutoff_mismatch"
    generated = _aware_datetime(observation.get("generated_at"))
    if not generated:
        return False, "observation_generated_at_invalid"
    if report_cutoff and generated > report_cutoff:
        return False, "observation_after_report_cutoff"
    return True, ""


def _matched_items(observation: Mapping, names: set[str], codes: set[str],
                   basis: str) -> list[dict]:
    matched = []
    for raw in _sequence(observation.get("items")):
        item = _mapping(raw)
        if _iso_date(item.get("data_date")) != basis:
            continue
        code = str(item.get("code") or "")
        if item.get("market") not in {"SH", "SZ", "BJ"} or len(code) != 6 or not code.isdigit():
            continue
        quality = _mapping(item.get("data_quality"))
        if item.get("status") != "ready" or quality.get("eligible") is not True:
            continue
        exact_names = []
        for membership_value in _sequence(item.get("sector_memberships")):
            membership = _mapping(membership_value)
            name = str(membership.get("name") or "").strip()
            if name not in names or membership.get("sector_type") != "industry":
                continue
            if _iso_date(membership.get("membership_data_date")) != basis:
                continue
            if membership.get("membership_quality") not in VERIFIED_MEMBERSHIP_QUALITY:
                continue
            exact_names.append(name)
        explicit_code = code in codes
        if not exact_names and not explicit_code:
            continue
        health = _mapping(_mapping(item.get("wyckoff")).get("event_health"))
        structure = str(health.get("state") or "state_unknown")
        matched.append({
            "code": code,
            "name": str(item.get("name") or item.get("code") or ""),
            "sector_names": sorted(set(exact_names)),
            "selection_basis": "exact_industry" if exact_names else "explicit_code",
            "structure_state": structure,
            "structure_verified": structure in VALID_STRUCTURE_STATES,
            "evidence_date": basis,
        })
    return matched


def _conditions(sectors: list[dict], items: list[dict]) -> list[dict]:
    sector_verified = bool(sectors)
    structures = bool(items) and all(item.get("structure_verified") for item in items)
    return [
        {
            "key": "sector_current_state",
            "label": "板块现有5/20日收益与连续上涨状态",
            "status": "verified" if sector_verified else "unverified",
            "reason": "已读取同依据日冻结板块证据" if sector_verified
            else "缺少同依据日且质量合格的冻结板块证据",
        },
        {
            "key": "observation_structure",
            "label": "观察标的结构仍有效",
            "status": "verified" if structures else "unverified",
            "reason": "冻结观察证据显示结构仍有效" if structures
            else "没有同依据日且质量合格的有效结构证据",
        },
        {
            "key": "activity_recovery",
            "label": "成交活跃度恢复或相对强弱改善",
            "status": "unverified",
            "reason": "缺少历史对比证据，不能从当前截面确认改善或恢复",
        },
    ]


def _base_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "basis_date": None,
        "a_share_anchor_date": None,
        "report_cutoff": None,
        "mapping_schema_version": None,
        "mapping_version": None,
        "mapping_verified_at": None,
        "rows": [],
        "sources": [],
        "reasons": [],
        "reason": "",
        "scope_note": (
            "仅作美股行业与A股行业观察关联；映射不构成个股业务关系证明，"
            "不生成仓位、买入价、胜率或A股涨幅预测。"
        ),
    }


def build_cross_market_observation(summary, sector_state=None,
                                   observation_state=None, *, mappings=None) -> dict:
    """Build render-ready rows strictly from supplied frozen artifacts."""
    state = _base_state()
    summary = _mapping(summary)
    sector_state = _mapping(sector_state)
    observation_state = _mapping(observation_state)
    try:
        document = load_mapping() if mappings is None else _validate_mapping_document(mappings)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        state["reasons"] = ["mapping_unavailable"]
        state["reason"] = "版本化跨市场静态映射缺失或无效"
        return state

    state.update({
        "mapping_schema_version": document["schema_version"],
        "mapping_version": document["version"],
        "mapping_verified_at": document["verified_at"],
    })
    basis = _iso_date(summary.get("basis_date"))
    anchor_date = _iso_date(summary.get("a_share_anchor_date"))
    cutoff_value = summary.get("requested_as_of") or summary.get("as_of")
    cutoff = _aware_datetime(cutoff_value)
    state["basis_date"] = basis
    state["a_share_anchor_date"] = anchor_date
    state["report_cutoff"] = cutoff_value
    if not basis or not cutoff or basis > cutoff.date().isoformat():
        state["reasons"] = ["summary_time_unverified"]
        state["reason"] = "美股摘要的A股依据日或请求截止时间无效"
        return state

    reasons = []
    if _iso_date(sector_state.get("basis_date")) != basis:
        reasons.append("sector_basis_mismatch")
    observation_ok, observation_reason = _observation_is_usable(
        observation_state, basis, cutoff)
    if not observation_ok:
        reasons.append(observation_reason)
    by_symbol = _summary_rows(summary)
    all_sources: list[dict] = []

    for raw in document["mappings"]:
        entry = _mapping(raw)
        symbol = str(entry.get("symbol") or "").strip().upper()
        relation_type = str(entry.get("relation_type") or "").strip()
        if not symbol:
            reasons.append("mapping_symbol_invalid")
            continue
        if relation_type not in RELATION_TYPES:
            reasons.append(f"unknown_relation_type:{symbol}")
            continue
        direction = str(entry.get("direction") or "").strip()
        sector_names = {
            str(name).strip() for name in _sequence(entry.get("sector_names"))
            if str(name).strip()
        }
        observation_codes = {
            str(code).strip() for code in _sequence(entry.get("observation_codes"))
            if str(code).strip()
        }
        if not direction or (not sector_names and not observation_codes):
            reasons.append(f"mapping_entry_invalid:{symbol}")
            continue
        us_row = by_symbol.get(symbol)
        if not us_row:
            reasons.append(f"us_symbol_missing:{symbol}")
            us_row = {"symbol": symbol, "name": symbol, "status": "unavailable"}
        latest_expected = summary.get("latest_completed_session")
        latest_actual = us_row.get("latest_completed_session") \
            or us_row.get("actual_end_session")
        date_valid = bool(_iso_date(latest_expected) and latest_actual == latest_expected
                          and latest_expected <= cutoff.date().isoformat()
                          and us_row.get("status") in {"complete", "partial", "cached"})
        daily = _finite(us_row.get("daily_pct")) if date_valid else None
        interval = _finite(us_row.get("interval_pct")) if date_valid else None
        if symbol in by_symbol and not date_valid:
            reasons.append(f"us_session_mismatch:{symbol}")
        if daily is None and interval is None:
            reasons.append(f"us_performance_unavailable:{symbol}")
        sources = [source for source in (
            _normalize_source(value) for value in _sequence(entry.get("sources"))
        ) if source]
        if not sources:
            reasons.append(f"mapping_sources_invalid:{symbol}")
            continue
        for source in sources:
            if source not in all_sources:
                all_sources.append(source)
        sectors = _sector_evidence(sector_state, sector_names, basis)
        items = _matched_items(
            observation_state, sector_names, observation_codes, basis) \
            if observation_ok else []
        state["rows"].append({
            "symbol": symbol,
            "name": str(us_row.get("name") or symbol),
            "latest_session": us_row.get("latest_completed_session")
            or us_row.get("actual_end_session"),
            "status": "complete" if daily is not None or interval is not None
            else "unavailable",
            "daily_pct": daily,
            "interval_pct": interval,
            "us_direction": _us_direction(daily, interval),
            "a_share_direction": direction,
            "relation_type": relation_type,
            "sector_names": sorted(sector_names),
            "sector_evidence": sectors,
            "items": items,
            "validation_conditions": _conditions(sectors, items),
            "sources": sources,
            "rationale": str(entry.get("rationale") or ""),
            "scope_note": state["scope_note"],
        })

    state["sources"] = all_sources
    state["reasons"] = list(dict.fromkeys(reason for reason in reasons if reason))
    if state["rows"]:
        state["status"] = "degraded" if state["reasons"] else "complete"
        state["reason"] = "；".join(state["reasons"])
    else:
        state["status"] = "degraded" if document["mappings"] else "unavailable"
        if not state["reasons"]:
            state["reasons"] = ["no_mapping_rows"]
        state["reason"] = "；".join(state["reasons"])
    return state
