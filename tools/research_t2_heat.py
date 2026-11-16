#!/usr/bin/env python3
"""Offline walk-forward study of a fixed T2 MA20 heat filter.

This tool does not modify production. For every evaluable month it trains the
frozen V4 model on all mature T1/T2 records before that month, then compares
the original daily T2 Top-N with a second arm that first requires
``close_to_ma20 <= 0.05`` and ranks the survivors with the same model.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "stock-trend" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from analysis.weak_to_strong_v4.train import (  # noqa: E402
    _boundaries, _calendar, _eligible_record, _training_record,
)
from analysis.weak_to_strong_v4.v4_shadow_scoring import (  # noqa: E402
    fit_v4_shadow_bundle, score_v4_shadow_record, validate_v4_shadow_bundle,
)


SCHEMA_VERSION = "t2-heat-research/v1"
HEAT_THRESHOLD = 0.05
DEFAULT_DRAWS = 2000
DEFAULT_SEED = 20261116
DEFAULT_BLOCK_LENGTH = 20


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def feature(record: Mapping[str, object], name: str) -> object:
    if record.get(name) is not None:
        return record[name]
    block = record.get("features")
    if isinstance(block, Mapping):
        if block.get(name) is not None:
            return block[name]
        nested = block.get("features")
        if isinstance(nested, Mapping):
            return nested.get(name)
    return None


def passes_heat_filter(record: Mapping[str, object]) -> bool:
    value = _finite(feature(record, "close_to_ma20"))
    return value is not None and value <= HEAT_THRESHOLD


def _identity(record: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(record.get("symbol") or ""), str(record.get("cycle_id") or ""),
        str(record.get("event_id") or ""),
    )


def v4_rank_key(record: Mapping[str, object]) -> tuple[float, float, tuple[str, str, str]]:
    return (
        -float(record["research_expected_net_return"]),
        -float(record["research_estimated_win_rate"]), _identity(record),
    )


def select_daily(
    records: Sequence[Mapping[str, object]], cap: int, *, heat_filter: bool,
) -> list[Mapping[str, object]]:
    by_day: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        if not heat_filter or passes_heat_filter(record):
            by_day[str(record["signal_date"])].append(record)
    return [
        record
        for day in sorted(by_day)
        for record in sorted(by_day[day], key=v4_rank_key)[:cap]
    ]


def summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    mature = [record for record in records if _finite(record.get("net_return")) is not None]
    returns = [float(record["net_return"]) for record in mature]
    return {
        "selected": len(records), "mature_trades": len(mature),
        "immature_selected": len(records) - len(mature),
        "mature_signal_days": len({str(record["signal_date"]) for record in mature}),
        "win_rate": statistics.fmean(value > 0 for value in returns) if returns else None,
        "mean_net_return": statistics.fmean(returns) if returns else None,
    }


def selection_diagnostics(
    population: Sequence[Mapping[str, object]],
    baseline: Sequence[Mapping[str, object]], heat: Sequence[Mapping[str, object]], cap: int,
) -> dict[str, int]:
    baseline_ids = {_identity(record) for record in baseline}
    heat_ids = {_identity(record) for record in heat}
    days = {str(record["signal_date"]) for record in population}
    heat_by_day = Counter(str(record["signal_date"]) for record in heat)
    baseline_by_day = Counter(str(record["signal_date"]) for record in baseline)
    return {
        "population": len(population),
        "heat_eligible": sum(passes_heat_filter(record) for record in population),
        "heat_removed_from_population": sum(
            not passes_heat_filter(record) for record in population),
        "baseline_selected": len(baseline), "heat_selected": len(heat),
        "baseline_selected_removed_by_filter": sum(
            not passes_heat_filter(record) for record in baseline),
        "heat_replacements_vs_baseline": len(heat_ids - baseline_ids),
        "heat_unfilled_slots_vs_baseline": sum(
            max(0, baseline_by_day[day] - heat_by_day[day]) for day in days),
        "daily_cap": cap,
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _totals(records: Sequence[Mapping[str, object]]) -> tuple[int, int, float]:
    mature = [record for record in records if _finite(record.get("net_return")) is not None]
    return (
        len(mature), sum(float(record["net_return"]) > 0 for record in mature),
        sum(float(record["net_return"]) for record in mature),
    )


def paired_mature_days(
    baseline: Sequence[Mapping[str, object]], heat: Sequence[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]], dict[str, object]]:
    """Censor a whole day when either selected arm contains an immature label."""
    immature_days = {
        str(record["signal_date"])
        for record in (*baseline, *heat)
        if _finite(record.get("net_return")) is None
    }
    return (
        [record for record in baseline if str(record["signal_date"]) not in immature_days],
        [record for record in heat if str(record["signal_date"]) not in immature_days],
        {
            "censored_days": sorted(immature_days),
            "censored_day_count": len(immature_days),
            "baseline_immature_selected": sum(
                _finite(record.get("net_return")) is None for record in baseline),
            "heat_immature_selected": sum(
                _finite(record.get("net_return")) is None for record in heat),
        },
    )


def contiguous_blocks(days: Sequence[str], block_length: int) -> list[list[str]]:
    block = max(1, min(block_length, len(days)))
    return [list(days[start:start + block])
            for start in range(len(days) - block + 1)]


def moving_block_bootstrap(
    baseline: Sequence[Mapping[str, object]], heat: Sequence[Mapping[str, object]],
    trading_days: Sequence[str], *, draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED, block_length: int = DEFAULT_BLOCK_LENGTH,
) -> dict[str, object]:
    """Bootstrap ratio differences over the complete session calendar."""
    if not trading_days or draws <= 0:
        return {"status": "insufficient", "draws": 0}
    baseline_by_day: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    heat_by_day: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in baseline:
        baseline_by_day[str(record["signal_date"])].append(record)
    for record in heat:
        heat_by_day[str(record["signal_date"])].append(record)
    block = max(1, min(block_length, len(trading_days)))
    blocks = contiguous_blocks(trading_days, block)
    rng = random.Random(seed)
    win_deltas, return_deltas = [], []
    for _ in range(draws):
        chosen: list[str] = []
        while len(chosen) < len(trading_days):
            chosen.extend(rng.choice(blocks))
        chosen = chosen[:len(trading_days)]
        base_totals = [0, 0, 0.0]
        heat_totals = [0, 0, 0.0]
        for day in chosen:
            for index, value in enumerate(_totals(baseline_by_day.get(day, ()))):
                base_totals[index] += value
            for index, value in enumerate(_totals(heat_by_day.get(day, ()))):
                heat_totals[index] += value
        if base_totals[0] and heat_totals[0]:
            win_deltas.append(
                heat_totals[1] / heat_totals[0] - base_totals[1] / base_totals[0])
            return_deltas.append(
                heat_totals[2] / heat_totals[0] - base_totals[2] / base_totals[0])
    base_n, base_wins, base_return = _totals(baseline)
    heat_n, heat_wins, heat_return = _totals(heat)
    if not base_n or not heat_n:
        return {"status": "insufficient", "draws": 0}
    return {
        "status": "ready", "draws": draws, "seed": seed,
        "cluster": "complete_trading_session_calendar",
        "block_length_sessions": block,
        "zero_signal_sessions_retained": True,
        "win_rate": {
            "difference": heat_wins / heat_n - base_wins / base_n,
            "ci95": [_percentile(win_deltas, .025), _percentile(win_deltas, .975)],
        },
        "mean_net_return": {
            "difference": heat_return / heat_n - base_return / base_n,
            "ci95": [_percentile(return_deltas, .025), _percentile(return_deltas, .975)],
        },
    }


def _load_records(
    bundle: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    sources = ((bundle.get("training_source_identity") or {}).get("sources") or [])
    records: list[dict[str, object]] = []
    evaluation_records: list[dict[str, object]] = []
    seen_keys: set[tuple[object, ...]] = set()
    seen_events: set[str] = set()
    seen_symbol_dates: set[tuple[str, str]] = set()
    raw_rows, open_rows = 0, 0
    raw_by_type, closed_by_type, open_by_type = Counter(), Counter(), Counter()
    for source in sources:
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for encoded_line in stream:
                digest.update(encoded_line)
                line = encoded_line.decode("utf-8")
                if not line.strip():
                    continue
                raw_rows += 1
                raw = json.loads(line)
                signal_type = str(raw.get("probe_pattern") or "").upper()
                if signal_type not in {"T1", "T2"}:
                    continue
                raw_by_type[signal_type] += 1
                key = tuple(raw.get("key") or ())
                event_id = str(raw.get("event_id") or "")
                symbol_date = (str(raw.get("symbol") or ""), str(raw.get("signal_date") or ""))
                if not key or key in seen_keys:
                    raise ValueError(f"missing or duplicate event key: {key}")
                if not event_id or event_id in seen_events:
                    raise ValueError(f"missing or duplicate event_id: {event_id}")
                if not all(symbol_date) or symbol_date in seen_symbol_dates:
                    raise ValueError(f"missing or duplicate symbol-date: {symbol_date}")
                seen_keys.add(key)
                seen_events.add(event_id)
                seen_symbol_dates.add(symbol_date)
                record = _training_record(raw)
                if record is None:
                    open_rows += 1
                    open_by_type[signal_type] += 1
                    evaluation = dict(raw)
                    evaluation["net_return"] = None
                    evaluation["label_status"] = "immature"
                else:
                    closed_by_type[signal_type] += 1
                    record["label_status"] = "mature"
                    records.append(record)
                    evaluation = record
                feature_asof = feature(evaluation, "feature_asof")
                if str(feature_asof or "") != str(evaluation["signal_date"]):
                    raise ValueError("feature_asof must equal signal_date")
                evaluation_records.append(evaluation)
        declared = str(source.get("sha256") or "")
        if not declared or digest.hexdigest() != declared:
            raise ValueError(f"source sha256 mismatch: {path}")
    return records, evaluation_records, {
        "source_files": len(sources), "raw_rows": raw_rows,
        "closed_records": len(records), "open_or_unlabelled_records": open_rows,
        "raw_records_by_type": dict(raw_by_type),
        "closed_records_by_type": dict(closed_by_type),
        "open_or_unlabelled_records_by_type": dict(open_by_type),
        "source_sha256_verified": len(sources),
    }


def _months(records: Sequence[Mapping[str, object]]) -> list[str]:
    return sorted({str(record["signal_date"])[:7] for record in records})


def _month_days(calendar: Sequence[str], month: str) -> list[str]:
    return [day for day in calendar if day.startswith(month)]


def training_records_for_month(
    records: Sequence[Mapping[str, object]], calendar: Sequence[str], month: str,
) -> tuple[list[Mapping[str, object]], dict[str, str]]:
    days = _month_days(calendar, month)
    if not days:
        raise ValueError(f"calendar does not cover {month}")
    boundaries = _boundaries(calendar, days[0])
    return [record for record in records if _eligible_record(record, boundaries)], boundaries


def score_validation_records(
    bundle: Mapping[str, object], records: Sequence[Mapping[str, object]], month: str,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    scored, rejected = [], Counter()
    for source in records:
        if (str(source.get("probe_pattern") or "").upper() != "T2"
                or not str(source["signal_date"]).startswith(month)):
            continue
        prediction = score_v4_shadow_record(bundle, source)
        if prediction.get("score_status") != "ready":
            rejected[str(prediction.get("fallback_reason") or
                         prediction.get("score_status") or "unknown")] += 1
            continue
        row = dict(source)
        row["research_expected_net_return"] = prediction["expected_net_return"]
        row["research_estimated_win_rate"] = prediction["estimated_win_rate"]
        scored.append(row)
    return scored, dict(rejected)


def _score_month(
    training_source: Sequence[Mapping[str, object]],
    evaluation_source: Sequence[Mapping[str, object]],
    calendar: Sequence[str], month: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    days = _month_days(calendar, month)
    if not days:
        return [], {"month": month, "status": "calendar_missing"}
    training, boundaries = training_records_for_month(training_source, calendar, month)
    t2_training_days = len({
        str(record["signal_date"]) for record in training
        if str(record.get("probe_pattern") or "").upper() == "T2"
    })
    if t2_training_days < 120:
        return [], {
            "month": month, "validation_start": boundaries["validation_start"],
            "training_cutoff": boundaries["training_cutoff"],
            "purge_cutoff": boundaries["purge_cutoff"],
            "training_records": sum(
                str(record.get("probe_pattern") or "").upper() == "T2"
                for record in training),
            "training_signal_days": t2_training_days,
            "status": "training_window_insufficient",
        }
    bundle = fit_v4_shadow_bundle(
        training, market_calendar=calendar, outcome_field="net_return",
        win_field="won_net", minimum_training_signal_days=120,
        model_version=f"T2-HEAT-RESEARCH-{month}", data_version="research-only",
        **boundaries,
    )
    model = bundle["models"]["T2"]
    metadata = {
        "month": month, "validation_start": boundaries["validation_start"],
        "training_cutoff": boundaries["training_cutoff"],
        "purge_cutoff": boundaries["purge_cutoff"],
        "training_records": model["training_records"],
        "training_signal_days": model["training_signal_days"],
        "status": "ready" if (
            model.get("status") == "ready"
            and int(model.get("training_signal_days", 0)) >= 120
        ) else "training_window_insufficient",
    }
    if metadata["status"] != "ready":
        return [], metadata
    scored, rejected = score_validation_records(bundle, evaluation_source, month)
    metadata["scored_validation_records"] = len(scored)
    metadata["unscorable_validation_records"] = sum(
        str(source.get("probe_pattern") or "").upper() == "T2"
        and str(source["signal_date"]).startswith(month)
        for source in evaluation_source
    ) - len(scored)
    metadata["unscorable_reasons"] = rejected
    metadata["mature_validation_records"] = sum(
        _finite(record.get("net_return")) is not None for record in scored)
    metadata["immature_validation_records"] = (
        len(scored) - metadata["mature_validation_records"])
    return scored, metadata


def execution_audit(records: Sequence[Mapping[str, object]], calendar: Sequence[str]) -> dict[str, object]:
    assumptions, reasons, holding = Counter(), Counter(), []
    positions = {day: index for index, day in enumerate(calendar)}
    for record in records:
        execution = record.get("execution")
        if not isinstance(execution, Mapping):
            continue
        entry = execution.get("entry")
        exit_data = execution.get("exit")
        if isinstance(entry, Mapping):
            assumptions[str(entry.get("assumption") or "unspecified")] += 1
        if isinstance(exit_data, Mapping):
            reasons[str(exit_data.get("reason") or "unspecified")] += 1
            entry_day = str(entry.get("date") or "") if isinstance(entry, Mapping) else ""
            exit_day = str(exit_data.get("date") or "")
            if entry_day in positions and exit_day in positions:
                holding.append(positions[exit_day] - positions[entry_day])
    return {
        "entry_assumptions": dict(assumptions), "exit_reasons": dict(reasons),
        "holding_sessions": {
            "minimum": min(holding) if holding else None,
            "median": statistics.median(holding) if holding else None,
            "maximum": max(holding) if holding else None,
        },
        "heat_feature_timing": "close_to_ma20 includes the signal-day close",
    }


def run_study(
    bundle_path: Path, calendar_path: Path, *, draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    source_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    validate_v4_shadow_bundle(source_bundle)
    calendar, calendar_hash = _calendar(calendar_path)
    records, evaluation_records, provenance = _load_records(source_bundle)
    t2 = [record for record in evaluation_records
          if str(record.get("probe_pattern") or "").upper() == "T2"]
    month_metadata, all_scored = [], []
    for month in _months(t2):
        scored, metadata = _score_month(records, evaluation_records, calendar, month)
        month_metadata.append(metadata)
        all_scored.extend(scored)

    comparisons: dict[str, object] = {}
    ready_months = [item["month"] for item in month_metadata if item["status"] == "ready"]
    if not ready_months:
        raise ValueError("no month has the required 120 T2 training signal days")
    first_ready_days = _month_days(calendar, ready_months[0])
    last_ready_days = _month_days(calendar, ready_months[-1])
    first_index = calendar.index(first_ready_days[0])
    last_index = calendar.index(last_ready_days[-1])
    complete_days = calendar[first_index:last_index + 1]
    for cap in (5, 10):
        baseline = select_daily(all_scored, cap, heat_filter=False)
        heat = select_daily(all_scored, cap, heat_filter=True)
        evaluated_baseline, evaluated_heat, censoring = paired_mature_days(
            baseline, heat)
        monthly = []
        for month in ready_months:
            population = [record for record in all_scored
                          if str(record["signal_date"]).startswith(month)]
            month_baseline = [record for record in baseline
                              if str(record["signal_date"]).startswith(month)]
            month_heat = [record for record in heat
                          if str(record["signal_date"]).startswith(month)]
            eval_baseline, eval_heat, month_censoring = paired_mature_days(
                month_baseline, month_heat)
            monthly.append({
                "month": month, "population": len(population),
                "baseline": summary(eval_baseline), "heat_filtered": summary(eval_heat),
                "censoring": month_censoring,
                "selection": selection_diagnostics(
                    population, month_baseline, month_heat, cap),
            })
        comparisons[f"top{cap}"] = {
            "rank_basis": "monthly_walk_forward_expected_net_return_then_win_rate",
            "baseline": summary(evaluated_baseline),
            "heat_filtered": summary(evaluated_heat), "censoring": censoring,
            "selection": selection_diagnostics(all_scored, baseline, heat, cap),
            "monthly": monthly,
            "bootstrap_difference_heat_minus_baseline": moving_block_bootstrap(
                evaluated_baseline, evaluated_heat, complete_days,
                draws=draws, seed=seed + cap),
        }

    return {
        "schema_version": SCHEMA_VERSION, "study_date": date.today().isoformat(),
        "fixed_hypothesis": {
            "signal_type": "T2", "feature": "close_to_ma20", "condition": "<=",
            "threshold": HEAT_THRESHOLD, "grid_search": False,
        },
        "source": {
            "bundle_path": str(bundle_path),
            "source_model_version": source_bundle.get("model_version"),
            "calendar_path": str(calendar_path), "calendar_hash": calendar_hash,
            **provenance,
        },
        "walk_forward": {
            "training_policy": "all mature T1/T2; signal<=purge and exit<=cutoff",
            "monthly_models": month_metadata, "ready_months": ready_months,
            "complete_calendar_sessions": len(complete_days), "comparisons": comparisons,
        },
        "execution_contract": execution_audit(
            [record for record in t2 if _finite(record.get("net_return")) is not None],
            calendar,
        ),
        "limitations": [
            "The 5% threshold was inspected on the 2025-07 through 2026-10 development sample before this run; this is retrospective research, not an independent holdout.",
            "Monthly models are causal, but the hypothesis itself was chosen with knowledge of the full development sample.",
            "Labels assume signal-day close fills, a fixed 3% stop path and a short time exit; same-close execution, next-day fill feasibility and portfolio returns are unverified.",
            "Historical membership, point-in-time adjustment, liquidity and limit-up/limit-down feasibility remain unverified.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=(
        ROOT / ".cache/stock-trend/models/weak_to_strong_v4/2026-11.json"))
    parser.add_argument("--calendar", type=Path, default=(
        ROOT / ".cache/stock-trend/trading_calendar.json"))
    parser.add_argument("--output", type=Path, default=(
        ROOT / ".cache/stock-trend/research/t2-heat-20261116/results.json"))
    parser.add_argument("--bootstrap-draws", type=int, default=DEFAULT_DRAWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    result = run_study(
        args.bundle.resolve(), args.calendar.resolve(),
        draws=args.bootstrap_draws, seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "complete", "output": str(args.output),
        "ready_months": len(result["walk_forward"]["ready_months"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
