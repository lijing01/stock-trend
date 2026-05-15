#!/usr/bin/env python3
"""Macro-economic snapshot fetcher for stock-trend skill.

Fetches key macro indicators: exchange rates, interest rates, PMI, CPI, M2,
reserve ratio, and major index data using AKShare.

Usage:
    python3 fetch_macro_snapshot.py [-o output.json] [--focus rate forex index policy]
"""

import argparse
import json
import os
import sys
import logging
from datetime import datetime

logging.getLogger("akshare").setLevel(logging.ERROR)


def _safe_float(val):
    if val is None:
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


def _safe_float_str(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    s = str(val).replace("%", "").replace(",", "").strip()
    try:
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def _get_latest_row(df, date_col="月份"):
    """Get most recent row from a date-indexed dataframe, sorted by date column."""
    if df is None or df.empty:
        return None
    if date_col in df.columns:
        try:
            df_sorted = df.sort_values(by=date_col, ascending=False)
            return df_sorted.iloc[0]
        except Exception:
            pass
    return df.iloc[-1]


def _try_fetch(fetch_fn, key, result_dict, errors):
    try:
        val = fetch_fn()
        if val is not None:
            result_dict[key] = val
    except Exception as e:
        errors.append(f"{key}: {e}")


def fetch_usd_cny():
    """Fetch USD/CNY exchange rate from Bank of China."""
    import akshare as ak
    df = ak.currency_boc_sina("美元")
    if df is not None and not df.empty:
        latest = df.iloc[-1]
        rate = _safe_float_str(latest.get("中行钞卖价") or latest.get("中行折算价"))
        prev = _safe_float_str(latest.get("中行折算价"))
        if rate and prev and prev != 0:
            return {"rate": rate, "change_pct": round((rate - prev) / prev * 100, 2)}
    return None


def fetch_china_10y_yield():
    """Fetch China 10-year government bond yield."""
    import akshare as ak
    df = ak.bond_zh_us_rate()
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            name = str(row.iloc[0])
            if "中国" in name and "10" in name:
                return _safe_float_str(row.iloc[1])
    return None


def fetch_us_10y_yield():
    """Fetch US 10-year Treasury yield."""
    import akshare as ak
    df = ak.bond_zh_us_rate()
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            name = str(row.iloc[0])
            if "美国" in name and "10" in name:
                return _safe_float_str(row.iloc[1])
    return None


def fetch_shibor():
    """Fetch SHIBOR rates."""
    import akshare as ak
    df = ak.macro_china_shibor_all()
    latest = _get_latest_row(df)
    if latest is not None:
        return {
            "on": _safe_float_str(latest.get("ON")),
            "1w": _safe_float_str(latest.get("1W")),
            "1m": _safe_float_str(latest.get("1M")),
        }
    return None


def fetch_lpr():
    """Fetch LPR rates."""
    import akshare as ak
    df = ak.macro_china_lpr()
    latest = _get_latest_row(df, date_col="日期")
    if latest is not None:
        return {
            "1y": _safe_float_str(latest.get("LPR1Y") or latest.iloc[0]),
            "5y": _safe_float_str(latest.get("LPR5Y") or latest.iloc[1]),
        }
    return None


def fetch_pmi():
    """Fetch PMI data — most recent first."""
    import akshare as ak
    df = ak.macro_china_pmi()
    latest = _get_latest_row(df)
    if latest is not None:
        val = latest.get("制造业-指数") or latest.get("制造业PMI")
        return _safe_float_str(val)
    return None


def fetch_cpi():
    """Fetch CPI YoY data — most recent first."""
    import akshare as ak
    df = ak.macro_china_cpi()
    latest = _get_latest_row(df)
    if latest is not None:
        val = latest.get("全国-同比增长") or latest.get("当月同比")
        if val is not None:
            raw = _safe_float_str(val)
            if raw is not None and raw > 50:
                raw = round(raw - 100, 1)
            return raw
    return None


def fetch_m2():
    """Fetch M2 money supply YoY growth rate."""
    import akshare as ak
    df = ak.macro_china_money_supply()
    latest = _get_latest_row(df)
    if latest is not None:
        return _safe_float_str(latest.get("M2同比") or latest.iloc[-1])
    return None


def fetch_reserve_ratio():
    """Fetch reserve requirement ratio."""
    import akshare as ak
    df = ak.macro_china_reserve_requirement_ratio()
    latest = _get_latest_row(df)
    if latest is not None:
        val = latest.get("大型金融机构") or latest.iloc[-1]
        # Filter out NaN
        try:
            f = float(val) if not isinstance(val, (int, float)) else val
            if not (f != f):  # not NaN
                return round(f, 2)
        except (ValueError, TypeError):
            pass
    return None


def fetch_hs300():
    """Fetch HS300 index snapshot."""
    import akshare as ak
    df = ak.index_zh_a_hist(symbol="000300", period="daily")
    if df is not None and not df.empty:
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else None
        close = _safe_float(latest.get("收盘"))
        change_pct = None
        if prev is not None:
            prev_close = _safe_float(prev.get("收盘"))
            if close and prev_close:
                change_pct = round((close - prev_close) / prev_close * 100, 2)
        return {"close": close, "change_pct": change_pct}
    return None


def main():
    parser = argparse.ArgumentParser(description="Fetch macro-economic snapshot")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--focus", nargs="*", choices=["rate", "forex", "index", "policy"],
                        help="Focus areas to fetch (default: all)")
    args = parser.parse_args()

    summary = {}
    errors = []

    _try_fetch(lambda: {"usd_cny": fetch_usd_cny()}, "usd_cny", summary, errors)
    _try_fetch(fetch_china_10y_yield, "china_10y_yield", summary, errors)
    _try_fetch(fetch_us_10y_yield, "us_10y_yield", summary, errors)
    _try_fetch(fetch_shibor, "shibor", summary, errors)
    _try_fetch(fetch_lpr, "lpr", summary, errors)
    _try_fetch(fetch_pmi, "pmi", summary, errors)
    _try_fetch(fetch_cpi, "cpi_yoy", summary, errors)
    _try_fetch(fetch_m2, "m2_growth_pct", summary, errors)
    _try_fetch(fetch_reserve_ratio, "reserve_ratio", summary, errors)
    _try_fetch(fetch_hs300, "hs300", summary, errors)

    filled = sum(1 for v in summary.values() if v is not None)
    if filled >= 6:
        data_quality = "good"
    elif filled >= 3:
        data_quality = "partial"
    else:
        data_quality = "error"

    result = {
        "meta": {
            "data_source": "akshare",
            "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
        },
        "summary": {
            "data_quality": data_quality,
            **{k: v for k, v in summary.items() if v is not None},
        },
        "data": summary,
        "errors": errors,
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Macro snapshot written to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
