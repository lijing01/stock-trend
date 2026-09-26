#!/usr/bin/env python3
"""Stock Trend Skill diagnostic script.

Checks data source availability, Python dependencies, and configuration.
Full results are cached in .cache/stock-trend/diag.json with 1-hour TTL.
Quick checks always inspect the current environment and skip API calls.

Usage:
    python3 diagnose.py              # Full diagnostic
    python3 diagnose.py --quick      # Quick check (skip API calls)
    python3 diagnose.py -o path.json # Save to custom path
"""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"
CACHE_PATH = str(CACHE_DIR / "diag.json")
CACHE_TTL = 3600  # 1 hour

# Shared temp dir for diagnostic subprocess outputs
_DIAG_TMP_DIR = None


def _diag_tmp(name):
    """Return temp file path for a diagnostic check."""
    global _DIAG_TMP_DIR
    if _DIAG_TMP_DIR is None:
        _DIAG_TMP_DIR = tempfile.mkdtemp(prefix="stock-trend-diag-")
    return os.path.join(_DIAG_TMP_DIR, f"{name}.json")


def check_environment():
    """Read the dependency contract from pyproject; never contact data sources."""
    checks = {}
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from importlib.metadata import PackageNotFoundError, version
        project = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        required = project["requires-python"]
        current = ".".join(map(str, sys.version_info[:3]))
        checks["python"] = {
            "status": "ok" if current in SpecifierSet(required) else "incompatible",
            "version": current, "required": required, "executable": sys.executable,
        }
        deps = {}
        groups = {"core": project.get("dependencies", [])}
        groups.update(project.get("optional-dependencies", {}))
        for group, declarations in groups.items():
            for declaration in declarations:
                req = Requirement(declaration)
                # The aggregate extra refers to this project, not another package.
                if req.name == project["name"]:
                    continue
                if req.marker and not req.marker.evaluate({"extra": group}):
                    continue
                try:
                    installed = version(req.name)
                    status = "ok" if installed in req.specifier else "incompatible"
                except PackageNotFoundError:
                    installed, status = None, "missing"
                info = deps.setdefault(req.name, {
                    "status": status, "version": installed,
                    "required": str(req.specifier), "groups": [],
                })
                info["groups"].append(group)
        checks["python_deps"] = deps
    except (ImportError, OSError, ValueError, KeyError) as exc:
        checks["environment_contract"] = {
            "status": "error", "detail": str(exc),
            "hint": "安装诊断工具: bash tools/python.sh -m pip install '.[test]'；检查 pyproject.toml",
        }
        checks["python_deps"] = {}
    return checks


def check_python_deps():
    """Compatibility entry point for dependency checks."""
    return check_environment()["python_deps"]


def check_tushare_token():
    """Check Tushare token availability."""
    # CLI arg > env var > config file
    env_token = os.environ.get("TUSHARE_TOKEN")
    if env_token:
        return {"status": "ok", "source": "env"}

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
                    return {"status": "ok", "source": str(cp)}
            except (json.JSONDecodeError, OSError):
                continue

    return {"status": "missing", "source": None}


def check_tushare_api():
    """Check if Tushare API is actually accessible."""
    token_info = check_tushare_token()
    if token_info["status"] == "missing":
        return {"status": "unavailable", "detail": "No Tushare token configured"}

    try:
        tmp = _diag_tmp("tushare")
        result = _run_script("fetchers/kline.py", "600519.SH", "--asset", "E", "-o", tmp)
        if result is None:
            return {"status": "error", "detail": "Script execution failed"}

        with open(tmp) as f:
            data = json.load(f)

        ds = data.get("meta", {}).get("data_source", "")
        if ds == "error":
            err = data.get("meta", {}).get("error", "Unknown error")
            return {"status": "error", "detail": err[:120]}
        else:
            count = data.get("meta", {}).get("record_count", 0)
            return {"status": "ok", "detail": f"data_source={ds}, records={count}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def check_eastmoney():
    """Check if EastMoney API is accessible."""
    try:
        tmp = _diag_tmp("eastmoney")
        result = _run_script("fetchers/kline_eastmoney.py", "600519.SH", "-o", tmp)
        if result is None:
            return {"status": "error", "detail": "Script execution failed"}

        with open(tmp) as f:
            data = json.load(f)

        ds = data.get("meta", {}).get("data_source", "")
        if ds == "error":
            err = data.get("meta", {}).get("error", "Unknown error")
            return {"status": "error", "detail": err[:120]}
        else:
            count = data.get("meta", {}).get("record_count", 0)
            host = data.get("meta", {}).get("em_host", "")
            return {"status": "ok", "detail": f"data_source={ds}, records={count}, host={host}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def check_baostock():
    """Check if BaoStock is importable and can query data."""
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            return {"status": "error", "detail": f"BaoStock login failed: {lg.error_msg}"}
        rs = bs.query_history_k_data_plus(
            "sh.600519",
            "date,open,high,low,close,volume,amount,pctChg,preclose",
            start_date=(datetime.now().replace(day=1)).strftime("%Y-%m-%d"),
            end_date=datetime.now().strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        count = 0
        while rs.error_code == "0" and rs.next():
            count += 1
        bs.logout()
        if count > 0:
            return {"status": "ok", "detail": f"BaoStock OK, returned {count} records for test query"}
        else:
            return {"status": "error", "detail": f"BaoStock returned 0 records: {rs.error_msg}"}
    except ImportError:
        return {"status": "missing", "detail": "baostock package not installed"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def check_hk_support():
    """Check if HK stock data can be fetched."""
    try:
        tmp = _diag_tmp("hk")
        result = _run_script("fetchers/kline_eastmoney.py", "00700.HK", "-o", tmp)
        if result is None:
            return {"status": "error", "detail": "Script execution failed"}

        with open(tmp) as f:
            data = json.load(f)

        ds = data.get("meta", {}).get("data_source", "")
        if ds == "error":
            err = data.get("meta", {}).get("error", "Unknown error")
            return {"status": "unsupported", "detail": err[:120]}
        else:
            count = data.get("meta", {}).get("record_count", 0)
            return {"status": "ok", "detail": f"HK data source={ds}, records={count}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def check_weekly_support():
    """Check if weekly K-line data can be fetched."""
    try:
        tmp = _diag_tmp("weekly")
        result = _run_script("fetchers/kline_eastmoney.py", "600519.SH", "--freq", "W", "-o", tmp)
        if result is None:
            return {"status": "error", "detail": "Script execution failed"}

        with open(tmp) as f:
            data = json.load(f)

        ds = data.get("meta", {}).get("data_source", "")
        if ds == "error":
            err = data.get("meta", {}).get("error", "Unknown error")
            return {"status": "error", "detail": err[:120]}
        else:
            count = data.get("meta", {}).get("record_count", 0)
            return {"status": "ok", "detail": f"Weekly data_source={ds}, records={count}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def _run_script(script_name, *args):
    """Run a fetch script and return the process exit code."""
    from core.cache_utils import run_script_file
    rc, _, _ = run_script_file(script_name, *args, timeout=30)
    return rc


def load_cache():
    """Load cached diagnostic results if still valid."""
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                cache = json.load(f)
            cache_time = cache.get("timestamp", "")
            if cache_time:
                cache_dt = datetime.strptime(cache_time, "%Y%m%d-%H%M%S")
                if (datetime.now() - cache_dt).total_seconds() < CACHE_TTL:
                    return cache
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return None


def save_cache(result):
    """Save diagnostic results to cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def run_diagnostic(quick=False):
    """Run full or quick diagnostic."""
    checks = {}
    warnings = []
    recommendations = []

    # Environment checks always reflect this interpreter, never cached results.
    checks.update(check_environment())
    if "environment_contract" in checks:
        warnings.append("无法校验环境依赖声明")
        recommendations.append(checks["environment_contract"]["hint"])
    if checks.get("python", {}).get("status") == "incompatible":
        warnings.append("Python 版本不满足 pyproject.toml 要求")
        recommendations.append("使用 bash tools/python.sh 选择满足要求的解释器")
    for pkg, info in checks["python_deps"].items():
        if info["status"] != "ok":
            core = "core" in info["groups"]
            kind = "核心依赖" if core else "可选依赖"
            warnings.append(f"{kind} {pkg}: {info['status']} (要求 {info['required']})")
            profile = "." if core else ".[" + ",".join(info["groups"]) + "]"
            recommendations.append(f"按运行说明安装 {profile}；可选依赖缺失只影响对应能力")

    # Tushare token
    checks["tushare_token"] = check_tushare_token()
    if checks["tushare_token"]["status"] == "missing":
        warnings.append("Tushare Token 未配置，将降级到东方财富/BaoStock")
        recommendations.append("配置Tushare Token: export TUSHARE_TOKEN=xxx 或创建 .claude/tushare-config.json")

    if not quick:
        # Tushare API
        checks["tushare_api"] = check_tushare_api()

        # EastMoney
        checks["eastmoney"] = check_eastmoney()

        # BaoStock
        checks["baostock"] = check_baostock()

        # HK support
        checks["hk_support"] = check_hk_support()

        # Weekly support
        checks["weekly_support"] = check_weekly_support()

        # Build recommendations based on checks
        if checks["tushare_api"].get("status") == "error":
            recommendations.append("Tushare API不可用，检查Token权限或网络连接")

        if checks["eastmoney"].get("status") == "error":
            warnings.append("东方财富API不可用，A股数据将依赖BaoStock")

        if checks["hk_support"].get("status") == "unsupported":
            warnings.append("港股数据源不可用（需Tushare权限或新浪API）")

        if checks["weekly_support"].get("status") == "error":
            warnings.append("周线数据获取失败，--multi-timeframe 模式可能不可用")

        # Determine data source priority
        sources = []
        if checks.get("tushare_api", {}).get("status") == "ok":
            sources.append("tushare")
        if checks.get("eastmoney", {}).get("status") == "ok":
            sources.append("eastmoney")
        if checks.get("baostock", {}).get("status") == "ok":
            sources.append("baostock")
        if checks.get("hk_support", {}).get("status") == "ok":
            sources.append("sina_hk")
    else:
        # Quick mode: just check availability, don't make API calls
        sources = ["(quick mode - skip API calls)"]
        if checks["tushare_token"]["status"] == "ok":
            recommendations.append("Tushare Token已配置，建议运行完整诊断确认API可用性")

    result = {
        "timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "mode": "quick" if quick else "full",
        "checks": checks,
        "data_sources_priority": sources,
        "warnings": warnings,
        "recommendations": recommendations,
    }

    if not quick:
        save_cache(result)

    return result


def main():
    parser = argparse.ArgumentParser(description="Stock Trend Skill diagnostic script")
    parser.add_argument("--quick", action="store_true", help="Quick check (fresh environment checks, skip API calls)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    args = parser.parse_args()

    # Quick mode is offline and fresh. Full API results retain their own cache.
    result = run_diagnostic(quick=args.quick)

    text = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Diagnostic written to {args.output}", file=sys.stderr)
    else:
        print(text)

    # Print summary to stderr
    if result["warnings"]:
        print("\n⚠️ Warnings:", file=sys.stderr)
        for w in result["warnings"]:
            print(f"  - {w}", file=sys.stderr)

    if result["recommendations"]:
        print("\n💡 Recommendations:", file=sys.stderr)
        for r in result["recommendations"]:
            print(f"  - {r}", file=sys.stderr)


if __name__ == "__main__":
    main()