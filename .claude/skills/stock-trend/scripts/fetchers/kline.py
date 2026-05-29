#!/usr/bin/env python3
"""Tushare Pro API K-line data fetcher for stock-trend skill.

Usage:
    python3 fetch_kline.py <ts_code> [options]

Examples:
    python3 fetch_kline.py 600519.SH
    python3 fetch_kline.py 513180.SH --asset FD --freq D -o /tmp/kline.json
    python3 fetch_kline.py 00700.HK --adj none
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from core.cache_utils import load_cache, output_json, save_cache, get_market_day_ttl
from core.resolve_code import detect_asset, detect_adj

# --- Token resolution ---


def resolve_token(cli_token=None):
    """Resolve Tushare token from CLI arg > env var > config file."""
    if cli_token:
        return cli_token

    env_token = os.environ.get("TUSHARE_TOKEN")
    if env_token:
        return env_token

    config_paths = [
        Path(".claude/tushare-config.json"),
        Path.home() / ".claude" / "tushare-config.json",
    ]
    for cp in config_paths:
        if cp.exists():
            try:
                with open(cp) as f:
                    cfg = json.load(f)
                if cfg.get("token"):
                    return cfg["token"]
            except (json.JSONDecodeError, OSError):
                continue

    return None


# --- Auto-detect asset type ---


def calc_start_date(end_date, freq):
    """Calculate start_date based on frequency."""
    end = datetime.strptime(end_date, "%Y%m%d")
    if freq == "W":
        start = end - timedelta(days=365)
    else:
        start = end - timedelta(days=180)
    return start.strftime("%Y%m%d")


# --- SDK fetch ---


def fetch_via_sdk(token, ts_code, asset, freq, adj, start_date, end_date, ma):
    """Fetch K-line data using tushare SDK."""
    import tushare as ts

    ts.set_token(token)

    kwargs = {
        "ts_code": ts_code,
        "asset": asset,
        "freq": freq,
        "start_date": start_date,
        "end_date": end_date,
    }
    if adj and adj != "none":
        kwargs["adj"] = adj
    if ma:
        kwargs["ma"] = ma

    df = ts.pro_bar(**kwargs)
    return df


# --- HTTP API fetch ---


def fetch_via_http(token, ts_code, asset, freq, start_date, end_date):
    """Fetch K-line data using Tushare HTTP API."""
    import urllib.request

    url = "https://api.tushare.pro"

    api_name = "daily"

    payload = {
        "api_name": api_name,
        "token": token,
        "params": {
            "ts_code": ts_code,
            "start_date": start_date,
            "end_date": end_date,
        },
        "fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_chg",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    response = urllib.request.urlopen(req, timeout=15)
    result = json.loads(response.read().decode())

    if result.get("code") != 0 or not result.get("data"):
        error_msg = result.get("msg", "Unknown HTTP API error")
        # Detect permission-related errors for auto-fallback
        permission_keywords = ["权限", "没有权限", "permission", "每分钟", "访问频次", "积分不足", "不够权限"]
        if any(kw in error_msg for kw in permission_keywords):
            raise PermissionError(f"Tushare权限不足: {error_msg}")
        raise RuntimeError(f"Tushare HTTP API error: {error_msg}")

    fields = result["data"]["fields"]
    items = result["data"]["items"]

    import pandas as pd

    df = pd.DataFrame(items, columns=fields)
    for col in ["open", "high", "low", "close", "pre_close", "vol", "amount", "pct_chg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# --- Fetch with retry and fallback ---


def fetch_kline(token, ts_code, asset, freq, adj, start_date, end_date, ma):
    """Fetch K-line data with SDK -> HTTP fallback and retry logic."""
    source = "tushare_sdk"
    df = None
    error_type = None

    # Try SDK first
    try:
        df = fetch_via_sdk(token, ts_code, asset, freq, adj, start_date, end_date, ma)
    except ImportError:
        source = "tushare_http"
    except PermissionError as e:
        # Permission error: skip retry, go directly to fallback
        source = "tushare_http"
        error_type = "permission"
    except Exception:
        # Retry once after 2s
        time.sleep(2)
        try:
            df = fetch_via_sdk(token, ts_code, asset, freq, adj, start_date, end_date, ma)
        except PermissionError:
            source = "tushare_http"
            error_type = "permission"
        except Exception:
            source = "tushare_http"

    # Fallback to HTTP API
    if df is None and source == "tushare_http":
        try:
            df = fetch_via_http(token, ts_code, asset, freq, start_date, end_date)
        except PermissionError:
            # Permission error on HTTP too: skip retry
            return None, "permission"
        except Exception:
            # Retry once after 2s
            time.sleep(2)
            try:
                df = fetch_via_http(token, ts_code, asset, freq, start_date, end_date)
            except PermissionError:
                return None, "permission"
            except Exception as e:
                return None, f"SDK和HTTP API均请求失败: {e}"

    if df is None:
        return None, "SDK请求失败，HTTP API降级也失败"

    if df.empty:
        return None, f"未获取到 {ts_code} 的数据（可能已停牌或代码无效）"

    return df, source


# --- Data processing ---


def df_to_records(df, compact=False):
    """Convert DataFrame to list of records."""
    df = df.sort_values("trade_date").reset_index(drop=True)

    # Normalize column names
    col_map = {"vol": "vol", "amount": "amount", "pct_chg": "pct_chg"}
    for orig, target in col_map.items():
        if orig in df.columns and target not in df.columns:
            df = df.rename(columns={orig: target})

    # Select output columns
    base_cols = ["trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
    ma_cols = [c for c in df.columns if c.startswith("ma")]
    extra_cols = ["pre_close"]

    if compact:
        out_cols = [c for c in base_cols + ma_cols if c in df.columns]
    else:
        out_cols = [c for c in base_cols + extra_cols + ma_cols if c in df.columns]

    records = []
    for _, row in df[out_cols].iterrows():
        record = {}
        for col in out_cols:
            val = row[col]
            if hasattr(val, "item"):
                val = val.item()
            if isinstance(val, float) and (val != val):  # NaN check
                continue
            record[col] = val
        records.append(record)

    return records, df


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="Fetch K-line data from Tushare Pro API")
    parser.add_argument("ts_code", help="Tushare code, e.g. 600519.SH, 513180.SH, 00700.HK")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("--freq", choices=["D", "W"], default="D", help="Frequency: D=daily, W=weekly")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], help="Adjustment type (auto-detected if omitted)")
    parser.add_argument("--start-date", help="Start date YYYYMMDD (auto-calculated if omitted)")
    parser.add_argument("--end-date", help="End date YYYYMMDD (default: today)")
    parser.add_argument("--ma", default="5,10,20,60", help="MA periods, comma-separated (default: 5,10,20,60)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--compact", action="store_true", help="Compact output with fewer columns")
    parser.add_argument("--token", help="Tushare token (overrides env var and config file)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")

    args = parser.parse_args()

    # Check cache before fetching
    adj = args.adj or ("none" if args.ts_code.endswith(".HK") else "qfq")
    cache_key = f"kline_{args.ts_code}_{args.freq}_{adj}"
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached:
            output_json(cached, output_path=args.output)
            return

    # Resolve token
    token = resolve_token(args.token)
    if not token:
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "data_source": "error",
                "error": (
                    "未配置 Tushare Token。请通过以下方式之一配置：\n"
                    "1. 设置环境变量: export TUSHARE_TOKEN=your_token\n"
                    "2. 创建配置文件: .claude/tushare-config.json，内容为 {\"token\": \"your_token\"}\n"
                    "3. 传入 --token 参数"
                ),
            },
            "data": [],
        }
        output_json(result, output_path=args.output)
        return

    # Resolve parameters
    asset = args.asset or detect_asset(args.ts_code)
    adj = args.adj or detect_adj(args.ts_code)
    end_date = args.end_date or datetime.now().strftime("%Y%m%d")
    start_date = args.start_date or calc_start_date(end_date, args.freq)
    ma_periods = [int(x) for x in args.ma.split(",") if x.strip()]

    # Fetch data
    df, source_or_error = fetch_kline(
        token, args.ts_code, asset, args.freq, adj, start_date, end_date, ma_periods
    )

    if df is None:
        result = {
            "meta": {
                "ts_code": args.ts_code,
                "asset": asset,
                "freq": args.freq,
                "data_source": "error",
                "error": source_or_error,
                "error_type": "permission" if source_or_error == "permission" else None,
            },
            "data": [],
        }
        output_json(result, output_path=args.output)
        return

    # Process data
    records, sorted_df = df_to_records(df, compact=args.compact)
    record_count = len(records)
    warnings = []

    if record_count < 60:
        warnings.append(f"数据记录不足60条（仅{record_count}条），部分指标可能无法准确计算")

    # Check for NaN MA values
    ma_cols = [c for c in sorted_df.columns if c.startswith("ma")]
    if ma_cols and record_count > 0:
        last_row = sorted_df.iloc[-1]
        missing_mas = [c for c in ma_cols if pd.isna(last_row.get(c))]
        if missing_mas:
            warnings.append(f"最新数据缺少均线值: {', '.join(missing_mas)}")

    result = {
        "meta": {
            "ts_code": args.ts_code,
            "asset": asset,
            "freq": args.freq,
            "adj": adj,
            "start_date": start_date,
            "end_date": end_date,
            "ma_periods": ma_periods,
            "record_count": record_count,
            "data_source": source_or_error if isinstance(source_or_error, str) and source_or_error.startswith("tushare") else "tushare_sdk",
            "warnings": warnings,
        },
        "data": records,
    }

    # Fix data_source for HTTP fallback
    if isinstance(source_or_error, str) and source_or_error == "tushare_http":
        result["meta"]["data_source"] = "tushare_http"

    # Cache successful result (only if data_source is not error)
    if result.get("meta", {}).get("data_source") != "error":
        save_cache(cache_key, result)

    output_json(result, output_path=args.output)



# Lazy import for pandas (used in df_to_records and NaN check)
try:
    import pandas as pd
except ImportError:
    pd = None

if __name__ == "__main__":
    main()