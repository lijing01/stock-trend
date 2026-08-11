# Long-Term Wyckoff Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate long-term Wyckoff structure from short-term triggers and validate long-horizon claims out of sample.

**Architecture:** Keep the current `phase` field as the short-term tactical signal for compatibility. Add a separate `long_term` field based exclusively on a context range; candidates cannot affect phase or score. Validate tactical and long-term modes independently.

**Tech Stack:** Python 3, standard-library `unittest`, existing K-line fetcher and backtest.

---

## Files

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py`
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/SKILL.md`

### Task 1: Lock the output contract with tests

**Files:** `.claude/skills/stock-trend/tests/test_wyckoff.py`

- [ ] Add tests before code:

```python
def test_long_term_does_not_use_minor_range(self):
    result = analyze_kline_dict({"meta": {}, "data": layered_range_rows()})
    self.assertEqual(result["phase"]["range_level"], "minor")
    self.assertEqual(result["long_term"]["range_level"], "context")

def test_ma_alignment_is_not_wyckoff_phase(self):
    result = analyze_kline_dict({"meta": {}, "data": trend_only_rows(80)})
    self.assertEqual(result["phase"]["primary"], PHASE_UNKNOWN)
    self.assertEqual(result["trend_context"]["direction"], "up")

def test_candidate_sos_cannot_be_jac(self):
    result = analyze_kline_dict({"meta": {}, "data": sos_rows(hold=False)})
    self.assertNotEqual(result["phase"]["primary_sub_phase"], SUB_JAC)
    self.assertEqual(result["signal"]["status"], "candidate")
```

- [ ] Run `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`; expected initial failure because the new fields do not exist and candidates can override phase.

### Task 2: Implement independent tactical and long-term layers

**Files:** `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:309-343,943-1160`

- [ ] Add the output helper:

```python
LONG_TERM_MIN_BARS = 250

def _phase_view(phase, sub_phase, confidence, trading_range, eligible=True):
    return {"phase": phase, "sub_phase": sub_phase,
            "confidence": round(confidence, 2),
            "range_id": (trading_range or {}).get("id", ""),
            "range_level": (trading_range or {}).get("level", ""),
            "eligible": eligible}
```

- [ ] Classify `_select_current_range(ranges, ...)` into existing `phase`; classify only `next((r for r in ranges if r["level"] == "context"), None)` into `long_term`. Do not copy the minor result into `long_term`.
- [ ] Replace MA20/MA60 fallback phase assignment with `trend_context = {"direction": "up" or "down", "source": "ma20_ma60"}` and leave both Wyckoff phases unknown without a valid structural range.
- [ ] Run `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`; expected PASS.

### Task 3: Make confirmed events the single source of Spring and JAC

**Files:** `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:415-480,819-888,1047-1062`

- [ ] Delete the raw `spring_candidates` branch from `classify_accumulation`; its undercut rule has no reclaim confirmation.
- [ ] Use the existing event model as follows:

```python
if active_event:
    signal = _signal_from_event(active_event)
    if active_event["status"] == "confirmed":
        phase, sub_phase = ((PHASE_ACCUMULATION, SUB_SPRING)
                            if active_event["type"] == "spring"
                            else (PHASE_MARKUP, SUB_JAC))
        confidence = active_event["confidence"]
```

- [ ] Filter BC/UTAD swings before classification:

```python
BC_UTAD_MAX_AGE = 8
recent_swing_highs = [s for s in recent_swing_highs
                      if latest_idx - s["index"] <= BC_UTAD_MAX_AGE]
```

- [ ] Add tests for unreclaimed Spring and an old UTAD; run the Wyckoff suite and expect PASS.

### Task 4: Retrieve enough data for a long-term claim

**Files:** `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py:33-38`, `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:235-269`

- [ ] Add `parser.add_argument("--limit", type=int, default=250)` to the fetcher and pass it to `fetch_eastmoney(..., lmt=args.limit)`.
- [ ] Change `_fetch_kline(ts_code, as_of_date="", limit=250)` to pass `--limit`, and call it with `limit=750` only from the long-term Wyckoff path.
- [ ] When fewer than 250 valid daily bars are returned, set `long_term.eligible` to `False` and keep `long_term.phase` unknown.
- [ ] Run `python3 .claude/skills/stock-trend/tests/test_wyckoff.py`; expected PASS.

### Task 5: Build a genuine long-horizon out-of-sample backtest

**Files:** `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py:224-401,642-676`, `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`

- [ ] Add a failing test:

```python
def test_long_term_mode_uses_holdout_and_long_windows(self):
    result = run_backtest(STOCKS, KLINES, mode="long_term")
    self.assertEqual(result["meta"]["eval_windows"], [20, 60, 120])
    self.assertLess(result["meta"]["train_end_date"], result["meta"]["holdout_start_date"])
```

- [ ] Extend the signature:

```python
def run_backtest(..., mode="tactical", train_ratio=0.7, round_trip_cost=0.002):
    if mode == "long_term":
        eval_windows, sample_interval = (20, 60, 120), 20
    elif mode != "tactical":
        raise ValueError("mode must be tactical or long_term")
```

- [ ] Split chronological observations at `int(len(sample_indices) * train_ratio)` and report train and holdout statistics separately. Derive `validated` only from holdout results:

```python
net_alpha = holdout_avg_alpha - round_trip_cost
validated = holdout_signal_count >= 30 and net_alpha > 0 and holdout_win_rate_alpha > 0
```

- [ ] Run `python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py`; expected PASS.

### Task 6: Document and verify

**Files:** `.claude/skills/stock-trend/SKILL.md`

- [ ] State that `phase` is a short-term trigger; `long_term` is a background structure; and `eligible=false` or `validated=false` prohibits language such as “confirmed long-term accumulation.”
- [ ] Run both mandatory quality gates:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: both exit 0; do not regenerate golden snapshots to conceal a contract change.

- [ ] Commit with `git add` for the seven modified source/test/doc files, then `git commit -m "fix: separate long-term Wyckoff structure"`.

## Success evidence

Tests prove the output contract, not investment profitability. A long-term conclusion must show at least 30 holdout signals and positive cost-adjusted alpha before it can be marked `validated`. It remains learning/reference material, not investment advice.
