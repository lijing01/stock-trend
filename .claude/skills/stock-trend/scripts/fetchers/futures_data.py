#!/usr/bin/env python3
"""Index futures data fetcher for stock-trend skill.

Fetches corresponding index futures data for ETFs: basis (期现价差),
open interest trends, and volume confirmation signals.

Data sources (in priority order):
1. East Money push2his API (primary, A-share + HK futures)
2. AKShare futures_hist_em (fallback, A-share futures only)
3. Graceful degradation: data_source="unavailable"

Usage:
    python3 fetch_futures_data.py <etf_code> [options]

Examples:
    python3 fetch_futures_data.py 510300 -o /tmp/futures_data.json
    python3 fetch_futures_data.py 159740 --no-cache
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

from core.cache_utils import load_cache, save_cache, get_market_day_ttl, safe_float, output_json
from core.eastmoney_utils import (
    EM_HEADERS, EM_API_HOSTS, rotate_em_host,
    get_futures_secid, FUTURES_SECID_MAP, INDEX_SECID_MAP,
)


def _fetch_futures_kline_em(secid, days=30, host="push2his.eastmoney.com"):
    """Fetch futures K-line data from East Money API.

    Includes f63 (open interest) field which is specific to futures.

    Returns (records, name) tuple or raises on error.
    """
    beg = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")

    fields1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    # f51=日期, f52=开, f53=收, f54=高, f55=低,
    # f56=成交量, f57=成交额, f58=振幅, f59=涨跌幅, f60=涨跌额,
    # f61=换手率(期货为0), f63=持仓量
    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f63"

    url = (
        f"https://{host}/api/qt/stock/kline/get"
        f"?secid={secid}"
        f"&fields1={fields1}"
        f"&fields2={fields2}"
        f"&klt=101"
        f"&fqt=1"
        f"&beg={beg}"
        f"&end={end}"
        f"&lmt={days}"
    )

    req = urllib.request.Request(url, headers=EM_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if not result or result.get("rc") != 0 or not result.get("data"):
        error_msg = result.get("message", "unknown") if result else "no response"
        raise RuntimeError(f"East Money futures API error: {error_msg}")

    data = result["data"]
    klines = data.get("klines", [])
    name = data.get("name", "")

    if not klines:
        raise RuntimeError(f"No futures data returned for secid={secid}")

    records = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            record = {
                "date": parts[0].replace("-", ""),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "pct_chg": float(parts[8]) if len(parts) > 8 else None,
                "change": float(parts[9]) if len(parts) > 9 else None,
                "open_interest": float(parts[11]) if len(parts) > 11 and parts[11] else None,
            }
            records.append(record)
        except (ValueError, IndexError):
            continue

    return records, name


def _fetch_index_close_em(secid, days=30, host="push2his.eastmoney.com"):
    """Fetch spot index close prices for basis calculation.

    Returns list of {date, close} dicts or raises on error.
    """
    beg = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")

    fields1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    url = (
        f"https://{host}/api/qt/stock/kline/get"
        f"?secid={secid}"
        f"&fields1={fields1}"
        f"&fields2={fields2}"
        f"&klt=101"
        f"&fqt=1"
        f"&beg={beg}"
        f"&end={end}"
        f"&lmt={days}"
    )

    req = urllib.request.Request(url, headers=EM_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if not result or result.get("rc") != 0 or not result.get("data"):
        raise RuntimeError(f"East Money index API error for secid={secid}")

    data = result["data"]
    klines = data.get("klines", [])

    if not klines:
        raise RuntimeError(f"No index data returned for secid={secid}")

    records = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            records.append({
                "date": parts[0].replace("-", ""),
                "close": float(parts[2]),
            })
        except (ValueError, IndexError):
            continue

    return records


def _fetch_futures_kline_akshare(futures_code, days=30):
    """Fetch A-share futures data via AKShare as fallback.

    Only supports CFFEX futures (IF, IH, IC, IM).
    Returns (records, name) tuple or raises on error.
    """
    import akshare as ak

    name_map = {
        "IF": "沪深300主连",
        "IH": "上证50主连",
        "IC": "中证500主连",
        "IM": "中证1000主连",
    }
    symbol = name_map.get(futures_code)
    if not symbol:
        raise RuntimeError(f"AKShare does not support futures code: {futures_code}")

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")

    df = ak.futures_hist_em(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
    )

    if df is None or df.empty:
        raise RuntimeError(f"AKShare returned no data for {symbol}")

    # Normalize column names
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "持仓量": "open_interest",
        "涨跌幅": "pct_chg", "涨跌额": "change",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    records = []
    for _, row in df.iterrows():
        try:
            record = {
                "date": str(row.get("date", "")).replace("-", ""),
                "open": float(row.get("open", 0)),
                "close": float(row.get("close", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "volume": float(row.get("volume", 0)),
                "amount": float(row.get("amount", 0)),
                "pct_chg": float(row.get("pct_chg", 0)) if "pct_chg" in row else None,
                "change": float(row.get("change", 0)) if "change" in row else None,
                "open_interest": float(row.get("open_interest", 0)) if "open_interest" in row and row.get("open_interest") else None,
            }
            records.append(record)
        except (ValueError, TypeError):
            continue

    # Keep only last `days` records
    if len(records) > days:
        records = records[-days:]

    return records, name_map[futures_code]


def _fetch_index_close_akshare(index_secid, days=30):
    """Fetch A-share spot index close prices via AKShare as fallback.

    Returns list of {date, close} dicts or raises on error.
    """
    import akshare as ak

    # Map secid to AKShare index name
    index_map = {
        "1.000300": "000300",   # 沪深300
        "1.000016": "000016",   # 上证50
        "0.399905": "399905",   # 中证500
        "0.399852": "399852",   # 中证1000
    }
    index_code = index_map.get(index_secid)
    if not index_code:
        raise RuntimeError(f"AKShare does not support index secid: {index_secid}")

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")

    df = ak.index_zh_a_hist(
        symbol=index_code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
    )

    if df is None or df.empty:
        raise RuntimeError(f"AKShare returned no index data for {index_code}")

    records = []
    for _, row in df.iterrows():
        try:
            records.append({
                "date": str(row.get("日期", "")).replace("-", ""),
                "close": float(row.get("收盘", 0)),
            })
        except (ValueError, TypeError):
            continue

    if len(records) > days:
        records = records[-days:]

    return records


def calculate_oi_trend(history):
    """Calculate open interest trend metrics from history.

    Returns dict with current OI, change ratios vs MA5/MA20.
    Returns None if insufficient data.
    """
    if not history or len(history) < 2:
        return None

    oi_values = [h.get("open_interest") for h in history if h.get("open_interest") is not None]
    if not oi_values:
        return None

    current_oi = oi_values[-1]
    prev_oi = oi_values[-2] if len(oi_values) >= 2 else None

    result = {"current_oi": current_oi}

    # 1-day change
    if prev_oi and prev_oi > 0:
        result["oi_change_1d"] = current_oi - prev_oi
        result["oi_change_pct_1d"] = round((current_oi - prev_oi) / prev_oi * 100, 2)

    # MA5 and MA20
    for period, label in [(5, "ma5"), (20, "ma20")]:
        if len(oi_values) >= period:
            ma = sum(oi_values[-period:]) / period
            result[f"oi_{label}"] = round(ma, 2)
            if ma > 0:
                result[f"oi_vs_{label}_pct"] = round((current_oi - ma) / ma * 100, 2)

    return result


def calculate_volume_trend(history):
    """Calculate volume trend metrics from history.

    Returns dict with current volume, ratios vs MA5/MA20.
    Returns None if insufficient data.
    """
    if not history:
        return None

    vol_values = [h.get("volume") for h in history if h.get("volume") is not None]
    if not vol_values:
        return None

    current_vol = vol_values[-1]
    result = {"current_volume": current_vol}

    for period, label in [(5, "ma5"), (20, "ma20")]:
        if len(vol_values) >= period:
            ma = sum(vol_values[-period:]) / period
            result[f"vol_{label}"] = round(ma, 2)
            if ma > 0:
                result[f"vol_vs_{label}_pct"] = round((current_vol - ma) / ma * 100, 2)

    return result


def calculate_basis(futures_close, index_close):
    """Calculate basis (futures price - index price) and signal.

    Returns dict with basis value, percentage, direction, and signal.
    """
    if futures_close is None or index_close is None or index_close == 0:
        return None

    basis = futures_close - index_close
    basis_pct = basis / index_close * 100

    if basis_pct > 2:
        signal = "strong_bullish"
        direction = "大幅升水"
    elif basis_pct > 0.5:
        signal = "slight_bullish"
        direction = "升水"
    elif basis_pct > -0.5:
        signal = "neutral"
        direction = "平水"
    elif basis_pct > -2:
        signal = "slight_bearish"
        direction = "贴水"
    else:
        signal = "strong_bearish"
        direction = "大幅贴水"

    return {
        "futures_close": futures_close,
        "index_close": index_close,
        "basis": round(basis, 2),
        "basis_pct": round(basis_pct, 2),
        "direction": direction,
        "signal": signal,
    }


def derive_signals(basis_data, oi_trend, volume_trend, history):
    """Derive trading signals from futures data.

    Returns dict with individual and composite signal scores.
    """
    # Basis signal: [-1, +1]
    if basis_data and basis_data.get("basis_pct") is not None:
        pct = basis_data["basis_pct"]
        # Extreme values may signal reversal
        if abs(pct) > 2:
            basis_score = 1.0 if pct > 0 else -1.0
        elif abs(pct) > 0.5:
            basis_score = 0.5 if pct > 0 else -0.5
        else:
            basis_score = 0
        basis_signal = basis_data.get("signal", "neutral")
    else:
        basis_score = 0
        basis_signal = "neutral"

    # OI signal: [-1, +1]
    # Price↑ + OI↑ = strong bullish (+1)
    # Price↓ + OI↑ = strong bearish (-1)
    # Price↑ + OI↓ = weak rally/short covering (-0.5)
    # Price↓ + OI↓ = weak decline/long liquidation (+0.5)
    oi_score = 0
    oi_signal = "neutral"
    if oi_trend and len(history) >= 2:
        price_chg = (history[-1].get("close", 0) - history[-2].get("close", 0))
        oi_chg = oi_trend.get("oi_change_1d", 0)
        if oi_chg is not None:
            if price_chg > 0 and oi_chg > 0:
                oi_score = 1.0
                oi_signal = "strong_bullish"
            elif price_chg < 0 and oi_chg > 0:
                oi_score = -1.0
                oi_signal = "strong_bearish"
            elif price_chg > 0 and oi_chg < 0:
                oi_score = -0.5
                oi_signal = "weak_rally"
            elif price_chg < 0 and oi_chg < 0:
                oi_score = 0.5
                oi_signal = "weak_decline"

    # Volume signal: [-0.5, +0.5]
    vol_score = 0
    vol_signal = "neutral"
    if volume_trend and volume_trend.get("vol_vs_ma5_pct") is not None:
        vol_vs_ma5 = volume_trend["vol_vs_ma5_pct"]
        if vol_vs_ma5 > 20:
            vol_score = 0.5
            vol_signal = "confirmed"
        elif vol_vs_ma5 < -20:
            vol_score = -0.5
            vol_signal = "diverging"

    # Composite: average of (basis_score, oi_score, vol_score)
    scores = [s for s in [basis_score, oi_score, vol_score] if s != 0]
    if scores:
        composite_score = round(sum(scores) / len(scores), 2)
    else:
        composite_score = 0
    composite_score = max(-1, min(1, composite_score))

    if composite_score >= 0.5:
        composite_signal = "bullish"
    elif composite_score <= -0.5:
        composite_signal = "bearish"
    else:
        composite_signal = "neutral"

    return {
        "basis_score": basis_score,
        "basis_signal": basis_signal,
        "oi_score": oi_score,
        "oi_signal": oi_signal,
        "volume_score": vol_score,
        "volume_signal": vol_signal,
        "composite_score": composite_score,
        "composite_signal": composite_signal,
    }


def fetch_futures_data(etf_code, days=30, no_cache=False):
    """Fetch index futures data for an ETF.

    Returns dict with meta, futures kline, basis, and signals.
    """
    futures_code, futures_secid = get_futures_secid(etf_code)
    if not futures_code:
        return {
            "meta": {
                "etf_code": etf_code,
                "futures_code": None,
                "futures_secid": None,
                "index_secid": None,
                "data_source": "unsupported",
                "record_count": 0,
                "warnings": [f"ETF {etf_code} has no corresponding index futures mapping"],
            },
            "futures": None,
            "basis": None,
            "signals": None,
            "errors": None,
        }

    index_secid = INDEX_SECID_MAP.get(futures_code)
    warnings = []
    errors = []

    # Check cache
    cache_key = f"futures_data_{etf_code}"
    if not no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached:
            return cached

    # Step 1: Fetch futures K-line
    history = None
    futures_name = ""
    data_source = None

    # Try East Money first
    try:
        (history, futures_name), used_host = rotate_em_host(
            lambda h: _fetch_futures_kline_em(futures_secid, days=days, host=h)
        )
        data_source = "eastmoney"
    except Exception as em_err:
        errors.append(f"EM futures: {em_err}")
        # Try AKShare fallback (A-share futures only)
        if futures_code in ("IF", "IH", "IC", "IM"):
            try:
                history, futures_name = _fetch_futures_kline_akshare(futures_code, days=days)
                data_source = "akshare"
            except Exception as ak_err:
                errors.append(f"AKShare futures: {ak_err}")

    if history is None:
        return {
            "meta": {
                "etf_code": etf_code,
                "futures_code": futures_code,
                "futures_secid": futures_secid,
                "index_secid": index_secid,
                "data_source": "unavailable",
                "record_count": 0,
                "warnings": [f"All futures data sources failed for {futures_code}"],
            },
            "futures": None,
            "basis": None,
            "signals": None,
            "errors": errors if errors else None,
        }

    # Step 2: Fetch spot index close for basis calculation
    index_close_map = {}
    index_data = None

    try:
        if data_source == "eastmoney":
            index_data, _ = rotate_em_host(
                lambda h: _fetch_index_close_em(index_secid, days=days, host=h)
            )
        else:
            index_data = _fetch_index_close_akshare(index_secid, days=days)

        # Build date->close map
        for item in index_data:
            index_close_map[item["date"]] = item["close"]
    except Exception as e:
        warnings.append(f"Index data unavailable for basis: {e}")

    # Step 3: Compute latest data point
    latest = history[-1] if history else None

    # Step 4: Calculate basis
    basis_data = None
    if latest and latest.get("close") is not None:
        idx_close = index_close_map.get(latest["date"])
        basis_data = calculate_basis(latest["close"], idx_close)

    # Step 5: Calculate OI trend
    oi_trend = calculate_oi_trend(history)

    # Step 6: Calculate volume trend
    volume_trend = calculate_volume_trend(history)

    # Step 7: Derive signals
    signals = derive_signals(basis_data, oi_trend, volume_trend, history)

    result = {
        "meta": {
            "etf_code": etf_code,
            "futures_code": futures_code,
            "futures_secid": futures_secid,
            "index_secid": index_secid,
            "data_source": data_source,
            "record_count": len(history),
            "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "warnings": warnings if warnings else None,
        },
        "futures": {
            "name": futures_name,
            "latest": latest,
            "history": history[-5:] if len(history) > 5 else history,
            "oi_trend": oi_trend,
            "volume_trend": volume_trend,
        },
        "basis": basis_data,
        "signals": signals,
        "errors": errors if errors else None,
    }

    # Cache successful result
    if result.get("meta", {}).get("data_source") not in ("error", "unavailable", None):
        save_cache(cache_key, result)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Fetch index futures data for ETF trend analysis"
    )
    parser.add_argument("etf_code", help="ETF code, e.g. 510300, 159740")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--days", type=int, default=30, help="Days of history to fetch (default: 30)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")

    args = parser.parse_args()

    result = fetch_futures_data(args.etf_code, days=args.days, no_cache=args.no_cache)
    output_json(result, output_path=args.output)


if __name__ == "__main__":
    main()