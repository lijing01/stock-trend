# Daily Candidates Wyckoff Mini-Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each `/candidates` short-term Wyckoff result as a Phase A–E label with a Chinese explanation in JSON, Markdown, and HTML.

**Architecture:** Keep the existing phase detector and trading gates unchanged. Add one deterministic presentation mapping from the detected primary phase and sub-phase to a mini-phase object, propagate it through the scanner candidate payload, and render that object in both report formats.

**Tech Stack:** Python 3.10+, unittest, existing Wyckoff/candidate scan modules.

---

### Task 1: Define and prove mini-phase semantics

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_minor_phase_maps_accumulation_spring_to_phase_c(self):
    minor = build_minor_phase(PHASE_ACCUMULATION, SUB_SPRING)
    self.assertEqual(minor["code"], "C")
    self.assertEqual(minor["name"], "阶段C：测试")
    self.assertIn("震仓", minor["description"])
```

```python
def test_build_minor_phase_marks_unmapped_signal_unconfirmed(self):
    minor = build_minor_phase(PHASE_UNKNOWN, "")
    self.assertEqual(minor["code"], "-")
    self.assertEqual(minor["name"], "小级别阶段未确认")
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: FAIL because `build_minor_phase` is not defined.

- [ ] **Step 3: Write the minimal implementation**

```python
def build_minor_phase(phase: str, sub_phase: str) -> dict:
    return MINI_PHASES.get(
        (phase, sub_phase),
        {"code": "-", "name": "小级别阶段未确认", "description": "未识别到足以归类 A–E 的小级别结构"},
    ).copy()
```

Add `minor_phase` to `short_term` and to the top-level `phase` result in `analyze_kline_dict`.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py`

Expected: exit 0.

### Task 2: Expose and render the mini phase in today’s recommendations

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

- [ ] **Step 1: Write the failing report tests**

```python
item["wyckoff"]["minor_phase"] = {
    "code": "D", "name": "阶段D：SOS/LPS 确认",
    "description": "需求占优，回踩缩量后等待向上确认",
}
self.assertIn("阶段D：SOS/LPS 确认", report)
self.assertIn("需求占优", html)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: FAIL because the rendered report omits the mini-phase fields.

- [ ] **Step 3: Write the minimal implementation**

```python
def _minor_phase_text(wyckoff):
    minor = wyckoff.get("minor_phase", {})
    name = minor.get("name", "小级别阶段未确认")
    description = minor.get("description", "")
    return f"{name}（{description}）" if description else name
```

Copy `wk["short_term"]["minor_phase"]` into the scanner’s public `wyckoff` dictionary, then use `_minor_phase_text` for the Markdown and HTML short-term-stage column.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: exit 0.

### Task 3: Validate the integrated contract

**Files:**

- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`
- Test: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Run project quality gates**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py`

Expected: exit 0.

- [ ] **Step 2: Verify golden semantics without updating snapshots**

Run: `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff`

Expected: exit 0; do not regenerate snapshots.
