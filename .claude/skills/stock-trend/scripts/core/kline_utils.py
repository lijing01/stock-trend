#!/usr/bin/env python3
"""Pure K-line payload helpers and fetcher command construction.

This module deliberately does not perform I/O, networking, retries, or provider
selection.  Callers keep ownership of cache policy and provider orchestration.
"""

from datetime import datetime
from pathlib import Path
import sys

from core.cache_utils import safe_float
from core.eastmoney_utils import latest_kline_record


_FETCHER_SCRIPTS = {
    "tushare": "kline.py",
    "eastmoney": "kline_eastmoney.py",
    "em": "kline_eastmoney.py",
}


def normalize_kline_date(value):
    """Return a valid K-line date as ``YYYY-MM-DD``, or an empty string."""
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def latest_kline_date(payload):
    """Return the newest valid bar date as ``YYYY-MM-DD``, or ``""``."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return ""
    dates = [
        normalize_kline_date(row.get("trade_date") or row.get("date"))
        for row in rows
        if isinstance(row, dict)
    ]
    valid_dates = [value for value in dates if value]
    return max(valid_dates) if valid_dates else ""


def validate_kline_coverage(payload, expected_date):
    """Describe whether a K-line payload covers the requested trading day."""
    latest_date = latest_kline_date(payload)
    expected_key = normalize_kline_date(expected_date)
    valid = bool(latest_date)
    if expected_date:
        valid = bool(latest_date and expected_key and latest_date >= expected_key)
    return {
        "expected_date": expected_date,
        "latest_date": latest_date,
        "valid": valid,
    }


def cache_validation(payload, expected_date):
    """Compatibility name for :func:`validate_kline_coverage`."""
    return validate_kline_coverage(payload, expected_date)


def reject_stale_kline_payload(result, expected_date):
    """Return an explicit error payload when fresh data misses expected_date."""
    validation = validate_kline_coverage(result, expected_date)
    result.setdefault("meta", {})["cache_validation"] = validation
    if validation["valid"]:
        return result

    meta = dict(result.get("meta", {}))
    stale_source = meta.get("data_source", "unknown")
    meta.update({
        "data_source": "error",
        "error_type": "stale_data",
        "stale_data_source": stale_source,
        "record_count": 0,
        "error": (
            f"数据最新日期{validation['latest_date'] or '未知'}早于"
            f"预期交易日{expected_date}"
        ),
        "cache_validation": validation,
    })
    return {"meta": meta, "data": []}


def reject_stale_payload(result, expected_date):
    """Compatibility name for :func:`reject_stale_kline_payload`."""
    return reject_stale_kline_payload(result, expected_date)


def is_usable_kline_payload(kline_data, require_ohlc=True):
    """Return whether a K-line payload is structurally usable by the pipeline."""
    if not isinstance(kline_data, dict):
        return False
    meta = kline_data.get("meta", {})
    if not isinstance(meta, dict):
        return False
    if meta.get("data_source") == "error":
        return False
    cache_state = meta.get("cache_validation", {})
    if isinstance(cache_state, dict) and cache_state.get("valid") is False:
        return False
    rows = kline_data.get("data")
    if not isinstance(rows, list) or not rows:
        return False
    if not require_ohlc:
        return True
    latest_row = latest_kline_record(rows)
    if latest_row is None:
        return False
    return all(
        safe_float(latest_row.get(key)) is not None
        for key in ("open", "high", "low", "close")
    )


def build_kline_fetch_command(
    provider,
    ts_code,
    output_path,
    *,
    asset,
    freq,
    adj=None,
    no_cache=False,
    expected_date=None,
    start_date=None,
    limit=None,
    provider_args=None,
    python_executable=sys.executable,
):
    """Build a deterministic argv for one of the K-line fetcher scripts.

    ``provider_args`` is intentionally an explicit passthrough for existing
    provider-specific flags (for example today's recommendation timeouts).
    The helper does not infer or add retry/fallback behavior.
    """
    try:
        script_name = _FETCHER_SCRIPTS[provider.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"Unsupported K-line provider: {provider!r}") from exc

    scripts_dir = Path(__file__).resolve().parent.parent
    command = [
        str(python_executable),
        str(scripts_dir / "fetchers" / script_name),
        str(ts_code),
        "--asset", str(asset),
        "--freq", str(freq),
    ]
    if adj is not None:
        command.extend(["--adj", str(adj)])
    if provider_args:
        command.extend(str(value) for value in provider_args)
    command.extend(["-o", str(output_path)])
    if no_cache:
        command.append("--no-cache")
    if expected_date:
        command.extend(["--expected-date", str(expected_date)])
    if start_date:
        command.extend(["--start-date", str(start_date)])
    if limit is not None:
        command.extend(["--lmt", str(limit)])
    return command
