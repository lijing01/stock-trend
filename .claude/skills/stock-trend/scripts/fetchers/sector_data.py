#!/usr/bin/env python3
"""East Money sector data fetcher for sector ranking and candidate scans.

Fetches A-share sector/concept rankings and constituent stock data.

Usage:
    python3 fetch_sector_data.py --rankings        # Get hot sector rankings
    python3 fetch_sector_data.py --stocks BKxxx     # Get sector constituents
    python3 fetch_sector_data.py --list             # List all sectors
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import tempfile
import time
import urllib.request
from datetime import date as datetime_date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from core.eastmoney_utils import EM_HEADERS, EM_PUSH2_HOSTS, rotate_em_host
from core.source_health import (
    SECTOR_RANKING_BUDGET_SECONDS,
    classify_failure,
    live_attempt,
    source_result,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = SCRIPT_DIR.parent.parent.parent.parent / ".cache" / "stock-trend"
TRADING_CALENDAR_FILE = CACHE_DIR / "trading_calendar.json"


# ──────────────────────── East Money API Helpers ────────────────────────


import random as _random


class ProviderFetchError(RuntimeError):
    """Provider failure retaining exact attempt count and classification."""

    def __init__(self, message, provider_attempts, reason):
        super().__init__(message)
        self.provider_attempts = provider_attempts
        self.reason = reason


def _fetch_json(url: str, timeout: int = 15, retries: int = 3,
                with_evidence: bool = False,
                deadline: float | None = None) -> dict:
    """Fetch JSON with bounded host rotation and optional attempt evidence."""
    last_error = None
    provider_attempts = 0
    for attempt in range(retries + 1):
        remaining = (deadline - time.monotonic()
                     if deadline is not None else timeout)
        if remaining <= 0:
            last_error = TimeoutError("live deadline exhausted")
            break
        host = EM_PUSH2_HOSTS[attempt % len(EM_PUSH2_HOSTS)]
        actual_url = url
        if host != "push2.eastmoney.com":
            actual_url = url.replace("https://push2.eastmoney.com",
                                     f"https://{host}", 1)
        try:
            provider_attempts += 1
            req = urllib.request.Request(actual_url, headers=EM_HEADERS)
            with urllib.request.urlopen(
                    req, timeout=min(timeout, remaining)) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if with_evidence:
                    return source_result(payload, live_attempt(
                        attempted=True,
                        provider_attempts=provider_attempts))
                return payload
        except Exception as e:
            last_error = e
            if attempt < retries:
                # Exponential backoff with random jitter: 1.5^attempt * (1-2s)
                sleep_sec = 1.5 ** attempt + _random.uniform(0.5, 1.5)
                if deadline is not None:
                    sleep_sec = min(
                        sleep_sec, max(0, deadline - time.monotonic()))
                if sleep_sec:
                    time.sleep(sleep_sec)
    reason = classify_failure(last_error)
    raise ProviderFetchError(
        f"东方财富API请求失败(尝试{provider_attempts}次): {last_error}",
        provider_attempts, reason)


def _check_result(result: dict) -> dict:
    """Validate API response, return data dict or raise."""
    if not result or result.get("rc") != 0 or not result.get("data"):
        msg = result.get("message", "未知错误") if result else "无响应"
        raise RuntimeError(f"API返回错误: {msg}")
    return result["data"]


def _unpack_fetch_result(result):
    """Accept evidence-aware fetches and legacy test/provider payloads."""
    if (isinstance(result, dict)
            and set(("payload", "live_attempt")) <= set(result)
            and isinstance(result.get("live_attempt"), dict)):
        return result["payload"], result["live_attempt"]
    return result, None


# ──────────────────────── Sector List & Rankings ────────────────────────


def get_sector_list() -> list[dict]:
    """Fetch all A-share sector/concept lists from East Money.

    Returns:
        List of {code, name, type} dicts where type is "industry" or "concept".
    """
    sectors = []
    today = datetime.now().strftime("%Y%m%d")
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"

    for idx, (stype, sname) in enumerate([("2", "industry"), ("3", "concept")]):
        # Stagger concurrent requests to avoid rate limiting
        if idx > 0:
            time.sleep(_random.uniform(0.3, 0.8))
        url = (
            f"{base_url}?fs=m:90+t:{stype}&fields=f12,f14"
            f"&pn=1&pz=500&po=0&np=1&fltt=2"
            f"&fid=f3&_={today}"
        )
        try:
            data = _fetch_json(url)
            items = _check_result(data).get("diff", [])
            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                if code and name:
                    sectors.append({"code": code, "name": name, "type": sname})
        except Exception as e:
            print(f"  Warning: 无法获取{sname}板块列表: {e}", file=sys.stderr)

    return sectors


MIN_RANKING_ROWS_PER_SOURCE = 5
RANKING_PAGE_SIZE = 100
MAX_RANKING_PAGES_PER_SOURCE = 20
SECTOR_FIELD_SEMANTICS_VERSION = "sector-fields/v2"
RANKINGS_CACHE_SCHEMA_VERSION = 2
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _provider_timestamp(value) -> Optional[str]:
    """Normalize an East Money f124 epoch to an aware Shanghai timestamp."""
    if isinstance(value, bool):
        return None
    try:
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch /= 1000
        parsed = datetime.fromtimestamp(epoch, tz=SHANGHAI_TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not (2000 <= parsed.year <= 2100):
        return None
    return parsed.isoformat()


def _nonnegative_int_or_none(value) -> Optional[int]:
    """Preserve missing provider counts instead of inventing a zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _nonnegative_number_or_none(value) -> Optional[float]:
    """Amounts and rates are nonnegative; missing quotes stay unavailable."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _apply_provider_date_evidence(meta: dict, timestamps: list[Optional[str]],
                                  row_count: int) -> None:
    """Set data_date only when every ranking row proves one provider date."""
    valid = [value for value in timestamps if value]
    dates = {value[:10] for value in valid}
    if row_count <= 0 or not valid:
        status = "missing"
    elif len(valid) != row_count:
        status = "incomplete"
    elif len(dates) != 1:
        status = "mixed"
    else:
        status = "verified"
    meta["provider_timestamp_status"] = status
    meta["provider_timestamp_count"] = len(valid)
    meta["provider_row_count"] = row_count
    meta["provider_timestamp_min"] = min(valid) if valid else None
    meta["provider_timestamp_max"] = max(valid) if valid else None
    if status == "verified":
        provider_date = next(iter(dates))
        if _verified_trading_date(provider_date) is None:
            meta["provider_timestamp_status"] = "invalid_date"
            meta["data_date"] = None
            meta["date_origin"] = "unknown"
            meta["alignment"] = "unknown"
            return
        meta["data_date"] = provider_date
        meta["date_origin"] = "provider"
        meta["alignment"] = (
            "matched" if provider_date == meta.get("requested_date")
            else "mismatched"
        )
    else:
        meta["data_date"] = None
        meta["date_origin"] = "unknown"
        meta["alignment"] = "unknown"


def get_sector_rankings(timeout: int = 15, retries: int = 3,
                        with_evidence: bool = False,
                        deadline: float | None = None,
                        allow_cross_source_fallback: bool = True) -> dict:
    """Fetch sector rankings with composite scoring data.

    Returns:
        dict with meta and sectors list (code, name, change_pct, amount,
        turnover_rate, up_count, down_count, up_down_sample_count,
        total_count, type). ``total_count`` remains unknown unless complete
        constituent membership proves it.
    """
    budget_deadline = time.monotonic() + SECTOR_RANKING_BUDGET_SECONDS
    deadline = min(deadline, budget_deadline) \
        if deadline is not None else budget_deadline
    fetched_at = datetime.now().astimezone().isoformat()
    requested_date = fetched_at[:10]
    result = {
        "meta": {
            "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "fetched_at": fetched_at,
            "requested_date": requested_date,
            "data_date": None,
            "date_origin": "unknown",
            "alignment": "unknown",
            "sources": {},
            "errors": [],
            "complete": False,
            "provider": "eastmoney",
            "field_semantics_version": SECTOR_FIELD_SEMANTICS_VERSION,
            "field_semantics": {
                "amount": "f6:yuan",
                "turnover_rate": "f8:percent",
                "up_down_sample_count": "f104+f105",
                "total_count": "unknown_without_complete_membership",
            },
            "ranking_diagnostics": {},
        },
        "sectors": [],
    }
    today = datetime.now().strftime("%Y%m%d")
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"
    provider_attempts = 0
    failure_reason = ""
    provider_timestamps = []
    provider_rows = 0

    for idx, (stype, sname) in enumerate([("2", "industry"), ("3", "concept")]):
        # Stagger concurrent requests to avoid rate limiting
        if idx > 0:
            delay = _random.uniform(0.3, 0.8)
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            if delay:
                time.sleep(delay)
        # f2=最新价, f3=涨跌幅, f4=涨跌额, f6=成交额, f8=换手率,
        # f12=代码, f14=名称, f20=总市值,
        # f104=涨家数, f105=跌家数, f62=主力净流入, f124=更新时间
        fields = "f2,f3,f4,f6,f8,f12,f14,f20,f62,f104,f105,f124,f168,f170,f171"
        source_rows = []
        seen_codes = set()
        reported_total = None
        reported_totals = []
        returned_rows = 0
        valid_rows = 0
        duplicate_rows = 0
        pages_fetched = 0
        source_reasons = []
        source_failure_reason = ""
        for page in range(1, MAX_RANKING_PAGES_PER_SOURCE + 1):
            if deadline is not None and time.monotonic() >= deadline:
                source_reasons.append("deadline_exhausted")
                source_failure_reason = "timeout"
                break
            # f12 is a stable identity.  Paging a live f3 sort can move rows
            # between pages while prices change and silently lose/duplicate
            # sectors.
            url = (
                f"{base_url}?fs=m:90+t:{stype}&fields={fields}"
                f"&pn={page}&pz={RANKING_PAGE_SIZE}&po=1&np=1&fltt=2"
                f"&fid=f12&_={today}"
            )
            try:
                fetched = _fetch_json(
                    url, timeout=timeout, retries=retries,
                    # Evidence is collected internally even for the historical
                    # payload-only public API.
                    with_evidence=True, deadline=deadline)
                data, attempt = _unpack_fetch_result(fetched)
                if attempt:
                    provider_attempts += attempt.get("provider_attempts", 0)
                payload = _check_result(data)
            except Exception as exc:
                provider_attempts += getattr(exc, "provider_attempts", 0)
                source_failure_reason = getattr(exc, "reason", "") \
                    or classify_failure(exc)
                source_reasons.append(
                    f"page_fetch_error:{source_failure_reason}")
                result["meta"]["errors"].append(
                    f"{sname}: page {page}: {exc}")
                break

            pages_fetched += 1
            page_total = _nonnegative_int_or_none(payload.get("total"))
            if page_total is None:
                if "reported_total_missing" not in source_reasons:
                    source_reasons.append("reported_total_missing")
            else:
                reported_totals.append(page_total)
                if reported_total is None:
                    reported_total = page_total
                elif page_total != reported_total:
                    if "reported_total_changed" not in source_reasons:
                        source_reasons.append("reported_total_changed")

            items = payload.get("diff", [])
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, list):
                items = []
            returned_rows += len(items)
            page_valid = [item for item in items
                          if isinstance(item, dict) and item.get("f12")]
            valid_rows += len(page_valid)
            for item in page_valid:
                code = str(item.get("f12"))
                if code in seen_codes:
                    duplicate_rows += 1
                    continue
                seen_codes.add(code)
                source_rows.append(item)

            if duplicate_rows and "duplicate_codes" not in source_reasons:
                source_reasons.append("duplicate_codes")
            if reported_total is None:
                break
            if len(seen_codes) >= reported_total:
                break
            if not items:
                source_reasons.append("premature_empty_page")
                break
            if len(items) < RANKING_PAGE_SIZE:
                source_reasons.append("premature_short_page")
                break
        else:
            source_reasons.append("page_limit_exhausted")

        if reported_total is not None and len(seen_codes) != reported_total:
            source_reasons.append("coverage_mismatch")
        if len(reported_totals) > 1 and len(set(reported_totals)) != 1 \
                and "reported_total_changed" not in source_reasons:
            source_reasons.append("reported_total_changed")

        active_items = sum(
            1 for item in source_rows
            if (_nonnegative_int_or_none(item.get("f104")) or 0) > 0
            or (_nonnegative_int_or_none(item.get("f105")) or 0) > 0)
        source_complete = bool(
            reported_total is not None
            and reported_total >= MIN_RANKING_ROWS_PER_SOURCE
            and len(seen_codes) == reported_total
            and not source_reasons)
        if source_complete:
            source_status = "ok"
        elif source_rows:
            source_status = (
                "sparse"
                if (len(source_rows) < MIN_RANKING_ROWS_PER_SOURCE
                    and not source_failure_reason)
                else "partial"
            )
        else:
            source_status = "error" if source_failure_reason else "empty"
        result["meta"]["ranking_diagnostics"][sname] = {
            "status": source_status,
            "returned_rows": returned_rows,
            "valid_rows": valid_rows,
            "unique_rows": len(seen_codes),
            "duplicate_rows": duplicate_rows,
            "active_rows": active_items,
            "reported_total": reported_total,
            "reported_totals": reported_totals,
            "pages_fetched": pages_fetched,
            "reasons": source_reasons,
        }
        result["meta"]["sources"][sname] = source_status
        if source_status != "ok" and not any(
                message.startswith(f"{sname}:")
                for message in result["meta"]["errors"]):
            detail = ",".join(source_reasons) or \
                f"only {len(source_rows)} valid rows"
            result["meta"]["errors"].append(f"{sname}: {detail}")

        for item in source_rows:
            up_count = _nonnegative_int_or_none(item.get("f104"))
            down_count = _nonnegative_int_or_none(item.get("f105"))
            sample_count = (
                up_count + down_count
                if up_count is not None and down_count is not None
                else None
            )
            provider_timestamp = _provider_timestamp(item.get("f124"))
            provider_timestamps.append(provider_timestamp)
            provider_rows += 1
            sector = {
                "code": item.get("f12", ""),
                "provider": "eastmoney",
                "provider_code": item.get("f12", ""),
                "sector_id": f"eastmoney:{sname}:{item.get('f12', '')}",
                "expand_symbol": item.get("f12", ""),
                "expandable": bool(item.get("f12")),
                "name": item.get("f14", ""),
                "type": sname,
                "change_pct": item.get("f3"),       # 涨跌幅%
                "amount": _nonnegative_number_or_none(item.get("f6")),
                "turnover_rate": _nonnegative_number_or_none(item.get("f8")),
                "up_count": up_count,                 # 涨家数
                "down_count": down_count,             # 跌家数
                "up_down_sample_count": sample_count,
                "flat_count": None,
                "suspended_count": None,
                "uncovered_count": None,
                "total_count": None,
                "constituent_coverage_complete": False,
                "provider_timestamp": provider_timestamp,
                "main_force_net": item.get("f62"),   # 主力净流入
            }
            if sector["code"]:
                result["sectors"].append(sector)
        if source_failure_reason:
            failure_reason = source_failure_reason
            print(
                f"  Warning: {sname}板块排行仅获取部分分页: "
                f"{source_failure_reason}", file=sys.stderr)

    _apply_provider_date_evidence(
        result["meta"], provider_timestamps, provider_rows)

    # If EM API returned zero active sectors, try AKShare fallback
    active = sum(
        1 for s in result["sectors"]
        if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
    )
    result["meta"]["total_sectors"] = len(result["sectors"])
    result["meta"]["complete"] = all(
        result["meta"]["sources"].get(source) == "ok"
        for source in ("industry", "concept")
    )
    if (allow_cross_source_fallback and (not result["meta"]["complete"]
            or active == 0
            or result["meta"]["total_sectors"] < 5)):
        try:
            from fetchers.sector_akshare import get_sector_rankings_akshare
            akshare_result = get_sector_rankings_akshare()
            if akshare_result and akshare_result.get("sectors"):
                akshare_active = sum(
                    1 for sector in akshare_result["sectors"]
                    if (sector.get("up_count", 0) or 0) > 0
                    or (sector.get("down_count", 0) or 0) > 0
                )
                if akshare_result.get("meta", {}).get("complete") is True \
                        and akshare_active >= MIN_RANKING_ROWS_PER_SOURCE:
                    print(
                        "  [AKShare] 备选数据源: "
                        f"{len(akshare_result['sectors'])} sectors"
                    )
                    akshare_result["meta"]["upstream_errors"] = list(
                        result["meta"]["errors"])
                    akshare_result["meta"]["upstream_diagnostics"] = dict(
                        result["meta"]["ranking_diagnostics"])
                    akshare_result["meta"]["fallback_reason"] = (
                        "eastmoney_incomplete" if result["meta"]["errors"]
                        else "eastmoney_zero_active")
                    result = akshare_result
                    active = akshare_active
        except Exception as e:
            print(f"  [AKShare] 备选数据源失败: {e}", file=sys.stderr)

    if not with_evidence:
        return result
    if result["meta"]["complete"] and active:
        failure_reason = ""
    elif not failure_reason:
        failure_reason = "incomplete"
    result["meta"]["live_attempt"] = live_attempt(
        attempted=provider_attempts > 0,
        provider_attempts=provider_attempts,
        reason=failure_reason,
    )
    return source_result(result, result["meta"]["live_attempt"])


def compute_hot_score(sector: dict) -> float:
    """Compute hot sector composite score.

    Weight: change_pct(40%) + main_force_net(30%) + up/down ratio(30%)

    Args:
        sector: dict with change_pct, main_force_net, up_count, down_count.

    Returns:
        Score 0-100.
    """
    # Normalize change_pct: 0% → 40, 5% → 100, < -5% → 0
    change = sector.get("change_pct") or 0
    change_score = min(100, max(0, 40 + change * 12))

    # Normalize capital flow (scaled by 1e8 for readability)
    # 0 → 50, +5亿 → 100, -5亿 → 0
    net = (sector.get("main_force_net") or 0) / 1e8
    capital_score = min(100, max(0, 50 + net * 10))

    # Up/down ratio: 1:1 → 50, all up → 100, all down → 0
    up = sector.get("up_count", 0) or 1
    down = sector.get("down_count", 0) or 1
    ratio = up / max(1, (up + down))
    ratio_score = ratio * 100

    return round(change_score * 0.40 + capital_score * 0.30 + ratio_score * 0.30, 1)


def _up_ratio(sector: dict) -> float:
    """Calculate up/(up+down) ratio for a sector."""
    up = sector.get("up_count", 0) or 0
    down = sector.get("down_count", 0) or 0
    total = up + down
    return up / total if total > 0 else 0


def rank_hot_sectors(rankings: dict, top_n: int = 10,
                     min_stocks: int = 8,
                     min_up_ratio: float = 0.15) -> list[dict]:
    """Rank sectors by composite hot score.

    Filters:
      - Tiny sectors (fewer than min_stocks constituents)
      - Weak sectors (up_count / total < min_up_ratio)
      - Duplicate child-level sectors (same base name + identical stats)

    Args:
        rankings: output from get_sector_rankings().
        top_n: number of top sectors to return.
        min_stocks: minimum constituent stocks. 0 disables.
        min_up_ratio: minimum up/(up+down) ratio. 0 disables.

    Returns:
        Sorted list with score added to each sector dict.
    """
    import re

    sectors = rankings.get("sectors", [])

    if min_stocks > 0:
        sectors = [
            s for s in sectors
            if ((s.get("up_count", 0) or 0)
                + (s.get("down_count", 0) or 0)) >= min_stocks
        ]

    # Filter by up/down ratio — exclude boards that are overwhelmingly red
    if min_up_ratio > 0:
        sectors = [s for s in sectors if _up_ratio(s) >= min_up_ratio]

    # Dedup: sectors with same base name (stripping Ⅰ/Ⅱ/Ⅲ/Ⅳ) and identical
    # (up_count, down_count, change_pct) are parent/child duplicates; keep first.
    seen_signatures = set()
    deduped = []
    for s in sectors:
        base_name = re.sub(r'[ⅠⅡⅢⅣ\u2160-\u2163]$', '', s.get("name", ""))
        sig = (base_name,
               s.get("up_count", 0),
               s.get("down_count", 0),
               round(s.get("change_pct", 0) or 0, 2))
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            deduped.append(s)
    sectors = deduped

    for s in sectors:
        absolute = compute_hot_score(s)
        s["absolute_hot_score"] = absolute
        s["hot_score"] = absolute

    sectors.sort(key=lambda x: x.get("absolute_hot_score", 0), reverse=True)

    # Min-max normalize to 0-100 for consistent differentiation
    if sectors:
        scores = [s["absolute_hot_score"] for s in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for s in sectors:
                s["hot_score"] = round(
                    (s["absolute_hot_score"] - lo) / (hi - lo) * 100, 1
                )

    return sectors[:top_n]


# ──────────────────────── Sector Constituent Stocks ────────────────────────


SECTOR_STOCKS_CACHE_DIR = CACHE_DIR / "sector_stocks"
SECTOR_STOCKS_CACHE_SCHEMA_VERSION = 3
SECTOR_STOCKS_MAX_AGE_HOURS = 24 * 30
SECTOR_STOCKS_RECENT_CACHE_MAX_AGE_HOURS = 24 * 5


def _sector_stocks_cache_path(sector_code: str) -> Path:
    """Return a cache path constrained to the constituent-cache directory."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sector_code or ""):
        raise ValueError(f"invalid sector code: {sector_code!r}")
    cache_dir = SECTOR_STOCKS_CACHE_DIR.resolve()
    path = (cache_dir / f"{sector_code}.json").resolve()
    if path.parent != cache_dir:
        raise ValueError(f"invalid sector code: {sector_code!r}")
    return path


def _sector_stocks_history_path(sector_code: str, data_date: str) -> Path:
    """Return the exact-date constituent snapshot path."""
    _sector_stocks_cache_path(sector_code)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data_date or "")):
        raise ValueError(f"invalid constituent snapshot date: {data_date!r}")
    history_dir = (SECTOR_STOCKS_CACHE_DIR / "history" / data_date).resolve()
    root = (SECTOR_STOCKS_CACHE_DIR / "history").resolve()
    if history_dir.parent != root:
        raise ValueError(f"invalid constituent snapshot date: {data_date!r}")
    return history_dir / f"{sector_code}.json"


def save_sector_stocks_cache(sector_code: str, stocks: list[dict],
                             data_date: str = "",
                             provider: str = "eastmoney",
                             requested_date: str = "") -> None:
    """Persist a successful constituent response for outage fallback."""
    path = _sector_stocks_cache_path(sector_code)
    SECTOR_STOCKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat()
    snapshot_date = _verified_trading_date(data_date) if data_date else None
    payload = {
        "schema_version": SECTOR_STOCKS_CACHE_SCHEMA_VERSION,
        "field_semantics_version": SECTOR_FIELD_SEMANTICS_VERSION,
        "cached_at": fetched_at,
        "fetched_at": fetched_at,
        "data_date": snapshot_date,
        "requested_date": requested_date or fetched_at[:10],
        "date_origin": "provider" if snapshot_date else "unknown",
        "provider": provider,
        "stocks": stocks,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    path.write_text(encoded, encoding="utf-8")
    if snapshot_date:
        history_path = _sector_stocks_history_path(sector_code, snapshot_date)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(encoded, encoding="utf-8")


def _protect_legacy_stock_amounts(payload: dict) -> dict:
    """Read old f8-as-amount caches without treating percentages as yuan."""
    if payload.get("field_semantics_version") == SECTOR_FIELD_SEMANTICS_VERSION:
        return payload
    protected = dict(payload)
    claimed_date = payload.get("data_date")
    protected["legacy_claimed_data_date"] = claimed_date
    protected["requested_date"] = payload.get("requested_date") or claimed_date
    protected["data_date"] = None
    protected["date_origin"] = "unknown"
    protected_stocks = []
    for raw in payload.get("stocks") or []:
        stock = dict(raw)
        ambiguous = stock.get("amount")
        if "turnover_rate" not in stock:
            stock["turnover_rate"] = ambiguous
        stock["amount"] = None
        stock["amount_quality"] = "legacy_ambiguous_f8"
        protected_stocks.append(stock)
    protected["stocks"] = protected_stocks
    protected["cache_semantics_status"] = "legacy_protected"
    return protected


def load_sector_stocks_cache(sector_code: str, as_of_date: str = "") -> Optional[dict]:
    """Load a non-empty constituent snapshot younger than 30 days."""
    paths = []
    if as_of_date:
        paths.append(_sector_stocks_history_path(sector_code, as_of_date))
    paths.append(_sector_stocks_cache_path(sector_code))
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = _protect_legacy_stock_amounts(
                json.loads(path.read_text(encoding="utf-8")))
            cached_at = datetime.fromisoformat(payload["cached_at"])
            age = datetime.now() - cached_at
            if age.total_seconds() > SECTOR_STOCKS_MAX_AGE_HOURS * 3600:
                continue
            if not payload.get("stocks"):
                continue
            if as_of_date and payload.get("data_date") != as_of_date:
                continue
            return payload
        except (KeyError, ValueError, json.JSONDecodeError, OSError):
            continue
    return None


def _sector_cache_metadata(cached_at: str) -> dict:
    """Return bounded, display-safe age metadata for a cache snapshot."""
    metadata = {
        "cached_at": str(cached_at or ""),
        "age_hours": None,
        "tier": "unknown",
    }
    try:
        cached_dt = datetime.fromisoformat(str(cached_at))
        if cached_dt.tzinfo is not None:
            cached_dt = cached_dt.replace(tzinfo=None)
        age_hours = max(0.0, (datetime.now() - cached_dt).total_seconds() / 3600)
    except (TypeError, ValueError):
        return metadata
    metadata["age_hours"] = round(age_hours, 1)
    if cached_dt.date() == datetime.now().date():
        metadata["tier"] = "same_day"
    elif age_hours <= SECTOR_STOCKS_RECENT_CACHE_MAX_AGE_HOURS:
        metadata["tier"] = "recent"
    else:
        metadata["tier"] = "old"
    return metadata


def _tag_sector_stocks(stocks: list[dict], source: str, data_date: str,
                       quality: str, cache_metadata: dict | None = None,
                       fallback_reason: str = "",
                       provider_attempts: int = 0) -> list[dict]:
    cache_metadata = cache_metadata or {}
    return [{
        **stock,
        "membership_source": source,
        "membership_data_date": data_date,
        "membership_quality": quality,
        "membership_cache_at": cache_metadata.get("cached_at", ""),
        "membership_cache_age_hours": cache_metadata.get("age_hours"),
        "membership_cache_tier": cache_metadata.get("tier", ""),
        "membership_fallback_reason": fallback_reason,
        "membership_provider_attempts": max(
            0, int(provider_attempts or 0)),
    } for stock in stocks]


def _load_tagged_sector_stocks_cache(sector_code: str,
                                     top_n: int, fallback_reason: str = "",
                                     provider_attempts: int = 0,
                                     as_of_date: str = "") -> list[dict]:
    cached = load_sector_stocks_cache(sector_code, as_of_date=as_of_date)
    if not cached:
        return []
    fetched_at = cached.get("fetched_at") or cached.get("cached_at", "")
    cache_metadata = _sector_cache_metadata(fetched_at)
    data_date = str(cached.get("data_date") or "")[:10]
    trusted_fallback = fallback_reason in {
        "dns", "timeout", "connection_error", "proxy_error", "http",
        "cache_only",
    } or fallback_reason.startswith("cache_only_")
    historical_verified = bool(
        as_of_date and data_date == as_of_date
        and cached.get("provider") == "eastmoney")
    cache_quality = (
        "historical_verified" if historical_verified else
        "same_day_verified"
        if cached.get("provider") == "eastmoney"
        and cache_metadata.get("tier") == "same_day"
        and data_date == datetime.now().strftime("%Y-%m-%d")
        and trusted_fallback
        else "degraded"
    )
    return _tag_sector_stocks(
        cached["stocks"],
        source="cache",
        data_date=data_date,
        quality=cache_quality,
        cache_metadata=cache_metadata,
        fallback_reason=fallback_reason,
        provider_attempts=provider_attempts,
    )[:top_n]


def get_sector_stocks_cached(sector_code: str, top_n: int = 50,
                             fallback_reason: str = "cache_only",
                             as_of_date: str = "") -> list[dict]:
    """Return cached constituents without attempting a live request."""
    _sector_stocks_cache_path(sector_code)
    return _load_tagged_sector_stocks_cache(
        sector_code, top_n, fallback_reason=fallback_reason,
        as_of_date=as_of_date)


def _sector_stocks_fallback_or_raise(sector_code: str, top_n: int,
                                     reason: str, as_of_date: str = "") -> list[dict]:
    cached_stocks = _load_tagged_sector_stocks_cache(
        sector_code, top_n, as_of_date=as_of_date)
    if cached_stocks:
        return cached_stocks
    raise RuntimeError(
        f"获取板块{sector_code}成分股失败: {reason}; 无有效成分股且无可用快照")


def get_sector_stocks(sector_code: str, top_n: int = 50,
                      timeout: int = 15, retries: int = 3,
                      with_evidence: bool = False,
                      deadline: float | None = None,
                      as_of_date: str = "") -> list[dict]:
    """Fetch constituent stocks for a sector.

    Args:
        sector_code: e.g. "BKxxx".
        top_n: max stocks to return.

    Returns:
        List of {code, name, change_pct, amount, turnover_rate,
        market_cap, pe}.
    """
    _sector_stocks_cache_path(sector_code)
    today_iso = datetime.now().strftime("%Y-%m-%d")
    if as_of_date and as_of_date != today_iso:
        cached = _load_tagged_sector_stocks_cache(
            sector_code, top_n, fallback_reason="historical_snapshot",
            as_of_date=as_of_date)
        if cached:
            wrapped = source_result(cached, live_attempt(
                attempted=False, cache_used=True, stale=False))
            return wrapped if with_evidence else cached
        raise ProviderFetchError(
            f"板块{sector_code}缺少{as_of_date}历史成分快照",
            provider_attempts=0, reason="historical_snapshot_missing")
    today = datetime.now().strftime("%Y%m%d")
    # b:BKxxx filters stocks belonging to this sector
    url = (
        f"https://push2.eastmoney.com/api/qt/clist/get"
        f"?fs=b:{sector_code}"
        f"&fields=f2,f3,f4,f6,f8,f12,f14,f20,f21,f23,f37,f62,f124,f168,f170,f171"
        f"&pn=1&pz={top_n}&po=0&np=1&fltt=2"
        f"&fid=f3&_={today}"
    )
    provider_attempts = 0
    failure_reason = ""
    try:
        fetched = _fetch_json(
            url, timeout=timeout, retries=retries,
            # Keep attempt counts even when callers request plain stock rows.
            with_evidence=True, deadline=deadline)
        data, attempt = _unpack_fetch_result(fetched)
        if attempt:
            provider_attempts = attempt.get("provider_attempts", 0)
        response_data = _check_result(data)
        items = response_data.get("diff", [])
        if isinstance(items, dict):
            items = list(items.values())
        try:
            membership_total_count = int(response_data.get("total"))
            if membership_total_count < 0:
                membership_total_count = None
        except (TypeError, ValueError):
            membership_total_count = None
    except Exception as e:
        provider_attempts += getattr(e, "provider_attempts", 0)
        failure_reason = getattr(e, "reason", "") or classify_failure(e)
        cached = _load_tagged_sector_stocks_cache(
            sector_code, top_n, fallback_reason=failure_reason,
            provider_attempts=provider_attempts, as_of_date=as_of_date)
        if cached:
            wrapped = source_result(cached, live_attempt(
                attempted=provider_attempts > 0,
                provider_attempts=provider_attempts,
                reason=failure_reason,
                cache_used=True, stale=True))
            return wrapped if with_evidence else cached
        raise RuntimeError(
            f"获取板块{sector_code}成分股失败: {e}; "
            "无有效成分股且无可用快照") from e
    if not items:
        failure_reason = "empty"
        cached = _load_tagged_sector_stocks_cache(
            sector_code, top_n, fallback_reason=failure_reason,
            provider_attempts=provider_attempts, as_of_date=as_of_date)
        if cached:
            wrapped = source_result(cached, live_attempt(
                attempted=True, provider_attempts=provider_attempts,
                reason=failure_reason, cache_used=True, stale=True))
            return wrapped if with_evidence else cached
        raise RuntimeError(
            f"获取板块{sector_code}成分股失败: 实时接口返回空列表; "
            "无有效成分股且无可用快照")

    stocks = []
    provider_timestamps = []
    for item in items:
        provider_timestamp = _provider_timestamp(item.get("f124"))
        provider_timestamps.append(provider_timestamp)
        stock = {
            "code": item.get("f12", ""),
            "name": item.get("f14", ""),
            "change_pct": item.get("f3"),
            "amount": _nonnegative_number_or_none(item.get("f6")),
            "turnover_rate": _nonnegative_number_or_none(item.get("f8")),
            "market_cap": item.get("f20"),   # 总市值
            "pe": item.get("f37"),           # 动态市盈率
            "provider_timestamp": provider_timestamp,
        }
        if stock["code"]:
            stocks.append(stock)
    if not stocks:
        failure_reason = "empty"
        cached = _load_tagged_sector_stocks_cache(
            sector_code, top_n, fallback_reason=failure_reason,
            provider_attempts=provider_attempts, as_of_date=as_of_date)
        if cached:
            wrapped = source_result(cached, live_attempt(
                attempted=True, provider_attempts=provider_attempts,
                reason=failure_reason, cache_used=True, stale=True))
            return wrapped if with_evidence else cached
        raise RuntimeError(
            f"获取板块{sector_code}成分股失败: "
            "实时接口未返回有效股票代码; 无有效成分股且无可用快照")
    unique_codes = {stock["code"] for stock in stocks}
    membership_returned_count = len(unique_codes)
    membership_coverage_complete = bool(
        membership_total_count is not None
        and top_n >= membership_total_count
        and membership_returned_count == membership_total_count
        and len(stocks) == membership_returned_count
    )
    for stock in stocks:
        stock["membership_total_count"] = membership_total_count
        stock["membership_returned_count"] = membership_returned_count
        stock["membership_coverage_complete"] = membership_coverage_complete
    cache_error = ""
    timestamp_dates = {value[:10] for value in provider_timestamps if value}
    data_date = (
        next(iter(timestamp_dates))
        if len(provider_timestamps) == len(stocks) and all(provider_timestamps) and len(timestamp_dates) == 1
        else ""
    )
    if _verified_trading_date(data_date) is None:
        data_date = ""
    if stocks:
        try:
            save_sector_stocks_cache(
                sector_code, stocks, data_date=data_date,
                requested_date=as_of_date or today_iso)
        except OSError as exc:
            cache_error = str(exc)
            print(
                f"  Warning: 板块{sector_code}成分股快照保存失败: {exc}",
                file=sys.stderr,
            )
    tagged = _tag_sector_stocks(
        stocks,
        source="realtime",
        data_date=data_date,
        quality=("partial" if cache_error or not data_date else "good"),
        provider_attempts=provider_attempts,
    )
    if cache_error:
        for stock in tagged:
            stock["membership_cache_error"] = cache_error
    if with_evidence:
        return source_result(tagged, live_attempt(
            attempted=True, provider_attempts=provider_attempts))
    return tagged


def _to_float(val: Any) -> float:
    """Parse amount/成交额 value, return in yuan."""
    if val is None:
        return 0.0
    return float(val)


def filter_leaders(stocks: list[dict], top_n: int = 3,
                   min_market_cap: float = 5e9,
                   max_market_cap: float = 5e11) -> list[dict]:
    """Filter leader stocks (龙头) from sector constituents.

    Criteria: phase return(50%) + turnover/amount(30%) + volume breakout(20%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of leaders to return.
        min_market_cap: minimum market cap in yuan (default 50亿).
        max_market_cap: maximum market cap in yuan (default 5000亿).

    Returns:
        Top N leaders sorted by composite leader score.
    """
    # Pre-filter: ST removal + market cap bounds
    filtered = []
    for s in stocks:
        name = s.get("name", "")
        if any(kw in name for kw in ("ST", "*ST", "退")):
            continue
        mcap = _to_float(s.get("market_cap"))
        if mcap < min_market_cap or mcap > max_market_cap:
            continue
        filtered.append(s)

    # Compute median amount for breakout detection
    amounts = [_to_float(s.get("amount")) for s in filtered if _to_float(s.get("amount")) > 0]
    median_amount = sorted(amounts)[len(amounts) // 2] if amounts else 1e8

    scored = []
    for s in filtered:
        change = s.get("change_pct") or 0
        amount = _to_float(s.get("amount"))

        # Leader score: today's change proxies phase return + volume
        change_score = min(100, max(0, 50 + change * 5))    # 0%→50, +10%→100
        amount_score = min(100, amount / 1e7)                # scaled

        # Volume breakout bonus: change > 3% AND amount > 1.5x sector median
        breakout_bonus = 0
        if change > 3.0 and median_amount > 0 and amount > median_amount * 1.5:
            breakout_bonus = min(100, max(0, 50 + (amount / median_amount - 1) * 50))

        leader_score = change_score * 0.50 + amount_score * 0.30 + breakout_bonus * 0.20

        s["leader_score"] = round(leader_score, 1)
        s["_volume_breakout"] = breakout_bonus > 0
        scored.append(s)

    scored.sort(key=lambda x: x.get("leader_score", 0), reverse=True)
    return scored[:top_n]


def filter_core_stocks(stocks: list[dict], top_n: int = 3,
                       min_market_cap: float = 5e9,
                       max_market_cap: float = 5e11) -> list[dict]:
    """Filter core stocks (中军) from sector constituents.

    Criteria: market cap(35%) + fundamentals/pe(35%) + stability(15%) + laggard bonus(15%)

    Args:
        stocks: list from get_sector_stocks().
        top_n: number of core stocks to return.
        min_market_cap: minimum market cap in yuan (default 50亿).
        max_market_cap: maximum market cap in yuan (default 5000亿).

    Returns:
        Top N core stocks sorted by composite core score.
    """
    # Pre-filter: ST removal + market cap bounds
    filtered = []
    for s in stocks:
        name = s.get("name", "")
        if any(kw in name for kw in ("ST", "*ST", "退")):
            continue
        mcap = _to_float(s.get("market_cap"))
        if mcap < min_market_cap or mcap > max_market_cap:
            continue
        filtered.append(s)

    # Precompute change percentiles for laggard detection
    changes = sorted([s.get("change_pct") or 0 for s in filtered])

    def _pct_rank(val, sorted_vals):
        """Percentile rank of val in sorted_vals (0-100)."""
        if not sorted_vals:
            return 50
        n = len(sorted_vals)
        rank = sum(1 for v in sorted_vals if v < val)
        return rank / n * 100

    scored = []
    for s in filtered:
        mcap = s.get("market_cap") or 0
        pe = s.get("pe") or 0
        change = s.get("change_pct") or 0

        # Market cap: >1000亿 → 100, >500亿 → 80, >100亿 → 60
        cap_score = min(100, max(0, _to_float(mcap) / 1e8 * 5))
        # PE reasonableness: 10-30 ideal, very high or negative penalized
        if pe is not None and pe > 0:
            pe_score = max(0, 100 - abs(pe - 20) * 1.5)
        else:
            pe_score = 30  # negative PE = unclear
        # Stability: moderate change (0~5%) preferred over extreme
        stability_score = max(0, 100 - abs(change) * 10)

        # Laggard bonus: underperformed within sector but has reasonable PE
        pct_rank = _pct_rank(change, changes)
        laggard_bonus = 0
        if pct_rank < 50 and pe and 0 < pe < 30:
            laggard_bonus = min(20, (50 - pct_rank) * 0.4)

        core_score = (cap_score * 0.35 + pe_score * 0.35
                      + stability_score * 0.15 + laggard_bonus * 0.15)
        s["core_score"] = round(core_score, 1)
        s["_is_laggard"] = laggard_bonus > 0
        scored.append(s)

    scored.sort(key=lambda x: x.get("core_score", 0), reverse=True)
    return scored[:top_n]


# ──────────────────────── Rankings Cache ────────────────────────

CACHE_FILE = CACHE_DIR / "sector_rankings_cache.json"
AKSHARE_CACHE_NAME = "sector_rankings_akshare_cache.json"
MAX_CACHE_AGE_HOURS = 96  # 4 days, covers long weekends

# A-share market hours: 9:30-11:30, 13:00-15:00 CST
_MARKET_OPEN_MINUTES = (9 * 60 + 30, 15 * 60)  # 570, 900


def _is_outside_market_hours(dt: datetime) -> bool:
    """Check if datetime is outside A-share trading hours.

    Cache written during market hours (9:30-15:00) may contain
    incomplete mid-session data — reject for weekend fallback.
    """
    t = dt.hour * 60 + dt.minute
    return t < _MARKET_OPEN_MINUTES[0] or t >= _MARKET_OPEN_MINUTES[1]


def _rankings_cache_file(provider: str = "eastmoney") -> Path:
    """Return a provider-isolated ranking cache path."""
    if provider in ("eastmoney", "realtime", ""):
        return CACHE_FILE
    return CACHE_DIR / AKSHARE_CACHE_NAME


def _rankings_cache_candidates(provider: str = "eastmoney") -> list[Path]:
    """Return isolated cache first, with a read-only legacy AKShare fallback."""
    primary = _rankings_cache_file(provider)
    candidates = [primary]
    if provider not in ("eastmoney", "realtime", "") and primary != CACHE_FILE:
        # Before provider isolation existed, a successful AKShare fallback was
        # written into CACHE_FILE.  Read that legacy payload only after it is
        # proven to be an AKShare/THS payload; never treat it as EM data.
        candidates.append(CACHE_FILE)
    return candidates


def save_rankings_cache(rankings: dict, hot_sectors: Optional[list] = None,
                        data_date: str = "") -> None:
    """Save sector rankings snapshot for non-trading-day fallback.

    Also saves pre-computed hot_sectors if provided, so non-trading day
    can return yesterday's actual hot sector rankings rather than
    regenerating from stale data.

    Does NOT overwrite existing cache if today's data has no real sector
    activity (non-trading day). This preserves the last trading day's cache.
    """
    meta = rankings.get("meta", {})
    if meta.get("complete") is False:
        return
    provider = meta.get("provider", "")
    source = meta.get("source", "")
    # A THS/AKShare ranking has name/ordinal identity and must never replace
    # the EM BK ranking cache.  It is still useful as an isolated fallback.
    if not provider:
        provider = "eastmoney" if source in ("", "eastmoney", "realtime") \
            else "akshare"
    cache_file = _rankings_cache_file(provider)
    date_key = _resolve_verified_data_date(rankings, data_date)
    # A cache without an upstream/explicit trading date is not safe to use as
    # a historical baseline.  Keep the live result in memory only.
    if not date_key:
        return
    sectors = rankings.get("sectors", [])
    active = sum(
        1 for s in sectors
        if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
    )
    # Never cache zero-activity data (non-trading day).
    # Without this guard, first run on a weekend creates a zero cache
    # that drowns the last trading day's data.
    if active == 0:
        return
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RANKINGS_CACHE_SCHEMA_VERSION,
        "field_semantics_version": SECTOR_FIELD_SEMANTICS_VERSION,
        "cached_at": datetime.now().isoformat(),
        "data_date": date_key,
        "date_origin": "provider",
        "rankings": rankings,
    }
    if hot_sectors:
        payload["hot_sectors"] = hot_sectors
    cache_file.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _protect_legacy_ranking_amounts(rankings: dict) -> tuple[dict, bool]:
    """Protect old amount/coverage fields without inventing provider semantics."""
    meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    if meta.get("field_semantics_version") == SECTOR_FIELD_SEMANTICS_VERSION:
        return rankings, False
    provider = meta.get("provider", "")
    source = meta.get("source", "")
    eastmoney_f8 = provider in ("", "eastmoney", "realtime") and source != "akshare"
    protected = dict(rankings)
    protected_meta = dict(meta)
    protected_meta["cache_semantics_status"] = "legacy_protected"
    protected_meta["field_semantics_version"] = "legacy_unknown"
    claimed_date = meta.get("data_date")
    protected_meta["legacy_claimed_data_date"] = claimed_date
    protected_meta["requested_date"] = meta.get("requested_date") or claimed_date
    protected_meta["data_date"] = None
    protected_meta["date_origin"] = "unknown"
    protected_meta["alignment"] = "unknown"
    protected["meta"] = protected_meta
    protected_sectors = []
    for raw in rankings.get("sectors") or []:
        sector = dict(raw)
        ambiguous = sector.get("amount")
        if eastmoney_f8 and "turnover_rate" not in sector:
            sector["turnover_rate"] = ambiguous
        sector["amount"] = None
        sector["amount_quality"] = "legacy_ambiguous_f8" if eastmoney_f8 else "legacy_unverified_amount"
        up = sector.get("up_count")
        down = sector.get("down_count")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
               for value in (up, down)):
            sector["up_down_sample_count"] = up + down
        elif isinstance(sector.get("total_count"), (int, float)) \
                and not isinstance(sector.get("total_count"), bool):
            sector["up_down_sample_count"] = sector["total_count"]
        else:
            sector["up_down_sample_count"] = None
        sector["total_count"] = None
        sector["flat_count"] = None
        sector["suspended_count"] = None
        sector["uncovered_count"] = None
        sector["constituent_coverage_complete"] = False
        protected_sectors.append(sector)
    protected["sectors"] = protected_sectors
    return protected, True


def _protect_rankings_cache_payload(payload: dict) -> dict:
    """Attach cache provenance and protect pre-v2 East Money field semantics."""
    protected = dict(payload)
    rankings, changed = _protect_legacy_ranking_amounts(
        payload.get("rankings", {}))
    wrapper_claimed_date = payload.get("data_date")
    ranking_meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    coverage_reasons = []
    diagnostics = ranking_meta.get("ranking_diagnostics", {}) \
        if isinstance(ranking_meta, dict) else {}
    if isinstance(diagnostics, dict):
        for source_name, diagnostic in diagnostics.items():
            if not isinstance(diagnostic, dict):
                continue
            total = _nonnegative_int_or_none(
                diagnostic.get("reported_total"))
            covered = _nonnegative_int_or_none(
                diagnostic.get("unique_rows"))
            if covered is None:
                covered = _nonnegative_int_or_none(
                    diagnostic.get("valid_rows"))
            if total is not None and covered is not None and covered < total:
                coverage_reasons.append(
                    f"{source_name}:coverage_mismatch")
    if coverage_reasons:
        rankings = dict(rankings)
        ranking_meta = dict(ranking_meta)
        ranking_meta["complete"] = False
        ranking_meta["coverage_status"] = "partial"
        ranking_meta["coverage_reasons"] = coverage_reasons
        rankings["meta"] = ranking_meta
    wrapper_date = _verified_trading_date(wrapper_claimed_date)
    ranking_date = _verified_trading_date(ranking_meta.get("data_date"))
    provider_date_proven = bool(
        not changed
        and wrapper_date
        and ranking_date == wrapper_date
        and ranking_meta.get("date_origin") == "provider"
    )
    if not provider_date_proven:
        protected["legacy_claimed_data_date"] = wrapper_claimed_date
        protected["requested_date"] = (
            payload.get("requested_date") or wrapper_claimed_date)
        protected["data_date"] = None
        protected["date_origin"] = "unknown"
    provenance = {
        "schema_version": payload.get("schema_version"),
        "field_semantics_version": payload.get("field_semantics_version"),
        "cached_at": payload.get("cached_at"),
        "data_date": protected.get("data_date"),
        "legacy_claimed_data_date": (
            protected.get("legacy_claimed_data_date")),
        "date_origin": protected.get("date_origin", "provider"),
        "semantics_status": (
            "legacy_protected" if changed
            else "verified" if provider_date_proven
            else "unverified"
        ),
    }
    rankings = dict(rankings)
    rankings_meta = dict(rankings.get("meta", {}))
    rankings_meta["cache_provenance"] = dict(provenance)
    rankings["meta"] = rankings_meta
    protected["rankings"] = rankings
    if changed and isinstance(payload.get("hot_sectors"), list):
        hot_wrapper, _ = _protect_legacy_ranking_amounts({
            "meta": payload.get("rankings", {}).get("meta", {}),
            "sectors": payload["hot_sectors"],
        })
        protected["hot_sectors"] = hot_wrapper["sectors"]
    protected["cache_provenance"] = provenance
    return protected


def load_rankings_cache(provider: str = "eastmoney",
                        max_age_hours: float | None = None) -> Optional[dict]:
    """Load cached sector rankings if fresh and has any sector data.

    Returns the rankings dict or None if expired / corrupted.
    Accepts caches from any time of day (including mid-session).
    Even a sparse intraday cache is more useful than multi-week stale
    BK K-line fallback.
    """
    for cache_file in _rankings_cache_candidates(provider):
        if not cache_file.exists():
            continue
        try:
            payload = _protect_rankings_cache_payload(
                json.loads(cache_file.read_text(encoding="utf-8")))
            cached_at = datetime.fromisoformat(payload["cached_at"])
            age = datetime.now() - cached_at
            age_limit = (MAX_CACHE_AGE_HOURS if max_age_hours is None
                         else max(0.0, float(max_age_hours)))
            if age.total_seconds() > age_limit * 3600:
                continue
            data_date = _verified_trading_date(payload.get("data_date", ""))
            legacy_readable = (
                payload.get("cache_provenance", {}).get("semantics_status")
                in {"legacy_protected", "unverified"})
            if ((not data_date and not legacy_readable)
                    or (data_date
                        and data_date > datetime.now().strftime("%Y-%m-%d"))):
                continue
            # Must have at least some sectors (not an empty cache)
            rankings = payload.get("rankings", {})
            if rankings.get("meta", {}).get("complete") is False:
                continue
            cached_meta = rankings.get("meta", {})
            cached_provider = cached_meta.get("provider", "")
            cached_source = cached_meta.get("source", "")
            if provider == "eastmoney" and (
                    cached_provider not in ("", "eastmoney")
                    or cached_source == "akshare"):
                continue
            if provider not in ("eastmoney", "realtime", "") and \
                    cached_provider not in ("", "ths", "akshare") \
                    and cached_source != "akshare":
                continue
            sectors = rankings.get("sectors", [])
            if len(sectors) < 5:
                continue
            return rankings
        except Exception:
            continue
    return None


def load_rankings_cache_full(provider: str = "eastmoney",
                             max_age_hours: float | None = None) -> Optional[dict]:
    """Load full cached payload including hot_sectors if present.

    Returns the raw payload dict, or None if expired / corrupted.
    """
    for cache_file in _rankings_cache_candidates(provider):
        if not cache_file.exists():
            continue
        try:
            payload = _protect_rankings_cache_payload(
                json.loads(cache_file.read_text(encoding="utf-8")))
            cached_at = datetime.fromisoformat(payload["cached_at"])
            age = datetime.now() - cached_at
            age_limit = (MAX_CACHE_AGE_HOURS if max_age_hours is None
                         else max(0.0, float(max_age_hours)))
            if age.total_seconds() > age_limit * 3600:
                continue
            data_date = _verified_trading_date(payload.get("data_date", ""))
            legacy_readable = (
                payload.get("cache_provenance", {}).get("semantics_status")
                in {"legacy_protected", "unverified"})
            if ((not data_date and not legacy_readable)
                    or (data_date
                        and data_date > datetime.now().strftime("%Y-%m-%d"))):
                continue
            rankings = payload.get("rankings", {})
            if rankings.get("meta", {}).get("complete") is False:
                continue
            cached_meta = rankings.get("meta", {})
            cached_provider = cached_meta.get("provider", "")
            cached_source = cached_meta.get("source", "")
            if provider == "eastmoney" and (
                    cached_provider not in ("", "eastmoney")
                    or cached_source == "akshare"):
                continue
            if provider not in ("eastmoney", "realtime", "") and \
                    cached_provider not in ("", "ths", "akshare") \
                    and cached_source != "akshare":
                continue
            return payload
        except Exception:
            continue
    return None


# ──────────────────────── Snapshot History ────────────────────────
# Daily sector snapshot history preserves the Top-30 sector ranking archive.
# Successful realtime scans append a snapshot for compatibility consumers.

SNAPSHOT_FILE = CACHE_DIR / "sector_snapshot_history.json"
SNAPSHOT_MAX_DAYS = 30  # auto-prune older snapshots
CANDIDATE_SNAPSHOT_FILE = CACHE_DIR / "candidate_sector_history.json"
CANDIDATE_SNAPSHOT_SCHEMA_VERSION = 1
CANDIDATE_SNAPSHOT_MAX_DAYS = 30


def _hot_ranked_sectors(rankings: dict, top_n: int = 30) -> list[dict]:
    """Extract top N sectors sorted by composite hot score.

    Same filtering as rank_hot_sectors but lighter — always applies
    min_stocks=4, min_up_ratio=0.05 to avoid degenerate data.
    Used for snapshot history (archival quality, not display quality).
    """
    sectors = rankings.get("sectors", [])
    # Filter tiny / dead sectors
    sectors = [
        s for s in sectors
        if (s.get("up_count", 0) or 0) + (s.get("down_count", 0) or 0) >= 4
        and _up_ratio(s) >= 0.05
    ]
    for s in sectors:
        s["hot_score"] = compute_hot_score(s)
    sectors.sort(key=lambda x: x.get("hot_score", 0), reverse=True)
    # Min-max normalize
    if sectors:
        scores = [s["hot_score"] for s in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for s in sectors:
                s["hot_score"] = round(
                    (s["hot_score"] - lo) / (hi - lo) * 100, 1
                )
    return sectors[:top_n]


def append_daily_snapshot(rankings: dict, override_date: str = "") -> None:
    """Append today's sector snapshot to history file.

    Called after successful realtime ranking fetch (NOT on non-trading
    days).  Stores compact sector summaries keyed by date for fast
    persistence loading.

    Args:
        rankings: output from get_sector_rankings().
        override_date: force date string YYYY-MM-DD (for testing).
    """
    sectors = rankings.get("sectors", [])
    active = sum(
        1 for s in sectors
        if (s.get("up_count", 0) or 0) > 0 or (s.get("down_count", 0) or 0) > 0
    )
    if active == 0:
        return  # non-trading day, skip

    date_key = _resolve_verified_data_date(rankings, override_date)
    # Never stamp a provider response with the wall-clock date when it did not
    # identify the represented session.  This is a no-op rather than a
    # guessed historical record.
    if not date_key:
        return

    # Compute top 30 summaries
    top = _hot_ranked_sectors(rankings, top_n=30)
    summary = []
    for s in top:
        up = s.get("up_count", 0) or 0
        down = s.get("down_count", 0) or 0
        total = up + down
        summary.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "hot_score": s.get("hot_score", 0),
            "change_pct": s.get("change_pct"),
            "net_flow": s.get("main_force_net"),
            "up_ratio": round(up / total, 3) if total > 0 else 0,
            "rank": len(summary) + 1,
        })

    # Load existing history, update, save.  Invalid legacy keys are retained
    # on disk for the explicit migration/quarantine command, but are not
    # allowed to participate in the active history.
    history = {}
    if SNAPSHOT_FILE.exists():
        try:
            history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            pass
    history[date_key] = summary

    # Auto-prune: keep last SNAPSHOT_MAX_DAYS
    dates = sorted(d for d in history if _verified_trading_date(d))
    if len(dates) > SNAPSHOT_MAX_DAYS:
        keep = set(dates[-SNAPSHOT_MAX_DAYS:])
        history = {k: v for k, v in history.items() if k in keep}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")


def _candidate_snapshot_row(sector: dict, position: int) -> dict:
    """Keep the compact fields needed by candidate persistence analysis."""
    up = sector.get("up_count", 0) or 0
    down = sector.get("down_count", 0) or 0
    total = up + down
    relative_hot = sector.get("relative_hot_score", sector.get("hot_score", 0))
    return {
        "code": sector.get("code", ""),
        "name": sector.get("name", ""),
        "rank": sector.get("rank", sector.get("ranking_position", position)),
        "absolute_hot_score": sector.get("absolute_hot_score", 0),
        "relative_hot_score": relative_hot,
        # Keep hot_score as a compatibility alias for simple consumers.
        "hot_score": relative_hot,
        "change_pct": sector.get("change_pct"),
        "net_flow": sector.get("net_flow", sector.get("main_force_net")),
        "up_ratio": (
            round(up / total, 3) if total > 0
            else sector.get("up_ratio", 0)
        ),
    }


def _atomic_json_write(path: Path, payload: dict) -> None:
    """Write JSON through a same-directory temporary file and replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def commit_candidate_sector_snapshot(
        rankings: dict, data_date: str, min_stocks: int = 10,
        min_up_ratio: float = 0.15) -> dict:
    """Rank and persist one complete BK candidate-sector snapshot."""
    meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    sectors = rankings.get("sectors", []) if isinstance(rankings, dict) else []
    if not isinstance(meta, dict) or not isinstance(sectors, list) \
            or not sectors or meta.get("complete") is not True:
        return {"status": "incomplete", "ranked_count": 0}
    if meta.get("source", "eastmoney") not in ("eastmoney", "realtime"):
        return {"status": "unsupported_source", "ranked_count": 0}
    sources = meta.get("sources")
    if sources is not None:
        if not isinstance(sources, dict) or not all(
                sources.get(name) == "ok"
                for name in ("industry", "concept")):
            return {"status": "incomplete", "ranked_count": 0}

    if _resolve_verified_data_date(rankings, data_date) is None:
        return {"status": "incomplete", "ranked_count": 0,
                "reason": "ranking_source_date_unverified"}

    meta.setdefault("source", "eastmoney")
    ranked = rank_hot_sectors(
        rankings, top_n=None,
        min_stocks=min_stocks, min_up_ratio=min_up_ratio,
    )
    if not ranked:
        return {"status": "incomplete", "ranked_count": 0}
    append_candidate_sector_snapshot(
        rankings,
        ranked=ranked,
        override_date=data_date,
        filter_meta={
            "min_stocks": min_stocks,
            "min_up_ratio": min_up_ratio,
            "deduplicated": True,
        },
    )
    return {
        "status": "saved",
        "data_date": data_date,
        "universe_count": len(sectors),
        "ranked_count": len(ranked),
    }


def append_candidate_sector_snapshot(
        rankings: dict, ranked: Optional[list[dict]] = None,
        override_date: str = "", filter_meta: Optional[dict] = None) -> None:
    """Append a complete candidate-universe sector ranking snapshot.

    This history intentionally has a different contract from
    ``sector_snapshot_history.json``: it records the full ranked universe used
    by ``/candidates`` so an absent sector can be distinguished from a hot
    sector that was not in the market Top-30 archive.
    """
    meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    if not isinstance(meta, dict) or meta.get("complete") is not True:
        return
    sectors = rankings.get("sectors", [])
    if not isinstance(sectors, list):
        return
    active = sum(
        1 for sector in sectors
        if (sector.get("up_count", 0) or 0) > 0
        or (sector.get("down_count", 0) or 0) > 0
    )
    if active == 0:
        return

    date_key = _resolve_verified_data_date(rankings, override_date)
    if not date_key:
        return
    if ranked is None:
        ranked = rank_hot_sectors(
            rankings, top_n=None, min_stocks=0, min_up_ratio=0)
    if not isinstance(ranked, list):
        return

    rows = [
        _candidate_snapshot_row(sector, position)
        for position, sector in enumerate(ranked, start=1)
        if isinstance(sector, dict) and sector.get("code")
    ]
    if not rows:
        return

    universe_count = meta.get("total_sectors")
    try:
        universe_count = int(universe_count)
    except (TypeError, ValueError):
        universe_count = len(sectors)
    record = {
        "data_date": date_key,
        "saved_at": datetime.now().isoformat(),
        "complete": True,
        "quality": "good",
        "source": meta.get("source", "realtime"),
        "universe_count": max(0, universe_count),
        "ranked_count": len(rows),
        "filter": dict(filter_meta or {}),
        "sectors": rows,
    }

    history = {}
    if CANDIDATE_SNAPSHOT_FILE.exists():
        try:
            payload = json.loads(
                CANDIDATE_SNAPSHOT_FILE.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid candidate snapshot history schema")
            if "snapshots" in payload:
                if not isinstance(payload["snapshots"], dict):
                    raise ValueError(
                        "invalid candidate snapshot history snapshots")
                history = dict(payload["snapshots"])
            else:
                # Tolerate an early date-keyed version of this new file.
                history = {
                    key: value for key, value in payload.items()
                    if key != "schema_version"
                }
        except OSError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid candidate snapshot history") from exc
    history[date_key] = record
    valid_dates = sorted(
        key for key, value in history.items()
        if _verified_trading_date(key)
        and key <= datetime.now().strftime("%Y-%m-%d")
        and isinstance(value, dict)
    )
    keep = valid_dates[-CANDIDATE_SNAPSHOT_MAX_DAYS:]
    history = {key: history[key] for key in keep}
    _atomic_json_write(CANDIDATE_SNAPSHOT_FILE, {
        "schema_version": CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
        "snapshots": history,
    })


def load_candidate_sector_history(
        days: int = 10, errors: Optional[list[str]] = None) -> dict[str, dict]:
    """Load recent candidate-universe snapshots, including partial records."""
    def record_error(reason):
        if isinstance(errors, list):
            errors.append(reason)

    if not CANDIDATE_SNAPSHOT_FILE.exists() or days <= 0:
        return {}
    try:
        payload = json.loads(
            CANDIDATE_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except OSError:
        record_error("read_error")
        return {}
    except json.JSONDecodeError:
        record_error("invalid_json")
        return {}
    except TypeError:
        record_error("invalid_json")
        return {}
    if not isinstance(payload, dict):
        record_error("invalid_schema")
        return {}
    if "snapshots" in payload and not isinstance(payload["snapshots"], dict):
        record_error("invalid_schema")
        return {}
    history = payload.get("snapshots", payload)
    if not isinstance(history, dict):
        record_error("invalid_schema")
        return {}
    reference_date = datetime.now().strftime("%Y-%m-%d")
    valid = {
        key: value for key, value in history.items()
        if _verified_trading_date(key)
        and key <= reference_date
        and isinstance(value, dict)
    }
    dates = sorted(valid)
    recent = dates[-days:] if len(dates) > days else dates
    return {date_key: valid[date_key] for date_key in recent}


def load_snapshot_history(days: int = 10) -> dict[str, list[dict]]:
    """Load snapshot history for the last N trading days.

    Returns dict mapping date YYYY-MM-DD -> list of sector summaries.
    Each summary has: code, name, hot_score, change_pct, up_ratio, rank.
    Returns empty dict if no history available.
    """
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return {}
    reference_date = datetime.now().strftime("%Y-%m-%d")
    valid_history = {
        key: value for key, value in history.items()
        if _verified_trading_date(key) and key <= reference_date
        and isinstance(value, list)
    }
    dates = sorted(valid_history)
    recent = dates[-days:] if len(dates) > days else dates
    return {d: valid_history[d] for d in recent}


def quarantine_invalid_snapshot_dates(path=None) -> dict:
    """Back up and quarantine malformed/weekend snapshot keys.

    No key is remapped: the original source date is not recoverable from a
    weekend/compact key alone.  The operation is explicit so normal reads
    remain side-effect free.
    """
    snapshot_path = Path(path or SNAPSHOT_FILE)
    if not snapshot_path.exists():
        return {"status": "missing", "moved": 0}
    try:
        history = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "invalid_file", "moved": 0}
    if not isinstance(history, dict):
        return {"status": "invalid_file", "moved": 0}
    valid, invalid = {}, {}
    reference_date = datetime.now().strftime("%Y-%m-%d")
    for key, value in history.items():
        if (_verified_trading_date(key) and key <= reference_date
                and isinstance(value, list)):
            valid[key] = value
        else:
            invalid[key] = value
    if not invalid:
        return {"status": "clean", "moved": 0}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    backup = snapshot_path.with_name(snapshot_path.name + f".{stamp}.bak")
    quarantine = snapshot_path.with_name(
        snapshot_path.name + f".quarantine-{stamp}.json")
    shutil.copy2(snapshot_path, backup)
    quarantine.write_text(json.dumps(invalid, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    tmp = snapshot_path.with_name(snapshot_path.name + f".tmp-{stamp}")
    try:
        tmp.write_text(json.dumps(valid, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, snapshot_path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return {
        "status": "migrated", "moved": len(invalid),
        "backup": str(backup), "quarantine": str(quarantine),
    }


def _strict_calendar_date(value) -> Optional[str]:
    """Return a strict calendar-valid YYYY-MM-DD value, else None."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = datetime_date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _verified_trading_date(value) -> Optional[str]:
    """Normalize an explicit date only when it is strict ISO and weekday.

    This intentionally does not remap compact dates or weekends.  A weekend
    record may have been produced by a stale fallback and its source session
    cannot be inferred safely from the key alone.
    """
    normalized = _strict_calendar_date(value)
    if normalized is None:
        return None
    return normalized if datetime_date.fromisoformat(normalized).weekday() < 5 else None


def _resolve_verified_data_date(rankings: dict, explicit_date: str = "") -> Optional[str]:
    """Resolve and cross-check the date represented by a ranking payload."""
    meta = rankings.get("meta", {}) if isinstance(rankings, dict) else {}
    upstream_raw = meta.get("data_date", "") if isinstance(meta, dict) else ""
    explicit_raw = explicit_date or ""
    upstream = _verified_trading_date(upstream_raw) if upstream_raw else None
    explicit = _verified_trading_date(explicit_raw) if explicit_raw else None
    if upstream_raw and upstream is None:
        raise ValueError("invalid ranking data_date")
    if explicit_raw and explicit is None:
        raise ValueError("invalid snapshot data_date")
    if upstream and explicit and upstream != explicit:
        raise ValueError("ranking/snapshot data_date mismatch")
    # An explicit/requested day is only an assertion against provider evidence.
    # It can never manufacture the represented market session.
    if not upstream or not isinstance(meta, dict) \
            or meta.get("date_origin") != "provider":
        return None
    resolved = upstream
    if resolved and resolved > datetime.now().strftime("%Y-%m-%d"):
        raise ValueError("future ranking data_date")
    return resolved


def _load_authoritative_trading_dates(now=None) -> set[str]:
    """Load the exchange calendar with a short bounded AKShare refresh."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    try:
        cached = json.loads(TRADING_CALENDAR_FILE.read_text(encoding="utf-8"))
        if cached.get("checked_date") == today:
            cached_dates = cached.get("trading_dates")
            if isinstance(cached_dates, list):
                normalized = [_strict_calendar_date(value)
                              for value in cached_dates]
                if normalized and all(normalized):
                    return set(normalized)
    except (OSError, ValueError, TypeError):
        pass

    result_queue = queue.Queue(maxsize=1)

    def _fetch():
        try:
            import akshare as ak
            frame = ak.tool_trade_date_hist_sina()
            dates = set()
            for value in frame["trade_date"].tolist():
                text = str(value)[:10] if value is not None else ""
                normalized = _strict_calendar_date(text)
                if normalized:
                    dates.add(normalized)
            result_queue.put(dates)
        except Exception:
            result_queue.put(set())

    thread = threading.Thread(target=_fetch, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    if thread.is_alive():
        return set()
    try:
        dates = result_queue.get_nowait()
    except queue.Empty:
        return set()
    if dates:
        try:
            TRADING_CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
            TRADING_CALENDAR_FILE.write_text(json.dumps({
                "checked_date": today,
                "trading_dates": sorted(dates),
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return dates


def get_last_trading_day(now=None) -> tuple[Optional[str], str]:
    """Determine last trading day from best available source.

    Three-tier lookup:
      1. Snapshot history latest date (exact, set by append_daily_snapshot)
      2. Rankings cache data_date (exact, set by save_rankings_cache)
      3. Calendar fallback: today - 1 weekday (approximate)

    Returns:
        (date_str YYYY-MM-DD or None, source_label)
        source_label: "snapshot" | "cache" | "calendar" | ""
    """
    current = now or datetime.now()

    # An explicit current-time request is the recommendation main-path signal
    # to consult the authoritative exchange calendar.  If unavailable, retain
    # the existing cache/snapshot fallback without inventing an open/closed
    # status from stale market data.
    if now is not None and current.weekday() < 5:
        trading_dates = _load_authoritative_trading_dates(current)
        today = current.strftime("%Y-%m-%d")
        eligible = [value for value in trading_dates if value <= today]
        if eligible:
            return max(eligible), (
                "calendar_open" if today in trading_dates
                else "calendar_closed"
            )

    # Tier 1: snapshot history latest date
    if SNAPSHOT_FILE.exists():
        try:
            history = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
            if history and isinstance(history, dict):
                dates = sorted(
                    key for key, value in history.items()
                    if _verified_trading_date(key)
                    and key <= current.strftime("%Y-%m-%d")
                    and isinstance(value, list)
                )
                if dates:
                    return dates[-1], "snapshot"
        except Exception:
            pass

    # Tier 2: verified data date from a fresh rankings cache
    if CACHE_FILE.exists():
        try:
            payload = _protect_rankings_cache_payload(
                json.loads(CACHE_FILE.read_text(encoding="utf-8")))
            cached_at_str = payload.get("cached_at", "")
            data_date = _verified_trading_date(payload.get("data_date", ""))
            if (cached_at_str and data_date
                    and data_date <= current.strftime("%Y-%m-%d")):
                cached_at = datetime.fromisoformat(cached_at_str)
                age = datetime.now() - cached_at
                if age.total_seconds() <= MAX_CACHE_AGE_HOURS * 3600:
                    return data_date, "cache"
        except Exception:
            pass

    # Tier 3: calendar fallback (weekend regression)
    if current.weekday() == 5:   # Saturday → Friday
        prev = current - timedelta(days=1)
        return prev.strftime("%Y-%m-%d"), "calendar"
    elif current.weekday() == 6:  # Sunday → Friday
        prev = current - timedelta(days=2)
        return prev.strftime("%Y-%m-%d"), "calendar"
    # Weekday but might be holiday — can't detect without calendar API
    return None, ""


# ──────────────────────── Main CLI ────────────────────────


def main():
    parser = argparse.ArgumentParser(description="东方财富板块数据获取")
    parser.add_argument("--rankings", action="store_true", help="获取板块排行")
    parser.add_argument("--stocks", type=str, help="获取板块成分股, 参数: BK代码")
    parser.add_argument("--list", action="store_true", help="列出所有板块")
    parser.add_argument("--top", type=int, default=10, help="排行数量")
    parser.add_argument("--min-stocks", type=int, default=8, help="最小成分股数")
    parser.add_argument("-o", "--output", type=str, help="输出JSON文件")

    args = parser.parse_args()

    if args.rankings:
        rankings = get_sector_rankings()
        hot = rank_hot_sectors(rankings, args.top, min_stocks=args.min_stocks)
        output = {
            "meta": rankings["meta"],
            "hot_sectors": hot,
            "total_sectors": rankings["meta"]["total_sectors"],
        }
    elif args.stocks:
        stocks = get_sector_stocks(args.stocks)
        leaders = filter_leaders(stocks)
        cores = filter_core_stocks(stocks)
        output = {
            "sector_code": args.stocks,
            "total_stocks": len(stocks),
            "leaders": leaders,
            "core_stocks": cores,
        }
    elif args.list:
        sectors = get_sector_list()
        output = {"total": len(sectors), "sectors": sectors}
    else:
        parser.print_help()
        return

    out_str = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(out_str, encoding="utf-8")
        print(f"Output: {args.output}")
    else:
        print(out_str)


if __name__ == "__main__":
    main()
