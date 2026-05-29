#!/usr/bin/env python3
"""ETF data fetcher from East Money (东方财富).

Fetches fund-specific data: NAV, IOPV premium, returns, top holdings,
stock position, fund size, and recent subscription/redemption flows.

Usage:
    python3 fetch_etf_data.py <fund_code> [-o output.json]

Examples:
    python3 fetch_etf_data.py 159740
    python3 fetch_etf_data.py 513180 -o /tmp/etf_data.json
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import re
import sys
import urllib.request
from core.cache_utils import safe_float, output_json
from core.eastmoney_utils import EM_HEADERS, fetch_url


def _fetch_fund_url(url, timeout=15):
    """Fetch fund-specific URL with fund.eastmoney.com Referer."""
    headers = {**EM_HEADERS, "Referer": "http://fund.eastmoney.com/"}
    return fetch_url(url, headers=headers, timeout=timeout)


def _parse_js_vars(content):
    """Extract JavaScript variable assignments from eastmoney JS content.

    Returns dict mapping var names to their raw string values.
    """
    result = {}
    for m in re.finditer(r'var\s+(\w+)\s*=\s*(.+?);', content):
        name = m.group(1)
        value = m.group(2).strip()
        # Try to parse as JSON-like value
        if value.startswith('"') or value.startswith("'"):
            result[name] = value.strip('"').strip("'")
        elif value.startswith("[") or value.startswith("{"):
            try:
                result[name] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                result[name] = value
        else:
            result[name] = value
    return result


def fetch_etf_data(fund_code):
    """Fetch comprehensive ETF data from East Money.

    Returns dict with fund info, NAV, IOPV, returns, holdings, etc.
    """
    result = {
        "fund_code": fund_code,
        "data_source": "eastmoney",
    }
    errors = []

    # 1. Fetch pingzhongdata (fund overview + returns + holdings)
    pingzhong_url = f"http://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    try:
        pingzhong_content = _fetch_fund_url(pingzhong_url)
        vars_dict = _parse_js_vars(pingzhong_content)

        # Fund name
        result["fund_name"] = vars_dict.get("fS_name", "")

        # Returns: Data_NetPerfMonitor [period, return_rate]
        returns = {}
        if "Data_NetPerfMonitor" in vars_dict and isinstance(vars_dict["Data_NetPerfMonitor"], list):
            for item in vars_dict["Data_NetPerfMonitor"]:
                if isinstance(item, list) and len(item) >= 2:
                    period = str(item[0])
                    rate = item[1]
                    if "近1月" in period or period == "0":
                        returns["1m"] = safe_float(rate)
                    elif "近3月" in period or period == "1":
                        returns["3m"] = safe_float(rate)
                    elif "近6月" in period or period == "2":
                        returns["6m"] = safe_float(rate)
                    elif "近1年" in period or period == "3":
                        returns["1y"] = safe_float(rate)
        result["returns"] = returns

        # Stock position ratio
        result["stock_position"] = safe_float(vars_dict.get("Data_currentFundPosition", None))

        # Top 10 holdings: Data_holderStructure or stockCodesNew
        holdings = []
        if "Data_holderStructure" in vars_dict:
            try:
                holder_data = vars_dict["Data_holderStructure"]
                if isinstance(holder_data, list):
                    for h in holder_data[:10]:
                        if isinstance(h, (list, tuple)) and len(h) >= 3:
                            holdings.append({
                                "name": str(h[0]),
                                "code": str(h[1]) if len(h) > 1 else "",
                                "weight": safe_float(h[2]) if len(h) > 2 else None,
                            })
            except (TypeError, IndexError):
                pass

        # Alternative: stockCodesNew for stock holdings
        if not holdings and "stockCodesNew" in vars_dict:
            try:
                stock_data = vars_dict["stockCodesNew"]
                if isinstance(stock_data, list):
                    for s in stock_data[:10]:
                        if isinstance(s, (list, tuple)) and len(s) >= 3:
                            holdings.append({
                                "name": str(s[1]),
                                "code": str(s[0]),
                                "weight": safe_float(s[2]),
                            })
            except (TypeError, IndexError):
                pass

        result["top_holdings"] = holdings

        # Fund size: Data_flvol (share) and Data_endNav (net asset)
        fund_size = {}
        if "Data_flvol" in vars_dict and isinstance(vars_dict["Data_flvol"], list) and vars_dict["Data_flvol"]:
            last = vars_dict["Data_flvol"][-1]
            if isinstance(last, list) and len(last) >= 2:
                fund_size["shares_billion"] = safe_float(last[1])
        if "Data_endNav" in vars_dict and isinstance(vars_dict["Data_endNav"], list) and vars_dict["Data_endNav"]:
            last = vars_dict["Data_endNav"][-1]
            if isinstance(last, list) and len(last) >= 2:
                fund_size["net_asset_billion"] = safe_float(last[1])
        result["fund_size"] = fund_size

        # Tracking index
        result["tracking_index"] = vars_dict.get("fS_code", "")

        # Fund type
        result["fund_type"] = vars_dict.get("fS_fundtype", "ETF")

    except Exception as e:
        errors.append(f"pingzhongdata: {e}")

    # 2. Fetch js/{code}.js for latest NAV and IOPV data
    js_url = f"http://fund.eastmoney.com/js/{fund_code}.js"
    try:
        js_content = _fetch_fund_url(js_url)
        # Parse: var jsonOpenOrCloseData = {...}
        nav_match = re.search(r'var\s+\w+\s*=\s*(\{.+?\});', js_content, re.DOTALL)
        if nav_match:
            try:
                nav_data = json.loads(nav_match.group(1))
                result["fund_name"] = result.get("fund_name") or nav_data.get("name", "")
                result["fund_type_name"] = nav_data.get("fundtype", "")
            except (json.JSONDecodeError, ValueError):
                pass
    except Exception:
        pass

    # 3. Fetch latest NAV from API
    nav_url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js"
    try:
        nav_content = _fetch_fund_url(nav_url)
        # Format: jsonpgz({...})
        m = re.search(r'jsonpgz\((.+?)\)', nav_content)
        if m:
            nav_data = json.loads(m.group(1))
            result["nav"] = {
                "nav": safe_float(nav_data.get("dwjz")),
                "nav_date": nav_data.get("jzrq", ""),
                "iopv": safe_float(nav_data.get("gsz")),
                "iopv_time": nav_data.get("gztime", ""),
                "iopv_chg_pct": safe_float(nav_data.get("gszzl")),
            }
            # Calculate IOPV premium/discount
            iopv = safe_float(nav_data.get("gsz"))
            nav = safe_float(nav_data.get("dwjz"))
            if iopv and nav and nav > 0:
                result["nav"]["iopv_premium_pct"] = round((iopv - nav) / nav * 100, 4)
    except Exception as e:
        errors.append(f"nav_api: {e}")

    # 4. Fetch recent subscription/redemption flows from pingzhongdata
    try:
        if "Data_flvol" in vars_dict and isinstance(vars_dict["Data_flvol"], list):
            # Last 5 entries for recent flow
            recent_flows = []
            for item in vars_dict["Data_flvol"][-5:]:
                if isinstance(item, list) and len(item) >= 2:
                    recent_flows.append({
                        "date": str(item[0]),
                        "shares_billion": safe_float(item[1]),
                    })
            result["recent_flows"] = recent_flows
    except Exception:
        pass

    result["errors"] = errors if errors else None
    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch ETF data from East Money")
    parser.add_argument("fund_code", help="Fund code, e.g. 159740, 513180")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")

    args = parser.parse_args()

    result = fetch_etf_data(args.fund_code)
    output_json(result, output_path=args.output)


if __name__ == "__main__":
    main()