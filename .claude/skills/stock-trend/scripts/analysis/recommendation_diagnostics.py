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
from datetime import date
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.evolution_contract import PRIMARY_WINDOW, build_evaluation_contract
from core.evolution_storage import LEGACY_RESEARCH_ROOT, input_manifest, load_research_snapshot, storage_root
from core.recommendation_snapshot import canonical_json, content_sha256
from core.research_events import assign_research_events, summarize_daily_alpha


SCHEMA_VERSION = "recommendation-diagnostics/v1"
DEFAULT_RESEARCH_ROOT = storage_root("research")
DEFAULT_ROOT = storage_root("diagnostics")
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
    parser.add_argument("--research-root", default=str(DEFAULT_RESEARCH_ROOT))
    parser.add_argument("--attribution-root", default=str(Path(CACHE_DIR) / "evolution" / "evaluations"))
    parser.add_argument("--contract-id")
    parser.add_argument("--as-of")
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
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
