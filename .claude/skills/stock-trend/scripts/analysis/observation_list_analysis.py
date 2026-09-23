"""Freeze six-dimension analysis for the user-maintained observation list.

The YAML list defines the universe and order.  This module does not select
recommendations or consume the candidate scanner's observation bucket.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from analysis import market_regime
from scans import stock_scanner
from core.resolve_code import resolve_suffix
from core.eastmoney_utils import EM_HEADERS, build_secid, rotate_push2_host
from fetchers import sector_data

SCHEMA = "yaml-observation-analysis/v1"
DIMENSIONS = (
    "momentum", "volume_price", "capital", "fundamental",
    "sector_strength", "wyckoff",
)
DEFAULT_YAML = market_regime.OBSERVATION_LIST_FILE
CACHE_DIR = Path(os.environ.get(
    "STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
ARTIFACT_DIR = CACHE_DIR / "observation_analyses"
NAME_CACHE_DIR = CACHE_DIR / "security_names"
SECTOR_SNAPSHOT_DIR = CACHE_DIR / "sector_stocks" / "history"
RANKING_CACHE = CACHE_DIR / "sector_rankings_cache.json"


def _display_name(value, code):
    """Return a real security name, never a code-shaped placeholder."""
    text = str(value or "").strip()
    return text if text and text != str(code) and not re.fullmatch(r"\d{6}", text) else ""


def _name_cache_path(data_date):
    return NAME_CACHE_DIR / f"{data_date}.json"


def _load_name_cache(data_date):
    """Load only the cache bound to this analysis date."""
    try:
        payload = json.loads(_name_cache_path(data_date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("schema") != "security-name-cache/v1" \
            or payload.get("data_date") != data_date:
        return {}
    entries = payload.get("items")
    return entries if isinstance(entries, dict) else {}


def _save_name_cache(data_date, entries):
    """Atomically persist display-only identity metadata."""
    path = _name_cache_path(data_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "security-name-cache/v1", "data_date": data_date,
        "generated_at": datetime.now().isoformat(), "items": entries,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fetch_eastmoney_name(code):
    """Resolve one A-share name without making it scoring evidence."""
    suffix = resolve_suffix(code)
    if not suffix:
        return None
    secid = build_secid(f"{code}{suffix}")
    if not secid:
        return None

    def _fetch(host):
        url = (f"https://{host}/api/qt/stock/get?secid={secid}"
               "&fields=f57,f58")
        request = urllib.request.Request(url, headers=EM_HEADERS)
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("empty_security_quote")
        returned_code = str(data.get("f57") or "").strip()
        name = _display_name(data.get("f58"), code)
        if returned_code and returned_code != code:
            raise RuntimeError("security_code_mismatch")
        if not name:
            raise RuntimeError("security_name_missing")
        return {"name": name, "provider": "eastmoney_stock_quote",
                "provider_host": host}

    try:
        result, _ = rotate_push2_host(_fetch, max_retries=2)
        return result
    except Exception:
        return None


def _fetch_eastmoney_profile(code):
    """Resolve same-source identity metadata needed for current-day mapping.

    ``f100`` is used only to discover an industry name.  The name is never
    accepted as membership evidence until the sector constituent endpoint
    verifies that the code is present in the matched same-day sector.
    """
    suffix = resolve_suffix(code)
    if not suffix:
        return None
    secid = build_secid(f"{code}{suffix}")
    if not secid:
        return None

    def _fetch(host):
        url = (f"https://{host}/api/qt/stock/get?secid={secid}"
               "&fields=f57,f58,f100")
        request = urllib.request.Request(url, headers=EM_HEADERS)
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("empty_security_profile")
        returned_code = str(data.get("f57") or "").strip()
        name = _display_name(data.get("f58"), code)
        industry = str(data.get("f100") or "").strip()
        if returned_code and returned_code != code:
            raise RuntimeError("security_code_mismatch")
        if not industry:
            raise RuntimeError("security_industry_missing")
        return {"name": name, "industry": industry,
                "provider": "eastmoney_stock_quote", "provider_host": host}

    try:
        result, _ = rotate_push2_host(_fetch, max_retries=2)
        return result
    except Exception:
        return None


def resolve_observation_name(code, data_date):
    """Return display-only identity metadata for a valid observation code.

    A quote name is intentionally marked ``identity_only``: it identifies the
    security for the report but never supplies historical market evidence or
    changes six-dimension eligibility.
    """
    cached = _load_name_cache(data_date).get(code)
    if isinstance(cached, dict) and _display_name(cached.get("name"), code):
        return {**cached, "name_quality": cached.get("name_quality", "identity_only"),
                "name_source": cached.get("name_source", "identity_cache")}
    fetched = _fetch_eastmoney_name(code)
    if not fetched:
        return {"name": "", "name_source": "unavailable", "name_quality": "unavailable",
                "name_data_date": "", "name_fetched_at": ""}
    result = {
        "name": fetched["name"], "name_source": fetched.get(
            "provider", "eastmoney_stock_quote"),
        "name_quality": "identity_only", "name_data_date": "",
        "name_fetched_at": datetime.now().isoformat(),
    }
    entries = _load_name_cache(data_date)
    entries[code] = result
    try:
        _save_name_cache(data_date, entries)
    except OSError:
        pass
    return result


def artifact_path_for(data_date, artifact_dir=None):
    """Constrain an artifact name to a verified ISO trading date."""
    try:
        verified = date.fromisoformat(str(data_date))
    except ValueError as exc:
        raise ValueError("invalid data_date") from exc
    if verified.isoformat() != data_date:
        raise ValueError("invalid data_date")
    return Path(artifact_dir or ARTIFACT_DIR) / f"{data_date}.json"


def _config_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def load_observation_config(yaml_path=None):
    """Use the daily-review YAML parser and add strict code diagnostics."""
    path = Path(yaml_path or DEFAULT_YAML)
    parsed = market_regime.load_observation_list(path)
    result = {
        **parsed, "path": str(path), "sha256": _config_digest(path),
        "items": [],
    }
    seen = set()
    for source in parsed.get("items", []):
        item = dict(source)
        code = str(item.get("code", ""))
        if not item.get("error"):
            if not re.fullmatch(r"\d{6}", code) or not resolve_suffix(code) \
                    or code.startswith(("5", "15")):
                item["error"] = "无效 A 股代码"
            elif code in seen:
                item["error"] = "重复代码"
        seen.add(code)
        result["items"].append(item)
    return result


def _read_exact_date_rankings(data_date):
    """A current ranking is never evidence for a different report date."""
    try:
        payload = json.loads(RANKING_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("data_date") != data_date:
        return {}
    rankings = payload.get("rankings") or {}
    meta = rankings.get("meta") or {}
    if meta.get("complete") is False or meta.get("provider") not in (
            None, "", "eastmoney"):
        return {}
    # The persisted ranking payload contains raw change/width/flow fields but
    # not the derived hot score used by stock_scanner.  Compute it against the
    # complete same-date ranking set so observation scores do not silently
    # fall back to 50.  Keep every provider sector code: rank_hot_sectors()
    # intentionally de-duplicates child boards for candidate selection, but a
    # watchlist lookup must not lose a valid industry merely because it is a
    # duplicate display row.
    raw_sectors = [dict(sector) for sector in rankings.get("sectors", [])
                   if isinstance(sector, dict) and sector.get("code")]
    for sector in raw_sectors:
        sector["absolute_hot_score"] = sector_data.compute_hot_score(sector)
    scores = [sector["absolute_hot_score"] for sector in raw_sectors]
    lo, hi = (min(scores), max(scores)) if scores else (0, 0)
    for sector in raw_sectors:
        sector["hot_score"] = round(
            (sector["absolute_hot_score"] - lo) / (hi - lo) * 100, 1
        ) if hi > lo else sector["absolute_hot_score"]
    return {
        sector.get("code"): sector
        for sector in raw_sectors
    }


def _expected_sector_size(ranking):
    """Return the provider's expected constituent count when available."""
    try:
        total = int(ranking.get("total_count") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > 0:
        return total
    try:
        return int(ranking.get("up_count") or 0) + int(
            ranking.get("down_count") or 0)
    except (TypeError, ValueError):
        return 0


def _sector_snapshot_complete(stocks, ranking):
    """Require a full same-day cohort before calculating relative strength."""
    expected = _expected_sector_size(ranking)
    return bool(stocks) and (not expected or len(stocks) >= expected)


def _historical_sector_hints(code, data_date):
    """Find prior same-source sector codes as lookup hints only.

    The returned codes are never used as current membership evidence; the
    current-day constituent endpoint must still contain ``code`` before a
    membership is accepted.
    """
    hints = []
    history_root = SECTOR_SNAPSHOT_DIR
    try:
        paths = sorted(history_root.glob("*/*.json"), reverse=True)
    except OSError:
        return hints
    for path in paths:
        if path.parent.name == data_date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("data_date") == data_date:
            continue
        if any(isinstance(stock, dict) and stock.get("code") == code
               for stock in payload.get("stocks", [])):
            hints.append(path.stem)
    return list(dict.fromkeys(hints))


def _same_day_sector_membership(code, data_date, rankings):
    """Fetch and verify one current-day industry membership, if possible.

    This path is deliberately disabled for historical replay.  It is also
    intentionally industry-only: concept membership has many-to-many and
    cross-source naming ambiguity that should remain unavailable rather than
    being guessed.
    """
    if data_date != datetime.now().strftime("%Y-%m-%d"):
        return None
    profile = _fetch_eastmoney_profile(code) or {}
    matches = [
        (sector_code, ranking)
        for sector_code, ranking in rankings.items()
        if ranking.get("type") == "industry"
        and profile.get("industry")
        and str(ranking.get("name") or "").strip() == profile.get("industry")
    ]
    # Some East Money quote hosts currently return only f57/f58 and omit the
    # industry field.  A prior same-source mapping may identify a sector code
    # to query, but the current full constituent response remains mandatory.
    if len(matches) != 1:
        hints = set(_historical_sector_hints(code, data_date))
        matches = [
            (sector_code, ranking)
            for sector_code, ranking in rankings.items()
            if sector_code in hints and ranking.get("type") == "industry"
        ]
    if len(matches) != 1:
        return None
    sector_code, ranking = matches[0]
    expected = max(1, _expected_sector_size(ranking))
    try:
        stocks = sector_data.get_sector_stocks(
            sector_code, top_n=expected, as_of_date=data_date)
    except Exception:
        return None
    if not _sector_snapshot_complete(stocks, ranking):
        return None
    target = next((stock for stock in stocks if stock.get("code") == code), None)
    if not target:
        return None
    return {
        "sector_code": sector_code,
        "ranking": ranking,
        "stocks": stocks,
        "target": target,
        "name": profile.get("name") or target.get("name") or code,
    }


def build_candidates(codes, data_date):
    """Build inputs from exact-date constituent snapshots, with audit errors.

    Existing snapshot rows supply stock metadata and membership provenance.
    A snapshot is accepted only when its embedded date equals ``data_date``.
    Sector rankings are likewise accepted only for that date.  This keeps a
    historical replay from borrowing today's industry ranking or membership.
    """
    wanted = set(codes)
    found = {code: [] for code in codes}
    snapshots = SECTOR_SNAPSHOT_DIR / data_date
    rankings = _read_exact_date_rankings(data_date)
    sector_stocks = {}

    def _add_sector_snapshot(sector_code, ranking, stocks, source):
        if not stocks:
            return
        sector_stocks[sector_code] = list(stocks)
        membership_quality = (
            "same_day_verified"
            if data_date == datetime.now().strftime("%Y-%m-%d")
            else "historical_verified"
        )
        for code in wanted:
            found[code] = [
                pair for pair in found.get(code, [])
                if pair[1].get("code") != sector_code
            ]
        for stock in stocks:
            if not isinstance(stock, dict) or stock.get("code") not in wanted:
                continue
            code = stock["code"]
            membership = stock_scanner.build_sector_membership(
                sector_code, ranking.get("name", sector_code),
                context={
                    **ranking,
                    "ranking_data_date": data_date if ranking else "",
                    "ranking_source": "eastmoney" if ranking else "",
                    "ranking_quality": "same_date" if ranking else "unknown",
                    "sector_actionable": False,
                },
                stock={
                    **stock, "membership_source": "historical_snapshot",
                    "membership_data_date": data_date,
                    "membership_quality": membership_quality,
                    "membership_provider": "eastmoney",
                    "membership_fetch_evidence": {
                        "status": "cache_valid", "source": str(source),
                        "data_date": data_date,
                    },
                },
            )
            found[code].append((stock, membership))

    for path in sorted(snapshots.glob("*.json")):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", path.stem):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("data_date") != data_date:
            continue
        _add_sector_snapshot(
            path.stem, rankings.get(path.stem, {}),
            payload.get("stocks", []), path,
        )

    # Existing candidate scans intentionally fetch a bounded Top-N.  Refresh
    # only the observed sectors whose cached cohort is incomplete, and only
    # for today's date.  Historical replay remains cache-only.
    sectors_to_refresh = set()
    for code in codes:
        for _, membership in found.get(code, []):
            sector_code = membership.get("code", "")
            ranking = rankings.get(sector_code, {})
            if sector_code and not _sector_snapshot_complete(
                    sector_stocks.get(sector_code, []), ranking):
                sectors_to_refresh.add(sector_code)
    for sector_code in sorted(sectors_to_refresh):
        ranking = rankings.get(sector_code, {})
        expected = max(1, _expected_sector_size(ranking))
        if data_date != datetime.now().strftime("%Y-%m-%d"):
            continue
        try:
            refreshed = sector_data.get_sector_stocks(
                sector_code, top_n=expected, as_of_date=data_date)
        except Exception:
            continue
        if _sector_snapshot_complete(refreshed, ranking):
            _add_sector_snapshot(
                sector_code, ranking, refreshed,
                SECTOR_SNAPSHOT_DIR / data_date / f"{sector_code}.json",
            )

    # A watchlist name may belong to an industry never selected by the
    # candidate scan.  Resolve and verify that one industry on the current
    # date, without attempting a broad all-sector crawl.
    for code in codes:
        if found.get(code):
            continue
        resolved = _same_day_sector_membership(code, data_date, rankings)
        if not resolved:
            continue
        _add_sector_snapshot(
            resolved["sector_code"], resolved["ranking"], resolved["stocks"],
            SECTOR_SNAPSHOT_DIR / data_date / f"{resolved['sector_code']}.json",
        )

    peer_cohorts = {
        sector_code: sorted(
            float(stock.get("change_pct"))
            for stock in stocks
            if isinstance(stock, dict)
            and isinstance(stock.get("change_pct"), (int, float))
        )
        for sector_code, stocks in sector_stocks.items()
        if _sector_snapshot_complete(stocks, rankings.get(sector_code, {}))
    }
    output = {}
    for code in codes:
        matches = found.get(code) or []
        if not matches:
            output[code] = {
                "candidate": {
                    "code": code, "ts_code": code + resolve_suffix(code),
                    "name": code, "sector_code": "", "sector_name": "",
                    "sector_memberships": [], "sector_actionable": False,
                },
                "metadata_source": "unavailable", "sector_status": "missing",
                "error": "缺少本依据日证券元数据/行业成分快照",
                "peer_cohorts": peer_cohorts,
            }
            continue
        stock, _ = matches[0]
        name = stock.get("name")
        memberships = [membership for _, membership in matches]
        primary = stock_scanner.select_primary_sector_membership(memberships)
        candidate = {
            **stock, "code": code, "ts_code": code + resolve_suffix(code),
            "name": name or code, "sector_memberships": memberships,
            "name_source": "sector_constituent_snapshot" if _display_name(name, code)
            else "unavailable",
            "name_quality": "same_date" if _display_name(name, code)
            else "unavailable",
            "name_data_date": data_date if _display_name(name, code) else "",
            "name_fetched_at": "",
            "sector_code": primary.get("code", ""),
            "sector_name": primary.get("name", ""),
            "sector_hot_score": primary.get("hot_score", 50),
            "sector_actionable": False,
        }
        primary_code = primary.get("code", "")
        primary_complete = _sector_snapshot_complete(
            sector_stocks.get(primary_code, []), rankings.get(primary_code, {}))
        ranking_same_day = any(
            m.get("ranking_data_date") == data_date for m in memberships)
        output[code] = {
            "candidate": candidate,
            "metadata_source": "sector_constituent_snapshot",
            "error": "证券名称缺失" if not name else "",
            "sector_status": "ready" if primary_complete and ranking_same_day
            else "peer_incomplete" if primary_code and ranking_same_day
            else "ranking_missing",
            "peer_cohorts": peer_cohorts,
        }
    return output


def _enrich_candidate_names(built, codes, data_date, resolver=None):
    """Fill missing display names without changing the analysis universe."""
    resolver = resolver or resolve_observation_name
    resolved_codes = set()
    for code in codes:
        if code in resolved_codes:
            continue
        resolved_codes.add(code)
        result = built.get(code)
        if not isinstance(result, dict):
            continue
        candidate = result.get("candidate")
        if not isinstance(candidate, dict):
            continue
        current = _display_name(candidate.get("name"), code)
        if current:
            candidate.setdefault("name_source", "sector_constituent_snapshot")
            candidate.setdefault("name_quality", "same_date")
            candidate.setdefault("name_data_date", data_date)
            candidate.setdefault("name_fetched_at", "")
            continue
        try:
            identity = resolver(code, data_date) or {}
        except Exception:
            identity = {}
        name = _display_name(identity.get("name"), code)
        candidate["name"] = name or code
        candidate["name_source"] = identity.get("name_source", "unavailable")
        candidate["name_quality"] = identity.get("name_quality", "unavailable")
        candidate["name_data_date"] = identity.get("name_data_date", "")
        candidate["name_fetched_at"] = identity.get("name_fetched_at", "")
        if name and result.get("error") == "证券名称缺失":
            result["error"] = ""
        result["name_source"] = candidate["name_source"]
        result["name_quality"] = candidate["name_quality"]
        result["name_data_date"] = candidate["name_data_date"]
        result["name_fetched_at"] = candidate["name_fetched_at"]


def _row(source, data_date, candidate_result=None, scored=None):
    """Keep an observation row even when its market analysis fails."""
    candidate_result = candidate_result or {}
    scored = scored if isinstance(scored, dict) else {}
    raw = scored.get("raw_dimensions") or {}
    dimensions = {key: raw.get(key) for key in DIMENSIONS}
    reasons = []
    if source.get("error"):
        reasons.append(source["error"])
    if candidate_result.get("error"):
        reasons.append(candidate_result["error"])
    if not scored and not reasons:
        reasons.append("六维分析未返回结果或 K 线不足")
    if candidate_result.get("sector_status") in ("ranking_missing", "missing"):
        if candidate_result.get("sector_status") == "ranking_missing":
            reasons.append("缺少本依据日行业排行证据")
        dimensions["sector_strength"] = None
    elif candidate_result.get("sector_status") == "peer_incomplete":
        reasons.append("sector_peer_coverage_incomplete")
        dimensions["sector_strength"] = None
    quality = scored.get("data_quality") or {}
    if quality and not quality.get("eligible", False):
        reasons.extend(str(reason) for reason in quality.get("reasons", []))
    if scored.get("wyckoff") and not scored["wyckoff"].get("signal", {}).get(
            "is_buy_signal", False):
        reasons.append("未确认维科夫买点")
    complete = all(dimensions[key] is not None for key in DIMENSIONS)
    raw_composite = scored.get("raw_composite_score") if complete else None
    adjusted = scored.get("quality_adjusted_score") if complete else None
    return {
        "code": source.get("code", ""), "date": source.get("date", ""),
        "entry_phase": source.get("entry_phase", ""),
        "data_date": data_date, "name": _display_name(
            scored.get("name"), source.get("code", "")) or _display_name(
                (candidate_result.get("candidate") or {}).get("name", ""),
                source.get("code", "")),
        "name_source": scored.get("name_source") or candidate_result.get(
            "name_source") or (candidate_result.get("candidate") or {}).get(
                "name_source", "unavailable"),
        "name_quality": scored.get("name_quality") or candidate_result.get(
            "name_quality") or (candidate_result.get("candidate") or {}).get(
                "name_quality", "unavailable"),
        "name_data_date": scored.get("name_data_date") or candidate_result.get(
            "name_data_date") or (candidate_result.get("candidate") or {}).get(
                "name_data_date", ""),
        "name_fetched_at": scored.get("name_fetched_at") or candidate_result.get(
            "name_fetched_at") or (candidate_result.get("candidate") or {}).get(
                "name_fetched_at", ""),
        "status": "ready" if complete and not reasons else "degraded",
        "reasons": list(dict.fromkeys(reasons)),
        "raw_dimensions": dimensions,
        "dimensions": dimensions,
        "raw_composite_score": raw_composite,
        "composite_score": raw_composite,
        "quality_adjusted_score": adjusted,
        "wyckoff": scored.get("wyckoff") or {},
        "data_quality": quality,
        "source_evidence": scored.get("source_evidence") or {},
        "sector_memberships": scored.get("sector_memberships") or
        (candidate_result.get("candidate") or {}).get("sector_memberships", []),
        "metadata_source": candidate_result.get("metadata_source", ""),
    }


def analyze_observation_list(data_date, yaml_path=None, artifact_path=None,
                             candidate_builder=None, analyzer=None,
                             capital_expected_date=None, name_resolver=None,
                             save=True):
    """Analyze YAML entries and optionally atomically freeze their artifact.

    ``candidate_builder(codes, data_date)`` returns a code-keyed mapping with
    ``candidate`` or ``error``. ``analyzer(candidates, **kwargs)`` has the
    ``stock_scanner.run_phase2`` contract; injections support deterministic
    offline tests.  Each valid candidate is scored separately, ensuring one
    failed provider cannot erase another YAML row.
    """
    artifact_path_for(data_date)  # Validate before network or filesystem work.
    config = load_observation_config(yaml_path)
    entries = config["items"]
    result = {
        "schema": SCHEMA, "status": "ready" if config["status"] == "ready"
        else "unavailable", "reason": config.get("reason", ""),
        "data_date": data_date, "generated_at": datetime.now().isoformat(),
        "config_path": config["path"], "config_sha256": config["sha256"],
        "items": [],
    }
    if config["status"] != "ready":
        return result
    valid_codes = [item["code"] for item in entries if not item.get("error")]
    try:
        built = (candidate_builder or build_candidates)(valid_codes, data_date)
        if not isinstance(built, dict):
            raise TypeError("candidate builder did not return a mapping")
    except Exception as exc:
        built = {code: {"error": f"候选输入构造失败: {type(exc).__name__}"}
                 for code in valid_codes}
    _enrich_candidate_names(built, valid_codes, data_date, resolver=name_resolver)
    analyzer = analyzer or stock_scanner.run_phase2
    for entry in entries:
        candidate_result = built.get(entry["code"], {}) if not entry.get("error") else {}
        candidate = candidate_result.get("candidate") if isinstance(
            candidate_result, dict) else None
        scored = None
        if candidate:
            try:
                rows = analyzer(
                    [candidate], enable_wyckoff=True,
                    require_wyckoff_gate=False,
                    as_of_date=data_date,
                    capital_expected_date=capital_expected_date or data_date,
                    top=1, min_candidates=1, disable_early_stop=True,
                    peer_cohorts=candidate_result.get("peer_cohorts"))
                scored = next((row for row in rows
                               if row.get("code") == entry["code"]), None)
            except Exception as exc:
                candidate_result = {
                    **candidate_result,
                    "error": f"六维分析失败: {type(exc).__name__}",
                }
        result["items"].append(_row(entry, data_date, candidate_result, scored))
    if any(item["status"] != "ready" for item in result["items"]):
        result["status"] = "degraded"
    if save:
        save_artifact(result, artifact_path)
    return result


def save_artifact(payload, artifact_path=None):
    """Atomically save one same-date YAML analysis."""
    if payload.get("schema") != SCHEMA:
        raise ValueError("invalid observation schema")
    path = Path(artifact_path) if artifact_path else artifact_path_for(
        payload.get("data_date"))
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def load_artifact(data_date, artifact_path=None, yaml_path=None):
    """Reject a stale artifact or an artifact for a changed YAML file."""
    path = Path(artifact_path) if artifact_path else artifact_path_for(data_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "本依据日 YAML 观察分析未生成", "items": []}
    config = load_observation_config(yaml_path)
    if payload.get("schema") != SCHEMA or payload.get("data_date") != data_date:
        return {"status": "unavailable", "reason": "观察分析 schema/依据日不匹配", "items": []}
    if not config["sha256"] or payload.get("config_sha256") != config["sha256"]:
        return {"status": "unavailable", "reason": "观察列表配置已变更", "items": []}
    if [item.get("code") for item in payload.get("items", [])] != [
            item.get("code") for item in config["items"]]:
        return {"status": "unavailable", "reason": "观察分析行与 YAML 不一致", "items": []}
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-date", required=True)
    parser.add_argument("--yaml", default=None)
    parser.add_argument("--artifact", default=None)
    parser.add_argument("--capital-expected-date", default=None)
    args = parser.parse_args(argv)
    result = analyze_observation_list(
        args.data_date, yaml_path=args.yaml, artifact_path=args.artifact,
        capital_expected_date=args.capital_expected_date)
    print(json.dumps({
        "status": result["status"], "data_date": result["data_date"],
        "artifact_path": str(args.artifact or artifact_path_for(args.data_date)),
        "item_count": len(result["items"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
