#!/usr/bin/env python3
"""今日复盘 + 市场环境评分 — 独立市场上下文整合.

聚合全市场数据(大盘指数/两市成交额/涨跌家数/涨停情绪/市场主力资金/板块排行),
输出市场环境评分(0-100) + 每日复盘报告,并持久化上下文供 /stock-trend 做大盘/板块对比.

数据源(全部复用现有 fetcher):
  - 指数K线:  kline_eastmoney (000001.SH上证 / 000300.SH沪深300 / 399001.SZ深成 / 399106.SZ深证综指)
  - 涨跌家数: sector_data 行业板块 up/down_count 加总(仅 industry,避免概念重复计数)
  - 板块排行: sector_data.get_sector_rankings (industry)
  - 涨停情绪: AKShare 涨停池 + 连板统计
  - 资金:     地域板块主力净流入加总

评分公式(对齐投资体系文档第一层):
  市场环境分 = 大盘趋势(25%) + 成交额(20%) + 赚钱效应(25%) + 涨停情绪(20%) + 资金(10%)
  ≥80 强势(可正常建仓) / 60-79 中性(轻仓观察) / <60 弱势(降仓/空仓)

Usage:
    python3 analysis/market_regime.py                     # 今日复盘
    python3 analysis/market_regime.py --json              # JSON 输出
    python3 analysis/market_regime.py --html              # 额外 HTML
    python3 analysis/market_regime.py --no-refresh        # 用今日缓存重出报告
"""

import argparse
import copy
import hashlib
import html as html_lib
import json
import os
import statistics
import sys
import time
import shutil
import tempfile
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = Path(os.environ.get("STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"
CONTEXT_FILE = CACHE_DIR / "market_regime.json"
HISTORY_FILE = CACHE_DIR / "market_regime_history.json"
MARKET_HISTORY_SCHEMA_VERSION = "market-regime-history/v2"
HISTORY_CONFLICT_DIR_NAME = "market_regime_history_conflicts"
OBSERVATION_LIST_FILE = Path(os.environ.get(
    "STOCK_TREND_OBSERVATION_LIST_FILE",
    str(PROJECT_ROOT / ".claude" / "skills" / "stock-trend" / "data" / "observation_list.yaml"),
))
OBSERVATION_BLOCK_START = "<!-- OBSERVATION_LIST:START -->"
OBSERVATION_BLOCK_END = "<!-- OBSERVATION_LIST:END -->"
OBSERVATION_ANALYSIS_DIR = CACHE_DIR / "observation_analyses"
HISTORY_MAX_DAYS = 30
MIN_AMOUNT_HISTORY_DAYS = 5
TOP_SECTOR_COUNT = 10
RETIRED_CONTEXT_KEYS = (
    "holdings", "portfolio_snapshot", "holdings_refreshed_at",
    "holdings_sync_note", "plan",
)

sys.path.insert(0, str(SCRIPT_DIR))

from analysis.wyckoff import format_minor_phase_text

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    yaml = None
    HAS_YAML = False


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _iso_date_from_value(value) -> str | None:
    """Normalize provider compact/ISO dates without inventing a date."""
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return _verified_history_date(text)


def _observed_at() -> str:
    """Local collection timestamp; it is not a provider event timestamp."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _amount_dates(records: list[dict]) -> list[str]:
    dates = {
        normalized for row in records or []
        if _safe_float(row.get("amount")) > 0
        for normalized in [_iso_date_from_value(row.get("trade_date"))]
        if normalized
    }
    return sorted(dates)


# ──────────────── 盘中会话时钟 ────────────────


def _session_elapsed_fraction(now=None) -> float:
    """A股当日已过交易时间占比(0-1)。

    240 交易分钟 = 9:30-11:30(120) + 13:00-15:00(120)。
    午休(11:30-13:00)按 0.5;盘前/收盘后/周末返回 0(走全天路径)。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 0.0
    t = now.hour * 60 + now.minute
    if 570 <= t < 690:          # 9:30-11:30
        return (t - 570) / 240.0
    if 690 <= t < 780:          # 11:30-13:00 午休
        return 0.5
    if 780 <= t <= 900:         # 13:00-15:00
        return 0.5 + (t - 780) / 240.0
    return 0.0                  # 盘前 / 收盘后


# 外推可信下限: 开盘 ~41 分钟前不外推(纯昨收),避免放大早盘噪音
FLOOR_FRACTION = 0.17


def _blend_weight(fraction: float, floor: float = FLOOR_FRACTION) -> float:
    """昨收→盘中 混合权重: 0=纯昨收, 1=纯盘中(0.75 已过时间后)."""
    if fraction <= floor:
        return 0.0
    return _clamp((fraction - floor) / (0.75 - floor), 0.0, 1.0)


# ──────────────── 数据收集 ────────────────

# 趋势指数: 上证 / 沪深300 / 深成
TREND_INDEX_CODES = ["000001.SH", "000300.SH", "399001.SZ"]
# 成交额: 上证(沪市) + 深证综指(全深市);深成只含成分股会低估深市
AMOUNT_INDEX_CODES = ["000001.SH", "399106.SZ"]
REGIME_COMPONENT_ORDER = [
    "index_trend", "volume", "breadth", "zt_emotion", "capital",
]
REGIME_WEIGHTS = {
    "index_trend": 0.25,
    "volume": 0.20,
    "breadth": 0.25,
    "zt_emotion": 0.20,
    "capital": 0.10,
}


def _sort_kline(records: list[dict]) -> list[dict]:
    records = [r for r in records if r.get("trade_date")]
    records.sort(key=lambda r: r["trade_date"])
    return records


def fetch_index_kline(code: str, lmt: int = 80, retries: int = 2,
                      diagnostics: dict | None = None) -> list[dict]:
    """Fetch index daily K-line, ascending by trade_date.

    降级链: 东财(push2his 节点轮换) → 腾讯 → BaoStock.
    """
    from fetchers.kline_eastmoney import (
        fetch_baostock,
        fetch_eastmoney,
        fetch_tencent_a_stock,
    )
    from core.eastmoney_utils import build_secid, rotate_em_host
    status = diagnostics if diagnostics is not None else {}
    errors = []
    fetched_at = _observed_at()
    secid = build_secid(code)

    def record_success(source: str, records: list[dict]) -> list[dict]:
        normalized_dates = [
            _iso_date_from_value(row.get("trade_date"))
            for row in records
        ]
        normalized_dates = [value for value in normalized_dates if value]
        status.update({
            "source": source,
            "provider": source,
            "record_count": len(records),
            "data_date": normalized_dates[-1] if normalized_dates else "",
            "data_date_raw": records[-1].get("trade_date") if records else "",
            "fetched_at": fetched_at,
            "source_timestamp": None,
            "amount_available_days": len(_amount_dates(records)),
            "amount_dates": _amount_dates(records),
            "errors": errors,
        })
        return records

    if secid:
        for _attempt in range(max(1, retries)):
            try:
                (records, _name), _used = rotate_em_host(
                    lambda h: fetch_eastmoney(secid, freq="D", lmt=lmt, host=h))
                records = _sort_kline(records)
                if records:
                    return record_success("eastmoney", records)
            except Exception as exc:
                errors.append(f"eastmoney: {exc}")
                continue
    try:
        records, _name = fetch_tencent_a_stock(code, "D")
        records = _sort_kline(records)[-lmt:]
        if records:
            return record_success("tencent", records)
    except Exception as exc:
        errors.append(f"tencent: {exc}")
    try:
        records, _name = fetch_baostock(code, "D")
        records = _sort_kline(records)[-lmt:]
        if records:
            return record_success("baostock", records)
    except Exception as exc:
        errors.append(f"baostock: {exc}")
    status.update({"source": "error", "provider": "error", "record_count": 0,
                   "data_date": "", "data_date_raw": "",
                   "fetched_at": fetched_at, "source_timestamp": None,
                   "amount_available_days": 0, "amount_dates": [],
                   "errors": errors})
    return []


def _ma(closes: list[float], period: int) -> list[float]:
    """SMA helper; first period-1 entries are None."""
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= period:
            out[i] = sum(closes[i + 1 - period:i + 1]) / period
    return out


def _index_metrics(records: list[dict]) -> dict:
    """Close / ma5 / ma20 / ma20_rising for a sorted index kline list."""
    if not records:
        return {"ok": False}
    closes = [_safe_float(r.get("close")) for r in records]
    closes = [c for c in closes if c > 0]
    if len(closes) < 20:
        return {"ok": False}
    ma20 = _ma(closes, 20)
    ma5 = _ma(closes, 5)
    close_now = closes[-1]
    ma20_now = ma20[-1] or close_now
    ma20_5ago = ma20[-6] if len(ma20) > 6 and ma20[-6] else ma20_now
    return {
        "ok": True,
        "close": close_now,
        "ma5": ma5[-1] if ma5[-1] else close_now,
        "ma20": ma20_now,
        "ma20_rising": ma20_now > ma20_5ago,
        "above_ma20": close_now > ma20_now,
        "pct_chg": _safe_float(records[-1].get("pct_chg")),
    }


def fetch_sector_rankings() -> list[dict]:
    """Industry sector rankings only (concept would double-count breadth)."""
    try:
        from fetchers.sector_data import get_sector_rankings
        result = get_sector_rankings()
        sectors = [s for s in result.get("sectors", []) if s.get("type") == "industry"]
        return sectors
    except Exception:
        return []


def fetch_zt_stats() -> dict:
    """涨停家数 / 连板家数 / 最高连板.

    直连 AKShare 涨停池，避免构建全市场板块映射。
    """
    data_date = datetime.now().date().isoformat()
    fetched_at = _observed_at()

    def result(count=0, streak_count=0, max_streak=0, *, completeness="missing", reasons=None):
        return {
            "count": count,
            "streak_count": streak_count,
            "max_streak": max_streak,
            "evidence": {
                "schema_version": "market-component-evidence/v1",
                "provider": "akshare",
                "data_date": data_date,
                "fetched_at": fetched_at,
                "source_timestamp": None,
                "completeness": completeness,
                "usage": "scorable" if completeness == "complete" else "unavailable",
                "reasons": list(reasons or []),
            },
        }

    if not HAS_AKSHARE:
        return result(reasons=["akshare_unavailable"])
    try:
        dt = data_date.replace("-", "")
        df = ak.stock_zt_pool_em(date=dt)
        if df is None or df.empty:
            return result(reasons=["provider_returned_empty"])
        streaks = []
        for v in df.get("连板数", []):
            try:
                streaks.append(int(v))
            except (TypeError, ValueError):
                streaks.append(1)
        streaks = [s if s >= 1 else 1 for s in streaks]
        return result(
            count=len(streaks),
            streak_count=sum(1 for s in streaks if s >= 2),
            max_streak=max(streaks) if streaks else 0,
            completeness="complete",
        )
    except Exception as exc:
        return result(reasons=[f"provider_error:{type(exc).__name__}"])


def fetch_market_activity() -> dict | None:
    """全市场涨跌家数 + 主力净流入: 东财地域板块(t:1)加总.

    地域板块每个股票恰属一个 → 加总精确,避免申万行业层级/概念板块重复计数.
    复用 sector_data._fetch_json(host轮换 + 无代理fallback).
    """
    try:
        from fetchers.sector_data import _fetch_json
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?pn=1&pz=60&po=0&np=1&fltt=2&fid=f3&fs=m:90+t:1"
               "&fields=f12,f14,f104,f105,f62")
        data = _fetch_json(url)
        items = (data.get("data") or {}).get("diff", [])
        if not items:
            return None
        up = sum(int(x.get("f104") or 0) for x in items)
        down = sum(int(x.get("f105") or 0) for x in items)
        main_force_yi = sum(float(x.get("f62") or 0) for x in items) / 1e8
        if up + down <= 0:
            return None
        return {
            "up": up,
            "down": down,
            "main_force_yi": round(main_force_yi, 1),
            "evidence": {
                "schema_version": "market-component-evidence/v1",
                "provider": "eastmoney",
                "data_date": datetime.now().date().isoformat(),
                "fetched_at": _observed_at(),
                "source_timestamp": None,
                "completeness": "complete",
                "usage": "scorable",
                "reasons": ["provider_event_timestamp_missing"],
            },
        }
    except Exception:
        return None


# ──────────────── 评分 ────────────────


def score_index_trend(index_metrics: dict[str, dict]) -> dict:
    """大盘趋势分: 上证/沪深300/深成 各自对 MA20 状态 → 平均."""
    scores = []
    detail = []
    for code, m in index_metrics.items():
        if not m.get("ok"):
            continue
        if m["above_ma20"] and m["ma20_rising"]:
            s = 100
        elif m["above_ma20"]:
            s = 60
        elif m["ma20_rising"]:
            s = 40
        else:
            s = 0
        scores.append(s)
        detail.append(f"{code} 收盘{'上' if m['above_ma20'] else '下'}MA20{'↑' if m['ma20_rising'] else '↓'}")
    if not scores:
        return {"score": 50.0, "detail": "指数数据不可用", "data_status": "missing"}
    return {"score": round(sum(scores) / len(scores), 1), "detail": "; ".join(detail),
            "data_status": "good"}


def score_volume(today_amount_yi: float | None, amount_history_yi: list[float],
                 amount_evidence: dict | None = None) -> dict:
    """成交额分: 两市成交额 vs 近20日均额."""
    if not today_amount_yi or today_amount_yi <= 0:
        return {"score": 50.0, "detail": "成交额不可用", "data_status": "missing"}
    hist = [a for a in amount_history_yi if a > 0][-20:]
    evidence_status = (amount_evidence or {}).get("baseline_status")
    if len(hist) < MIN_AMOUNT_HISTORY_DAYS or evidence_status not in (None, "good"):
        reason = (
            f"成交额来源对齐不足({(amount_evidence or {}).get('amount_available_days', len(hist))}天)"
            if evidence_status not in (None, "good") else
            f"成交额历史不足 {len(hist)}/{MIN_AMOUNT_HISTORY_DAYS}"
        )
        return {
            "score": 50.0,
            "detail": f"两市 {today_amount_yi:.0f}亿,{reason}",
            "data_status": "partial",
        }
    base = sum(hist) / len(hist)
    ratio = today_amount_yi / base
    score = _clamp(50 + (ratio - 1.0) * 150)
    pct = (ratio - 1.0) * 100
    return {
        "score": round(score, 1),
        "detail": f"两市 {today_amount_yi:.0f}亿,较20日均额 {pct:+.0f}%",
        "data_status": "good",
    }


def score_breadth(breadth: dict | None, industry_sectors: list[dict]) -> dict:
    """赚钱效应分: 全市场涨跌家数比(地域板块加总) + 行业板块上涨占比."""
    if not breadth or breadth.get("up", 0) + breadth.get("down", 0) <= 0:
        return {"score": 50.0, "detail": "涨跌家数不可用", "up": None, "down": None,
                "data_status": "missing"}
    up = breadth["up"]
    down = breadth["down"]
    up_ratio = up / (up + down)
    sector_up = sum(1 for s in industry_sectors if (_safe_float(s.get("change_pct")) or 0) > 0)
    sector_up_ratio = sector_up / max(len(industry_sectors), 1)
    score = _clamp(up_ratio * 70 + sector_up_ratio * 30)
    return {
        "score": round(score, 1),
        "detail": f"涨跌 {int(up)}/{int(down)},行业板块上涨占比 {sector_up_ratio * 100:.0f}%",
        "up": int(up),
        "down": int(down),
        "up_ratio": round(up_ratio, 3),
        "data_status": "good",
    }


def score_zt_emotion(zt: dict, history_counts: list[int],
                      zt_evidence: dict | None = None) -> dict:
    """涨停情绪分: 涨停家数 vs 近20日均值 + 连板高度."""
    count = zt.get("count", 0)
    max_streak = zt.get("max_streak", 0)
    streak_count = zt.get("streak_count", 0)
    hist = [c for c in history_counts if c > 0][-20:]
    if len(hist) >= 5:
        base = sum(hist) / len(hist)
        base_score = _clamp(50 + (count - base) * 1.5)
        vs = f"近20日均值 {base:.0f}家"
    else:
        base_score = _clamp(50 + (count - 50) * 0.3)
        vs = "历史不足,按绝对家数"
    bonus = 0
    if max_streak >= 5:
        bonus = 20
    elif max_streak >= 3:
        bonus = 10
    elif max_streak >= 2:
        bonus = 5
    score = _clamp(base_score + bonus)
    result = {
        "score": round(score, 1),
        "detail": f"涨停 {count}家(连板{streak_count},最高{max_streak}板;{vs})+连板加成{bonus}",
        "data_status": "good" if len(hist) >= 5 else "partial",
    }
    if zt_evidence and zt_evidence.get("completeness") != "complete":
        result["data_status"] = "missing" if count <= 0 else "partial"
    return result


def score_capital(market_activity: dict | None) -> dict:
    """资金分: 全市场主力净流入(地域板块加总,精确)."""
    main_force_yi = market_activity.get("main_force_yi") if market_activity else None
    if main_force_yi is not None:
        score = _clamp(50 + main_force_yi * 0.06)
        return {"score": round(score, 1),
                "detail": f"全市场主力净流入 {main_force_yi:+.1f}亿",
                "data_status": "good"}
    return {"score": 50.0, "detail": "资金数据不可用", "data_status": "missing"}


def _regime_gate(score: float) -> tuple[str, str]:
    """评分 → (label, advice)."""
    if score >= 80:
        return "强势", "可正常建仓/加仓,重点做强势板块龙头"
    if score >= 60:
        return "中性", "轻仓观察,只做高分标的,不追高"
    return "弱势", "降仓/空仓,不找牛股,等大盘站上MA20"


def compute_regime(components: dict) -> dict:
    """综合市场环境分(0-100) + gate 标签."""
    total = 0.0
    used_weight = 0.0
    missing = []
    partial = []
    for key in REGIME_COMPONENT_ORDER:
        w = REGIME_WEIGHTS[key]
        comp = components.get(key) or {}
        s = comp.get("score")
        if s is None:
            missing.append(key)
            continue
        status = comp.get("data_status", "good")
        if status == "missing":
            missing.append(key)
        elif status == "partial":
            partial.append(key)
        total += _safe_float(s) * w
        used_weight += w
    if used_weight <= 0:
        return {"score": 50.0, "label": "中性", "advice": "数据不可用"}
    score = round(_clamp(total / used_weight), 1)
    label, advice = _regime_gate(score)
    missing_weight = sum(REGIME_WEIGHTS.get(key, 0) for key in missing)
    score_lower = _clamp(total)
    score_upper = _clamp(total + missing_weight * 100)
    result = {"score": score, "label": label, "advice": advice,
              "data_quality": "missing" if missing else ("partial" if partial else "good"),
              "missing_components": missing, "partial_components": partial,
              "score_lower": round(score_lower, 1),
              "score_upper": round(score_upper, 1),
              "normalization_denominator": round(used_weight, 3),
              "raw_weighted_total": round(total, 3)}
    return result


# ──────────────── 持久化 ────────────────


def _verified_history_date(value) -> str | None:
    """Return a strict ISO weekday; never remap malformed/weekend keys."""
    if not isinstance(value, str):
        return None
    text = value
    if len(text) == 8 and text.isdigit():
        # Compact dates are accepted from provider rows, but not as persisted
        # history keys.  Callers can normalize them before this boundary.
        return None
    if len(text) != 10:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if parsed.isoformat() != text or parsed.weekday() >= 5:
        return None
    return text


def load_history(days: int = 30) -> dict:
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            today_key = date.today().isoformat()
            return {
                key: value for key, value in data.items()
                if _verified_history_date(key) and isinstance(value, dict)
                and key <= today_key
                and (
                    not value.get("date")
                    or value.get("date") == key
                )
            }
    except Exception:
        pass
    return {}


def _baseline_history(history: dict, data_date: str,
                      sanity_floor_ratio: float = 0.55) -> dict:
    """只留可做**全天**基线的历史条目。

    过滤规则(修复盘中 partial 污染):
      - date < data_date(排除今日 partial 条目)
      - 非 partial/intraday 标记(未来防护)
      - amount 低于 median*0.55 的异常低值剔除(修复已污染条目,如 08-18 半日额 9914)
        真实低量日(≥15000)不受影响
    """
    data_key = _verified_history_date(data_date)
    if data_key is None:
        return {}
    entries = {
        k: v for k, v in history.items()
        if _verified_history_date(k) and k < data_key
        and isinstance(v, dict)
        and (not v.get("date") or v.get("date") == k)
        and not (v.get("partial") or v.get("intraday"))
    }
    amounts = sorted(
        _safe_float(e.get("amount_yi")) for e in entries.values()
        if _safe_float(e.get("amount_yi")) > 0)
    if amounts:
        floor = statistics.median(amounts) * sanity_floor_ratio
        entries = {
            k: v for k, v in entries.items()
            if _safe_float(v.get("amount_yi")) <= 0
            or _safe_float(v.get("amount_yi")) >= floor
        }
    return entries


def _last_close_context(history: dict, data_date: str) -> dict | None:
    """上一完整交易日条目 {date, score, label, components};无则 None."""
    prior = _baseline_history(history, data_date)
    if not prior:
        return None
    key = max(prior)
    e = prior[key]
    return {
        "date": key,
        "score": _safe_float(e.get("regime_score")),
        "label": e.get("label", ""),
        "components": e.get("components"),
    }


def previous_amounts(history: dict, before_date: str,
                     fetched: dict | None = None, *,
                     require_evidence: bool = False,
                     fetched_dates: set[str] | None = None) -> list[float]:
    """Return prior turnover values, preferring freshly fetched dates."""
    before_key = _verified_history_date(before_date)
    if before_key is None:
        return []
    by_date = {}
    for history_date, entry in sorted(_baseline_history(history, before_key).items()):
        if require_evidence and not _history_amount_eligible(entry):
            continue
        amount = _safe_float(entry.get("amount_yi"))
        if amount > 0:
            by_date[history_date] = amount
    for raw_date, raw_amount in (fetched or {}).items():
        text = str(raw_date)
        compact = (f"{text[:4]}-{text[4:6]}-{text[6:8]}"
                   if len(text) == 8 and text.isdigit() else text)
        iso_date = _verified_history_date(compact)
        amount = _safe_float(raw_amount)
        if (iso_date and iso_date < before_key and amount > 0
                and (not require_evidence
                     or iso_date in (fetched_dates or set()))):
            by_date[iso_date] = amount
    return [by_date[key] for key in sorted(by_date)]


def complete_market_amounts(index_rows: dict) -> dict[str, float]:
    """Sum turnover only for dates reported by both Shanghai and Shenzhen."""
    by_market = {}
    for code in AMOUNT_INDEX_CODES:
        values = {}
        for row in index_rows.get(code, []):
            trade_date = row.get("trade_date")
            amount = _safe_float(row.get("amount"))
            if trade_date and amount > 0:
                values[trade_date] = amount / 1e8
        by_market[code] = values
    complete_dates = set.intersection(*(
        set(by_market[code]) for code in AMOUNT_INDEX_CODES
    ))
    return {
        trade_date: sum(by_market[code][trade_date]
                        for code in AMOUNT_INDEX_CODES)
        for trade_date in sorted(complete_dates)
    }


def build_amount_evidence(index_rows: dict, index_diagnostics: dict | None = None,
                          data_date: str = "") -> dict:
    """Build auditable two-market turnover provenance.

    A date is baseline-eligible only when both the Shanghai and Shenzhen
    indices expose a positive amount for that exact trade date.  Provider
    fallback records are retained for diagnosis, but never promoted to a
    complete history merely because one side has a value.
    """
    diagnostics = index_diagnostics or {}
    per_index = {}
    date_sets = []
    provider_names = []
    fetched_values = []
    reasons = []
    for code in AMOUNT_INDEX_CODES:
        records = list(index_rows.get(code, []) or [])
        diag = diagnostics.get(code) if isinstance(diagnostics, dict) else {}
        diag = diag if isinstance(diag, dict) else {}
        dates = _amount_dates(records)
        date_sets.append(set(dates))
        provider = str(diag.get("provider") or diag.get("source") or "unknown")
        provider_names.append(provider)
        if diag.get("fetched_at"):
            fetched_values.append(str(diag["fetched_at"]))
        item_reasons = []
        if not records:
            item_reasons.append("index_history_missing")
        if not dates:
            item_reasons.append("amount_missing")
        if provider in {"tencent", "tencent_a"} and len(dates) <= 1:
            item_reasons.append("provider_no_historical_amount")
        if diag.get("errors"):
            item_reasons.append("fallback_or_provider_errors")
        per_index[code] = {
            "provider": provider,
            "data_date": _iso_date_from_value(diag.get("data_date")),
            "fetched_at": diag.get("fetched_at"),
            "source_timestamp": diag.get("source_timestamp"),
            "record_count": int(diag.get("record_count") or len(records)),
            "amount_available_days": len(dates),
            "amount_dates": dates,
            "completeness": "complete" if dates and not item_reasons else (
                "partial" if dates else "missing"),
            "usage": "reference_only" if item_reasons else "scorable",
            "reasons": sorted(set(item_reasons)),
            "errors": list(diag.get("errors") or []),
        }

    common_dates = set.intersection(*date_sets) if date_sets else set()
    union_dates = set.union(*date_sets) if date_sets else set()
    if not common_dates:
        if union_dates:
            reasons.append("single_market_amount_missing_or_date_mismatch")
        else:
            reasons.append("amount_history_missing")
    elif len(common_dates) < MIN_AMOUNT_HISTORY_DAYS:
        reasons.append("amount_history_insufficient")
    if union_dates and common_dates != union_dates:
        reasons.append("amount_dates_not_aligned")
    if any(reason == "provider_no_historical_amount"
           for item in per_index.values() for reason in item["reasons"]):
        reasons.append("fallback_provider_lacks_historical_amount")
    if any(reason == "fallback_or_provider_errors"
           for item in per_index.values() for reason in item["reasons"]):
        reasons.append("provider_fallback_used")

    source_kind = "primary"
    if any(provider in {"tencent", "baostock", "tencent_a"}
           for provider in provider_names):
        source_kind = "alternative"
    if any(provider == "unknown" for provider in provider_names):
        source_kind = "unknown"
    baseline_status = "good" if len(common_dates) >= MIN_AMOUNT_HISTORY_DAYS else "partial"
    completeness = "complete" if baseline_status == "good" else (
        "partial" if common_dates or union_dates else "missing")
    return {
        "schema_version": "market-amount-evidence/v1",
        "provider": "+".join(dict.fromkeys(provider_names)) or "unknown",
        "source_kind": source_kind,
        "data_date": _iso_date_from_value(data_date),
        "fetched_at": max(fetched_values) if fetched_values else None,
        "source_timestamp": None,
        "per_index": per_index,
        "complete_dates": sorted(common_dates),
        "union_dates": sorted(union_dates),
        "amount_available_days": len(common_dates),
        "completeness": completeness,
        "baseline_status": baseline_status,
        "usage": "scorable" if baseline_status == "good" else "reference_only",
        "reasons": sorted(set(reasons)),
    }


def _history_amount_eligible(entry: dict) -> bool:
    evidence = entry.get("amount_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = (entry.get("component_evidence") or {}).get("volume") or {}
    return (
        isinstance(evidence, dict)
        and evidence.get("completeness") == "complete"
        and evidence.get("usage") == "scorable"
        and _safe_float(entry.get("amount_yi")) > 0
    )


def _history_zt_eligible(entry: dict) -> bool:
    evidence = entry.get("zt_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = (entry.get("component_evidence") or {}).get("zt_emotion") or {}
    return (
        isinstance(evidence, dict)
        and evidence.get("completeness") == "complete"
        and evidence.get("usage") == "scorable"
        and isinstance(entry.get("zt"), dict)
        and _safe_float((entry.get("zt") or {}).get("count")) >= 0
    )


def should_save_history(ctx: dict) -> bool:
    """盘中快照不写历史基线(避免 partial 数据污染);全天/收盘后条目才写."""
    return not bool(ctx.get("intraday", False))


_HISTORY_RUNTIME_FIELDS = {
    "content_sha256", "schema_version", "fetched_at", "generated_at", "recorded_at", "saved_at",
}
LAST_HISTORY_WRITE_RESULT = {"status": "not_attempted"}


def _history_decision_projection(value):
    if isinstance(value, dict):
        return {
            key: _history_decision_projection(child)
            for key, child in value.items()
            if key not in _HISTORY_RUNTIME_FIELDS
        }
    if isinstance(value, list):
        return [_history_decision_projection(child) for child in value]
    return value


def _history_digest(value: dict) -> str:
    projection = _history_decision_projection(value)
    payload = json.dumps(
        projection, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_json_write(path: Path, value: dict | list) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(temporary_name)
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.write(b"\n")
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
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_history_conflict(history_path: Path, entry_date: str,
                             existing: dict, incoming: dict,
                             existing_digest: str, incoming_digest: str) -> Path:
    conflict_dir = history_path.parent / HISTORY_CONFLICT_DIR_NAME
    conflict_dir.mkdir(parents=True, exist_ok=True)
    target = conflict_dir / f"{entry_date}-{incoming_digest[:16]}.json"
    payload = {
        "schema_version": "market-regime-history-conflict/v1",
        "recorded_at": _observed_at(),
        "history_path": str(history_path),
        "date": entry_date,
        "existing_content_sha256": existing_digest,
        "incoming_content_sha256": incoming_digest,
        "existing": existing,
        "incoming": incoming,
    }
    if target.exists():
        return target
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(conflict_dir), prefix=f".{target.name}.", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def get_last_history_write_result() -> dict:
    return copy.deepcopy(LAST_HISTORY_WRITE_RESULT)


def save_history(entry: dict) -> bool:
    global LAST_HISTORY_WRITE_RESULT
    if not isinstance(entry, dict):
        LAST_HISTORY_WRITE_RESULT = {"status": "invalid"}
        return False
    entry_date = _verified_history_date(entry.get("date"))
    if entry_date is None:
        LAST_HISTORY_WRITE_RESULT = {"status": "invalid", "reason": "invalid_date"}
        return False
    history = load_history()
    stored = copy.deepcopy(entry)
    stored["date"] = entry_date
    stored.setdefault("intraday", False)
    stored.setdefault("schema_version", MARKET_HISTORY_SCHEMA_VERSION)
    incoming_digest = _history_digest(stored)
    stored["content_sha256"] = incoming_digest
    previous = history.get(entry_date)
    if isinstance(previous, dict):
        existing_digest = _history_digest(previous)
        if existing_digest == incoming_digest:
            # Upgrade legacy entries with the auditable envelope without
            # treating a rerun with a different collection timestamp as a
            # conflict.
            if previous.get("content_sha256") != incoming_digest or not previous.get("schema_version"):
                history[entry_date] = stored
                items = sorted(history.items(), key=lambda kv: kv[0])[-HISTORY_MAX_DAYS:]
                try:
                    _atomic_json_write(HISTORY_FILE, dict(items))
                except Exception as exc:
                    LAST_HISTORY_WRITE_RESULT = {"status": "error", "reason": type(exc).__name__}
                    return False
            LAST_HISTORY_WRITE_RESULT = {
                "status": "unchanged", "date": entry_date,
                "content_sha256": incoming_digest,
            }
            return True
        try:
            conflict = _record_history_conflict(
                Path(HISTORY_FILE), entry_date, previous, stored,
                existing_digest, incoming_digest)
            LAST_HISTORY_WRITE_RESULT = {
                "status": "conflict", "date": entry_date,
                "content_sha256": incoming_digest, "path": str(conflict),
            }
        except Exception as exc:
            LAST_HISTORY_WRITE_RESULT = {
                "status": "conflict_error", "date": entry_date,
                "content_sha256": incoming_digest, "reason": type(exc).__name__,
            }
        return False
    history[entry_date] = stored
    # prune to newest N
    items = sorted(history.items(), key=lambda kv: kv[0])[-HISTORY_MAX_DAYS:]
    try:
        _atomic_json_write(HISTORY_FILE, dict(items))
        LAST_HISTORY_WRITE_RESULT = {
            "status": "created", "date": entry_date,
            "content_sha256": incoming_digest,
        }
        return True
    except Exception as exc:
        LAST_HISTORY_WRITE_RESULT = {
            "status": "error", "date": entry_date,
            "content_sha256": incoming_digest, "reason": type(exc).__name__,
        }
        return False


def quarantine_invalid_history_dates(path=None) -> dict:
    """Back up and quarantine malformed/weekend market history entries."""
    history_path = Path(path or HISTORY_FILE)
    if not history_path.exists():
        return {"status": "missing", "moved": 0}
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "invalid_file", "moved": 0}
    if not isinstance(data, dict):
        return {"status": "invalid_file", "moved": 0}
    valid, invalid = {}, {}
    today_key = date.today().isoformat()
    for key, value in data.items():
        if (_verified_history_date(key) and key <= today_key
                and isinstance(value, dict)
                and (not value.get("date") or value.get("date") == key)):
            valid[key] = value
        else:
            invalid[key] = value
    if not invalid:
        return {"status": "clean", "moved": 0}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    backup = history_path.with_name(history_path.name + f".{stamp}.bak")
    quarantine = history_path.with_name(
        history_path.name + f".quarantine-{stamp}.json")
    shutil.copy2(history_path, backup)
    quarantine.write_text(json.dumps(invalid, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    tmp = history_path.with_name(history_path.name + f".tmp-{stamp}")
    try:
        tmp.write_text(json.dumps(valid, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, history_path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return {
        "status": "migrated", "moved": len(invalid),
        "backup": str(backup), "quarantine": str(quarantine),
    }


def save_context(ctx: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CONTEXT_FILE.write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_context() -> dict | None:
    try:
        if CONTEXT_FILE.exists():
            ctx = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
            if isinstance(ctx, dict):
                for key in RETIRED_CONTEXT_KEYS:
                    ctx.pop(key, None)
            return ctx
    except Exception:
        pass
    return None


# ──────────────── 报告 ────────────────


def _observation_text(value, fallback="未记录") -> str:
    """Convert YAML scalar values to stable display text."""
    if value is None or value == "":
        return fallback
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def load_observation_list(path=None) -> dict:
    """Read the user-maintained observation list without changing it.

    The report must remain renderable when this optional input is missing or
    malformed, so errors are returned as data for the HTML block to display.
    """
    observation_path = Path(path or OBSERVATION_LIST_FILE)
    if not HAS_YAML:
        return {"status": "unavailable", "reason": "YAML 依赖不可用", "items": []}
    try:
        raw = yaml.safe_load(observation_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "观察列表文件不存在", "items": []}
    except OSError as exc:
        return {"status": "unavailable", "reason": f"读取失败: {type(exc).__name__}", "items": []}
    except Exception as exc:
        return {"status": "unavailable", "reason": f"YAML 格式错误: {type(exc).__name__}", "items": []}

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "顶层结构不是对象", "items": []}
    entries = raw.get("observation_list", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return {"status": "unavailable", "reason": "observation_list 不是列表", "items": []}

    items = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            items.append({
                "code": _observation_text(entry),
                "date": "未记录",
                "entry_phase": "未记录",
                "error": f"第 {index} 项不是对象",
            })
            continue
        item = {
            "code": _observation_text(entry.get("code")),
            "date": _observation_text(entry.get("date")),
            "entry_phase": _observation_text(entry.get("entry_phase")),
        }
        missing = [key for key in ("code", "date", "entry_phase")
                   if entry.get(key) in (None, "")]
        if missing:
            item["error"] = "缺少字段: " + ", ".join(missing)
        items.append(item)
    return {"status": "ready", "items": items, "path": str(observation_path)}


def load_observation_analysis(data_date, artifact_path=None, yaml_path=None) -> dict:
    """Load a same-date analysis of the current hand-maintained YAML list."""
    from analysis.observation_list_analysis import load_artifact
    path = artifact_path or OBSERVATION_ANALYSIS_DIR / f"{data_date}.json"
    try:
        state = load_artifact(data_date, artifact_path=path,
                              yaml_path=yaml_path or OBSERVATION_LIST_FILE)
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "YAML 观察分析文件损坏或不可读", "items": []}
    if not isinstance(state.get("items"), list):
        return {"status": "unavailable", "reason": "YAML 观察分析条目无效", "items": []}
    return state


def _observation_score(value) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _observation_cell(value) -> str:
    return html_lib.escape(_observation_text(value), quote=True)


def _observation_identity(item: dict) -> tuple[str, str]:
    """Return a non-duplicated display name and the raw six-digit code."""
    code = _observation_text(item.get("code"), "未记录")
    name = str(item.get("name") or "").strip()
    reasons = item.get("reasons") or item.get("reason") or item.get("error") or []
    if isinstance(reasons, list):
        reasons = "；".join(str(reason) for reason in reasons)
    if not name or name == code or (name.isdigit() and len(name) == 6):
        name = "名称未提供" if "无效 A 股代码" in str(reasons) else "名称未获取"
    return name, code


def render_observation_list_html(state: dict | None = None, *, pending: bool = False) -> str:
    """Render the replaceable observation-list section for a daily-review HTML."""
    state = state or {"status": "ready", "items": []}
    if pending:
        body = '<p class="dt" data-observation-status="pending">YAML 观察列表六维分析进行中，完成后将更新此区块。</p>'
    elif state.get("status") not in {"ready", "degraded"}:
        reason = html_lib.escape(str(state.get("reason") or "未知原因"), quote=True)
        body = f'<p class="dt" data-observation-status="unavailable">观察列表不可用：{reason}</p>'
    else:
        rows = []
        for item in state.get("items", []):
            dimensions = item.get("raw_dimensions") or item.get("dimensions") or {}
            quality = item.get("data_quality") or {}
            wyckoff = item.get("wyckoff") or {}
            reasons = item.get("reasons") or item.get("reason") or item.get("error") or []
            if isinstance(reasons, list):
                reasons = "；".join(str(reason) for reason in reasons)
            scores = "".join(f"<td>{_observation_score(dimensions.get(key))}</td>" for key in
                             ("momentum", "volume_price", "capital", "fundamental", "sector_strength", "wyckoff"))
            structure = (
                format_minor_phase_text(wyckoff)
                if wyckoff.get("minor_phase")
                else wyckoff.get("sub_phase") or wyckoff.get("phase") or "未提供"
            )
            quality_text = quality.get("status") or quality.get("quality") or item.get("status") or "未提供"
            display_name, display_code = _observation_identity(item)
            rows.append("<tr>" +
                        f"<td>{_observation_cell(display_name)}<br><small>{_observation_cell(display_code)}</small></td>" +
                        f"<td>{_observation_cell(item.get('date'))}</td>" +
                        f"<td>{_observation_cell(item.get('entry_phase'))}</td>" + scores +
                        f"<td>{_observation_score(item.get('composite_score'))} / {_observation_score(item.get('quality_adjusted_score'))}</td>" +
                        f"<td>{_observation_cell(structure)}</td><td>{_observation_cell(quality_text)}</td>" +
                        f"<td>{_observation_cell(reasons)}</td></tr>")
        headers = ("股票名称 / 代码", "加入日期", "加入时阶段", "动量", "量价", "资金", "基本面", "板块强度",
                   "维科夫", "综合 / 质量调整", "维科夫结构", "数据质量", "观察原因")
        status_note = ('<p class="dt" data-observation-status="degraded">部分观察标的数据或资格证据不足，详见各行原因。</p>'
                       if state.get("status") == "degraded" else "")
        body = (status_note + f'<p class="dt">分析依据日：{_observation_cell(state.get("data_date"))}</p>' +
                '<div class="observation-table-wrap"><table><thead><tr>' +
                "".join(f"<th>{head}</th>" for head in headers) + "</tr></thead><tbody>" +
                ("".join(rows) or '<tr><td colspan="13">YAML 观察列表为空</td></tr>') +
                "</tbody></table></div>")
    return (
        f"{OBSERVATION_BLOCK_START}\n"
        '<section id="observation-list">\n'
        "<h2>观察列表</h2>\n"
        '<p class="dt">手工观察对象的六维分析，仅供学习参考，非正式推荐。</p>\n'
        f"{body}\n"
        "</section>\n"
        f"{OBSERVATION_BLOCK_END}"
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a generated report atomically so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def update_observation_list_html(html_path, *, pending: bool = False, data_date=None,
                                 artifact_path=None, yaml_path=None) -> dict:
    """Update only the marked observation block in an existing daily-review HTML."""
    path = Path(html_path)
    original = path.read_text(encoding="utf-8")
    start = original.find(OBSERVATION_BLOCK_START)
    end_marker = OBSERVATION_BLOCK_END
    end = original.find(end_marker, start + len(OBSERVATION_BLOCK_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ValueError("observation_block_missing")
    end += len(end_marker)
    state = (load_observation_analysis(data_date, artifact_path, yaml_path) if not pending else None)
    replacement = render_observation_list_html(state, pending=pending)
    updated = original[:start] + replacement + original[end:]
    _atomic_write_text(path, updated)
    return {
        "status": "pending" if pending else "completed",
        "html_path": str(path.resolve()),
        "observation_status": state.get("status") if state else "pending",
        "count": len(state.get("items", [])) if state else 0,
    }


DISCLAIMER = "本报告仅供学习参考,不构成任何投资建议。股市有风险,投资需谨慎。"


def _market_explanation_for_report(ctx: dict) -> dict | None:
    explanation = ctx.get("market_explanation")
    if isinstance(explanation, dict):
        return explanation
    try:
        from analysis.market_explanation import build_market_explanation
        return build_market_explanation(
            ctx, ctx.get("data_date") or date.today().isoformat())
    except (TypeError, ValueError):
        return None


def generate_report(ctx: dict) -> str:
    lines = []
    lines.append(f"## 📅 今日复盘 ({ctx.get('data_date', '')})")
    lines.append("")
    lines.append(f"▸ 生成时间: {ctx.get('generated_at', '')}")
    regime = ctx.get("regime", {})
    label_icon = {"强势": "🟢", "中性": "🟡", "弱势": "🔴"}.get(regime.get("label", ""), "⚪")
    lines.append(f"▸ 市场环境评分: **{regime.get('score', 0)} / 100** {label_icon} {regime.get('label', '')}")
    lines.append(f"▸ 操作建议: {regime.get('advice', '')}")
    if ctx.get("stale_note"):
        lines.append(f"▸ ⚠️ {ctx['stale_note']}")
    if ctx.get("intraday_note"):
        lines.append(f"▸ ⚠️ {ctx['intraday_note']}")
    lines.append("")

    # ① 市场环境
    lines.append("### ① 市场环境")
    lines.append("")
    lines.append("| 组件 | 得分 | 说明 |")
    lines.append("|------|------|------|")
    for key, name in [("index_trend", "大盘趋势"), ("volume", "成交额"),
                      ("breadth", "赚钱效应"), ("zt_emotion", "涨停情绪"),
                      ("capital", "资金")]:
        comp = (ctx.get("components") or {}).get(key) or {}
        lines.append(f"| {name} | **{comp.get('score', '—')}** | {comp.get('detail', '—')} |")
    lines.append("")
    breadth = (ctx.get("components") or {}).get("breadth") or {}
    lines.append(f"▸ 涨跌家数: 涨 {breadth.get('up') if breadth.get('up') is not None else '—'} / 跌 {breadth.get('down') if breadth.get('down') is not None else '—'} | "
                 f"两市成交 {ctx.get('amount_yi', 0):.0f}亿 | "
                 f"涨停 {ctx.get('zt', {}).get('count', 0)}家(连板{ctx.get('zt', {}).get('streak_count', 0)})")
    lines.append("")
    explanation = _market_explanation_for_report(ctx)
    if explanation:
        from reporting.market_explanation import render_market_explanation
        lines.append(render_market_explanation(explanation, "markdown"))
        lines.append("")

    # ② 板块
    lines.append("### ② 板块")
    lines.append("")
    top = ctx.get("top_sectors", [])[:TOP_SECTOR_COUNT]
    bottom = ctx.get("bottom_sectors", [])[:3]
    lines.append(f"**最强前{TOP_SECTOR_COUNT}**:")
    if top:
        for s in top:
            lines.append(f"- {s.get('name', '')} {_safe_float(s.get('change_pct')):+.2f}%")
    else:
        lines.append("- —")
    lines.append("")
    lines.append("**最弱前3**:")
    if bottom:
        for s in bottom:
            lines.append(f"- {s.get('name', '')} {_safe_float(s.get('change_pct')):+.2f}%")
    else:
        lines.append("- —")
    lines.append("")

    lines.append("---")
    lines.append(f"> *数据来源: 东方财富/腾讯 + AKShare | {DISCLAIMER}*")
    return "\n".join(lines)


# ──────────────── 主流程 ────────────────


def collect_context(now=None) -> dict:
    """拉数据 → 评分 → 组装今日上下文.

    now: 可注入时钟供盘中混合测试;默认 datetime.now().
    """
    # 指数
    index_codes = list(dict.fromkeys(TREND_INDEX_CODES + AMOUNT_INDEX_CODES))
    index_rows = {}
    index_diagnostics = {}
    for code in index_codes:
        diagnostics = {}
        index_rows[code] = fetch_index_kline(code, lmt=80,
                                             diagnostics=diagnostics)
        index_diagnostics[code] = diagnostics
    index_metrics = {
        code: _index_metrics(index_rows.get(code, []))
        for code in TREND_INDEX_CODES
    }
    # 成交额历史优先使用指数K线；腾讯仅提供当日额时补持久化历史。
    amount_hist = complete_market_amounts(index_rows)
    index_dates = []
    for rows in index_rows.values():
        for row in rows:
            raw = str(row.get("trade_date") or "")
            compact = (raw if len(raw) == 8 and raw.isdigit()
                       else raw.replace("-", "")[:8])
            if len(compact) == 8 and compact.isdigit():
                iso = _verified_history_date(
                    f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}")
                if iso:
                    index_dates.append(iso)
    data_date = max(index_dates) if index_dates else ""
    raw_date = data_date.replace("-", "") if data_date else ""
    today_amount_yi = amount_hist.get(raw_date) if raw_date else None
    amount_evidence = build_amount_evidence(
        index_rows, index_diagnostics, data_date=data_date)

    sectors = fetch_sector_rankings()
    zt = fetch_zt_stats()
    activity = fetch_market_activity()

    zt_evidence = (zt.get("evidence") or {}) if isinstance(zt, dict) else {}
    if not zt_evidence:
        zt_evidence = {
            "schema_version": "market-component-evidence/v1",
            "provider": "unknown",
            "data_date": data_date or None,
            "fetched_at": None,
            "source_timestamp": None,
            "completeness": "missing",
            "usage": "unavailable",
            "reasons": ["legacy_or_unverified_zt_payload"],
        }
    activity_evidence = (activity.get("evidence") or {}) if isinstance(activity, dict) else {}
    if not activity_evidence:
        activity_evidence = {
            "schema_version": "market-component-evidence/v1",
            "provider": "unknown",
            "data_date": data_date or None,
            "fetched_at": None,
            "source_timestamp": None,
            "completeness": "missing",
            "usage": "unavailable",
            "reasons": ["legacy_or_unverified_activity_payload"],
        }

    history = load_history()
    amount_history_yi = previous_amounts(
        history, data_date, amount_hist,
        require_evidence=True,
        fetched_dates=set(amount_evidence.get("complete_dates") or []),
    )
    history_zt_counts = [
        int(h.get("zt", {}).get("count", 0))
        for h in _baseline_history(history, data_date).values()
        if _history_zt_eligible(h)
    ]

    components = {
        "index_trend": score_index_trend(index_metrics),
        "volume": score_volume(today_amount_yi, amount_history_yi, amount_evidence),
        "breadth": score_breadth(activity, sectors),
        "zt_emotion": score_zt_emotion(zt, history_zt_counts, zt_evidence),
        "capital": score_capital(activity),
    }

    index_providers = [
        str((index_diagnostics.get(code) or {}).get("provider")
            or (index_diagnostics.get(code) or {}).get("source") or "unknown")
        for code in TREND_INDEX_CODES
    ]
    index_fetched_at = [
        str((index_diagnostics.get(code) or {}).get("fetched_at"))
        for code in TREND_INDEX_CODES
        if (index_diagnostics.get(code) or {}).get("fetched_at")
    ]
    index_source_kind = (
        "unknown" if any(value == "unknown" for value in index_providers)
        else "alternative" if any(value not in {"eastmoney", "akshare"}
                                   for value in index_providers)
        else "primary"
    )
    components["index_trend"].update({
        "metric": "index_ma20_state",
        "provider": "+".join(dict.fromkeys(index_providers)),
        "source_kind": index_source_kind,
        "data_date": data_date or None,
        "fetched_at": max(index_fetched_at) if index_fetched_at else None,
        "source_timestamp": None,
    })
    components["volume"].update({
        "metric": "turnover_vs_20d_avg",
        "provider": amount_evidence.get("provider", "unknown"),
        "source_kind": amount_evidence.get("source_kind", "unknown"),
        "data_date": amount_evidence.get("data_date"),
        "fetched_at": amount_evidence.get("fetched_at"),
        "source_timestamp": amount_evidence.get("source_timestamp"),
    })
    for key, metric in (("breadth", "market_breadth"), ("capital", "market_main_force_net_inflow")):
        components[key].update({
            "metric": metric,
            "provider": activity_evidence.get("provider", "unknown"),
            "source_kind": "primary" if activity_evidence.get("provider") == "eastmoney" else "unknown",
            "data_date": activity_evidence.get("data_date") or data_date or None,
            "fetched_at": activity_evidence.get("fetched_at"),
            "source_timestamp": activity_evidence.get("source_timestamp"),
        })
    components["zt_emotion"].update({
        "metric": "limit_up_emotion",
        "provider": zt_evidence.get("provider", "unknown"),
        "source_kind": "primary" if zt_evidence.get("provider") == "akshare" else "unknown",
        "data_date": zt_evidence.get("data_date") or data_date or None,
        "fetched_at": zt_evidence.get("fetched_at"),
        "source_timestamp": zt_evidence.get("source_timestamp"),
    })
    regime = compute_regime(components)

    # ── 盘中混合: 昨收锚 + 盘中外推(避免半日数据对全天基线误判弱势) ──
    now = now or datetime.now()
    fraction = _session_elapsed_fraction(now)
    is_intraday = fraction > 0 and data_date == now.date().isoformat()
    intraday_note = ""
    intraday_evidence = None
    amount_yi_display = round(today_amount_yi, 0) if today_amount_yi else None
    zt_display = zt
    if is_intraday:
        last_close = _last_close_context(history, data_date)
        w = _blend_weight(fraction)
        if fraction >= FLOOR_FRACTION and today_amount_yi and activity:
            # 外推: est = partial / 已过交易时间占比
            est_amount = today_amount_yi / max(fraction, FLOOR_FRACTION)
            est_zt = {**zt, "count": int(round(zt.get("count", 0) / max(fraction, FLOOR_FRACTION)))} if zt else {}
            est_activity = {
                **activity,
                "up": int(round(activity.get("up", 0) / max(fraction, FLOOR_FRACTION))),
                "down": int(round(activity.get("down", 0) / max(fraction, FLOOR_FRACTION))),
            }
            if activity.get("main_force_yi") is not None:
                est_activity["main_force_yi"] = (
                    activity["main_force_yi"] / max(fraction, FLOOR_FRACTION))
            ext_components = {
                "index_trend": components["index_trend"],
                "volume": score_volume(est_amount, amount_history_yi, amount_evidence),
                "breadth": score_breadth(est_activity, sectors),
                "zt_emotion": score_zt_emotion(est_zt, history_zt_counts, zt_evidence),
                "capital": score_capital(est_activity),
            }
            for key, source in (("volume", amount_evidence), ("zt_emotion", zt_evidence)):
                ext_components[key].update({
                    "provider": source.get("provider", "unknown"),
                    "source_kind": source.get("source_kind", "unknown"),
                    "data_date": source.get("data_date") or data_date or None,
                    "fetched_at": source.get("fetched_at"),
                    "source_timestamp": source.get("source_timestamp"),
                })
            for key in ("breadth", "capital"):
                ext_components[key].update({
                    "provider": activity_evidence.get("provider", "unknown"),
                    "source_kind": "primary" if activity_evidence.get("provider") == "eastmoney" else "unknown",
                    "data_date": activity_evidence.get("data_date") or data_date or None,
                    "fetched_at": activity_evidence.get("fetched_at"),
                    "source_timestamp": activity_evidence.get("source_timestamp"),
                })
            ext_regime = compute_regime(ext_components)
            amount_yi_display = round(est_amount, 0)
            zt_display = est_zt
        else:
            # 开盘前 ~40 分钟不外推(放大早盘噪音): 直接用昨收条目组件
            # history 存储的 components 为 {key: score_float},需还原为 {key: {"score": ...}}
            stored = ((last_close or {}).get("components") or {})
            ext_components = (
                {k: {"score": v} for k, v in stored.items() if v is not None}
                if stored else components)
            ext_regime = compute_regime(ext_components)
        anchor_score = (last_close or {}).get("score") if last_close else None
        calculation_anchor_score = anchor_score if anchor_score is not None else 50.0
        blended = round((1 - w) * _safe_float(calculation_anchor_score) + w * ext_regime["score"], 1)
        label, advice = _regime_gate(blended)
        # Keep the quality/audit contract calculated from the actual intraday
        # components.  Only the displayed score and its gate are blended with
        # the prior-close anchor; rebuilding a short dict here would silently
        # turn complete/partial/missing data into ``unknown`` downstream.
        regime = dict(ext_regime)
        regime.update({
            "score": blended,
            "label": label,
            "advice": advice,
            "intraday": True,
        })
        components = ext_components
        intraday_evidence = {
            "anchor_score": anchor_score,
            "blend_weight": round(w, 6),
            "projected_score": ext_regime["score"],
            "blended_score": blended,
            "anchor_date": (last_close or {}).get("date"),
            "fraction": round(fraction, 6),
            "session_elapsed_fraction": round(fraction, 6),
        }
        anchor_label = f"昨收 {_safe_float(anchor_score):.1f}" if last_close else "中性 50(无前收基准)"
        intraday_note = (
            f"盘中快照 {fraction:.0%} 时段: 评分为 {anchor_label} 与按 "
            f"{max(fraction, FLOOR_FRACTION):.0%} 外推盘中分的混合(权重 {w:.0%}); "
            f"收盘后请复跑 /daily-review 确认"
        )

    # 板块最强/最弱(industry, 按 change_pct)
    ranked = sorted(sectors, key=lambda s: _safe_float(s.get("change_pct")), reverse=True)
    top_sectors = [{"name": s.get("name"), "change_pct": _safe_float(s.get("change_pct"))} for s in ranked[:TOP_SECTOR_COUNT]]
    bottom_sectors = [{"name": s.get("name"), "change_pct": _safe_float(s.get("change_pct"))} for s in ranked[-5:]]

    ctx = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": data_date,
        "stale_note": (
            "" if not data_date or data_date == date.today().isoformat()
            else f"数据日期 {data_date},非今日(可能非交易日或盘中)"
        ),
        "regime": regime,
        "components": components,
        "amount_evidence": amount_evidence,
        "zt_evidence": zt_evidence,
        "activity_evidence": activity_evidence,
        "intraday_evidence": intraday_evidence,
        "capital_context": {
            "metric": "market_main_force_net_inflow" if (
                activity and activity.get("main_force_yi") is not None
            ) else "capital_flow",
            "source_kind": "primary" if activity_evidence.get("provider") == "eastmoney" else "unknown",
            "provider": activity_evidence.get("provider", "unknown"),
            "data_date": activity_evidence.get("data_date") or data_date or None,
            "fetched_at": activity_evidence.get("fetched_at"),
            "source_timestamp": activity_evidence.get("source_timestamp"),
        },
        "amount_yi": amount_yi_display,
        "zt": zt_display,
        "intraday": is_intraday,
        "intraday_note": intraday_note,
        "indices": {
            code: {
                "ok": m.get("ok", False),
                "close": m.get("close"),
                "pct_chg": m.get("pct_chg"),
                "above_ma20": m.get("above_ma20"),
                "ma20_rising": m.get("ma20_rising"),
                "data_date": _iso_date_from_value(index_diagnostics.get(code, {}).get("data_date")),
                "source": index_diagnostics.get(code, {}).get("source"),
                "provider": index_diagnostics.get(code, {}).get("provider")
                            or index_diagnostics.get(code, {}).get("source"),
                "fetched_at": index_diagnostics.get(code, {}).get("fetched_at"),
                "source_timestamp": index_diagnostics.get(code, {}).get("source_timestamp"),
                "record_count": index_diagnostics.get(code, {}).get("record_count", 0),
                "amount_available_days": index_diagnostics.get(code, {}).get("amount_available_days", 0),
            }
            for code, m in index_metrics.items()
        },
        "index_data_quality": index_diagnostics,
        "top_sectors": top_sectors,
        "bottom_sectors": bottom_sectors,
    }
    from analysis.market_explanation import build_market_explanation
    ctx["market_explanation"] = build_market_explanation(
        ctx, data_date or date.today().isoformat())
    return ctx


def build_agent_output(ctx: dict) -> dict:
    """Build the compact JSON contract consumed by agents."""
    return {
        "meta": {"generated_at": ctx["generated_at"],
                 "data_date": ctx["data_date"]},
        "regime": ctx["regime"],
        "components": ctx["components"],
        "market_explanation": ctx.get("market_explanation"),
        "indices": ctx.get("indices", {}),
        "amount_evidence": ctx.get("amount_evidence", {}),
        "zt_evidence": ctx.get("zt_evidence", {}),
        "activity_evidence": ctx.get("activity_evidence", {}),
        "capital_context": ctx.get("capital_context", {}),
        "intraday_evidence": ctx.get("intraday_evidence"),
        "amount_yi": ctx["amount_yi"],
        "intraday": ctx.get("intraday", False),
        "intraday_note": ctx.get("intraday_note", ""),
        "index_data_quality": ctx.get("index_data_quality", {}),
        "zt": ctx["zt"],
        "top_sectors": ctx["top_sectors"],
        "bottom_sectors": ctx["bottom_sectors"],
    }


def main():
    parser = argparse.ArgumentParser(description="今日复盘 + 市场环境评分")
    parser.add_argument("--no-refresh", action="store_true",
                        help="跳过实时拉取,用今日缓存重出报告")
    parser.add_argument("--json", action="store_true", help="JSON 输出到 stdout")
    parser.add_argument("--html", dest="html", action="store_true", default=True,
                        help="(默认) 生成 HTML 报告")
    parser.add_argument("--no-html", dest="html", action="store_false",
                        help="不生成 HTML(仅 MD)")
    parser.add_argument("--observation-status", choices=("pending", "ready"), default="ready",
                        help="HTML 观察列表状态；统一入口先写 pending，再由后台更新")
    args = parser.parse_args()

    start = time.time()

    if args.no_refresh:
        ctx = load_context()
        if not ctx:
            print("⚠️ 无今日缓存(market_regime.json),先不带 --no-refresh 跑一次")
            return
        save_context(ctx)
    else:
        print("[1/5] 拉取指数K线 + 成交额...")
        print("[2/5] 拉取行业板块排行...")
        print("[3/5] 拉取涨停情绪...")
        print("[4/5] 拉取资金(全市场主力净流入)...")
        print("[5/5] 计算市场评分...")
        ctx = collect_context()
        # 持久化: 盘中快照不写 history(避免 partial 污染基线),但 context 仍写
        # (candidates 盘中需要当日 regime 分档)
        if should_save_history(ctx):
            component_evidence = {
                str(item.get("id")): copy.deepcopy(item.get("evidence") or {})
                for item in (ctx.get("market_explanation") or {}).get("components", [])
                if isinstance(item, dict) and item.get("id")
            }
            history_entry = {
                "date": ctx["data_date"],
                "regime_score": ctx["regime"]["score"],
                "label": ctx["regime"]["label"],
                "components": {k: v.get("score") for k, v in ctx["components"].items()},
                "amount_yi": ctx["amount_yi"],
                "zt": ctx["zt"],
                "intraday": False,
                "component_evidence": component_evidence,
                "amount_evidence": copy.deepcopy(ctx.get("amount_evidence") or {}),
                "zt_evidence": copy.deepcopy(ctx.get("zt_evidence") or {}),
                "activity_evidence": copy.deepcopy(ctx.get("activity_evidence") or {}),
            }
            saved = save_history(history_entry)
            ctx["history_persistence"] = get_last_history_write_result()
            if not saved and ctx["history_persistence"].get("status") == "error":
                ctx["history_persistence"]["status"] = "write_error"
        save_context(ctx)

    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.json:
        # 精简 JSON 供 Agent 消费
        print(json.dumps(build_agent_output(ctx), ensure_ascii=False, indent=2))
    else:
        report = generate_report(ctx)
        print(report)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        md_path = REPORTS_DIR / f"daily-review-{now_ts}.md"
        md_path.write_text(report, encoding="utf-8")
        print(f"\nMD: {md_path}")

    if args.html:
        try:
            observation_state = None
            if args.observation_status == "ready":
                try:
                    from analysis.observation_list_analysis import analyze_observation_list
                    analyze_observation_list(ctx.get("data_date") or "")
                    observation_state = load_observation_analysis(ctx.get("data_date") or "")
                except Exception as exc:
                    observation_state = {"status": "unavailable", "items": [],
                                         "reason": f"YAML 观察分析失败: {type(exc).__name__}"}
            html = _generate_html(ctx, now_ts, observation_status=args.observation_status,
                                  observation_state=observation_state)
            html_path = REPORTS_DIR / f"daily-review-{now_ts}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"HTML: {html_path}")
        except Exception as e:
            print(f"⚠️ HTML 生成失败: {e}")

    print(f"\nDone in {time.time() - start:.1f}s")


def _generate_html(ctx: dict, now_ts: str, *, observation_status: str = "ready",
                   observation_state: dict | None = None) -> str:
    """Lightweight HTML mirror of the MD report."""
    regime = ctx.get("regime", {})
    label_color = {"强势": "#dc2626", "中性": "#d97706", "弱势": "#16a34a"}.get(regime.get("label", ""), "#86868b")
    comps = ctx.get("components", {})
    explanation = _market_explanation_for_report(ctx)
    explanation_html = ""
    if explanation:
        from reporting.market_explanation import render_market_explanation
        explanation_html = render_market_explanation(explanation, "html")

    def comp_row(key, name):
        c = comps.get(key) or {}
        return f"<tr><td>{name}</td><td><strong>{c.get('score', '—')}</strong></td><td>{c.get('detail', '—')}</td></tr>"

    top = "".join(
        f"<li><strong>{s.get('name','')}</strong> {_safe_float(s.get('change_pct')):+.2f}%</li>"
        for s in ctx.get("top_sectors", [])[:TOP_SECTOR_COUNT])
    bottom = "".join(
        f"<li><strong>{s.get('name','')}</strong> {_safe_float(s.get('change_pct')):+.2f}%</li>"
        for s in ctx.get("bottom_sectors", [])[:3])
    observation_pending = observation_status == "pending"
    if not observation_pending and observation_state is None:
        observation_state = load_observation_analysis(ctx.get("data_date") or "")
    observation_html = render_observation_list_html(
        observation_state, pending=observation_pending)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>今日复盘 {ctx.get('data_date','')}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px}}
.w{{max-width:1000px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:32px 36px}}
h1{{font-size:24px}} h2{{font-size:18px;margin:22px 0 10px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}}
.dt{{color:#86868b;font-size:14px;margin:4px 0}}
.score{{font-size:44px;font-weight:800;color:{label_color}}}
table{{width:100%;border-collapse:collapse;margin:12px 0;border-radius:8px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:14px}}
th{{background:#1d4ed8;color:#fff;font-size:13px}}
.observation-table-wrap{{overflow-x:auto;margin:12px 0}}.observation-table-wrap table{{min-width:1120px}}
ul{{padding-left:20px;line-height:1.8}}
.disc{{color:#a1a1a6;font-size:12px;text-align:center;margin-top:28px}}
</style></head><body><div class="w">
<h1>📅 今日复盘 {ctx.get('data_date','')}</h1>
<p class="dt">{ctx.get('generated_at','')}</p>
<div class="score">{regime.get('score',0)} / 100 <span style="font-size:18px">{regime.get('label','')}</span></div>
<p class="dt">{regime.get('advice','')}</p>
{'<p class="dt">⚠️ ' + ctx.get('stale_note','') + '</p>' if ctx.get('stale_note') else ''}
{'<p class="dt" style="color:#d97706">⚠️ ' + ctx.get('intraday_note','') + '</p>' if ctx.get('intraday_note') else ''}

<h2>① 市场环境</h2>
<table><thead><tr><th>组件</th><th>得分</th><th>说明</th></tr></thead><tbody>
{comp_row('index_trend','大盘趋势')}{comp_row('volume','成交额')}{comp_row('breadth','赚钱效应')}{comp_row('zt_emotion','涨停情绪')}{comp_row('capital','资金')}
</tbody></table>
{explanation_html}

<h2>② 板块</h2>
<p><strong>最强前{TOP_SECTOR_COUNT}:</strong></p><ul>{top or '<li>—</li>'}</ul>
<p><strong>最弱前3:</strong></p><ul>{bottom or '<li>—</li>'}</ul>

{observation_html}

<footer><p class="disc">数据来源: 东方财富 + AKShare | {DISCLAIMER}</p></footer>
</div></body></html>"""


if __name__ == "__main__":
    main()
