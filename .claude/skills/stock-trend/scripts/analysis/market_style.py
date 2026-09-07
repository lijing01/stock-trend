"""Experimental market-style observer kept outside the formal regime model."""

import argparse
import copy
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

STYLE_DEFINITIONS = {
    "hs300": {"code": "000300.SH", "name": "沪深300"},
    "csi500": {"code": "000905.SH", "name": "中证500"},
    "csi1000": {"code": "000852.SH", "name": "中证1000"},
    "chinext": {"code": "399006.SZ", "name": "创业板指"},
    "star50": {"code": "000688.SH", "name": "科创50"},
}
STYLE_TIMEOUT_SECONDS = 90
STYLE_INDEX_TIMEOUT_SECONDS = 30


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_text(value):
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _sma(values, period):
    result = [None] * len(values)
    for index in range(period - 1, len(values)):
        result[index] = sum(values[index + 1 - period:index + 1]) / period
    return result


def _bucket(close, ma20, ma20_previous):
    rising = ma20_previous is None or ma20 > ma20_previous
    above = close > ma20
    if above and rising:
        return 100, "strong"
    if above:
        return 60, "mixed"
    if rising:
        return 40, "mixed"
    return 0, "weak"


def _unknown_style(definition, reasons, basis_date, sample_count=0,
                   source=None):
    source = source or {}
    return {
        "style_id": next(
            key for key, item in STYLE_DEFINITIONS.items()
            if item is definition),
        "code": definition["code"],
        "name": definition["name"],
        "score": None,
        "status": "unknown",
        "reasons": list(dict.fromkeys(reasons)),
        "metrics": {
            "close": None,
            "ma20": None,
            "ma20_distance_pct": None,
            "ma20_slope_pct": None,
            "return_5d": None,
            "return_20d": None,
            "sample_count": sample_count,
        },
        "evidence": {
            "basis_date": basis_date,
            "last_date": None,
            "sample_count": sample_count,
            "source": source.get("source", "unknown"),
            "provider": source.get("provider", source.get("source", "unknown")),
            "usage": "unavailable",
        },
    }


def _style_observation(style_id, definition, basis_date, rows, source):
    reasons = []
    raw_rows = list(rows or [])
    normalized = []
    seen = set()
    for row in raw_rows:
        if not isinstance(row, dict):
            reasons.append("invalid_row")
            continue
        trade_date = _date_text(row.get("trade_date") or row.get("date"))
        close = _finite(row.get("close"))
        if trade_date is None:
            reasons.append("invalid_trade_date")
            continue
        if trade_date in seen:
            reasons.append("duplicate_trade_date")
            continue
        seen.add(trade_date)
        if trade_date > basis_date:
            reasons.append("future_record")
            continue
        if close is None or close <= 0:
            reasons.append("non_finite_close" if close is None else "invalid_close")
            continue
        normalized.append((trade_date, close))

    normalized.sort(key=lambda item: item[0])
    if "duplicate_trade_date" in reasons:
        return _unknown_style(definition, reasons, basis_date,
                              len(normalized), source)
    if len(normalized) < 21:
        reasons.append("insufficient_valid_rows")
    if not normalized or normalized[-1][0] != basis_date:
        reasons.append("basis_date_not_last")
    if len(normalized) < 21 or not normalized or normalized[-1][0] != basis_date:
        return _unknown_style(definition, reasons, basis_date,
                              len(normalized), source)

    dates = [item[0] for item in normalized]
    closes = [item[1] for item in normalized]
    ma20_series = _sma(closes, 20)
    ma20 = ma20_series[-1]
    ma20_previous = ma20_series[-6] if len(ma20_series) >= 6 else None
    score, status = _bucket(closes[-1], ma20, ma20_previous)
    distance = closes[-1] / ma20 - 1.0
    slope = None
    if ma20_previous and ma20_previous > 0:
        slope = ma20 / ma20_previous - 1.0
    else:
        reasons.append("insufficient_ma20_slope_history")
    if len(closes) < 25:
        reasons.append("short_slope_history")

    metrics = {
        "close": round(closes[-1], 6),
        "ma20": round(ma20, 6),
        "ma20_distance_pct": round(distance * 100, 6),
        "ma20_slope_pct": round(slope * 100, 6) if slope is not None else None,
        "return_5d": round(closes[-1] / closes[-6] - 1.0, 6),
        "return_20d": round(closes[-1] / closes[-21] - 1.0, 6),
        "sample_count": len(closes),
    }
    return {
        "style_id": style_id,
        "code": definition["code"],
        "name": definition["name"],
        "score": score,
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "metrics": metrics,
        "evidence": {
            "basis_date": basis_date,
            "last_date": dates[-1],
            "sample_count": len(closes),
            "source": source.get("source", "unknown"),
            "provider": source.get("provider", source.get("source", "unknown")),
            "usage": "scorable",
        },
    }


def compute_style_observations(context, rows_by_code):
    """Compute all five style observations without I/O or mutation."""
    context = copy.deepcopy(context or {})
    rows_by_code = rows_by_code or {}
    basis_date = _date_text(context.get("basis_date") or context.get("data_date"))
    if basis_date is None:
        styles = {
            definition["code"]: _unknown_style(
                definition, ["invalid_basis_date"], None)
            for definition in STYLE_DEFINITIONS.values()
        }
        return {
            "schema_version": "market-style-shadow/v1",
            "model_version": "style-ma20/v1",
            "parameter_version": "style-observer/v1",
            "basis_date": None,
            "snapshot_type": "provisional" if context.get("intraday")
            else "formal",
            "styles": styles,
            "status": "missing",
            "formal_policy_affected": False,
        }

    styles = {}
    source_quality = context.get("index_data_quality") or {}
    for style_id, definition in STYLE_DEFINITIONS.items():
        source = source_quality.get(definition["code"], {})
        styles[definition["code"]] = _style_observation(
            style_id, definition, basis_date,
            rows_by_code.get(definition["code"], []), source)
    available = sum(item["score"] is not None for item in styles.values())
    status = "complete" if available == len(styles) else (
        "partial" if available else "missing")
    return {
        "schema_version": "market-style-shadow/v1",
        "model_version": "style-ma20/v1",
        "parameter_version": "style-observer/v1",
        "basis_date": basis_date,
        "snapshot_type": "provisional" if context.get("intraday") else "formal",
        "styles": styles,
        "status": status,
        "formal_policy_affected": False,
    }


_MEMBER_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|HK)$")


def _normalise_member_code(value):
    text = str(value or "").strip().upper()
    if text.isdigit() and len(text) == 6:
        return f"{text}.{'SH' if text.startswith('6') else 'SZ'}"
    return text


def match_candidate_styles(code, records, basis_time):
    """Match only time-valid, auditable index memberships for one candidate."""
    basis_date = _date_text(basis_time)
    normalized_code = _normalise_member_code(code)
    result = {
        "code": code,
        "status": "unknown",
        "index_codes": [],
        "records": [],
        "reasons": [],
    }
    if not _MEMBER_CODE_RE.fullmatch(normalized_code):
        result["reasons"].append("candidate_code_format_invalid")
        return result
    if basis_date is None:
        result["reasons"].append("invalid_basis_date")
        return result

    valid = []
    for record in records or []:
        if not isinstance(record, dict):
            result["reasons"].append("invalid_membership_record")
            continue
        record_member_code = _normalise_member_code(record.get("member_code"))
        if record_member_code != normalized_code:
            continue
        index_code = record.get("index_code")
        if index_code not in {item["code"] for item in STYLE_DEFINITIONS.values()}:
            result["reasons"].append("index_code_format_invalid")
            continue
        if not _MEMBER_CODE_RE.fullmatch(record_member_code):
            result["reasons"].append("member_code_format_invalid")
        if not record.get("source"):
            result["reasons"].append("source_missing")
        known_at = _date_text(record.get("known_at"))
        effective_from = _date_text(record.get("effective_from"))
        effective_to = _date_text(record.get("effective_to")) \
            if record.get("effective_to") else None
        if known_at is None:
            result["reasons"].append("known_at_invalid")
        elif known_at > basis_date:
            result["reasons"].append("known_at_future")
        if effective_from is None:
            result["reasons"].append("effective_from_invalid")
        if record.get("effective_to") and effective_to is None:
            result["reasons"].append("effective_to_invalid")
        covers = (
            effective_from is not None
            and effective_from <= basis_date
            and (effective_to is None or basis_date <= effective_to)
        )
        if not covers:
            result["reasons"].append("effective_window_miss")
        if (known_at is None or known_at > basis_date
                or effective_from is None or not covers
                or not _MEMBER_CODE_RE.fullmatch(record_member_code)
                or not record.get("source")):
            continue
        valid.append({
            "index_code": index_code,
            "member_code": normalized_code,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "known_at": known_at,
            "source": record["source"],
        })

    by_index = {}
    for item in valid:
        by_index.setdefault(item["index_code"], []).append(item)
    if any(len(items) > 1 for items in by_index.values()):
        result["reasons"].append("effective_interval_conflict")
        valid = []
    if valid:
        result["status"] = "ready"
        result["records"] = valid
        result["index_codes"] = [
            definition["code"] for definition in STYLE_DEFINITIONS.values()
            if definition["code"] in by_index
        ]
    elif not result["reasons"]:
        result["reasons"].append("no_membership_evidence")
    result["reasons"] = list(dict.fromkeys(result["reasons"]))
    return result


def _shadow_bucket(status, legacy_bucket):
    """Return a display-only style bucket without changing formal policy."""
    if status in {"strong", "mixed", "weak"}:
        return legacy_bucket
    return legacy_bucket


def _candidate_style_state(statuses):
    statuses = list(statuses or [])
    if not statuses or any(status not in {"strong", "mixed", "weak"}
                           for status in statuses):
        return "unknown"
    if all(status == "strong" for status in statuses):
        return "strong"
    if all(status == "weak" for status in statuses):
        return "weak"
    return "mixed"


def annotate_candidates_for_shadow(candidates, buckets, shadow, memberships):
    """Attach read-only style diagnostics to report copies of candidates.

    The input lists and bucket membership are never mutated.  The returned
    bucket names remain exactly the formal classifier's bucket names; style
    observations are deliberately diagnostic and cannot change recommendations.
    """
    shadow = shadow if isinstance(shadow, dict) else {}
    style_by_code = shadow.get("styles") or {}
    membership_records = memberships
    if isinstance(memberships, dict):
        membership_records = memberships.get("records", [])
    membership_records = membership_records if isinstance(membership_records, list) else []
    basis_date = shadow.get("basis_date")

    def annotate(row, legacy_bucket):
        item = copy.deepcopy(row)
        code = item.get("code") if isinstance(item, dict) else None
        membership = match_candidate_styles(code, membership_records, basis_date)
        styles = [style_by_code.get(index_code, {})
                  for index_code in membership.get("index_codes", [])]
        statuses = [style.get("status") for style in styles
                    if isinstance(style, dict)]
        state = _candidate_style_state(statuses)
        reasons = list(membership.get("reasons", []))
        if membership.get("status") != "ready":
            reasons.append("membership_unavailable")
        missing_style = [index_code for index_code in membership.get("index_codes", [])
                         if not isinstance(style_by_code.get(index_code), dict)
                         or style_by_code[index_code].get("score") is None]
        if missing_style:
            reasons.append("style_observation_unavailable")
        item["style_shadow"] = {
            "matched_style_state": state,
            "membership": copy.deepcopy(membership),
            "styles": [copy.deepcopy(style_by_code[index_code])
                        for index_code in membership.get("index_codes", [])
                        if isinstance(style_by_code.get(index_code), dict)],
            "legacy_bucket": legacy_bucket,
            "shadow_bucket": _shadow_bucket(state, legacy_bucket),
            "action_changed": False,
            "reasons": list(dict.fromkeys(reasons)),
        }
        return item

    bucket_by_code = {}
    for bucket_name, rows in (buckets or {}).items():
        for row in rows or []:
            if isinstance(row, dict) and row.get("code"):
                bucket_by_code.setdefault(row["code"], bucket_name)
    annotated_candidates = [
        annotate(row, bucket_by_code.get(row.get("code"))
                 if isinstance(row, dict) else None)
        for row in (candidates or [])
    ]
    annotated_buckets = {}
    for bucket_name, rows in (buckets or {}).items():
        annotated_buckets[bucket_name] = [annotate(row, bucket_name)
                                           for row in (rows or [])]
    return annotated_candidates, annotated_buckets


def fetch_style_rows(context, fetcher=None, total_timeout=STYLE_TIMEOUT_SECONDS,
                     per_index_timeout=STYLE_INDEX_TIMEOUT_SECONDS):
    """Fetch five indices with a bounded two-worker fan-out."""
    if fetcher is None:
        from analysis.market_regime import fetch_index_kline
        fetcher = fetch_index_kline
    rows_by_code = {}
    diagnostics = {}
    pool = ThreadPoolExecutor(max_workers=2)
    future_to_code = {
        pool.submit(fetcher, definition["code"], lmt=80): definition["code"]
        for definition in STYLE_DEFINITIONS.values()
    }
    started = time.monotonic()
    try:
        for future in as_completed(
                future_to_code, timeout=max(0.001, total_timeout)):
            code = future_to_code[future]
            elapsed = time.monotonic() - started
            if elapsed > total_timeout:
                diagnostics[code] = {"source": "timeout", "reason": "total_timeout"}
                continue
            try:
                rows = future.result(timeout=per_index_timeout)
                rows_by_code[code] = rows or []
                diagnostics[code] = {
                    "source": "live", "record_count": len(rows or []),
                }
            except Exception as exc:
                diagnostics[code] = {
                    "source": "error", "reason": type(exc).__name__,
                }
    except TimeoutError:
        for future, code in future_to_code.items():
            if not future.done():
                future.cancel()
                diagnostics[code] = {"source": "timeout", "reason": "total_timeout"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return rows_by_code, diagnostics


def build_style_run(context, rows_by_code=None, fetcher=None,
                    total_timeout=STYLE_TIMEOUT_SECONDS,
                    per_index_timeout=STYLE_INDEX_TIMEOUT_SECONDS):
    legacy_context = copy.deepcopy(context or {})
    context = copy.deepcopy(legacy_context)
    supplied_rows = rows_by_code is not None
    if rows_by_code is None:
        rows_by_code, diagnostics = fetch_style_rows(
            context, fetcher=fetcher, total_timeout=total_timeout,
            per_index_timeout=per_index_timeout)
        context["index_data_quality"] = diagnostics
    rows_by_code = copy.deepcopy(rows_by_code or {})
    result = compute_style_observations(context, rows_by_code)
    from core.recommendation_snapshot import content_sha256
    result["legacy_context_sha256"] = content_sha256(legacy_context)
    result["index_data_quality"] = copy.deepcopy(
        context.get("index_data_quality") or {})
    result["raw_inputs"] = rows_by_code
    result["input_mode"] = "supplied" if supplied_rows else "fetched"
    result["fetched_at"] = time.time()
    return result


def _load_context(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("context must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Market style shadow observer")
    parser.add_argument("--context", required=True, help="market_regime.json")
    parser.add_argument("--json", action="store_true", help="JSON stdout")
    parser.add_argument(
        "--save", action="store_true",
        help="persist an immutable run under market_shadow_history")
    args = parser.parse_args(argv)
    try:
        context = _load_context(args.context)
        result = build_style_run(context)
    except Exception as exc:
        print(f"style shadow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.save:
        from core.market_shadow_snapshot import save_shadow_run
        try:
            persisted = save_shadow_run(result)
            result["persistence"] = {
                "status": persisted.status,
                "path": persisted.path,
                "content_sha256": persisted.content_sha256,
                "reason": persisted.reason,
            }
        except Exception as exc:
            result["persistence"] = {
                "status": "write_failed",
                "path": None,
                "content_sha256": None,
                "reason": f"{type(exc).__name__}: {exc}",
            }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
