#!/usr/bin/env python3
"""今日复盘 + 市场环境评分 — 独立市场上下文整合.

聚合全市场数据(大盘指数/两市成交额/涨跌家数/涨停情绪/北向资金/板块排行),
输出市场环境评分(0-100) + 每日复盘报告,并持久化上下文供 /stock-trend 做大盘/板块对比.

数据源(全部复用现有 fetcher):
  - 指数K线:  kline_eastmoney (000001.SH上证 / 000300.SH沪深300 / 399001.SZ深成 / 399106.SZ深证综指)
  - 涨跌家数: sector_data 行业板块 up/down_count 加总(仅 industry,避免概念重复计数)
  - 板块排行: sector_data.get_sector_rankings (industry)
  - 涨停情绪: zt_replay.fetch_limitup_stocks + 连板统计
  - 资金:     capital_flow.fetch_northbound_flow(北向不可用降级用板块主力净流入)

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
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = Path(os.environ.get("STOCK_TREND_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "stock-trend")))
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"
PORTFOLIO_YAML = SCRIPT_DIR.parent / "data" / "portfolio.yaml"
CONTEXT_FILE = CACHE_DIR / "market_regime.json"
HISTORY_FILE = CACHE_DIR / "market_regime_history.json"
HISTORY_MAX_DAYS = 30
MIN_AMOUNT_HISTORY_DAYS = 5

sys.path.insert(0, str(SCRIPT_DIR))

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


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
    secid = build_secid(code)
    if secid:
        for _attempt in range(max(1, retries)):
            try:
                (records, _name), _used = rotate_em_host(
                    lambda h: fetch_eastmoney(secid, freq="D", lmt=lmt, host=h))
                records = _sort_kline(records)
                if records:
                    status.update({
                        "source": "eastmoney",
                        "record_count": len(records),
                        "data_date": records[-1]["trade_date"],
                        "errors": errors,
                    })
                    return records
            except Exception as exc:
                errors.append(f"eastmoney: {exc}")
                continue
    try:
        records, _name = fetch_tencent_a_stock(code, "D")
        records = _sort_kline(records)[-lmt:]
        if records:
            status.update({
                "source": "tencent",
                "record_count": len(records),
                "data_date": records[-1]["trade_date"],
                "errors": errors,
            })
            return records
    except Exception as exc:
        errors.append(f"tencent: {exc}")
    try:
        records, _name = fetch_baostock(code, "D")
        records = _sort_kline(records)[-lmt:]
        if records:
            status.update({
                "source": "baostock",
                "record_count": len(records),
                "data_date": records[-1]["trade_date"],
                "errors": errors,
            })
            return records
    except Exception as exc:
        errors.append(f"baostock: {exc}")
    status.update({"source": "error", "record_count": 0,
                   "data_date": "", "errors": errors})
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

    直连 AKShare 涨停池(绕开 zt_replay 的 concept map,避免触发全市场板块映射构建).
    """
    if not HAS_AKSHARE:
        return {"count": 0, "streak_count": 0, "max_streak": 0}
    try:
        dt = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=dt)
        if df is None or df.empty:
            return {"count": 0, "streak_count": 0, "max_streak": 0}
        streaks = []
        for v in df.get("连板数", []):
            try:
                streaks.append(int(v))
            except (TypeError, ValueError):
                streaks.append(1)
        streaks = [s if s >= 1 else 1 for s in streaks]
        return {
            "count": len(streaks),
            "streak_count": sum(1 for s in streaks if s >= 2),
            "max_streak": max(streaks) if streaks else 0,
        }
    except Exception:
        return {"count": 0, "streak_count": 0, "max_streak": 0}


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
        return {"up": up, "down": down, "main_force_yi": round(main_force_yi, 1)}
    except Exception:
        return None


def fetch_northbound() -> float | None:
    """北向净买入(亿元);不可用或全零(2024-08 披露机制调整后净买入已不公开)返回 None."""
    try:
        from fetchers.capital_flow import fetch_northbound_flow
        flow = fetch_northbound_flow()
        if flow:
            vals = [_safe_float(r.get("net_buy_billion")) for r in flow]
            if vals and any(v != 0 for v in vals):
                return vals[-1]
    except Exception:
        pass
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
        return {"score": 50.0, "detail": "指数数据不可用"}
    return {"score": round(sum(scores) / len(scores), 1), "detail": "; ".join(detail)}


def score_volume(today_amount_yi: float | None, amount_history_yi: list[float]) -> dict:
    """成交额分: 两市成交额 vs 近20日均额."""
    if not today_amount_yi or today_amount_yi <= 0:
        return {"score": 50.0, "detail": "成交额不可用"}
    hist = [a for a in amount_history_yi if a > 0][-20:]
    if len(hist) < MIN_AMOUNT_HISTORY_DAYS:
        return {
            "score": 50.0,
            "detail": f"两市 {today_amount_yi:.0f}亿,成交额历史不足 "
                      f"{len(hist)}/{MIN_AMOUNT_HISTORY_DAYS}",
        }
    base = sum(hist) / len(hist)
    ratio = today_amount_yi / base
    score = _clamp(50 + (ratio - 1.0) * 150)
    pct = (ratio - 1.0) * 100
    return {
        "score": round(score, 1),
        "detail": f"两市 {today_amount_yi:.0f}亿,较20日均额 {pct:+.0f}%",
    }


def score_breadth(breadth: dict | None, industry_sectors: list[dict]) -> dict:
    """赚钱效应分: 全市场涨跌家数比(地域板块加总) + 行业板块上涨占比."""
    if not breadth or breadth.get("up", 0) + breadth.get("down", 0) <= 0:
        return {"score": 50.0, "detail": "涨跌家数不可用", "up": 0, "down": 0}
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
    }


def score_zt_emotion(zt: dict, history_counts: list[int]) -> dict:
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
    return {
        "score": round(score, 1),
        "detail": f"涨停 {count}家(连板{streak_count},最高{max_streak}板;{vs})+连板加成{bonus}",
    }


def score_capital(northbound_yi: float | None, market_activity: dict | None) -> dict:
    """资金分: 北向净买入;不可用降级用全市场主力净流入(地域板块加总,精确)."""
    if northbound_yi is not None:
        score = _clamp(50 + northbound_yi * 8)
        return {"score": round(score, 1), "detail": f"北向净买入 {northbound_yi:+.1f}亿"}
    main_force_yi = market_activity.get("main_force_yi") if market_activity else None
    if main_force_yi is not None:
        score = _clamp(50 + main_force_yi * 0.06)
        return {"score": round(score, 1),
                "detail": f"全市场主力净流入 {main_force_yi:+.1f}亿(北向不可用降级)"}
    return {"score": 50.0, "detail": "资金数据不可用"}


def _regime_gate(score: float) -> tuple[str, str]:
    """评分 → (label, advice)."""
    if score >= 80:
        return "强势", "可正常建仓/加仓,重点做强势板块龙头"
    if score >= 60:
        return "中性", "轻仓观察,只做高分标的,不追高"
    return "弱势", "降仓/空仓,不找牛股,等大盘站上MA20"


def compute_regime(components: dict) -> dict:
    """综合市场环境分(0-100) + gate 标签."""
    weights = {
        "index_trend": 0.25,
        "volume": 0.20,
        "breadth": 0.25,
        "zt_emotion": 0.20,
        "capital": 0.10,
    }
    total = 0.0
    used_weight = 0.0
    for key, w in weights.items():
        comp = components.get(key) or {}
        s = comp.get("score")
        if s is None:
            continue
        total += _safe_float(s) * w
        used_weight += w
    if used_weight <= 0:
        return {"score": 50.0, "label": "中性", "advice": "数据不可用"}
    score = round(_clamp(total / used_weight), 1)
    label, advice = _regime_gate(score)
    return {"score": score, "label": label, "advice": advice}


# ──────────────── 持久化 ────────────────


def load_history(days: int = 30) -> dict:
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict)}
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
    entries = {
        k: v for k, v in history.items()
        if k < data_date and isinstance(v, dict)
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
                     fetched: dict | None = None) -> list[float]:
    """Return prior turnover values, preferring freshly fetched dates."""
    by_date = {}
    for history_date, entry in sorted(_baseline_history(history, before_date).items()):
        amount = _safe_float(entry.get("amount_yi"))
        if amount > 0:
            by_date[history_date] = amount
    for raw_date, raw_amount in (fetched or {}).items():
        iso_date = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                    if len(raw_date) == 8 else raw_date)
        amount = _safe_float(raw_amount)
        if iso_date < before_date and amount > 0:
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


def should_save_history(ctx: dict) -> bool:
    """盘中快照不写历史基线(避免 partial 数据污染);全天/收盘后条目才写."""
    return not bool(ctx.get("intraday", False))


def save_history(entry: dict) -> None:
    history = load_history()
    entry.setdefault("intraday", False)
    history[entry["date"]] = entry
    # prune to newest N
    items = sorted(history.items(), key=lambda kv: kv[0])[-HISTORY_MAX_DAYS:]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(
            json.dumps(dict(items), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


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
            return json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


# ──────────────── 持仓轻量分析 ────────────────


def load_portfolio() -> list[dict]:
    """Read active holdings from portfolio.yaml."""
    try:
        import yaml
        data = yaml.safe_load(PORTFOLIO_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    holdings = data.get("holdings", []) if isinstance(data, dict) else []
    return [h for h in holdings if h.get("status") == "active"]


def portfolio_snapshot_meta(holdings: list[dict] | None = None) -> dict:
    """Return a versioned description of the portfolio used by a review.

    Market context can be safely reused on non-trading days, but the active
    holdings list can change at any time.  Persist both a file fingerprint and
    the active codes so cached reviews can detect and repair that divergence.
    """
    active = holdings if holdings is not None else load_portfolio()
    meta = {
        "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "active_codes": [str(h.get("code", "")) for h in active],
        "active_count": len(active),
    }
    try:
        raw = PORTFOLIO_YAML.read_bytes()
        meta["source_mtime_ns"] = PORTFOLIO_YAML.stat().st_mtime_ns
        meta["content_sha256"] = hashlib.sha256(raw).hexdigest()
    except OSError:
        meta["source_mtime_ns"] = None
        meta["content_sha256"] = None
    return meta


def refresh_cached_holdings(ctx: dict) -> dict:
    """Reconcile a cached market review with the current active holdings.

    This deliberately does not fetch new K-lines: ``--no-refresh`` remains a
    market-data cache path.  Existing technical snapshots are retained for
    unchanged codes; new holdings are shown as pending the next live review.
    """
    active = load_portfolio()
    snapshot = portfolio_snapshot_meta(active)
    cached = {str(h.get("code", "")): h for h in ctx.get("holdings", [])}
    refreshed = []
    for holding in active[:8]:
        code = str(holding.get("code", ""))
        previous = cached.get(code)
        if previous:
            item = dict(previous)
            # Portfolio fields are authoritative even when its technical data
            # comes from the cached market snapshot.
            item["name"] = holding.get("name") or item.get("name") or code
            item["stop_loss"] = holding.get("stop_loss")
            item["targets"] = holding.get("targets") or []
        else:
            item = {
                "code": code,
                "name": holding.get("name") or code,
                "close": None,
                "pct_chg": None,
                "ma5": None,
                "ma20": None,
                "above_ma5": None,
                "above_ma20": None,
                "stop_loss": holding.get("stop_loss"),
                "targets": holding.get("targets") or [],
                "ok": False,
            }
        refreshed.append(item)

    previous_meta = ctx.get("portfolio_snapshot") or {}
    old_codes = previous_meta.get("active_codes") or list(cached)
    changed = (
        old_codes != snapshot["active_codes"]
        or previous_meta.get("content_sha256") != snapshot.get("content_sha256")
    )
    ctx["holdings"] = refreshed
    ctx["portfolio_snapshot"] = snapshot
    ctx["holdings_refreshed_at"] = snapshot["loaded_at"]
    if changed:
        ctx["holdings_sync_note"] = (
            "持仓已按当前持仓记录刷新；市场与技术数据仍沿用缓存快照，"
            "新增持仓的技术数据待下次实时复盘补齐。"
        )
    else:
        ctx.pop("holdings_sync_note", None)
    ctx["plan"] = build_plan(ctx.get("regime", {}), refreshed)
    return ctx


def analyze_holding(holding: dict) -> dict:
    """Lightweight holding check: 现价 vs MA5/MA20 + 今日涨跌 + 相对止损/目标."""
    code = str(holding.get("code", ""))
    suffix = holding.get("ts_code", "")
    if not suffix and code:
        suffix = code + ".SH" if code.startswith("6") else code + ".SZ"
    records = fetch_index_kline(suffix, lmt=40) if suffix else []
    result = {
        "code": code,
        "name": holding.get("name") or code,
        "close": None,
        "pct_chg": None,
        "ma5": None,
        "ma20": None,
        "above_ma5": None,
        "above_ma20": None,
        "stop_loss": holding.get("stop_loss"),
        "targets": holding.get("targets") or [],
        "ok": False,
    }
    if not records:
        return result
    metrics = _index_metrics(records)
    if not metrics.get("ok"):
        return result
    result["close"] = metrics["close"]
    result["pct_chg"] = metrics.get("pct_chg")
    result["ma5"] = metrics["ma5"]
    result["ma20"] = metrics["ma20"]
    result["above_ma5"] = metrics["close"] > metrics["ma5"]
    result["above_ma20"] = metrics["above_ma20"]
    result["ok"] = True
    return result


# ──────────────── 报告 ────────────────

DISCLAIMER = "本报告仅供学习参考,不构成任何投资建议。股市有风险,投资需谨慎。"


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
    lines.append(f"▸ 涨跌家数: 涨 {breadth.get('up', '—')} / 跌 {breadth.get('down', '—')} | "
                 f"两市成交 {ctx.get('amount_yi', 0):.0f}亿 | "
                 f"涨停 {ctx.get('zt', {}).get('count', 0)}家(连板{ctx.get('zt', {}).get('streak_count', 0)})")
    lines.append("")

    # ② 板块
    lines.append("### ② 板块")
    lines.append("")
    top = ctx.get("top_sectors", [])[:3]
    bottom = ctx.get("bottom_sectors", [])[:3]
    lines.append("**最强前3**:")
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

    # ③ 持仓
    lines.append("### ③ 持仓")
    lines.append("")
    portfolio_meta = ctx.get("portfolio_snapshot") or {}
    if portfolio_meta:
        lines.append(
            f"▸ 持仓快照: {portfolio_meta.get('loaded_at', '—')} | "
            f"活跃持仓 {portfolio_meta.get('active_count', '—')} 笔")
    if ctx.get("holdings_sync_note"):
        lines.append(f"▸ ⚠️ {ctx['holdings_sync_note']}")
    if portfolio_meta or ctx.get("holdings_sync_note"):
        lines.append("")
    holdings = ctx.get("holdings", [])
    if holdings:
        lines.append("| 代码 | 名称 | 现价 | 今日% | MA5 | MA20 | 相对止损 |")
        lines.append("|------|------|------|-------|-----|------|---------|")
        for h in holdings:
            if not h.get("ok"):
                lines.append(f"| {h['code']} | {h['name']} | — | — | — | — | 数据不可用 |")
                continue
            rel_sl = ""
            if h.get("stop_loss"):
                rel_sl = f"{(_safe_float(h['close']) / _safe_float(h['stop_loss']) - 1) * 100:+.1f}%"
            lines.append(
                f"| {h['code']} | {h['name']} | {h['close']:.3f} | "
                f"{_safe_float(h['pct_chg']):+.2f}% | {h['ma5']:.3f} | {h['ma20']:.3f} | "
                f"{rel_sl or '—'} |")
    else:
        lines.append("- 无活跃持仓")
    lines.append("")

    # ④ 明日计划
    lines.append("### ④ 明日计划 (if-then)")
    lines.append("")
    for p in ctx.get("plan", []):
        lines.append(f"- {p}")
    lines.append("")

    lines.append("---")
    lines.append(f"> *数据来源: 东方财富/腾讯 + AKShare | {DISCLAIMER}*")
    return "\n".join(lines)


def build_plan(regime: dict, holdings: list[dict]) -> list[str]:
    plan = []
    label = regime.get("label", "")
    if label == "强势":
        plan.append("如果 市场评分≥80维持强势 且 个股评分≥90 → 可建仓/加仓强势板块龙头")
        plan.append("如果 市场转弱跌破60 → 停止加仓,收紧止损")
    elif label == "中性":
        plan.append("如果 市场维持中性 → 轻仓,只做高分标的,不追高")
        plan.append("如果 市场评分站上80 → 可加大仓位")
        plan.append("如果 市场评分跌破60 → 降仓防守")
    else:
        plan.append("如果 市场弱势 → 降仓/空仓,不找牛股,等大盘站上MA20")
        plan.append("如果 大盘放量站回MA20 → 再恢复选股")
    for h in holdings:
        if not h.get("ok"):
            continue
        name = h["name"]
        if h.get("stop_loss") and _safe_float(h["close"]) <= _safe_float(h["stop_loss"]):
            plan.append(f"如果 {name} 跌破止损位 {h['stop_loss']} → 无条件离场")
        elif not h.get("above_ma20"):
            plan.append(f"如果 {name} 失守MA20 {h['ma20']:.3f} → 减仓/离场")
        elif h.get("above_ma5"):
            plan.append(f"如果 {name} 站稳MA5且MA20向上 → 继续持有")
        else:
            plan.append(f"如果 {name} 跌破MA5但不破MA20 → 持有观察,破MA20离场")
    if not plan:
        plan.append("如果 出现明确买点(放量突破/LPS) → 按评分系统建仓")
    return plan


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
    index_dates = [
        row["trade_date"]
        for rows in index_rows.values() for row in rows
        if row.get("trade_date")
    ]
    raw_date = max(index_dates) if index_dates else date.today().strftime("%Y%m%d")
    today_amount_yi = amount_hist.get(raw_date)
    # 归一为 ISO YYYY-MM-DD
    try:
        data_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        data_date = date.today().isoformat()

    sectors = fetch_sector_rankings()
    zt = fetch_zt_stats()
    activity = fetch_market_activity()

    history = load_history()
    amount_history_yi = previous_amounts(history, data_date, amount_hist)
    history_zt_counts = [
        int(h.get("zt", {}).get("count", 0))
        for h in _baseline_history(history, data_date).values()
    ]

    # 北向(不可用降级到全市场主力净流入)
    northbound = fetch_northbound()

    components = {
        "index_trend": score_index_trend(index_metrics),
        "volume": score_volume(today_amount_yi, amount_history_yi),
        "breadth": score_breadth(activity, sectors),
        "zt_emotion": score_zt_emotion(zt, history_zt_counts),
        "capital": score_capital(northbound, activity),
    }
    regime = compute_regime(components)

    # ── 盘中混合: 昨收锚 + 盘中外推(避免半日数据对全天基线误判弱势) ──
    now = now or datetime.now()
    fraction = _session_elapsed_fraction(now)
    is_intraday = fraction > 0 and data_date == now.date().isoformat()
    intraday_note = ""
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
                "volume": score_volume(est_amount, amount_history_yi),
                "breadth": score_breadth(est_activity, sectors),
                "zt_emotion": score_zt_emotion(est_zt, history_zt_counts),
                "capital": score_capital(northbound, est_activity),
            }
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
        anchor_score = (last_close or {}).get("score") if last_close else 50.0
        blended = round((1 - w) * _safe_float(anchor_score) + w * ext_regime["score"], 1)
        label, advice = _regime_gate(blended)
        regime = {"score": blended, "label": label, "advice": advice, "intraday": True}
        components = ext_components
        anchor_label = f"昨收 {_safe_float(anchor_score):.1f}" if last_close else "中性 50(无前收基准)"
        intraday_note = (
            f"盘中快照 {fraction:.0%} 时段: 评分为 {anchor_label} 与按 "
            f"{max(fraction, FLOOR_FRACTION):.0%} 外推盘中分的混合(权重 {w:.0%}); "
            f"收盘后请复跑 /daily-review 确认"
        )

    # 板块最强/最弱(industry, 按 change_pct)
    ranked = sorted(sectors, key=lambda s: _safe_float(s.get("change_pct")), reverse=True)
    top_sectors = [{"name": s.get("name"), "change_pct": _safe_float(s.get("change_pct"))} for s in ranked[:5]]
    bottom_sectors = [{"name": s.get("name"), "change_pct": _safe_float(s.get("change_pct"))} for s in ranked[-5:]]

    # 持仓轻量分析
    holdings_raw = load_portfolio()
    holdings = []
    for h in holdings_raw[:8]:
        holdings.append(analyze_holding(h))

    ctx = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": data_date,
        "stale_note": "" if data_date == date.today().isoformat() else f"数据日期 {data_date},非今日(可能非交易日或盘中)",
        "regime": regime,
        "components": components,
        "amount_yi": amount_yi_display,
        "zt": zt_display,
        "intraday": is_intraday,
        "intraday_note": intraday_note,
        "indices": {
            code: {
                "close": m.get("close"),
                "pct_chg": m.get("pct_chg"),
                "above_ma20": m.get("above_ma20"),
                "ma20_rising": m.get("ma20_rising"),
            }
            for code, m in index_metrics.items()
        },
        "index_data_quality": index_diagnostics,
        "top_sectors": top_sectors,
        "bottom_sectors": bottom_sectors,
        "holdings": holdings,
        "portfolio_snapshot": portfolio_snapshot_meta(holdings_raw),
        "holdings_refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plan": build_plan(regime, holdings),
    }
    return ctx


def build_agent_output(ctx: dict) -> dict:
    """Build the compact JSON contract consumed by agents."""
    return {
        "meta": {"generated_at": ctx["generated_at"],
                 "data_date": ctx["data_date"]},
        "regime": ctx["regime"],
        "components": ctx["components"],
        "amount_yi": ctx["amount_yi"],
        "intraday": ctx.get("intraday", False),
        "intraday_note": ctx.get("intraday_note", ""),
        "index_data_quality": ctx.get("index_data_quality", {}),
        "zt": ctx["zt"],
        "top_sectors": ctx["top_sectors"],
        "bottom_sectors": ctx["bottom_sectors"],
        "holdings": ctx["holdings"],
        "portfolio_snapshot": ctx.get("portfolio_snapshot", {}),
        "holdings_refreshed_at": ctx.get("holdings_refreshed_at"),
        "holdings_sync_note": ctx.get("holdings_sync_note", ""),
        "plan": ctx["plan"],
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
    args = parser.parse_args()

    start = time.time()

    if args.no_refresh:
        ctx = load_context()
        if not ctx:
            print("⚠️ 无今日缓存(market_regime.json),先不带 --no-refresh 跑一次")
            return
        # 市场数据沿用缓存，但持仓状态必须以当前组合为准。
        refresh_cached_holdings(ctx)
        save_context(ctx)
    else:
        print("[1/5] 拉取指数K线 + 成交额...")
        print("[2/5] 拉取行业板块排行...")
        print("[3/5] 拉取涨停情绪...")
        print("[4/5] 拉取资金(北向/主力)...")
        print("[5/5] 计算评分 + 持仓分析...")
        ctx = collect_context()
        # 持久化: 盘中快照不写 history(避免 partial 污染基线),但 context 仍写
        # (candidates 盘中需要当日 regime 分档)
        if should_save_history(ctx):
            history_entry = {
                "date": ctx["data_date"],
                "regime_score": ctx["regime"]["score"],
                "label": ctx["regime"]["label"],
                "components": {k: v.get("score") for k, v in ctx["components"].items()},
                "amount_yi": ctx["amount_yi"],
                "zt": ctx["zt"],
                "intraday": False,
            }
            save_history(history_entry)
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
            html = _generate_html(ctx, now_ts)
            html_path = REPORTS_DIR / f"daily-review-{now_ts}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"HTML: {html_path}")
        except Exception as e:
            print(f"⚠️ HTML 生成失败: {e}")

    print(f"\nDone in {time.time() - start:.1f}s")


def _generate_html(ctx: dict, now_ts: str) -> str:
    """Lightweight HTML mirror of the MD report."""
    regime = ctx.get("regime", {})
    label_color = {"强势": "#dc2626", "中性": "#d97706", "弱势": "#16a34a"}.get(regime.get("label", ""), "#86868b")
    comps = ctx.get("components", {})

    def comp_row(key, name):
        c = comps.get(key) or {}
        return f"<tr><td>{name}</td><td><strong>{c.get('score', '—')}</strong></td><td>{c.get('detail', '—')}</td></tr>"

    top = "".join(
        f"<li><strong>{s.get('name','')}</strong> {_safe_float(s.get('change_pct')):+.2f}%</li>"
        for s in ctx.get("top_sectors", [])[:3])
    bottom = "".join(
        f"<li><strong>{s.get('name','')}</strong> {_safe_float(s.get('change_pct')):+.2f}%</li>"
        for s in ctx.get("bottom_sectors", [])[:3])
    holdings_rows = ""
    for h in ctx.get("holdings", [])[:8]:
        if not h.get("ok"):
            holdings_rows += f"<tr><td>{h['code']}</td><td>{h['name']}</td><td colspan='5'>数据不可用</td></tr>"
            continue
        rel_sl = f"{(_safe_float(h['close'])/_safe_float(h['stop_loss'])-1)*100:+.1f}%" if h.get("stop_loss") else "—"
        holdings_rows += (
            f"<tr><td>{h['code']}</td><td>{h['name']}</td>"
            f"<td>{h['close']:.3f}</td><td>{_safe_float(h['pct_chg']):+.2f}%</td>"
            f"<td>{h['ma5']:.3f}</td><td>{h['ma20']:.3f}</td><td>{rel_sl}</td></tr>")
    plan = "".join(f"<li>{p}</li>" for p in ctx.get("plan", []))

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

<h2>② 板块</h2>
<p><strong>最强前3:</strong></p><ul>{top or '<li>—</li>'}</ul>
<p><strong>最弱前3:</strong></p><ul>{bottom or '<li>—</li>'}</ul>

<h2>③ 持仓</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>今日%</th><th>MA5</th><th>MA20</th><th>相对止损</th></tr></thead><tbody>
{holdings_rows or '<tr><td colspan="7">无活跃持仓</td></tr>'}
</tbody></table>

<h2>④ 明日计划</h2>
<ul>{plan or '<li>—</li>'}</ul>

<footer><p class="disc">数据来源: 东方财富 + AKShare | {DISCLAIMER}</p></footer>
</div></body></html>"""


if __name__ == "__main__":
    main()
