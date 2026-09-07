"""Historical evaluation of immutable recommendation snapshots.

The evaluator deliberately keeps provider metadata and immutable snapshot
identity next to calculated windows so a later run cannot silently change the
meaning of an earlier result.
"""

import argparse
import copy
import dataclasses
import inspect
import json
import math
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core.cache_utils import CACHE_DIR
from core.evolution_contract import (
    PRIMARY_WINDOW, build_evaluation_contract,
)
from core.recommendation_snapshot import iter_official_snapshots


WINDOWS = (5, 10, 20, 60)
EVALUATOR_VERSION = "recommendation-attribution/v4"
CANDIDATE_EVALUATOR_VERSION = "candidate-signal-performance/v2"


class AttributionDataError(ValueError):
    """An expected provider/data-contract failure that can be retried."""


@dataclasses.dataclass(frozen=True)
class CostModel:
    buy_commission_bps: float = 0
    sell_commission_bps: float = 0
    buy_slippage_bps: float = 0
    sell_slippage_bps: float = 0
    sell_tax_bps: float = 0

    def __post_init__(self):
        if any(not math.isfinite(x) or x < 0 for x in dataclasses.astuple(self)):
            raise ValueError("cost bps must be finite and non-negative")

    @property
    def mode(self):
        return "gross" if not any(dataclasses.astuple(self)) else "explicit_cost"


def normalize_trade_date(value):
    """Return an ISO date for either provider or snapshot date formats."""
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        except ValueError as exc:
            raise AttributionDataError("invalid_trade_date") from exc
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise AttributionDataError("invalid_trade_date") from exc


def _dt(value):
    return date.fromisoformat(normalize_trade_date(value))


def _row_date(row):
    raw = row.get("date") or row.get("trade_date")
    if not raw:
        return ""
    try:
        return normalize_trade_date(raw)
    except AttributionDataError:
        return ""


def _normalized_sessions(values):
    result = []
    for value in values or []:
        result.append(normalize_trade_date(value))
    return sorted(set(result))


def _rows_by_date(rows):
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    return {
        d: row for row in (rows or [])
        if isinstance(row, dict) and (d := _row_date(row))
    }


def _number(row, key, default=None):
    try:
        value = float(row.get(key, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _limit_pct(code, row, security_meta=None):
    """Return the applicable A-share daily limit, or None when it is unknown.

    This is deliberately a narrow, date-aware rule table.  It is only used to
    reject a one-price board; it does not turn an OHLC bar into proof of an
    executable fill.  Explicit provider metadata wins over an inferred code.
    """
    meta = security_meta or {}
    explicit = _number(meta, "price_limit_pct", _number(row, "price_limit_pct"))
    if explicit is not None and explicit > 0:
        return explicit / 100 if explicit > 1 else explicit
    name = str(meta.get("name") or row.get("name") or "").upper()
    if "ST" in name:
        return 0.05
    text = str(code or meta.get("code") or "")
    trade_date = _row_date(row)
    if text.startswith(("300", "301")):
        return 0.20 if trade_date >= "2020-08-24" else 0.10
    if text.startswith("688"):
        return 0.20
    if text.startswith(("8", "4")):
        return 0.30 if trade_date >= "2021-11-15" else None
    if text.startswith(("0", "002", "003", "6")):
        return 0.10
    return None


def _one_price_limit(row, previous_close, code="", security_meta=None, direction="up"):
    """Return True/False/None for a one-price limit board.

    ``None`` means an otherwise one-price bar cannot be classified because
    neither the rule nor a prior close is available.  The caller must not
    silently treat that as executable.
    """
    prices = [_number(row, key) for key in ("open", "high", "low", "close")]
    if any(value is None for value in prices) or len(set(prices)) != 1:
        return False
    meta = security_meta or {}
    explicit_price = _number(
        row, "limit_up_price" if direction == "up" else "limit_down_price",
        _number(meta, "limit_up_price" if direction == "up" else "limit_down_price"),
    )
    previous = _number({}, "", previous_close)
    if previous is None or previous <= 0:
        return None
    price = prices[0]
    if explicit_price is not None and explicit_price > 0:
        return abs(price - explicit_price) <= max(0.01, explicit_price * 0.001)
    change = price / previous - 1
    # A flat or opposite-direction one-price bar is not the limit board that
    # blocks this action, even if its security rule is unavailable.
    if direction == "up" and change <= 0:
        return False
    if direction == "down" and change >= 0:
        return False
    limit = _limit_pct(code, row, security_meta)
    if limit is None:
        return None
    tolerance = 0.003
    return change >= limit - tolerance if direction == "up" else change <= -limit + tolerance


def _ret(start, end):
    return None if start in (None, 0) or end is None else end / start - 1


def _error_result(recommendation_date, code, evaluation_as_of, cost_model, windows, reason):
    costs = dataclasses.asdict(cost_model)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "recommendation_date": recommendation_date,
        "code": code,
        "evaluation_as_of": evaluation_as_of,
        "execution": {"status": "data_error", "reason": reason},
        "cost_model": costs,
        "windows": {
            str(window): {"status": "data_error", "reason": reason}
            for window in windows
        },
    }


def validate_series_metadata(meta):
    """Require the adjustment mode promised by the recommendation contract."""
    if not isinstance(meta, dict) or meta.get("adj") != "qfq":
        raise AttributionDataError("wrong_adjustment")


def resolve_entry(plan, recommendation_date, market_sessions, stock_rows, code="", security_meta=None):
    recommendation_date = normalize_trade_date(recommendation_date)
    sessions = [
        d for d in _normalized_sessions(market_sessions)
        if d > recommendation_date
    ]
    if not sessions:
        return {"status": "data_error", "reason": "market_calendar_missing"}
    entry_date = sessions[0]
    rows = _rows_by_date(stock_rows)
    row = rows.get(entry_date)
    if row is None:
        return {"status": "data_error", "reason": "t1_data_missing", "date": entry_date}
    volume = _number(row, "vol", _number(row, "volume", None))
    if volume is None:
        return {"status": "data_error", "reason": "t1_volume_missing", "date": entry_date}
    if volume <= 0:
        return {"status": "unexecutable", "reason": "t1_suspended", "date": entry_date}
    previous_sessions = [d for d in _normalized_sessions(market_sessions) if d < entry_date]
    previous_row = rows.get(previous_sessions[-1]) if previous_sessions else None
    limit_up = _one_price_limit(row, _number(previous_row or {}, "close"), code, security_meta, "up")
    if limit_up is None:
        return {"status": "data_error", "reason": "t1_limit_rule_unknown", "date": entry_date}
    if limit_up:
        return {"status": "unexecutable", "reason": "t1_one_price_limit_up", "date": entry_date}
    entry = plan.get("entry", {})
    low = _number(entry, "low", _number(entry, "price"))
    high = _number(entry, "high", low)
    stop = _number(plan.get("stop_loss", {}), "price", -math.inf)
    if low is None or high is None or stop is None:
        return {"status": "data_error", "reason": "trade_plan_invalid", "date": entry_date}
    if _number(row, "open", 0) < stop:
        return {"status": "unexecutable", "reason": "t1_open_below_stop", "date": entry_date}
    if _number(row, "low", 0) > high or _number(row, "high", 0) < low:
        return {
            "status": "unexecutable",
            "reason": "t1_entry_zone_not_reached",
            "date": entry_date,
        }
    return {
        "status": "executable",
        "date": entry_date,
        "price": min(high, max(low, _number(row, "open", low))),
        "fill_assumption": "entry_zone_touched_on_daily_bar",
    }


def _benchmark_return(series, entry, exit_date):
    rows = _rows_by_date(series)
    start = rows.get(entry)
    end = rows.get(exit_date)
    start_close = _number(start or {}, "close")
    end_close = _number(end or {}, "close")
    return _ret(start_close, end_close)


def evaluate_candidate_signal(recommendation, evaluation_as_of, market_sessions,
                              stock_rows, hs300_rows=None, sector_rows=None,
                              windows=WINDOWS, stock_meta=None):
    """Evaluate a fixed candidate sample without requiring a trade plan.

    The measurement starts at the next market session close, so it is signal
    performance rather than a claim that an entry was executable or traded.
    """
    content = recommendation.get("content", recommendation)
    recommendation_date = normalize_trade_date(content.get("recommendation_date"))
    candidate = recommendation.get("candidate") or recommendation
    code = candidate.get("code", "")
    evaluation_as_of = normalize_trade_date(evaluation_as_of)
    try:
        validate_series_metadata(stock_meta)
        sessions = _normalized_sessions(market_sessions)
    except AttributionDataError as exc:
        return _error_result(recommendation_date, code, evaluation_as_of, CostModel(), windows, str(exc))
    future = [session for session in sessions if recommendation_date < session <= evaluation_as_of]
    result = {"evaluator_version": CANDIDATE_EVALUATOR_VERSION,
              "recommendation_date": recommendation_date, "code": code,
              "evaluation_as_of": evaluation_as_of,
              "measurement": {"status": "signal_close_to_close", "entry_rule": "next_market_session_close", "trade_plan_required": False},
              "windows": {}}
    rows = _rows_by_date(stock_rows)
    if not future:
        for window in windows:
            result["windows"][str(window)] = {"status": "pending", "required_session": window}
        return result
    entry = future[0]
    entry_close = _number(rows.get(entry, {}), "close")
    if entry_close is None:
        for window in windows:
            result["windows"][str(window)] = {"status": "data_error", "reason": "t1_data_missing"}
        return result
    for window in windows:
        if len(future) < window:
            result["windows"][str(window)] = {"status": "pending", "required_session": window}
            continue
        exit_date = future[window - 1]
        ret = _ret(entry_close, _number(rows.get(exit_date, {}), "close"))
        if ret is None:
            result["windows"][str(window)] = {"status": "data_error", "reason": "historical_data_missing"}
            continue
        item = {"status": "complete", "signal_return": ret,
                "entry_date": entry, "exit_date": exit_date}
        for label, series in (("hs300", hs300_rows), ("sector", sector_rows)):
            benchmark = _benchmark_return(series, entry, exit_date) if series else None
            item[label + "_return"] = benchmark
            item[label + "_alpha"] = ret - benchmark if benchmark is not None else None
        result["windows"][str(window)] = item
    return result


def evaluate_recommendation(
    recommendation,
    evaluation_as_of,
    market_sessions,
    stock_rows,
    hs300_rows=None,
    sector_rows=None,
    cost_model=None,
    windows=WINDOWS,
    stock_meta=None,
):
    costs = cost_model or CostModel()
    content = recommendation.get("content", recommendation)
    recommendation_date = normalize_trade_date(content.get("recommendation_date"))
    candidate = recommendation.get("candidate") or recommendation
    if not candidate and content.get("candidates"):
        candidate = content["candidates"][0]
    candidate = candidate or {}
    code = candidate.get("code", "")
    evaluation_as_of = normalize_trade_date(evaluation_as_of)
    try:
        validate_series_metadata(stock_meta)
    except AttributionDataError as exc:
        return _error_result(
            recommendation_date, code, evaluation_as_of, costs, windows, str(exc)
        )

    plan = candidate.get("trade_plan") or {}
    try:
        sessions = _normalized_sessions(market_sessions)
    except AttributionDataError as exc:
        return _error_result(
            recommendation_date, code, evaluation_as_of, costs, windows, str(exc)
        )
    eligible_sessions = [
        session for session in sessions
        if recommendation_date < session <= evaluation_as_of
    ]
    if not eligible_sessions:
        return {
            "evaluator_version": EVALUATOR_VERSION,
            "recommendation_date": recommendation_date,
            "code": code,
            "evaluation_as_of": evaluation_as_of,
            "execution": {
                "status": "pending",
                "reason": "evaluation_cutoff_before_t1",
            },
            "cost_model": dataclasses.asdict(costs),
            "windows": {
                str(window): {"status": "pending", "required_session": window}
                for window in windows
            },
        }
    execution = (
        resolve_entry(plan, recommendation_date, sessions, stock_rows, code, stock_meta)
        if plan
        else {"status": "unexecutable", "reason": "trade_plan_missing"}
    )
    result = {
        "evaluator_version": EVALUATOR_VERSION,
        "recommendation_date": recommendation_date,
        "code": code,
        "evaluation_as_of": evaluation_as_of,
        "execution": execution,
        "cost_model": dataclasses.asdict(costs),
        "windows": {},
    }
    if execution.get("status") != "executable":
        for window in windows:
            result["windows"][str(window)] = {
                "status": execution.get("status"),
                "reason": execution.get("reason"),
            }
        return result

    entry = execution["date"]
    rows = _rows_by_date(stock_rows)
    entry_row = rows[entry]
    entry_price = execution["price"]
    close0 = _number(entry_row, "close", entry_price)
    stop = _number(plan.get("stop_loss", {}), "price", -math.inf)
    targets = plan.get("targets") or {}
    target = _number(targets, "primary", _number(plan.get("target", {}), "price", math.inf))
    future = [session for session in sessions if entry <= session <= evaluation_as_of]

    for window in windows:
        if len(future) < window:
            result["windows"][str(window)] = {
                "status": "pending",
                "required_session": window,
            }
            continue
        path = future[:window]
        mark = rows.get(path[-1])
        previous_close = close0
        mfe = -math.inf
        mae = math.inf
        exit_reason = None
        exit_date = None
        exit_price = None
        carried_suspension = False
        missing_data = False
        for session in path:
            row = rows.get(session)
            if row is None:
                missing_data = True
                continue
            volume = _number(row, "vol", _number(row, "volume", None))
            if volume is None:
                missing_data = True
                continue
            if volume <= 0:
                carried_suspension = True
                continue
            low = _number(row, "low", previous_close)
            high = _number(row, "high", previous_close)
            close = _number(row, "close", previous_close)
            mfe = max(mfe, _ret(entry_price, high) or 0)
            mae = min(mae, _ret(entry_price, low) or 0)
            # A-share stock bought today cannot be sold today.  We retain its
            # MFE/MAE but begin executable exits on the next market session.
            if session == entry:
                previous_close = close
                continue
            prior = rows.get(sessions[sessions.index(session) - 1])
            limit_down = _one_price_limit(row, _number(prior or {}, "close"), code, stock_meta, "down")
            if limit_down is None:
                missing_data = True
                continue
            if limit_down:
                carried_suspension = True
                previous_close = close
                continue
            if exit_reason is None and low <= stop:
                exit_reason = "stop"
                exit_date = session
                # A gap below the stop cannot be filled at the stop price.
                exit_price = min(stop, _number(row, "open", stop))
            elif exit_reason is None and high >= target:
                exit_reason = "target"
                exit_date = session
                exit_price = max(target, _number(row, "open", target))
            previous_close = close
        if missing_data or mark is None:
            result["windows"][str(window)] = {
                "status": "data_error",
                "reason": "historical_data_missing",
            }
            continue
        mark_close = _number(mark, "close", previous_close)
        mark_to_market = _ret(entry_price, mark_close)
        plan_path_return = (
            _ret(entry_price, exit_price)
            if exit_price is not None
            else mark_to_market
        )
        cost_bps = sum(dataclasses.astuple(costs))
        gross = plan_path_return if plan_path_return is not None else 0
        net = gross - cost_bps / 10000
        item = {
            "status": "complete",
            "entry_date": entry,
            "mark_date": path[-1],
            "mark_to_market_return": mark_to_market,
            "plan_path_return": plan_path_return,
            "gross_return": gross,
            "net_return": net,
            "mfe": mfe if mfe != -math.inf else None,
            "mae": mae if mae != math.inf else None,
            "exit_reason": exit_reason,
            "exit_date": exit_date,
            "carried_suspension": carried_suspension,
            "execution_assumptions": {
                "t_plus_one_sale": True,
                "gap_stop_fill": "open_or_stop_whichever_is_lower",
                "gap_target_fill": "open_or_target_whichever_is_higher",
                "same_bar_stop_target": "stop_precedes_target",
                "one_price_limit_down": "carry_position_until_a_tradable_session",
                "cost_mode": costs.mode,
            },
        }
        for label, series in (("hs300", hs300_rows), ("sector", sector_rows)):
            benchmark = _benchmark_return(series, entry, path[-1]) if series else None
            item[label + "_return"] = benchmark
            item[label + "_alpha"] = gross - benchmark if benchmark is not None else None
        result["windows"][str(window)] = item
    return result


_IDENTITY_FIELDS = ("snapshot_sha256", "evaluator_version", "recommendation_date", "code", "cost_model", "evaluation_contract")


def _assert_compatible(existing, incoming):
    for field in _IDENTITY_FIELDS:
        old = existing.get(field)
        new = incoming.get(field)
        if old is not None and new is not None and old != new:
            raise ValueError("sidecar_identity_mismatch:" + field)


def _is_newer_or_equal(existing, incoming):
    old = existing.get("evaluation_as_of")
    new = incoming.get("evaluation_as_of")
    if not old or not new:
        return True
    return normalize_trade_date(new) >= normalize_trade_date(old)


def _merge_record(existing, incoming):
    _assert_compatible(existing, incoming)
    out = copy.deepcopy(existing)
    can_update_metadata = _is_newer_or_equal(existing, incoming)
    for field in (
        "snapshot_sha256", "evaluator_version", "recommendation_date", "code",
        "evaluation_as_of", "cost_model", "evaluation_contract", "execution",
    ):
        if field in incoming and (can_update_metadata or field in _IDENTITY_FIELDS):
            out[field] = copy.deepcopy(incoming[field])
    old_windows = out.setdefault("windows", {})
    for label, window in (incoming.get("windows") or {}).items():
        previous = old_windows.get(label)
        if not previous or (
            previous.get("status") in ("pending", "data_error") and can_update_metadata
        ):
            old_windows[label] = copy.deepcopy(window)
    return out


def merge_attribution(existing, incoming):
    if not existing:
        return copy.deepcopy(incoming)
    if "items" not in incoming and "items" not in existing:
        return _merge_record(existing, incoming)
    _assert_compatible(existing, incoming)
    out = copy.deepcopy(existing)
    can_update_metadata = _is_newer_or_equal(existing, incoming)
    for field in (
        "snapshot_sha256", "evaluator_version", "recommendation_date",
        "evaluation_as_of", "cost_model", "evaluation_contract", "execution",
    ):
        if field in incoming and (can_update_metadata or field in _IDENTITY_FIELDS):
            out[field] = copy.deepcopy(incoming[field])
    old_items = {str(item.get("code")): item for item in out.setdefault("items", [])}
    for item in incoming.get("items", []):
        code = str(item.get("code"))
        old_items[code] = (
            _merge_record(old_items[code], item)
            if code in old_items
            else copy.deepcopy(item)
        )
    out["items"] = [old_items[key] for key in sorted(old_items)]
    old_signals = {str(item.get("code")): item for item in out.setdefault("candidate_signal_items", [])}
    for item in incoming.get("candidate_signal_items", []):
        code = str(item.get("code"))
        old_signals[code] = (_merge_record(old_signals[code], item)
                             if code in old_signals else copy.deepcopy(item))
    out["candidate_signal_items"] = [old_signals[key] for key in sorted(old_signals)]
    if "candidate_signal_evaluator_version" in incoming:
        out["candidate_signal_evaluator_version"] = incoming["candidate_signal_evaluator_version"]
    return out


def write_sidecar(payload, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    os.close(fd)
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return path


def read_sidecar(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_sidecar_json") from exc


@contextmanager
def _sidecar_lock(path):
    """Serialize read/merge/write for one recommendation date."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def sidecar_path(root, recommendation_date):
    return Path(root) / (normalize_trade_date(recommendation_date) + ".json")


def _completed_primary_events(items, primary_window=PRIMARY_WINDOW):
    """Return de-duplicated mature events for a rolling primary window.

    A later same-code recommendation whose primary holding window starts before
    the earlier one ends is an audit record, not a second independent event.
    The evaluator has the actual session endpoints, so this avoids guessing
    from calendar days.
    """
    label = str(primary_window)
    candidates = []
    for item in items:
        window = (item.get("windows") or {}).get(label, {})
        if window.get("status") != "complete":
            continue
        entry_date = window.get("entry_date") or (item.get("execution") or {}).get("date")
        exit_date = window.get("exit_date") or window.get("mark_date")
        if not entry_date or not exit_date:
            # Complete records from an old evaluator without endpoints remain
            # observable but cannot be treated as independently de-duplicated.
            continue
        candidates.append((str(item.get("code", "")), normalize_trade_date(entry_date),
                           normalize_trade_date(exit_date), item, window))
    kept, duplicates, last_exit = [], [], {}
    for code, entry_date, exit_date, item, window in sorted(candidates, key=lambda row: (row[0], row[1], row[2])):
        if code and code in last_exit and entry_date <= last_exit[code]:
            duplicates.append((item, window))
            continue
        kept.append((item, window))
        if code:
            last_exit[code] = exit_date
    return kept, duplicates


def summarize_attribution(items, minimum_dates=20, minimum_mature=100,
                          primary_window=PRIMARY_WINDOW):
    by_window = {}
    for item in items:
        for label, window in (item.get("windows") or {}).items():
            stats = by_window.setdefault(label, {"completed": [], "pending": 0, "unexecutable": 0, "errors": 0})
            status = window.get("status")
            if status == "complete":
                stats["completed"].append(window)
            elif status == "pending":
                stats["pending"] += 1
            elif status == "unexecutable":
                stats["unexecutable"] += 1
            elif status == "data_error":
                stats["errors"] += 1
    summarized = {}
    for label, stats in by_window.items():
        values = [window.get("net_return") for window in stats["completed"] if window.get("net_return") is not None]
        summarized[label] = {
            "raw_records": (len(stats["completed"]) + stats["pending"]
                            + stats["unexecutable"] + stats["errors"]),
            "mature_observations": len(stats["completed"]),
            "pending": stats["pending"],
            "unexecutable": stats["unexecutable"],
            "errors": stats["errors"],
            "mean_net_return": sum(values) / len(values) if values else None,
        }
    primary_label = str(primary_window)
    primary = summarized.get(primary_label, {})
    primary_available = primary_label in summarized
    deduped, duplicates = _completed_primary_events(items, primary_window)
    mature_dates = len({item.get("recommendation_date") for item, _ in deduped
                        if item.get("recommendation_date")})
    raw_primary = primary.get("mature_observations", 0)
    evaluated_primary = raw_primary + primary.get("pending", 0) + primary.get("unexecutable", 0) + primary.get("errors", 0)
    deduped_mature = len(deduped)
    deduped_primary_values = [window.get("net_return") for _, window in deduped
                              if window.get("net_return") is not None]
    return {
        "official_dates": minimum_dates,
        "primary_window": primary_window,
        "mature_dates": mature_dates,
        "mature_observations": deduped_mature,
        "raw_mature_records": raw_primary,
        "deduplicated_mature_events": deduped_mature,
        "duplicate_primary_records": len(duplicates),
        "pending": primary.get("pending", 0),
        "unexecutable": primary.get("unexecutable", 0),
        "errors": primary.get("errors", 0),
        "data_evaluable_coverage": raw_primary / evaluated_primary if evaluated_primary else 0.0,
        "date_coverage": mature_dates / minimum_dates if minimum_dates else 0.0,
        "status": "evidence_insufficient" if not primary_available or mature_dates < 20 or deduped_mature < minimum_mature else "ready",
        "mean_net_return": (sum(deduped_primary_values) / len(deduped_primary_values)
                            if deduped_primary_values else None),
        "by_window": summarized,
    }


def summarize_candidate_performance(items, minimum_dates=20, minimum_mature=100,
                                    primary_window=PRIMARY_WINDOW):
    """Keep candidate-signal statistics distinct from simulated trade P&L."""
    summary = summarize_attribution(items, minimum_dates, minimum_mature, primary_window)
    for label, stats in summary["by_window"].items():
        if label == str(primary_window):
            completed = [window for _, window in _completed_primary_events(items, primary_window)[0]]
            stats["deduplicated_mature_events"] = len(completed)
        else:
            completed = [window for item in items for key, window in (item.get("windows") or {}).items()
                         if key == label and window.get("status") == "complete"]
        values = [row.get("signal_return") for row in completed if row.get("signal_return") is not None]
        alphas = [row.get("hs300_alpha") for row in completed if row.get("hs300_alpha") is not None]
        stats["mean_signal_return"] = sum(values) / len(values) if values else None
        stats["mean_hs300_alpha"] = sum(alphas) / len(alphas) if alphas else None
        stats.pop("mean_net_return", None)
    summary["measurement"] = "候选信号表现（次一交易日收盘至窗口收盘），非交易模拟、非实际成交收益"
    return summary


def candidate_research_readiness(candidate_summary):
    """Candidate research is valid without a trade plan or trade simulation."""
    ready = candidate_summary.get("status") == "ready"
    return {
        "status": "eligible_for_walk_forward_review" if ready else "evidence_insufficient",
        "minimum_mature_dates": 20,
        "minimum_deduplicated_mature_events": 100,
        "primary_window": PRIMARY_WINDOW,
        "candidate_signal_ready": ready,
        "policy": "候选信号研究不依赖交易计划或交易模拟；仅允许人工逐项、时间切分的 walk-forward 评估。",
    }


def trade_research_readiness(trade_summary):
    ready = trade_summary.get("status") == "ready"
    return {
        "status": "eligible_for_walk_forward_review" if ready else "evidence_insufficient",
        "minimum_mature_dates": 20,
        "minimum_deduplicated_mature_events": 100,
        "primary_window": PRIMARY_WINDOW,
        "trade_simulation_ready": ready,
        "policy": "交易模拟研究必须在固定成本与成交假设下单独审阅，不自动调整仓位。",
    }


def calibration_readiness(candidate_summary, trade_summary):
    """Compatibility status plus independent P0 research gates."""
    candidate_gate = candidate_research_readiness(candidate_summary)
    trade_gate = trade_research_readiness(trade_summary)
    return {
        "status": "eligible_for_walk_forward_review" if candidate_gate["candidate_signal_ready"] else "evidence_insufficient",
        "minimum_official_dates": 20,
        "minimum_mature_observations": 100,
        "candidate_signal_ready": candidate_gate["candidate_signal_ready"],
        "trade_simulation_ready": trade_gate["trade_simulation_ready"],
        "candidate_research": candidate_gate,
        "trade_research": trade_gate,
        "policy": "候选研究与交易模拟资格分开；不自动调整买点奖励、市场门槛或仓位。",
    }


def _call_series_loader(loader, code, candidate, recommendation_date, evaluation_as_of):
    try:
        signature = inspect.signature(loader)
        parameters = list(signature.parameters.values())
        accepts_extra = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters)
        if accepts_extra or len(parameters) >= 4:
            return loader(code, candidate, recommendation_date, evaluation_as_of)
    except (TypeError, ValueError):
        pass
    return loader(code, candidate)


def track_attribution(snapshot, series_loader, evaluation_as_of, root=None, cost_model=None, windows=WINDOWS):
    """Evaluate actionable items and merge their mutable date sidecar."""
    content = snapshot.get("content", snapshot)
    recommendation_date = normalize_trade_date(content["recommendation_date"])
    evaluation_as_of = normalize_trade_date(evaluation_as_of)
    buckets = content.get("buckets") or {}
    candidates = list(buckets.get("actionable", []))
    signal_candidates = list(content.get("candidates") or [])
    results = []
    signal_results = []
    for candidate in candidates:
        try:
            series = _call_series_loader(
                series_loader, candidate.get("code"), candidate,
                recommendation_date, evaluation_as_of,
            ) or {}
            result = evaluate_recommendation(
                {"recommendation_date": recommendation_date, "candidate": candidate},
                evaluation_as_of,
                series.get("market_sessions", []),
                series.get("stock_rows", []),
                series.get("hs300_rows"),
                series.get("sector_rows"),
                cost_model=cost_model,
                windows=windows,
                stock_meta=series.get("stock_meta"),
            )
        except (AttributionDataError, OSError, RuntimeError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
            result = _error_result(
                recommendation_date, candidate.get("code", ""),
                evaluation_as_of, cost_model or CostModel(), windows, reason,
            )
        results.append(result)
    for candidate in signal_candidates:
        try:
            series = _call_series_loader(series_loader, candidate.get("code"), candidate,
                                         recommendation_date, evaluation_as_of) or {}
            signal_results.append(evaluate_candidate_signal(
                {"recommendation_date": recommendation_date, "candidate": candidate},
                evaluation_as_of, series.get("market_sessions", []), series.get("stock_rows", []),
                series.get("hs300_rows"), series.get("sector_rows"), windows,
                series.get("stock_meta")))
        except (AttributionDataError, OSError, RuntimeError, ValueError) as exc:
            signal_results.append(_error_result(recommendation_date, candidate.get("code", ""), evaluation_as_of,
                                                CostModel(), windows, str(exc)))
    contract = build_evaluation_contract(windows, dataclasses.asdict(cost_model or CostModel()))
    payload = {
        "evaluator_version": EVALUATOR_VERSION,
        "recommendation_date": recommendation_date,
        "evaluation_as_of": evaluation_as_of,
        "snapshot_sha256": snapshot.get("content_sha256"),
        "cost_model": dataclasses.asdict(cost_model or CostModel()),
        "evaluation_contract": contract,
        "items": results,
        "candidate_signal_items": signal_results,
        "candidate_signal_evaluator_version": CANDIDATE_EVALUATOR_VERSION,
    }
    if root is not None:
        path = sidecar_path(Path(root) / contract["contract_id"], recommendation_date)
        with _sidecar_lock(path):
            prior = read_sidecar(path)
            payload = merge_attribution(prior, payload) if prior else payload
            write_sidecar(payload, path)
    return payload


def default_series_loader(code, candidate, recommendation_date=None, evaluation_as_of=None):
    """Fetch qfq stock, HS300 and sector rows through existing adapters."""
    from scans.stock_scanner import _fetch_kline
    from analysis.market_regime import fetch_index_kline
    from fetchers.sector_kline import fetch_single_kline

    trade_plan = candidate.get("trade_plan") or {}
    # Decision inputs belong to the snapshot date, but result series must
    # cover the requested evaluation cutoff.  Never silently score a shorter
    # current cache merely because it covers the original recommendation.
    basis_date = trade_plan.get("basis_date") or candidate.get("basis_date") or recommendation_date
    result_cutoff = evaluation_as_of or basis_date
    ts_code = candidate.get("ts_code") or (
        str(code) + ".SH" if str(code).startswith(("0", "3", "6")) else str(code)
    )
    stock = _fetch_kline(ts_code, as_of_date=result_cutoff or "") or {}
    stock_rows = stock.get("data", []) if isinstance(stock, dict) else []
    if not stock_rows:
        raise AttributionDataError("historical_data_missing")
    hs300 = fetch_index_kline("000300.SH", lmt=180)
    if not hs300:
        raise AttributionDataError("benchmark_data_missing")
    sector = []
    if candidate.get("sector_code"):
        sector = fetch_single_kline(candidate["sector_code"], min_records=180)
    market_sessions = [_row_date(row) for row in hs300 if _row_date(row)]
    return {
        "market_sessions": market_sessions,
        "stock_rows": stock_rows,
        "stock_meta": stock.get("meta") if isinstance(stock, dict) else None,
        "hs300_rows": hs300,
        "sector_rows": sector,
    }


def track_official_history(
    history_root=None,
    attribution_root=None,
    evaluation_as_of=None,
    through_date=None,
    series_loader=default_series_loader,
    cost_model=None,
    windows=WINDOWS,
    history=120,
):
    """Process the most recent valid official snapshots."""
    history_root = Path(history_root or (Path(CACHE_DIR) / "recommendation_history"))
    attribution_root = Path(attribution_root or (Path(CACHE_DIR) / "evolution" / "evaluations"))
    normalized_through = normalize_trade_date(through_date) if through_date else None
    snapshots, rejected = iter_official_snapshots(history_root, normalized_through)
    if history > 0:
        snapshots = snapshots[-history:]
    as_of = normalize_trade_date(evaluation_as_of or normalized_through or date.today())
    payloads = [
        track_attribution(
            snapshot, series_loader, as_of, root=attribution_root,
            cost_model=cost_model, windows=windows,
        )
        for snapshot in snapshots
    ]
    items = [item for payload in payloads for item in payload.get("items", [])]
    signal_items = [item for payload in payloads
                    for item in payload.get("candidate_signal_items", [])]
    summary = summarize_attribution(items, minimum_dates=len(snapshots))
    summary["snapshots"] = len(snapshots)
    summary["rejected_snapshots"] = rejected
    candidate_summary = summarize_candidate_performance(
        signal_items, minimum_dates=len(snapshots))
    return {"summary": summary, "items": items,
            "candidate_signal_summary": candidate_summary,
            "candidate_signal_items": signal_items,
            "strategy_calibration": calibration_readiness(candidate_summary, summary)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--through")
    parser.add_argument("--history", type=int, default=120)
    parser.add_argument("--history-root")
    parser.add_argument("--attribution-root")
    parser.add_argument("--evaluation-as-of")
    parser.add_argument("--windows", default="5,10,20,60")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--buy-commission-bps", type=float, default=0)
    parser.add_argument("--sell-commission-bps", type=float, default=0)
    parser.add_argument("--buy-slippage-bps", type=float, default=0)
    parser.add_argument("--sell-slippage-bps", type=float, default=0)
    parser.add_argument("--sell-tax-bps", type=float, default=0)
    args = parser.parse_args(argv)
    windows = tuple(int(value) for value in args.windows.split(",") if value)
    costs = CostModel(
        args.buy_commission_bps, args.sell_commission_bps,
        args.buy_slippage_bps, args.sell_slippage_bps, args.sell_tax_bps,
    )
    output = track_official_history(
        args.history_root, args.attribution_root, args.evaluation_as_of,
        args.through, windows=windows, cost_model=costs, history=args.history,
    )
    output.update({
        "evaluator_version": EVALUATOR_VERSION,
        "through": args.through,
        "windows": list(windows),
    })
    print(json.dumps(output, ensure_ascii=False) if args.json else "evidence_insufficient")
    return 0


if __name__ == "__main__":
    main()
