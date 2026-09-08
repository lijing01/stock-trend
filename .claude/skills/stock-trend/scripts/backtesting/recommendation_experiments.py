"""Walk-forward, paired shadow evaluation for frozen candidate experiments."""
import argparse
import copy
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path: sys.path.insert(0, str(SCRIPT_ROOT))

from analysis.recommendation_diagnostics import load_candidate_signal_items, load_primary_research_snapshots
from core.recommendation_snapshot import canonical_json, content_sha256
from core.evolution_registry import validate_experiment_definition
from core.evolution_storage import input_manifest, storage_root
from core.research_events import assign_research_events, summarize_daily_alpha

SCHEMA_VERSION = "recommendation-experiment/v1"
DEFAULT_ROOT = storage_root("experiments")
PRIMARY_WINDOW = 20
FROZEN_BASELINE = {"strict_level_1": 1, "strict_level_2": 3, "strict_level_3": 2}
ZERO_TREATMENT = {"strict_level_1": 0, "strict_level_2": 0, "strict_level_3": 0}


def default_experiment():
    return {"kind": "buy_point_priority_bonus", "baseline": FROZEN_BASELINE,
            "treatment": ZERO_TREATMENT, "changes": ["within_bucket_ranking"]}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value); return value if math.isfinite(value) else None
    except (TypeError, ValueError): return None


def _outcomes(items):
    result = {}
    for item in items or []:
        window = (item.get("windows") or {}).get(str(PRIMARY_WINDOW), {})
        value = copy.deepcopy(window)
        value["_market_sessions"] = item.get("market_sessions")
        value["_record_id"] = item.get("record_id")
        result[(str(item.get("recommendation_date") or ""), str(item.get("code") or ""))] = value
    return result


def _eligible(record):
    candidate = record.get("candidate") or {}
    quality = candidate.get("data_quality") or {}
    return (quality.get("eligible") is True and candidate.get("sector_actionable", True)
            and candidate.get("score_eligible", True)
            and not candidate.get("research_terminal_status"))


def _replay_date(records, bonuses, limit):
    """Re-rank frozen eligible inputs only; never relax an eligibility gate."""
    rows = []
    for record in records:
        if not _eligible(record): continue
        candidate = record.get("candidate") or {}; level = str((record.get("scores") or {}).get("buy_point_level") or candidate.get("buy_point_level") or "")
        quality = _number((record.get("scores") or {}).get("quality_adjusted_score"))
        if quality is None: quality = _number(candidate.get("quality_adjusted_score"))
        if quality is None: continue
        rows.append((min(100.0, quality + float(bonuses.get(level, 0))), str(record.get("code")), record))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in rows[:limit]]


def _bootstrap(deltas, seed=20260907, draws=2000):
    if not deltas: return None
    rng = random.Random(seed); values = list(deltas); means = []
    for _ in range(draws): means.append(sum(rng.choice(values) for _ in values) / len(values))
    means.sort(); return {"method": "date_block_bootstrap", "seed": seed, "draws": draws,
                          "lower_95": means[int(.025 * (draws - 1))], "upper_95": means[int(.975 * (draws - 1))]}


def _percentile(values, q):
    values = sorted(values)
    if not values: return None
    return values[max(0, min(len(values) - 1, int(q * (len(values) - 1))))]


def _split_dates(dates, max_window=PRIMARY_WINDOW):
    """Three ordered OOS blocks; leave one max-window-sized purge gap."""
    ordered = sorted(set(dates)); gap = int(max_window)
    usable = len(ordered) - 2 * gap
    if usable < 3: return None
    step = usable // 3
    if step < 1: return None
    blocks = []
    start = 0
    for index in range(3):
        end = start + step if index < 2 else start + (usable - step * 2)
        blocks.append(ordered[start:end]); start = end + gap
    return blocks


def run_walk_forward(research_snapshots, candidate_signal_items, definition=None, top=None):
    definition = validate_experiment_definition(definition or default_experiment())
    by_date = defaultdict(list); skipped = []
    for snapshot in research_snapshots or []:
        content = snapshot.get("content", snapshot) if isinstance(snapshot, dict) else {}
        if content.get("snapshot_type") != "formal" or (content.get("official_snapshot") or {}).get("link_status") != "linked": continue
        limit = (content.get("parameter_summary") or {}).get("top") if top is None else top
        if not isinstance(limit, int) or limit < 1:
            skipped.append("replay_input_missing_top"); continue
        for record in content.get("records") or []:
            if not isinstance(record, dict) or not record.get("code") or not isinstance(record.get("candidate"), dict):
                skipped.append("replay_input_missing_candidate"); continue
            by_date[str(content.get("recommendation_date") or record.get("basis_date") or "")].append(record)
    outcomes = _outcomes(candidate_signal_items); dates = sorted(day for day in by_date if day)
    blocks = _split_dates(dates)
    content = {"schema_version": SCHEMA_VERSION, "definition": definition,
               "primary_window": PRIMARY_WINDOW, "purge_sessions": PRIMARY_WINDOW,
               "input_dates": dates, "skipped_reasons": sorted(set(skipped)),
               "input_manifest": input_manifest(
                   research_run_ids=sorted(str((item or {}).get("run_id") or "") for item in research_snapshots or []),
                   research_content_sha256=sorted(str((item or {}).get("content_sha256") or "") for item in research_snapshots or []),
                   candidate_signal_items=candidate_signal_items or [],
                   definition=definition,
                   primary_window=PRIMARY_WINDOW,
               )}
    if not blocks:
        content.update({"status": "continue_accumulating", "reason": "insufficient_time_span_for_three_purged_oos_blocks"})
        return _package(content)
    paired, coverage, baseline_maes, treatment_maes = [], [], [], []
    mature_event_records = []
    daily_alpha_rows = []
    for block_no, block in enumerate(blocks, 1):
        for day in block:
            limit = top if top is not None else (next((s.get("content", s).get("parameter_summary", {}).get("top") for s in research_snapshots if str(s.get("content", s).get("recommendation_date")) == day), None))
            base = _replay_date(by_date[day], definition["baseline"], limit); trial = _replay_date(by_date[day], definition["treatment"], limit)
            def metrics(rows, side):
                values, maes = [], []
                for row in rows:
                    outcome = outcomes.get((day, str(row.get("code")))) or {}
                    # A forward label that crosses this OOS block's end would
                    # leak its future realization into the next time split.
                    exit_date = str(outcome.get("exit_date") or "")
                    if not exit_date or exit_date > block[-1]:
                        skipped.append("label_crosses_oos_boundary")
                        continue
                    alpha = _number(outcome.get("hs300_alpha")); mae = _number(outcome.get("mae"))
                    if outcome.get("status") == "complete" and alpha is not None:
                        values.append(alpha)
                        daily_alpha_rows.append({"recommendation_date": day,
                            "hs300_alpha": alpha, "evaluation_status": "complete"})
                        if outcome.get("entry_date") and outcome.get("exit_date"):
                            mature_event_records.append({
                                "record_id": outcome.get("_record_id") or f"{day}:{outcome.get('market', 'default')}:{row.get('code')}",
                                "market": outcome.get("market") or "default",
                                "code": str(row.get("code")),
                                "entry_date": outcome.get("entry_date"),
                                "exit_date": outcome.get("exit_date"),
                                "market_sessions": outcome.get("_market_sessions"),
                            })
                    if outcome.get("status") == "complete" and mae is not None: maes.append(mae)
                return (sum(values) / len(values) if values else None), maes
            old, old_maes = metrics(base, "baseline"); new, new_maes = metrics(trial, "treatment")
            baseline_maes.extend(old_maes); treatment_maes.extend(new_maes)
            coverage.append({"date": day, "baseline_count": len(base), "treatment_count": len(trial)})
            if old is not None and new is not None: paired.append({"date": day, "block": block_no, "delta": new - old})
    deltas = [row["delta"] for row in paired]; interval = _bootstrap(deltas)
    baseline_coverage = sum(row["baseline_count"] > 0 for row in coverage); treatment_coverage = sum(row["treatment_count"] > 0 for row in coverage)
    baseline_tail, treatment_tail = _percentile(baseline_maes, .05), _percentile(treatment_maes, .05)
    assigned_events = assign_research_events(mature_event_records, PRIMARY_WINDOW)
    daily_alpha = summarize_daily_alpha(daily_alpha_rows)
    maturity = {"dedup_mature_events": len(assigned_events["events"]),
                "mature_dates": len({row["date"] for row in paired}),
                "valid_alpha_events": len(assigned_events["events"]),
                "alpha_mature_dates": daily_alpha["mature_dates"],
                "alpha_mean": daily_alpha["mean_alpha"],
                "daily_alpha": daily_alpha["daily"],
                "alpha_missing_records": daily_alpha["missing_records"]}
    gates = {"minimum_events": maturity["dedup_mature_events"] >= 100,
             "minimum_dates": maturity["mature_dates"] >= 20,
             "three_oos_blocks": len(blocks) >= 3,
             "coverage": treatment_coverage >= .9 * baseline_coverage,
             "ci_lower_positive": bool(interval and interval["lower_95"] > 0),
             "mae_tail": bool(baseline_tail is not None and treatment_tail is not None and treatment_tail >= baseline_tail - .01)}
    status = "validated" if len(paired) >= 3 else "continue_accumulating"
    content.update({"status": status, "oos_blocks": blocks, "paired_dates": paired,
                    "coverage": {"baseline_dates": baseline_coverage, "treatment_dates": treatment_coverage,
                    "ratio": treatment_coverage / baseline_coverage if baseline_coverage else None}, "interval": interval,
                    "maturity": maturity, "mae_tail_5pct": {"baseline": baseline_tail, "treatment": treatment_tail},
                    "promotion": {"eligible": all(gates.values()), "gates": gates,
                                  "reason": "frozen_p3_promotion_gates"}})
    return _package(content)


def _package(content):
    return {"experiment_id": content_sha256(content)[:16], "content_sha256": content_sha256(content), "content": content}


def save_experiment(result, root=DEFAULT_ROOT):
    root = Path(root); root.mkdir(parents=True, exist_ok=True); path = root / (result["experiment_id"] + ".json")
    if path.exists(): return {"status": "unchanged", "path": str(path), "experiment_id": result["experiment_id"]}
    temporary = path.with_suffix(".tmp"); temporary.write_bytes(canonical_json(result) + b"\n")
    try: __import__("os").link(temporary, path)
    except FileExistsError: pass
    finally: temporary.unlink(missing_ok=True)
    return {"status": "created", "path": str(path), "experiment_id": result["experiment_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run frozen recommendation walk-forward experiment")
    parser.add_argument("--research-root", default=str(storage_root("research"))); parser.add_argument("--attribution-root", default=str(storage_root("evaluations"))); parser.add_argument("--contract-id"); parser.add_argument("--save", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv); result = run_walk_forward(load_primary_research_snapshots(args.research_root), load_candidate_signal_items(args.attribution_root, args.contract_id))
    if args.save: result["persistence"] = save_experiment(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
