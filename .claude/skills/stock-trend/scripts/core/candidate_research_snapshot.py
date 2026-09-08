"""Immutable research records for the full /candidates scan population.

These records are intentionally separate from official recommendations: they
retain rejected and truncated rows needed for later research, while only a
formal, non-conflicting run may be consumed as a training sample.
"""
import copy
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .recommendation_snapshot import canonical_json, content_sha256, _normalize_for_json
from .evolution_storage import input_manifest, storage_root


SCHEMA_VERSION = "candidate-research-snapshot/v1"
DEFAULT_ROOT = storage_root("research")


def _bucket_by_code(buckets):
    return {
        str(item.get("code")): name
        for name, rows in (buckets or {}).items()
        for item in (rows or [])
        if isinstance(item, dict) and item.get("code")
    }


def _selection_reason(item, bucket, min_score):
    if item.get("research_terminal_reason"):
        return item["research_terminal_reason"]
    if bucket:
        return "bucket_" + bucket
    try:
        below_floor = float(item.get("composite_score") or 0) < float(min_score)
    except (TypeError, ValueError):
        below_floor = True
    return "raw_score_below_min_score" if below_floor else "top_n_truncated"


def _evidence_summary(item):
    """Keep provenance and compact evidence, not a mutable cache pointer."""
    quality = item.get("data_quality") or {}
    return {
        "data_quality": copy.deepcopy(quality),
        "source_evidence": copy.deepcopy(item.get("source_evidence") or {}),
        "source_dates": {
            name: (detail or {}).get("data_date")
            for name, detail in (quality.get("dimensions") or {}).items()
            if isinstance(detail, dict)
        },
        "raw_input_reference": {
            "ts_code": item.get("ts_code"),
            "sector_memberships": copy.deepcopy(item.get("sector_memberships") or []),
            "ranking_source": item.get("ranking_source"),
            "membership_source": item.get("membership_source"),
        },
    }


def _market_for(item):
    value = item.get("market") or item.get("exchange")
    if value:
        return str(value)
    ts_code = str(item.get("ts_code") or "")
    if "." in ts_code:
        return ts_code.rsplit(".", 1)[-1].upper()
    return "default"


def _source_time_status(known_at, captured_at):
    if known_at and captured_at:
        return "known_and_captured"
    if known_at:
        return "known"
    if captured_at:
        return "captured"
    return "unknown"


def _finite_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _preselection_contract(candidate, min_score):
    """Freeze the selector inputs required for deterministic experiment replay."""
    composite = _finite_number(candidate.get("composite_score"))
    quality = _finite_number(candidate.get("quality_adjusted_score"))
    quality_payload = candidate.get("data_quality")
    data_quality_eligible = (
        quality_payload.get("eligible")
        if isinstance(quality_payload, dict) else None
    )
    sector_actionable = candidate.get("sector_actionable")
    score_eligible = (
        quality is not None and _finite_number(min_score) is not None
        and quality >= float(min_score)
    ) if isinstance(data_quality_eligible, bool) else None
    qualification_status = "unknown"
    if (isinstance(data_quality_eligible, bool)
            and isinstance(sector_actionable, bool)
            and isinstance(score_eligible, bool)):
        qualification_status = (
            "eligible" if data_quality_eligible and sector_actionable and score_eligible
            else "ineligible"
        )
    # Keep the hierarchy explicit even when the scanner did not expose a
    # richer phase label.  ``unknown`` is intentionally replay-ineligible.
    hierarchy = {
        "phase": candidate.get("selection_phase")
        or candidate.get("phase") or "unknown",
        "layer": candidate.get("selection_layer")
        or candidate.get("layer") or "unknown",
        "source": candidate.get("ranking_source") or "unknown",
    }
    return {
        "composite_score": composite,
        "quality_adjusted_score": quality,
        "data_quality_eligible": data_quality_eligible,
        "sector_actionable": sector_actionable,
        "score_eligible": score_eligible,
        "qualification_status": qualification_status,
        "hierarchy": hierarchy,
        "min_score": _finite_number(min_score),
    }


def build_research_snapshot(scanned_candidates, buckets, recommendation_date,
                            policy, market_regime, sector_codes, min_score,
                            official_tracking=None, known_at=None,
                            model_version="daily-candidates/v4",
                            parameter_summary=None, decision_at=None,
                            captured_at=None):
    """Build a detached, deterministic snapshot for every scanned object."""
    by_code = _bucket_by_code(buckets)
    records = []
    for item in scanned_candidates or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        candidate = copy.deepcopy(item)
        code = str(candidate["code"])
        market = _market_for(candidate)
        record_known_at = known_at if known_at is not None else candidate.get("known_at")
        record_captured_at = captured_at if captured_at is not None else candidate.get("captured_at")
        bucket = by_code.get(code)
        preselection = _preselection_contract(candidate, min_score)
        records.append({
            "record_id": f"{recommendation_date}:{market}:{code}",
            "code": code,
            "market": market,
            "basis_date": recommendation_date,
            "decision_at": decision_at,
            "known_at": record_known_at,
            "captured_at": record_captured_at,
            "source_time_status": _source_time_status(record_known_at, record_captured_at),
            "selection_status": "selected" if bucket else "not_selected",
            "final_status": bucket or item.get("research_terminal_status") or "not_selected",
            "selection_reason": _selection_reason(candidate, bucket, min_score),
            # Frozen selector inputs are separate from the mutable candidate
            # payload so replay can reject unknown/contradictory fields.
            "preselection": preselection,
            "selection_hierarchy": copy.deepcopy(preselection["hierarchy"]),
            "selection_parameters": {
                "top": (parameter_summary or {}).get("top"),
                "min_score": preselection["min_score"],
            },
            "scores": {
                "raw_composite_score": candidate.get("raw_composite_score"),
                "composite_score": candidate.get("composite_score"),
                "quality_adjusted_score": candidate.get("quality_adjusted_score"),
                "execution_priority_score": candidate.get("execution_priority_score"),
                "buy_point_priority_bonus": candidate.get("buy_point_priority_bonus"),
                "buy_point_level": candidate.get("buy_point_level"),
            },
            "sector_persistence": {
                key: candidate.get(key) for key in (
                    "sector_code", "sector_name", "sector_actionable",
                    "sector_persistence_status", "history_window_days",
                    "history_coverage_days", "hot_appearance_days", "hot_streak",
                )
            },
            "evidence": _evidence_summary(candidate),
            # Full frozen candidate preserves all model inputs available to the
            # decision, including per-dimension score and Wyckoff evidence.
            "candidate": candidate,
        })
    records.sort(key=lambda row: row["code"])
    content = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_date": recommendation_date,
        "decision_at": decision_at,
        "known_at": known_at,
        "captured_at": captured_at,
        "source_time_status": _source_time_status(known_at, captured_at),
        "snapshot_type": "provisional" if (policy or {}).get("provisional") else "formal",
        "model_version": model_version,
        "policy": copy.deepcopy(policy or {}),
        "market_regime": copy.deepcopy(market_regime or {}),
        "sectors": copy.deepcopy(sector_codes or []),
        "parameter_summary": copy.deepcopy(parameter_summary or {}),
        "input_manifest": input_manifest(
            candidate_records=records,
            market_regime=market_regime or {},
            policy=policy or {},
            sectors=sector_codes or [],
            model_version=model_version,
            parameter_summary=parameter_summary or {},
            official_snapshot={
                "content_sha256": (official_tracking or {}).get("content_sha256"),
                "link_status": "linked" if (official_tracking or {}).get("status") in ("created", "unchanged") else "unlinked",
            },
        ),
        # ``created`` and ``unchanged`` describe the same formal decision on
        # a rerun.  Persist the stable linkage rather than write telemetry
        # into the run identity.
        "official_snapshot": {
            "link_status": (
                "linked" if (official_tracking or {}).get("status")
                in ("created", "unchanged") else "unlinked"
            ),
            "path": (official_tracking or {}).get("path"),
            "content_sha256": (official_tracking or {}).get("content_sha256"),
        },
        "records": records,
    }
    run_id = content_sha256(content)[:16]
    return _normalize_for_json({
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "content_sha256": content_sha256(content),
        "content": content,
    })


def _atomic_new(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def save_research_snapshot(snapshot, root=DEFAULT_ROOT):
    """Persist idempotently; same-day alternatives are conflict audit only."""
    root = Path(root)
    snapshot = _normalize_for_json(copy.deepcopy(snapshot))
    content = snapshot.get("content") or {}
    day = str(content.get("recommendation_date") or "")
    run_id = str(snapshot.get("run_id") or "")
    if not day or not run_id or snapshot.get("content_sha256") != content_sha256(content):
        raise ValueError("invalid candidate research snapshot")
    payload = canonical_json(snapshot) + b"\n"
    is_provisional = content.get("snapshot_type") == "provisional"
    tracking = content.get("official_snapshot") or {}
    official_ok = tracking.get("link_status") == "linked"
    day_root = root / day
    target = day_root / ("provisional" if is_provisional else "formal") / (run_id + ".json")
    if target.exists():
        return {"status": "unchanged", "path": str(target), "run_id": run_id,
                "content_sha256": snapshot["content_sha256"], "training_eligible": False}
    # Only one formal, official-linked run becomes the date's research sample.
    index = day_root / "formal" / "primary.json"
    if not is_provisional and (not official_ok or (index.exists() and
            index.read_text(encoding="utf-8").strip() != run_id)):
        conflict = day_root / "conflicts" / (run_id + ".json")
        _atomic_new(conflict, payload)
        return {"status": "conflict", "path": str(conflict), "run_id": run_id,
                "content_sha256": snapshot["content_sha256"], "training_eligible": False}
    created = _atomic_new(target, payload)
    if not is_provisional and created:
        _atomic_new(index, (run_id + "\n").encode())
    return {"status": "created" if created else "unchanged", "path": str(target),
            "run_id": run_id, "content_sha256": snapshot["content_sha256"],
            "training_eligible": bool(not is_provisional and official_ok)}


def save_research_snapshot_safely(snapshot, root=DEFAULT_ROOT):
    try:
        return save_research_snapshot(snapshot, root=root)
    except Exception as exc:
        return {"status": "write_failed", "path": None, "run_id": snapshot.get("run_id"),
                "content_sha256": snapshot.get("content_sha256"),
                "training_eligible": False, "reason": f"{type(exc).__name__}: {exc}"}
