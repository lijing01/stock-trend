"""Frozen, research-only single-dimension ablations of daily candidate ranks."""

import copy
import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path

from backtesting.recommendation_experiments import (
    DEFAULT_CONTRACT_ID, _frozen_replay_candidate, _replay_selection,
)
from core.recommendation_snapshot import content_sha256
from core.recommendation_snapshot import canonical_json, load_official_snapshot, DEFAULT_ROOT as OFFICIAL_ROOT
from core.evolution_storage import EVOLUTION_ROOT, load_research_snapshot
from core.research_events import assign_research_events
from analysis.recommendation_diagnostics import load_candidate_signal_items
from scans.daily_candidates import (
    _candidate_gate_pass, _entry_timing_bucket, classify_candidates,
)
from analysis.wyckoff import classify_entry_timing

SCHEMA_VERSION = "factor-ablation/v1"
WEIGHTS = {"momentum": .25, "volume_price": .15, "capital": .15,
           "fundamental": .10, "sector_strength": .10, "wyckoff": .25}
TOP_K = (1, 3, 5)
RECOMMENDATION_BUCKETS = ("actionable", "waiting_trigger")
ALL_BUCKETS = ("actionable", "waiting_trigger", "next_day_confirmation",
               "observation", "data_rejected", "unenriched_observation")


def definition():
    """The registered comparisons and outcome rules are constant across days."""
    return {"schema_version": SCHEMA_VERSION, "weights": WEIGHTS,
            "treatments": [f"remove_{name}" for name in WEIGHTS],
            "top_k": list(TOP_K), "windows": [5, 10, 20, 60],
            "primary_window": 20, "confirmation_window": 60,
            "minimum_mature_dates": 20, "minimum_unique_alpha_events": 100,
            "evaluation_contract_id": DEFAULT_CONTRACT_ID,
            "changes": ["within_bucket_ranking"],
            "five_day_role": "data_quality_and_anomaly_only",
            "multiple_comparisons": "six_pre_registered_removals"}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_tenth(left, right):
    right = _number(right)
    return right is not None and abs(left - right) < 1e-8


def _codes(rows):
    return [str(row.get("code")) for row in rows or []]


def _bucket_codes(buckets):
    return {name: _codes((buckets or {}).get(name)) for name in ALL_BUCKETS}


def _scope_records(content):
    """Require explicit, signed scope metadata; no historic scope inference."""
    scope = content.get("selection_scope")
    records = content.get("records")
    if not isinstance(scope, dict) or not isinstance(records, list):
        return None, "scope_unverified"
    mode = scope.get("mode")
    codes = scope.get("codes")
    if mode not in ("all", "codes", "full_scan", "explicit_codes") or not isinstance(codes, list):
        return None, "scope_unverified"
    if not all(isinstance(code, str) and code for code in codes) or len(codes) != len(set(codes)):
        return None, "scope_unverified"
    if scope.get("codes_sha256") != content_sha256(sorted(codes)):
        return None, "scope_unverified"
    included = []
    seen_ids = set()
    for record in records:
        if not isinstance(record, dict) or not record.get("record_id"):
            return None, "input_incomplete"
        if record["record_id"] in seen_ids:
            return None, "input_incomplete"
        seen_ids.add(record["record_id"])
        flag = record.get("selection_scope_included")
        if not isinstance(flag, bool):
            return None, "scope_unverified"
        expected = record.get("code") in codes
        if flag != expected:
            return None, "scope_unverified"
        if flag:
            included.append(record)
    if mode in ("codes", "explicit_codes") and set(codes) != {row["code"] for row in included}:
        return None, "scope_unverified"
    if mode in ("all", "full_scan") and set(codes) != {row["code"] for row in included}:
        return None, "scope_unverified"
    return included, None


def _reconstruct(record):
    candidate = record.get("candidate") or {}
    dimensions = candidate.get("raw_dimensions")
    quality = candidate.get("data_quality")
    scores = record.get("scores") or {}
    if not isinstance(dimensions, dict) or set(dimensions) != set(WEIGHTS):
        return None, "missing_six_dimensions"
    values = {name: _number(dimensions.get(name)) for name in WEIGHTS}
    if any(value is None for value in values.values()):
        return None, "invalid_dimension"
    if not isinstance(quality, dict):
        return None, "missing_quality_factor"
    coverage = _number(quality.get("coverage_factor"))
    freshness = _number(quality.get("freshness_factor"))
    bonus = _number(scores.get("buy_point_priority_bonus"))
    if coverage is None or freshness is None or bonus is None:
        return None, "missing_quality_or_bonus"
    wyckoff = candidate.get("wyckoff") or {}
    if "entry_timing" in wyckoff and classify_entry_timing(wyckoff).get("executable") is not True:
        if abs(bonus) > 1e-8:
            return None, "timing_blocked_bonus_mismatch"
    raw = round(sum(values[name] * weight for name, weight in WEIGHTS.items()), 1)
    adjusted = round(raw * coverage * freshness, 1)
    priority = round(min(100.0, adjusted + bonus), 1)
    preselection = record.get("preselection") or {}
    expected = ((raw, scores.get("raw_composite_score")),
                (raw, preselection.get("composite_score")),
                (adjusted, preselection.get("quality_adjusted_score")),
                (priority, scores.get("execution_priority_score")))
    if not all(_same_tenth(left, right) for left, right in expected):
        return None, "score_reconstruction_mismatch"
    if any(not _same_tenth(value, candidate.get(field)) for value, field in (
            (raw, "raw_composite_score"), (raw, "composite_score"),
            (adjusted, "quality_adjusted_score"),
            (priority, "execution_priority_score"))):
        return None, "score_reconstruction_mismatch"
    return {"dimensions": values, "coverage": coverage,
            "freshness": freshness, "bonus": bonus}, None


def _shadow_priority(frozen, removed):
    raw = round(sum(value * WEIGHTS[name] for name, value in frozen["dimensions"].items()
                    if name != removed) / (1 - WEIGHTS[removed]), 1)
    adjusted = round(raw * frozen["coverage"] * frozen["freshness"], 1)
    return round(min(100.0, adjusted + frozen["bonus"]), 1)


def _rank(records, evidence, removed, top, min_score, policy):
    candidates = []
    for record in records:
        candidate, reason = _frozen_replay_candidate(record, min_score)
        if reason:
            raise ValueError(reason)
        if candidate["composite_score"] < min_score:
            continue
        candidate["execution_priority_score"] = _shadow_priority(
            evidence[record["record_id"]], removed)
        candidates.append(candidate)
    candidates.sort(key=lambda item: (
        _candidate_gate_pass(item, min_score, policy), _entry_timing_bucket(item),
        item["execution_priority_score"], str(item.get("code", ""))), reverse=True)
    selected = candidates[:top]
    return selected, classify_candidates(selected, policy)


def run_daily_ablation(research_snapshot, official_snapshot):
    """Validate one frozen formal date and return six shadow rankings.

    This function has no filesystem, provider, or publishing side effects.
    Any failed integrity check stops all six comparisons for the date.
    """
    content = (research_snapshot or {}).get("content") or {}
    formal = (official_snapshot or {}).get("content") or {}
    day = content.get("recommendation_date")
    result = {"schema_version": SCHEMA_VERSION, "recommendation_date": day,
              "research_snapshot_sha256": (research_snapshot or {}).get("content_sha256"),
              "official_snapshot_sha256": (official_snapshot or {}).get("content_sha256"),
              "definition": definition(), "status": "skipped", "reason": None,
              "treatments": {}}
    if content.get("snapshot_type") != "formal" or formal.get("snapshot_type") != "formal":
        result["reason"] = "not_formal"
        return result
    if not day or day != formal.get("recommendation_date"):
        result["reason"] = "date_mismatch"
        return result
    if (content.get("official_snapshot") or {}).get("link_status") != "linked":
        result["reason"] = "official_unlinked"
        return result
    # Legacy snapshots do not carry the production selector universe.  Keep
    # them auditable, but classify them explicitly before newer manifest
    # checks can turn the same condition into a generic input mismatch.
    if not isinstance(content.get("selection_scope"), dict):
        result["status"] = "scope_unverified"
        result["reason"] = "scope_unverified"
        return result
    if (not result["research_snapshot_sha256"] or
            result["research_snapshot_sha256"] != content_sha256(content) or
            not result["official_snapshot_sha256"] or
            result["official_snapshot_sha256"] != content_sha256(formal) or
            (content.get("official_snapshot") or {}).get("content_sha256") != result["official_snapshot_sha256"]):
        result["reason"] = "snapshot_hash_mismatch"
        return result
    manifest = content.get("input_manifest") or {}
    if (manifest.get("input_sha256") != content_sha256(manifest.get("inputs")) or
            (manifest.get("inputs") or {}).get("candidate_records") != content.get("records") or
            (manifest.get("inputs") or {}).get("selection_scope") != content.get("selection_scope")):
        result["reason"] = "input_manifest_mismatch"
        return result
    records, scope_error = _scope_records(content)
    if scope_error:
        result["status"] = scope_error
        result["reason"] = scope_error
        return result
    scope = content["selection_scope"]
    params = content.get("parameter_summary") or {}
    top, min_score = params.get("top"), _number(params.get("min_score"))
    if (isinstance(top, bool) or not isinstance(top, int) or top < 1 or
            min_score is None or scope.get("top") != top or
            _number(scope.get("min_score")) != min_score or
            (content.get("model_version") != formal.get("model_version") and
             not (content.get("model_version") == "daily-candidates/v5-news-shadow"
                  and formal.get("model_version") == "daily-candidates/v4")) or
            content.get("policy") != formal.get("policy")):
        result["status"] = "input_incomplete"
        result["reason"] = "model_or_parameter_mismatch"
        return result
    result["selection_scope_sha256"] = scope["codes_sha256"]
    result["selection_scope"] = copy.deepcopy(scope)
    result["input_sha256"] = content_sha256({
        "research": result["research_snapshot_sha256"],
        "official": result["official_snapshot_sha256"],
        "definition": result["definition"]})
    result["experiment_id"] = result["input_sha256"][:16]
    active = [row for row in records if row.get("final_status") != "phase2_filtered"]
    result["filtered_terminal_count"] = len(records) - len(active)
    result["scope_record_count"] = len(records)
    evidence, errors = {}, []
    for record in active:
        item, error = _reconstruct(record)
        if error:
            errors.append({"record_id": record.get("record_id"), "reason": error})
        else:
            evidence[record["record_id"]] = item
    result["invalid_records"] = errors
    result["comparable_record_count"] = len(evidence)
    result["invalid_record_count"] = len(errors)
    result["qualification_coverage"] = (
        len(evidence) / len(active) if active else 1.0
    )
    if errors:
        result["status"] = "input_incomplete"
        result["reason"] = "score_evidence_incomplete"
        return result
    bonuses = (content.get("parameter_summary") or {}).get("buy_point_priority_bonus")
    if bonuses is None:
        result["status"] = "input_incomplete"
        result["reason"] = "missing_priority_bonuses"
        return result
    baseline = _replay_selection(active, bonuses, top, min_score, content.get("policy"))
    if baseline["status"] != "ok":
        result["status"] = "input_incomplete"
        result["reason"] = ",".join(baseline["reasons"])
        return result
    baseline_codes = _codes(baseline["selected"])
    if (baseline_codes != _codes(formal.get("candidates")) or
            _bucket_codes(baseline["buckets"]) != _bucket_codes(formal.get("buckets"))):
        result["status"] = "baseline_mismatch"
        result["reason"] = "selection_or_bucket_mismatch"
        return result
    result["baseline"] = {"selected_codes": baseline_codes,
                          "buckets": _bucket_codes(baseline["buckets"]),
                          "top_k": _top_k(baseline["buckets"])}
    result["no_executable_recommendation_day"] = not bool(
        result["baseline"]["top_k"]["1"])
    record_ids = {str(row["code"]): str(row["record_id"]) for row in active}
    result["record_ids"] = record_ids
    result["bucket_counts"] = {name: len(rows) for name, rows in
                               baseline["buckets"].items()}
    for removed in WEIGHTS:
        selected, buckets = _rank(active, evidence, removed, top, min_score,
                                  content.get("policy") or {})
        label = f"remove_{removed}"
        treatment_top = _top_k(buckets)
        result["treatments"][label] = {
            "selected_codes": _codes(selected), "buckets": _bucket_codes(buckets),
            "top_k": treatment_top,
            "overlap": {key: len(set(values) & set(result["baseline"]["top_k"][key]))
                        for key, values in treatment_top.items()},
            "turnover": {key: round(1 - len(set(values) & set(result["baseline"]["top_k"][key])) /
                                   len(values), 4) if values else None
                         for key, values in treatment_top.items()}}
    result["status"] = "completed"
    return result


def _top_k(buckets):
    ordered = [row["code"] for name in RECOMMENDATION_BUCKETS
               for row in (buckets or {}).get(name, [])]
    return {str(k): ordered[:k] for k in TOP_K}


def _atomic_new(path, payload):
    """Write once so a rerun cannot mutate a previously reviewed result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("factor_ablation_artifact_conflict")
        return path
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    os.close(fd)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("factor_ablation_artifact_conflict")
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return path


def _persist(root, section, day, artifact):
    digest = artifact.get("input_sha256") or content_sha256(artifact)
    path = Path(root) / "factor_ablation" / section / day / f"{digest}.json"
    _atomic_new(path, canonical_json(artifact) + b"\n")
    return str(path)


def run_daily(as_of, state_root=EVOLUTION_ROOT, research_root=None,
              official_root=None):
    """Load the primary formal pair, calculate, and persist a daily artifact."""
    try:
        date.fromisoformat(str(as_of))
    except ValueError:
        return {"status": "skipped", "reason": "invalid_recommendation_date"}
    state_root = Path(state_root)
    research_root = (Path(research_root) if research_root is not None
                     else state_root / "research")
    official_root = Path(official_root) if official_root is not None else OFFICIAL_ROOT
    index = research_root / str(as_of) / "formal" / "primary.json"
    try:
        run_id = index.read_text(encoding="utf-8").strip()
        research_path = index.parent / (run_id + ".json")
        research = load_research_snapshot(research_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        research = None
    if research is None:
        return {"status": "skipped", "reason": "research_snapshot_missing"}
    official_path = official_root / f"{as_of}.json"
    if not official_path.is_file():
        return {"status": "skipped", "reason": "official_snapshot_missing"}
    try:
        artifact = run_daily_ablation(research, load_official_snapshot(official_path))
        path = _persist(state_root, "daily", as_of, artifact)
        return {"status": artifact["status"], "reason": artifact.get("reason"),
                "path": path, "input_sha256": artifact.get("input_sha256")}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "failed", "reason": type(exc).__name__}


def _outcome_index(items, cutoff, contract_id):
    indexed, conflicts = {}, set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("point_in_time_status") in ("evaluation_conflict", "legacy_unverified"):
            continue
        if item.get("conflicts"):
            continue
        if item.get("evaluation_as_of") is None or str(item["evaluation_as_of"])[:10] > cutoff:
            continue
        item_contract = item.get("contract_id") or (
            item.get("evaluation_contract") or {}).get("contract_id")
        if item_contract != contract_id:
            continue
        key = (item.get("record_id"), item.get("research_snapshot_sha256"), item_contract)
        if not all(key):
            continue
        if key in indexed:
            previous = indexed[key]
            previous_as_of = str(previous.get("evaluation_as_of"))[:10]
            current_as_of = str(item.get("evaluation_as_of"))[:10]
            if current_as_of > previous_as_of:
                indexed[key] = item
            elif current_as_of == previous_as_of:
                conflicts.add(key)
        else:
            indexed[key] = item
    for key in conflicts:
        indexed.pop(key, None)
    return indexed, len(conflicts)


def _complete_alpha(day, codes, mapping, snapshot_hash, index, contract_id, window):
    if not codes:
        return None, "no_recommendations"
    values = []
    for code in codes:
        record_id = mapping.get(code)
        outcome = index.get((record_id, snapshot_hash, contract_id))
        if outcome is None or outcome.get("recommendation_date") != day:
            return None, "outcome_missing_or_identity_conflict"
        result = (outcome.get("windows") or {}).get(str(window)) or {}
        alpha = _number(result.get("hs300_alpha"))
        if result.get("status") != "complete" or alpha is None:
            return None, "outcome_pending_or_invalid_alpha"
        values.append(alpha)
    return sum(values) / len(values), None


def evaluate_ablation_days(daily_artifacts, outcome_items, cutoff,
                           contract_id=DEFAULT_CONTRACT_ID):
    """Compare dates only when both Top-K sides have complete finite alpha."""
    index, conflicts = _outcome_index(outcome_items, cutoff, contract_id)
    by_day = {}
    for item in daily_artifacts:
        if item.get("status") != "completed" or item.get("recommendation_date", "") > cutoff:
            continue
        if (item.get("definition") or {}).get("evaluation_contract_id") != contract_id:
            continue
        by_day.setdefault(item.get("recommendation_date"), []).append(item)
    duplicate_days = sorted(day for day, rows in by_day.items() if len(rows) != 1)
    daily = [rows[0] for day, rows in sorted(by_day.items()) if len(rows) == 1]
    result = {"schema_version": SCHEMA_VERSION, "evaluation_as_of": cutoff,
              "evaluation_contract_id": contract_id,
              "daily_count": len(daily), "duplicate_daily_dates": duplicate_days,
              "duplicate_outcome_identities": conflicts,
              "comparisons": {}, "status": "continue_accumulating"}
    for label in definition()["treatments"]:
        by_k = {}
        for window in (5, 20, 60):
            window_results = {}
            for k in TOP_K:
                pairs, missing = [], {}
                for item in daily:
                    day = item["recommendation_date"]
                    mapping = item.get("record_ids") or {}
                    snapshot_hash = item.get("research_snapshot_sha256")
                    baseline, baseline_reason = _complete_alpha(
                        day, item["baseline"]["top_k"][str(k)], mapping,
                        snapshot_hash, index, contract_id, window)
                    treatment, treatment_reason = _complete_alpha(
                        day, item["treatments"][label]["top_k"][str(k)], mapping,
                        snapshot_hash, index, contract_id, window)
                    if baseline_reason or treatment_reason:
                        reason = baseline_reason or treatment_reason
                        missing[reason] = missing.get(reason, 0) + 1
                    else:
                        pairs.append({"date": day, "baseline_alpha": baseline,
                                      "treatment_alpha": treatment,
                                      "delta": treatment - baseline})
                window_results[str(k)] = {
                    "paired_dates": len(pairs), "missing": missing,
                    "coverage_rate": (len(pairs) / len(daily) if daily else None),
                    "date_pairs": pairs,
                    "mean_delta": (sum(pair["delta"] for pair in pairs) / len(pairs)
                                   if pairs else None),
                    "win_rate_delta": (
                        sum(pair["treatment_alpha"] > 0 for pair in pairs) / len(pairs)
                        - sum(pair["baseline_alpha"] > 0 for pair in pairs) / len(pairs)
                        if pairs else None),
                }
            by_k[str(window)] = window_results
        # Keep the mature 20-day window directly addressable while retaining
        # the 5/20/60-day audit detail under ``windows``.
        result["comparisons"][label] = {
            **by_k.get("20", {}),
            "20": by_k.get("20", {}),
            "windows": by_k,
        }
    event_rows = []
    selected_keys = set()
    for item in daily:
        mapping = item.get("record_ids") or {}
        codes = set(item.get("baseline", {}).get("top_k", {}).get("5", []))
        for treatment in (item.get("treatments") or {}).values():
            codes.update(treatment.get("top_k", {}).get("5", []))
        for code in codes:
            selected_keys.add((mapping.get(code), item.get("research_snapshot_sha256"),
                               contract_id))
    for key in selected_keys:
        item = index.get(key)
        if not item:
            continue
        window = (item.get("windows") or {}).get("20") or {}
        if window.get("status") != "complete" or _number(window.get("hs300_alpha")) is None:
            continue
        event_rows.append({"record_id": item.get("record_id"),
                           "market": item.get("market") or item.get("exchange") or "default",
                           "code": item.get("code"),
                           "recommendation_date": item.get("recommendation_date"),
                           "entry_date": window.get("entry_date"),
                           "exit_date": window.get("exit_date") or window.get("mark_date"),
                           "market_sessions": item.get("market_sessions")})
    events = assign_research_events(event_rows, 20)
    result["mature_unique_alpha_events"] = len(events["events"])
    result["invalid_event_count"] = len(events["invalid"])
    paired_by_comparison = [
        comparison["windows"]["20"]["5"]["paired_dates"]
        for comparison in result["comparisons"].values()
    ]
    # A conclusion needs the same mature-date denominator for every removal;
    # one well-covered treatment cannot mask a sparse comparison.
    mature_dates = min(paired_by_comparison) if paired_by_comparison else 0
    result["mature_paired_dates"] = mature_dates
    if (mature_dates >= definition()["minimum_mature_dates"] and
            result["mature_unique_alpha_events"] >=
            definition()["minimum_unique_alpha_events"]):
        result["status"] = "completed"
        result["conclusion_scope"] = "research_diagnostics_only_holdout_pending"
    return result


def run_evaluation(as_of, state_root=EVOLUTION_ROOT, outcome_root=None,
                   contract_id=None):
    """Persist the current weekly research diagnosis from existing artifacts."""
    try:
        week = date.fromisoformat(str(as_of)).isocalendar()
    except ValueError:
        return {"status": "skipped", "reason": "invalid_evaluation_date"}
    chosen_contract = contract_id or DEFAULT_CONTRACT_ID
    daily_paths = sorted((Path(state_root) / "factor_ablation" / "daily").glob("*/*.json"))
    daily = []
    for path in daily_paths:
        try:
            daily.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if outcome_root is None:
        outcome_root = Path(state_root) / "evaluations"
    try:
        outcomes = load_candidate_signal_items(outcome_root, contract_id=chosen_contract,
                                               as_of=as_of)
        artifact = evaluate_ablation_days(daily, outcomes, as_of, chosen_contract)
        artifact["input_sha256"] = content_sha256({
            "daily": sorted(item.get("input_sha256") for item in daily
                            if item.get("input_sha256")),
            "outcomes": outcomes, "cutoff": as_of, "contract_id": chosen_contract})
        week_label = f"{week.year}-W{week.week:02d}"
        path = _persist(state_root, "weekly", week_label, artifact)
        return {"status": artifact["status"], "path": path,
                "input_sha256": artifact["input_sha256"]}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "failed", "reason": type(exc).__name__}
