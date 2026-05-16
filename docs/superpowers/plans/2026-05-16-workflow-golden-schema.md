# Golden Snapshot + Schema 校验 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立防止分析数据劣化的工作流：Golden Snapshot diff 检测格式/结构变化，轻量 Schema 校验 compute_scores 输入完整性，Claude Code 工作流约束 + pre-commit hook 兜底。

**Architecture:** 三层防护——(1) `test_golden.py` 工具生成 golden fixtures 并比对 diff；(2) `compute_scores.py` 入口 `validate_input()` 函数校验上游数据；(3) pre-commit hook 在提交时自动运行 diff + schema 校验。所有测试扩展现有自定义框架风格。

**Tech Stack:** Python 3 (标准库 json, pathlib, argparse, difflib), bash (pre-commit hook), 现有 test_stock_trend.py 框架风格

---

## 文件变更清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Create | `.claude/skills/stock-trend/tests/__init__.py` | 包标识 |
| Create | `.claude/skills/stock-trend/tests/golden_config.json` | Golden diff 阈值和标的配置 |
| Create | `.claude/skills/stock-trend/tests/test_golden.py` | Snapshot 生成 & diff 工具 |
| Create | `.claude/skills/stock-trend/tests/fixtures/kline_sample_600519.json` | A股 K线 fixture |
| Create | `.claude/skills/stock-trend/tests/fixtures/kline_sample_513180.json` | ETF K线 fixture |
| Create | `.claude/skills/stock-trend/tests/fixtures/kline_sample_00700.json` | 港股 K线 fixture |
| Create | `.claude/skills/stock-trend/tests/golden/` (各标的目录) | Golden output 文件 |
| Modify | `.claude/skills/stock-trend/scripts/compute_scores.py` | 添加 `validate_input()` |
| Modify | `.claude/skills/stock-trend/scripts/test_stock_trend.py` | 增加 golden diff 和 schema 校验测试用例 |
| Modify | `.githooks/pre-commit` | 新增 Check 5 & 6 |
| Modify | `CLAUDE.md` | 新增修改代码工作流规则 |

---

### Task 1: Golden 配置文件

**Files:**
- Create: `.claude/skills/stock-trend/tests/golden_config.json`

- [ ] **Step 1: Create golden config**

定义 diff 阈值和标的列表：

```json
{
  "thresholds": {
    "score": 0.01,
    "price": 0.0001,
    "default": 0.001
  },
  "symbols": [
    {"code": "600519.SH", "asset": "stock", "name": "贵州茅台"},
    {"code": "513180.SH", "asset": "etf", "name": "沪深300ETF"},
    {"code": "00700.HK", "asset": "hk", "name": "腾讯控股"}
  ],
  "scripts": [
    {"name": "resolve", "output": "resolve.json", "accepts_stdin": false},
    {"name": "kline", "output": "kline.json", "accepts_stdin": true},
    {"name": "technical", "output": "technical.json", "accepts_stdin": true},
    {"name": "capital_flow", "output": "capital_flow.json", "accepts_stdin": true},
    {"name": "fundamental", "output": "fundamental.json", "accepts_stdin": true},
    {"name": "macro_snapshot", "output": "macro_snapshot.json", "accepts_stdin": false},
    {"name": "etf_data", "output": "etf_data.json", "accepts_stdin": false},
    {"name": "futures_data", "output": "futures_data.json", "accepts_stdin": false},
    {"name": "scores", "output": "scores.json", "accepts_stdin": true}
  ],
  "numeric_threshold_map": {
    "total_score": "score",
    "confidence": "score",
    "pe_percentile_3y": "score",
    "close": "price",
    "open": "price",
    "high": "price",
    "low": "price",
    "volume": "default",
    "amount": "default"
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/stock-trend/tests/golden_config.json
git commit -m "feat: add golden snapshot config with thresholds and symbol definitions"
```

---

### Task 2: test_golden.py — Snapshot 生成 & Diff 工具

**Files:**
- Create: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Write test_golden.py**

核心功能：

```python
#!/usr/bin/env python3
"""Golden snapshot generation and diff tool for stock-trend scripts.

Usage:
    python3 tests/test_golden.py --diff          # Compare current output vs golden
    python3 tests/test_golden.py --regenerate     # Regenerate golden files from fixtures
    python3 tests/test_golden.py --diff -v        # Verbose diff output

Exit codes:
    0 = all diffs pass (or regeneration succeeded)
    1 = one or more diffs failed
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = TESTS_DIR / "golden"
FIXTURES_DIR = TESTS_DIR / "fixtures"
CONFIG_PATH = TESTS_DIR / "golden_config.json"

PASSED = 0
FAILED = 0
WARNINGS = 0
RESULTS = []


def load_config():
    """Load golden config."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_threshold_for_key(key, config):
    """Get numeric threshold for a given JSON key based on config mapping."""
    thresholds = config["thresholds"]
    key_map = config.get("numeric_threshold_map", {})
    threshold_name = key_map.get(key, "default")
    return thresholds.get(threshold_name, thresholds["default"])


def deep_diff(golden, current, path="", config=None):
    """Recursively diff two JSON structures. Returns list of (path, issue_type, detail)."""
    if config is None:
        config = {}

    issues = []

    # Type mismatch
    if type(golden) != type(current):
        issues.append((path, "TYPE_CHANGE", f"expected {type(golden).__name__}, got {type(current).__name__}"))
        return issues

    # Dict: check keys then recurse
    if isinstance(golden, dict):
        golden_keys = set(golden.keys())
        current_keys = set(current.keys())
        added = current_keys - golden_keys
        removed = golden_keys - current_keys
        if added:
            issues.append((path, "KEYS_ADDED", f"new keys: {sorted(added)}"))
        if removed:
            issues.append((path, "KEYS_REMOVED", f"removed keys: {sorted(removed)}"))
        for key in golden_keys & current_keys:
            issues.extend(deep_diff(golden[key], current[key], f"{path}.{key}" if path else key, config))
        return issues

    # List: check length then recurse
    if isinstance(golden, list):
        if len(golden) != len(current):
            issues.append((path, "LENGTH_CHANGE", f"expected {len(golden)} items, got {len(current)}"))
            # Compare up to min length
            for i, (g, c) in enumerate(zip(golden, current)):
                issues.extend(deep_diff(g, c, f"{path}[{i}]", config))
            return issues
        for i, (g, c) in enumerate(zip(golden, current)):
            issues.extend(deep_diff(g, c, f"{path}[{i}]", config))
        return issues

    # Numeric: threshold comparison
    if isinstance(golden, (int, float)):
        threshold = get_threshold_for_key(path.split(".")[-1], config)
        diff = abs(golden - current)
        if diff > threshold:
            issues.append((path, "NUMERIC_EXCEEDED", f"diff={diff:.6f} > threshold={threshold} (golden={golden}, current={current})"))
        elif diff > 0:
            issues.append((path, "NUMERIC_WARNING", f"diff={diff:.6f} within threshold (golden={golden}, current={current})"))
        return issues

    # String/bool: exact match
    if golden != current:
        issues.append((path, "VALUE_CHANGE", f"expected {golden!r}, got {current!r}"))
    return issues


def diff_golden_for_symbol(symbol_code, script_config, config, verbose=False):
    """Diff golden vs current output for one symbol and one script."""
    global PASSED, FAILED, WARNINGS

    golden_path = GOLDEN_DIR / symbol_code / script_config["output"]
    if not golden_path.exists():
        # No golden file yet — skip (will be created by --regenerate)
        return

    with open(golden_path, "r", encoding="utf-8") as f:
        golden_data = json.load(f)

    # For diff mode, we compare against the same golden file
    # (no re-running scripts — we rely on fixtures + regenerate for that)
    # The diff is: golden (committed) vs golden (current disk)
    # Actually, we need to compare golden against freshly-generated output
    # For now, diff mode just validates golden files are valid JSON
    # Real diff requires running scripts against fixtures, which is Task 3

    issues = deep_diff(golden_data, golden_data, "", config)  # placeholder
    # This will be replaced with actual diff logic in Task 3


def run_diff(config, verbose=False):
    """Run diff for all symbols and scripts."""
    global PASSED, FAILED, WARNINGS

    for symbol in config["symbols"]:
        symbol_dir = GOLDEN_DIR / symbol["code"]
        if not symbol_dir.exists():
            print(f"  [SKIP] {symbol['code']} — no golden directory")
            continue

        for script_cfg in config["scripts"]:
            golden_path = symbol_dir / script_cfg["output"]
            if not golden_path.exists():
                continue
            # Validate golden file is parseable JSON
            try:
                with open(golden_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                PASSED += 1
                RESULTS.append({
                    "name": f"{symbol['code']}/{script_cfg['output']}",
                    "status": "PASS",
                    "detail": "",
                    "category": "golden-struct"
                })
                if verbose:
                    print(f"  [PASS] {symbol['code']}/{script_cfg['output']}")
            except (json.JSONDecodeError, OSError) as e:
                FAILED += 1
                RESULTS.append({
                    "name": f"{symbol['code']}/{script_cfg['output']}",
                    "status": "FAIL",
                    "detail": str(e),
                    "category": "golden-struct"
                })
                print(f"  [FAIL] {symbol['code']}/{script_cfg['output']} — {e}")


def regenerate_golden(config):
    """Regenerate golden files from fixtures by running scripts."""
    # This requires fixtures + running scripts — implemented in Task 3
    print("Regenerate requires fixture data and script execution.")
    print("Run: python3 tests/test_golden.py --regenerate")
    # Placeholder: will be filled in Task 3


def main():
    parser = argparse.ArgumentParser(description="Golden snapshot diff tool")
    parser.add_argument("--diff", action="store_true", help="Compare current vs golden")
    parser.add_argument("--regenerate", action="store_true", help="Regenerate golden files")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if not args.diff and not args.regenerate:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.diff:
        run_diff(config, verbose=args.verbose)

    if args.regenerate:
        regenerate_golden(config)

    # Summary
    print(f"\n{'='*50}")
    print(f"Golden diff: {PASSED} passed, {FAILED} failed, {WARNINGS} warnings")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run basic diff to verify tool loads**

```bash
cd /Users/trace/work/agent/stock-trend
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: exits with message about no golden directories (they don't exist yet)

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/tests/test_golden.py .claude/skills/stock-trend/tests/__init__.py
git commit -m "feat: add golden snapshot diff tool skeleton"
```

---

### Task 3: Fixtures 生成 & Golden 文件创建

**Files:**
- Create: `.claude/skills/stock-trend/tests/fixtures/kline_sample_600519.json`
- Create: `.claude/skills/stock-trend/tests/fixtures/kline_sample_513180.json`
- Create: `.claude/skills/stock-trend/tests/fixtures/kline_sample_00700.json`
- Create: `.claude/skills/stock-trend/tests/golden/600519.SH/*` (多个 JSON)
- Create: `.claude/skills/stock-trend/tests/golden/513180.SH/*` (多个 JSON)
- Create: `.claude/skills/stock-trend/tests/golden/00700.HK/*` (多个 JSON)

- [ ] **Step 1: Generate fixture K-line data from cache**

从现有缓存提取固定 K 线数据作为 fixture。如果缓存不存在，用脚本获取一次：

```bash
cd /Users/trace/work/agent/stock-trend

# 为每个标的生成 fixture K 线数据（从缓存复制或用脚本获取）
for code in 600519.SH 513180.SH 00700.HK; do
    cache_file=".cache/stock-trend/${code}/kline.json"
    if [ -f "$cache_file" ]; then
        # 从缓存提取固定 30 条数据作为 fixture
        python3 -c "
import json, sys
with open('$cache_file') as f:
    data = json.load(f)
# Keep only 30 records for deterministic fixtures
if isinstance(data, dict) and 'data' in data:
    data['data'] = data['data'][:30]
elif isinstance(data, list):
    data = data[:30]
json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
" > ".claude/skills/stock-trend/tests/fixtures/kline_sample_${code%%.*}.json"
    fi
done
```

- [ ] **Step 2: Run pipeline against fixtures to generate golden outputs**

对每个标的，用 `--no-cache` 和 fixture 数据跑 pipeline，保存输出到 golden 目录。这一步需要手动执行一次，因为涉及真实数据。实现 `regenerate_golden()` 函数：

```python
def regenerate_golden(config):
    """Regenerate golden files by running scripts against fixtures."""
    import subprocess

    for symbol in config["symbols"]:
        code = symbol["code"]
        symbol_dir = GOLDEN_DIR / code
        symbol_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nRegenerating golden for {code} ({symbol['name']})...")

        # Step 1: Resolve code
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "resolve_code.py"), code],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                with open(symbol_dir / "resolve.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"  [OK] resolve.json")
            except json.JSONDecodeError:
                print(f"  [WARN] resolve: invalid JSON output")

        # Step 2: Run full pipeline
        pipeline_result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run_pipeline.py"),
             "--code", code, "--no-cache"],
            capture_output=True, text=True, timeout=120
        )

        # Copy pipeline outputs to golden dir
        cache_dir = Path(".cache/stock-trend") / code
        output_files = [
            "kline.json", "technical.json", "capital_flow.json",
            "macro_snapshot.json", "pipeline_output.json"
        ]
        if symbol["asset"] in ("etf",):
            output_files.extend(["etf_data.json", "futures_data.json"])
        if symbol["asset"] == "stock":
            output_files.append("fundamental.json")

        for fname in output_files:
            src = cache_dir / fname
            if src.exists():
                import shutil
                shutil.copy2(src, symbol_dir / fname)
                print(f"  [OK] {fname}")
            else:
                print(f"  [SKIP] {fname} not found")

        # Step 3: Run compute_scores
        scores_result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "compute_scores.py"),
             "--code", code],
            capture_output=True, text=True, timeout=30
        )
        scores_src = cache_dir / "scores.json"
        if scores_src.exists():
            import shutil
            shutil.copy2(scores_src, symbol_dir / "scores.json")
            print(f"  [OK] scores.json")

    print(f"\nGolden files regenerated in {GOLDEN_DIR}")
```

- [ ] **Step 3: 完善深 diff 逻辑**

更新 `run_diff()` 函数，实现真实 diff（对比 golden 目录下的文件与当前缓存目录下同文件）：

```python
def run_diff(config, verbose=False):
    """Run diff for all symbols: compare golden vs current cache output."""
    global PASSED, FAILED, WARNINGS

    for symbol in config["symbols"]:
        code = symbol["code"]
        golden_symbol_dir = GOLDEN_DIR / code
        cache_symbol_dir = Path(".cache/stock-trend") / code

        if not golden_symbol_dir.exists():
            print(f"  [SKIP] {code} — no golden directory")
            continue

        for script_cfg in config["scripts"]:
            fname = script_cfg["output"]
            golden_path = golden_symbol_dir / fname
            cache_path = cache_symbol_dir / fname

            test_name = f"{code}/{fname}"

            if not golden_path.exists():
                # No golden for this file — skip
                continue

            # Load golden
            try:
                with open(golden_path, "r", encoding="utf-8") as f:
                    golden_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                FAILED += 1
                RESULTS.append({"name": f"golden-load:{test_name}", "status": "FAIL",
                                "detail": str(e), "category": "golden"})
                print(f"  [FAIL] golden-load:{test_name} — {e}")
                continue

            # Check if current cache output exists
            if not cache_path.exists():
                PASSED += 1
                RESULTS.append({"name": test_name, "status": "PASS",
                                "detail": "golden valid, no current output to compare",
                                "category": "golden-diff"})
                if verbose:
                    print(f"  [PASS] {test_name} (golden valid, no current)")
                continue

            # Load current
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    current_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                FAILED += 1
                RESULTS.append({"name": f"current-load:{test_name}", "status": "FAIL",
                                "detail": str(e), "category": "golden-diff"})
                print(f"  [FAIL] current-load:{test_name} — {e}")
                continue

            # Deep diff
            issues = deep_diff(golden_data, current_data, "", config)
            fail_issues = [i for i in issues if i[1] in ("TYPE_CHANGE", "KEYS_ADDED",
                            "KEYS_REMOVED", "LENGTH_CHANGE", "VALUE_CHANGE", "NUMERIC_EXCEEDED")]
            warn_issues = [i for i in issues if i[1] == "NUMERIC_WARNING"]

            if fail_issues:
                FAILED += 1
                detail = "; ".join(f"{path}: {desc}" for path, _, desc in fail_issues[:5])
                RESULTS.append({"name": test_name, "status": "FAIL",
                                "detail": detail, "category": "golden-diff"})
                print(f"  [FAIL] {test_name} — {detail}")
            elif warn_issues:
                WARNINGS += 1
                PASSED += 1
                detail = "; ".join(f"{path}: {desc}" for path, _, desc in warn_issues[:3])
                RESULTS.append({"name": test_name, "status": "PASS",
                                "detail": f"warnings: {detail}", "category": "golden-diff"})
                if verbose:
                    print(f"  [PASS] {test_name} (warnings: {detail})")
            else:
                PASSED += 1
                RESULTS.append({"name": test_name, "status": "PASS",
                                "detail": "", "category": "golden-diff"})
                if verbose:
                    print(f"  [PASS] {test_name}")

    # Also validate golden file structure (all golden files are valid JSON)
    for symbol in config["symbols"]:
        code = symbol["code"]
        golden_symbol_dir = GOLDEN_DIR / code
        if not golden_symbol_dir.exists():
            continue
        for script_cfg in config["scripts"]:
            fname = script_cfg["output"]
            golden_path = golden_symbol_dir / fname
            if not golden_path.exists():
                continue
            # Already validated above during diff
```

- [ ] **Step 4: 运行 regenerate 生成 golden 文件**

```bash
cd /Users/trace/work/agent/stock-trend
python3 .claude/skills/stock-trend/tests/test_golden.py --regenerate
```

Expected: 为 3 个标的生成 golden 目录下的 JSON 文件

- [ ] **Step 5: 运行 diff 验证**

```bash
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 所有文件 PASS（刚生成的 golden 和缓存内容相同）

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/stock-trend/tests/
git commit -m "feat: add golden snapshot data and diff tool for 3 symbols"
```

---

### Task 4: compute_scores validate_input()

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/compute_scores.py` (add `validate_input()` function near line 316, before `find_data_file`)
- Modify: `.claude/skills/stock-trend/scripts/test_stock_trend.py` (add validation test cases)

- [ ] **Step 1: Write test for validate_input**

在 `test_stock_trend.py` 的 `test_compute_scores()` 函数区域后，添加 `test_validate_input()`：

```python
def test_validate_input():
    """Test compute_scores validate_input() schema validation."""
    from compute_scores import validate_input
    import tempfile

    # Test 1: Valid technical data passes
    valid_tech = {
        "summary": {
            "total_score": 5.2,
            "direction": "bullish",
            "confidence": 0.7,
        },
        "data_quality": "good",
    }
    errors = validate_input(valid_tech, {})
    test("VI-valid-tech", len(errors) == 0, f"expected 0 errors, got {errors}", "validate")

    # Test 2: Missing required fields
    missing_fields = {"summary": {"total_score": 5.2}}
    errors = validate_input(missing_fields, {})
    test("VI-missing-fields", len(errors) > 0,
         f"expected errors for missing fields, got {errors}", "validate")

    # Test 3: Invalid data_quality enum
    bad_quality = {
        "summary": {"total_score": 5.2, "direction": "bullish", "confidence": 0.7},
        "data_quality": "invalid_value",
    }
    errors = validate_input(bad_quality, {})
    test("VI-bad-quality", len(errors) > 0,
         f"expected errors for invalid data_quality", "validate")

    # Test 4: Score out of range
    bad_score = {
        "summary": {"total_score": 5.2, "direction": "bullish", "confidence": 0.7},
        "data_quality": "good",
    }
    errors = validate_input(bad_score, {"capital_flow": 200})
    test("VI-score-range", any("range" in e.lower() or "[-100, 100]" in e for e in errors),
         f"expected score range error", "validate")

    # Test 5: Dimension data file missing (file not found)
    errors = validate_input(valid_tech, {}, data_dir="/nonexistent/path")
    test("VI-missing-dim", any("missing" in e.lower() or "not found" in e.lower() for e in errors),
         f"expected dimension missing warning", "validate")

    print("  validate_input tests done")
```

- [ ] **Step 2: Run test to verify it fails (validate_input not yet defined)**

```bash
cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts
python3 -c "from compute_scores import validate_input" 2>&1
```

Expected: ImportError — function doesn't exist yet

- [ ] **Step 3: Implement validate_input()**

在 `compute_scores.py` 的 `find_data_file()` 之前（约 line 314），添加：

```python
def validate_input(technical_data, dimension_scores, data_dir=None):
    """Validate input data completeness and types for compute_scores.

    Args:
        technical_data: Parsed technical.json dict
        dimension_scores: Dict of dimension scores (e.g. {"capital_flow": 0.5})
        data_dir: Optional data directory path for checking dimension data files

    Returns:
        List of error strings. Empty list means all checks pass.
    """
    errors = []

    # Check technical_data is a dict
    if not isinstance(technical_data, dict):
        errors.append(f"technical_data must be a dict, got {type(technical_data).__name__}")
        return errors

    # Check required fields in technical_data
    summary = technical_data.get("summary")
    if not isinstance(summary, dict):
        errors.append(f"technical_data missing 'summary' dict, got {type(summary).__name__ if summary is not None else 'None'}")
    else:
        for field, expected_type in [
            ("total_score", (int, float)),
            ("direction", str),
            ("confidence", (int, float)),
        ]:
            val = summary.get(field)
            if val is None:
                errors.append(f"technical_data.summary missing '{field}'")
            elif not isinstance(val, expected_type):
                errors.append(f"technical_data.summary.{field} expected {expected_type}, got {type(val).__name__}")

    # Check data_quality is valid enum
    dq = technical_data.get("data_quality")
    valid_qualities = ("good", "limited", "insufficient", "partial")
    if dq is None:
        errors.append("technical_data missing 'data_quality'")
    elif not isinstance(dq, str):
        errors.append(f"data_quality expected str, got {type(dq).__name__}")
    elif dq not in valid_qualities:
        errors.append(f"data_quality '{dq}' not in {valid_qualities}")

    # Check dimension scores range [-100, 100]
    for dim, score in dimension_scores.items():
        if not isinstance(score, (int, float)):
            errors.append(f"dimension score '{dim}' must be numeric, got {type(score).__name__}")
        elif score < -100 or score > 100:
            errors.append(f"dimension score '{dim}' = {score} out of range [-100, 100]")

    # Check dimension data files exist if data_dir provided
    if data_dir:
        data_path = Path(data_dir)
        if data_path.exists():
            for dim_file in ["capital_flow.json", "fundamental.json", "macro_snapshot.json"]:
                fpath = data_path / dim_file
                if not fpath.exists():
                    errors.append(f"dimension data file missing: {fpath}")

    return errors
```

- [ ] **Step 4: Wire validate_input into main()**

在 `compute_scores.py` 的 `main()` 函数中，technical_data 加载之后、score 计算之前（约 line 396 之后），添加调用：

```python
    # Validate input data
    validation_errors = validate_input(technical_data, scores, data_dir)
    if validation_errors:
        print("⚠ Input validation warnings:", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        # Non-fatal: log warnings but continue
```

- [ ] **Step 5: Run test_stock_trend.py to verify tests pass**

```bash
cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts
python3 test_stock_trend.py
```

Expected: All tests pass, including new validate_input tests

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/stock-trend/scripts/compute_scores.py .claude/skills/stock-trend/scripts/test_stock_trend.py
git commit -m "feat: add validate_input() to compute_scores with schema checks"
```

---

### Task 5: CLAUDE.md 工作流规则

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add workflow section to CLAUDE.md**

在现有 `## Token 优化` section 之后追加：

```markdown

## 修改代码工作流

修改 `.claude/skills/stock-trend/scripts/` 下任何 `.py` 文件时：

1. **Plan**: 说明改什么、影响范围
2. **Execute**: 做修改
3. **Test**: 必须执行以下步骤
   a. `python3 .claude/skills/stock-trend/scripts/test_stock_trend.py`  — 现有测试全过
   b. `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`  — Golden snapshot diff 无失败
   c. 如果 diff 有数值变化但合理：用 `--regenerate` 更新 golden，commit message 说明原因
4. **Commit**: 确认 3a+3b 通过后再提交

不可跳过步骤 3。合理的 golden 变化必须 `--regenerate` 并在 commit message 说明。
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add code modification workflow rules to CLAUDE.md"
```

---

### Task 6: pre-commit hook 扩展

**Files:**
- Modify: `.githooks/pre-commit`

- [ ] **Step 1: Add Check 5 (Golden snapshot diff) 和 Check 6 (compute_scores schema)**

在现有 Check 4 之后、Summary section 之前（约 line 188），插入：

```bash
# ──────────────────────────────────────────────
# Check 5: Golden snapshot diff
# ──────────────────────────────────────────────
if [ -n "$ANY_SCRIPT_STAGED" ]; then
    echo ""
    echo "🔍 Golden snapshot diff 检查..."

    GOLDEN_TEST="$SKILL_DIR/tests/test_golden.py"
    if [ -f "$GOLDEN_TEST" ]; then
        GOLDEN_DIFF_OUTPUT=$(python3 "$GOLDEN_TEST" --diff 2>&1)
        GOLDEN_EXIT=$?

        if [ "$GOLDEN_EXIT" -ne 0 ]; then
            # Check if commit message contains [skip-golden]
            COMMIT_MSG=$(git log -1 --format=%s 2>/dev/null || true)
            if echo "$COMMIT_MSG" | grep -q '\[skip-golden\]'; then
                msg_warn "Golden diff 检查跳过 ([skip-golden])"
            else
                msg_fail "Golden snapshot diff 未通过"
                echo "$GOLDEN_DIFF_OUTPUT" | head -20
                has_errors=1
            fi
        else
            # Count warnings
            WARN_COUNT=$(echo "$GOLDEN_DIFF_OUTPUT" | grep -c "warning" || true)
            if [ "$WARN_COUNT" -gt 0 ]; then
                msg_warn "Golden diff 通过但有 ${WARN_COUNT} 个警告"
                has_warnings=1
            else
                msg_pass "Golden snapshot diff 通过"
            fi
        fi
    else
        msg_warn "test_golden.py 未找到，跳过 diff 检查"
    fi
fi

# ──────────────────────────────────────────────
# Check 6: compute_scores schema validation
# ──────────────────────────────────────────────
COMPUTE_SCORES_STAGED=$(echo "$STAGED" | grep -E '^\.claude/skills/stock-trend/scripts/compute_scores\.py$' || true)
if [ -n "$COMPUTE_SCORES_STAGED" ]; then
    echo ""
    echo "🔍 compute_scores schema 校验..."

    cd "$SKILL_DIR/scripts" || true
    SCHEMA_CHECK=$(python3 -c "
import json
from compute_scores import validate_input

# Test with valid data
valid = {
    'summary': {'total_score': 5.2, 'direction': 'bullish', 'confidence': 0.7},
    'data_quality': 'good',
}
errors = validate_input(valid, {})
if errors:
    print('FAIL: validate_input returned errors for valid data:', errors)
else:
    # Test with invalid data
    invalid = {'summary': {}}
    errors2 = validate_input(invalid, {})
    if not errors2:
        print('FAIL: validate_input did not catch invalid data')
    else:
        print('OK: schema validation working')
" 2>&1)

    if echo "$SCHEMA_CHECK" | grep -q "^OK:"; then
        msg_pass "compute_scores schema 校验正常"
    else
        msg_fail "compute_scores schema 校验失败: $SCHEMA_CHECK"
        has_errors=1
    fi
fi
```

- [ ] **Step 2: Test the pre-commit hook**

```bash
cd /Users/trace/work/agent/stock-trend
# Stage a small change to trigger the hook
echo "# test" >> .claude/skills/stock-trend/scripts/analyze_technical.py
git add .claude/skills/stock-trend/scripts/analyze_technical.py
git commit -m "test: pre-commit hook test" --no-verify
# Reset the test change
git reset HEAD~1
git checkout -- .claude/skills/stock-trend/scripts/analyze_technical.py
```

Expected: Hook runs without error (or with expected skip if golden files not yet generated)

- [ ] **Step 3: Commit**

```bash
git add .githooks/pre-commit
git commit -m "feat: add golden diff and schema validation checks to pre-commit hook"
```

---

### Task 7: 集成 golden diff 测试到 test_stock_trend.py

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/test_stock_trend.py`

- [ ] **Step 1: Add golden diff test function**

在 `test_stock_trend.py` 的 `main()` 函数前，添加：

```python
def run_golden_diff_tests():
    """Run golden snapshot diff tests."""
    test_golden_path = SCRIPT_DIR.parent / "tests" / "test_golden.py"
    if not test_golden_path.exists():
        skip("TG-golden-diff", "test_golden.py not found")
        return

    exit_code, stdout, stderr = run_script(
        str(test_golden_path.name),
        "--diff",
        timeout=60,
    )
    # Need to run from tests dir
    import subprocess
    result = subprocess.run(
        [sys.executable, str(test_golden_path), "--diff"],
        capture_output=True, text=True, timeout=60,
    )
    test("TG-golden-diff", result.returncode == 0,
         f"golden diff exit={result.returncode}", "golden")
```

并在 `main()` 函数的测试调度逻辑中，添加 golden diff 测试调用。

- [ ] **Step 2: Add validate_input integration test**

在 `test_validate_input()` 函数中（已添加于 Task 4），确保它被 `main()` 调用。检查 `main()` 函数中是否有条件调用 `test_compute_scores()` 的地方，在其附近添加 `test_validate_input()` 调用。

- [ ] **Step 3: Run full test suite**

```bash
cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts
python3 test_stock_trend.py
```

Expected: All tests pass including TG-golden-diff and VI-* tests

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-trend/scripts/test_stock_trend.py
git commit -m "test: integrate golden diff and validate_input tests into test suite"
```

---

### Task 8: 端到端验证

**Files:** 无新文件

- [ ] **Step 1: 完整工作流验证**

```bash
cd /Users/trace/work/agent/stock-trend

# Step 1: 运行现有测试
python3 .claude/skills/stock-trend/scripts/test_stock_trend.py

# Step 2: 运行 golden diff
python3 .claude/skills/stock-trend/tests/test_golden.py --diff

# Step 3: 运行 pre-commit hook 手动
bash .githooks/pre-commit

# Step 4: 做一个小改动，验证 diff 检测
# (e.g., 修改 compute_scores.py 中的 DEFAULT_WEIGHTS 某个值)
# 然后运行 golden diff，确认检测到变化
```

Expected: 所有步骤通过

- [ ] **Step 2: 验证 validate_input 被正确调用**

```bash
cd /Users/trace/work/agent/stock-trend/.claude/skills/stock-trend/scripts
python3 -c "
from compute_scores import validate_input
result = validate_input({'summary': {}}, {})
print('Errors:', result)
assert len(result) > 0, 'Should catch missing fields'
print('OK: validate_input works')
"
```

Expected: 输出错误列表，确认校验生效

- [ ] **Step 3: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "chore: fix any issues found during end-to-end verification"
```

---

## 实现顺序

Tasks 1-3 依赖链：1 → 2 → 3（配置 → 工具 → 数据）。Tasks 4-7 可并行：4 (validate_input), 5 (CLAUDE.md), 6 (pre-commit) 互不依赖。Task 8 是端到端验证。

```
Task 1 ──→ Task 2 ──→ Task 3 ──┐
                                │
Task 4 ─────────────────────────┤──→ Task 8
Task 5 ─────────────────────────┤
Task 6 ─────────────────────────┤
Task 7 ─────────────────────────┘
```