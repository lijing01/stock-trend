"""Deterministic, single-dimension diagnostics for candidate research.

Diagnostics describe correlations in frozen research samples.  They do not
claim causality and intentionally do not search high-dimensional parameter
combinations.
"""
import copy
import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.evolution_contract import PRIMARY_WINDOW, build_evaluation_contract
from core.evolution_storage import LEGACY_RESEARCH_ROOT, input_manifest, load_research_snapshot, storage_root
from core.recommendation_snapshot import canonical_json, content_sha256, load_official_snapshot
from core.research_events import assign_research_events, summarize_daily_alpha


SCHEMA_VERSION = "recommendation-diagnostics/v1"
DEFAULT_RESEARCH_ROOT = storage_root("research")
DEFAULT_ROOT = storage_root("diagnostics")
P0_AUDIT_SCHEMA_VERSION = "recommendation-p0-audit/v1"
P0_WINDOWS = (5, 10, 20)
DEFAULT_MARKET_HISTORY = Path(CACHE_DIR) / "market_regime_history.json"
DEFAULT_RECOMMENDATION_ROOT = Path(CACHE_DIR) / "recommendation_history"
DEFAULT_P0_AUDIT_ROOT = storage_root("diagnostics") / "p0"
MIN_GROUP_EVENTS = 30


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _day(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        except ValueError:
            return None
    try:
        parsed = date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if len(text) >= 10 else None


def _cutoff(value):
    if value is None or value == "":
        return None
    parsed = _day(value)
    if parsed is None:
        raise ValueError("invalid_evaluation_as_of")
    return parsed


def _validate_payload_contract(contract):
    """Validate a persisted v2 contract, not only its directory name."""
    if not isinstance(contract, dict):
        raise ValueError("evaluation_contract_missing")
    try:
        expected = build_evaluation_contract(
            contract.get("windows") or [], contract.get("trade_cost_model") or {},
            primary_window=contract.get("primary_window", PRIMARY_WINDOW),
            primary_benchmark=contract.get("primary_benchmark", "hs300"),
            adjustment=contract.get("adjustment", "qfq"),
            population_kind=contract.get("population_kind", ""),
            evaluation_version=contract.get("evaluation_version", ""),
            population_identity=contract.get("population_identity") or {},
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("evaluation_contract_invalid") from exc
    if expected != contract or not contract.get("contract_id"):
        raise ValueError("evaluation_contract_invalid")
    return contract


def _median(values):
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _band(value, cuts, labels, unknown="unknown"):
    value = _number(value)
    if value is None:
        return unknown
    for cutoff, label in zip(cuts, labels):
        if value < cutoff:
            return label
    return labels[-1]


def diagnostic_dimensions(record):
    """Return P2's fixed one-dimensional descriptors for one scan record."""
    candidate = record.get("candidate") or {}
    score = record.get("scores") or {}
    regime = record.get("market_regime") or {}
    persistence = record.get("sector_persistence") or {}
    quality = score.get("quality_adjusted_score")
    if quality is None:
        quality = candidate.get("quality_adjusted_score")
    news = candidate.get("news_analysis") or {}
    return {
        "selection_status": record.get("final_status") or "unknown",
        "buy_point_level": score.get("buy_point_level") or candidate.get("buy_point_level") or "unknown",
        "market_regime_band": _band(
            regime.get("score", regime.get("market_score")), (60, 80), ("weak_<60", "neutral_60_79", "strong_80_plus")),
        "sector_persistence": persistence.get("sector_persistence_status") or (
            "actionable" if persistence.get("sector_actionable") else "not_actionable"),
        "quality_score_band": _band(quality, (60, 80), ("low_<60", "medium_60_79", "high_80_plus")),
        "news_score_band": _band(
            news.get("score"), (-0.5, 0.01),
            ("negative", "neutral", "positive"), unknown="unavailable"),
        "news_risk_level": news.get("risk_level") or "unavailable",
        "news_shadow_selection": (
            "selected" if news.get("shadow_selected") is True else
            "not_selected" if news.get("shadow_selected") is False else
            "unavailable"),
    }


def _outcome_index(items, primary_window, evaluation_as_of=None):
    index = {}
    label = str(primary_window)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        recommendation_date = _day(item.get("recommendation_date"))
        if not recommendation_date:
            continue
        if evaluation_as_of and recommendation_date > evaluation_as_of:
            continue
        market = str(item.get("market") or item.get("exchange") or "default")
        key = (recommendation_date, market, str(item.get("code") or ""))
        if not all(key):
            continue
        window = (item.get("windows") or {}).get(label)
        if isinstance(window, dict):
            value = copy.deepcopy(window)
            if item.get("conflicts") or item.get("point_in_time_status") == "evaluation_conflict":
                value = {"status": "evaluation_conflict",
                         "reason": "conflicting_evaluation_content",
                         "conflicts": copy.deepcopy(item.get("conflicts"))}
            elif item.get("point_in_time_status") == "legacy_unverified":
                value = {"status": "legacy_unverified",
                         "reason": "legacy_evaluation_identity_missing"}
            label_end = _day(value.get("exit_date") or value.get("mark_date"))
            if evaluation_as_of and label_end and label_end > evaluation_as_of:
                value = {"status": "pending", "reason": "after_evaluation_cutoff",
                         "required_date": label_end}
            index[key] = {
                "window": value,
                "record_id": item.get("record_id"),
                "research_snapshot_sha256": item.get("research_snapshot_sha256"),
                "population_kind": item.get("population_kind"),
                "market_sessions": item.get("market_sessions"),
            }
    return index


def _summarize_group(rows):
    complete = [row for row in rows if row["status"] == "complete"]
    values = [row["hs300_alpha"] for row in complete
              if row.get("hs300_alpha") is not None]
    returns = [row["signal_return"] for row in complete if row.get("signal_return") is not None]
    maes = [row["mae"] for row in complete if row.get("mae") is not None]
    event_records = [row for row in complete
                     if row.get("event_id") and row.get("event_anchor")]
    event_count = len({row["event_id"] for row in event_records})
    dates = sorted({row["recommendation_date"] for row in complete})
    statuses = defaultdict(int)
    for row in rows:
        statuses[row["status"]] += 1
    daily_alpha = summarize_daily_alpha([
        {**row, "evaluation_status": row.get("status")}
        for row in rows
    ])
    valid_alpha_rows = [row for row in event_records
                        if row.get("hs300_alpha") is not None]
    valid_alpha_events = len({row["event_id"] for row in valid_alpha_rows})
    valid_alpha_dates = sorted({row["recommendation_date"] for row in valid_alpha_rows})
    return {
        "records": len(rows), "mature_records": len(complete),
        "mature_events": event_count,
        "valid_alpha_events": valid_alpha_events,
        # Proposal gates use dates with a valid, deduplicated primary alpha;
        # mature_dates remains the broader complete-outcome count for audit.
        "valid_alpha_dates": len(valid_alpha_dates),
        "alpha_mature_dates": len(valid_alpha_dates),
        "mature_dates": len(dates), "time_distribution": dates,
        "alpha_time_distribution": valid_alpha_dates,
        "missing_outcome": statuses["missing_outcome"], "pending": statuses["pending"],
        "data_errors": statuses["data_error"], "unexecutable": statuses["unexecutable"],
        "legacy_unverified": statuses["legacy_unverified"],
        "evaluation_conflicts": statuses["evaluation_conflict"],
        "mean_hs300_alpha": daily_alpha["mean_alpha"],
        "daily_hs300_alpha": daily_alpha["daily"],
        "alpha_missing_records": daily_alpha["missing_records"],
        "median_hs300_alpha": _median(values),
        "mean_signal_return": sum(returns) / len(returns) if returns else None,
        "mean_mae": sum(maes) / len(maes) if maes else None,
        "proposal_eligible": (
            event_count >= MIN_GROUP_EVENTS
            and valid_alpha_events >= MIN_GROUP_EVENTS
        ),
    }


def build_diagnostics(research_snapshots, candidate_signal_items,
                      primary_window=PRIMARY_WINDOW, evaluation_as_of=None,
                      as_of=None):
    """Join immutable scan records to P0 signal outcomes and group once per dimension."""
    if evaluation_as_of is not None and as_of is not None and str(evaluation_as_of) != str(as_of):
        raise ValueError("conflicting_evaluation_cutoffs")
    if evaluation_as_of is None:
        evaluation_as_of = as_of
    cutoff = _cutoff(evaluation_as_of)
    outcome_by_key = _outcome_index(candidate_signal_items, primary_window, cutoff)
    rows, skipped = [], 0
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        snapshot_day = _day(content.get("recommendation_date"))
        if not snapshot_day:
            continue
        if cutoff and snapshot_day > cutoff:
            continue
        # Conflict and intraday samples remain auditable but cannot be research input.
        official = content.get("official_snapshot") or {}
        if content.get("snapshot_type") != "formal" or official.get("link_status") != "linked":
            skipped += len(content.get("records") or [])
            continue
        for record in content.get("records") or []:
            if not isinstance(record, dict) or not record.get("code"):
                continue
            recommendation_date = _day(record.get("basis_date") or snapshot_day)
            if not recommendation_date or (cutoff and recommendation_date > cutoff):
                continue
            market = str(record.get("market") or record.get("exchange") or "default")
            code = str(record["code"])
            key = (recommendation_date, market, code)
            outcome_entry = outcome_by_key.get(key)
            if outcome_entry is None:
                # Older v1 signal items omitted market.  The compatibility
                # lookup is safe only when there is one unambiguous code/day.
                fallback = [value for candidate_key, value in outcome_by_key.items()
                            if candidate_key[0] == recommendation_date
                            and candidate_key[2] == code]
            record_id = str(record.get("record_id") or (
                f"{recommendation_date}:{market}:{code}:{primary_window}"))
            expected_hash = snapshot.get("content_sha256")
            # v2 research snapshots bind an outcome to the exact frozen
            # record and snapshot hash.  Bare fixtures/legacy snapshots lack
            # those identities and remain readable as audit-only evidence.
            requires_identity = bool(expected_hash)
            def identity_matches(candidate_entry):
                if not candidate_entry:
                    return False
                if not requires_identity:
                    return True
                return (
                    candidate_entry.get("record_id") == record.get("record_id")
                    and candidate_entry.get("research_snapshot_sha256") == expected_hash
                    and candidate_entry.get("population_kind") == "frozen_investable_research_population"
                )
            if not identity_matches(outcome_entry):
                fallback = [value for value in fallback if identity_matches(value)] if outcome_entry is None else []
                outcome_entry = fallback[0] if len(fallback) == 1 else None
            window = (outcome_entry or {}).get("window") or {}
            status = window.get("status") if outcome_entry else "missing_outcome"
            rows.append({
                "record_id": record_id, "market": market,
                "recommendation_date": recommendation_date, "code": code, "status": status,
                "signal_return": _number(window.get("signal_return")),
                "hs300_alpha": _number(window.get("hs300_alpha")),
                "mae": _number(window.get("mae")),
                "entry_date": _day(window.get("entry_date")),
                "exit_date": _day(window.get("exit_date") or window.get("mark_date")),
                "market_sessions": (outcome_entry or {}).get("market_sessions"),
                "dimensions": diagnostic_dimensions({**record, "market_regime": content.get("market_regime") or {}}),
            })
    event_records = [row for row in rows
                     if row.get("entry_date") and row.get("exit_date")]
    assigned = assign_research_events(
        [{key: row.get(key) for key in (
            "record_id", "market", "code", "recommendation_date",
            "entry_date", "exit_date", "market_sessions")}
         for row in event_records], primary_window)
    event_by_record = assigned["record_to_event"]
    event_anchors = {event["record_id"] for event in assigned["events"]}
    for row in rows:
        row["event_id"] = event_by_record.get(row["record_id"])
        row["event_anchor"] = row["record_id"] in event_anchors
    excluded_rows = [row for row in rows if row.get("status") == "excluded"]
    metric_rows = [row for row in rows if row.get("status") != "excluded"]
    grouped = {name: defaultdict(list) for name in (
        "selection_status", "buy_point_level", "market_regime_band",
        "sector_persistence", "quality_score_band", "news_score_band",
        "news_risk_level", "news_shadow_selection")}
    for row in metric_rows:
        for name, value in row["dimensions"].items():
            grouped[name][str(value)].append(row)
    dimensions = {}
    evidence_index = {}
    for name, values in grouped.items():
        dimensions[name] = {}
        for value, group_rows in sorted(values.items()):
            group = _summarize_group(group_rows)
            evidence_id = f"{name}:{value}"
            group["evidence_id"] = evidence_id
            dimensions[name][value] = group
            evidence_index[evidence_id] = {
                "mature_events": group["mature_events"],
                "proposal_eligible": group["proposal_eligible"],
            }
    overall = _summarize_group(metric_rows)
    overall["excluded_records"] = len(excluded_rows)
    content = {
        "schema_version": SCHEMA_VERSION, "primary_window": primary_window,
        "correlation_notice": "分组归因仅描述相关性，不表示因果关系或策略有效性。",
        "overall": overall, "dimensions": dimensions, "evidence_index": evidence_index,
        "input": {"research_snapshots": len(research_snapshots or []),
                  "candidate_signal_items": len(candidate_signal_items or []),
                  "excluded_records": len(excluded_rows),
                  "skipped_ineligible_records": skipped,
                  "evaluation_as_of": cutoff},
    }
    content["input_manifest"] = input_manifest(
        research_run_ids=sorted(str((item or {}).get("run_id") or "") for item in research_snapshots or []),
        research_content_sha256=sorted(str((item or {}).get("content_sha256") or "") for item in research_snapshots or []),
        evaluation_window=primary_window,
        evaluation_as_of=cutoff,
        candidate_signal_items=candidate_signal_items or [],
    )
    return {"schema_version": SCHEMA_VERSION, "diagnostic_id": content_sha256(content)[:16],
            "content_sha256": content_sha256(content), "content": content}


def load_primary_research_snapshots(root=DEFAULT_RESEARCH_ROOT, as_of=None):
    root = Path(root)
    cutoff = _cutoff(as_of)
    roots = [root]
    # P1 originally persisted under candidate_research_history.  Retain it as
    # a read-only source while all new writes use evolution/research.
    if root == DEFAULT_RESEARCH_ROOT and LEGACY_RESEARCH_ROOT != root:
        roots.append(LEGACY_RESEARCH_ROOT)
    snapshots = []
    seen_dates = set()
    for candidate_root in roots:
        for index in sorted(candidate_root.glob("*/formal/primary.json")):
            day = index.parent.parent.name
            if cutoff and day > cutoff:
                continue
            if day in seen_dates:
                continue
            run_id = index.read_text(encoding="utf-8").strip()
            path = index.parent / (run_id + ".json")
            try:
                snapshot = load_research_snapshot(path)
                snapshot_day = _day((snapshot.get("content") or {}).get("recommendation_date"))
                if cutoff and snapshot_day and snapshot_day > cutoff:
                    continue
                snapshots.append(snapshot)
                seen_dates.add(day)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    snapshots.sort(key=lambda item: str((item.get("content") or {}).get("recommendation_date") or ""))
    return snapshots


def load_candidate_signal_items(root, contract_id=None, as_of=None):
    """Load P0 candidate outcomes from exactly one evaluation contract.

    Contract identities must never be mixed because their adjustment and
    evaluation rules can differ.  Callers choose one explicit contract when a
    root contains more than one.
    """
    root = Path(root)
    if contract_id:
        roots = [root / contract_id]
    else:
        if not root.exists():
            roots = []
        elif any(root.glob("*.json")) or (root / "v2").is_dir():
            roots = [root]
        else:
            roots = [path for path in root.iterdir() if path.is_dir()]
        if not roots:
            return []
        if len(roots) != 1:
            raise ValueError("evaluation_contract_id_required")
    cutoff = _cutoff(as_of)
    selected_by_day = {}
    payloads = []
    seen_payload_contract_id = None
    paths = roots[0].rglob("*.json")
    for path in sorted(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload_version = payload.get("evaluation_version")
        payload_contract = payload.get("evaluation_contract")
        payload_contract_id = None
        if payload_version == "v2":
            payload_contract = _validate_payload_contract(payload_contract)
            payload_contract_id = payload_contract["contract_id"]
            if contract_id and payload_contract_id != str(contract_id):
                raise ValueError("evaluation_contract_mismatch")
            if seen_payload_contract_id is not None \
                    and payload_contract_id != seen_payload_contract_id:
                raise ValueError("evaluation_contract_mixed")
            seen_payload_contract_id = payload_contract_id
        payload_as_of = _day(payload.get("evaluation_as_of"))
        # Versioned v2 sidecars are partitioned by evaluation_as_of.  The
        # directory fallback keeps hand-copied/audit fixtures safe when the
        # payload omitted the field, while still failing closed for a future
        # partition instead of treating it as data available at the cutoff.
        if payload_version == "v2" and payload_as_of is None:
            payload_as_of = _day(path.parent.name)
        if cutoff and payload_version == "v2":
            if payload_as_of is None or payload_as_of > cutoff:
                continue
        raw_items = payload.get("candidate_signal_items") or []
        payload_day = _day(payload.get("recommendation_date"))
        item_days = {
            _day(item.get("recommendation_date"))
            for item in raw_items if isinstance(item, dict)
        }
        item_days.discard(None)
        if payload_day:
            item_days.add(payload_day)
        if not item_days:
            continue
        rank = (1 if payload_version == "v2" else 0,
                payload_as_of or "", str(path))
        payloads.append((path, payload, rank))
        for item_day in item_days:
            previous = selected_by_day.get(item_day)
            if previous is None or rank > previous[0]:
                selected_by_day[item_day] = (rank, path)

    items = []
    for path, payload, _rank in payloads:
        payload_version = payload.get("evaluation_version")
        payload_contract = payload.get("evaluation_contract")
        payload_contract_id = (payload_contract or {}).get("contract_id") \
            if isinstance(payload_contract, dict) else None
        payload_day = _day(payload.get("recommendation_date"))
        for item in payload.get("candidate_signal_items") or []:
            item_day = _day(item.get("recommendation_date")) if isinstance(item, dict) else None
            item_day = item_day or payload_day
            if item_day is None or selected_by_day.get(item_day, (None, None))[1] != path:
                continue
            if cutoff and item_day and item_day > cutoff:
                continue
            item_as_of = _day(item.get("evaluation_as_of")) if isinstance(item, dict) else None
            if cutoff and item_as_of and item_as_of > cutoff:
                continue
            if isinstance(item, dict) and item.get("conflicts"):
                # Keep the old value for audit, but prevent a conflicting
                # retry from being promoted into a mature research sample.
                item = copy.deepcopy(item)
                item["point_in_time_status"] = "evaluation_conflict"
            elif isinstance(item, dict) and payload_version != "v2":
                # Legacy sidecars remain readable for audit, but they cannot
                # be promoted to point-in-time evidence without a v2 identity.
                item = copy.deepcopy(item)
                item["point_in_time_status"] = "legacy_unverified"
            if payload_version == "v2" and isinstance(item, dict):
                item = copy.deepcopy(item)
                item_contract = item.get("evaluation_contract")
                item_contract_id = item.get("contract_id")
                if isinstance(item_contract, dict) and item_contract.get("contract_id") \
                        and item_contract.get("contract_id") != payload_contract_id:
                    raise ValueError("evaluation_contract_mismatch")
                if item_contract_id and item_contract_id != payload_contract_id:
                    raise ValueError("evaluation_contract_mismatch")
                # Candidate-signal records historically carried only the
                # window outcomes.  Attach the immutable payload contract so
                # replay cannot silently mix contracts after loading.
                item["contract_id"] = payload_contract_id
                item["evaluation_contract"] = copy.deepcopy(payload_contract)
            items.append(item)
    return items


def p0_trading_days(start_date, end_date, trading_days=None):
    """Return the immutable date axis used by the P0 audit.

    A supplied calendar is preferred.  Without one, weekdays are used only as
    a conservative audit axis; missing exchange holidays remain visible as
    missing facts rather than being silently removed or backfilled.
    """
    start = _cutoff(start_date)
    end = _cutoff(end_date)
    if not start or not end or start > end:
        raise ValueError("invalid_p0_date_range")
    if trading_days is not None:
        values = sorted({_day(value) for value in trading_days if _day(value)})
        return [value for value in values if start <= value <= end]
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    values = []
    while cursor <= last:
        if cursor.weekday() < 5:
            values.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return values


def _p0_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def load_market_regime_history(path=DEFAULT_MARKET_HISTORY):
    """Read market history without repairing or rewriting any entry."""
    payload = _p0_json(path)
    if not isinstance(payload, dict):
        return {}
    return {str(day): value for day, value in payload.items()
            if isinstance(value, dict)}


def load_official_recommendation_history(root=DEFAULT_RECOMMENDATION_ROOT,
                                         start_date=None, end_date=None):
    """Load formal recommendation snapshots keyed by date, read-only."""
    root = Path(root)
    days = p0_trading_days(start_date, end_date) if start_date and end_date else None
    if days is None:
        days = sorted(path.stem for path in root.glob("????-??-??.json"))
    result = {}
    for day in days:
        path = root / f"{day}.json"
        if not path.exists():
            continue
        try:
            snapshot = load_official_snapshot(path)
            content = snapshot.get("content") or {}
            result[day] = {
                "status": "loaded",
                "content": content,
                "content_sha256": snapshot.get("content_sha256"),
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result[day] = {"status": "invalid", "reason": type(exc).__name__}
    return result


def load_p0_research_inventory(root=DEFAULT_RESEARCH_ROOT,
                               start_date=None, end_date=None):
    """Index formal/provisional research snapshots while preserving gaps."""
    root = Path(root)
    days = p0_trading_days(start_date, end_date) if start_date and end_date else []
    roots = [root]
    if root == DEFAULT_RESEARCH_ROOT and LEGACY_RESEARCH_ROOT != root:
        roots.append(LEGACY_RESEARCH_ROOT)
    inventory = {}
    for day in days:
        formal_path = None
        for candidate_root in roots:
            path = candidate_root / day / "formal" / "primary.json"
            if path.exists():
                formal_path = path
                break
        if formal_path is not None:
            try:
                run_id = formal_path.read_text(encoding="utf-8").strip()
                snapshot = load_research_snapshot(formal_path.parent / (run_id + ".json"))
                content = snapshot.get("content") or {}
                official = content.get("official_snapshot") or {}
                inventory[day] = {
                    "status": "formal",
                    "snapshot_type": content.get("snapshot_type") or "formal",
                    "link_status": official.get("link_status") or "missing",
                    "content": content,
                    "content_sha256": snapshot.get("content_sha256"),
                }
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                inventory[day] = {"status": "invalid", "link_status": "invalid"}
                continue
        provisional = []
        for candidate_root in roots:
            provisional.extend(sorted((candidate_root / day / "provisional").glob("*.json")))
        if provisional:
            loaded = None
            for path in provisional:
                try:
                    loaded = load_research_snapshot(path)
                    break
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            if loaded is not None:
                content = loaded.get("content") or {}
                inventory[day] = {
                    "status": "provisional",
                    "snapshot_type": content.get("snapshot_type") or "provisional",
                    "link_status": "missing",
                    "content": content,
                    "content_sha256": loaded.get("content_sha256"),
                }
                continue
        inventory[day] = {"status": "missing", "link_status": "missing"}
    return inventory


def _p0_content(value):
    return value.get("content") if isinstance(value, dict) and isinstance(value.get("content"), dict) else value


def _p0_bucket_counts(content):
    buckets = content.get("buckets") or {}
    counts = {}
    for name, values in buckets.items():
        counts[str(name)] = len(values) if isinstance(values, list) else 0
    for name in ("actionable", "waiting_trigger", "next_day_confirmation", "observation",
                 "unenriched_observation", "data_rejected"):
        counts.setdefault(name, 0)
    return counts


def _p0_finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _p0_alpha_reason(window):
    status = str((window or {}).get("status") or "missing")
    value = (window or {}).get("hs300_alpha")
    if status == "complete" and _p0_finite(value):
        return None
    if status == "complete":
        return "hs300_alpha_missing"
    if status == "pending":
        return "pending"
    if status == "excluded":
        return "excluded_research_population"
    if status == "data_error":
        reason = str((window or {}).get("reason") or "unknown")
        if reason == "historical_data_missing":
            return "historical_data_missing"
        if reason == "background_stage_timeout":
            return "background_stage_timeout"
        if reason.startswith("获取板块"):
            return "sector_data_error"
        return "data_error_other"
    return "window_missing"


def _p0_event_inputs(items, window):
    rows = []
    for number, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        outcome = (item.get("windows") or {}).get(str(window)) or {}
        if outcome.get("status") == "excluded":
            continue
        if not outcome.get("entry_date") or not outcome.get("exit_date"):
            continue
        day = _day(item.get("recommendation_date"))
        if not day:
            continue
        rows.append({
            "record_id": _p0_record_id(item, window, number),
            "market": str(item.get("market") or item.get("exchange") or "default"),
            "code": str(item.get("code") or ""),
            "recommendation_date": day,
            "entry_date": outcome.get("entry_date"),
            "exit_date": outcome.get("exit_date") or outcome.get("mark_date"),
            "market_sessions": item.get("market_sessions"),
        })
    return rows


def _p0_record_id(item, window, number=0):
    day = _day(item.get("recommendation_date")) or "unknown"
    return str(item.get("record_id") or
               f"{day}:{item.get('market') or 'default'}:{item.get('code') or number}:{window}")


def _p0_event_context(items, window):
    event_inputs = _p0_event_inputs(items, window)
    assigned = assign_research_events(event_inputs, window)
    event_by_record = assigned["record_to_event"]
    anchors = {event["record_id"] for event in assigned["events"]}
    anchor_dates = defaultdict(int)
    for event in assigned["events"]:
        anchor_dates[event["recommendation_date"]] += 1
    valid_alpha = set()
    for number, item in enumerate(items):
        outcome = (item.get("windows") or {}).get(str(window)) or {}
        record_id = _p0_record_id(item, window, number)
        if (record_id in anchors and outcome.get("status") == "complete"
                and _p0_finite(outcome.get("hs300_alpha"))):
            valid_alpha.add(event_by_record.get(record_id))
    return {
        "event_by_record": event_by_record,
        "anchor_ids": anchors,
        "anchor_dates": anchor_dates,
        "valid_alpha_ids": {value for value in valid_alpha if value},
        "invalid_event_records": len(assigned["invalid"]),
        "event_record_count": len(event_inputs),
    }


def _p0_window_audit(items, window, event_context=None):
    statuses = defaultdict(int)
    missing_reasons = defaultdict(int)
    for item in items:
        outcome = (item.get("windows") or {}).get(str(window)) or {}
        status = str(outcome.get("status") or "missing")
        statuses[status] += 1
        reason = _p0_alpha_reason(outcome)
        if reason:
            missing_reasons[reason] += 1
    context = event_context or _p0_event_context(items, window)
    event_by_record = context["event_by_record"]
    event_anchors = context["anchor_ids"]
    anchor_dates = defaultdict(int)
    for number, item in enumerate(items):
        record_id = _p0_record_id(item, window, number)
        event_id = event_by_record.get(record_id)
        if record_id in event_anchors:
            anchor_dates[_day(item.get("recommendation_date"))] += 1
    local_event_ids = set()
    valid_alpha_events = set()
    for number, item in enumerate(items):
        outcome = (item.get("windows") or {}).get(str(window)) or {}
        record_id = _p0_record_id(item, window, number)
        event_id = event_by_record.get(record_id)
        if record_id in event_anchors and event_id:
            local_event_ids.add(event_id)
            if outcome.get("status") == "complete" and _p0_finite(outcome.get("hs300_alpha")):
                valid_alpha_events.add(event_id)
    return {
        "records": len(items),
        "status_counts": dict(sorted(statuses.items())),
        "pending": statuses["pending"],
        "data_error": statuses["data_error"],
        "complete": statuses["complete"],
        "excluded": statuses["excluded"],
        "missing": statuses["missing"],
        "hs300_alpha_missing_reasons": dict(sorted(missing_reasons.items())),
        "deduplicated_events": len(local_event_ids),
        "deduplicated_event_dates": dict(sorted(anchor_dates.items())),
        "valid_alpha_events": len(valid_alpha_events),
        "invalid_event_records": context["invalid_event_records"],
        "event_record_count": context["event_record_count"],
    }


def build_p0_audit(trading_days, market_history, official_history,
                   research_inventory, candidate_signal_items,
                   windows=P0_WINDOWS):
    """Build a deterministic P0 gap audit from frozen, read-only inputs."""
    days = sorted({_day(day) for day in trading_days if _day(day)})
    items_by_day = defaultdict(list)
    for item in candidate_signal_items or []:
        day = _day(item.get("recommendation_date")) if isinstance(item, dict) else None
        if day in days:
            items_by_day[day].append(item)
    all_items = [item for day in days for item in items_by_day[day]]
    event_contexts = {str(window): _p0_event_context(all_items, window)
                      for window in windows}
    rows = []
    link_statuses = defaultdict(int)
    window_totals = {str(window): defaultdict(int) for window in windows}
    alpha_totals = {str(window): defaultdict(int) for window in windows}
    event_totals = {str(window): 0 for window in windows}
    valid_event_totals = {str(window): 0 for window in windows}
    formal_total = observation_total = 0
    frozen_known_total = 0
    frozen_unknown_dates = []
    market_partial_dates = []
    for day in days:
        market = market_history.get(day) if isinstance(market_history, dict) else None
        market = market if isinstance(market, dict) else {}
        official_entry = official_history.get(day) if isinstance(official_history, dict) else None
        official = _p0_content(official_entry or {}) if official_entry else None
        official_status = (official_entry or {}).get("status", "missing") if official_entry else "missing"
        bucket_counts = _p0_bucket_counts(official or {}) if official else _p0_bucket_counts({})
        formal_count = sum(bucket_counts.get(name, 0) for name in
                           ("actionable", "waiting_trigger", "next_day_confirmation"))
        observation_count = sum(bucket_counts.get(name, 0) for name in
                                ("observation", "unenriched_observation"))
        formal_total += formal_count
        observation_total += observation_count
        research = research_inventory.get(day) if isinstance(research_inventory, dict) else None
        research = research or {"status": "missing", "link_status": "missing"}
        link_status = str(research.get("link_status") or research.get("status") or "missing")
        link_statuses[link_status] += 1
        research_content = _p0_content(research) or {}
        market_context = research_content.get("market_regime") or {}
        partial_components = sorted(set((market.get("partial_components") or []))
                                     | set((market_context.get("partial_components") or [])))
        missing_components = sorted(set((market.get("missing_components") or []))
                                    | set((market_context.get("missing_components") or [])))
        market_quality = market_context.get("data_quality")
        if partial_components or market_quality == "partial":
            market_partial_dates.append(day)
        scope = research_content.get("selection_scope")
        if (research.get("status") == "formal" and link_status == "linked"
                and isinstance(scope, dict) and isinstance(scope.get("codes"), list)):
            frozen_count = len(scope["codes"])
            scope_status = "known"
            frozen_known_total += frozen_count
        else:
            frozen_count = None
            scope_status = "missing"
            frozen_unknown_dates.append(day)
        day_items = items_by_day.get(day, [])
        day_windows = {}
        for window in windows:
            audit = _p0_window_audit(day_items, window, event_contexts[str(window)])
            day_windows[str(window)] = audit
            for key, value in audit["status_counts"].items():
                window_totals[str(window)][key] += value
            for key, value in audit["hs300_alpha_missing_reasons"].items():
                alpha_totals[str(window)][key] += value
            event_totals[str(window)] = len(event_contexts[str(window)]["anchor_ids"])
            valid_event_totals[str(window)] = len(event_contexts[str(window)]["valid_alpha_ids"])
        rows.append({
            "date": day,
            "market": {
                "history_available": bool(market),
                "amount_available": _p0_finite(market.get("amount_yi")),
                "zt_available": isinstance(market.get("zt"), dict) and _p0_finite((market.get("zt") or {}).get("count")),
                "amount_yi": market.get("amount_yi") if _p0_finite(market.get("amount_yi")) else None,
                "zt_count": (market.get("zt") or {}).get("count") if isinstance(market.get("zt"), dict) else None,
                "intraday": bool(market.get("intraday", False)),
                "data_quality": market_quality,
                "partial_components": partial_components,
                "missing_components": missing_components,
            },
            "formal_recommendation": {
                "snapshot_status": official_status,
                "scan_status": (official or {}).get("scan_status") if official else None,
                "policy_mode": ((official or {}).get("policy") or {}).get("mode") if official else None,
                "policy_reasons": sorted(((official or {}).get("policy") or {}).get("reasons") or []) if official else [],
                "bucket_counts": bucket_counts,
                "denominator": formal_count,
            },
            "research_snapshot": {
                "status": research.get("status", "missing"),
                "snapshot_type": research.get("snapshot_type"),
                "link_status": link_status,
                "records": len(research_content.get("records") or []),
                "selection_scope_status": scope_status,
                "frozen_candidate_denominator": frozen_count,
            },
            "observation_pool": {"denominator": observation_count},
            "windows": day_windows,
        })
    summary = {
        "market_history_available_days": sum(1 for row in rows if row["market"]["history_available"]),
        "amount_baseline_days": sum(1 for row in rows if row["market"]["amount_available"]),
        "zt_baseline_days": sum(1 for row in rows if row["market"]["zt_available"]),
        "market_partial_dates": market_partial_dates,
        "formal_recommendation_denominator": formal_total,
        "frozen_research_candidate_denominator": frozen_known_total,
        "frozen_research_candidate_unknown_dates": frozen_unknown_dates,
        "observation_pool_denominator": observation_total,
        "research_snapshot_link_statuses": dict(sorted(link_statuses.items())),
        "window_status_counts": {window: dict(sorted(values.items()))
                                 for window, values in window_totals.items()},
        "hs300_alpha_missing_reasons": {window: dict(sorted(values.items()))
                                         for window, values in alpha_totals.items()},
        "deduplicated_events": event_totals,
        "valid_alpha_events": valid_event_totals,
        "denominator_definitions": {
            "formal_recommendation": "actionable + waiting_trigger + next_day_confirmation from immutable official snapshot",
            "frozen_research_candidates": "selection_scope.codes only; missing scope remains unknown",
            "observation_pool": "observation + unenriched_observation from immutable official snapshot",
        },
    }
    content = {
        "schema_version": P0_AUDIT_SCHEMA_VERSION,
        "audit_kind": "p0_baseline_gap_audit",
        "date_range": {"start": days[0] if days else None, "end": days[-1] if days else None},
        "trading_days": days,
        "rows": rows,
        "summary": summary,
        "input": {
            "market_history_records": sum(1 for day in days if day in (market_history or {})),
            "official_snapshots": sum(1 for day in days if day in (official_history or {})),
            "research_inventory_records": sum(1 for day in days if day in (research_inventory or {})),
            "candidate_signal_items": sum(len(items_by_day[day]) for day in days),
            "windows": [int(window) for window in windows],
        },
        "disclaimer": "P0为证据链审计，不代表策略胜率或投资建议。",
    }
    return {"schema_version": P0_AUDIT_SCHEMA_VERSION,
            "audit_id": content_sha256(content)[:16],
            "content_sha256": content_sha256(content), "content": content}


def save_p0_audit(snapshot, root=DEFAULT_P0_AUDIT_ROOT):
    """Persist an audit by content hash; identical reruns are unchanged."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (snapshot["audit_id"] + ".json")
    payload = canonical_json(snapshot) + b"\n"
    if path.exists():
        return {"status": "unchanged", "path": str(path), "audit_id": snapshot["audit_id"]}
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "created", "path": str(path), "audit_id": snapshot["audit_id"]}


def save_diagnostics(snapshot, root=DEFAULT_ROOT):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (snapshot["diagnostic_id"] + ".json")
    payload = canonical_json(snapshot) + b"\n"
    if path.exists():
        return {"status": "unchanged", "path": str(path), "diagnostic_id": snapshot["diagnostic_id"]}
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "created", "path": str(path), "diagnostic_id": snapshot["diagnostic_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build deterministic candidate diagnostics")
    parser.add_argument("--p0", action="store_true", help="build the P0 baseline gap audit")
    parser.add_argument("--research-root", default=str(DEFAULT_RESEARCH_ROOT))
    parser.add_argument("--attribution-root", default=str(Path(CACHE_DIR) / "evolution" / "evaluations"))
    parser.add_argument("--market-history-root", default=str(DEFAULT_MARKET_HISTORY))
    parser.add_argument("--recommendation-root", default=str(DEFAULT_RECOMMENDATION_ROOT))
    parser.add_argument("--start-date", default="2026-09-08")
    parser.add_argument("--contract-id")
    parser.add_argument("--as-of")
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.p0:
        if not args.as_of:
            raise ValueError("p0_as_of_required")
        days = p0_trading_days(args.start_date, args.as_of)
        audit = build_p0_audit(
            days,
            load_market_regime_history(args.market_history_root),
            load_official_recommendation_history(args.recommendation_root,
                                                 args.start_date, args.as_of),
            load_p0_research_inventory(args.research_root, args.start_date, args.as_of),
            load_candidate_signal_items(args.attribution_root, args.contract_id, as_of=args.as_of),
        )
        if args.save:
            audit["tracking"] = save_p0_audit(audit, Path(args.output_root) / "p0")
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True) if args.json
              else audit["audit_id"])
        return 0
    snapshot = build_diagnostics(
        load_primary_research_snapshots(args.research_root, as_of=args.as_of),
        load_candidate_signal_items(args.attribution_root, args.contract_id, as_of=args.as_of),
        evaluation_as_of=args.as_of,
    )
    if args.save:
        snapshot["tracking"] = save_diagnostics(snapshot, args.output_root)
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) if args.json
          else snapshot["content"]["overall"]["mature_events"])
    return 0


if __name__ == "__main__":
    main()
