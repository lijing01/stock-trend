"""Versioned, immutable contracts for recommendation-evolution evaluations."""

import hashlib
import json


EVALUATION_CONTRACT_VERSION = "today-recommendation-evaluation/v2"
PRIMARY_WINDOW = 20
PRIMARY_BENCHMARK = "hs300"
REQUIRED_ADJUSTMENT = "qfq"


def build_evaluation_contract(windows, cost_model, primary_window=PRIMARY_WINDOW,
                              primary_benchmark=PRIMARY_BENCHMARK,
                              adjustment=REQUIRED_ADJUSTMENT,
                              population_kind="official_candidate_population",
                              evaluation_version="v2",
                              population_identity=None):
    """Build the frozen P0 contract and a stable storage identity.

    The contract id includes fields that change the evaluation *rules* and is
    stable across recommendation dates.  Per-run population identity is kept
    in the contract payload and input manifest, but deliberately does not
    create one contract directory per day; otherwise a weekly loader could not
    aggregate one frozen contract over time.
    """
    normalized_windows = tuple(sorted({int(value) for value in windows}))
    if primary_window != PRIMARY_WINDOW:
        raise ValueError("primary_window_must_be_20")
    if primary_benchmark != PRIMARY_BENCHMARK:
        raise ValueError("primary_benchmark_must_be_hs300")
    if adjustment != REQUIRED_ADJUSTMENT:
        raise ValueError("adjustment_must_be_qfq")
    costs = dict(cost_model or {})
    payload = {
        "version": EVALUATION_CONTRACT_VERSION,
        "primary_window": primary_window,
        "primary_benchmark": primary_benchmark,
        "windows": list(normalized_windows),
        "primary_window_available": primary_window in normalized_windows,
        "adjustment": adjustment,
        "trade_cost_model": costs,
        "trade_cost_mode": "gross" if not any(costs.values()) else "explicit_cost",
        "candidate_measurement": "next_market_session_close_to_window_close",
        "population_kind": population_kind,
        "evaluation_version": evaluation_version,
        "population_identity": dict(population_identity or {}),
    }
    identity_payload = dict(payload)
    identity_payload.pop("population_identity", None)
    canonical = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload["contract_id"] = hashlib.sha256(canonical).hexdigest()[:16]
    return payload
