# Wyckoff BU 候选与 LPS 二次确认 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将突破后的回踩建模为不可交易的 BU 候选，只有在有效 SOS/JAC、缩量守位及后续二次转强全部成立后，才输出并放行已确认 LPS。

**Architecture:** 以 `analysis/wyckoff.py` 的事件历史为唯一事实源，新增“confirmed SOS → BU candidate → confirmed LPS / expired”的无未来函数状态转换。BU 候选保存回踩日、父 SOS、冻结的突破 ATR 和量价证据；确认日保存为 `detected_index`，使历史回放在确认之前不会看到 LPS。扫描器只接受 confirmed LPS，报告层明确展示 BU 与 LPS，保留旧 `lps_candidate` 字段作为只读兼容别名。

**Tech Stack:** Python 3.10+、标准库 `unittest`、现有 OHLCV/ATR/维科夫事件引擎、扫描器与回测脚本。

---

## Scope and behavior contract

This plan applies only to **post-breakout re-accumulation LPS** (`phase=markup`, `sub_phase=lps`). It does not change accumulation-stage LPS, Spring, ST, or the SOS detector's existing confirmation rule.

The post-breakout sequence is:

```text
confirmed SOS/JAC
  → BU candidate (retest bar; observation only)
  → confirmed LPS (later validation bar; actionable if fresh and aligned)
  → expired (no validation before deadline; observation only)
```

Initial, named thresholds are deliberately conservative and must remain constants in `wyckoff.py`, rather than scattered literals:

```python
LPS_CANDIDATE_MAX_AGE = 5
LPS_CONFIRM_MAX_BARS = 3
LPS_SUPPORT_CLOSE_ATR = 0.30
LPS_SUPPORT_LOW_ATR = 0.50
LPS_MAX_RETRACE_ATR = 2.00
LPS_MAX_SPREAD_ATR = 1.00
LPS_VOLUME_VS_SOS = 0.85
LPS_VOLUME_VS_AVERAGE = 0.90
```

All price distances use `breakout_atr` frozen on the SOS bar. A BU candidate requires all of: a confirmed parent SOS for the same `range_id`; low/close above the defined former-resistance buffers; post-SOS retrace no deeper than `LPS_MAX_RETRACE_ATR`; true range no greater than `LPS_MAX_SPREAD_ATR`; a non-upward retest close; and volume below the SOS-day volume, trailing 5-day volume, trailing 10-day volume, and TR-window volume median by the defined limits. An LPS is confirmed only when, in the next 1–3 bars, either the close exceeds the BU candidate bar's high or two consecutive closes re-accept the former resistance. This is causal: `event_index` is the BU date, and `detected_index` is the later confirmation date.

## File structure and ownership

| File | Responsibility | Planned change |
|---|---|---|
| `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` | Canonical phases, event detection, buy-point rules, output schema | Implement the BU→LPS event state machine and labels. |
| `.claude/skills/stock-trend/tests/test_wyckoff.py` | Deterministic OHLCV event tests | Replace immediate-LPS expectation with candidate/confirmation/expiry tests. |
| `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` | Candidate funnel | Continue using shared gate; remove BU from actionable semantics. |
| `.claude/skills/stock-trend/tests/test_stock_scanner.py` | Scanner gate and score regression coverage | Assert BU candidate rejection and confirmed LPS admission. |
| `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` | Candidate-report Wyckoff label rendering | Render BU candidate and confirmed LPS separately. |
| `.claude/skills/stock-trend/scripts/reporting/report.py` | Full-report Wyckoff label styling | Stop identifying states by the display string `BU/LPS`. |
| `.claude/skills/stock-trend/tests/test_daily_candidates.py` | HTML rendering contract | Replace combined BU/LPS label fixture with two distinct states. |
| `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py` | Causal historical signal replay | No expected code change; validate that it only receives LPS from the confirmation date. |
| `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` | Replay and signal-date regression coverage | Add a fixture proving no trade occurs on BU candidate day. |
| `docs/wyckoff-analysis-design.md` | User-facing behavior contract | Document post-breakout terminology and confirmation timing. |

### Task 1: Lock the causal BU→LPS contract in unit tests

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py:123-174`

- [ ] **Step 1: Replace the existing immediate-confirmation fixture with a four-bar causal sequence**

Create `_ohlcv_with_sos_bu_and_lps_confirmation()` based on the current deterministic range `100–110`. It must contain: SOS at index 50, SOS hold at 51, low-volume/narrow BU candidate at 52, and an up-close above the BU high at 53. Keep `detect_trading_ranges()` patched to the fixed range.

```python
ohlcv["open"][52], ohlcv["high"][52], ohlcv["low"][52] = 112.5, 113.0, 110.2
ohlcv["close"][52], ohlcv["volume"][52] = 111.2, 70.0
ohlcv["open"][53], ohlcv["high"][53], ohlcv["low"][53] = 111.5, 114.0, 111.0
ohlcv["close"][53], ohlcv["volume"][53] = 113.4, 95.0
```

- [ ] **Step 2: Add failing assertions for observation on the BU day**

Analyze a slice ending at index 52. Assert that only a candidate BU is visible, that no confirmed LPS exists, and that the generic buy gate is closed.

```python
events = detect_wyckoff_events(ohlcv_until_bu, atr_until_bu, trading_range)
bu = next(event for event in events if event["type"] == "bu")
self.assertEqual(bu["status"], "candidate")
self.assertFalse(any(e["type"] == "lps" and e["status"] == "confirmed" for e in events))
self.assertFalse(is_buy_signal(analyze_kline_dict(kline_until_bu)))
```

- [ ] **Step 3: Add failing assertions for confirmation only on the following bar**

Analyze the full four-bar sequence. Assert that the LPS preserves the BU date as its occurrence, the subsequent date as its detection, and the same SOS as its parent.

```python
lps = next(event for event in events if event["type"] == "lps")
self.assertEqual(lps["status"], "confirmed")
self.assertEqual(lps["event_index"], 52)
self.assertEqual(lps["detected_index"], 53)
self.assertEqual(lps["parent_event"], "sos")
self.assertEqual(result["signal"]["event"], "lps")
self.assertEqual(result["signal"]["status"], "confirmed")
self.assertTrue(is_buy_signal(result))
```

- [ ] **Step 4: Add failure-path tests with explicit input changes**

Add one test each for: no SOS parent; volume at index 52 raised to `140.0`; low at index 52 dropped to `108.5`; wide spread at index 52 above one frozen ATR; and no confirmation within three bars. Each must assert no confirmed LPS. The no-confirmation test must assert an expired/aged-out BU is not a buy signal.

```python
self.assertFalse(any(e["type"] == "lps" and e["status"] == "confirmed" for e in events))
self.assertFalse(is_buy_signal(result))
```

- [ ] **Step 5: Run the focused test and verify the intended initial failure**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: the newly added assertions fail because the current implementation immediately emits confirmed LPS on the BU bar and has no `bu` event type. Existing tests should otherwise remain green.

- [ ] **Step 6: Commit the test contract**

```bash
git add .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "test: define causal BU to LPS confirmation"
```

### Task 2: Add auditable post-SOS retest primitives

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:144-150,941-1067`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Add state constants and attach frozen ATR to SOS events**

Define the constants in the scope contract beside `EVENT_MAX_AGE`. When appending an SOS event, set its frozen ATR and do not later overwrite it:

```python
sos_event = _event_record("sos", i, detected_idx, dates, status, level, trading_range, confidence)
sos_event["breakout_atr"] = round(atr_values[i] or 0.0, 4)
events.append(sos_event)
```

- [ ] **Step 2: Replace `_is_lps_pullback()` with a candidate-evidence function**

Implement a pure helper returning `dict | None`, not a boolean, so all output fields are auditable:

```python
def _bu_candidate_evidence(ohlcv, trading_range, sos_event, index) -> dict | None:
    breakout_atr = _safe_float(sos_event.get("breakout_atr"))
    if not breakout_atr or index <= sos_event["detected_index"]:
        return None
    resistance = trading_range["resistance"]
    spread = ohlcv["high"][index] - ohlcv["low"][index]
    avg5 = _ma_of_last_n(ohlcv["volume"], index, 5)
    avg10 = _ma_of_last_n(ohlcv["volume"], index, 10)
    tr_median = median(ohlcv["volume"][trading_range["support_idx"]:index])
    # Return None unless every price, spread, and four-way volume condition holds.
    return {"breakout_atr": round(breakout_atr, 4), "volume_avg5": avg5,
            "volume_avg10": avg10, "volume_tr_median": tr_median}
```

Use `statistics.median` from the standard library. Guard all lookbacks so an unavailable baseline returns `None` rather than silently treating missing evidence as confirmation.

- [ ] **Step 3: Add a confirmation helper that sees only later bars**

Implement the two allowed confirmation routes and return the confirmation index, otherwise `None`:

```python
def _confirm_lps(ohlcv, trading_range, bu_index, end_index) -> int | None:
    for index in range(bu_index + 1, min(bu_index + 1 + LPS_CONFIRM_MAX_BARS, end_index + 1)):
        if ohlcv["close"][index] > ohlcv["high"][bu_index]:
            return index
        if index > bu_index + 1 and all(
            ohlcv["close"][day] >= trading_range["resistance"]
            for day in (index - 1, index)
        ):
            return index
    return None
```

The caller passes the current final bar as `end_index`; this preserves causal replay.

- [ ] **Step 4: Run the new helper-level tests**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: candidate evidence accepts only the narrow, shallow, multi-baseline low-volume fixture; `_confirm_lps()` returns 53 for the positive fixture and `None` for every negative fixture.

- [ ] **Step 5: Commit the primitives**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: add auditable BU retest evidence"
```

### Task 3: Implement candidate, confirmed, and expired events

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:958-1092,1307-1364`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] **Step 1: Generate one BU candidate per confirmed SOS**

Replace the current loop that emits `"lps"` directly. Scan only after each confirmed SOS; use `_bu_candidate_evidence()` to create a `bu` event with `status="candidate"` and the parent linkage. Do not generate a candidate from SOS candidates or from another range.

```python
bu_event = _event_record("bu", bu_index, bu_index, dates, "candidate", level, trading_range, 0.62)
bu_event.update(evidence)
bu_event.update({"parent_event": "sos", "parent_event_index": sos_event["event_index"]})
events.append(bu_event)
```

- [ ] **Step 2: Upgrade only with a later confirmation bar**

Use `_confirm_lps()` after creating the BU candidate. If it returns an index, append a separate confirmed LPS event; preserve the BU occurrence date and set the detection date to the confirmation date.

```python
confirmation_index = _confirm_lps(ohlcv, trading_range, bu_index, len(closes) - 1)
if confirmation_index is not None:
    lps_event = _event_record("lps", bu_index, confirmation_index, dates, "confirmed", level, trading_range, 0.78)
    lps_event.update({"parent_event": "sos", "parent_event_index": sos_event["event_index"],
                      "candidate_event_index": bu_index, "breakout_atr": evidence["breakout_atr"]})
    events.append(lps_event)
```

If the confirmation window has ended, set the BU event's status to `"expired"`; otherwise keep it as `"candidate"`. This preserves failed-candidate auditability without making an expired event actionable.

- [ ] **Step 3: Update event arbitration and current-signal mapping**

Extend `_current_event()` with `"bu": SUB_BU`, but choose confirmed LPS over its same-parent candidate BU. In `analyze_kline_dict()`, map candidate BU to `phase=markup`, `sub_phase=backup`, `signal.status="candidate"`; map confirmed LPS to `phase=markup`, `sub_phase=lps`, `signal.status="confirmed"`.

```python
event_priority = {"lps": 4, "bu": 3, "sos": 2, "spring": 1}
if active_event["type"] == "bu":
    phase, sub_phase = PHASE_MARKUP, SUB_BU
elif active_event["type"] == "lps":
    phase, sub_phase = PHASE_MARKUP, SUB_LPS
```

- [ ] **Step 4: Publish explicit output fields with backward compatibility**

Add canonical `bu_candidate` to the result. Retain `lps_candidate` as the same candidate dict for one compatibility release, annotate it with `deprecated_alias=True`, and ensure neither is used by `is_buy_signal()`.

```python
"bu_candidate": bu_candidate,
"lps_candidate": ({**bu_candidate, "deprecated_alias": True} if bu_candidate else None),
```

- [ ] **Step 5: Run event tests and inspect their payloads**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: candidate-day output contains `bu_candidate`, confirmed-day output contains an LPS whose `detected_date` is later than its `event_date`, and expiry has no actionable signal.

- [ ] **Step 6: Commit the state machine**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: confirm LPS only after BU validation"
```

### Task 4: Make buy eligibility match the strict terminology

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:907-928,617-650`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1244-1271`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py:735-789`

- [ ] **Step 1: Write the scanner failures first**

Replace the test that treats markup BU as a buy point. Add explicit candidate, expired, confirmed LPS, and stale LPS cases:

```python
bu = _wk(phase="markup", sub="backup", conf=0.7)
bu["signal"] = {"status": "candidate", "age_bars": 0, "event": "bu"}
self.assertFalse(sc.wyckoff_gate_pass(bu))

lps = _wk(phase="markup", sub="lps", conf=0.7, score=2.0)
lps["signal"] = {"status": "confirmed", "age_bars": 0, "event": "lps"}
self.assertTrue(sc.wyckoff_gate_pass(lps))
```

- [ ] **Step 2: Remove BU from the canonical actionable sub-phase list**

BU is now an observation state; delete `SUB_BU` from `BUY_SUB_PHASES`. Keep `SUB_BU` defined and rendered, but require `is_buy_point()` to receive a confirmed LPS for this post-breakout setup.

```python
BUY_SUB_PHASES = (SUB_SPRING, SUB_LPS, SUB_ST, SUB_PRE_MARKUP, SUB_JAC)
```

- [ ] **Step 3: Stop the range classifier from independently confirming BU**

In `classify_markup()`, remove both direct `return (SUB_BU, ...)` branches. Return `SUB_CONTINUATION` when no current event establishes a BU/LPS state. Candidate BU must be generated only by the parent-SOS event machinery in Task 3.

```python
return (SUB_CONTINUATION, 0.6, latest_idx)
```

- [ ] **Step 4: Keep the scanner as a consumer of shared truth**

Do not create a scanner-specific BU/LPS condition. Its existing `is_buy_signal()` call remains the only eligibility test. Update its docstring and score comment to say “confirmed LPS”; retain the existing LPS/PRE_MARKUP/JAC score bonus.

- [ ] **Step 5: Run focused scanner tests**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: candidate/expired BU is excluded; fresh confirmed LPS remains eligible and gets the existing score treatment; unrelated Spring/ST/JAC behavior does not regress.

- [ ] **Step 6: Commit the gate alignment**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "fix: exclude unconfirmed BU from buy funnel"
```

### Task 5: Distinguish BU observation from confirmed LPS in reports

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:77-82,1209-1214`
- Modify: `.claude/skills/stock-trend/scripts/reporting/report.py:859-868`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1091-1100`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py:1715-1726`

- [ ] **Step 1: Add rendering tests before changing labels**

Replace the combined label test with two exact fixtures. BU must say it is awaiting confirmation and use the observation styling; LPS must say it has been confirmed and use the existing highlighted styling.

```python
bu_minor = {"code": "D", "name": "阶段D：BU回踩待确认", "description": "缩量守位，等待再次转强"}
lps_minor = {"code": "D", "name": "阶段D：LPS已确认", "description": "回踩后已重新转强"}
```

- [ ] **Step 2: Change phase and trading-implication text**

Use unambiguous labels; never concatenate `BU/LPS`:

```python
(PHASE_MARKUP, SUB_BU): ("D", "阶段D：BU回踩待确认", "突破后缩量回踩，等待重新站稳或突破回踩高点")
(PHASE_MARKUP, SUB_LPS): ("D", "阶段D：LPS已确认", "回踩守位后已再次转强，供应测试失败")
```

Set `SUB_BU` implication to observation only. Set `SUB_LPS` implication to an eligible signal, subject to the unchanged long/short alignment gate.

- [ ] **Step 3: Replace display-text logic with structured fields**

In `report.py` and `daily_candidates.py`, style by `minor_phase.code == "D"` plus `phase.primary_sub_phase == "lps"`; do not inspect `"BU/LPS"` in the user-visible name.

```python
is_confirmed_lps = (
    minor_phase.get("code") == "D"
    and w_phase.get("primary_sub_phase") == "lps"
    and short_term.get("signal_status") == "confirmed"
)
```

- [ ] **Step 4: Run report rendering tests**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: HTML distinguishes the two states; report text does not contain the combined `BU/LPS` label; all disclaimer assertions remain intact.

- [ ] **Step 5: Commit report terminology**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/scripts/reporting/report.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "fix: render BU and LPS as separate states"
```

### Task 6: Prove the backtest remains causal and document the contract

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- Modify: `docs/wyckoff-analysis-design.md`

- [ ] **Step 1: Add replay regression coverage**

Using the Task 1 fixture, invoke the existing replay helper at the BU candidate as-of date and at the LPS confirmation as-of date. Assert no signal/trade appears on the former and the `lps` signal's as-of date is the latter.

```python
candidate_result = analyze_kline_dict(kline_until_bu)
confirmed_result = analyze_kline_dict(kline_until_confirmation)
self.assertFalse(is_buy_signal(candidate_result))
self.assertTrue(is_buy_signal(confirmed_result))
self.assertEqual(confirmed_result["signal"]["detected_date"], "20260154")
```

- [ ] **Step 2: Run the backtest test suite before implementation adjustments**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: new replay assertion initially fails on candidate-day leakage, then passes after Tasks 2–4. Do not modify production backtest code unless this test shows it bypasses `is_buy_signal()`.

- [ ] **Step 3: Update the design document**

Add this normative section:

```text
突破后：BU 是“正在验证的回踩行为”，不是买点；LPS 是“回踩已通过后续价格确认的结果”。
post-breakout LPS 必须具有 confirmed SOS 父事件、缩量窄幅守住突破区，且在未来 1–3 根可见 K 线中重新突破 BU 高点或连续收回箱顶。
回测以 LPS 的 detected_date 计入信号；BU event_date 不计入交易信号。
```

- [ ] **Step 4: Run all required validation gates**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: all tests pass. Treat every golden diff as a behavior review: update a snapshot only after confirming it reflects BU being demoted to observation or LPS moving to its later confirmation date.

- [ ] **Step 5: Run a bounded replay smoke test**

Run:

```bash
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --codes 600129 --lookback-days 120 --eval-windows 5,10,20
```

Expected: command completes; any change in signal count is explained as removal of candidate-day entries, not presented as proof of improved returns. The sample remains too small for performance claims.

- [ ] **Step 6: Commit verification and documentation**

```bash
git add .claude/skills/stock-trend/tests/test_wyckoff_backtest.py docs/wyckoff-analysis-design.md
git commit -m "docs: define post-breakout LPS confirmation"
```

## Acceptance criteria

1. A post-breakout LPS always has a same-range, confirmed SOS parent and a frozen `breakout_atr`.
2. On the BU candidate day, JSON reports `bu_candidate`; `signal.status` is not `confirmed`; the scanner and backtest do not treat it as a buy point.
3. A confirmed LPS records the BU day as `event_date` and the later validation bar as `detected_date`; no analysis slice before `detected_date` contains that confirmed LPS.
4. Candidate volume is lower than SOS, 5-day, 10-day, and TR-median baselines; deep, broad, high-volume, or unparented pullbacks never confirm LPS.
5. Confirmation requires a close above the BU high or two consecutive closes at/above former resistance, within three subsequent bars.
6. BU is excluded from `BUY_SUB_PHASES`; fresh confirmed LPS retains current score and scanner eligibility.
7. Reports never label an unconfirmed BU as `BU/LPS` or as an actionable LPS.
8. Focused tests, both repository quality gates, golden diff review, and the bounded 600129 replay complete with the stated evidence.

## Risks and non-goals

- The constants are a conservative initial contract, not statistically optimized parameters. Do not tune them from one stock or one replay; evaluate by-sub-phase results only after a sufficiently broad universe is available.
- This plan does not redefine accumulation-stage LPS or require JAC/SOS before that separate structure.
- This plan does not alter position sizing, stop-loss levels, or investment recommendations.
- No golden snapshot may be regenerated solely to hide an expected test failure.

This design and any resulting analysis are for learning and reference only; they are not investment advice.
