"""Batch adapter for caller-supplied market data; no network access."""
from __future__ import annotations

from typing import Iterable, Mapping

from .market import classify_v4_market_results
from .v4_algorithm import analyze_daily_weak_to_strong_v4
from .v4_shadow_scoring import validate_v4_shadow_bundle
from .weak_to_strong import analyze_daily_weak_to_strong
from .weak_to_strong_v3 import analyze_daily_weak_to_strong_v3


def scan_market(
    stocks: Iterable[Mapping[str, object]], *, as_of_date: str,
    bundle: Mapping[str, object] | None,
    calendar_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Evaluate collected bars and return research candidates and observations.

    Each stock supplies code, kline_payload, and optionally name and
    close_evidence. The caller owns universe coverage and data provenance.
    A missing or invalid bundle is reported while V3 structures are evaluated.
    """
    model_error = None
    if bundle is None:
        model_error = "model_missing"
    else:
        try:
            validate_v4_shadow_bundle(bundle)
        except (ValueError, TypeError, KeyError):
            model_error = "model_invalid"
    results = []
    for stock in stocks:
        code = str(stock.get("code") or "").strip()
        if not code:
            raise ValueError("each stock requires a code")
        payload = stock.get("kline_payload")
        close = stock.get("close_evidence")
        name = str(stock.get("name") or "")
        if model_error is None:
            result = analyze_daily_weak_to_strong_v4(
                payload, bundle=bundle, as_of_date=as_of_date, code=code,
                name=name, close_evidence=close, calendar_evidence=calendar_evidence,
            )
        else:
            v1 = analyze_daily_weak_to_strong(
                payload, as_of_date=as_of_date, close_evidence=close,
                calendar_evidence=calendar_evidence,
            )
            v3 = analyze_daily_weak_to_strong_v3(
                payload, as_of_date=as_of_date, code=code, name=name,
                close_evidence=close, calendar_evidence=calendar_evidence,
                v1_result=v1,
            )
            result = {"status": "fallback", "reason": model_error,
                      "as_of_date": as_of_date, "symbol": code,
                      "prediction": None, "v3_result": v3}
        results.append(result)
    report = classify_v4_market_results(results)
    report["model_status"] = model_error or "loaded"
    report["requested_as_of_date"] = as_of_date
    return report
