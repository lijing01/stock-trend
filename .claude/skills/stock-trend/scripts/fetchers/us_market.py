"""Bounded Yahoo Finance daily-price fetcher for the US-market summary.

The public entry point runs yfinance in a disposable subprocess so a stalled
request or an optional-dependency failure cannot block the daily review.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import subprocess
import sys
from typing import Any, Iterable


PROVIDER = "yfinance/Yahoo Finance"
_RESULT_PREFIX = "__STOCK_TREND_US_MARKET_JSON__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_result(symbols: Iterable[str], reason: str) -> dict[str, Any]:
    normalized = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols
                                    if str(symbol).strip()))
    return {
        "provider": PROVIDER,
        "fetched_at": _now_iso(),
        "rows": {symbol: [] for symbol in normalized},
        "errors": {symbol: reason for symbol in normalized},
    }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_date(value: Any) -> str | None:
    try:
        parsed = value.date() if hasattr(value, "date") else date.fromisoformat(str(value)[:10])
        return parsed.isoformat()
    except (TypeError, ValueError, AttributeError):
        return None


def _symbol_frame(frame: Any, symbol: str, symbol_count: int) -> Any | None:
    """Select one ticker from yfinance's version-dependent column layout."""
    columns = getattr(frame, "columns", None)
    if columns is None:
        return None

    nlevels = getattr(columns, "nlevels", 1)
    if nlevels > 1:
        for level in range(nlevels):
            try:
                values = {str(value).upper() for value in columns.get_level_values(level)}
                if symbol in values:
                    return frame.xs(symbol, axis=1, level=level, drop_level=True)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    # A flat frame is unambiguous only for a one-symbol request.
    return frame if symbol_count == 1 else None


def _frame_to_rows(
    frame: Any,
    symbols: Iterable[str],
    start: date,
    end: date,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Normalize yfinance DataFrames without assuming a MultiIndex orientation."""
    requested = list(symbols)
    rows: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in requested}
    errors: dict[str, str] = {}

    if frame is None or bool(getattr(frame, "empty", True)):
        return rows, {symbol: "Yahoo Finance returned no data" for symbol in requested}

    for symbol in requested:
        selected = _symbol_frame(frame, symbol, len(requested))
        if selected is None or bool(getattr(selected, "empty", True)):
            errors[symbol] = "Yahoo Finance returned no data for symbol"
            continue

        by_date: dict[str, dict[str, Any]] = {}
        duplicate_date: str | None = None
        for index, record in selected.iterrows():
            session_date = _iso_date(index)
            if session_date is None or not (start.isoformat() <= session_date <= end.isoformat()):
                continue
            if session_date in by_date:
                duplicate_date = session_date
                break
            by_date[session_date] = {
                "date": session_date,
                "close": _finite_number(record.get("Close")),
                "adj_close": _finite_number(record.get("Adj Close")),
                "dividends": _finite_number(record.get("Dividends")),
                "stock_splits": _finite_number(record.get("Stock Splits")),
            }

        if duplicate_date is not None:
            rows[symbol] = []
            errors[symbol] = f"duplicate_date: {duplicate_date}"
            continue

        rows[symbol] = [by_date[key] for key in sorted(by_date)]
        if not rows[symbol]:
            errors[symbol] = "Yahoo Finance returned no usable rows for date range"

    return rows, errors


def _worker_fetch(request: dict[str, Any]) -> dict[str, Any]:
    symbols = request["symbols"]
    start = date.fromisoformat(request["start"])
    end = date.fromisoformat(request["end"])
    request_timeout = max(1.0, float(request.get("request_timeout", 10)))

    try:
        import yfinance as yf
    except (ImportError, ModuleNotFoundError) as exc:
        return _empty_result(symbols, f"yfinance unavailable: {exc}")

    try:
        frame = yf.download(
            tickers=symbols,
            start=start.isoformat(),
            # yfinance treats end as exclusive; the public API is inclusive.
            end=(end + timedelta(days=1)).isoformat(),
            actions=True,
            auto_adjust=False,
            prepost=False,
            group_by="ticker",
            threads=min(4, max(1, len(symbols))),
            progress=False,
            timeout=request_timeout,
        )
        rows, errors = _frame_to_rows(frame, symbols, start, end)
        return {
            "provider": PROVIDER,
            "fetched_at": _now_iso(),
            "rows": rows,
            "errors": errors,
        }
    except Exception as exc:  # yfinance surfaces several backend exception types
        return _empty_result(symbols, f"Yahoo Finance request failed: {exc}")


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    if isinstance(symbols, (str, bytes)):
        symbols = [symbols]
    return list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols
                              if str(symbol).strip()))


def _parse_worker_output(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(_RESULT_PREFIX):
            payload = json.loads(line[len(_RESULT_PREFIX):])
            if not isinstance(payload, dict):
                raise ValueError("worker payload is not an object")
            return payload
    raise ValueError("worker result marker missing")


def fetch_us_market(
    symbols: Iterable[str],
    start: str | date,
    end: str | date,
    timeout: float = 25,
) -> dict[str, Any]:
    """Fetch inclusive daily history with a hard wall-time budget.

    Returns provider/fetched_at/rows/errors for every normalized symbol. Bad or
    unavailable numeric values remain ``None``; this function never fabricates
    prices or corporate actions.
    """
    normalized = _normalize_symbols(symbols)
    try:
        start_date = start if isinstance(start, date) else date.fromisoformat(str(start))
        end_date = end if isinstance(end, date) else date.fromisoformat(str(end))
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout must be positive")
        if start_date > end_date:
            raise ValueError("start must be on or before end")
    except (TypeError, ValueError) as exc:
        return _empty_result(normalized, f"invalid request: {exc}")

    if not normalized:
        return _empty_result([], "no symbols requested")

    request = {
        "symbols": normalized,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        # Leave cleanup/serialization time inside the parent wall budget.
        "request_timeout": max(1.0, min(10.0, timeout_value - 1.0)),
    }
    try:
        completed = subprocess.run(
            [sys.executable, __file__, "--worker"],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_value,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _empty_result(normalized, f"Yahoo Finance fetch timed out after {timeout_value:g}s")
    except (OSError, ValueError) as exc:
        return _empty_result(normalized, f"Yahoo Finance worker failed: {exc}")

    try:
        payload = _parse_worker_output(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = (completed.stderr or "").strip().splitlines()
        suffix = f": {detail[-1][:200]}" if detail else ""
        return _empty_result(normalized, f"Yahoo Finance worker returned invalid output{suffix}")

    payload["provider"] = PROVIDER
    payload.setdefault("fetched_at", _now_iso())
    worker_rows = payload.get("rows") if isinstance(payload.get("rows"), dict) else {}
    worker_errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    payload["rows"] = {symbol: worker_rows.get(symbol, []) for symbol in normalized}
    payload["errors"] = {
        symbol: str(worker_errors.get(symbol, "Yahoo Finance returned no data for symbol"))
        for symbol in normalized if not payload["rows"][symbol] or symbol in worker_errors
    }
    return payload


def _worker_main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        payload = _worker_fetch(request)
    except Exception as exc:
        symbols = request.get("symbols", []) if isinstance(locals().get("request"), dict) else []
        payload = _empty_result(symbols, f"Yahoo Finance worker failed: {exc}")
    print(_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch bounded US daily market data")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return _worker_main()
    parser.error("this module is intended to be imported")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
