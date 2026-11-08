"""Research-only T1/T2 score calibration models.

The models are deliberately separate from production scoring.  Every fitted
quantity comes from closed training labels supplied by the walk-forward
runner; the resulting 0--100 score is a within-type percentile, while
``expected_net_return`` remains the comparable ranking value.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
import hashlib
import json
import math
import statistics
from typing import Mapping, Sequence


SCHEMA_VERSION = "v3-score-typed-calibration/v1"
CANDIDATES = {
    "T1-E": ("T1", ()),
    "T1-MA": ("T1", ("close_to_ma20", "ma10_to_ma20")),
    "T1-VOL": ("T1", ("signal_volume_to_prior20_median",)),
    "T1-PHASE": (
        "T1", ("close_to_ma20", "ma10_to_ma20",
               "signal_volume_to_prior20_median", "stage"),
    ),
    "T2-E": ("T2", ()),
    "T2-HEAT": (
        "T2", ("close_to_ma20", "ma10_to_ma20",
               "signal_volume_to_prior20_median"),
    ),
    "T2-PHASE": (
        "T2", ("close_to_ma20", "ma10_to_ma20",
               "signal_volume_to_prior20_median", "stage", "probe_status"),
    ),
    "T2-RETEST": (
        "T2", ("close_to_ma20", "ma10_to_ma20",
               "signal_volume_to_prior20_median", "stage", "probe_status",
               "t2_retest_to_anchor_volume"),
    ),
}
MONOTONE_DECREASING = {
    ("T1", "signal_volume_to_prior20_median"),
    ("T2", "close_to_ma20"),
    ("T2", "ma10_to_ma20"),
    ("T2", "signal_volume_to_prior20_median"),
}
CATEGORICAL = {"stage", "probe_status"}
_VALIDATED_MODELS: dict[int, str] = {}


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _day(row: Mapping[str, object]) -> str:
    return str(row.get("signal_date") or row.get("trade_date") or row.get("date") or "")


def _probe(row: Mapping[str, object]) -> str | None:
    raw = str(row.get("probe_pattern") or row.get("probe_type") or "").upper()
    if raw.startswith("T1"):
        return "T1"
    if raw.startswith("T2"):
        return "T2"
    features = row.get("features")
    nested = features.get("features") if isinstance(features, Mapping) else None
    raw = str(nested.get("probe_pattern") if isinstance(nested, Mapping) else "").upper()
    return "T1" if raw.startswith("T1") else "T2" if raw.startswith("T2") else None


def _feature(row: Mapping[str, object], name: str) -> object:
    if name == "probe_status":
        value = row.get("probe_status")
        if value is not None:
            return str(value).upper()
    signal = row.get("signal")
    if name == "stage" and isinstance(signal, Mapping) and signal.get("stage") is not None:
        return str(signal["stage"]).upper()
    if row.get(name) is not None:
        return row[name]
    features = row.get("features")
    if isinstance(features, Mapping):
        if features.get(name) is not None:
            return features[name]
        nested = features.get("features")
        if isinstance(nested, Mapping) and nested.get(name) is not None:
            return nested[name]
    return None


def _old_score(row: Mapping[str, object]) -> float | None:
    for name in ("old_score", "v3_score", "score"):
        value = _finite(_feature(row, name))
        if value is not None:
            return value
    signal = row.get("signal")
    score = signal.get("score") if isinstance(signal, Mapping) else None
    return _finite(score.get("total")) if isinstance(score, Mapping) else None


def _outcome(row: Mapping[str, object], field: str) -> float:
    value = _finite(row.get(field))
    label = row.get("label")
    if value is None and isinstance(label, Mapping):
        value = _finite(label.get(field))
    if value is None:
        raise ValueError(f"training record has no finite {field!r}")
    return value


def _won(row: Mapping[str, object], outcome: float, field: str | None) -> bool:
    if field is None:
        return outcome > 0
    value = row.get(field)
    if isinstance(value, bool):
        return value
    numeric = _finite(value)
    if numeric is None:
        raise ValueError(f"training record has no boolean/numeric {field!r}")
    return numeric > 0


def _cuts(values: Sequence[float], quantiles: int) -> list[float]:
    ordered = sorted(values)
    maximum = ordered[-1]
    # Use observed values and omit a maximum-valued cut.  This avoids empty
    # bins for discrete legacy scores and highly tied factors.
    return sorted({
        ordered[math.floor((len(ordered) - 1) * index / quantiles)]
        for index in range(1, quantiles)
        if ordered[math.floor((len(ordered) - 1) * index / quantiles)] < maximum
    })


def _shrink(value: float, count: int, parent: float, strength: float) -> float:
    weight = count / (count + strength)
    return weight * value + (1 - weight) * parent


def _isotonic_decreasing(values: Sequence[float], weights: Sequence[int]) -> list[float]:
    """Weighted pool-adjacent-violators fit constrained to be non-increasing."""
    blocks: list[list[float | int]] = []
    for index, (value, weight) in enumerate(zip(values, weights)):
        blocks.append([index, index, float(value), int(weight)])
        while len(blocks) > 1 and float(blocks[-2][2]) < float(blocks[-1][2]):
            right = blocks.pop()
            left = blocks.pop()
            total = int(left[3]) + int(right[3])
            mean = (float(left[2]) * int(left[3])
                    + float(right[2]) * int(right[3])) / total
            blocks.append([int(left[0]), int(right[1]), mean, total])
    output = [0.0] * len(values)
    for start, end, value, _ in blocks:
        for index in range(int(start), int(end) + 1):
            output[index] = float(value)
    return output


def _fit_numeric_effect(rows, residual_returns, residual_wins, name, *, probe,
                        quantiles, shrinkage, minimum_count, minimum_days):
    observations = []
    for index, row in enumerate(rows):
        value = _finite(_feature(row, name))
        if value is not None:
            observations.append((index, value))
    if not observations:
        return {"kind": "numeric", "status": "unavailable", "cuts": [], "bins": []}
    cuts = _cuts([item[1] for item in observations], quantiles)
    groups = [[] for _ in range(len(cuts) + 1)]
    for index, value in observations:
        groups[bisect_left(cuts, value)].append(index)
    bins = []
    for indices in groups:
        count = len(indices)
        days = len({_day(rows[index]) for index in indices})
        ret = statistics.fmean(residual_returns[index] for index in indices)
        win = statistics.fmean(residual_wins[index] for index in indices)
        ready = count >= minimum_count and days >= minimum_days
        bins.append({
            "count": count,
            "signal_days": days,
            "return_effect": _shrink(ret, count, 0.0, shrinkage) if ready else 0.0,
            "win_effect": _shrink(win, count, 0.0, shrinkage) if ready else 0.0,
            "fallback_parent": not ready,
        })
    if (probe, name) in MONOTONE_DECREASING:
        weights = [int(item["count"]) for item in bins]
        for field in ("return_effect", "win_effect"):
            fitted = _isotonic_decreasing([float(item[field]) for item in bins], weights)
            for item, value in zip(bins, fitted):
                item[field] = value
    return {"kind": "numeric", "status": "ready", "cuts": cuts, "bins": bins,
            "monotone": "non_increasing" if (probe, name) in MONOTONE_DECREASING else None}


def _fit_category_effect(rows, residual_returns, residual_wins, name, *, shrinkage,
                         minimum_count, minimum_days):
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        value = _feature(row, name)
        if value is not None:
            groups[str(value).upper()].append(index)
    cells = {}
    for value, indices in sorted(groups.items()):
        count = len(indices)
        days = len({_day(rows[index]) for index in indices})
        ready = count >= minimum_count and days >= minimum_days
        cells[value] = {
            "count": count,
            "signal_days": days,
            "return_effect": _shrink(
                statistics.fmean(residual_returns[index] for index in indices),
                count, 0.0, shrinkage,
            ) if ready else 0.0,
            "win_effect": _shrink(
                statistics.fmean(residual_wins[index] for index in indices),
                count, 0.0, shrinkage,
            ) if ready else 0.0,
            "fallback_parent": not ready,
        }
    return {"kind": "categorical", "status": "ready" if cells else "unavailable",
            "cells": cells}


def _effect_value(effect: Mapping[str, object], row: Mapping[str, object], name: str):
    if effect.get("status") != "ready":
        return 0.0, 0.0, "unavailable"
    value = _feature(row, name)
    if effect["kind"] == "categorical":
        cell = effect["cells"].get(str(value).upper()) if value is not None else None
        if cell is None:
            return 0.0, 0.0, "category_fallback"
        status = "parent_fallback" if cell.get("fallback_parent") else "ready"
        return float(cell["return_effect"]), float(cell["win_effect"]), status
    numeric = _finite(value)
    if numeric is None:
        return 0.0, 0.0, "missing_fallback"
    index = bisect_left(effect["cuts"], numeric)
    cell = effect["bins"][index]
    status = "parent_fallback" if cell.get("fallback_parent") else "ready"
    return float(cell["return_effect"]), float(cell["win_effect"]), status


def _midrank_distribution(values: Sequence[float]) -> list[dict[str, float | int]]:
    counts = Counter(float(value) for value in values)
    less, total, result = 0, len(values), []
    for value, count in sorted(counts.items()):
        result.append({"value": value, "count": count,
                       "less": less,
                       "score": 100 * (less + .5 * count) / total})
        less += count
    return result


def _midrank_score(distribution: Sequence[Mapping[str, object]], value: float, *,
                   points: Sequence[float] | None = None,
                   total: int | None = None) -> float:
    points = points if points is not None else [float(item["value"]) for item in distribution]
    index = bisect_left(points, value)
    if index < len(points) and points[index] == value:
        return float(distribution[index]["score"])
    less = (int(distribution[index]["less"]) if index < len(distribution)
            else int(distribution[-1]["less"]) + int(distribution[-1]["count"]))
    total = total if total is not None else (
        int(distribution[-1]["less"]) + int(distribution[-1]["count"])
    )
    return 100 * less / total


def _hash(model: Mapping[str, object]) -> str:
    payload = {key: value for key, value in model.items() if key != "config_hash"}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_typed_model(model: Mapping[str, object]) -> None:
    """Validate a fitted model once per in-memory object."""
    declared = str(model.get("config_hash") or "")
    if _VALIDATED_MODELS.get(id(model)) == declared:
        return
    if model.get("schema_version") != SCHEMA_VERSION or declared != _hash(model):
        raise ValueError("typed score model is invalid or was modified")
    _VALIDATED_MODELS[id(model)] = declared


def fit_typed_model(
    records: Sequence[Mapping[str, object]], candidate: str, *,
    outcome_field: str = "net_return", win_field: str | None = None,
    quantiles: int = 5, minimum_count: int = 300, minimum_days: int = 20,
    shrinkage: float = 300,
) -> dict[str, object]:
    """Fit one frozen, type-specific research candidate."""
    candidate = candidate.upper()
    if candidate not in CANDIDATES:
        raise ValueError(f"candidate must be one of {sorted(CANDIDATES)}")
    if quantiles < 2 or minimum_count < 1 or minimum_days < 1 or shrinkage < 0:
        raise ValueError("invalid typed-model training configuration")
    probe, feature_names = CANDIDATES[candidate]
    rows = sorted((row for row in records if _probe(row) == probe), key=lambda row: (
        _day(row), str(row.get("symbol") or ""), str(row.get("cycle_id") or ""),
        str(row.get("event_id") or ""),
    ))
    days = len({_day(row) for row in rows})
    model: dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "candidate": candidate,
        "probe_type": probe, "outcome_field": outcome_field, "win_field": win_field,
        "quantiles": quantiles, "minimum_count": minimum_count,
        "minimum_days": minimum_days, "shrinkage": float(shrinkage),
        "training_records": len(rows), "training_signal_days": days,
        "status": "insufficient", "feature_order": list(feature_names),
        "relative_score_is_probability": False,
    }
    if len(rows) < minimum_count or days < minimum_days:
        model["config_hash"] = _hash(model)
        return model
    outcomes = [_outcome(row, outcome_field) for row in rows]
    wins = [_won(row, outcome, win_field) for row, outcome in zip(rows, outcomes)]
    scores = [_old_score(row) for row in rows]
    if any(value is None for value in scores):
        raise ValueError("typed model requires finite old_score")
    global_return = statistics.fmean(outcomes)
    global_win = statistics.fmean(float(value) for value in wins)
    old_cuts = _cuts([float(value) for value in scores], quantiles)
    old_groups = [[] for _ in range(len(old_cuts) + 1)]
    for index, score in enumerate(scores):
        old_groups[bisect_left(old_cuts, float(score))].append(index)
    old_bins = []
    predictions_return = [global_return] * len(rows)
    predictions_win = [global_win] * len(rows)
    for indices in old_groups:
        count = len(indices)
        bin_days = len({_day(rows[index]) for index in indices})
        ready = count >= minimum_count and bin_days >= minimum_days
        estimate_return = (_shrink(
            statistics.fmean(outcomes[index] for index in indices), count,
            global_return, shrinkage,
        ) if ready else global_return)
        estimate_win = (_shrink(
            statistics.fmean(float(wins[index]) for index in indices), count,
            global_win, shrinkage,
        ) if ready else global_win)
        old_bins.append({"count": count,
                         "signal_days": bin_days,
                         "estimated_net_return": estimate_return,
                         "estimated_win_rate": estimate_win,
                         "fallback_parent": not ready})
        for index in indices:
            predictions_return[index] = estimate_return
            predictions_win[index] = estimate_win
    effects = {}
    for name in feature_names:
        residual_returns = [outcome - prediction
                            for outcome, prediction in zip(outcomes, predictions_return)]
        residual_wins = [float(won) - prediction
                         for won, prediction in zip(wins, predictions_win)]
        effect = (_fit_category_effect(rows, residual_returns, residual_wins, name,
                                       shrinkage=shrinkage,
                                       minimum_count=minimum_count,
                                       minimum_days=minimum_days)
                  if name in CATEGORICAL else
                  _fit_numeric_effect(rows, residual_returns, residual_wins, name,
                                      probe=probe, quantiles=quantiles,
                                      shrinkage=shrinkage,
                                      minimum_count=minimum_count,
                                      minimum_days=minimum_days))
        effects[name] = effect
        weight = .5 if name == "t2_retest_to_anchor_volume" else 1.0
        for index, row in enumerate(rows):
            ret, win, _ = _effect_value(effect, row, name)
            predictions_return[index] += weight * ret
            predictions_win[index] = min(1.0, max(0.0, predictions_win[index] + weight * win))
    score_distribution = _midrank_distribution(predictions_return)
    model.update({
        "status": "ready", "global_mean_net_return": global_return,
        "global_win_rate": global_win, "old_score_cuts": old_cuts,
        "old_score_bins": old_bins, "effects": effects,
        "score_distribution": score_distribution,
        "score_points": [float(item["value"]) for item in score_distribution],
        "score_total": len(predictions_return),
        "retest_effect_weight": .5,
    })
    model["config_hash"] = _hash(model)
    return model


def score_typed_record(model: Mapping[str, object], record: Mapping[str, object]
                       ) -> dict[str, object]:
    """Score a record with a frozen type-specific model."""
    validate_typed_model(model)
    candidate, probe = str(model["candidate"]), _probe(record)
    base = {"candidate": candidate, "probe_type": probe,
            "config_hash": model["config_hash"]}
    if probe != model["probe_type"]:
        return {**base, "status": "not_applicable", "score": None,
                "rank_value": None, "expected_net_return": None,
                "estimated_net_return": None, "estimated_win_rate": None,
                "predicted_win_rate": None, "details": {}}
    old = _old_score(record)
    if model["status"] != "ready":
        return {**base, "status": "fallback_a", "score": old,
                "rank_value": old, "expected_net_return": None,
                "estimated_net_return": None, "estimated_win_rate": None,
                "predicted_win_rate": None,
                "fallback_status": "type_training_insufficient", "details": {}}
    if old is None:
        return {**base, "status": "unscorable", "score": None,
                "rank_value": None, "expected_net_return": None,
                "estimated_net_return": None, "estimated_win_rate": None,
                "predicted_win_rate": None, "details": {}}
    old_index = bisect_left(model["old_score_cuts"], old)
    old_bin = model["old_score_bins"][old_index]
    estimated_return = float(old_bin["estimated_net_return"])
    estimated_win = float(old_bin["estimated_win_rate"])
    contributions = {}
    for name in model["feature_order"]:
        ret, win, status = _effect_value(model["effects"][name], record, name)
        weight = float(model["retest_effect_weight"]) if name == "t2_retest_to_anchor_volume" else 1.0
        estimated_return += weight * ret
        estimated_win += weight * win
        contributions[name] = {"return_effect": weight * ret,
                               "win_effect": weight * win, "status": status}
    estimated_win = min(1.0, max(0.0, estimated_win))
    score = _midrank_score(
        model["score_distribution"], estimated_return,
        points=model["score_points"], total=int(model["score_total"]),
    )
    details = {"old_score": old, "old_score_bin": old_index,
               "feature_contributions": contributions}
    return {**base, "status": "ready", "score": score,
            "rank_value": estimated_return,
            "expected_net_return": estimated_return,
            "estimated_net_return": estimated_return,
            "estimated_win_rate": estimated_win,
            "predicted_win_rate": estimated_win,
            "fallback_status": None, "details": details}
