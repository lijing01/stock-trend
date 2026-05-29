#!/usr/bin/env python3
"""BK index K-line fetcher for sector/market-theme analysis.

Fetches historical daily K-line data for sector indices (BKxxxx)
from East Money API. Used by analyze_market_theme.py.

Usage:
    python3 fetch_sector_kline.py BK0477 [--days 20]
    python3 fetch_sector_kline.py BK0477 BK0478 BK0479 --days 10 -o /tmp/sector_kline.json
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from eastmoney_utils import EM_HEADERS, EM_PUSH2_HOSTS, fetch_url, build_em_kline_url, parse_em_kline_line

SCRIPT_DIR = Path(__file__).resolve().parent


def _parse_kline_response(raw: str, min_records: int) -> list[dict]:
    result = json.loads(raw)
    if not result or result.get("rc") != 0 or not result.get("data"):
        msg = result.get("message", "未知错误") if result else "无响应"
        raise RuntimeError(f"API返回错误: {msg}")

    klines = result["data"].get("klines", [])
    if not klines:
        raise RuntimeError("未获取到数据")

    records = []
    for line in klines:
        record = parse_em_kline_line(line)
        if record:
            records.append(record)

    records.sort(key=lambda x: x["trade_date"])
    return records[-min_records:]


def fetch_single_kline(sector_code: str, min_records: int = 20,
                       retries: int = 3) -> list[dict]:
    """Fetch BK index K-line data for one sector.

    Requests 6 months of data (server may return ~28 sparse records).
    Returns last min_records sorted ascending.

    Args:
        sector_code: e.g. "BK0477".
        min_records: minimum records to return (default 20).
        retries: retry count with host rotation.

    Returns:
        List of daily records sorted by date ascending.

    Raises:
        RuntimeError: all hosts exhausted.
    """
    import time as _time
    secid = f"90.{sector_code}"
    last_error = None

    for attempt in range(retries + 1):
        host = EM_PUSH2_HOSTS[attempt % len(EM_PUSH2_HOSTS)]
        url = build_em_kline_url(host, secid, lmt=200)
        try:
            raw = fetch_url(url, headers=EM_HEADERS)
            return _parse_kline_response(raw, min_records)
        except Exception as e:
            last_error = e
            if attempt < retries:
                _time.sleep(0.5)

    raise RuntimeError(f"获取板块[{sector_code}]K线失败: {last_error or '所有节点无响应'}")


def batch_fetch_kline(sector_codes: list[str], min_records: int = 20,
                      max_workers: int = 4) -> dict[str, list[dict]]:
    """Fetch BK index K-lines for multiple sectors in parallel.

    Failed sectors get empty list (no hard failure).
    """
    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(fetch_single_kline, code, min_records): code
            for code in sector_codes
        }
        for fut in as_completed(fut_map):
            code = fut_map[fut]
            try:
                results[code] = fut.result()
            except Exception as e:
                print(f"  [Warn] {code}: {e}", file=sys.stderr)
                results[code] = []
    return results


def main():
    parser = argparse.ArgumentParser(description="BK板块指数K线获取")
    parser.add_argument("codes", nargs="+", help="BK代码, 如 BK0477 BK0478")
    parser.add_argument("--records", type=int, default=20, help="返回记录数, 默认20")
    parser.add_argument("--workers", type=int, default=4, help="并行数, 默认4")
    parser.add_argument("-o", "--output", type=str, help="输出JSON文件")
    args = parser.parse_args()

    results = batch_fetch_kline(args.codes, min_records=args.records, max_workers=args.workers)

    output = {
        "meta": {
            "fetched_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "records": args.records,
            "total_sectors": len(args.codes),
            "success": sum(1 for v in results.values() if v),
            "failed": sum(1 for v in results.values() if not v),
        },
        "data": results,
    }

    out_str = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(out_str, encoding="utf-8")
        print(f"Output: {args.output}")
    else:
        print(out_str)


if __name__ == "__main__":
    main()
