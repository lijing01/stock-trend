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
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.evolution_contract import PRIMARY_WINDOW
from core.evolution_storage import LEGACY_RESEARCH_ROOT, input_manifest, load_research_snapshot, storage_root
from core.recommendation_snapshot import canonical_json, content_sha256


SCHEMA_VERSION = "recommendation-diagnostics/v1"
DEFAULT_RESEARCH_ROOT = storage_root("research")
DEFAULT_ROOT = storage_root("diagnostics")
MIN_GROUP_EVENTS = 30


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


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
    return {
        "selection_status": record.get("final_status") or "unknown",
        "buy_point_level": score.get("buy_point_level") or candidate.get("buy_point_level") or "unknown",
        "market_regime_band": _band(
            regime.get("score", regime.get("market_score")), (60, 80), ("weak_<60", "neutral_60_79", "strong_80_plus")),
        "sector_persistence": persistence.get("sector_persistence_status") or (
            "actionable" if persistence.get("sector_actionable") else "not_actionable"),
        "quality_score_band": _band(quality, (60, 80), ("low_<60", "medium_60_79", "high_80_plus")),
    }


def _outcome_index(items, primary_window):
    index = {}
    label = str(primary_window)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("recommendation_date") or ""), str(item.get("code") or ""))
        if not all(key):
            continue
        window = (item.get("windows") or {}).get(label)
        if isinstance(window, dict):
            index[key] = copy.deepcopy(window)
    return index


def _summarize_group(rows):
    complete = [row for row in rows if row["status"] == "complete"]
    values = [row["hs300_alpha"] for row in complete if row.get("hs300_alpha") is not None]
    returns = [row["signal_return"] for row in complete if row.get("signal_return") is not None]
    maes = [row["mae"] for row in complete if row.get("mae") is not None]
    dates = sorted({row["recommendation_date"] for row in complete})
    statuses = defaultdict(int)
    for row in rows:
        statuses[row["status"]] += 1
    return {
        "records": len(rows), "mature_events": len(complete),
        "mature_dates": len(dates), "time_distribution": dates,
        "missing_outcome": statuses["missing_outcome"], "pending": statuses["pending"],
        "data_errors": statuses["data_error"], "unexecutable": statuses["unexecutable"],
        "mean_hs300_alpha": sum(values) / len(values) if values else None,
        "median_hs300_alpha": _median(values),
        "mean_signal_return": sum(returns) / len(returns) if returns else None,
        "mean_mae": sum(maes) / len(maes) if maes else None,
        "proposal_eligible": len(complete) >= MIN_GROUP_EVENTS,
    }


def build_diagnostics(research_snapshots, candidate_signal_items, primary_window=PRIMARY_WINDOW):
    """Join immutable scan records to P0 signal outcomes and group once per dimension."""
    outcome_by_key = _outcome_index(candidate_signal_items, primary_window)
    rows, skipped = [], 0
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        # Conflict and intraday samples remain auditable but cannot be research input.
        official = content.get("official_snapshot") or {}
        if content.get("snapshot_type") != "formal" or official.get("link_status") != "linked":
            skipped += len(content.get("records") or [])
            continue
        for record in content.get("records") or []:
            if not isinstance(record, dict) or not record.get("code"):
                continue
            key = (str(record.get("basis_date") or content.get("recommendation_date") or ""), str(record["code"]))
            outcome = outcome_by_key.get(key)
            window = outcome or {}
            status = window.get("status") if outcome else "missing_outcome"
            rows.append({
                "recommendation_date": key[0], "code": key[1], "status": status,
                "signal_return": _number(window.get("signal_return")),
                "hs300_alpha": _number(window.get("hs300_alpha")),
                "mae": _number(window.get("mae")),
                "dimensions": diagnostic_dimensions({**record, "market_regime": content.get("market_regime") or {}}),
            })
    grouped = {name: defaultdict(list) for name in (
        "selection_status", "buy_point_level", "market_regime_band",
        "sector_persistence", "quality_score_band")}
    for row in rows:
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
    overall = _summarize_group(rows)
    content = {
        "schema_version": SCHEMA_VERSION, "primary_window": primary_window,
        "correlation_notice": "分组归因仅描述相关性，不表示因果关系或策略有效性。",
        "overall": overall, "dimensions": dimensions, "evidence_index": evidence_index,
        "input": {"research_snapshots": len(research_snapshots or []),
                  "candidate_signal_items": len(candidate_signal_items or []),
                  "skipped_ineligible_records": skipped},
    }
    content["input_manifest"] = input_manifest(
        research_run_ids=sorted(str((item or {}).get("run_id") or "") for item in research_snapshots or []),
        research_content_sha256=sorted(str((item or {}).get("content_sha256") or "") for item in research_snapshots or []),
        evaluation_window=primary_window,
        candidate_signal_items=candidate_signal_items or [],
    )
    return {"schema_version": SCHEMA_VERSION, "diagnostic_id": content_sha256(content)[:16],
            "content_sha256": content_sha256(content), "content": content}


def load_primary_research_snapshots(root=DEFAULT_RESEARCH_ROOT):
    root = Path(root)
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
            if day in seen_dates:
                continue
            run_id = index.read_text(encoding="utf-8").strip()
            path = index.parent / (run_id + ".json")
            try:
                snapshots.append(load_research_snapshot(path))
                seen_dates.add(day)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return snapshots


def load_candidate_signal_items(root, contract_id=None):
    """Load P0 candidate outcomes from exactly one evaluation contract.

    Contract identities must never be mixed because their adjustment and
    evaluation rules can differ.  Callers choose one explicit contract when a
    root contains more than one.
    """
    root = Path(root)
    if contract_id:
        roots = [root / contract_id]
    else:
        roots = [path for path in root.iterdir() if path.is_dir()] if root.exists() else []
        if not roots:
            return []
        if len(roots) != 1:
            raise ValueError("evaluation_contract_id_required")
    items = []
    for path in sorted(roots[0].glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.extend(payload.get("candidate_signal_items") or [])
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
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    snapshot = build_diagnostics(
        load_primary_research_snapshots(args.research_root),
        load_candidate_signal_items(args.attribution_root, args.contract_id),
    )
    if args.save:
        snapshot["tracking"] = save_diagnostics(snapshot, args.output_root)
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) if args.json
          else snapshot["content"]["overall"]["mature_events"])
    return 0


if __name__ == "__main__":
    main()
