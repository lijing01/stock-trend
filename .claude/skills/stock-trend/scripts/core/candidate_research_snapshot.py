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

from .cache_utils import CACHE_DIR
from .recommendation_snapshot import canonical_json, content_sha256, _normalize_for_json


SCHEMA_VERSION = "candidate-research-snapshot/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "candidate_research_history"


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


def build_research_snapshot(scanned_candidates, buckets, recommendation_date,
                            policy, market_regime, sector_codes, min_score,
                            official_tracking=None, known_at=None,
                            model_version="daily-candidates/v4",
                            parameter_summary=None):
    """Build a detached, deterministic snapshot for every scanned object."""
    by_code = _bucket_by_code(buckets)
    records = []
    for item in scanned_candidates or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        candidate = copy.deepcopy(item)
        code = str(candidate["code"])
        bucket = by_code.get(code)
        records.append({
            "code": code,
            "basis_date": recommendation_date,
            "known_at": known_at or recommendation_date,
            "selection_status": "selected" if bucket else "not_selected",
            "final_status": bucket or item.get("research_terminal_status") or "not_selected",
            "selection_reason": _selection_reason(candidate, bucket, min_score),
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
        "snapshot_type": "provisional" if (policy or {}).get("provisional") else "formal",
        "model_version": model_version,
        "policy": copy.deepcopy(policy or {}),
        "market_regime": copy.deepcopy(market_regime or {}),
        "sectors": copy.deepcopy(sector_codes or []),
        "parameter_summary": copy.deepcopy(parameter_summary or {}),
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
