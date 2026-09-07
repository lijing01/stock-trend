"""Bounded, evidence-checked proposal handling for recommendation research."""
import copy
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.recommendation_snapshot import canonical_json, content_sha256


SCHEMA_VERSION = "recommendation-evolution-proposal/v1"
DEFAULT_ROOT = Path(CACHE_DIR) / "evolution" / "proposals"
MAX_WEEKLY_PROPOSALS = 3
ALLOWED_VARIABLE = "buy_point_priority_bonus"
ALLOWED_LEVELS = {"strict_level_1", "strict_level_2", "strict_level_3"}


def _week_key(value=None):
    return (value or date.today()).isocalendar()


def proposal_schema():
    """A JSON-schema-shaped contract usable by an external model adapter."""
    return {"type": "object", "required": ["proposals"], "properties": {"proposals": {
        "type": "array", "maxItems": MAX_WEEKLY_PROPOSALS, "items": {"type": "object",
        "required": ["hypothesis", "evidence_ids", "counterexample", "change", "expected_direction", "failure_condition", "validation_plan"]}}}}


def research_material(diagnostics):
    content = diagnostics.get("content", diagnostics)
    return {"diagnostic_id": diagnostics.get("diagnostic_id"),
            "content_sha256": diagnostics.get("content_sha256"),
            "schema": proposal_schema(), "overall": content.get("overall"),
            "dimensions": content.get("dimensions"),
            "instruction": "只基于 evidence_ids 提案；样本不足时返回 proposals=[]，不得声称因果或直接改参数。"}


def validate_proposals(diagnostics, response):
    content = diagnostics.get("content", diagnostics)
    evidence = content.get("evidence_index") or {}
    proposals = (response or {}).get("proposals")
    if not isinstance(proposals, list):
        return [], ["proposals_not_list"]
    errors, valid = [], []
    if len(proposals) > MAX_WEEKLY_PROPOSALS:
        return [], ["too_many_proposals"]
    for number, proposal in enumerate(proposals):
        prefix = f"proposal_{number}"
        if not isinstance(proposal, dict):
            errors.append(prefix + ":not_object"); continue
        required = ("hypothesis", "evidence_ids", "counterexample", "change", "expected_direction", "failure_condition", "validation_plan")
        if any(not proposal.get(field) for field in required):
            errors.append(prefix + ":missing_required_field"); continue
        ids = proposal["evidence_ids"]
        if not isinstance(ids, list) or not ids or any(item not in evidence for item in ids):
            errors.append(prefix + ":invalid_evidence_reference"); continue
        if any(not evidence[item].get("proposal_eligible") for item in ids):
            errors.append(prefix + ":insufficient_group_sample"); continue
        change = proposal["change"]
        if not isinstance(change, dict) or change.get("variable") != ALLOWED_VARIABLE:
            errors.append(prefix + ":parameter_out_of_scope"); continue
        values = change.get("values")
        if not isinstance(values, dict) or set(values) - ALLOWED_LEVELS or any(
                not isinstance(value, (int, float)) or value < 0 or value > 3 for value in values.values()):
            errors.append(prefix + ":parameter_out_of_bounds"); continue
        valid.append(copy.deepcopy(proposal))
    return valid, errors


def build_proposal_run(diagnostics, response=None, week=None, adapter_status="model_unavailable", usage=None):
    """Create an idempotent weekly proposal record without invoking a model.

    An orchestration layer may supply one adapter response and one format repair;
    both calls and their token usage belong in ``usage`` for auditability.
    """
    content = diagnostics.get("content", diagnostics)
    overall = content.get("overall") or {}
    # The 30-event group floor prevents noisy slice-level stories.  The P0
    # research gate remains stricter: no weekly proposal before 20 mature
    # dates and 100 mature primary-window events overall.
    ready = (overall.get("mature_events", 0) >= 100 and
             overall.get("mature_dates", 0) >= 20)
    valid, errors = validate_proposals(diagnostics, response) if response else ([], [])
    status = "proposed" if valid else ("continue_accumulating" if not ready else "no_valid_proposal")
    run_content = {"schema_version": SCHEMA_VERSION, "week": list(_week_key(week)),
                   "diagnostic_id": diagnostics.get("diagnostic_id"),
                   "diagnostic_sha256": diagnostics.get("content_sha256"), "status": status,
                   "adapter_status": adapter_status, "usage": copy.deepcopy(usage or {"generation_calls": 0, "repair_calls": 0}),
                   "proposals": valid, "rejected": errors,
                   "policy": "每周最多3条；只生成假设，不修改参数、不自动发布。"}
    return {"schema_version": SCHEMA_VERSION, "proposal_id": content_sha256(run_content)[:16],
            "content_sha256": content_sha256(run_content), "content": run_content}


def generate_weekly_proposals(diagnostics, adapter=None, week=None):
    """Use an optional adapter once, with at most one format-only repair.

    ``adapter`` can be a callable or an object exposing ``generate`` and,
    optionally, ``repair``.  Provider failures deliberately degrade to a
    persisted no-proposal result so diagnostics remain useful offline.
    """
    if adapter is None:
        return build_proposal_run(diagnostics, week=week)
    material = research_material(diagnostics)
    usage = {"generation_calls": 1, "repair_calls": 0}
    try:
        generate = getattr(adapter, "generate", adapter)
        response = generate(material)
        valid, errors = validate_proposals(diagnostics, response)
        if not valid and errors and callable(getattr(adapter, "repair", None)):
            usage["repair_calls"] = 1
            response = adapter.repair(material, response, errors)
        return build_proposal_run(diagnostics, response, week=week,
                                  adapter_status="completed", usage=usage)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return build_proposal_run(diagnostics, week=week,
                                  adapter_status="model_failed:" + type(exc).__name__, usage=usage)


def save_proposal_run(run, root=DEFAULT_ROOT):
    week = run["content"]["week"]
    path = Path(root) / f"{week[0]}-W{week[1]:02d}" / (run["proposal_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(run) + b"\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    try:
        os.link(temporary, path)
        status = "created"
    except FileExistsError:
        status = "unchanged"
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": status, "path": str(path), "proposal_id": run["proposal_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate bounded evolution proposals")
    parser.add_argument("--diagnostics", required=True)
    parser.add_argument("--response", help="Optional model JSON response; omitted means offline accumulation")
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    diagnostics = json.loads(Path(args.diagnostics).read_text(encoding="utf-8"))
    response = json.loads(Path(args.response).read_text(encoding="utf-8")) if args.response else None
    run = build_proposal_run(diagnostics, response)
    if args.save:
        run["tracking"] = save_proposal_run(run, args.output_root)
    print(json.dumps(run, ensure_ascii=False, sort_keys=True) if args.json else run["content"]["status"])
    return 0


if __name__ == "__main__":
    main()
