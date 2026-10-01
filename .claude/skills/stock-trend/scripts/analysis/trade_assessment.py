#!/usr/bin/env python3
"""Independent offline LPS plans and versioned single-opportunity simulations."""
import argparse
import copy
import json
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from analysis.lps_distance_research import _atomic_new, run_daily_distance
from analysis.recommendation_diagnostics import load_primary_research_snapshots
from analysis.open_only_trade_simulation import build_contract, simulate_trade
from core.evolution_storage import EVOLUTION_ROOT, input_manifest
from core.lps_trade_assessment import build_lps_trade_assessment
from core.recommendation_snapshot import DEFAULT_ROOT, canonical_json, content_sha256, load_official_snapshot
from reporting.trade_assessment_report import render_html, render_markdown

SCHEMA_VERSION = "independent-trade-assessment/v1"
CONTEXT_VERSION = "trade-assessment-context/v1"


def assess_snapshot(research, official, contexts, cutoff):
    """Validate the entire frozen population before preparing independent plans."""
    replay = run_daily_distance(research, official)
    content = research.get("content") or {}
    result = {"recommendation_date": content.get("recommendation_date"),
              "research_snapshot_sha256": research.get("content_sha256"),
              "official_snapshot_sha256": official.get("content_sha256"),
              "status": replay.get("status"), "reason": replay.get("reason"), "items": []}
    if replay.get("status") != "completed":
        return result
    for record in content.get("records") or []:
        evidence = next((r for r in replay.get("records", [])
                         if r.get("record_id") == record.get("record_id")), {})
        if not evidence.get("is_lps"):
            continue
        key = f"{record.get('record_id')}@{research.get('content_sha256')}"
        supplied = contexts.get(key) or {}
        context = copy.deepcopy(supplied.get("decision_context") or {})
        frozen_record = copy.deepcopy(record)
        frozen_record["research_snapshot_sha256"] = research.get("content_sha256")
        item = build_lps_trade_assessment(frozen_record, content.get("policy") or {},
                                         content["recommendation_date"], context)
        item.update(opportunity_id=key, research_snapshot_sha256=research.get("content_sha256"))
        item["simulations"] = []
        # Every frozen opportunity is retained in all four contracts, even when
        # planning or subsequent market evidence is insufficient.
        for window in (20, 60):
            for scenario in ("reference", "stress"):
                simulation = simulate_trade(item.get("plan") or {},
                                            supplied.get("market_data") or {}, cutoff,
                                            window=window, cost_scenario=scenario)
                simulation.update(opportunity_id=key, window=window, cost_scenario=scenario)
                item["simulations"].append(simulation)
        result["items"].append(item)
    return result


def summarize(daily):
    items = [item for day in daily for item in day.get("items", [])]
    groups = []
    for window in (20, 60):
        for scenario in ("reference", "stress"):
            rows = [r for item in items for r in item.get("simulations", [])
                    if r["window"] == window and r["cost_scenario"] == scenario]
            completed = [r for r in rows if r.get("opportunity_status") == "completed"]
            filled = [r for r in rows if (r.get("cash_flows") or {}).get("invested_cash_cny")]
            unknown_fill = [r for r in rows if r.get("opportunity_status") in {"pending", "data_error"}
                            and not (r.get("cash_flows") or {}).get("invested_cash_cny")]
            net = [r["returns"]["net_return"] for r in completed
                   if (r.get("returns") or {}).get("net_return") is not None]
            alpha = [r["benchmark"]["actual_exit"]["net_excess_return"] for r in completed
                     if ((r.get("benchmark") or {}).get("actual_exit") or {}).get("net_excess_return") is not None]
            mae = [(r.get("risk") or {}).get("conservative_mae") for r in filled
                   if (r.get("risk") or {}).get("conservative_mae") is not None]
            stopped = [r for r in completed if str((r.get("execution") or {}).get("exit_reason", "")).startswith("stop")]
            delayed = [r for r in filled if (r.get("execution") or {}).get("delayed_exit")]
            groups.append({"window": window, "cost_scenario": scenario,
                           "opportunities": len(rows),
                           "status_counts": dict(Counter(r.get("opportunity_status", "unknown") for r in rows)),
                           "filled_opportunities": len(filled),
                           "fill_rate": len(filled) / len(rows) if rows and not unknown_fill else None,
                           "fill_rate_denominator": len(rows),
                           "fill_unknown_opportunities": len(unknown_fill),
                           "execution_data_coverage": (len(rows) - len(unknown_fill)) / len(rows) if rows else None,
                           "completed_trades": len(completed), "net_return_samples": len(net),
                           "unfilled_reason_counts": dict(Counter((r.get("execution") or {}).get("reason", "unknown")
                                                                  for r in rows if r.get("opportunity_status") == "not_filled")),
                           "stop_rate": len(stopped) / len(completed) if completed else None,
                           "stop_rate_denominator": len(completed),
                           "delayed_exit_rate": len(delayed) / len(filled) if filled else None,
                           "delayed_exit_rate_denominator": len(filled),
                           "net_excess_return_mean": statistics.mean(alpha) if alpha else None,
                           "net_excess_return_samples": len(alpha),
                           "conservative_mae_mean": statistics.mean(mae) if mae else None,
                           "conservative_mae_samples": len(mae),
                           "net_return_mean": statistics.mean(net) if net else None,
                           "net_return_median": statistics.median(net) if net else None,
                           "real_account_net_return": None})
    return {"opportunities": len(items),
            "population_kind": "all_frozen_lps_descriptive_opportunities",
            "formal_bucket_counts": dict(Counter(item.get("formal_bucket", "unknown") for item in items)),
            "plan_status_counts": dict(Counter(item.get("status", "unknown") for item in items)),
            "contracts": groups, "strategy_upgrade_allowed": False,
            "portfolio_backtest": False,
            "overlapping_opportunities": "independent_single_trade_not_additive"}


def save_artifact(artifact, root=None, report_root=None):
    """Create immutable, content-addressed outputs; never overwrite history."""
    digest = artifact["input_sha256"]
    day = artifact["evaluation_as_of"]
    cache = Path(root) if root else EVOLUTION_ROOT / "shadow" / "trade_assessment"
    reports = Path(report_root) if report_root else SCRIPT_ROOT.parents[3] / "reports"
    name = f"trade-assessment-{day}-{digest}"
    json_path = cache / day / f"{digest}.json"
    _atomic_new(json_path, canonical_json(artifact) + b"\n")
    paths = {"cache_json": str(json_path)}
    for extension, payload in (("json", canonical_json(artifact) + b"\n"),
                               ("md", render_markdown(artifact).encode("utf-8")),
                               ("html", render_html(artifact).encode("utf-8"))):
        path = reports / f"{name}.{extension}"
        _atomic_new(path, payload)
        paths[extension] = str(path)
    return paths


def run(as_of, contexts=None, state_root=EVOLUTION_ROOT, official_root=DEFAULT_ROOT,
        save=False, report_root=None):
    cutoff = date.fromisoformat(as_of).isoformat()
    bundle = contexts or {"schema_version": CONTEXT_VERSION, "opportunities": {}}
    if bundle.get("schema_version") != CONTEXT_VERSION or not isinstance(bundle.get("opportunities"), dict):
        raise ValueError("trade_assessment_context_schema_invalid")
    research = load_primary_research_snapshots(Path(state_root) / "research", as_of=cutoff)
    # This command prepares the latest available frozen recommendation only.
    # Historical multi-day cohorts are a separate forward-validation task.
    snapshots = research[-1:]
    daily, officials = [], []
    for snapshot in snapshots:
        day = snapshot["content"]["recommendation_date"]
        path = Path(official_root) / f"{day}.json"
        if not path.is_file():
            daily.append({"recommendation_date": day, "status": "skipped",
                          "reason": "official_snapshot_missing", "items": []})
            continue
        official = load_official_snapshot(path)
        officials.append(official)
        daily.append(assess_snapshot(snapshot, official, bundle["opportunities"], cutoff))
    # Originals are immutable, hash-validated archives. Retain their identities
    # without duplicating the entire scan population in every standalone report.
    research_refs = [{"recommendation_date": s["content"]["recommendation_date"],
                      "run_id": s.get("run_id"), "content_sha256": s.get("content_sha256")}
                     for s in snapshots]
    official_refs = [{"recommendation_date": s["content"]["recommendation_date"],
                      "content_sha256": s.get("content_sha256")} for s in officials]
    implementation = {str(path.relative_to(SCRIPT_ROOT)): content_sha256(path.read_text(encoding="utf-8"))
                      for path in (Path(__file__), SCRIPT_ROOT / "core" / "lps_trade_assessment.py",
                                   SCRIPT_ROOT / "analysis" / "open_only_trade_simulation.py",
                                   SCRIPT_ROOT / "reporting" / "trade_assessment_report.py")}
    manifest = input_manifest(research_snapshots=research_refs, official_snapshots=official_refs,
                              implementation=implementation,
                              context_bundle=bundle, evaluation_as_of=cutoff,
                              contracts=[build_contract(s) for s in ("reference", "stress")])
    artifact = {"schema_version": SCHEMA_VERSION, "evaluation_as_of": cutoff,
                "status": "completed" if any(d.get("status") == "completed" for d in daily) else "data_insufficient",
                "recommendation_date": snapshots[-1]["content"]["recommendation_date"] if snapshots else None,
                "basis_date": snapshots[-1]["content"]["recommendation_date"] if snapshots else None,
                "daily": daily, "items": [item for d in daily for item in d.get("items", [])],
                "summary": summarize(daily), "input_manifest": manifest,
                "input_sha256": manifest["input_sha256"], "formal_policy_changed": False,
                "scope": "latest_frozen_recommendation_independent_single_opportunity",
                "disclaimer": "本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。"}
    if save:
        artifact["report_paths"] = save_artifact(artifact, Path(state_root) / "shadow" / "trade_assessment", report_root)
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    contexts = json.loads(args.context_file.read_text(encoding="utf-8")) if args.context_file else None
    result = run(args.as_of, contexts, save=args.save)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
