#!/usr/bin/env python3
"""Capital flow data fetcher for stock-trend skill.

Fetches money flow, northbound capital, margin trading, and
dragon & tiger list data. Supports both individual stocks and ETFs.

Usage:
    python3 fetch_capital_flow.py <ts_code> [--asset E|FD] [-o output.json]

Examples:
    python3 fetch_capital_flow.py 600519.SH
    python3 fetch_capital_flow.py 159740.SZ --asset FD -o /tmp/capital_flow.json
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
import urllib.request
import logging
from pathlib import Path
from core.cache_utils import CACHE_DIR, load_cache, safe_float, save_cache, get_market_day_ttl, output_json
from datetime import datetime, timedelta
from core.eastmoney_utils import EM_HEADERS, build_secid as resolve_secid

logging.getLogger("akshare").setLevel(logging.ERROR)

CACHE_ROOT = Path(CACHE_DIR)


def fetch_stock_capital_flow_tushare(ts_code, days=5, timeout=10):
    """Fetch capital flow from Tushare moneyflow API as fallback."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as TEO

    def _do_fetch():
        import tushare as ts
        pro = ts.pro_api()
        end_d = datetime.now()
        start_d = end_d - timedelta(days=15)
        code = ts_code.split(".")[0]
        df = pro.moneyflow(
            ts_code=f"{code}.{ts_code.split('.')[1]}",
            start_date=start_d.strftime("%Y%m%d"),
            end_date=end_d.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date")
        df = df.tail(days)
        records = []
        for _, row in df.iterrows():
            main_net = (safe_float(row.get("buy_lg_amount", 0)) +
                        safe_float(row.get("buy_elg_amount", 0)) -
                        safe_float(row.get("sell_lg_amount", 0)) -
                        safe_float(row.get("sell_elg_amount", 0)))
            main_in = (safe_float(row.get("buy_lg_amount", 0)) +
                       safe_float(row.get("buy_elg_amount", 0)))
            main_out = (safe_float(row.get("sell_lg_amount", 0)) +
                        safe_float(row.get("sell_elg_amount", 0)))
            retail_net = (safe_float(row.get("buy_sm_amount", 0)) +
                          safe_float(row.get("buy_md_amount", 0)) -
                          safe_float(row.get("sell_sm_amount", 0)) -
                          safe_float(row.get("sell_md_amount", 0)))
            retail_in = (safe_float(row.get("buy_sm_amount", 0)) +
                         safe_float(row.get("buy_md_amount", 0)))
            retail_out = (safe_float(row.get("sell_sm_amount", 0)) +
                          safe_float(row.get("sell_md_amount", 0)))
            total_net = safe_float(row.get("net_mf_amount", 0))
            records.append({
                "date": str(row.get("trade_date", "")),
                "main_net_inflow": main_net,
                "main_inflow": main_in,
                "main_outflow": main_out,
                "retail_net_inflow": retail_net,
                "retail_inflow": retail_in,
                "retail_outflow": retail_out,
                "total_net_inflow": total_net,
            })
        return records

    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(_do_fetch)
        try:
            return fut.result(timeout=timeout)
        except TEO:
            print(f"Tushare capital flow fallback timed out ({timeout}s)", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Tushare capital flow fallback failed: {e}", file=sys.stderr)
            return None


def estimate_capital_flow_from_kline(code, days=5):
    """Estimate capital flow from cached K-line data when APIs unavailable.

    Uses price position within daily range as a buying/selling proxy.
    Main net inflow ~ amount * ((close-open) / (high-low+eps)) * 0.35
    """
    kline_path = Path(CACHE_ROOT) / code / "kline.json"
    if not kline_path.exists():
        return None
    try:
        with open(kline_path, encoding="utf-8") as f:
            data = json.load(f)
        records = data.get("data", [])
        if not records:
            return None
        records = records[-days:]
        out = []
        for r in records:
            o, h, l, c = r["open"], r["high"], r["low"], r["close"]
            amount = r.get("amount", 0)
            vol = r.get("vol", 0)
            amp = h - l
            if amp < 0.001:
                amp = 0.001
            # Price position ratio: -1 (close=low) to +1 (close=high)
            if c >= o:
                mf_ratio = (c - l) / amp * 2 - 1
            else:
                mf_ratio = (c - l) / amp * 2 - 1
            mf_ratio = max(-1, min(1, mf_ratio))
            total_net_raw = amount * mf_ratio * 0.08
            main_net_raw = total_net_raw * 0.45
            out.append({
                "date": str(r.get("trade_date", "")),
                "main_net_inflow": round(main_net_raw, 2),
                "main_inflow": round(abs(main_net_raw) if main_net_raw > 0 else 0, 2),
                "main_outflow": round(abs(main_net_raw) if main_net_raw < 0 else 0, 2),
                "retail_net_inflow": round(total_net_raw * 0.55, 2),
                "retail_inflow": round(abs(total_net_raw * 0.55) if total_net_raw > 0 else 0, 2),
                "retail_outflow": round(abs(total_net_raw * 0.55) if total_net_raw < 0 else 0, 2),
                "total_net_inflow": round(total_net_raw, 2),
                "estimated": True,
            })
        return out
    except Exception as e:
        print(f"K-line estimation failed: {e}", file=sys.stderr)
        return None


def fetch_stock_capital_flow(secid, days=5):
    """Fetch capital flow data for individual stocks from East Money."""
    from core.eastmoney_utils import rotate_push2_host

    def _do_fetch(host):
        url = (
            f"https://{host}/api/qt/stock/fflow/kline/get"
            f"?secid={secid}"
            f"&fields1=f1,f2,f3,f7"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
            f"&klt=101"
            f"&lmt={days}"
        )
        req = urllib.request.Request(url, headers=EM_HEADERS)
        with urllib.request.urlopen(req, timeout=6) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result or result.get("rc") != 0 or not result.get("data"):
            raise RuntimeError(f"东方财富资金流向API返回错误(host={host})")
        data = result["data"]
        klines = data.get("klines", [])
        records = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 10:
                continue
            try:
                record = {
                    "date": parts[0].replace("-", ""),
                    "main_net_inflow": safe_float(parts[1]),
                    "main_inflow": safe_float(parts[2]),
                    "main_outflow": safe_float(parts[3]),
                    "retail_net_inflow": safe_float(parts[4]),
                    "retail_inflow": safe_float(parts[5]),
                    "retail_outflow": safe_float(parts[6]),
                    "total_net_inflow": safe_float(parts[7]) if len(parts) > 7 else None,
                }
                records.append(record)
            except (ValueError, IndexError):
                continue
        return records
    records, used_host = rotate_push2_host(_do_fetch, max_retries=2)
    return records


def fetch_etf_capital_flow(fund_code, days=5):
    """Fetch ETF subscription/redemption flow from East Money pingzhongdata."""
    import re
    url = f"http://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    req = urllib.request.Request(url, headers={
        "User-Agent": EM_HEADERS["User-Agent"],
        "Referer": "http://fund.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read().decode("utf-8")
    m = re.search(r'var\s+Data_flvol\s*=\s*(\[.+?\]);', content, re.DOTALL)
    if not m:
        return []
    try:
        flvol_data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    records = []
    for item in flvol_data[-days:]:
        if isinstance(item, list) and len(item) >= 2:
            records.append({
                "date": str(item[0]).replace("-", ""),
                "shares_billion": safe_float(item[1]),
                "type": "etf_subscription_redemption",
            })
    return records


def fetch_northbound_flow():
    """Fetch northbound (沪深股通) capital flow history using AKShare."""
    import akshare as ak
    df = ak.stock_hsgt_hist_em("北向资金")
    if df is not None and not df.empty:
        records = []
        for _, row in df.iterrows():
            records.append({
                "date": str(row.iloc[0]).replace("-", ""),
                "net_buy_billion": safe_float(row.get("沪股通净流入") or row.get("深股通净流入")),
            })
        daily = {}
        for r in records:
            d = r["date"]
            if d not in daily:
                daily[d] = 0
            if r["net_buy_billion"]:
                daily[d] += r["net_buy_billion"]
        result = [{"date": d, "net_buy_billion": round(v, 2)} for d, v in sorted(daily.items())]
        return result[-10:]
    return None


def fetch_individual_northbound(code):
    """Fetch individual stock northbound holding data."""
    import akshare as ak
    try:
        df = ak.stock_hsgt_individual_em(code)
        if df is not None and not df.empty:
            latest = df.iloc[-1] if len(df) > 1 else df.iloc[0]
            return {
                "date": str(latest.get("日期", "")),
                "hold_shares": safe_float(latest.get("持股股数")),
                "hold_value_billion": safe_float(latest.get("持股数")),
                "change_shares": safe_float(latest.get("股数变动")),
            }
    except Exception:
        pass
    return None


def fetch_margin_detail(code, exchange="SH"):
    """Fetch margin trading detail for a given stock."""
    import akshare as ak
    today = datetime.now().strftime("%Y%m%d")
    try:
        if exchange == "SH":
            df = ak.stock_margin_detail_sse(date=today)
        else:
            df = ak.stock_margin_detail_szse(date=today)
        if df is not None and not df.empty:
            match = df[df["证券代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                return {
                    "margin_balance_billion": safe_float(row.get("融资余额")),
                    "margin_buy_billion": safe_float(row.get("融资买入额")),
                    "net_margin_billion": safe_float(row.get("融资余额")),
                }
    except Exception:
        pass
    return None


def fetch_longhubang(code, days=5):
    """Fetch dragon & tiger list data for a given stock."""
    import akshare as ak
    end = datetime.now()
    start = end - timedelta(days=20)
    try:
        df = ak.stock_lhb_detail_em(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            match = df[df["代码"] == code]
            if not match.empty:
                records = []
                for _, row in match.iterrows():
                    records.append({
                        "date": str(row.get("日期", "")).replace("-", ""),
                        "reason": row.get("上榜原因", ""),
                        "total_net_buy_billion": safe_float(row.get("龙虎榜净买入额")),
                        "institution_net_buy_billion": safe_float(row.get("机构净买入额")),
                    })
                return records[-days:]
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Fetch capital flow data")
    parser.add_argument("ts_code", help="Tushare-style code, e.g. 600519.SH, 159740.SZ")
    parser.add_argument("--asset", choices=["E", "FD"], help="Asset type (auto-detected if omitted)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--no-cache", action="store_true", help="Force refresh, ignore cache")
    args = parser.parse_args()

    cache_key = f"capital_flow_{args.ts_code}"
    if not args.no_cache:
        cached = load_cache(cache_key, ttl_seconds=get_market_day_ttl())
        if cached:
            output_json(cached, output_path=args.output)
            return

    code = args.ts_code.split(".")[0]
    suffix = "." + args.ts_code.split(".")[1] if "." in args.ts_code else ""
    asset = args.asset or ("FD" if code.startswith(("5", "15")) else "E")

    errors = []
    result = {"meta": {}, "data": [], "data_extended": {}}

    if asset == "FD":
        fund_code = code
        try:
            flows = fetch_etf_capital_flow(fund_code)
            result["meta"] = {
                "ts_code": args.ts_code, "asset": "FD", "fund_code": fund_code,
                "data_source": "eastmoney_etf", "record_count": len(flows),
            }
            result["data"] = flows
        except Exception as e:
            result["meta"] = {
                "ts_code": args.ts_code, "asset": "FD",
                "data_source": "error", "error": f"ETF资金流向获取失败: {e}",
            }
            result["data"] = []
    else:
        secid = resolve_secid(args.ts_code)
        if secid is None:
            result["meta"] = {
                "ts_code": args.ts_code, "data_source": "error",
                "error": f"不支持的市场代码: {args.ts_code}",
            }
            result["data"] = []
        else:
            data_ok = False
            em_err = ""
            ts_err = ""

            # Try 1: East Money
            try:
                flows = fetch_stock_capital_flow(secid)
                result["meta"] = {
                    "ts_code": args.ts_code, "asset": "E", "secid": secid,
                    "data_source": "eastmoney", "record_count": len(flows),
                }
                result["data"] = flows
                data_ok = True
            except Exception as e:
                em_err = str(e)

            # Try 2: Tushare fallback for A-shares
            if not data_ok and suffix in (".SH", ".SZ"):
                print(f"Trying Tushare fallback for {args.ts_code}", file=sys.stderr)
                ts_flows = fetch_stock_capital_flow_tushare(args.ts_code)
                if ts_flows:
                    result["meta"] = {
                        "ts_code": args.ts_code, "asset": "E",
                        "data_source": "tushare_fallback", "record_count": len(ts_flows),
                    }
                    result["data"] = ts_flows
                    data_ok = True
                else:
                    ts_err = "Tushare不可用"

            # Try 3: K-line estimation for A-shares
            if not data_ok and suffix in (".SH", ".SZ"):
                print(f"Trying K-line estimation for {args.ts_code}", file=sys.stderr)
                est_flows = estimate_capital_flow_from_kline(code)
                if est_flows:
                    result["meta"] = {
                        "ts_code": args.ts_code, "asset": "E",
                        "data_source": "kline_estimate", "record_count": len(est_flows),
                    }
                    result["data"] = est_flows
                    data_ok = True

            if not data_ok:
                err_msg = em_err or "未知错误"
                if ts_err:
                    err_msg += f"; {ts_err}"
                result["meta"] = {
                    "ts_code": args.ts_code, "asset": "E",
                    "data_source": "error", "error": f"资金流向获取失败: {err_msg}",
                }
                result["data"] = []

    is_hk = suffix == ".HK" or args.ts_code.endswith(".HK")
    if asset == "E" and not is_hk:
        try:
            nb_market = fetch_northbound_flow()
            if nb_market:
                result["data_extended"]["northbound_market"] = nb_market
        except Exception as e:
            errors.append(f"北向资金: {e}")
        try:
            nb_individual = fetch_individual_northbound(code)
            if nb_individual:
                result["data_extended"]["northbound_individual"] = nb_individual
        except Exception as e:
            errors.append(f"个股北向: {e}")
        try:
            exchange = "SH" if suffix == ".SH" else "SZ"
            margin = fetch_margin_detail(code, exchange)
            if margin:
                result["data_extended"]["margin"] = margin
        except Exception as e:
            errors.append(f"融资融券: {e}")
        try:
            lhb = fetch_longhubang(code)
            if lhb:
                result["data_extended"]["longhubang"] = lhb
        except Exception as e:
            errors.append(f"龙虎榜: {e}")

    # Compute individual stock capital flow streak (consecutive net inflow days)
    if asset == "E" and result.get("data"):
        main_streak = 0
        total_streak = 0
        for record in result["data"]:
            if (record.get("main_net_inflow") or 0) > 0:
                main_streak += 1
            else:
                break
        for record in result["data"]:
            if (record.get("total_net_inflow") or 0) > 0:
                total_streak += 1
            else:
                break
        result["data_extended"]["individual_streak"] = {
            "main_streak": main_streak,
            "total_streak": total_streak,
        }

    if errors:
        result["warnings"] = errors

    if result.get("meta", {}).get("data_source") not in ("error", None):
        save_cache(cache_key, result)

    output_json(result, output_path=args.output)


if __name__ == "__main__":
    main()
