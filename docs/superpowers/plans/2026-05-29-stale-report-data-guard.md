# Stale Report Data Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent `/stock-trend` reports from displaying stale prices or stale technical levels when the current K-line fetch fails.

**Architecture:** Fix this at two layers. `run_pipeline.py` must stop publishing stale downstream artifacts when the current K-line step fails, and `generate_report.py` must defensively prefer validated K-line data for "当前价" while suppressing stale technical context when the K-line is invalid or inconsistent. Tests reproduce the exact failure mode: stale `technical.json` says 7.46 while current K-line is missing/error.

**Tech Stack:** Python 3 standard library, existing custom test runner in `.claude/skills/stock-trend/tests/test_stock_trend.py`, existing report templates.

---

## File Structure

- Modify `.claude/skills/stock-trend/scripts/run_pipeline.py`
  - Add small helpers to validate K-line payloads and delete stale downstream outputs.
  - Track whether `technical.json` and `chip_distribution.json` were produced in the current run.
  - Only include those paths in `pipeline_output.json` when they are fresh for the current run.

- Modify `.claude/skills/stock-trend/scripts/generate_report.py`
  - Add helpers to read latest close/date from valid K-line rows.
  - Prefer latest K-line close over `technical.latest.close`.
  - If a K-line file is present but invalid/error, do not fall back to old technical current price; show `—` and emit a data-quality warning.
  - If K-line and technical dates both exist but disagree, keep the current price from K-line and mark technical context as stale for warnings.

- Modify `.claude/skills/stock-trend/tests/test_stock_trend.py`
  - Add failing regression tests for stale pipeline outputs and stale report current price.
  - Keep tests deterministic; do not depend on live EastMoney/Sina/Tencent endpoints.

---

### Task 1: Add regression tests for stale pipeline and report price

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Add a failing test helper for stale technical fixtures**

Add these helpers near the existing report fixture helpers in `.claude/skills/stock-trend/tests/test_stock_trend.py`:

```python
def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _build_stale_technical_fixture(ts_code="300241.SZ", close=7.46):
    return {
        "meta": {
            "ts_code": ts_code,
            "data_source": "eastmoney",
            "analysis_date": "20260528",
            "data_points": 243,
        },
        "latest": {
            "trade_date": "20260528",
            "close": close,
        },
        "summary": {
            "total_score": 0.76,
            "direction": "震荡偏多",
            "confidence": "低",
            "support_levels": [7.42, 6.76, 6.0],
            "resistance_levels": [7.5, 8.28, 9.0],
            "stop_loss": 6.68,
            "target": 9.0,
            "risk_reward_ratio": 1.97,
            "position_sizing": "轻仓(20-30%)",
        },
        "patterns": [],
    }


def _build_error_kline_fixture(ts_code="300241.SZ"):
    return {
        "meta": {
            "ts_code": ts_code,
            "asset": "E",
            "freq": "D",
            "adj": "qfq",
            "data_source": "error",
            "record_count": 0,
            "error": "all K-line sources failed",
        },
        "data": [],
    }
```

- [ ] **Step 2: Add the failing report regression test**

Add this test near the existing `TF-RPT-*` report tests:

```python
def test_report_does_not_use_stale_technical_price_when_kline_failed(tmpdir):
    """Regression: 300241 report showed 2026-05-28 close after 2026-05-29 K-line failed."""
    technical_path = os.path.join(tmpdir, "stale_technical.json")
    kline_path = os.path.join(tmpdir, "error_kline.json")
    output_md = os.path.join(tmpdir, "stale_report.md")

    _write_json(technical_path, _build_stale_technical_fixture())
    _write_json(kline_path, _build_error_kline_fixture())

    rc, stdout, stderr = run_script(
        "generate_report.py",
        "--technical", technical_path,
        "--kline", kline_path,
        "--ts-code", "300241.SZ",
        "--stock-name", "瑞丰光电",
        "--date", "2026-05-29",
        "--output-md", output_md,
    )

    if rc != 0 or not os.path.exists(output_md):
        test("TF-RPT-STALE-01: K线失败不复用旧当前价", False, f"exit_code={rc}, stderr={stderr[:200]}", "report")
        return

    content = open(output_md, encoding="utf-8").read()
    test(
        "TF-RPT-STALE-01: K线失败不复用旧当前价",
        "| 当前价 | — |" in content and "| 当前价 | 7.46 |" not in content,
        content[content.find("| 当前价 |"):content.find("| 当前价 |") + 80],
        "report",
    )
    test(
        "TF-RPT-STALE-02: K线失败报告提示数据不可用",
        "K线数据不可用" in content or "技术分析数据可能过期" in content,
        content[:500],
        "report",
    )
```

- [ ] **Step 3: Add the failing pipeline helper regression test**

Add this test near `run_pipeline_tests`:

```python
def test_pipeline_output_files_do_not_publish_stale_technical(tmpdir):
    """Failed K-line runs must not point pipeline_output.json at stale technical.json."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from run_pipeline import build_output_files

    output_dir = Path(tmpdir)
    technical_path = str(output_dir / "technical.json")
    chip_path = str(output_dir / "chip_distribution.json")
    _write_json(technical_path, _build_stale_technical_fixture())
    _write_json(chip_path, {"avg_cost": 7.1})

    files = build_output_files(
        output_dir=output_dir,
        kline_path=str(output_dir / "kline.json"),
        kline_available=False,
        technical_available=False,
        chip_available=False,
        is_etf=False,
        no_etf=False,
        no_capital=False,
        no_fundamental=False,
        no_macro=False,
        no_futures=False,
        no_index_valuation=False,
        asset="E",
    )

    test(
        "TP-PL-STALE-01: K线失败不发布旧technical路径",
        files.get("technical") is None and files.get("chip_distribution") is None,
        f"technical={files.get('technical')}, chip={files.get('chip_distribution')}",
        "pipeline",
    )
```

- [ ] **Step 4: Wire the new tests into existing runners**

Call the new tests from existing runners:

```python
def run_new_script_tests():
    ...
    test_report_does_not_use_stale_technical_price_when_kline_failed(tmpdir)


def run_pipeline_tests(tmpdir):
    ...
    test_pipeline_output_files_do_not_publish_stale_technical(tmpdir)
```

- [ ] **Step 5: Run tests to verify they fail**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected:

```text
[FAIL] TF-RPT-STALE-01: K线失败不复用旧当前价
[FAIL] TP-PL-STALE-01: K线失败不发布旧technical路径
```

- [ ] **Step 6: Commit failing tests**

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "test(report): reproduce stale price regression"
```

---

### Task 2: Stop pipeline from publishing stale downstream outputs

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/run_pipeline.py`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Add helper functions**

Add these functions after `read_json` in `.claude/skills/stock-trend/scripts/run_pipeline.py`:

```python
def is_successful_kline(kline_data):
    """Return True only when the current K-line payload has usable rows."""
    if not isinstance(kline_data, dict):
        return False
    if kline_data.get("meta", {}).get("data_source") == "error":
        return False
    rows = kline_data.get("data")
    return isinstance(rows, list) and len(rows) > 0


def remove_stale_file(path, label, errors):
    """Delete stale downstream output so later report generation cannot reuse it."""
    if not path or not os.path.exists(path):
        return False
    try:
        os.remove(path)
        errors.append(f"Removed stale {label}: {path}")
        return True
    except OSError as exc:
        errors.append(f"Failed to remove stale {label}: {path}: {exc}")
        return False


def build_output_files(
    output_dir,
    kline_path,
    kline_available,
    technical_available,
    chip_available,
    is_etf,
    no_etf,
    no_capital,
    no_fundamental,
    no_macro,
    no_futures,
    no_index_valuation,
    asset,
):
    """Build pipeline output file map using freshness flags from this run."""
    return {
        "kline": kline_path,
        "technical": str(output_dir / "technical.json") if technical_available else None,
        "etf_data": str(output_dir / "etf_data.json") if is_etf and not no_etf else None,
        "capital_flow": str(output_dir / "capital_flow.json") if not no_capital else None,
        "fundamental": str(output_dir / "fundamental.json") if not no_fundamental and asset != "FD" else None,
        "macro_snapshot": str(output_dir / "macro_snapshot.json") if not no_macro else None,
        "futures_data": str(output_dir / "futures_data.json") if is_etf and not no_futures else None,
        "index_valuation": str(output_dir / "index_valuation.json") if is_etf and not no_index_valuation else None,
        "chip_distribution": str(output_dir / "chip_distribution.json") if chip_available else None,
    }
```

- [ ] **Step 2: Track freshness flags in `main()`**

Initialize these after `results = {}`:

```python
kline_available = False
technical_available = False
chip_available = False
chip_result = {"success": False}
```

Replace the existing `if kline_data:` block after K-line fallback with:

```python
if kline_data:
    data_source = kline_data.get("meta", {}).get("data_source", "unknown")
    record_count = kline_data.get("meta", {}).get("record_count", 0)
    kline_available = is_successful_kline(kline_data)
    print(f"  K-line data: {data_source}, {record_count} records")
    results["kline"] = {
        "data_source": data_source,
        "record_count": record_count,
    }
    if not kline_available:
        errors.append("K-line data unavailable or empty")
else:
    errors.append("K-line data unavailable")
    results["kline"] = {"data_source": "error", "record_count": 0}
```

- [ ] **Step 3: Use `kline_available` for dependent steps**

Change chip and technical guards:

```python
if kline_available:
    ...
else:
    print(f"[3/5] Skipping chip distribution (no K-line data)")
```

```python
if kline_available:
    ...
else:
    print(f"[3.5/5] Skipping technical analysis (no K-line data)")
```

Set `chip_available = True` only after loading a valid chip output:

```python
if chip_data and "error" not in chip_data:
    chip_available = True
    ...
```

Set `technical_available = True` only after loading a technical output whose current run succeeded:

```python
if tech_result["success"] and tech_data:
    technical_available = True
    summary = tech_data.get("summary", {})
    ...
```

- [ ] **Step 4: Delete stale dependent files when K-line is unavailable**

After the technical-analysis skip block, add:

```python
if not kline_available:
    remove_stale_file(technical_path, "technical analysis", errors)
    remove_stale_file(chip_distribution_path, "chip distribution", errors)
```

- [ ] **Step 5: Build `pipeline_output["output_files"]` from freshness flags**

Replace the literal `output_files` dict at the end of `run_pipeline.py` with:

```python
"output_files": build_output_files(
    output_dir=output_dir,
    kline_path=kline_path,
    kline_available=kline_available,
    technical_available=technical_available,
    chip_available=chip_available,
    is_etf=is_etf,
    no_etf=args.no_etf,
    no_capital=args.no_capital,
    no_fundamental=args.no_fundamental,
    no_macro=args.no_macro,
    no_futures=args.no_futures,
    no_index_valuation=args.no_index_valuation,
    asset=asset,
),
```

- [ ] **Step 6: Run targeted pipeline regression**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected: `TP-PL-STALE-01` passes. `TF-RPT-STALE-01` may still fail until Task 3.

- [ ] **Step 7: Commit pipeline fix**

```bash
git add .claude/skills/stock-trend/scripts/run_pipeline.py .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "fix(pipeline): avoid stale technical outputs"
```

---

### Task 3: Make report current price come from validated K-line data

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Add K-line freshness helpers**

Add these helpers before `build_context` in `.claude/skills/stock-trend/scripts/generate_report.py`:

```python
def _normalize_trade_date(value):
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("-", "")[:8]


def _valid_kline_rows(kline):
    if not isinstance(kline, dict):
        return []
    if kline.get("meta", {}).get("data_source") == "error":
        return []
    rows = kline.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("close") is not None]


def _latest_kline_row(kline):
    rows = _valid_kline_rows(kline)
    if not rows:
        return None
    return rows[-1]


def _technical_latest_date(technical):
    latest = technical.get("latest", {}) if isinstance(technical, dict) else {}
    meta = technical.get("meta", {}) if isinstance(technical, dict) else {}
    return _normalize_trade_date(
        latest.get("trade_date")
        or latest.get("date")
        or meta.get("end_date")
        or meta.get("analysis_date")
    )


def _kline_latest_date(kline):
    row = _latest_kline_row(kline)
    if not row:
        return ""
    return _normalize_trade_date(row.get("trade_date") or row.get("date") or row.get("datetime"))


def _latest_close_for_report(technical, kline, kline_path_present):
    """Prefer validated K-line close; avoid stale technical close when K-line failed."""
    row = _latest_kline_row(kline)
    if row:
        return row.get("close"), False
    if kline_path_present:
        return None, bool(technical.get("latest", {}).get("close"))
    return technical.get("latest", {}).get("close"), False
```

- [ ] **Step 2: Use the helper in `build_context`**

Replace:

```python
# Latest close price
latest_close = technical.get("latest", {}).get("close", None)
```

with:

```python
# Latest close price. Reports must not reuse stale technical close after K-line failure.
latest_close, stale_technical_price = _latest_close_for_report(
    technical,
    kline,
    kline_path_present=bool(args.kline),
)
technical_date = _technical_latest_date(technical)
kline_date = _kline_latest_date(kline)
stale_technical_date = bool(technical_date and kline_date and technical_date != kline_date)
```

- [ ] **Step 3: Surface stale data warnings**

Replace:

```python
"数据质量警告": summary.get("risk_reward_warning") or ("⚠️ 数据不足，分析可靠性有限" if meta.get("data_points", 999) < 60 else ""),
```

with:

```python
"数据质量警告": (
    "⚠️ K线数据不可用，已禁用旧技术分析当前价"
    if stale_technical_price
    else (
        f"⚠️ 技术分析数据可能过期：技术面日期{technical_date}，K线日期{kline_date}"
        if stale_technical_date
        else summary.get("risk_reward_warning")
        or ("⚠️ 数据不足，分析可靠性有限" if meta.get("data_points", 999) < 60 else "")
    )
),
```

- [ ] **Step 4: Keep action plan conservative when current price is missing**

No extra code is required if `latest_close` becomes `None`: existing `build_action_plan(...)` treats missing/invalid price as `只观察`. Confirm this by reading the existing helper before editing.

- [ ] **Step 5: Run targeted report regression**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected:

```text
[PASS] TF-RPT-STALE-01: K线失败不复用旧当前价
[PASS] TF-RPT-STALE-02: K线失败报告提示数据不可用
```

- [ ] **Step 6: Commit report fix**

```bash
git add .claude/skills/stock-trend/scripts/generate_report.py .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "fix(report): guard stale current prices"
```

---

### Task 4: Verify the real 300241 failure mode and full suite

**Files:**
- No production edits expected.
- Use existing files under `.cache/stock-trend/300241/` only for local verification; do not commit generated reports or cache files.

- [ ] **Step 1: Reproduce with current stale 300241 cache**

Run:

```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --code 300241 \
  --date 2026-05-29 \
  --output-md /tmp/300241-stale-check.md \
  --output-html /tmp/300241-stale-check.html
```

Expected after the fix:

```text
/tmp/300241-stale-check.md exists
```

Then inspect:

```bash
grep -n "当前价\\|K线数据不可用\\|技术分析数据可能过期" /tmp/300241-stale-check.md
```

Expected:

```text
当前价 | — |
⚠️ K线数据不可用，已禁用旧技术分析当前价
```

- [ ] **Step 2: Verify a valid K-line report still shows latest K-line close**

Run:

```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --technical .cache/stock-trend/600519/technical.json \
  --kline .cache/stock-trend/600519/kline.json \
  --ts-code 600519.SH \
  --stock-name 贵州茅台 \
  --date 2026-05-29 \
  --output-md /tmp/600519-price-check.md
```

Then compare latest K-line close and report current price:

```bash
python3 - <<'PY'
import json, re
k=json.load(open('.cache/stock-trend/600519/kline.json'))
expected=str(k['data'][-1]['close'])
report=open('/tmp/600519-price-check.md', encoding='utf-8').read()
print('expected', expected)
print('report_line', next(line for line in report.splitlines() if '| 当前价 |' in line))
assert expected in report
PY
```

Expected:

```text
expected <latest close from kline>
report_line | 当前价 | <same close> |
```

- [ ] **Step 3: Run required stock-trend validation**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected:

```text
0 failed
```

- [ ] **Step 4: Commit any final test-only adjustment**

Only if Task 4 required small test expectation changes:

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "test(report): cover stale data guard"
```

---

## Self-Review

- Spec coverage: The plan covers the observed 300241 failure, the pipeline source of stale file references, and the report-layer defense against direct stale technical input.
- Placeholder scan: No placeholder markers or unspecified test steps remain.
- Type consistency: New helper names are used consistently: `is_successful_kline`, `remove_stale_file`, `build_output_files`, `_latest_close_for_report`.
- Test strategy: Tests are deterministic and do not require live quote endpoints. The real 300241 cache is used only for local verification, not as a committed fixture.
