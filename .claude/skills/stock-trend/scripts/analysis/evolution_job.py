"""Idempotent P4 orchestration for recommendation-evolution operations.

This module deliberately does not run market collection or candidate scanning:
the scheduled close pipeline must finish those upstream jobs first.  It only
consumes their immutable formal records, so a failed upstream day remains an
observable gap rather than a fabricated recommendation.
"""
import argparse
import copy
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path: sys.path.insert(0, str(SCRIPT_ROOT))

from analysis.recommendation_attribution import track_official_history
from analysis.recommendation_diagnostics import (build_diagnostics, load_candidate_signal_items,
                                                  load_primary_research_snapshots, save_diagnostics,
                                                  DEFAULT_RESEARCH_ROOT,
                                                  DEFAULT_ROOT as DEFAULT_DIAGNOSTICS_ROOT)
from analysis.evolution_proposals import build_proposal_run, save_proposal_run
from backtesting.recommendation_experiments import default_experiment, run_walk_forward, save_experiment
from core.cache_utils import CACHE_DIR
from core.evolution_registry import (DEFAULT_RELEASE_ROOT, load_active_policy, publish_experiment,
                                     resolve_experiment, rollback_active_policy, transition_experiment,
                                     recover_policy_incident)
from core.recommendation_snapshot import canonical_json, content_sha256, load_official_snapshot
from core.research_events import summarize_daily_alpha

SCHEMA_VERSION = "recommendation-evolution-job/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "jobs"
DEFAULT_HISTORY_ROOT = Path(CACHE_DIR) / "recommendation_history"
DEFAULT_EVALUATION_ROOT = Path(CACHE_DIR) / "evolution" / "evaluations"
PRE_REGISTERED_DEGRADATION = {"data_failure_rate": .20, "minimum_coverage": .90,
                               "mean_alpha_floor": -.03}
MONITORING_CONTRACT = {"schema_version": "recommendation-evolution-monitor/v1",
                       "thresholds": PRE_REGISTERED_DEGRADATION,
                       "primary_window": 20, "recent_close_sessions": 5,
                       "outcome_cohort_sessions": 60}
_FAILURE_CLASSES = ("contract", "interface", "schema", "digest", "active_pointer")


def _day(value):
    try: return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc: raise ValueError("invalid_as_of_date") from exc


def _classify_failure(exc):
    """Expose only an allowlisted, credential-free operational failure class."""
    message = str(exc).lower()
    for failure_class in _FAILURE_CLASSES:
        if failure_class in message:
            return failure_class, failure_class
    return "upstream", type(exc).__name__


def _save(run, root=DEFAULT_ROOT):
    path = Path(root) / run["content"]["kind"] / (run["job_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists(): return {"status": "unchanged", "path": str(path), "job_id": run["job_id"]}
    # Completion time is persistence metadata, not part of the immutable job
    # content or its id.  Monitoring must never infer chronology from a hash.
    payload = {**copy.deepcopy(run), "completed_at": datetime.now(
        timezone(timedelta(hours=8))).isoformat()}
    tmp = path.with_suffix(".tmp"); tmp.write_bytes(canonical_json(payload) + b"\n")
    try: os.link(tmp, path); status = "created"
    except FileExistsError: status = "unchanged"
    finally: tmp.unlink(missing_ok=True)
    return {"status": status, "path": str(path), "job_id": run["job_id"]}


def _package(kind, content):
    body = {"schema_version": SCHEMA_VERSION, "kind": kind, **copy.deepcopy(content)}
    return {"job_id": content_sha256(body)[:16], "content_sha256": content_sha256(body), "content": body}


def _load_publish_evidence(value):
    """Load structured v2 release evidence from a JSON file or JSON argument."""
    try:
        candidate = Path(str(value or ""))
        is_file = candidate.is_file()
    except (OSError, ValueError):
        candidate, is_file = None, False
    if is_file:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("publish_evidence_json_required") from exc
    if not isinstance(payload, dict):
        raise ValueError("publish_evidence_object_required")
    return payload


def _formal_preflight(as_of, history_root):
    path = Path(history_root) / f"{as_of}.json"
    if not path.exists(): return {"ready": False, "reason": "formal_snapshot_missing", "path": str(path)}
    try:
        snapshot = load_official_snapshot(path)
        content = snapshot.get("content") or {}
        if content.get("snapshot_type") != "formal": return {"ready": False, "reason": "snapshot_not_formal", "path": str(path)}
        if not content.get("market_regime") or "sectors" not in content:
            return {"ready": False, "reason": "formal_context_or_sector_missing", "path": str(path)}
        return {"ready": True, "path": str(path), "content_sha256": snapshot.get("content_sha256")}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ready": False, "reason": "formal_snapshot_invalid:" + type(exc).__name__, "path": str(path)}


def run_close(as_of, history_root=DEFAULT_HISTORY_ROOT, attribution_root=DEFAULT_EVALUATION_ROOT,
              dry_run=False, tracker=track_official_history,
              research_root=DEFAULT_RESEARCH_ROOT):
    """Evaluate matured labels after an already-complete formal close record."""
    as_of = _day(as_of); preflight = _formal_preflight(as_of, history_root)
    base = {"as_of": as_of, "preflight": preflight, "active_policy": load_active_policy(),
            "degradation_thresholds": PRE_REGISTERED_DEGRADATION}
    if dry_run:
        return _package("close", {**base, "status": "dry_run",
                                  "stage": "attribution" if preflight["ready"] else "preflight"})
    try:
        output = tracker(history_root=history_root, attribution_root=attribution_root,
                         evaluation_as_of=as_of, through_date=as_of,
                         research_root=research_root)
        summary = output.get("summary") or {}
        candidate_summary = output.get("candidate_signal_summary") or {}
        historical_status = candidate_summary.get("status", "evidence_insufficient")
        stages = {
            "daily_collection": {
                "status": "formal_snapshot_available" if preflight["ready"] else "upstream_gap",
                "snapshot_path": preflight.get("path"),
                "reason": None if preflight["ready"] else preflight.get("reason"),
            },
            "historical_maturity_evaluation": {
                "status": historical_status,
                "candidate_signal": {
                    "status": historical_status,
                    # Report the same valid, de-duplicated alpha evidence used
                    # by the candidate readiness gate. Complete endpoints
                    # without a finite benchmark alpha remain audit-only.
                    "mature_dates": candidate_summary.get(
                        "valid_alpha_dates",
                        candidate_summary.get("alpha_mature_dates", 0),
                    ),
                    "mature_events": candidate_summary.get(
                        "valid_alpha_events",
                        candidate_summary.get("deduplicated_mature_events", 0),
                    ),
                    "valid_alpha_dates": candidate_summary.get(
                        "valid_alpha_dates",
                        candidate_summary.get("alpha_mature_dates", 0),
                    ),
                },
                "trade_simulation": {
                    "status": summary.get("status", "evidence_insufficient"),
                    "mature_dates": summary.get("mature_dates", 0),
                    "mature_events": summary.get("deduplicated_mature_events", 0),
                },
                "snapshots": summary.get("snapshots", 0),
            },
        }
        return _package("close", {**base,
                                  "status": "completed" if preflight["ready"] else "upstream_gap",
                                  "stage": "attribution" if preflight["ready"] else "preflight",
                                  "reason": None if preflight["ready"] else preflight.get("reason"),
                                  "candidate_signal_summary": output.get("candidate_signal_summary", {}),
                                  "research_link_statuses": output.get("research_link_statuses", []),
                                  "loader_stats": output.get("loader_stats"),
                                  "snapshots": summary.get("snapshots", 0),
                                  "historical_evaluation_status": historical_status,
                                  "trade_simulation_status": summary.get("status", "evidence_insufficient"),
                                  "stages": stages})
    except Exception as exc:
        failure_class, reason = _classify_failure(exc)
        return _package("close", {**base, "status": "failed", "stage": "attribution",
                                  "failure_class": failure_class, "reason": reason})


def run_weekly(as_of, research_root=DEFAULT_RESEARCH_ROOT,
               attribution_root=DEFAULT_EVALUATION_ROOT, contract_id=None, experiment_id=None,
               dry_run=False, diagnostics_root=DEFAULT_DIAGNOSTICS_ROOT,
               proposal_root=Path(CACHE_DIR) / "evolution" / "proposals"):
    """Build diagnostics offline, optionally replaying only a registered frozen experiment."""
    as_of = _day(as_of)
    research = load_primary_research_snapshots(research_root, as_of=as_of)
    items = load_candidate_signal_items(attribution_root, contract_id, as_of=as_of)
    diagnostics = build_diagnostics(research, items, evaluation_as_of=as_of)
    content = {"as_of": as_of, "status": "dry_run" if dry_run else "completed",
               "input": {"research_snapshots": len(research), "candidate_signal_items": len(items),
                         "contract_id": contract_id}, "diagnostic_id": diagnostics["diagnostic_id"]}
    if not dry_run:
        content["diagnostics"] = save_diagnostics(diagnostics, diagnostics_root)
        proposals = build_proposal_run(diagnostics, week=date.fromisoformat(as_of))
        content["proposals"] = save_proposal_run(proposals, proposal_root)
    if experiment_id:
        registered = resolve_experiment(experiment_id)
        definition = (registered.get("content") or {}).get("definition")
        experiment = run_walk_forward(
            research, items, definition=definition, experiment_id=experiment_id)
        content["experiment"] = {"experiment_id": experiment_id, "result_id": experiment["experiment_id"],
                                 "status": experiment["content"]["status"]}
        if not dry_run:
            content["experiment"]["persistence"] = save_experiment(experiment)
            if experiment["content"]["status"] == "validated" and registered["content"].get("state") == "draft":
                content["experiment"]["registry"] = transition_experiment(
                    experiment_id, "validated", {
                        "validation_result_id": experiment["experiment_id"],
                        "as_of": as_of,
                    })
    return _package("weekly", content)


def _close_run_days(job_root):
    """Return the last completed attempt per business day, never filename order."""
    attempts = []
    for path in (Path(job_root) / "close").glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            content = payload.get("content", {})
            as_of = _day(content.get("as_of"))
            completed_at = payload.get("completed_at") or content.get("completed_at")
            if not isinstance(completed_at, str) or not completed_at:
                completed_at = ""
            attempts.append((as_of, completed_at, content))
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            continue
    latest = {}
    for as_of, completed_at, content in sorted(attempts, key=lambda row: (row[0], row[1])):
        latest[as_of] = content
    return latest


def _monitoring_coverage(items, expected_days, as_of):
    """Calculate monitor denominators from real frozen evaluation rows."""
    required = ("record_id", "recommendation_date", "market", "code")
    complete, due, intact = 0, 0, 0
    recommendation_days = set()
    for item in items:
        if not isinstance(item.get("recommendation_date"), str) \
                or item["recommendation_date"] > as_of:
            continue
        if all(item.get(key) for key in required):
            intact += 1
        day = item.get("recommendation_date")
        if isinstance(day, str): recommendation_days.add(day)
        window = (item.get("windows") or {}).get("20") or {}
        status = window.get("status")
        label_end = window.get("exit_date")
        if isinstance(label_end, str) and label_end <= as_of and status != "pending":
            due += 1
        value = window.get("hs300_alpha")
        if status == "complete" and isinstance(value, (int, float)) \
                and not isinstance(value, bool) and math.isfinite(value):
            complete += 1
    expected = set(expected_days or [])
    return {
        "recommendation_date_coverage": (len(recommendation_days & expected) / len(expected)
                                         if expected else None),
        "research_record_integrity": intact / len(items) if items else None,
        "mature_outcome_coverage": complete / due if due else None,
        "due_outcomes": due, "complete_outcomes": complete,
    }


def _is_contract_failure(run):
    return run.get("failure_class") in {"contract", "interface", "schema", "digest", "active_pointer"}


def monitoring_snapshot(job_root=DEFAULT_ROOT, attribution_root=DEFAULT_EVALUATION_ROOT,
                        contract_id=None, expected_trading_days=None,
                        release_root=DEFAULT_RELEASE_ROOT, as_of=None):
    """Pre-registered degradation checks; statistical drift requests review, not auto tuning."""
    close_by_day = _close_run_days(job_root)
    expected = sorted(set(_day(day) for day in (expected_trading_days or [])))
    as_of = _day(as_of) if as_of else (expected[-1] if expected else None)
    if as_of:
        expected = [day for day in expected if day <= as_of]
    recent = [close_by_day.get(day) for day in expected[-5:]]
    observed = [run for run in recent if run is not None]
    # A missing expected close task is a data failure, not an omitted sample.
    failed = sum(run is None or run.get("status") in ("failed", "upstream_gap") for run in recent)
    items = load_candidate_signal_items(attribution_root, contract_id)
    cohort_days = set(expected[-60:])
    cohort_start = min(cohort_days) if cohort_days else None
    monitor_items = [item for item in items
                     if as_of and isinstance(item.get("recommendation_date"), str)
                     and item["recommendation_date"] <= as_of
                     and (cohort_start is None or item["recommendation_date"] >= cohort_start)]
    complete = [((x.get("windows") or {}).get("20") or {}) for x in monitor_items]
    mature = [x for x in complete if x.get("status") == "complete"
              and isinstance(x.get("hs300_alpha"), (int, float))
              and not isinstance(x.get("hs300_alpha"), bool)
              and math.isfinite(x.get("hs300_alpha"))]
    alpha_summary = summarize_daily_alpha([
        {"recommendation_date": item.get("recommendation_date"),
         "hs300_alpha": (item.get("windows") or {}).get("20", {}).get("hs300_alpha"),
         "evaluation_status": (item.get("windows") or {}).get("20", {}).get("status")}
        for item in monitor_items
    ])
    alpha = alpha_summary["mean_alpha"]
    failure_rate = failed / len(recent) if recent else None
    coverage = _monitoring_coverage(monitor_items, expected[-60:], as_of or "")
    insufficient = []
    if not as_of: insufficient.append("monitoring_as_of_required")
    if not expected: insufficient.append("trading_calendar_required")
    if not recent: insufficient.append("close_runs_missing")
    if len(recent) < 5: insufficient.append("expected_trading_days_insufficient")
    if not mature: insufficient.append("mature_outcomes_missing")
    if coverage["mature_outcome_coverage"] is None: insufficient.append("mature_outcome_denominator_missing")
    reasons = []
    contract_failures = [run for run in observed if _is_contract_failure(run)]
    upstream_failure_days = [run.get("as_of") for run in observed
                             if run.get("status") in ("failed", "upstream_gap")]
    maturity_status_counts = {}
    for window in complete:
        key = str(window.get("status") or "unknown")
        maturity_status_counts[key] = maturity_status_counts.get(key, 0) + 1
    if failure_rate is not None and failure_rate > PRE_REGISTERED_DEGRADATION["data_failure_rate"]: reasons.append("data_failure_rate")
    if alpha is not None and alpha < PRE_REGISTERED_DEGRADATION["mean_alpha_floor"]: reasons.append("mature_alpha_review")
    if coverage["recommendation_date_coverage"] is not None \
            and coverage["recommendation_date_coverage"] < PRE_REGISTERED_DEGRADATION["minimum_coverage"]:
        reasons.append("recommendation_date_coverage")
    if coverage["research_record_integrity"] is not None \
            and coverage["research_record_integrity"] < PRE_REGISTERED_DEGRADATION["minimum_coverage"]:
        reasons.append("research_record_integrity")
    if coverage["mature_outcome_coverage"] is not None \
            and coverage["mature_outcome_coverage"] < PRE_REGISTERED_DEGRADATION["minimum_coverage"]:
        reasons.append("mature_outcome_coverage")
    fallback = None
    if contract_failures:
        fallback = recover_policy_incident({
            "failure_class": contract_failures[-1]["failure_class"],
            "trigger": "monitor_contract_failure",
            "business_dates": [run.get("as_of") for run in contract_failures],
        }, release_root)
    status = ("fallback_required" if contract_failures else
              "insufficient_data" if insufficient else ("review_required" if reasons else "healthy"))
    return _package("monitor", {"status": status,
             "active_policy": load_active_policy(release_root), "monitoring_contract": MONITORING_CONTRACT,
             "thresholds": PRE_REGISTERED_DEGRADATION,
             "data_failure_rate": failure_rate, "mature_mean_hs300_alpha": alpha,
             "mature_events": len(mature),
             "mature_dates": alpha_summary["mature_dates"],
             "alpha_missing_records": alpha_summary["missing_records"],
             "close_run_days": len(close_by_day), "expected_trading_days": expected[-5:],
             "upstream_failure_days": upstream_failure_days,
             "maturity_status_counts": maturity_status_counts,
             "coverage": coverage, "insufficient_reasons": insufficient, "reasons": reasons,
             "contract_failure_days": [run.get("as_of") for run in contract_failures],
             "fallback": fallback})


def main(argv=None):
    parser = argparse.ArgumentParser(description="P4 recommendation evolution run and release manager")
    parser.add_argument("command", choices=("close", "weekly", "monitor", "publish", "rollback"))
    parser.add_argument("--as-of", default=date.today().isoformat()); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--contract-id"); parser.add_argument("--experiment-id"); parser.add_argument("--evidence", default="manual_release")
    parser.add_argument("--research-root")
    parser.add_argument("--attribution-root")
    parser.add_argument("--diagnostics-root")
    parser.add_argument("--proposal-root")
    parser.add_argument("--trading-sessions", help="JSON file or JSON array for monitor calendar")
    parser.add_argument("--json", action="store_true"); args = parser.parse_args(argv)
    if args.command == "close":
        run = run_close(args.as_of, dry_run=args.dry_run,
                        research_root=args.research_root or DEFAULT_RESEARCH_ROOT,
                        attribution_root=args.attribution_root or DEFAULT_EVALUATION_ROOT)
    elif args.command == "weekly": run = run_weekly(args.as_of, contract_id=args.contract_id,
                                                       experiment_id=args.experiment_id, dry_run=args.dry_run,
                                                       research_root=args.research_root or DEFAULT_RESEARCH_ROOT,
                                                       attribution_root=args.attribution_root or DEFAULT_EVALUATION_ROOT,
                                                       diagnostics_root=args.diagnostics_root or DEFAULT_DIAGNOSTICS_ROOT,
                                                       proposal_root=args.proposal_root or Path(CACHE_DIR) / "evolution" / "proposals")
    elif args.command == "monitor":
        sessions = None
        if args.trading_sessions:
            source = Path(args.trading_sessions)
            raw = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else json.loads(args.trading_sessions)
            sessions = raw.get("trading_sessions") if isinstance(raw, dict) else raw
            if not isinstance(sessions, list): raise ValueError("trading_sessions_list_required")
        run = monitoring_snapshot(contract_id=args.contract_id, expected_trading_days=sessions,
                                  as_of=args.as_of)
    elif args.command == "publish":
        if args.dry_run:
            record = resolve_experiment(args.experiment_id)
            run = _package("publish", {"status": "dry_run", "experiment_id": args.experiment_id,
                                        "registry_state": record["content"].get("state")})
        else:
            run = publish_experiment(args.experiment_id, _load_publish_evidence(args.evidence))
    else:
        if args.dry_run:
            run = _package("rollback", {"status": "dry_run", "active_policy": load_active_policy()})
        else:
            run = rollback_active_policy({"summary": args.evidence})
    if args.command in ("close", "weekly", "monitor") and not args.dry_run: run["persistence"] = _save(run)
    print(json.dumps(run, ensure_ascii=False, sort_keys=True) if args.json else run.get("content", run).get("status"))
    return 0


if __name__ == "__main__": main()
