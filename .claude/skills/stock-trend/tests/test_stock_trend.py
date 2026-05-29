#!/usr/bin/env python3
"""Stock Trend Skill test suite.

Runs automated tests covering data fetching, technical analysis,
data quality checks, and edge cases.

Usage:
    python3 test_stock_trend.py              # Run all tests
    python3 test_stock_trend.py -v            # Verbose output
    python3 test_stock_trend.py --fetch-only  # Only run fetch tests
    python3 test_stock_trend.py --analyze-only # Only run analysis tests
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SCRIPTS_DIR = SCRIPT_DIR.parent / "scripts"

# Test result tracking
PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def run_script(script_name, *args, timeout=30):
    """Run a script and return (exit_code, stdout, stderr)."""
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def load_json_output(path):
    """Load JSON from a file path."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test(name, condition, detail="", category="general"):
    """Record a test result."""
    global PASSED, FAILED, SKIPPED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def skip(name, reason=""):
    """Record a skipped test."""
    global SKIPPED
    SKIPPED += 1
    RESULTS.append({"name": name, "status": "SKIP", "detail": reason, "category": "skip"})
    print(f"  [SKIP] {name}" + (f" — {reason}" if reason else ""))


# ========================
# Fetch Tests (TF-*)
# ========================

def run_fetch_tests():
    """Test data fetching scripts."""
    print("\n📦 数据获取测试 (Fetch)")
    print("=" * 50)

    tmpdir = tempfile.mkdtemp()

    # TF-01: 上交所股票 (茅台)
    path = os.path.join(tmpdir, "tf01.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "600519.SH", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        count = data.get("meta", {}).get("record_count", 0)
        test("TF-01: 上交所股票(茅台)", ds != "error" and count > 0,
             f"source={ds}, records={count}", "fetch")
        test("TF-01a: 数据量≥60", count >= 60,
             f"records={count}", "fetch")
    else:
        test("TF-01: 上交所股票(茅台)", False, f"exit_code={rc}", "fetch")

    # TF-02: 深交所股票 (平安银行)
    path = os.path.join(tmpdir, "tf02.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "000001.SZ", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        count = data.get("meta", {}).get("record_count", 0)
        test("TF-02: 深交所股票(平安银行)", ds != "error" and count > 0,
             f"source={ds}, records={count}", "fetch")
    else:
        test("TF-02: 深交所股票(平安银行)", False, f"exit_code={rc}", "fetch")

    # TF-05: 上交所ETF
    path = os.path.join(tmpdir, "tf05.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "513180.SH", "--asset", "FD", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        count = data.get("meta", {}).get("record_count", 0)
        test("TF-05: 上交所ETF(513180)", ds != "error" and count > 0,
             f"source={ds}, records={count}", "fetch")
    else:
        test("TF-05: 上交所ETF(513180)", False, f"exit_code={rc}", "fetch")

    # TF-07: 港股 (腾讯) - 关键测试：Fix 1
    path = os.path.join(tmpdir, "tf07.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "00700.HK", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        count = data.get("meta", {}).get("record_count", 0)
        test("TF-07: 港股(腾讯00700)", ds != "error" and count > 0,
             f"source={ds}, records={count}", "fetch")
        if ds != "error" and count > 0:
            # Verify HK data fields
            first = data["data"][0] if data["data"] else {}
            has_ohlcv = all(k in first for k in ["trade_date", "open", "close", "high", "low"])
            test("TF-07a: 港股数据字段完整性", has_ohlcv,
                 f"fields={list(first.keys())}", "fetch")
    else:
        test("TF-07: 港股(腾讯00700)", False, f"exit_code={rc}", "fetch")

    # TF-08: 无效代码
    path = os.path.join(tmpdir, "tf08.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "999999.SH", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        has_error = ds == "error"
        test("TF-08: 无效代码返回error", has_error,
             f"data_source={ds}", "fetch")
    else:
        test("TF-08: 无效代码返回error", False, f"exit_code={rc}", "fetch")

    # TF-09: 周线数据 (关键测试：Fix 2)
    path = os.path.join(tmpdir, "tf09.json")
    rc, stdout, stderr = run_script("fetch_kline_eastmoney.py", "600519.SH", "--freq", "W", "-o", path)
    if rc == 0:
        data = load_json_output(path)
        ds = data.get("meta", {}).get("data_source", "")
        count = data.get("meta", {}).get("record_count", 0)
        test("TF-09: 周线数据获取", ds != "error" and count > 0,
             f"source={ds}, records={count}", "fetch")
    else:
        test("TF-09: 周线数据获取", False, f"exit_code={rc}", "fetch")

    # TF-11: 数据字段完整性
    path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(path):
        data = load_json_output(path)
        if data.get("data"):
            first = data["data"][0]
            required = ["trade_date", "open", "close", "high", "low", "vol"]
            has_all = all(k in first for k in required)
            test("TF-11: 数据字段完整性", has_all,
                 f"missing={[k for k in required if k not in first]}", "fetch")

    return tmpdir


# ========================
# Analysis Tests (TA-*)
# ========================

def run_analyze_tests(tmpdir):
    """Test technical analysis script."""
    print("\n📊 技术分析测试 (Analyze)")
    print("=" * 50)

    # TA-01: 正常数据(200+条)
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        tech_path = os.path.join(tmpdir, "ta01.json")
        rc, stdout, stderr = run_script("analyze_technical.py", kline_path, "-o", tech_path)
        if rc == 0:
            data = load_json_output(tech_path)
            summary = data.get("summary", {})
            latest = data.get("latest", {})

            test("TA-01: 正常数据分析完成", "total_score" in summary, "fetch")
            test("TA-01a: summary有direction", "direction" in summary,
                 f"direction={summary.get('direction')}", "analyze")
            test("TA-01b: summary有confidence", "confidence" in summary,
                 f"confidence={summary.get('confidence')}", "analyze")
            test("TA-01c: summary有stop_loss", "stop_loss" in summary,
                 f"stop_loss={summary.get('stop_loss')}", "analyze")

            # TA-05: score范围检查
            indicators = ["ma", "macd", "rsi", "kdj", "bollinger", "volume", "adx", "obv"]
            all_scores_valid = True
            for ind in indicators:
                sig = latest.get(ind, {}).get("signal", {})
                score = sig.get("score", 0)
                if not (-3 <= score <= 3):
                    all_scores_valid = False
            test("TA-05: score范围[-3,+3]", all_scores_valid, "analyze")

            # TA-08: pattern去重
            patterns = data.get("patterns", [])
            names = [p["name"] for p in patterns]
            test("TA-08: pattern无重名", len(names) == len(set(names)),
                 f"patterns={names}", "analyze")

            # TA-06: summary字段完整性
            required_fields = ["total_score", "direction", "confidence", "consistency",
                              "support_levels", "resistance_levels", "stop_loss", "target"]
            missing = [f for f in required_fields if f not in summary]
            test("TA-06: summary字段完整", len(missing) == 0,
                 f"missing={missing}", "analyze")
        else:
            test("TA-01: 正常数据分析完成", False, f"exit_code={rc}", "analyze")

    # TA-02: 小数据量测试 (Fix 3 - data_quality标记)
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        # Create small data file
        data = load_json_output(kline_path)
        small_data = {"meta": data["meta"], "data": data["data"][:15]}
        small_path = os.path.join(tmpdir, "small_data.json")
        with open(small_path, "w") as f:
            json.dump(small_data, f)

        tech_path = os.path.join(tmpdir, "ta02.json")
        rc, stdout, stderr = run_script("analyze_technical.py", small_path, "-o", tech_path)
        if rc == 0:
            result = load_json_output(tech_path)
            summary = result.get("summary", {})
            dq = summary.get("data_quality")
            signals = summary.get("key_signals", [])

            test("TA-02: 小数据量(15条)有data_quality", dq == "insufficient",
                 f"data_quality={dq}", "analyze")
            test("TA-02a: key_signals含数据量警告",
                 any("数据仅" in s for s in signals),
                 f"signals={signals[:2]}", "analyze")
        else:
            test("TA-02: 小数据分析", False, f"exit_code={rc}", "analyze")

    # TA-04: 空数据/error输入
    error_data = {"meta": {"ts_code": "999999.SH", "data_source": "error", "error": "test error"}, "data": []}
    error_path = os.path.join(tmpdir, "error_data.json")
    with open(error_path, "w") as f:
        json.dump(error_data, f)

    tech_path = os.path.join(tmpdir, "ta04.json")
    rc, stdout, stderr = run_script("analyze_technical.py", error_path, "-o", tech_path)
    if rc == 0:
        result = load_json_output(tech_path)
        summary = result.get("summary", {})
        test("TA-04: 空数据分析", summary.get("total_score") == 0,
             f"total_score={summary.get('total_score')}", "analyze")
        test("TA-04a: 空数据direction=neutral", summary.get("direction") == "neutral",
             f"direction={summary.get('direction')}", "analyze")
    else:
        test("TA-04: 空数据分析", False, f"exit_code={rc}", "analyze")

    # TA-03: 50条数据 (limited data quality)
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        data = load_json_output(kline_path)
        limited_data = {"meta": data["meta"], "data": data["data"][:50]}
        limited_path = os.path.join(tmpdir, "limited_data.json")
        with open(limited_path, "w") as f:
            json.dump(limited_data, f)

        tech_path = os.path.join(tmpdir, "ta03.json")
        rc, stdout, stderr = run_script("analyze_technical.py", limited_path, "-o", tech_path)
        if rc == 0:
            result = load_json_output(tech_path)
            summary = result.get("summary", {})
            dq = summary.get("data_quality")
            test("TA-03: 50条数据data_quality=limited", dq == "limited",
                 f"data_quality={dq}", "analyze")
        else:
            test("TA-03: 50条数据分析", False, f"exit_code={rc}", "analyze")

    # TA-stdin: 测试stdin输入支持 (Fix 4)
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        # Test piping via stdin using "-" argument
        with open(kline_path, "r") as f:
            kline_data = f.read()
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "analyze_technical.py"), "-", "-o", os.path.join(tmpdir, "ta_stdin.json")],
            input=kline_data, capture_output=True, text=True, timeout=30
        )
        test("TA-stdin: 支持'-'作为stdin输入", result.returncode == 0,
             f"exit_code={result.returncode}", "analyze")

    # TA-10: 三级目标体系输出
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        tech_path = os.path.join(tmpdir, "ta01.json")
        if os.path.exists(tech_path):
            data = load_json_output(tech_path)
            summary = data.get("summary", {})
            test("TA-10: 三级目标体系(target_conservative)", "target_conservative" in summary,
                 f"target_conservative={summary.get('target_conservative')}", "analyze")
            test("TA-10: 三级目标体系(target_moderate)", "target_moderate" in summary,
                 f"target_moderate={summary.get('target_moderate')}", "analyze")
            test("TA-10: 三级目标体系(target_aggressive)", "target_aggressive" in summary,
                 f"target_aggressive={summary.get('target_aggressive')}", "analyze")

    # TA-11: 动态聚类阈值(ATR-based)
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        tech_path = os.path.join(tmpdir, "ta01.json")
        if os.path.exists(tech_path):
            data = load_json_output(tech_path)
            summary = data.get("summary", {})
            # Dynamic threshold should produce fewer or equal levels vs old 0.5% fixed threshold
            support_levels = summary.get("support_levels", [])
            resistance_levels = summary.get("resistance_levels", [])
            test("TA-11: 动态聚类阈值(支撑位≤5)", len(support_levels) <= 5,
                 f"support_count={len(support_levels)}", "analyze")
            test("TA-11: 动态聚类阈值(压力位≤5)", len(resistance_levels) <= 5,
                 f"resistance_count={len(resistance_levels)}", "analyze")

    # TA-12: 自适应止损应低于现价，且不能过近到被日常波动轻易扫掉
    kline_path = os.path.join(tmpdir, "tf01.json")
    if os.path.exists(kline_path):
        tech_path = os.path.join(tmpdir, "ta01.json")
        if os.path.exists(tech_path):
            data = load_json_output(tech_path)
            summary = data.get("summary", {})
            stop_loss = summary.get("stop_loss")
            latest = data.get("latest", {})
            atr = latest.get("atr", {}).get("atr")
            close = latest.get("close")
            if stop_loss and atr and close:
                stop_distance = close - stop_loss
                test("TA-12: 止损低于现价且至少保留0.5ATR缓冲",
                     stop_loss < close and stop_distance >= atr * 0.5 - 0.01,
                     f"stop_loss={stop_loss}, close={close:.2f}, atr={atr:.2f}", "analyze")


# ========================
# New Script Tests (TF-ETF, TF-CF, TF-RPT)
# ========================

def _write_report_fixture(tmpdir, name, *, confidence="中", rr_ratio=2.2, latest_close=1260.0):
    technical_path = os.path.join(tmpdir, f"{name}_technical.json")
    kline_path = os.path.join(tmpdir, f"{name}_kline.json")
    scores_path = os.path.join(tmpdir, f"{name}_scores.json")

    technical = {
        "meta": {"ts_code": "600519.SH"},
        "latest": {"close": latest_close},
        "summary": {
            "total_score": 2.1,
            "direction": "看多",
            "confidence": confidence,
            "key_signals": ["均线多头排列", "支撑位附近缩量企稳"],
            "support_levels": [1235.0, 1248.0],
            "resistance_levels": [1288.0, 1315.0],
            "stop_loss": 1236.0,
            "target_conservative": 1288.0,
            "target_moderate": 1315.0,
            "target_aggressive": 1340.0,
            "risk_reward_ratio": rr_ratio,
            "rr_conservative": 0.9,
            "rr_moderate": rr_ratio,
            "rr_aggressive": 3.1,
            "position_sizing": "标准仓位(50-70%)",
            "max_drawdown_pct": -1.9,
        },
        "patterns": [],
    }

    kline = {
        "meta": {
            "ts_code": "600519.SH",
            "data_source": "eastmoney",
            "record_count": 120,
            "start_date": "2026-01-02",
            "end_date": "2026-05-29",
        },
        "data": [{"trade_date": "2026-05-29", "close": latest_close}],
    }

    scores = {
        "scores": {"technical": 2, "capital_flow": 1, "fundamental": 0, "sentiment": 0, "macro": 0},
        "direction": "看多",
        "composite_score": 2.1,
        "confidence": confidence,
        "risks": ["量能不足"],
        "analysis": {
            "core_conflict": "趋势偏多，但当前位置略高于理想回踩位。",
            "events": [{"date": "2026-06-10", "event": "股东大会", "impact": "事件前若未回踩则放弃计划"}],
            "advice": ["回踩 1248-1252 分批试仓", "若放量站稳 1288 再考虑追踪"],
        },
        "report_params": {
            "entry_verdict": "watch",
            "entry_signals": ["回踩支撑不破", "量能回补"],
            "support_levels": [1235.0, 1248.0],
            "resistance_levels": [1288.0, 1315.0],
            "stop_loss": 1236.0,
            "target_conservative": 1288.0,
            "target_moderate": 1315.0,
            "target_aggressive": 1340.0,
            "risk_reward_ratio": rr_ratio,
            "rr_conservative": 0.9,
            "rr_moderate": rr_ratio,
            "rr_aggressive": 3.1,
            "position_sizing": "标准仓位(50-70%)",
            "max_drawdown_pct": -1.9,
        },
    }

    for path, payload in (
        (technical_path, technical),
        (kline_path, kline),
        (scores_path, scores),
    ):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return technical_path, kline_path, scores_path


def run_new_script_tests(tmpdir):
    """Test new data scripts: fetch_etf_data, fetch_capital_flow, generate_report."""
    print("\n📦 新脚本测试 (New Scripts)")
    print("=" * 50)

    # TF-ETF-01: fetch_etf_data.py 基本功能测试
    etf_path = os.path.join(tmpdir, "tf_etf01.json")
    rc, stdout, stderr = run_script("fetch_etf_data.py", "513180", "-o", etf_path, timeout=15)
    if rc == 0 and os.path.exists(etf_path):
        try:
            data = load_json_output(etf_path)
            ds = data.get("data_source", "")
            test("TF-ETF-01: ETF数据获取(513180)", ds == "eastmoney",
                 f"source={ds}", "fetch")
            test("TF-ETF-01a: 含fund_code", "fund_code" in data,
                 f"fund_code={data.get('fund_code')}", "fetch")
        except (json.JSONDecodeError, OSError):
            test("TF-ETF-01: ETF数据获取(513180)", False, "JSON解析失败", "fetch")
    else:
        test("TF-ETF-01: ETF数据获取(513180)", False, f"exit_code={rc}", "fetch")

    # TF-CF-01: fetch_capital_flow.py 股票资金流向测试
    cf_path = os.path.join(tmpdir, "tf_cf01.json")
    rc, stdout, stderr = run_script("fetch_capital_flow.py", "600519.SH", "--asset", "E", "-o", cf_path, timeout=30)
    if rc == 0 and os.path.exists(cf_path):
        try:
            data = load_json_output(cf_path)
            ds = data.get("meta", {}).get("data_source", "")
            count = data.get("meta", {}).get("record_count", 0)
            test("TF-CF-01: 股票资金流向(600519)", ds != "error",
                 f"source={ds}, records={count}", "fetch")
        except (json.JSONDecodeError, OSError):
            test("TF-CF-01: 股票资金流向(600519)", False, "JSON解析失败", "fetch")
    else:
        test("TF-CF-01: 股票资金流向(600519)", False, f"exit_code={rc}", "fetch")

    # TF-RPT-01: generate_report.py 模板渲染测试
    tech_path, kline_path, scores_path = _write_report_fixture(tmpdir, "render")
    md_path = os.path.join(tmpdir, "test_report.md")
    html_path = os.path.join(tmpdir, "test_report.html")
    rc, stdout, stderr = run_script(
        "generate_report.py",
        "--technical", tech_path,
        "--kline", kline_path,
        "--scores-file", scores_path,
        "--stock-name", "贵州茅台",
        "--date", "2026-05-29",
        "--output-md", md_path,
        "--output-html", html_path,
        timeout=15,
    )
    test("TF-RPT-01: 报告生成(exit_code)", rc == 0, f"exit_code={rc}", "report")
    if rc == 0:
        md_exists = os.path.exists(md_path)
        test("TF-RPT-01a: Markdown报告存在", md_exists, f"path={md_path}", "report")
        html_exists = os.path.exists(html_path)
        test("TF-RPT-01b: HTML报告存在", html_exists, f"path={html_path}", "report")

        if md_exists:
            with open(md_path, "r", encoding="utf-8") as f:
                md_content = f.read()
            test("TF-RPT-01c: MD含今日动作", "今日动作" in md_content, md_content[:200], "report")
            test("TF-RPT-01d: MD含场景A", "场景 A：继续上冲" in md_content, md_content[:200], "report")
            test("TF-RPT-01e: MD含场景B", "场景 B：回调到位" in md_content, md_content[:200], "report")
            test("TF-RPT-01f: MD含执行时间窗", "执行时间窗" in md_content, md_content[:200], "report")

        if html_exists:
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            test("TF-RPT-01g: HTML含今日动作", "今日动作" in html_content, html_content[:200], "report")
            test("TF-RPT-01h: HTML含场景A", "场景 A：继续上冲" in html_content, html_content[:200], "report")
            test("TF-RPT-01i: HTML含场景B", "场景 B：回调到位" in html_content, html_content[:200], "report")

    weak_tech_path, weak_kline_path, weak_scores_path = _write_report_fixture(
        tmpdir,
        "render_weak",
        confidence="低",
        rr_ratio=None,
        latest_close=1260.0,
    )
    weak_md_path = os.path.join(tmpdir, "test_report_weak.md")
    rc, stdout, stderr = run_script(
        "generate_report.py",
        "--technical", weak_tech_path,
        "--kline", weak_kline_path,
        "--scores-file", weak_scores_path,
        "--stock-name", "贵州茅台",
        "--date", "2026-05-29",
        "--output-md", weak_md_path,
        timeout=15,
    )
    weak_md_content = ""
    if rc == 0 and os.path.exists(weak_md_path):
        with open(weak_md_path, "r", encoding="utf-8") as f:
            weak_md_content = f.read()
    test("TF-RPT-02: 低置信度默认只观察",
         "只观察" in weak_md_content,
         weak_md_content[:200], "report")

    sys.path.insert(0, str(SCRIPTS_DIR))
    import generate_report

    tech_path, kline_path, scores_path = _write_report_fixture(tmpdir, "actionable")

    args = argparse.Namespace(
        technical=tech_path,
        kline=kline_path,
        etf_data=None,
        capital_flow=None,
        scores=json.dumps({"technical": 2, "capital_flow": 1, "fundamental": 0, "sentiment": 0, "macro": 0}, ensure_ascii=False),
        scores_file=scores_path,
        pipeline=None,
        direction="看多",
        score=2.1,
        confidence="中",
        risks=json.dumps(["量能不足"], ensure_ascii=False),
        special=None,
        ts_code="600519.SH",
        stock_name="贵州茅台",
        date="2026-05-29",
        horizon="日线",
        focus=None,
        capital_summary="—",
        fundamental_summary="—",
        sentiment_summary="—",
        macro_summary="—",
        entry_verdict=None,
        entry_signals=None,
        analysis=None,
        chart=None,
        fundamental_data=None,
        macro_data=None,
        futures_data=None,
        chip_distribution=None,
        output_md=None,
        output_html=None,
        code=None,
        data_dir=None,
    )

    context = generate_report.build_context(args)
    test("TF-RPT-CTX-01: 今日动作标签", context.get("今日动作标签") == "可低吸",
         f"label={context.get('今日动作标签')}", "report")
    test("TF-RPT-CTX-02: 场景A标题", context.get("场景A标题") == "场景 A：继续上冲",
         f"title={context.get('场景A标题')}", "report")
    test("TF-RPT-CTX-03: 场景B动作含分批试仓",
         "分批试仓" in str(context.get("场景B动作", "")),
         str(context.get("场景B动作")), "report")
    test("TF-RPT-CTX-04: 执行时间窗含事件日期",
         "2026-06-10" in str(context.get("执行时间窗", "")),
         str(context.get("执行时间窗")), "report")

    nearest_plan = generate_report.build_action_plan(
        "看多",
        "中",
        100.8,
        {
            "support_levels": [95.0, 100.0],
            "resistance_levels": [106.0],
            "stop_loss": 98.0,
            "target_conservative": 106.0,
            "target_moderate": 110.0,
            "risk_reward_ratio": 2.0,
        },
        {},
    )
    nearest_text = " ".join(str(v) for v in nearest_plan.values())
    test("TF-RPT-ACT-01: 选择最近支撑位",
         nearest_plan.get("今日动作标签") == "可低吸" and "100.00" in nearest_text and "95.00" not in nearest_text,
         f"label={nearest_plan.get('今日动作标签')}, summary={nearest_plan.get('今日动作摘要')}", "report")

    incomplete_plan = generate_report.build_action_plan(
        "看多",
        "中",
        100.8,
        {
            "support_levels": [95.0, 100.0],
            "stop_loss": 98.0,
            "risk_reward_ratio": 2.0,
        },
        {},
    )
    executable_text = " ".join(
        str(incomplete_plan.get(key, ""))
        for key in (
            "场景A条件", "场景A动作", "场景B条件", "场景B动作",
            "退出条件1", "退出动作1", "退出条件2", "退出动作2", "退出条件3", "退出动作3",
        )
    )
    test("TF-RPT-ACT-02: 决策价位不完整时只观察",
         incomplete_plan.get("今日动作标签") == "只观察",
         f"label={incomplete_plan.get('今日动作标签')}", "report")
    test("TF-RPT-ACT-03: 决策价位不完整时不输出占位执行价",
         "—" not in executable_text,
         executable_text, "report")

    low_confidence_plan = generate_report.build_action_plan(
        "看多",
        "低",
        100.8,
        {
            "support_levels": [95.0, 100.0],
            "resistance_levels": [106.0],
            "stop_loss": 98.0,
            "target_conservative": 106.0,
            "target_moderate": 110.0,
            "risk_reward_ratio": 2.0,
        },
        {},
    )
    low_confidence_scenario_text = " ".join(
        str(low_confidence_plan.get(key, ""))
        for key in (
            "场景A动作", "场景B动作", "退出动作1", "退出动作2", "退出动作3",
        )
    )
    test("TF-RPT-ACT-04: 低置信度完整价位只观察",
         low_confidence_plan.get("今日动作标签") == "只观察",
         f"label={low_confidence_plan.get('今日动作标签')}", "report")
    test("TF-RPT-ACT-05: 低置信度只观察动作保持被动",
         ("分批试仓" not in low_confidence_scenario_text
          and "分批止盈" not in low_confidence_scenario_text
          and "追踪" not in low_confidence_scenario_text
          and ("观察" in low_confidence_scenario_text or "等待" in low_confidence_scenario_text)),
         low_confidence_scenario_text, "report")


# ========================
# Diagnostic Tests (TD-*)
# ========================

def run_diagnostic_tests():
    """Test the diagnostic script."""
    print("\n🔍 诊断脚本测试 (Diagnostic)")
    print("=" * 50)

    tmpdir = tempfile.mkdtemp()

    # Quick diagnostic
    path = os.path.join(tmpdir, "diag_quick.json")
    rc, stdout, stderr = run_script("diagnose.py", "--quick", "-o", path)
    if rc == 0 and os.path.exists(path):
        data = load_json_output(path)
        test("TD-quick: 快速诊断运行", "checks" in data,
             f"mode={data.get('mode')}", "diagnostic")
        test("TD-quick: Python依赖检查", "python_deps" in data.get("checks", {}),
             "diagnostic")
    else:
        test("TD-quick: 快速诊断运行", False, f"exit_code={rc}", "diagnostic")


# ========================
# Pipeline & Automation Tests (TP-*)
# ========================

def run_pipeline_tests(tmpdir):
    """Test resolve_code, run_pipeline, and compute_scores scripts."""
    print("\n🔧 管线与自动化测试 (Pipeline)")
    print("=" * 50)

    # TP-RC-01: resolve_code.py - 代码输入
    rc, stdout, stderr = run_script("resolve_code.py", "513180")
    if rc == 0:
        try:
            data = json.loads(stdout)
            test("TP-RC-01: 代码解析(513180)",
                 data.get("ts_code") == "513180.SH",
                 f"ts_code={data.get('ts_code')}", "resolve")
            test("TP-RC-01a: asset=FD",
                 data.get("asset") == "FD",
                 f"asset={data.get('asset')}", "resolve")
            test("TP-RC-01b: adj=qfq",
                 data.get("adj") == "qfq",
                 f"adj={data.get('adj')}", "resolve")
        except json.JSONDecodeError:
            test("TP-RC-01: 代码解析(513180)", False, "JSON解析失败", "resolve")
    else:
        test("TP-RC-01: 代码解析(513180)", False, f"exit_code={rc}", "resolve")

    # TP-RC-02: resolve_code.py - 名称输入
    rc, stdout, stderr = run_script("resolve_code.py", "恒生科技ETF大成")
    if rc == 0:
        try:
            data = json.loads(stdout)
            test("TP-RC-02: 名称解析(恒生科技ETF大成)",
                 data.get("ts_code") == "159740.SZ",
                 f"ts_code={data.get('ts_code')}", "resolve")
        except json.JSONDecodeError:
            test("TP-RC-02: 名称解析(恒生科技ETF大成)", False, "JSON解析失败", "resolve")
    else:
        test("TP-RC-02: 名称解析(恒生科技ETF大成)", False, f"exit_code={rc}", "resolve")

    # TP-RC-03: resolve_code.py - 港股代码
    rc, stdout, stderr = run_script("resolve_code.py", "00700.HK")
    if rc == 0:
        try:
            data = json.loads(stdout)
            test("TP-RC-03: 港股代码解析",
                 data.get("ts_code") == "00700.HK" and data.get("asset") == "E",
                 f"ts_code={data.get('ts_code')}, asset={data.get('asset')}", "resolve")
        except json.JSONDecodeError:
            test("TP-RC-03: 港股代码解析", False, "JSON解析失败", "resolve")
    else:
        test("TP-RC-03: 港股代码解析", False, f"exit_code={rc}", "resolve")

    # TP-RC-04: resolve_code.py - 输出到文件
    out_path = os.path.join(tmpdir, "resolve_out.json")
    rc, stdout, stderr = run_script("resolve_code.py", "600519", "-o", out_path)
    if rc == 0:
        data = load_json_output(out_path)
        test("TP-RC-04: 文件输出",
             data.get("ts_code") == "600519.SH",
             f"ts_code={data.get('ts_code')}", "resolve")
    else:
        test("TP-RC-04: 文件输出", False, f"exit_code={rc}", "resolve")

    # TP-CS-01: compute_scores.py - 基本评分计算
    tech_path = os.path.join(tmpdir, "ta01.json")
    if os.path.exists(tech_path):
        scores_path = os.path.join(tmpdir, "scores01.json")
        rc, stdout, stderr = run_script(
            "compute_scores.py",
            "--technical", tech_path,
            "--capital-flow-score", "0.5",
            "--fundamental-score", "1",
            "--sentiment-score", "1",
            "--macro-score", "0.5",
            "--asset-type", "stock",
            "-o", scores_path,
        )
        if rc == 0 and os.path.exists(scores_path):
            data = load_json_output(scores_path)
            test("TP-CS-01: 评分计算",
                 "composite_score" in data and "direction" in data,
                 f"score={data.get('composite_score')}, dir={data.get('direction')}", "scores")
            test("TP-CS-01a: 包含权重",
                 "weights" in data and len(data.get("weights", {})) == 5,
                 f"weights={data.get('weights')}", "scores")
            test("TP-CS-01b: 包含风险列表",
                 "risks" in data and isinstance(data.get("risks"), list),
                 f"risks_count={len(data.get('risks', []))}", "scores")
        else:
            test("TP-CS-01: 评分计算", False, f"exit_code={rc}", "scores")
    else:
        skip("TP-CS-01: 评分计算", "缺少技术分析数据")

    # TP-CS-02: compute_scores.py - ETF类型自动生成special
    tech_path = os.path.join(tmpdir, "ta01.json")
    etf_path = os.path.join(tmpdir, "tf_etf01.json")
    if os.path.exists(tech_path) and os.path.exists(etf_path):
        scores_path = os.path.join(tmpdir, "scores02.json")
        rc, stdout, stderr = run_script(
            "compute_scores.py",
            "--technical", tech_path,
            "--asset-type", "etf",
            "--etf-data", etf_path,
            "-o", scores_path,
        )
        if rc == 0 and os.path.exists(scores_path):
            data = load_json_output(scores_path)
            special = data.get("special")
            test("TP-CS-02: ETF特殊标记",
                 special is not None and special.get("type") == "etf",
                 f"type={special.get('type') if special else None}", "scores")
        else:
            test("TP-CS-02: ETF特殊标记", False, f"exit_code={rc}", "scores")
    else:
        skip("TP-CS-02: ETF特殊标记", "缺少前置数据")

    # TP-CS-03: compute_scores.py - focus权重调整
    tech_path = os.path.join(tmpdir, "ta01.json")
    if os.path.exists(tech_path):
        scores_path = os.path.join(tmpdir, "scores03.json")
        rc, stdout, stderr = run_script(
            "compute_scores.py",
            "--technical", tech_path,
            "--focus", "technical",
            "-o", scores_path,
        )
        if rc == 0 and os.path.exists(scores_path):
            data = load_json_output(scores_path)
            weights = data.get("weights", {})
            test("TP-CS-03: focus权重调整(技术55%)",
                 abs(weights.get("technical", 0) - 0.55) < 0.01,
                 f"technical_weight={weights.get('technical')}", "scores")
        else:
            test("TP-CS-03: focus权重调整", False, f"exit_code={rc}", "scores")
    else:
        skip("TP-CS-03: focus权重调整", "缺少技术分析数据")

    # TP-PL-01: run_pipeline.py - ETF标的
    pipeline_path = os.path.join(tmpdir, "pipeline01.json")
    rc, stdout, stderr = run_script(
        "run_pipeline.py", "159740.SZ", "--asset", "FD", "--adj", "qfq",
        "-o", tmpdir, timeout=180,
    )
    if rc == 0:
        # Check pipeline output
        pp = os.path.join(tmpdir, "pipeline_output.json")
        if os.path.exists(pp):
            data = load_json_output(pp)
            test("TP-PL-01: 管线执行(ETF)",
                 "meta" in data and "results" in data,
                 f"ts_code={data.get('meta', {}).get('ts_code')}", "pipeline")
            kline_result = data.get("results", {}).get("kline", {})
            test("TP-PL-01a: K线数据获取",
                 kline_result.get("record_count", 0) > 0,
                 f"records={kline_result.get('record_count')}", "pipeline")
        else:
            test("TP-PL-01: 管线执行(ETF)", False, "pipeline_output.json不存在", "pipeline")
    else:
        test("TP-PL-01: 管线执行(ETF)", False, f"exit_code={rc}, stderr={stderr[:200]}", "pipeline")

    # TP-PL-02: run_pipeline.py - 股票标的
    pipeline_path = os.path.join(tmpdir, "pipeline02.json")
    rc, stdout, stderr = run_script(
        "run_pipeline.py", "600519.SH", "--asset", "E", "--adj", "qfq",
        "-o", tmpdir, timeout=180,
    )
    if rc == 0:
        pp = os.path.join(tmpdir, "pipeline_output.json")
        if os.path.exists(pp):
            data = load_json_output(pp)
            meta = data.get("meta", {})
            test("TP-PL-02: 管线执行(股票)",
                 meta.get("ts_code") == "600519.SH" and meta.get("asset") == "E",
                 f"ts_code={meta.get('ts_code')}", "pipeline")
        else:
            test("TP-PL-02: 管线执行(股票)", False, "pipeline_output.json不存在", "pipeline")
    else:
        test("TP-PL-02: 管线执行(股票)", False, f"exit_code={rc}", "pipeline")


# ========================
# Validate Input Tests (VI-*)
# ========================

def run_validate_tests():
    """Test validate_input() function in compute_scores.py."""
    print("\n✅ 输入校验测试 (Validate)")
    print("=" * 50)

    # Import validate_input from compute_scores
    sys.path.insert(0, str(SCRIPTS_DIR))
    from compute_scores import validate_input

    # VI-valid-tech: valid technical data passes (errors == 0)
    valid_tech = {
        "summary": {
            "total_score": 1.5,
            "direction": "看多",
            "confidence": 0.7,
        },
        "data_quality": "good",
    }
    valid_scores = {
        "technical": 1.5,
        "capital_flow": 0.5,
        "fundamental": -1,
        "sentiment": 0,
        "macro": 0,
    }
    errors = validate_input(valid_tech, valid_scores)
    test("VI-valid-tech: valid technical data passes",
         len(errors) == 0, f"errors={errors}", "validate")

    # VI-missing-fields: missing required fields (errors > 0)
    missing_fields_tech = {
        "summary": {},  # missing total_score, direction, confidence
    }
    errors = validate_input(missing_fields_tech, valid_scores)
    test("VI-missing-fields: missing required fields",
         len(errors) > 0, f"errors={errors}", "validate")

    # VI-bad-quality: invalid data_quality enum (errors > 0)
    bad_quality_tech = {
        "summary": {
            "total_score": 1.5,
            "direction": "看多",
            "confidence": 0.7,
        },
        "data_quality": "invalid_quality",
    }
    errors = validate_input(bad_quality_tech, valid_scores)
    test("VI-bad-quality: invalid data_quality enum",
         len(errors) > 0, f"errors={errors}", "validate")

    # VI-score-range: score out of range [-100, 100]
    out_of_range_scores = {
        "technical": 200,
        "capital_flow": 0.5,
        "fundamental": -1,
        "sentiment": 0,
        "macro": 0,
    }
    errors = validate_input(valid_tech, out_of_range_scores)
    test("VI-score-range: score out of range",
         any("range" in e for e in errors), f"errors={errors}", "validate")

    # VI-missing-dim: dimension data file missing
    tmpdir = tempfile.mkdtemp()
    errors = validate_input(valid_tech, valid_scores, data_dir=tmpdir)
    test("VI-missing-dim: dimension data file missing",
         any("missing" in e.lower() or "not found" in e.lower() for e in errors),
         f"errors={errors}", "validate")
    # Cleanup
    os.rmdir(tmpdir)


def run_portfolio_integration_tests():
    """Run portfolio manager tests from tests/test_portfolio.py."""
    print("\n📁 持仓管理测试 (Portfolio)")
    print("=" * 50)
    tests_dir = SCRIPT_DIR
    sys.path.insert(0, str(tests_dir))
    try:
        from test_portfolio import run_portfolio_tests
        p, f = run_portfolio_tests()
        global PASSED, FAILED
        PASSED += p
        FAILED += f
    except ImportError as e:
        print(f"  [SKIP] portfolio tests — {e}")


def run_backtest_integration_tests():
    """Run backtest engine tests from tests/test_backtest.py."""
    print("\n📊 回测验证测试 (Backtest)")
    print("=" * 50)
    tests_dir = SCRIPT_DIR
    sys.path.insert(0, str(tests_dir))
    try:
        from test_backtest import run_backtest_tests
        p, f = run_backtest_tests()
        global PASSED, FAILED
        PASSED += p
        FAILED += f
    except ImportError as e:
        print(f"  [SKIP] backtest tests — {e}")


def run_golden_diff_tests():
    """Run golden snapshot diff tests by invoking test_golden.py."""
    test_golden_path = SCRIPT_DIR / "test_golden.py"
    if not test_golden_path.exists():
        skip("TG-golden-diff", "test_golden.py not found")
        return

    result = subprocess.run(
        [sys.executable, str(test_golden_path), "--diff"],
        capture_output=True, text=True, timeout=240,
    )
    test("TG-golden-diff: snapshot diff",
         result.returncode == 0,
         f"exit={result.returncode}, output={result.stdout[:200]}",
         "golden")


def test_eastmoney_utils():
    """Test eastmoney_utils shared module."""
    from eastmoney_utils import EM_HEADERS, build_secid, EM_API_HOSTS
    assert EM_HEADERS["User-Agent"]
    assert len(EM_API_HOSTS) == 3
    assert build_secid("600519.SH") == "1.600519"
    assert build_secid("000001.SZ") == "0.000001"
    assert build_secid("00700.HK") is None
    assert build_secid("159740.SZ") == "0.159740"
    print("  eastmoney_utils: OK")


def test_base_fetcher_subclass():
    """Test BaseFetcher subclass contract."""
    from base_fetcher import BaseFetcher
    class TestFetcher(BaseFetcher):
        def fetch(self):
            return {"meta": {"data_source": "test"}, "data": []}
    f = TestFetcher()
    assert f.cache_key_suffix == ""
    assert f.cache_ttl_seconds is None
    print("  base_fetcher subclass: OK")


def test_cache_dir_is_project_relative():
    """Test cache dir migrated from /tmp to project .cache/."""
    from cache_utils import CACHE_DIR
    assert "/tmp/stock-trend-cache" not in CACHE_DIR
    assert ".cache" in CACHE_DIR
    print("  cache dir project-relative: OK")


def test_clean_cache():
    """Test clean_cache() handles empty/non-existent dir."""
    from cache_utils import clean_cache
    import os
    old_dir = os.environ.get("STOCK_TREND_CACHE_DIR")
    os.environ["STOCK_TREND_CACHE_DIR"] = "/tmp/__test_cache_nonexistent__"
    import importlib
    import cache_utils
    importlib.reload(cache_utils)
    result = cache_utils.clean_cache(max_size_mb=1)
    assert result == 0
    if old_dir:
        os.environ["STOCK_TREND_CACHE_DIR"] = old_dir
    else:
        del os.environ["STOCK_TREND_CACHE_DIR"]
    print("  clean_cache empty: OK")


def run_script_unit_tests():
    """Run script-level unit tests for eastmoney_utils, base_fetcher, cache_utils."""
    print("\n✅ 脚本单元测试 (Script Unit)")
    print("=" * 50)
    sys.path.insert(0, str(SCRIPTS_DIR))

    cases = [
        ("SU-eastmoney-utils", test_eastmoney_utils),
        ("SU-base-fetcher", test_base_fetcher_subclass),
        ("SU-cache-dir", test_cache_dir_is_project_relative),
        ("SU-clean-cache", test_clean_cache),
    ]
    for name, fn in cases:
        try:
            fn()
            test(name, True, "ok", "script_unit")
        except Exception as e:
            test(name, False, str(e), "script_unit")


# ========================
# Main
# ========================

def main():
    parser = argparse.ArgumentParser(description="Stock Trend Skill test suite")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--fetch-only", action="store_true", help="Only run fetch tests")
    parser.add_argument("--analyze-only", action="store_true", help="Only run analysis tests")
    args = parser.parse_args()

    print("=" * 50)
    print("🧪 Stock Trend Skill 测试套件")
    print("=" * 50)

    tmpdir = None

    if not args.analyze_only:
        tmpdir = run_fetch_tests()

    if not args.fetch_only:
        if tmpdir is None:
            # Need to fetch data first for analyze tests
            tmpdir = run_fetch_tests()
        run_analyze_tests(tmpdir)

    # New script tests (ETF, capital flow, report generation)
    if not args.fetch_only and not args.analyze_only:
        if tmpdir is None:
            tmpdir = run_fetch_tests()
        run_new_script_tests(tmpdir)

    if not args.fetch_only and not args.analyze_only:
        run_diagnostic_tests()

    # Validate input tests (unit tests, no network needed)
    if not args.fetch_only and not args.analyze_only:
        run_validate_tests()

    # Script unit tests (eastmoney_utils, base_fetcher, cache_utils)
    if not args.fetch_only and not args.analyze_only:
        run_script_unit_tests()

    # Portfolio manager tests
    if not args.fetch_only and not args.analyze_only:
        run_portfolio_integration_tests()

    # Backtest engine tests
    if not args.fetch_only and not args.analyze_only:
        run_backtest_integration_tests()

    # Golden snapshot diff tests
    if not args.fetch_only and not args.analyze_only:
        run_golden_diff_tests()

    # Pipeline & automation tests
    if not args.fetch_only and not args.analyze_only:
        if tmpdir is None:
            tmpdir = run_fetch_tests()
        run_pipeline_tests(tmpdir)

    # Summary
    total = PASSED + FAILED + SKIPPED
    print("\n" + "=" * 50)
    print(f"📋 测试结果汇总: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped (total: {total})")
    print("=" * 50)

    if FAILED > 0:
        print("\n❌ 失败的测试:")
        for r in RESULTS:
            if r["status"] == "FAIL":
                print(f"  - {r['name']}: {r['detail']}")

    # Save results
    results_dir = Path(os.environ.get("TMPDIR", Path.cwd())) / "stock-trend-test-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = str(results_dir / "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"summary": {"passed": PASSED, "failed": FAILED, "skipped": SKIPPED, "total": total},
                    "results": RESULTS}, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: {results_path}")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
