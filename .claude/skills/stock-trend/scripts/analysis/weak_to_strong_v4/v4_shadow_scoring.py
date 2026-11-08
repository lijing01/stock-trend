"""Research-only V4 shadow scoring contract.

This module freezes the first V4 candidates described by the execution plan
and wraps :mod:`v3_score_typed_model` with the metadata, causal feature
snapshot, integrity checks, and explicit V3 fallback required by a daily
shadow run.  It does not modify production signal selection or ranking.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from .v3_score_typed_model import (
    fit_typed_model,
    score_typed_record,
    validate_typed_model,
)


SCHEMA_VERSION = "v4-shadow-scoring/v1"
FEATURE_SCHEMA_VERSION = "v4-shadow-feature-snapshot/v1"
DEFAULT_MODEL_VERSION = "V4-SHADOW-1"
TYPE_CONFIG = {
    "T1": {
        "candidate": "T1-VOL",
        "candidate_version": "T1-VOL-S300",
        "shrinkage": 300.0,
        "required_features": ("signal_volume_to_prior20_median",),
    },
    "T2": {
        "candidate": "T2-HEAT",
        "candidate_version": "T2-HEAT-S600",
        "shrinkage": 600.0,
        "required_features": (
            "close_to_ma20",
            "ma10_to_ma20",
            "signal_volume_to_prior20_median",
        ),
    },
}
DIAGNOSTIC_FEATURES = (
    "close_to_ma10",
    "close_to_ma20",
    "ma10_to_ma20",
    "ma10_slope_3",
    "ma20_slope_3",
    "signal_volume_to_prior5_mean",
    "signal_volume_to_prior20_median",
    "t2_retest_to_anchor_volume",
    "t2_retest_to_prior5_mean",
    "probe_age_sessions",
    "stage",
    "probe_status",
)


def _normalise_date(value: object) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
    except ValueError:
        return ""


def _containers(record: Mapping[str, object]):
    yield record
    features = record.get("features")
    if isinstance(features, Mapping):
        yield features
        nested = features.get("features")
        if isinstance(nested, Mapping):
            yield nested


def _value(record: Mapping[str, object], name: str) -> object:
    for container in _containers(record):
        if container.get(name) is not None:
            return container[name]
    return None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _signal_type(record: Mapping[str, object]) -> str | None:
    for name in ("signal_type", "probe_type", "probe_pattern"):
        raw = str(_value(record, name) or "").upper()
        if raw.startswith("T1"):
            return "T1"
        if raw.startswith("T2"):
            return "T2"
    return None


def _v3_score(record: Mapping[str, object]) -> float | None:
    for name in ("v3_score", "old_score", "score"):
        value = _finite(_value(record, name))
        if value is not None:
            return value
    signal = record.get("signal")
    score = signal.get("score") if isinstance(signal, Mapping) else None
    return _finite(score.get("total")) if isinstance(score, Mapping) else None


def _canonical_hash(payload: Mapping[str, object], hash_field: str) -> str:
    content = {key: value for key, value in payload.items() if key != hash_field}
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_feature_snapshot(record: Mapping[str, object]) -> dict[str, object]:
    """Freeze only information available at the signal confirmation date."""
    signal_date = _normalise_date(
        record.get("signal_date") or record.get("trade_date") or record.get("date")
    )
    feature_asof = _normalise_date(_value(record, "feature_asof")) or signal_date
    signal_type = _signal_type(record)
    features = {name: _value(record, name) for name in DIAGNOSTIC_FEATURES}
    if features["probe_status"] is None:
        features["probe_status"] = record.get("probe_status")
    missing = {}
    source_missing = record.get("missing_reasons")
    feature_block = record.get("features")
    if isinstance(feature_block, Mapping):
        source_missing = feature_block.get("missing_reasons", source_missing)
    if isinstance(source_missing, Mapping):
        missing.update({str(key): str(value) for key, value in source_missing.items()})
    for name in TYPE_CONFIG.get(signal_type, {}).get("required_features", ()):
        if _finite(features.get(name)) is None:
            missing.setdefault(name, "required_feature_missing")
    snapshot: dict[str, object] = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "signal_type": signal_type,
        "probe_pattern": signal_type,
        "signal_date": signal_date,
        "feature_asof": feature_asof,
        "v3_score": _v3_score(record),
        "old_score": _v3_score(record),
        "features": features,
        "missing_reasons": missing,
    }
    snapshot["snapshot_hash"] = _canonical_hash(snapshot, "snapshot_hash")
    return snapshot


def validate_feature_snapshot(snapshot: Mapping[str, object]) -> None:
    if (snapshot.get("schema_version") != FEATURE_SCHEMA_VERSION
            or snapshot.get("snapshot_hash") != _canonical_hash(snapshot, "snapshot_hash")):
        raise ValueError("V4 feature snapshot is invalid or was modified")


def fit_v4_shadow_bundle(
    records: Sequence[Mapping[str, object]], *, training_cutoff: object,
    model_version: str = DEFAULT_MODEL_VERSION, outcome_field: str = "net_return",
    win_field: str | None = None, quantiles: int = 5,
    minimum_count: int = 300, minimum_days: int = 20,
    valid_until: object | None = None, data_version: str = "unspecified",
    training_code_hash: str | None = None,
    purge_cutoff: object | None = None,
    minimum_training_signal_days: int = 120,
    validation_start: object | None = None,
    market_calendar: Sequence[str] | None = None,
) -> dict[str, object]:
    """Fit the frozen T1 and T2 V4 shadow candidates as one versioned bundle."""
    cutoff = _normalise_date(training_cutoff)
    if not cutoff:
        raise ValueError("training_cutoff must be a valid date")
    expiry = _normalise_date(valid_until) if valid_until is not None else ""
    if not expiry or expiry <= cutoff:
        raise ValueError("valid_until must be a valid date")
    purge = _normalise_date(purge_cutoff)
    if not purge or purge > cutoff:
        raise ValueError("purge_cutoff must be a valid date no later than training_cutoff")
    start = _normalise_date(validation_start)
    calendar = [_normalise_date(day) for day in (market_calendar or ())]
    if not start or not calendar or calendar != sorted(set(calendar)) or start not in calendar:
        raise ValueError("validation_start and an ordered market_calendar are required")
    index = calendar.index(start)
    if index < 6 or purge != calendar[index - 6] or cutoff >= start:
        raise ValueError("purge_cutoff must exclude the five market sessions before validation_start")
    if minimum_training_signal_days < 1:
        raise ValueError("minimum_training_signal_days must be positive")
    for record in records:
        signal_date = _normalise_date(
            record.get("signal_date") or record.get("trade_date") or record.get("date")
        )
        exit_date = _normalise_date(
            record.get("exit_date") or record.get("label_exit_date")
        )
        if not signal_date or signal_date > purge:
            raise ValueError("training signal is after purge_cutoff")
        if not exit_date or exit_date > cutoff or exit_date < signal_date:
            raise ValueError("training label was not closed by training_cutoff")
    models = {}
    for signal_type, config in TYPE_CONFIG.items():
        models[signal_type] = fit_typed_model(
            records, str(config["candidate"]), outcome_field=outcome_field,
            win_field=win_field, quantiles=quantiles,
            minimum_count=minimum_count, minimum_days=minimum_days,
            shrinkage=float(config["shrinkage"]),
        )
    bundle: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": str(model_version),
        "training_cutoff": cutoff,
        "purge_cutoff": purge,
        "validation_start": start,
        "valid_until": expiry or None,
        "data_version": str(data_version),
        "training_code_hash": training_code_hash or hashlib.sha256(
            Path(__file__).with_name("v3_score_typed_model.py").read_bytes()
        ).hexdigest(),
        "outcome_field": outcome_field,
        "win_field": win_field,
        "quantiles": quantiles,
        "minimum_count": minimum_count,
        "minimum_days": minimum_days,
        "minimum_training_signal_days": minimum_training_signal_days,
        "candidate_versions": {
            signal_type: config["candidate_version"]
            for signal_type, config in TYPE_CONFIG.items()
        },
        "models": models,
    }
    bundle["bundle_hash"] = _canonical_hash(bundle, "bundle_hash")
    return bundle


def validate_v4_shadow_bundle(bundle: Mapping[str, object]) -> None:
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported V4 shadow bundle schema")
    if bundle.get("bundle_hash") != _canonical_hash(bundle, "bundle_hash"):
        raise ValueError("V4 shadow bundle hash does not match its content")
    models = bundle.get("models")
    if not isinstance(models, Mapping) or set(models) != set(TYPE_CONFIG):
        raise ValueError("V4 shadow bundle must contain T1 and T2 models")
    for signal_type, model in models.items():
        if not isinstance(model, Mapping):
            raise ValueError(f"V4 {signal_type} model is invalid")
        validate_typed_model(model)
        expected = TYPE_CONFIG[signal_type]["candidate"]
        if model.get("candidate") != expected:
            raise ValueError(f"V4 {signal_type} candidate is not frozen to {expected}")


def load_v4_shadow_bundle(path: str | Path) -> dict[str, object]:
    with Path(path).open(encoding="utf-8") as handle:
        bundle = json.load(handle)
    validate_v4_shadow_bundle(bundle)
    return bundle


def _fallback(
    bundle: Mapping[str, object], snapshot: Mapping[str, object], reason: str,
    *, status: str = "fallback_v3",
) -> dict[str, object]:
    return {
        "signal_type": snapshot.get("signal_type"),
        "signal_date": snapshot.get("signal_date"),
        "v3_score": snapshot.get("v3_score"),
        "v4_score": None,
        "expected_net_return": None,
        "estimated_win_rate": None,
        "model_version": bundle.get("model_version"),
        "training_cutoff": bundle.get("training_cutoff"),
        "feature_asof": snapshot.get("feature_asof"),
        "score_status": status,
        "fallback_reason": reason,
        "fallback_v3_score": snapshot.get("v3_score"),
        "model_hash": bundle.get("bundle_hash"),
        "candidate_version": None,
        "factor_contributions": {},
        "missing_reasons": dict(snapshot.get("missing_reasons") or {}),
    }


def score_v4_shadow_record(
    bundle: Mapping[str, object], record: Mapping[str, object], *,
    feature_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Score one signal or return an explicit, unit-safe V3 fallback."""
    snapshot = dict(feature_snapshot) if feature_snapshot is not None else build_feature_snapshot(record)
    try:
        validate_feature_snapshot(snapshot)
    except (TypeError, ValueError):
        safe_snapshot = build_feature_snapshot(record)
        return _fallback(bundle, safe_snapshot, "feature_snapshot_integrity_error")
    if snapshot["snapshot_hash"] != build_feature_snapshot(record)["snapshot_hash"]:
        return _fallback(bundle, build_feature_snapshot(record), "feature_snapshot_record_mismatch")
    try:
        validate_v4_shadow_bundle(bundle)
    except (TypeError, ValueError):
        return _fallback(bundle, snapshot, "model_integrity_error")
    signal_type = snapshot.get("signal_type")
    if signal_type not in TYPE_CONFIG:
        return _fallback(bundle, snapshot, "unsupported_signal_type", status="not_applicable")
    signal_date = str(snapshot.get("signal_date") or "")
    feature_asof = str(snapshot.get("feature_asof") or "")
    if not signal_date:
        return _fallback(bundle, snapshot, "signal_date_missing", status="unscorable")
    if feature_asof != signal_date:
        return _fallback(bundle, snapshot, "feature_asof_mismatch")
    if signal_date <= str(bundle["training_cutoff"]):
        return _fallback(bundle, snapshot, "signal_not_after_training_cutoff")
    if signal_date < str(bundle["validation_start"]):
        return _fallback(bundle, snapshot, "signal_before_validation_start")
    valid_until = str(bundle.get("valid_until") or "")
    if valid_until and signal_date > valid_until:
        return _fallback(bundle, snapshot, "model_expired")
    if snapshot.get("v3_score") is None:
        return _fallback(bundle, snapshot, "v3_score_missing", status="unscorable")
    required = TYPE_CONFIG[str(signal_type)]["required_features"]
    missing = [name for name in required if _finite(_value(snapshot, name)) is None]
    if missing:
        return _fallback(bundle, snapshot, "required_feature_missing:" + ",".join(missing))
    model = bundle["models"][signal_type]
    if int(model.get("training_signal_days", 0)) < int(bundle["minimum_training_signal_days"]):
        return _fallback(bundle, snapshot, "training_window_insufficient")
    scored = score_typed_record(model, snapshot)
    if scored["status"] != "ready":
        reason = ("training_window_insufficient" if scored["status"] == "fallback_a"
                  else str(scored["status"]))
        return _fallback(bundle, snapshot, reason)
    details = scored.get("details") or {}
    return {
        "signal_type": signal_type,
        "signal_date": signal_date,
        "v3_score": snapshot.get("v3_score"),
        "v4_score": scored["score"],
        "expected_net_return": scored["expected_net_return"],
        "estimated_win_rate": scored["estimated_win_rate"],
        "model_version": bundle.get("model_version"),
        "training_cutoff": bundle.get("training_cutoff"),
        "feature_asof": feature_asof,
        "score_status": "ready",
        "fallback_reason": None,
        "fallback_v3_score": None,
        "model_hash": bundle.get("bundle_hash"),
        "candidate_version": bundle["candidate_versions"][signal_type],
        "factor_contributions": details.get("feature_contributions", {}),
        "missing_reasons": dict(snapshot.get("missing_reasons") or {}),
        "training_records": model.get("training_records"),
        "training_signal_days": model.get("training_signal_days"),
    }


def v4_ready_sort_key(record: Mapping[str, object]) -> tuple[float, float, str, str]:
    """Order ready V4 records; fallback records must use a separate V3 policy."""
    expected = _finite(record.get("expected_net_return"))
    win = _finite(record.get("estimated_win_rate"))
    if record.get("score_status") != "ready" or expected is None or win is None:
        raise ValueError("V4 ranking requires a ready prediction")
    return (-expected, -win, str(record.get("symbol") or ""), str(record.get("event_id") or ""))
