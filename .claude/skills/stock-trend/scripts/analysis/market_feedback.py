"""Pure analysis for next-session feedback on frozen strong-stock cohorts."""

import copy
import hashlib
import json
import math
from datetime import date, datetime, time
from statistics import median
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "market-feedback/v1"
RULE_VERSION = "strong-stock-next-session-feedback/v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
GROUPS = ("limit_up", "height_core", "core")


def canonical_digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, str) and parsed.isoformat() == value else None


def aware_datetime(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def feedback_mode(basis_date, as_of):
    cutoff = aware_datetime(as_of)
    if not valid_date(basis_date) or cutoff is None:
        raise ValueError("basis_date and aware as_of are required")
    completed = datetime.combine(
        date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    return "close" if cutoff.astimezone(SHANGHAI) >= completed else "intraday"


def _member_rows(cohort):
    rows = cohort.get("members") if isinstance(cohort, dict) else None
    if not isinstance(rows, list):
        return []
    result = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        roles = list(dict.fromkeys(
            str(role) for role in raw.get("roles") or [] if role in GROUPS))
        result.append({
            "code": code,
            "name": str(raw.get("name") or code),
            "roles": roles,
            "streak": finite(raw.get("streak")),
            "selection_reason": str(raw.get("selection_reason") or ""),
        })
    return result


def _quote_index(quotes, basis_date, cutoff, mode):
    result = {}
    session_start = datetime.combine(date.fromisoformat(basis_date), time(0, 0), SHANGHAI)
    close_at = datetime.combine(date.fromisoformat(basis_date), time(15, 10), SHANGHAI)
    for raw in quotes or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        fetched = aware_datetime(raw.get("fetched_at"))
        if (not code or code in result or raw.get("data_date") != basis_date
                or not raw.get("source")
                or fetched is None or fetched > cutoff
                or fetched.astimezone(SHANGHAI) < session_start
                or (mode == "close" and (raw.get("mode") == "intraday"
                                          or fetched.astimezone(SHANGHAI) < close_at))):
            continue
        result[code] = raw
    return result


def _summary(rows):
    total = len(rows)
    values = [row["pct_chg"] for row in rows
              if row["feedback"] in ("up", "down", "flat")]
    counts = {key: sum(row["feedback"] == key for row in rows)
              for key in ("missing", "suspended", "up", "down", "flat")}
    available = len(values)
    observed = total - counts["missing"]
    return {
        "total": total,
        "available": available,
        **counts,
        "mean_pct_chg": sum(values) / available if available else None,
        "median_pct_chg": median(values) if available else None,
        "positive_ratio": counts["up"] / available if available else None,
        "negative_ratio": counts["down"] / available if available else None,
        "coverage": available / total if total else 0.0,
        "observed_coverage": observed / total if total else 0.0,
        "worst_pct_chg": min(values) if values else None,
    }


def build_feedback(cohort, quotes, *, basis_date, as_of):
    """Left join today's eligible quote onto every frozen prior member.

    ``pct_chg`` is the provider's session change, not an executable return.
    A suspended row is observed only when the provider says so explicitly.
    """
    cutoff = aware_datetime(as_of)
    if not valid_date(basis_date) or cutoff is None:
        raise ValueError("basis_date and aware as_of are required")
    members = _member_rows(cohort)
    mode = feedback_mode(basis_date, cutoff)
    quote_by_code = _quote_index(quotes, basis_date, cutoff, mode)
    rows = []
    for member in members:
        quote = quote_by_code.get(member["code"])
        feedback = "missing"
        pct_chg = close = volume = None
        source = fetched_at = None
        if quote is not None:
            status = str(quote.get("status") or "").lower()
            source = quote.get("source")
            fetched_at = quote.get("fetched_at")
            if status == "suspended":
                feedback = "suspended"
            elif status in ("ok", "normal", "trading", "active", "close"):
                pct_chg = finite(quote.get("pct_chg"))
                close = finite(quote.get("close"))
                volume = finite(quote.get("volume"))
                if (pct_chg is not None and close is not None and close > 0
                        and volume is not None and volume >= 0):
                    feedback = "up" if pct_chg > 0 else "down" if pct_chg < 0 else "flat"
                else:
                    pct_chg = close = volume = None
        rows.append({
            **member,
            "feedback": feedback,
            "pct_chg": pct_chg,
            "close": close,
            "volume": volume,
            "quote_source": source,
            "quote_fetched_at": fetched_at,
        })
    groups = {
        role: _summary([row for row in rows if role in row["roles"]])
        for role in GROUPS
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "basis_date": basis_date,
        "cohort_date": cohort.get("basis_date") if isinstance(cohort, dict) else None,
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "measurement": "provider_session_pct_change_not_executable_return",
        "members": rows,
        "summary": _summary(rows),
        "groups": groups,
        "notes": [
            "一字涨停或一字跌停仅按行情反馈统计，不代表可买入或可卖出。",
            "core仅来自显式冻结的市场核心；height_core仅表示最高连板高度样本。",
        ],
    }
    result["content_sha256"] = canonical_digest(result)
    return result


def compare_close_feedback(current, previous):
    """Compare two complete adjacent close artifacts using stable rules."""
    result = {
        "status": "unavailable", "reasons": [],
        "median_pct_chg_change": None,
        "positive_ratio_change_pp": None,
    }
    if not isinstance(current, dict) or not isinstance(previous, dict):
        result["reasons"] = ["adjacent_close_feedback_missing"]
        return result
    if current.get("mode") != "close" or previous.get("mode") != "close":
        result["reasons"] = ["close_feedback_required"]
        return result
    if current.get("rule_version") != RULE_VERSION or previous.get("rule_version") != RULE_VERSION:
        result["reasons"] = ["feedback_rule_mismatch"]
        return result
    current_summary = current.get("summary") or {}
    previous_summary = previous.get("summary") or {}
    if current_summary.get("coverage") != 1 or previous_summary.get("coverage") != 1:
        result["reasons"] = ["complete_coverage_required"]
        return result
    current_median = finite(current_summary.get("median_pct_chg"))
    previous_median = finite(previous_summary.get("median_pct_chg"))
    current_positive = finite(current_summary.get("positive_ratio"))
    previous_positive = finite(previous_summary.get("positive_ratio"))
    if None in (current_median, previous_median, current_positive, previous_positive):
        result["reasons"] = ["comparable_metrics_missing"]
        return result
    result.update({
        "status": "comparable",
        "median_pct_chg_change": current_median - previous_median,
        "positive_ratio_change_pp": (current_positive - previous_positive) * 100,
    })
    return result


def digest_valid(value):
    if not isinstance(value, dict):
        return False
    expected = value.get("content_sha256")
    content = copy.deepcopy(value)
    content.pop("content_sha256", None)
    return bool(expected and expected == canonical_digest(content))
