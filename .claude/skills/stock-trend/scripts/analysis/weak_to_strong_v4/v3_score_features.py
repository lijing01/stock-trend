"""Point-in-time features for the V3 score calibration study.

The extractor intentionally owns no I/O.  It only uses bars no later than the
explicit signal date and reports missing values instead of coercing them to
zero, so the research scanner can preserve a complete, auditable sample.
"""
from __future__ import annotations

from datetime import date
import math
import statistics
from typing import Mapping


SCHEMA_VERSION = "v3-score-features/v1"


def _normalise_date(value: object) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
    except ValueError:
        return ""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _rows(payload: object, signal_date: str) -> tuple[list[dict[str, float | str]], list[str]]:
    raw_rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(raw_rows, list):
        return [], ["kline_rows_invalid"]
    by_date: dict[str, dict[str, float | str]] = {}
    reasons: list[str] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            reasons.append("kline_row_invalid")
            continue
        day = _normalise_date(raw.get("trade_date") or raw.get("date"))
        if not day:
            reasons.append("kline_date_invalid")
            continue
        if day > signal_date:
            continue
        values = {name: _number(raw.get(name)) for name in ("open", "high", "low", "close")}
        volume = _number(raw.get("vol", raw.get("volume")))
        if any(value is None for value in values.values()) or volume is None:
            reasons.append("kline_value_invalid")
            continue
        row: dict[str, float | str] = {"date": day, **values, "volume": volume}
        if (min(values.values()) <= 0 or volume < 0
                or values["high"] < max(values["open"], values["close"])
                or values["low"] > min(values["open"], values["close"])):
            reasons.append("kline_ohlcv_invalid")
            continue
        if day in by_date:
            reasons.append("kline_duplicate")
            continue
        by_date[day] = row
    return [by_date[day] for day in sorted(by_date)], list(dict.fromkeys(reasons))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(numerator: float | None, denominator: float | None, *, offset: bool = False) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    result = numerator / denominator
    return result - 1 if offset else result


def _ma(closes: list[float], end: int, window: int) -> float | None:
    start = end + 1 - window
    return _mean(closes[start:end + 1]) if start >= 0 else None


def _existing_features(result: object) -> dict[str, object]:
    source = result if isinstance(result, Mapping) else {}
    metrics = source.get("metrics") if isinstance(source.get("metrics"), Mapping) else {}
    return {
        "stage": source.get("stage"),
        "trigger_distance_atr": metrics.get("trigger_distance_atr"),
        "structure_age": metrics.get("structure_age"),
        "close_strength": metrics.get("close_strength"),
        "existing_volume_prior5_ratio": metrics.get("volume_prior5_ratio"),
        "risk_flags": list(source.get("risk_flags") or []),
    }


def extract_v3_score_features(
    kline_payload: object,
    *,
    signal_date: object,
    selected_probe: Mapping[str, object] | None = None,
    v3_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return causal moving-average, volume, and selected-probe features.

    Moving averages include the signal close.  Signal-volume denominators
    exclude the signal day.  T2 retest denominators exclude the candidate day.
    Missing values remain ``None`` and have a corresponding reason code.
    """
    target = _normalise_date(signal_date)
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "feature_asof": target,
        "status": "ready",
        "reason_codes": [],
        "features": {},
        "missing_reasons": {},
    }
    if not target:
        result.update(status="data_insufficient", reason_codes=["signal_date_invalid"])
        return result
    rows, row_reasons = _rows(kline_payload, target)
    positions = {str(row["date"]): index for index, row in enumerate(rows)}
    if target not in positions:
        result.update(status="data_insufficient",
                      reason_codes=list(dict.fromkeys(row_reasons + ["signal_bar_missing"])))
        return result
    if row_reasons:
        result.update(status="data_insufficient", reason_codes=row_reasons)
        return result

    index = positions[target]
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"]) for row in rows]
    close, signal_volume = closes[index], volumes[index]
    ma10, ma20 = _ma(closes, index, 10), _ma(closes, index, 20)
    ma10_lag3, ma20_lag3 = _ma(closes, index - 3, 10), _ma(closes, index - 3, 20)
    prior5 = _mean(volumes[index - 5:index]) if index >= 5 else None
    prior20 = statistics.median(volumes[index - 20:index]) if index >= 20 else None

    features: dict[str, object] = {
        "close_to_ma10": _ratio(close, ma10, offset=True),
        "close_to_ma20": _ratio(close, ma20, offset=True),
        "ma10_to_ma20": _ratio(ma10, ma20, offset=True),
        "ma10_slope_3": _ratio(ma10, ma10_lag3, offset=True),
        "ma20_slope_3": _ratio(ma20, ma20_lag3, offset=True),
        "signal_volume_to_prior5_mean": _ratio(signal_volume, prior5),
        "signal_volume_to_prior20_median": _ratio(signal_volume, prior20),
        "t2_retest_to_anchor_volume": None,
        "t2_retest_to_prior5_mean": None,
        "probe_age_sessions": None,
        "probe_pattern": selected_probe.get("pattern") if isinstance(selected_probe, Mapping) else None,
        "probe_status": selected_probe.get("status") if isinstance(selected_probe, Mapping) else None,
    }
    features.update(_existing_features(v3_result))
    missing: dict[str, str] = {}
    minimums = {
        "close_to_ma10": (ma10, "ma10_history_insufficient"),
        "close_to_ma20": (ma20, "ma20_history_insufficient"),
        "ma10_to_ma20": (ma20 if ma10 is not None else None, "ma20_history_insufficient"),
        "ma10_slope_3": (ma10_lag3, "ma10_lag3_history_insufficient"),
        "ma20_slope_3": (ma20_lag3, "ma20_lag3_history_insufficient"),
        "signal_volume_to_prior5_mean": (prior5, "prior5_volume_history_insufficient"),
        "signal_volume_to_prior20_median": (prior20, "prior20_volume_history_insufficient"),
    }
    for name, (denominator, insufficient) in minimums.items():
        if features[name] is None:
            missing[name] = "volume_denominator_non_positive" if denominator == 0 else insufficient

    if not isinstance(selected_probe, Mapping):
        for name in ("t2_retest_to_anchor_volume", "t2_retest_to_prior5_mean",
                     "probe_age_sessions"):
            missing[name] = "selected_probe_missing"
    else:
        candidate_date = _normalise_date(selected_probe.get("candidate_date"))
        candidate_index = positions.get(candidate_date)
        if candidate_index is None:
            reason = "probe_date_after_signal" if candidate_date and candidate_date > target else "probe_bar_missing"
            for name in ("t2_retest_to_anchor_volume", "t2_retest_to_prior5_mean",
                         "probe_age_sessions"):
                missing[name] = reason
        else:
            features["probe_age_sessions"] = index - candidate_index
            pattern = selected_probe.get("pattern")
            if pattern != "T2_LOW_VOLUME_RETEST":
                missing["t2_retest_to_anchor_volume"] = "not_applicable"
                missing["t2_retest_to_prior5_mean"] = "not_applicable"
            else:
                anchor_date = _normalise_date(selected_probe.get("anchor_date"))
                anchor_index = positions.get(anchor_date)
                candidate_volume = volumes[candidate_index]
                anchor_volume = volumes[anchor_index] if anchor_index is not None else None
                candidate_prior5 = (_mean(volumes[candidate_index - 5:candidate_index])
                                    if candidate_index >= 5 else None)
                features["t2_retest_to_anchor_volume"] = _ratio(candidate_volume, anchor_volume)
                features["t2_retest_to_prior5_mean"] = _ratio(candidate_volume, candidate_prior5)
                if features["t2_retest_to_anchor_volume"] is None:
                    missing["t2_retest_to_anchor_volume"] = (
                        "anchor_volume_non_positive" if anchor_volume == 0
                        else "anchor_bar_missing")
                if features["t2_retest_to_prior5_mean"] is None:
                    missing["t2_retest_to_prior5_mean"] = (
                        "volume_denominator_non_positive" if candidate_prior5 == 0
                        else "candidate_prior5_history_insufficient")

    result["features"] = features
    result["missing_reasons"] = missing
    return result
