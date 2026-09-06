#!/usr/bin/env python3
"""AKShare 备选板块数据获取（当直连东方财富 API 不可用时）。

封装 AKShare 的同花顺/东方财富板块接口，输出格式与 sector_data.py 兼容。

目前可用数据源:
  - stock_board_industry_summary_ths()  — 同花顺行业实时排行 ✅
  - stock_board_concept_name_ths()      — 同花顺概念列表     ✅

Usage:
    from fetchers.sector_akshare import get_sector_rankings_akshare
    rankings = get_sector_rankings_akshare()
"""

import hashlib
import json
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

try:
    from core.source_health import classify_failure, live_attempt, source_result
except ImportError:  # pragma: no cover - direct standalone execution
    classify_failure = None
    live_attempt = None
    source_result = None


PROJECT_ROOT = Path(__file__).resolve().parents[5]
CACHE_DIR = Path(os.environ.get(
    "STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
THS_STOCKS_CACHE_MAX_AGE_HOURS = 24 * 30
_QUOTE_CACHE = None
_QUOTE_CACHE_LOCK = threading.Lock()


class SectorMembershipFetchError(RuntimeError):
    """AKShare constituent failure retaining provider evidence."""

    def __init__(self, message, provider_attempts=0, reason="unknown"):
        super().__init__(message)
        self.provider_attempts = provider_attempts
        self.reason = reason


def get_sector_rankings_akshare() -> Optional[dict]:
    """Get sector rankings via AKShare (同花顺 data), compatible format.

    Returns:
        Same format as sector_data.get_sector_rankings():
        {
            "meta": {"fetch_time": ..., "total_sectors": ...},
            "sectors": [
                {
                    "code": str,          # provider-scoped identity, not EM BK
                    "provider": "ths",    # upstream provider identity
                    "provider_code": str, # upstream ordinal/name code
                    "expand_symbol": str, # name accepted by constituent API
                    "name": str,          # 板块名称
                    "type": "industry",   # industry / concept
                    "change_pct": float,  # 涨跌幅%
                    "amount": float,      # 成交额
                    "up_count": int,      # 上涨家数
                    "down_count": int,    # 下跌家数
                    "total_count": int,   # 总计家数
                    "main_force_net": float,  # 主力净流入
                }
            ]
        }
        Returns None if AKShare unavailable or all APIs fail.
    """
    if not HAS_AKSHARE:
        return None

    sectors = []
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    sources = {}
    errors = []

    # ── 1. 同花顺行业板块实时排行 ──
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                change = _safe_float(row.get("涨跌幅"))
                amount = _safe_float(row.get("总成交额")) * 1e8  # 亿→元
                net = _safe_float(row.get("净流入")) * 1e8
                up = _safe_int(row.get("上涨家数"))
                down = _safe_int(row.get("下跌家数"))
                total = up + down

                name = str(row.get("板块", "") or "").strip()
                sector_id = f"ths:industry:{name}"
                sectors.append({
                    # ``序号`` is only a display ordinal.  Expose a stable
                    # provider-scoped identity as ``code`` so it cannot be
                    # mistaken for an EM ``BK`` code downstream.
                    "code": sector_id,
                    "provider": "ths",
                    "provider_code": str(row.get("序号", "")) or "",
                    "sector_id": sector_id,
                    "expand_symbol": name,
                    "expandable": bool(name),
                    "name": name,
                    "type": "industry",
                    "change_pct": change,
                    "amount": amount,
                    "up_count": up,
                    "down_count": down,
                    "total_count": total,
                    "main_force_net": net,
                })
        industry_count = sum(
            1 for sector in sectors if sector["type"] == "industry"
        )
        sources["industry"] = "ok" if industry_count >= 5 else (
            "sparse" if industry_count else "empty"
        )
    except Exception as e:
        sources["industry"] = "error"
        errors.append(f"industry: {e}")
        print(f"  [AKShare] Warning: 行业板块排行失败: {e}", file=sys.stderr)

    # ── 2. 同花顺概念列表（名称+代码，无实时行情） ──
    # 概念板块没有实时排行 API，但把名称列表加进去
    # 这样 hot_score 逻辑至少能识别这些概念存在
    try:
        df = ak.stock_board_concept_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", ""))
                code = str(row.get("code", ""))
                if name and code:
                    sector_id = f"ths:concept:{name}"
                    sectors.append({
                        "code": sector_id,
                        "provider": "ths",
                        "provider_code": code,
                        "sector_id": sector_id,
                        "expand_symbol": name,
                        "expandable": True,
                        "name": name,
                        "type": "concept",
                        "change_pct": 0,
                        "amount": 0,
                        "up_count": 0,
                        "down_count": 0,
                        "total_count": 0,
                        "main_force_net": 0,
                    })
        concept_count = sum(
            1 for sector in sectors if sector["type"] == "concept"
        )
        sources["concept"] = "ok" if concept_count >= 5 else (
            "sparse" if concept_count else "empty"
        )
    except Exception as e:
        sources["concept"] = "error"
        errors.append(f"concept: {e}")
        print(f"  [AKShare] Warning: 概念列表失败: {e}", file=sys.stderr)

    if not sectors:
        return None

    active = sum(
        1 for sector in sectors
        if sector.get("up_count", 0) or sector.get("down_count", 0)
    )
    complete = active >= 5 and all(
        sources.get(source) == "ok" for source in ("industry", "concept")
    )
    return {
        "meta": {
            "fetch_time": now,
            "total_sectors": len(sectors),
            "source": "akshare",
            "provider": "ths",
            "sources": sources,
            "errors": errors,
            "complete": complete,
        },
        "sectors": sectors,
    }


def get_sector_list_akshare() -> list[dict]:
    """Get sector list via AKShare, compatible with sector_data.get_sector_list().

    Returns:
        [{code, name, type}, ...]
    """
    sectors = []
    try:
        df = ak.stock_board_industry_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", ""))
                code = str(row.get("code", ""))
                sectors.append({
                    "code": f"ths:industry:{name}",
                    "provider": "ths",
                    "provider_code": code,
                    "sector_id": f"ths:industry:{name}",
                    "expand_symbol": name,
                    "expandable": bool(name),
                    "name": name,
                    "type": "industry",
                })
    except Exception:
        pass

    try:
        df = ak.stock_board_concept_name_ths()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", ""))
                code = str(row.get("code", ""))
                sectors.append({
                    "code": f"ths:concept:{name}",
                    "provider": "ths",
                    "provider_code": code,
                    "sector_id": f"ths:concept:{name}",
                    "expand_symbol": name,
                    "expandable": bool(name),
                    "name": name,
                    "type": "concept",
                })
    except Exception:
        pass

    return sectors


def _row_value(row: Any, *keys: str):
    """Read the first non-null DataFrame/record value among ``keys``."""
    for key in keys:
        try:
            value = row.get(key)
        except AttributeError:
            try:
                value = row[key]
            except (KeyError, TypeError, IndexError):
                value = None
        if value is None:
            continue
        try:
            if isinstance(value, float) and math.isnan(value):
                continue
        except TypeError:
            pass
        text = str(value).strip().lower()
        if text in {"", "nan", "none", "null", "-", "--"}:
            continue
        return value
    return None


def _number(value, default=None):
    """Parse numeric provider values, including 亿/万亿 suffixes."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else default
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "-", "--"}:
        return default
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier = 1e12
        text = text[:-2]
    elif text.endswith("亿"):
        multiplier = 1e8
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 1e4
        text = text[:-1]
    elif text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text) * multiplier
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normalise_stock_code(value) -> str:
    """Normalize provider codes without turning missing values into ``nan``."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def _normalise_constituent_row(row: Any) -> dict:
    """Normalize AKShare constituent output to the scanner's stock shape."""
    code = _normalise_stock_code(
        _row_value(row, "代码", "code", "证券代码", "symbol"))
    name = str(_row_value(row, "名称", "name", "证券简称") or "").strip()
    return {
        "code": code,
        "name": name,
        "change_pct": _number(_row_value(row, "涨跌幅", "change_pct")),
        "amount": _number(_row_value(row, "成交额", "amount")),
        "market_cap": _number(
            _row_value(row, "总市值", "总市值(元)", "market_cap")),
        "pe": _number(_row_value(
            row, "市盈率-动态", "市盈率", "pe", "pe_ttm")),
    }


def _ths_stock_cache_path(sector_name: str, sector_type: str) -> Path:
    """Return a provider/name-keyed cache path, isolated from EM BK caches."""
    identity = f"{sector_type}:{sector_name}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    return CACHE_DIR / "sector_stocks_ths" / f"{digest}.json"


def _tag_ths_stocks(stocks: list[dict], *, source: str, data_date: str,
                    cached_at: str = "", fallback_reason: str = "",
                    provider_attempts: int = 0,
                    membership_provider: str = "akshare",
                    mapping: str = "", provider_code: str = "",
                    membership_quality: str = "") -> list[dict]:
    age_hours = None
    tier = ""
    if cached_at:
        try:
            cached_dt = datetime.fromisoformat(str(cached_at))
            age_hours = round(max(
                0.0, (datetime.now() - cached_dt).total_seconds() / 3600), 1)
            tier = "same_day" if cached_dt.date() == datetime.now().date() \
                else ("recent" if age_hours <= 24 * 5 else "old")
        except (TypeError, ValueError):
            pass
    return [{
        **stock,
        "membership_source": source,
        "membership_data_date": data_date,
        "membership_quality": membership_quality or (
            "good" if source == "realtime" else "degraded"),
        "membership_cache_at": cached_at,
        "membership_cache_age_hours": age_hours,
        "membership_cache_tier": tier,
        "membership_fallback_reason": fallback_reason,
        "membership_provider_attempts": max(0, int(provider_attempts or 0)),
        "membership_provider": membership_provider,
        "membership_provider_code": provider_code,
        "membership_mapping": mapping,
    } for stock in stocks if stock.get("code")]


def _save_ths_stock_cache(sector_name: str, sector_type: str,
                          stocks: list[dict], data_date: str,
                          membership_provider="akshare", provider_code="",
                          mapping="", membership_quality="") -> None:
    path = _ths_stock_cache_path(sector_name, sector_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()
    payload = {
        "schema_version": 2,
        "cached_at": now,
        "data_date": data_date or now[:10],
        "provider": "akshare",
        "membership_provider": membership_provider,
        "membership_provider_code": provider_code or sector_name,
        "membership_mapping": mapping,
        "membership_quality": membership_quality,
        "sector_name": sector_name,
        "sector_type": sector_type,
        "stocks": stocks,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_ths_stock_cache(sector_name: str, sector_type: str,
                          top_n: int = 50) -> tuple[list[dict], dict]:
    path = _ths_stock_cache_path(sector_name, sector_type)
    if not path.exists():
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(payload["cached_at"])
        age = datetime.now() - cached_at
        if age.total_seconds() > THS_STOCKS_CACHE_MAX_AGE_HOURS * 3600:
            return [], {}
        if not payload.get("stocks") or payload.get("provider") != "akshare":
            return [], {}
        data_date = str(payload.get("data_date") or cached_at.date())
        if data_date > datetime.now().strftime("%Y-%m-%d"):
            return [], {}
        return payload["stocks"][:top_n], payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return [], {}


def _historical_em_sector_code(sector_name: str, sector_type: str) -> str:
    """Find a previously verified EM BK code by exact sector name."""
    del sector_type  # historical snapshots are already filtered by name
    for filename in ("candidate_sector_history.json",
                     "sector_snapshot_history.json"):
        path = CACHE_DIR / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        for row in walk(payload):
            if row.get("name") != sector_name:
                continue
            code = str(row.get("code", ""))
            if code.startswith("BK"):
                return code
    return ""


def _historical_em_cache(sector_name: str, sector_type: str, top_n: int,
                         fallback_reason: str, provider_attempts: int) -> list[dict]:
    """Use a verified historical BK cache only after the named live route fails."""
    code = _historical_em_sector_code(sector_name, sector_type)
    if not code:
        return []
    try:
        from fetchers.sector_data import get_sector_stocks_cached
        stocks = get_sector_stocks_cached(
            code, top_n=top_n, fallback_reason=fallback_reason)
    except Exception:
        return []
    for stock in stocks:
        stock["membership_provider"] = "eastmoney"
        stock["membership_provider_code"] = code
        stock["membership_mapping"] = "historical_name_to_em_cache"
    return stocks


def _evidence(*, attempted: bool, provider_attempts: int = 0,
              reason: str = "", cache_used: bool = False,
              stale: bool = False) -> dict:
    if live_attempt is not None:
        return live_attempt(
            attempted=attempted, provider_attempts=provider_attempts,
            reason=reason, cache_used=cache_used, stale=stale)
    return {
        "attempted": attempted, "provider_attempts": provider_attempts,
        "reason": reason, "cache_used": cache_used, "stale": stale,
    }


def get_em_sector_directory(sector_type: str, timeout: int = 15,
                            retries: int = 1, deadline: float | None = None):
    """Load one typed East Money directory for a scan and preserve evidence."""
    from fetchers.sector_data import _fetch_json, _check_result

    kind = "3" if sector_type == "concept" else "2"
    page = 1
    attempts = 0
    codes_by_name = {}
    while True:
        try:
            wrapped = _fetch_json(
                "https://push2.eastmoney.com/api/qt/clist/get?"
                f"fs=m:90+t:{kind}&fields=f12,f14&pn={page}&pz=100"
                "&po=0&np=1&fltt=2&fid=f12",
                timeout=timeout, retries=retries, deadline=deadline,
                with_evidence=True)
            attempts += wrapped["live_attempt"]["provider_attempts"]
            data = _check_result(wrapped["payload"])
        except Exception as exc:
            attempts += getattr(exc, "provider_attempts", 0)
            raise SectorMembershipFetchError(
                str(exc), attempts,
                getattr(exc, "reason", "") or classify_failure(exc)) from exc
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            code = str(row.get("f12", ""))
            name = str(row.get("f14", "")).strip()
            if name and code.startswith("BK") and code[2:].isdigit():
                codes_by_name.setdefault(name, []).append(code)
        if not rows or page * 100 >= int(data.get("total", len(rows))):
            break
        page += 1
    return {
        "provider": "eastmoney", "sector_type": sector_type,
        "codes_by_name": codes_by_name, "provider_attempts": attempts,
    }


def _fetch_named_em_stocks(sector_name, sector_type, top_n, timeout,
                           retries, deadline, em_directory=None):
    """Resolve names within their type using the bounded EM host rotation.

    AKShare's name-based membership API uses fixed 17/29.push2 hosts. Both
    can fail while the project's alternate hosts are healthy. Parse fields
    by key here, preserving market cap and avoiding positional schema drift.
    """
    from fetchers.sector_data import _fetch_json, _check_result

    attempts = 0

    def fetch(query):
        nonlocal attempts
        try:
            wrapped = _fetch_json(
                "https://push2.eastmoney.com/api/qt/clist/get?" + query,
                timeout=timeout, retries=retries, deadline=deadline,
                with_evidence=True)
            attempts += wrapped["live_attempt"]["provider_attempts"]
            return _check_result(wrapped["payload"])
        except Exception as exc:
            attempts += getattr(exc, "provider_attempts", 0)
            raise SectorMembershipFetchError(
                str(exc), attempts,
                getattr(exc, "reason", "") or classify_failure(exc)) from exc

    directory = em_directory if em_directory is not None \
        else get_em_sector_directory(
            sector_type, timeout=timeout, retries=retries, deadline=deadline)
    if directory.get("error_reason"):
        raise SectorMembershipFetchError(
            f"East Money {sector_type} directory unavailable: "
            f"{directory['error_reason']}", attempts,
            directory["error_reason"])
    matches = set(directory.get("codes_by_name", {}).get(sector_name, []))
    if len(matches) > 1:
        raise SectorMembershipFetchError(
            f"Ambiguous {sector_type} sector name: {sector_name}",
            attempts, "sector_mapping_ambiguous")
    if not matches:
        raise SectorMembershipFetchError(
            f"No exact East Money {sector_type} name: {sector_name}",
            attempts, "sector_mapping_missing")
    code = matches.pop()

    data = fetch(
        f"fs=b:{code}+f:!50&fields=f12,f14,f3,f6,f20,f9"
        f"&pn=1&pz={top_n}&po=1&np=1&fltt=2&fid=f3")
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    stocks = [_normalise_constituent_row({
        "代码": row.get("f12"), "名称": row.get("f14"),
        "涨跌幅": row.get("f3"), "成交额": row.get("f6"),
        "总市值": row.get("f20"), "市盈率-动态": row.get("f9"),
    }) for row in rows]
    return [stock for stock in stocks if stock["code"]], attempts, code


def get_sector_stocks_akshare(
        sector_name: str, sector_type: str = "industry", top_n: int = 50,
        timeout: int = 15, retries: int = 1, with_evidence: bool = False,
        deadline: float | None = None, as_of_date: str = "",
        cache_only: bool = False, fallback_reason: str = "",
        em_directory: dict | None = None) -> list[dict]:
    """Fetch constituents by provider/name, never by THS display ordinal.

    Retains the public adapter/cache contract, but uses the project's bounded
    EM node rotation instead of AKShare's fixed membership hosts. An ordinal
    such as ``"1"`` must never reach ``fs=b:...`` as if it were a BK code.
    """
    sector_name = str(sector_name or "").strip()
    sector_type = "concept" if sector_type == "concept" else "industry"
    failure_detail = ""

    def finish(stocks, attempt):
        if failure_detail:
            attempt["failure_detail"] = failure_detail
        payload = stocks[:top_n] if stocks else []
        return source_result(payload, attempt) if with_evidence else payload

    if cache_only:
        stocks, payload = _load_ths_stock_cache(
            sector_name, sector_type, top_n=top_n)
        if stocks:
            tagged = _tag_ths_stocks(
                stocks, source="cache",
                data_date=str(payload.get("data_date", "")),
                cached_at=str(payload.get("cached_at", "")),
                fallback_reason=fallback_reason or "cache_only",
                membership_provider=payload.get("membership_provider", "akshare"),
                provider_code=payload.get("membership_provider_code", sector_name),
                mapping="ths_name_cache")
            return finish(tagged, _evidence(
                attempted=False, cache_used=True, stale=True,
                reason=fallback_reason or "cache_only"))
        return finish([], _evidence(
            attempted=False, reason=fallback_reason or "cache_miss"))

    provider_attempts = 0
    failure_reason = ""
    provider_code = ""
    stocks = []
    if deadline is not None and deadline <= time.monotonic():
        failure_reason = "timeout"
    else:
        try:
            stocks, provider_attempts, provider_code = _fetch_named_em_stocks(
                sector_name, sector_type, top_n, timeout, retries, deadline,
                em_directory=em_directory)
            if not stocks:
                failure_reason = "empty"
        except Exception as exc:
            provider_attempts = getattr(exc, "provider_attempts", 0)
            failure_reason = getattr(exc, "reason", "") or classify_failure(exc)
            failure_detail = str(exc)
            print(f"  [EM] Warning: {sector_name}成分股失败: {exc}",
                  file=sys.stderr)

    if stocks:
        data_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
        try:
            _save_ths_stock_cache(
                sector_name, sector_type, stocks[:top_n], data_date,
                membership_provider="eastmoney", provider_code=provider_code,
                mapping="cross_source_exact_name_unverified",
                membership_quality="cross_source_unverified")
        except OSError as exc:
            print(f"  Warning: 同花顺板块缓存保存失败: {exc}", file=sys.stderr)
        tagged = _tag_ths_stocks(
            stocks[:top_n], source="realtime", data_date=data_date,
            provider_attempts=provider_attempts,
            mapping="cross_source_exact_name_unverified",
            membership_provider="eastmoney", provider_code=provider_code,
            membership_quality="cross_source_unverified")
        return finish(tagged, _evidence(
            attempted=True, provider_attempts=provider_attempts))

    cached, payload = _load_ths_stock_cache(
        sector_name, sector_type, top_n=top_n)
    if cached:
        tagged = _tag_ths_stocks(
            cached, source="cache",
            data_date=str(payload.get("data_date", "")),
            cached_at=str(payload.get("cached_at", "")),
            fallback_reason=failure_reason or "cache_fallback",
            provider_attempts=provider_attempts,
            membership_provider=payload.get("membership_provider", "akshare"),
            provider_code=payload.get("membership_provider_code", sector_name),
            mapping=payload.get("membership_mapping", "ths_name_cache"),
            membership_quality=payload.get(
                "membership_quality", "cross_source_unverified"))
        return finish(tagged, _evidence(
            attempted=provider_attempts > 0,
            provider_attempts=provider_attempts,
            reason=failure_reason or "cache_fallback",
            cache_used=True, stale=True))

    historical = _historical_em_cache(
        sector_name, sector_type, top_n,
        failure_reason or "name_mapped_cache", provider_attempts)
    if historical:
        return finish(historical, _evidence(
            attempted=provider_attempts > 0,
            provider_attempts=provider_attempts,
            reason=failure_reason or "name_mapped_cache",
            cache_used=True, stale=True))

    reason = failure_reason or "empty"
    raise SectorMembershipFetchError(
        f"获取板块{sector_name}成分股失败: {reason}; {failure_detail}; "
        "无有效成分股且无可用同名或历史快照",
        provider_attempts=provider_attempts, reason=reason)


def get_sector_stocks_akshare_cached(
        sector_name: str, sector_type: str = "industry", top_n: int = 50,
        fallback_reason: str = "cache_only") -> list[dict]:
    """Return only provider-isolated THS/name or verified historical caches."""
    stocks, payload = _load_ths_stock_cache(
        str(sector_name or "").strip(), sector_type, top_n=top_n)
    if stocks:
        return _tag_ths_stocks(
            stocks, source="cache",
            data_date=str(payload.get("data_date", "")),
            cached_at=str(payload.get("cached_at", "")),
            fallback_reason=fallback_reason,
            membership_provider=payload.get("membership_provider", "akshare"),
            provider_code=payload.get("membership_provider_code", str(sector_name or "")),
            mapping="ths_name_cache")
    return _historical_em_cache(
        str(sector_name or "").strip(), sector_type, top_n,
        fallback_reason, 0)


def enrich_stock_market_data(stocks: list[dict]) -> list[dict]:
    """Fill missing market cap from one bulk quote request when available."""
    if not stocks or not any(
            stock.get("market_cap") in (None, "") for stock in stocks):
        return stocks
    global _QUOTE_CACHE
    with _QUOTE_CACHE_LOCK:
        if _QUOTE_CACHE is None:
            _QUOTE_CACHE = {}
            if HAS_AKSHARE and getattr(ak, "stock_zh_a_spot_em", None):
                try:
                    frame = ak.stock_zh_a_spot_em()
                    if frame is not None and not frame.empty:
                        for _, row in frame.iterrows():
                            code = _normalise_stock_code(
                                _row_value(row, "代码", "code"))
                            if code:
                                _QUOTE_CACHE[code] = {
                                    "market_cap": _number(
                                        _row_value(row, "总市值", "market_cap")),
                                    "change_pct": _number(
                                        _row_value(row, "涨跌幅", "change_pct")),
                                    "amount": _number(
                                        _row_value(row, "成交额", "amount")),
                                    "pe": _number(_row_value(
                                        row, "市盈率-动态", "市盈率", "pe")),
                                }
                except Exception as exc:
                    print(
                        f"  [AKShare] Warning: A股报价市值补全失败: {exc}",
                        file=sys.stderr)
    enriched = []
    for stock in stocks:
        item = dict(stock)
        quote = _QUOTE_CACHE.get(_normalise_stock_code(item.get("code")), {})
        if item.get("market_cap") in (None, ""):
            item["market_cap"] = quote.get("market_cap")
        for field in ("change_pct", "amount", "pe"):
            if item.get(field) in (None, "") and quote.get(field) is not None:
                item[field] = quote[field]
        if item.get("market_cap") not in (None, ""):
            item["market_cap_source"] = item.get(
                "market_cap_source", "akshare_quote")
        else:
            item["market_cap_missing"] = True
        enriched.append(item)
    return enriched


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
