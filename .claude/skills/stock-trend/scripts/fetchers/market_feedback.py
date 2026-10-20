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
import threading
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path
import sys
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.eastmoney_utils import EM_HEADERS, build_secid, fetch_url
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


def _fetch_pool(basis_date: str, timeout: float) -> tuple[dict, list[dict]]:
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
    try:
        raw = fetch_url(
            f"{POOL_URL}?{urlencode(params)}",
            headers=EM_HEADERS,
            timeout=max(0.1, float(timeout)),
            fallback_timeout=max(0.1, min(float(timeout), 1.0)),
        )
        payload = json.loads(raw)
    except Exception as exc:
        pool["fetched_at"] = _now().isoformat()
        errors.append({"stage": "pool", "code": None, "error": str(exc)})
        return pool, errors

    pool["fetched_at"] = _now().isoformat()

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


def _empty_quote(code: str, fetched_at: str) -> dict[str, Any]:
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
    }


def _quote_for(code: str, basis_date: str, as_of: datetime) -> dict[str, Any]:
    secid = build_secid(code)
    # The pool's provider market identifier is 0 for Beijing shares, including
    # the BSE's 920 code range. Keep .BJ identity distinct from Shenzhen.
    if secid is None and code.endswith(".BJ") and _stock_code(code):
        secid = f"0.{code.split('.')[0]}"
    if not secid:
        raise ValueError(f"unsupported Eastmoney market code: {code}")
    records, _name = fetch_eastmoney(
        secid, "D", lmt=5, timeout=3, fallback_timeout=1)
    quote = _empty_quote(code, _now().isoformat())
    target_raw = basis_date.replace("-", "")
    target = next((row for row in reversed(records)
                   if isinstance(row, dict)
                   and str(row.get("trade_date", "")).replace("-", "") == target_raw), None)
    if target is None:
        return quote

    is_intraday = (
        as_of.date().isoformat() == basis_date
        and as_of.timetz().replace(tzinfo=None) < clock_time(15, 10)
    )
    quote.update({
        "data_date": basis_date,
        "pct_chg": _number(target.get("pct_chg")),
        "close": _number(target.get("close")),
        "open": _number(target.get("open")),
        "high": _number(target.get("high")),
        "low": _number(target.get("low")),
        "volume": _number(target.get("vol")),
        "status": "ok",
        "mode": "intraday" if is_intraday else "close",
        "source_time_raw": target.get("trade_date"),
    })
    return quote


def _fetch_quotes(codes: list[str], basis_date: str, as_of: datetime,
                  budget_seconds: float) -> tuple[list[dict], list[dict]]:
    initial_time = _now().isoformat()
    quotes = {code: _empty_quote(code, initial_time) for code in codes}
    errors: list[dict] = []
    finished: set[str] = set()
    work: queue.Queue[str] = queue.Queue()
    for code in codes:
        work.put(code)
    lock = threading.Lock()
    accepting = threading.Event()
    accepting.set()
    deadline = time.monotonic() + max(0.0, float(budget_seconds))

    def worker() -> None:
        while True:
            if time.monotonic() >= deadline:
                return
            try:
                code = work.get_nowait()
            except queue.Empty:
                return
            try:
                value = _quote_for(code, basis_date, as_of)
                with lock:
                    if accepting.is_set():
                        quotes[code] = value
                        finished.add(code)
            except Exception as exc:
                with lock:
                    if accepting.is_set():
                        quotes[code] = _empty_quote(code, _now().isoformat())
                        errors.append({
                            "stage": "quote", "code": code,
                            "error": str(exc),
                        })
                        finished.add(code)
            finally:
                work.task_done()

    workers = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(4, len(codes)))]
    for thread in workers:
        thread.start()
    for thread in workers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)

    with lock:
        accepting.clear()
        for code in codes:
            if code not in finished:
                quotes[code] = _empty_quote(code, _now().isoformat())
                errors.append({
                    "stage": "quote", "code": code,
                    "error": "total quote budget exceeded",
                })
    return [quotes[code] for code in codes], errors


def fetch_inputs(basis_date: str, prior_members: Iterable[Any] | dict[str, Any] | None,
                 *, as_of: datetime | str, timeout: float = 5.0,
                 budget_seconds: float = 30.0) -> dict[str, Any]:
    """Fetch the current limit-up pool and next-day quotes for prior members."""
    normalized_date = _iso_date(basis_date)
    if normalized_date is None:
        raise ValueError("basis_date must be YYYY-MM-DD")
    cutoff = _as_of_datetime(as_of)
    deadline = time.monotonic() + max(0.0, float(budget_seconds))
    pool, pool_errors = _fetch_pool(normalized_date, timeout)
    pool["mode"] = "intraday" if (
        cutoff.date().isoformat() == normalized_date
        and cutoff.timetz().replace(tzinfo=None) < clock_time(15, 10)
    ) else "close"
    quotes, quote_errors = _fetch_quotes(
        _prior_codes(prior_members), normalized_date, cutoff,
        max(0.0, deadline - time.monotonic()))
    return {"pool": pool, "quotes": quotes, "errors": pool_errors + quote_errors}

