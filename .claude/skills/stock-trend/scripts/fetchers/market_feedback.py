#!/usr/bin/env python3
"""Bounded inputs for the daily review's next-day market feedback.

The fetcher has two independent jobs: freeze the requested session's limit-up
pool, and obtain the requested session's native daily quote for every member
carried from the prior review.  Missing quotes remain explicit rows so losers,
suspensions, unsupported markets, and timeouts cannot silently disappear.
"""

from __future__ import annotations

import json
import math
import queue
import re
import socket
import threading
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
import sys
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.eastmoney_utils import EM_API_HOSTS, EM_HEADERS, build_secid, fetch_url
from fetchers.kline_eastmoney import fetch_eastmoney


SHANGHAI = ZoneInfo("Asia/Shanghai")
POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
POOL_PARAMS = {
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
    "dpt": "wz.ztzt",
    "Pageindex": "0",
    "pagesize": "10000",
    "sort": "fbt:asc",
}
MAX_QUOTE_WORKERS = 4
RECOVERY_RESERVE_SECONDS = 10.0
TENCENT_QUOTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_HEADERS = {
    "User-Agent": EM_HEADERS["User-Agent"],
    "Referer": "https://finance.qq.com/",
    "Accept": "*/*",
}


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _as_of_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _number(value: Any, *, scale: float = 1.0) -> float | int | None:
    try:
        number = float(value) / scale
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _safe_error(exc: Exception | str) -> str:
    """Keep a bounded diagnostic without retaining query credentials."""
    text = str(exc).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text)
    text = re.sub(
        r"(?i)\b(token|api[_-]?key|authorization|ut)\s*=\s*[^\s&;,]+",
        lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:300]


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text or "超时" in text:
        return "timeout"
    if isinstance(exc, ValueError) and "unsupported" in text:
        return "unsupported_market"
    return "provider_error"


def _stock_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if "." in text:
        raw, market = text.rsplit(".", 1)
        if len(raw) == 6 and raw.isdigit() and market in {"SH", "SZ", "BJ"}:
            return f"{raw}.{market}"
        return None
    if len(text) != 6 or not text.isdigit():
        return None
    if text.startswith("6"):
        return f"{text}.SH"
    if text.startswith(("0", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8", "920")):
        return f"{text}.BJ"
    return None


def _pool_total(data: dict[str, Any]) -> int | None:
    """Return a provider count only when it is an explicit non-negative int."""
    for key in ("total", "tc", "num"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0 and str(value).strip() in {str(parsed), f"{parsed}.0"}:
            return parsed
    return None


def _pool_member(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    code = _stock_code(item.get("c"))
    if code is None:
        return None
    return {
        "code": code,
        "name": str(item.get("n") or "").strip(),
        "close": _number(item.get("p"), scale=1000.0),
        "streak": _number(item.get("lbc")),
        "roles": ["limit_up"],
    }


def _fetch_pool(basis_date: str, timeout: float, *, deadline: float | None = None,
                cancel_event: threading.Event | None = None,
                request_slots: threading.BoundedSemaphore | None = None,
                ) -> tuple[dict, list[dict]]:
    pool = {
        "data_date": None,
        "date_origin": "unknown",
        "fetched_at": None,
        "source": "eastmoney",
        "complete": False,
        "members": [],
    }
    errors: list[dict] = []
    params = dict(POOL_PARAMS, date=basis_date.replace("-", ""))
    request_deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + min(5.0, max(0.0, float(timeout))),
    )
    remaining = max(0.0, request_deadline - time.monotonic())
    if (cancel_event is not None and cancel_event.is_set()) or remaining <= 0:
        pool["fetched_at"] = _now().isoformat()
        errors.append({
            "stage": "pool", "code": None, "reason": "budget_exhausted",
            "error": "pool request was not started before the shared deadline",
        })
        return pool, errors
    holder: dict[str, Any] = {}
    holder_lock = threading.Lock()
    accepting = threading.Event()
    accepting.set()
    slots = request_slots or threading.BoundedSemaphore(1)

    def request_pool() -> None:
        acquired = False
        request_kwargs = {
            "headers": EM_HEADERS,
            "timeout": max(0.05, remaining),
            "fallback_timeout": max(0.05, min(remaining, 1.0)),
        }
        request_kwargs["deadline"] = request_deadline
        if cancel_event is not None:
            request_kwargs["cancel_event"] = cancel_event
        try:
            wait = max(0.0, request_deadline - time.monotonic())
            acquired = slots.acquire(timeout=wait)
            if (not acquired or not accepting.is_set()
                    or (cancel_event is not None and cancel_event.is_set())
                    or time.monotonic() >= request_deadline):
                return
            raw = fetch_url(f"{POOL_URL}?{urlencode(params)}", **request_kwargs)
            value = ("raw", raw, _now().isoformat())
        except Exception as exc:
            value = ("error", exc, _now().isoformat())
        finally:
            if acquired:
                slots.release()
        with holder_lock:
            if (accepting.is_set()
                    and not (cancel_event is not None and cancel_event.is_set())
                    and time.monotonic() <= request_deadline):
                holder["result"] = value

    worker = threading.Thread(target=request_pool, daemon=True)
    worker.start()
    worker.join(max(0.0, request_deadline - time.monotonic()))
    with holder_lock:
        accepting.clear()
        result = holder.get("result")
    if result is None:
        pool["fetched_at"] = _now().isoformat()
        errors.append({
            "stage": "pool", "code": None, "reason": "budget_exhausted",
            "error": "pool request exceeded its bounded response budget",
        })
        return pool, errors
    kind, value, fetched_at = result
    pool["fetched_at"] = fetched_at
    if kind == "error":
        errors.append({
            "stage": "pool", "code": None, "reason": _failure_reason(value),
            "error": _safe_error(value),
        })
        return pool, errors
    try:
        payload = json.loads(value)
    except Exception as exc:
        errors.append({
            "stage": "pool", "code": None, "reason": "provider_error",
            "error": _safe_error(exc),
        })
        return pool, errors

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        errors.append({"stage": "pool", "code": None, "error": "provider data missing"})
        return pool, errors

    provider_date = _iso_date(data.get("qdate"))
    if provider_date:
        pool["data_date"] = provider_date
        pool["date_origin"] = "provider"

    raw_members = data.get("pool")
    if not isinstance(raw_members, list):
        errors.append({"stage": "pool", "code": None, "error": "provider pool missing"})
        return pool, errors

    parsed_members = [_pool_member(item) for item in raw_members]
    members = [item for item in parsed_members if item is not None]
    unique_members = list({item["code"]: item for item in members}.values())
    pool["members"] = unique_members
    provider_total = _pool_total(data)
    pool.update({"provider_total": provider_total,
                 "raw_member_count": len(raw_members),
                 "unique_member_count": len(unique_members),
                 "raw_members": raw_members})
    pool["complete"] = bool(
        provider_date == basis_date
        and provider_total is not None
        and provider_total == len(raw_members)
        and len(members) == len(raw_members)
        and len(unique_members) == len(raw_members)
    )
    return pool, errors


def _prior_codes(prior_members: Iterable[Any] | dict[str, Any] | None) -> list[str]:
    if isinstance(prior_members, dict):
        values = prior_members.get("members", [])
    else:
        values = prior_members or []
    codes: list[str] = []
    seen: set[str] = set()
    for member in values:
        raw = member.get("code") if isinstance(member, dict) else member
        code = _stock_code(raw)
        # Preserve malformed identifiers as explicit missing rows when possible.
        normalized = code or str(raw or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            codes.append(normalized)
    return codes


def _empty_quote(code: str, fetched_at: str, *, reason: str = "not_started",
                 attempts: list[dict] | None = None) -> dict[str, Any]:
    tencent_supported = code.endswith((".SH", ".SZ"))
    return {
        "code": code,
        "data_date": None,
        "pct_chg": None,
        "close": None,
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
        "source": "eastmoney",
        "fetched_at": fetched_at,
        "status": "missing",
        "reason": reason,
        "failure_reason": reason,
        "latest_data_date": None,
        "latest_provider_date": None,
        "provider_attempts": list(attempts or []),
        "fallback_capability": {
            "provider": "tencent_native",
            "status": "verified" if tencent_supported else "disabled",
            "reason": "" if tencent_supported else "unsupported_market",
        },
    }


def _tencent_identity(code: str) -> tuple[str, str, str] | None:
    raw, _, suffix = code.partition(".")
    if len(raw) != 6 or not raw.isdigit():
        return None
    if suffix == "SH":
        return f"sh{raw}", "1", "SH"
    if suffix == "SZ":
        return f"sz{raw}", "51", "SZ"
    return None


def _quote_for_tencent(code: str, basis_date: str, as_of: datetime, *,
                       deadline: float, previous_attempts: list[dict] | None = None,
                       started_at: str | None = None,
                       cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Read Tencent's native qt array; never derive returns from qfq K-lines."""
    attempts = list(previous_attempts or [])
    started_at = started_at or _now().isoformat()
    identity = _tencent_identity(code)
    if identity is None:
        finished_at = _now().isoformat()
        attempts.append({
            "provider": "tencent_native", "status": "disabled",
            "reason": "unsupported_market", "requested_date": basis_date,
            "latest_data_date": None, "latest_provider_date": None,
            "started_at": started_at, "finished_at": finished_at,
            "error": "Tencent native quote supports verified SH/SZ identities only",
        })
        return _empty_quote(code, finished_at, reason="unsupported_market",
                            attempts=attempts)

    stock_key, market_id, market_name = identity
    remaining = max(0.0, deadline - time.monotonic())
    if (cancel_event is not None and cancel_event.is_set()) or remaining <= 0:
        return _empty_quote(code, _now().isoformat(), reason="budget_exhausted",
                            attempts=attempts)
    url = f"{TENCENT_QUOTE_URL}?param={stock_key},day,,,5,qfq"
    request_kwargs = {
        "headers": TENCENT_HEADERS,
        "timeout": min(3.0, remaining),
        "fallback_timeout": min(1.0, remaining),
        "deadline": deadline,
    }
    if cancel_event is not None:
        request_kwargs["cancel_event"] = cancel_event
    try:
        payload = json.loads(fetch_url(url, **request_kwargs))
    except Exception as exc:
        finished_at = _now().isoformat()
        reason = _failure_reason(exc)
        attempts.append({
            "provider": "tencent_native", "status": "error", "reason": reason,
            "requested_date": basis_date, "latest_data_date": None,
            "latest_provider_date": None, "started_at": started_at,
            "finished_at": finished_at, "error": _safe_error(exc),
        })
        return _empty_quote(code, finished_at, reason=reason, attempts=attempts)

    finished = _now()
    finished_at = finished.isoformat()
    reason = "invalid_native_quote"
    latest_data_date = None
    source_time_raw = None
    try:
        stock_data = payload["data"][stock_key]
        qt = stock_data["qt"]
        values = qt[stock_key]
        market_values = qt["market"]
        if not isinstance(values, list) or len(values) < 36:
            raise ValueError("native qt array is shorter than verified contract")
        if not isinstance(market_values, list) or not market_values:
            raise ValueError("native market status is missing")
        market_status = str(market_values[0])
        source_time_raw = str(values[30])
        source_time = datetime.strptime(
            source_time_raw, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        latest_data_date = source_time.date().isoformat()
        if payload.get("code") != 0:
            reason = "provider_error"
            raise ValueError("native provider response code is not zero")
        if str(values[0]) != market_id or str(values[2]) != code[:6]:
            reason = "identity_mismatch"
            raise ValueError("native quote identity does not match request")
        if latest_data_date != basis_date:
            reason = "source_date_mismatch"
            raise ValueError("native quote source date does not match request")
        if source_time > finished:
            reason = "future_source_time"
            raise ValueError("native quote source time is later than fetch completion")

        last = _number(values[3])
        previous_close = _number(values[4])
        open_price = _number(values[5])
        volume = _number(values[6])
        pct_chg = _number(values[32])
        high = _number(values[33])
        low = _number(values[34])
        if (last is None or last <= 0 or previous_close is None
                or previous_close <= 0 or open_price is None or open_price <= 0
                or volume is None or volume < 0 or pct_chg is None
                or high is None or low is None or high < low
                or not low <= last <= high):
            raise ValueError("native quote contains invalid price or volume fields")
        check_pct = (float(last) / float(previous_close) - 1.0) * 100.0
        if abs(check_pct - float(pct_chg)) > 0.03:
            reason = "native_pct_inconsistent"
            raise ValueError("native percentage change failed consistency check")

        is_intraday = (
            as_of.date().isoformat() == basis_date
            and as_of.timetz().replace(tzinfo=None) < clock_time(15, 10)
        )
        market_flags = set(market_status.split("|"))
        if not is_intraday and (
                source_time.timetz().replace(tzinfo=None) < clock_time(15, 10)
                or f"{market_name}_close_已收盘" not in market_flags):
            reason = "close_not_confirmed"
            raise ValueError("native quote does not contain verified close evidence")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        attempts.append({
            "provider": "tencent_native", "status": "invalid", "reason": reason,
            "requested_date": basis_date,
            "latest_data_date": latest_data_date,
            "latest_provider_date": latest_data_date,
            "source_time_raw": source_time_raw,
            "started_at": started_at, "finished_at": finished_at,
            "error": _safe_error(exc),
        })
        quote = _empty_quote(code, finished_at, reason=reason, attempts=attempts)
        quote.update({"latest_data_date": latest_data_date,
                      "latest_provider_date": latest_data_date})
        return quote

    attempts.append({
        "provider": "tencent_native", "status": "success", "reason": "",
        "requested_date": basis_date, "latest_data_date": basis_date,
        "latest_provider_date": basis_date, "source_time_raw": source_time_raw,
        "started_at": started_at, "finished_at": finished_at, "error": "",
    })
    quote = _empty_quote(code, finished_at, attempts=attempts)
    quote.update({
        "data_date": basis_date,
        "pct_chg": pct_chg,
        "close": last,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "volume_unit": "provider_native_volume",
        "source": "tencent_native_quote",
        "status": "ok",
        "mode": "intraday" if is_intraday else "close",
        "source_time_raw": source_time_raw,
        "reason": "",
        "failure_reason": "",
        "latest_data_date": basis_date,
        "latest_provider_date": basis_date,
        "provider_attempts": attempts,
        "pct_chg_origin": "provider",
        "price_basis": "native",
        "close_confirmed": not is_intraday,
        "finality": "intraday_native_quote" if is_intraday else "live_native_quote_close",
    })
    return quote


def _quote_for(code: str, basis_date: str, as_of: datetime, *,
               deadline: float, host: str | None = None,
               previous_attempts: list[dict] | None = None,
               started_at: str | None = None,
               cancel_event: threading.Event | None = None) -> dict[str, Any]:
    attempts = list(previous_attempts or [])
    started_at = started_at or _now().isoformat()
    secid = build_secid(code)
    # The pool's provider market identifier is 0 for Beijing shares, including
    # the BSE's 920 code range. Keep .BJ identity distinct from Shenzhen.
    if secid is None and code.endswith(".BJ") and _stock_code(code):
        secid = f"0.{code.split('.')[0]}"
    if not secid:
        error = ValueError(f"unsupported Eastmoney market code: {code}")
        attempts.append({
            "provider": "eastmoney", "host": host or EM_API_HOSTS[0],
            "status": "error", "reason": "unsupported_market",
            "requested_date": basis_date, "latest_data_date": None,
            "latest_provider_date": None,
            "started_at": started_at, "finished_at": _now().isoformat(),
            "error": _safe_error(error),
        })
        return _empty_quote(code, attempts[-1]["finished_at"],
                            reason="unsupported_market", attempts=attempts)

    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        return _empty_quote(code, _now().isoformat(),
                            reason="budget_exhausted", attempts=attempts)
    kwargs = {
        "lmt": 5,
        "timeout": min(3.0, remaining),
        "fallback_timeout": min(1.0, remaining),
        "adjustment": 0,
        "deadline": deadline,
        "cancel_event": cancel_event,
    }
    if host is not None:
        kwargs["host"] = host
    try:
        records, _name = fetch_eastmoney(secid, "D", **kwargs)
    except Exception as exc:
        finished_at = _now().isoformat()
        reason = _failure_reason(exc)
        attempts.append({
            "provider": "eastmoney", "host": host or EM_API_HOSTS[0],
            "status": "error", "reason": reason,
            "requested_date": basis_date, "latest_data_date": None,
            "latest_provider_date": None,
            "started_at": started_at, "finished_at": finished_at,
            "error": _safe_error(exc),
        })
        return _empty_quote(code, finished_at, reason=reason, attempts=attempts)

    finished_at = _now().isoformat()
    quote = _empty_quote(code, finished_at, attempts=attempts)
    target_raw = basis_date.replace("-", "")
    provider_dates = [
        _iso_date(row.get("trade_date"))
        for row in records if isinstance(row, dict)
    ]
    provider_dates = [value for value in provider_dates if value]
    latest_provider_date = max(provider_dates, default=None)
    target = next((row for row in reversed(records)
                   if isinstance(row, dict)
                   and str(row.get("trade_date", "")).replace("-", "") == target_raw), None)
    if target is None:
        attempts.append({
            "provider": "eastmoney", "host": host or EM_API_HOSTS[0],
            "status": "missing", "reason": "missing_target_session",
            "requested_date": basis_date,
            "latest_data_date": latest_provider_date,
            "latest_provider_date": latest_provider_date,
            "started_at": started_at, "finished_at": finished_at,
            "error": "",
        })
        quote.update({
            "reason": "missing_target_session",
            "failure_reason": "missing_target_session",
            "latest_data_date": latest_provider_date,
            "latest_provider_date": latest_provider_date,
            "provider_attempts": attempts,
        })
        return quote

    pct_chg = _number(target.get("pct_chg"))
    close = _number(target.get("close"))
    volume = _number(target.get("vol"))
    if pct_chg is None or close is None or close <= 0 or volume is None or volume < 0:
        attempts.append({
            "provider": "eastmoney", "host": host or EM_API_HOSTS[0],
            "status": "invalid", "reason": "invalid_quote",
            "requested_date": basis_date,
            "latest_data_date": latest_provider_date,
            "latest_provider_date": latest_provider_date,
            "started_at": started_at, "finished_at": finished_at,
            "error": "required native quote fields are missing or invalid",
        })
        quote.update({
            "reason": "invalid_quote",
            "failure_reason": "invalid_quote",
            "latest_data_date": latest_provider_date,
            "latest_provider_date": latest_provider_date,
            "provider_attempts": attempts,
        })
        return quote

    is_intraday = (
        as_of.date().isoformat() == basis_date
        and as_of.timetz().replace(tzinfo=None) < clock_time(15, 10)
    )
    attempts.append({
        "provider": "eastmoney", "host": host or EM_API_HOSTS[0],
        "status": "success", "reason": "",
        "requested_date": basis_date, "latest_data_date": basis_date,
        "latest_provider_date": basis_date,
        "started_at": started_at, "finished_at": finished_at,
        "error": "",
    })
    quote.update({
        "data_date": basis_date,
        "pct_chg": pct_chg,
        "close": close,
        "open": _number(target.get("open")),
        "high": _number(target.get("high")),
        "low": _number(target.get("low")),
        "volume": volume,
        "status": "ok",
        "mode": "intraday" if is_intraday else "close",
        "source_time_raw": target.get("trade_date"),
        "reason": "",
        "failure_reason": "",
        "latest_data_date": basis_date,
        "latest_provider_date": basis_date,
        "provider_attempts": attempts,
        "pct_chg_origin": "provider",
        "price_basis": "native",
        "close_confirmed": not is_intraday,
        "finality": "intraday_live_daily" if is_intraday else "live_daily_close",
    })
    return quote


def _fetch_quotes(codes: list[str], basis_date: str, as_of: datetime,
                  budget_seconds: float, *, deadline: float | None = None,
                  cancel_event: threading.Event | None = None,
                  request_slots: threading.BoundedSemaphore | None = None,
                  ) -> tuple[list[dict], list[dict]]:
    initial_time = _now().isoformat()
    quotes = {code: _empty_quote(code, initial_time) for code in codes}
    lock = threading.Lock()
    started_codes: set[str] = set()
    local_active = threading.Event()
    local_active.set()
    external_cancel = cancel_event or threading.Event()
    transport_slots = request_slots or threading.BoundedSemaphore(
        MAX_QUOTE_WORKERS)
    absolute_deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + max(0.0, float(budget_seconds)),
    )
    reserve = min(RECOVERY_RESERVE_SECONDS,
                  max(0.0, absolute_deadline - time.monotonic()) * 0.34)
    primary_deadline = max(time.monotonic(), absolute_deadline - reserve)

    def run_phase(phase_codes: list[str], phase_deadline: float,
                  *, provider: str) -> list[threading.Thread]:
        work: queue.Queue[str] = queue.Queue()
        for code in phase_codes:
            work.put(code)
        accepting = threading.Event()
        accepting.set()

        def worker() -> None:
            while (accepting.is_set() and local_active.is_set()
                   and not external_cancel.is_set()
                   and time.monotonic() < phase_deadline):
                try:
                    code = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    with lock:
                        started_codes.add(code)
                        prior = quotes[code]
                        attempts = list(prior.get("provider_attempts") or [])
                    hosts = EM_API_HOSTS[1:] if provider == "eastmoney_recovery" else [None]
                    value = prior
                    for host in hosts:
                        if (not local_active.is_set() or external_cancel.is_set()
                                or not accepting.is_set()
                                or time.monotonic() >= phase_deadline):
                            break
                        wait = max(0.0, phase_deadline - time.monotonic())
                        acquired = transport_slots.acquire(timeout=wait)
                        if not acquired:
                            break
                        try:
                            if (not accepting.is_set() or not local_active.is_set()
                                    or external_cancel.is_set()
                                    or time.monotonic() >= phase_deadline):
                                break
                            if provider == "tencent_native":
                                value = _quote_for_tencent(
                                    code, basis_date, as_of,
                                    deadline=phase_deadline,
                                    previous_attempts=attempts,
                                    started_at=_now().isoformat(),
                                    cancel_event=external_cancel)
                            else:
                                value = _quote_for(
                                    code, basis_date, as_of,
                                    deadline=phase_deadline, host=host,
                                    previous_attempts=attempts,
                                    started_at=_now().isoformat(),
                                    cancel_event=external_cancel)
                        finally:
                            transport_slots.release()
                        attempts = list(value.get("provider_attempts") or attempts)
                        if value.get("status") == "ok":
                            break
                    with lock:
                        if (accepting.is_set() and local_active.is_set()
                                and not external_cancel.is_set()
                                and time.monotonic() <= phase_deadline):
                            quotes[code] = value
                finally:
                    work.task_done()

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(MAX_QUOTE_WORKERS, len(phase_codes)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            remaining = phase_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        accepting.clear()
        return threads

    primary_threads = run_phase(
        codes, primary_deadline, provider="eastmoney_primary")
    if (local_active.is_set() and not external_cancel.is_set()
            and time.monotonic() < absolute_deadline):
        with lock:
            retry_codes = [code for code in codes
                           if quotes[code].get("status") != "ok"]
        recovery_remaining = max(0.0, absolute_deadline - time.monotonic())
        tencent_deadline = min(
            absolute_deadline, time.monotonic() + recovery_remaining * 0.5)
        tencent_threads = run_phase(
            retry_codes, tencent_deadline, provider="tencent_native")
        for thread in tencent_threads:
            remaining = tencent_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        with lock:
            retry_codes = [code for code in codes
                           if quotes[code].get("status") != "ok"]
        recovery_threads = run_phase(
            retry_codes, absolute_deadline, provider="eastmoney_recovery")
        for thread in recovery_threads:
            remaining = absolute_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    local_active.clear()
    finished_at = _now().isoformat()
    errors: list[dict] = []
    with lock:
        result = []
        for code in codes:
            quote = quotes[code]
            if quote.get("status") != "ok":
                reason = quote.get("failure_reason") or "not_started"
                if reason == "not_started" and (
                        code in started_codes or time.monotonic() >= absolute_deadline):
                    reason = "budget_exhausted"
                    quote = _empty_quote(
                        code, finished_at, reason=reason,
                        attempts=quote.get("provider_attempts"))
                errors.append({
                    "stage": "quote", "code": code, "reason": reason,
                    "error": _safe_error(
                        next((attempt.get("error") for attempt in reversed(
                            quote.get("provider_attempts") or [])
                              if attempt.get("error")), reason)),
                    "latest_provider_date": quote.get("latest_provider_date"),
                    "latest_data_date": quote.get("latest_data_date"),
                    "provider_attempts": list(quote.get("provider_attempts") or []),
                })
            result.append(quote)
    return result, errors


def fetch_inputs(basis_date: str, prior_members: Iterable[Any] | dict[str, Any] | None,
                 *, as_of: datetime | str, timeout: float = 5.0,
                 budget_seconds: float = 30.0, deadline: float | None = None,
                 cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Fetch the current limit-up pool and next-day quotes for prior members."""
    normalized_date = _iso_date(basis_date)
    if normalized_date is None:
        raise ValueError("basis_date must be YYYY-MM-DD")
    cutoff = _as_of_datetime(as_of)
    shared_deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + max(0.0, float(budget_seconds)),
    )
    request_slots = threading.BoundedSemaphore(MAX_QUOTE_WORKERS)
    pool, pool_errors = _fetch_pool(
        normalized_date, min(float(timeout), 5.0),
        deadline=shared_deadline, cancel_event=cancel_event,
        request_slots=request_slots)
    pool["mode"] = "intraday" if (
        cutoff.date().isoformat() == normalized_date
        and cutoff.timetz().replace(tzinfo=None) < clock_time(15, 10)
    ) else "close"
    quotes, quote_errors = _fetch_quotes(
        _prior_codes(prior_members), normalized_date, cutoff,
        max(0.0, shared_deadline - time.monotonic()),
        deadline=shared_deadline, cancel_event=cancel_event,
        request_slots=request_slots)
    return {"pool": pool, "quotes": quotes, "errors": pool_errors + quote_errors}

