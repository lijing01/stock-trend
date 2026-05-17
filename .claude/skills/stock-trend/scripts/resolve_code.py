#!/usr/bin/env python3
"""Resolve stock/ETF code from name or code input.

Supports:
- 6-digit A-share codes (e.g. 600519, 000001, 513180)
- 5-digit HK codes (e.g. 00700)
- Codes with suffix (e.g. 600519.SH, 00700.HK)
- Chinese names (e.g. 恒生科技ETF大成, 贵州茅台)

Usage:
    python3 resolve_code.py <name_or_code> [-o output.json]

Examples:
    python3 resolve_code.py 513180
    python3 resolve_code.py 00700.HK
    python3 resolve_code.py 恒生科技ETF大成
    python3 resolve_code.py 贵州茅台 -o /tmp/resolve.json
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.parse

# --- Built-in ETF/stock name mapping (common ones) ---

COMMON_ETFS = {
    # 恒生科技系列
    "恒生科技ETF华夏": "513180.SH",
    "恒生科技ETF华泰柏瑞": "513130.SH",
    "恒生科技ETF大成": "159740.SZ",
    "恒生科技ETF嘉实": "159741.SZ",
    "恒生科技ETF博时": "159742.SZ",
    "恒生科技ETF易方达": "513010.SH",
    "恒生科技ETF天弘": "520920.SH",
    # 中概/互联网
    "中概互联网ETF易方达": "513050.SH",
    "恒生互联网ETF华夏": "513330.SH",
    # 宽基
    "沪深300ETF华泰柏瑞": "510300.SH",
    "沪深300ETF易方达": "510310.SH",
    "中证500ETF": "510500.SH",
    "创业板ETF": "159915.SZ",
    "科创50ETF": "588000.SH",
    # 医药
    "恒生医疗ETF博时": "513060.SH",
    # 消费
    "消费ETF": "159928.SZ",
}

COMMON_STOCKS = {
    "贵州茅台": "600519.SH",
    "中国平安": "601318.SH",
    "招商银行": "600036.SH",
    "宁德时代": "300750.SZ",
    "比亚迪": "002594.SZ",
    "腾讯控股": "00700.HK",
    "阿里巴巴": "09988.HK",
    "美团": "03690.HK",
    "小米集团": "01810.HK",
}

# Merge all built-in mappings
BUILTIN_MAP = {**COMMON_ETFS, **COMMON_STOCKS}

# Aliases: short names and common variations
ALIASES = {
    "恒生科技": "恒生科技ETF华夏",
    "恒生科技ETF": "恒生科技ETF华夏",
    "中概互联": "中概互联网ETF易方达",
    "沪深300": "沪深300ETF华泰柏瑞",
    "茅台": "贵州茅台",
    "腾讯": "腾讯控股",
    "阿里": "阿里巴巴",
    "美团-W": "美团",
    "小米": "小米集团",
}


def resolve_suffix(code):
    """Determine market suffix from code pattern."""
    # 5-digit codes are HK
    if len(code) == 5 and code.isdigit():
        return ".HK"
    # 6-digit codes
    if len(code) == 6 and code.isdigit():
        if code.startswith(("6", "68", "5")):
            return ".SH"
        elif code.startswith(("0", "3", "1")):
            return ".SZ"
    return None


def resolve_asset(ts_code):
    """Determine asset type from ts_code."""
    code = ts_code.split(".")[0]
    if code.startswith(("5", "15")):
        return "FD"
    return "E"


def resolve_adj(ts_code):
    """Determine adjustment type from ts_code."""
    if ts_code.endswith(".HK"):
        return "none"
    return "qfq"


def resolve_market(ts_code):
    """Determine market from ts_code."""
    if ts_code.endswith(".SH"):
        return "上交所"
    elif ts_code.endswith(".SZ"):
        return "深交所"
    elif ts_code.endswith(".HK"):
        return "港股"
    return "未知"


def search_eastmoney(name):
    """Search stock/ETF name via East Money API.

    Returns list of dicts with code, name, market info.
    """
    encoded = urllib.parse.quote(name)
    url = (
        f"https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={encoded}&type=12"
        f"&token=D43BF722C8E33BDC906FB84D85E326E8"
        f"&count=10"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://fund.eastmoney.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
            data = json.loads(text)
            results = []
            for item in data.get("Data", []) or []:
                code = item.get("Code", "")
                name = item.get("Name", "")
                mkt_num = item.get("MktNum", "")
                # Determine market suffix
                if mkt_num == "1":
                    suffix = ".SH"
                elif mkt_num == "0":
                    suffix = ".SZ"
                elif mkt_num == "116":
                    suffix = ".HK"
                else:
                    suffix = ""
                ts_code = f"{code}{suffix}" if suffix else code
                results.append({
                    "ts_code": ts_code,
                    "name": name,
                    "code": code,
                    "market_num": mkt_num,
                })
            return results
    except Exception:
        return []


def resolve_code(input_str):
    """Resolve stock/ETF code from name or code string.

    Returns dict with ts_code, asset, adj, market, name, code.
    """
    input_str = input_str.strip()

    # 1. Check if already a valid ts_code (with suffix)
    if re.match(r'^\d{5,6}\.(SH|SZ|HK)$', input_str):
        code = input_str.split(".")[0]
        return {
            "ts_code": input_str,
            "code": code,
            "asset": resolve_asset(input_str),
            "adj": resolve_adj(input_str),
            "market": resolve_market(input_str),
            "name": "",
            "source": "direct_input",
        }

    # 2. Check if numeric code without suffix
    if re.match(r'^\d{5,6}$', input_str):
        suffix = resolve_suffix(input_str)
        if suffix is None:
            return {"error": f"无法识别代码格式: {input_str}"}
        ts_code = f"{input_str}{suffix}"
        return {
            "ts_code": ts_code,
            "code": input_str,
            "asset": resolve_asset(ts_code),
            "adj": resolve_adj(ts_code),
            "market": resolve_market(ts_code),
            "name": "",
            "source": "code_auto_detect",
        }

    # 3. Check built-in name mapping (exact match)
    if input_str in BUILTIN_MAP:
        ts_code = BUILTIN_MAP[input_str]
        code = ts_code.split(".")[0]
        return {
            "ts_code": ts_code,
            "code": code,
            "asset": resolve_asset(ts_code),
            "adj": resolve_adj(ts_code),
            "market": resolve_market(ts_code),
            "name": input_str,
            "source": "builtin_map",
        }

    # 4. Check aliases
    if input_str in ALIASES:
        resolved_name = ALIASES[input_str]
        if resolved_name in BUILTIN_MAP:
            ts_code = BUILTIN_MAP[resolved_name]
            code = ts_code.split(".")[0]
            return {
                "ts_code": ts_code,
                "code": code,
                "asset": resolve_asset(ts_code),
                "adj": resolve_adj(ts_code),
                "market": resolve_market(ts_code),
                "name": resolved_name,
                "source": "alias_map",
            }

    # 5. Fuzzy match on built-in names
    for bname, bcode in BUILTIN_MAP.items():
        if input_str in bname or bname in input_str:
            code = bcode.split(".")[0]
            return {
                "ts_code": bcode,
                "code": code,
                "asset": resolve_asset(bcode),
                "adj": resolve_adj(bcode),
                "market": resolve_market(bcode),
                "name": bname,
                "source": "fuzzy_match",
            }

    # 6. Search East Money API
    results = search_eastmoney(input_str)
    if results:
        best = results[0]
        ts_code = best["ts_code"]
        code = best["code"]
        return {
            "ts_code": ts_code,
            "code": code,
            "asset": resolve_asset(ts_code),
            "adj": resolve_adj(ts_code),
            "market": resolve_market(ts_code),
            "name": best["name"],
            "source": "eastmoney_search",
            "alternatives": results[1:5],
        }

    return {"error": f"未找到标的: {input_str}"}


# ── Import-friendly aliases ────────────────────────────────────────

detect_asset = resolve_asset
detect_adj = resolve_adj


def code_to_ts_code(code: str) -> str:
    """Convert raw code to ts_code (with .SH/.SZ/.HK suffix)."""
    code = str(code).strip()
    if code.endswith((".SH", ".SZ", ".HK")):
        return code
    suffix = resolve_suffix(code)
    if suffix:
        return f"{code}{suffix}"
    return code


def main():
    parser = argparse.ArgumentParser(
        description="Resolve stock/ETF code from name or code input"
    )
    parser.add_argument("input", help="Stock/ETF code or name (e.g. 513180, 恒生科技ETF大成)")
    parser.add_argument("-o", "--output", help="Output JSON file path")
    args = parser.parse_args()

    result = resolve_code(args.input)

    # Add futures mapping for ETFs
    if "error" not in result and result.get("asset") == "FD":
        from eastmoney_utils import get_futures_secid
        futures_code, futures_secid = get_futures_secid(result.get("code", ""))
        result["futures_code"] = futures_code
        result["futures_secid"] = futures_secid

    if "error" in result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    output = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        from pathlib import Path
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Code resolved: {args.input} -> {result['ts_code']}")
    else:
        print(output)


if __name__ == "__main__":
    main()