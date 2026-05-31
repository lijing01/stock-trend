#!/usr/bin/env python3
"""Integrated entry point — runs ths-theme → longtou → merge in one pass.

Usage:
    python3 run_integrated.py [--top N] [--compact]

Pipeline:
    1. ths_theme.py --export-sectors
    2. IF qualified_sectors.json non-empty → market_leader.py --sectors-from
    3. integrated_report.py → merge output
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/bridge/
SCRIPTS_DIR = SCRIPT_DIR.parent                      # scripts/
PROJECT_ROOT = SCRIPTS_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
REPORTS_DIR = PROJECT_ROOT / "reports" / "lists"
QUALIFIED_PATH = CACHE_DIR / "qualified_sectors.json"

sys.path.insert(0, str(SCRIPTS_DIR))  # enable "from bridge.* import ..."


def run_step(cmd: list[str], desc: str, timeout: int = 300) -> tuple[int, str]:
    """Run a pipeline step. Returns (returncode, stdout)."""
    print(f"\n{'='*60}")
    print(f"[{desc}]")
    print(f"{'='*60}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        print(proc.stdout[-2000:] if len(proc.stdout) > 2000 else proc.stdout)
        if proc.stderr:
            print(f"  stderr: {proc.stderr[-500:]}", file=sys.stderr)
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ 超时 ({timeout}s)")
        return -1, ""
    except Exception as e:
        print(f"  ⚠️ 失败: {e}")
        return -1, ""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ths-theme + longtou 整合入口")
    parser.add_argument("--top", type=int, default=10, help="显示数量")
    parser.add_argument("--compact", action="store_true", help="精简输出")
    parser.add_argument("--output-html", action="store_true", help="生成 HTML 报告")
    parser.add_argument("--lhb-date", type=str, help="龙虎榜日期 YYYYMMDD")
    parser.add_argument("--zt-date", type=str, help="涨停日期 YYYY-MM-DD")
    args = parser.parse_args()

    start = time.time()
    now = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Step 1: ths-theme
    ths_cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "analysis/ths_theme.py"),
        "--top", str(args.top),
        "--export-sectors",
        "--no-html",
    ]
    if args.lhb_date:
        ths_cmd.extend(["--lhb-date", args.lhb_date])
    if args.zt_date:
        ths_cmd.extend(["--zt-date", args.zt_date])

    rc1, ths_stdout = run_step(ths_cmd, "Step 1/3: 板块热力分析 (ths-theme)")
    ths_report = ths_stdout
    has_qualified = QUALIFIED_PATH.exists()

    # Step 2: longtou (only if qualified sectors available)
    leader_report = ""
    overview_data = {}

    if has_qualified:
        try:
            data = json.loads(QUALIFIED_PATH.read_text(encoding="utf-8"))
            sectors = data.get("sectors", [])
            overview_data = {
                "total_hot_sectors": len(sectors),
                "top_sectors": [s["name"] for s in sectors[:5]],
            }
        except Exception:
            overview_data = {"total_hot_sectors": 0, "top_sectors": []}

        if overview_data.get("total_hot_sectors", 0) > 0:
            leader_cmd = [
                sys.executable,
                str(SCRIPTS_DIR / "scans/market_leader.py"),
                "--sectors-from", str(QUALIFIED_PATH),
                "--top", str(args.top),
            ]
            if args.compact:
                leader_cmd.append("--compact")
            if args.output_html:
                leader_cmd.append("--output-html")

            rc2, leader_stdout = run_step(leader_cmd, "Step 2/3: 龙头扫描 (longtou)")
            leader_report = leader_stdout
        else:
            print("\n⚠️ 无满足双强条件的板块，跳过龙头扫描")
    else:
        print("\n⚠️ 无 qualified_sectors.json，跳过龙头扫描")
        print("  (ths-theme --export-sectors 未产生输出)")

    # Step 3: Integrated report
    print(f"\n{'='*60}")
    print("[Step 3/3] 生成整合报告")
    print(f"{'='*60}")

    try:
        from bridge.integrated_report import (
            build_sector_overview,
            generate_integrated_md,
            generate_integrated_html,
        )

        # Count leaders from leader_report
        leader_count = 0
        if leader_report:
            for line in leader_report.split("\n"):
                stripped = line.strip()
                if stripped.startswith("- ") and "(" in stripped and ")" in stripped:
                    leader_count += 1

        overview = build_sector_overview(
            total_hot=overview_data.get("total_hot_sectors", 0),
            leaders=leader_count,
            dual=overview_data.get("total_hot_sectors", 0),
            lhb=0,
            top=overview_data.get("top_sectors", []),
        )

        md = generate_integrated_md(
            date=now,
            ths_report=ths_report or "",
            leader_report=leader_report or "",
            overview=overview,
        )

        # Output MD
        print(md)

        # HTML
        if args.output_html:
            html = generate_integrated_html(
                date=now,
                ths_report=ths_report or "",
                leader_report=leader_report or "",
                overview=overview,
            )
            path = REPORTS_DIR / f"integrated-{ts}.html"
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            print(f"\nHTML report: {path}")

        # Also output structured JSON for agent consumption
        json_output = json.dumps({
            "meta": {"scan_time": now, "elapsed": round(time.time() - start, 1)},
            "overview": overview,
            "has_leader_report": bool(leader_report),
        }, ensure_ascii=False, indent=2)
        print(f"\n<!--JSON_OUTPUT-->\n{json_output}\n<!--END_JSON_OUTPUT-->")

    except ImportError as e:
        print(f"  ⚠️ 整合报告模块导入失败: {e}")
        print("  输出原始报告:")
        if ths_report:
            print(ths_report)
        if leader_report:
            print(leader_report)

    elapsed = time.time() - start
    print(f"\n整合分析完成，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
