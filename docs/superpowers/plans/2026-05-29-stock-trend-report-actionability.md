# Stock Trend Report Actionability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/stock-trend` reports answer "today what should I do?" without changing the existing report skeleton.

**Architecture:** Keep the current report section order, derive a new actionability block inside `generate_report.py` from existing `technical.summary`, `scores.json.report_params`, and `analysis.events`, then render the same fields in both Markdown and HTML templates. Use the existing `test_stock_trend.py` runner for deterministic TDD coverage and finish with the repo-mandated report + golden validations.

**Tech Stack:** Python 3, simple template engine in `generate_report.py`, Markdown/HTML templates, existing script-style test runners

---

## File structure map

- **Modify:** `.claude/skills/stock-trend/scripts/generate_report.py`
  - Keep rendering flow intact.
  - Add small helper functions for numeric parsing/formatting.
  - Derive actionability context fields from existing report data.
  - Fix the `scores_file` hydration path so `entry_verdict`, `entry_signals`, and `report_params` from `scores.json` actually reach `build_context()`.

- **Modify:** `.claude/skills/stock-trend/assets/report-template.md`
  - Add terminal-friendly `今日动作`, dual-scenario plan, exit rules, and execution window.

- **Modify:** `.claude/skills/stock-trend/assets/report-template.html`
  - Render the same actionability fields as Markdown with stronger visual hierarchy only.

- **Modify:** `.claude/skills/stock-trend/tests/test_stock_trend.py`
  - Add deterministic context-level tests for action-plan derivation.
  - Add deterministic render smoke tests for Markdown + HTML output.
  - Keep everything wired into the existing `python3 .../test_stock_trend.py` runner.

## Implementation notes before touching code

1. Do **not** add a new scoring system in `compute_scores.py`.
2. Do **not** change `/etf-scan`, `/longtou`, or their templates.
3. Prefer deriving from `scores.json.report_params` when present, then fall back to `technical.summary`.
4. Conservative fallback is part of the feature: missing key levels / missing R:R / low confidence must bias to `只观察` or `等回踩`.
5. Because `generate_report.py` lives under `.claude/skills/stock-trend/scripts/`, the final validation must include:
   - `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`

### Task 1: Lock action-plan context with failing tests

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py:353-419`
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py:827-883`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Add a deterministic fixture builder and context-level failing tests**

Add a focused helper near `run_new_script_tests(tmpdir)` so report-action tests do not depend on live fetched data:

```python
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
            "support_levels": [1248.0, 1235.0],
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
            "support_levels": [1248.0, 1235.0],
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
```

Then add a new context-level test body inside `run_new_script_tests(tmpdir)`:

```python
    sys.path.insert(0, str(SCRIPTS_DIR))
    import generate_report

    tech_path, kline_path, scores_path = _write_report_fixture(tmpdir, "actionable")
    with open(scores_path, "r", encoding="utf-8") as f:
        report_params_data = json.load(f)["report_params"]

    args = argparse.Namespace(
        technical=tech_path,
        kline=kline_path,
        etf_data=None,
        capital_flow=None,
        scores=json.dumps({"technical": 2, "capital_flow": 1, "fundamental": 0, "sentiment": 0, "macro": 0}, ensure_ascii=False),
        scores_file=None,
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
        entry_verdict="watch",
        entry_signals=json.dumps(["回踩支撑不破", "量能回补"], ensure_ascii=False),
        analysis=json.dumps({
            "core_conflict": "趋势偏多，但当前位置略高于理想回踩位。",
            "events": [{"date": "2026-06-10", "event": "股东大会", "impact": "事件前若未回踩则放弃计划"}],
            "advice": ["回踩 1248-1252 分批试仓", "若放量站稳 1288 再考虑追踪"],
        }, ensure_ascii=False),
        chart=None,
        fundamental_data=None,
        macro_data=None,
        futures_data=None,
        chip_distribution=None,
        output_md=None,
        output_html=None,
        code=None,
        data_dir=None,
        report_params_data=report_params_data,
    )

    context = generate_report.build_context(args)
    test("TF-RPT-CTX-01: 今日动作标签", context.get("今日动作标签") == "等回踩",
         f"label={context.get('今日动作标签')}", "report")
    test("TF-RPT-CTX-02: 场景A标题", context.get("场景A标题") == "场景 A：继续上冲",
         f"title={context.get('场景A标题')}", "report")
    test("TF-RPT-CTX-03: 场景B动作含分批试仓",
         "分批试仓" in str(context.get("场景B动作", "")),
         str(context.get("场景B动作")), "report")
    test("TF-RPT-CTX-04: 执行时间窗含事件日期",
         "2026-06-10" in str(context.get("执行时间窗", "")),
         str(context.get("执行时间窗")), "report")
```

- [ ] **Step 2: Run the existing test runner and confirm the new assertions fail**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected: FAIL on `TF-RPT-CTX-*` because `generate_report.build_context()` does not yet produce the new action-plan keys.

- [ ] **Step 3: Keep the failing test in the tree and commit it**

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "test(report): lock action plan context"
```

- [ ] **Step 4: If the suite is too noisy, capture just the failing names in notes**

Use the final summary lines from the runner to note which `TF-RPT-CTX-*` cases failed before implementation. Do not remove the tests.

### Task 2: Implement action-plan derivation in `generate_report.py`

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py:80-180`
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py:180-552`
- Modify: `.claude/skills/stock-trend/scripts/generate_report.py:650-694`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Add tiny parsing/formatting helpers and the action-plan builder**

Insert helpers after `direction_symbol()`:

```python
def _safe_float(value):
    if value in (None, "", "—"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_numeric(values):
    for value in values or []:
        num = _safe_float(value)
        if num is not None:
            return num
    return None


def _fmt_price(value):
    num = _safe_float(value)
    return "—" if num is None else f"{num:.2f}"


def _build_time_window(events):
    if not events:
        return "未来 5-10 个交易日有效；若始终未触发则重新评估。"
    first = events[0]
    label = first.get("date") or first.get("name") or "关键事件"
    return f"未来 5-10 个交易日有效；若 {label} 前仍未触发则该计划失效，需重新评估。"


def build_action_plan(direction, confidence, latest_close, report_params, analysis_data):
    support = _first_numeric(report_params.get("support_levels"))
    resistance = _first_numeric(report_params.get("resistance_levels"))
    stop_loss = _safe_float(report_params.get("stop_loss"))
    target_1 = _safe_float(report_params.get("target_conservative"))
    target_2 = _safe_float(report_params.get("target_moderate"))
    rr_ratio = _safe_float(report_params.get("risk_reward_ratio"))
    close = _safe_float(latest_close)
    events = analysis_data.get("events", []) if analysis_data else []

    bearish_bias = "空" in (direction or "")
    low_conviction = confidence == "低" or rr_ratio is None or rr_ratio < 1.5
    near_target = close is not None and target_1 is not None and close >= target_1 * 0.98
    near_support = close is not None and support is not None and abs(close - support) / support <= 0.015

    if near_target:
        label = "分批止盈"
        summary = "已有仓位优先按目标位落袋，新开仓赔率一般。"
    elif bearish_bias or low_conviction or support is None or stop_loss is None:
        label = "只观察"
        summary = "当前位置胜率或赔率不足，等待更多确认。"
    elif near_support:
        label = "可低吸"
        summary = f"当前位置接近支撑 {_fmt_price(support)}，可按计划小仓试错。"
    else:
        label = "等回踩"
        summary = f"当前位置偏离支撑 {_fmt_price(support)}，先不追高，等回踩再看。"

    return {
        "操作计划": True,
        "今日动作标签": label,
        "今日动作摘要": f"{label}：{summary}",
        "今日动作说明": summary,
        "场景A标题": "场景 A：继续上冲",
        "场景A条件": f"价格继续向压力位 {_fmt_price(resistance)} 推进",
        "场景A动作": (
            f"仅在放量站稳 {_fmt_price(resistance)} 后考虑追踪；未确认前只观察。"
            if resistance is not None else
            "若继续上冲，等待放量确认后再评估，不直接追高。"
        ),
        "场景B标题": "场景 B：回调到位",
        "场景B条件": f"价格回踩支撑区 {_fmt_price(support)} 附近并止跌",
        "场景B动作": (
            f"先开 1/2 仓试错；若二次确认不破位再补齐，统一止损放在 {_fmt_price(stop_loss)}。"
            if support is not None and stop_loss is not None else
            "缺少关键价位，回调场景仅保留观察。"
        ),
        "退出条件1": f"到达 TP1 {_fmt_price(target_1)}",
        "退出动作1": "减仓 1/3，锁定首段利润",
        "退出条件2": f"到达 TP2 {_fmt_price(target_2)}",
        "退出动作2": "再减仓 1/3，剩余仓位跟踪趋势",
        "退出条件3": f"跌破止损位 {_fmt_price(stop_loss)}",
        "退出动作3": "视为计划失效，执行止损",
        "执行时间窗": _build_time_window(events),
    }
```

- [ ] **Step 2: Hydrate `scores.json.report_params` and existing entry fields before calling `build_context()`**

In the `if args.scores_file:` block, replace the current default-based logic with explicit `None` checks and save the raw report params for reuse:

```python
    parser.add_argument("--entry-verdict", help="Entry timing: ready/watch/wait/avoid")
    parser.add_argument("--entry-signals", help="JSON array of entry confirmation signal strings")
```

Then update the `scores_file` hydration block:

```python
            rp = scores_data.get("report_params", {})
            args.report_params_data = rp
            if args.entry_verdict is None:
                args.entry_verdict = rp.get("entry_verdict", "wait")
            if args.entry_signals is None:
                args.entry_signals = json.dumps(rp.get("entry_signals", []), ensure_ascii=False)
```

After the `scores_file` and `pipeline` hydration blocks, normalize empty values before `build_context(args)`:

```python
    if args.entry_verdict is None:
        args.entry_verdict = "wait"
    if args.entry_signals is None:
        args.entry_signals = "[]"
    if not hasattr(args, "report_params_data"):
        args.report_params_data = {}
```

- [ ] **Step 3: Merge `report_params` into `build_context()` and expose the new fields**

Inside `build_context(args)`:

```python
    report_params = getattr(args, "report_params_data", {}) or {}

    stop_loss = report_params.get("stop_loss", summary.get("stop_loss", "—"))
    target_conservative = report_params.get("target_conservative", summary.get("target_conservative"))
    target_moderate = report_params.get("target_moderate", summary.get("target_moderate") or summary.get("target", "—"))
    target_aggressive = report_params.get("target_aggressive", summary.get("target_aggressive"))
    rr_ratio = report_params.get("risk_reward_ratio", summary.get("risk_reward_ratio", "—"))
    favorable_rr = report_params.get("favorable_rr", summary.get("favorable_rr"))
    position_sizing = report_params.get("position_sizing", summary.get("position_sizing", "—"))
    max_drawdown = report_params.get("max_drawdown_pct", summary.get("max_drawdown_pct", "—"))
    support_levels = report_params.get("support_levels", summary.get("support_levels", []))
    resistance_levels = report_params.get("resistance_levels", summary.get("resistance_levels", []))
```

Parse `analysis_data` **before** building the final `context`, then merge the derived action plan:

```python
    analysis_data = None
    if args.analysis:
        try:
            analysis_data = json.loads(args.analysis)
        except (json.JSONDecodeError, TypeError):
            pass

    action_plan = build_action_plan(
        direction=direction,
        confidence=confidence,
        latest_close=latest_close,
        report_params=report_params,
        analysis_data=analysis_data,
    )
```

Add the new fields to `context`:

```python
        "操作计划": action_plan["操作计划"],
        "今日动作标签": action_plan["今日动作标签"],
        "今日动作摘要": action_plan["今日动作摘要"],
        "今日动作说明": action_plan["今日动作说明"],
        "场景A标题": action_plan["场景A标题"],
        "场景A条件": action_plan["场景A条件"],
        "场景A动作": action_plan["场景A动作"],
        "场景B标题": action_plan["场景B标题"],
        "场景B条件": action_plan["场景B条件"],
        "场景B动作": action_plan["场景B动作"],
        "退出条件1": action_plan["退出条件1"],
        "退出动作1": action_plan["退出动作1"],
        "退出条件2": action_plan["退出条件2"],
        "退出动作2": action_plan["退出动作2"],
        "退出条件3": action_plan["退出条件3"],
        "退出动作3": action_plan["退出动作3"],
        "执行时间窗": action_plan["执行时间窗"],
```

Keep the existing `综合研判` block below this change; do not remove it.

- [ ] **Step 4: Re-run the report suite and make sure the new context tests pass**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected: the new `TF-RPT-CTX-*` cases pass, but render-focused assertions should still fail until the templates are updated.

- [ ] **Step 5: Commit the context implementation**

```bash
git add .claude/skills/stock-trend/scripts/generate_report.py .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "feat(report): derive actionability context"
```

### Task 3: Add failing render smoke tests for Markdown and HTML

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py:388-419`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`

- [ ] **Step 1: Replace the current report smoke test with deterministic `--scores-file` coverage**

Rewrite the `TF-RPT-01` block in `run_new_script_tests(tmpdir)` so it uses the fixture helper instead of live `ta01.json` / `tf01.json`:

```python
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
```

Add deterministic assertions for the new copy:

```python
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
```

- [ ] **Step 2: Add a degraded-data render assertion**

Use the same helper with a weak setup:

```python
    weak_tech_path, weak_kline_path, weak_scores_path = _write_report_fixture(
        tmpdir,
        "render_weak",
        confidence="低",
        rr_ratio=None,
        latest_close=1298.0,
    )
```

Generate only Markdown and assert the fallback language:

```python
    test("TF-RPT-02: 低置信度默认只观察",
         "只观察" in weak_md_content,
         weak_md_content[:200], "report")
```

- [ ] **Step 3: Run the existing runner and confirm the new render assertions fail**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

Expected: FAIL on `TF-RPT-01c` through `TF-RPT-01i` because the templates do not yet render the new actionability fields.

- [ ] **Step 4: Commit the failing render tests**

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "test(report): lock actionability rendering"
```

### Task 4: Render the actionability block in Markdown + HTML and run required validations

**Files:**
- Modify: `.claude/skills/stock-trend/assets/report-template.md:1-138`
- Modify: `.claude/skills/stock-trend/assets/report-template.html:15-161`
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py` (only if a selector name or expected string must be adjusted)
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`
- Test: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Update the Markdown template**

Add the new line below the conclusion:

```md
**今日动作**: {{今日动作摘要}}
```

Then expand the current `## 三、操作建议区间` block to keep the existing price table and append an actionability section:

```md
### 执行摘要

**{{今日动作标签}}**：{{今日动作说明}}

| 场景 | 触发条件 | 执行动作 |
|---|---|---|
| {{场景A标题}} | {{场景A条件}} | {{场景A动作}} |
| {{场景B标题}} | {{场景B条件}} | {{场景B动作}} |

| 退出条件 | 执行动作 |
|---|---|
| {{退出条件1}} | {{退出动作1}} |
| {{退出条件2}} | {{退出动作2}} |
| {{退出条件3}} | {{退出动作3}} |

**执行时间窗**: {{执行时间窗}}
```

Do not delete the existing support/resistance/current/stop/target tables.

- [ ] **Step 2: Update the HTML template with the same fields**

Add a summary row below the existing conclusion banner:

```html
<p class="meta" style="margin-top:8px">今日动作: <strong>{{今日动作摘要}}</strong></p>
```

Add a compact action-plan card after the current price/target tables:

```html
<div class="analysis">
  <h3>执行摘要</h3>
  <p><strong>{{今日动作标签}}</strong>：{{今日动作说明}}</p>
</div>

<table>
  <thead><tr><th>场景</th><th>触发条件</th><th>执行动作</th></tr></thead>
  <tbody>
    <tr><td>{{场景A标题}}</td><td>{{场景A条件}}</td><td>{{场景A动作}}</td></tr>
    <tr><td>{{场景B标题}}</td><td>{{场景B条件}}</td><td>{{场景B动作}}</td></tr>
  </tbody>
</table>

<table>
  <thead><tr><th>退出条件</th><th>执行动作</th></tr></thead>
  <tbody>
    <tr><td>{{退出条件1}}</td><td>{{退出动作1}}</td></tr>
    <tr><td>{{退出条件2}}</td><td>{{退出动作2}}</td></tr>
    <tr><td>{{退出条件3}}</td><td>{{退出动作3}}</td></tr>
  </tbody>
</table>

<p class="meta" style="margin:8px 0">执行时间窗: <strong>{{执行时间窗}}</strong></p>
```

If you need extra styling, add a tiny class block near the existing `.analysis` / `.spec` styles instead of introducing a new layout system.

- [ ] **Step 3: Run the required validations**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected:

1. `test_stock_trend.py` passes with the new `TF-RPT-CTX-*`, `TF-RPT-01*`, and `TF-RPT-02` checks.
2. `test_golden.py --diff` either stays clean or only reports intentional report-adjacent changes that you can explain.

- [ ] **Step 4: Regenerate golden snapshots only if the diff proves the new output path changed committed JSON**

If `python3 .claude/skills/stock-trend/tests/test_golden.py --diff` shows legitimate, intended changes caused by the report-actionability work, regenerate and rerun:

```bash
python3 .claude/skills/stock-trend/tests/test_golden.py --regenerate
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Only do this when the diff is understood and accepted.

- [ ] **Step 5: Commit the finished rollout**

If no golden files changed:

```bash
git add .claude/skills/stock-trend/scripts/generate_report.py \
        .claude/skills/stock-trend/assets/report-template.md \
        .claude/skills/stock-trend/assets/report-template.html \
        .claude/skills/stock-trend/tests/test_stock_trend.py
git commit -m "feat(report): add actionability guidance"
```

If golden files changed too:

```bash
git add .claude/skills/stock-trend/scripts/generate_report.py \
        .claude/skills/stock-trend/assets/report-template.md \
        .claude/skills/stock-trend/assets/report-template.html \
        .claude/skills/stock-trend/tests/test_stock_trend.py \
        .claude/skills/stock-trend/tests/golden
git commit -m "feat(report): add actionability guidance"
```

## Self-review checklist for whoever executes this plan

Before starting execution, quickly verify:

1. The plan only changes `/stock-trend` report generation, not ETF/longtou templates.
2. `generate_report.py` consumes `scores.json.report_params` instead of accidentally ignoring it.
3. Both Markdown and HTML render the same actionability facts.
4. Low-confidence / weak-R:R cases explicitly downgrade to `只观察`.
5. The final validation includes both repo-required commands from `CLAUDE.md`.
