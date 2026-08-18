# Daily Recommendation Timing and Actionability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an auditable Wyckoff early-warning layer, quantified per-stock execution plans, and evidence-gated market/sector ranking improvements without weakening the existing confirmed-buy recommendation gate.

**Architecture:** Preserve `confirmed` as the only Wyckoff tier eligible for `今日可执行` or `等待触发`. Add a separate `forming` tier for Spring/SOS events that are observable but not confirmed, expose it as `提前预警（非买入）`, and attach a deterministic execution plan derived from the current Wyckoff range and ATR. Compute market/sector opportunity adjustments in shadow mode first; only allow them to affect ranking after timing and forward-snapshot evidence passes explicit sample and outcome gates.

**Tech Stack:** Python 3.10+, `unittest`, existing Wyckoff/scanner/candidate modules, JSON snapshot artifacts under `.cache/stock-trend/`.

---

## Requirements summary

- Keep the current `is_buy_signal()` and `wyckoff_gate_pass()` meaning backward compatible: only fresh, confirmed buy points return `True`.
- Surface candidate Spring/SOS events no more than three trading bars after the event as `forming`; never promote them into actionable or waiting recommendation buckets.
- Build forming rows from already-fetched K-line and sector evidence only. Capital and fundamental request counts must remain bounded by the confirmed lane.
- Keep existing market-regime hard safety limits (`<60`, `60–79`, `>=80`) until evidence supports a change.
- Add continuous market/sector scoring as a separate `opportunity_score_shadow`; do not silently redefine `raw_composite_score` or `quality_adjusted_score`.
- Every actionable and waiting row must have a machine-readable trigger, invalidation/stop, two targets, risk/reward, maximum chase price, position cap, and validity window, or an explicit `unavailable_reason`. Early-watch rows expose only confirmation, cancellation, event age, and validity; they must not look like immediate entry plans.
- Measure signal confirmation delay and candidate-to-confirmed conversion before changing production ranking.
- Preserve the existing JSON fields `candidates`, `recommendations`, `waiting_trigger`, `next_day_confirmation`, and `observation`; new fields are additive.
- Keep `wyckoff_pass_count`, scan early-stop eligibility, and `final_valid_count` confirmed-only; add separate forming counters.
- Do not claim production edge when sample gates are not met. Output `evidence_insufficient` instead.

## Non-goals

- No intraday/T+0 entry model.
- No relaxation of data freshness, 70% coverage, sector actionability, or long-term countertrend gates.
- No automatic conversion of a single-day sector pulse into a buy recommendation.
- No new dependency and no golden snapshot regeneration merely to hide output drift.
- No automatic switch to shadow ranking based on in-sample results alone.

## File map

| File | Responsibility |
|---|---|
| `.claude/skills/stock-trend/scripts/analysis/wyckoff.py` | Signal-tier classification and confirmation-delay metadata |
| `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` | Confirmed/forming scan policy, public Wyckoff payload, trade-plan attachment |
| `.claude/skills/stock-trend/scripts/core/candidate_trade_plan.py` | Pure price-level and risk/reward calculation |
| `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` | Early-watch bucket, shadow opportunity score, JSON/Markdown/HTML rendering |
| `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py` | Immutable official-run snapshots and model-version metadata |
| `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py` | Candidate-vs-confirmed timing study |
| `.claude/skills/stock-trend/scripts/backtesting/recommendation_tracker.py` | Matured snapshot evaluation by regime, sector, tier, and horizon |
| `.claude/skills/stock-trend/tests/test_wyckoff.py` | Signal-tier unit contract |
| `.claude/skills/stock-trend/tests/test_stock_scanner.py` | Scanner policy and execution-plan integration |
| `.claude/skills/stock-trend/tests/test_daily_candidates.py` | Bucket exclusivity, compatibility, report, and shadow-score tests |
| `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py` | Price/risk calculation tests |
| `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py` | Timing-pair and delay metric tests |
| `.claude/skills/stock-trend/tests/test_recommendation_tracker.py` | Snapshot immutability and forward evaluation tests |
| `.claude/skills/stock-trend/SKILL.md` | User-facing `/candidates` and backtest contract |
| `docs/daily-recommendation-optimization.md` | P1/P2 status and evidence-gate documentation |

---

### Task 1: Define the Wyckoff `forming` signal contract

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:883-1009`
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py:1123-1158`

- [ ] **Step 1: Write failing tests for confirmed, forming, stale, and non-buy events**

Add imports for `classify_buy_signal` and these tests:

```python
class TestBuySignalTier(unittest.TestCase):
    def test_confirmed_buy_remains_confirmed(self):
        analysis = {
            "phase": {"primary": PHASE_MARKUP, "primary_sub_phase": SUB_JAC,
                      "confidence": 0.72},
            "signal": {"status": "confirmed", "event": "sos", "age_bars": 1,
                       "confidence": 0.72},
        }
        decision = classify_buy_signal(analysis)
        self.assertEqual(decision["tier"], "confirmed")
        self.assertTrue(decision["accepted"])
        self.assertTrue(is_buy_signal(analysis))

    def test_recent_candidate_sos_is_forming_not_buy(self):
        analysis = {
            "phase": {"primary": PHASE_UNKNOWN, "primary_sub_phase": "",
                      "confidence": 0.0},
            "signal": {"status": "candidate", "event": "sos", "age_bars": 1,
                       "confidence": 0.65},
        }
        decision = classify_buy_signal(analysis)
        self.assertEqual(decision["tier"], "forming")
        self.assertEqual(decision["prospective_sub_phase"], SUB_JAC)
        self.assertFalse(decision["accepted"])
        self.assertFalse(is_buy_signal(analysis))

    def test_candidate_older_than_confirmation_window_is_none(self):
        analysis = {
            "phase": {"primary": PHASE_UNKNOWN, "primary_sub_phase": "",
                      "confidence": 0.0},
            "signal": {"status": "candidate", "event": "spring", "age_bars": 4,
                       "confidence": 0.62},
        }
        self.assertEqual(classify_buy_signal(analysis)["tier"], "none")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: FAIL because `classify_buy_signal` is not defined.

- [ ] **Step 3: Add the pure signal-tier classifier without changing `is_buy_signal`**

Implement this contract next to `is_buy_point`:

```python
FORMING_SIGNAL_MAX_AGE = 3
FORMING_SIGNAL_MIN_CONFIDENCE = 0.45


def classify_buy_signal(analysis: dict | None) -> dict:
    empty = {
        "tier": "none", "accepted": False, "reason": "no_buy_event",
        "prospective_phase": "", "prospective_sub_phase": "",
    }
    if not analysis:
        return empty
    if is_buy_signal(analysis):
        phase = analysis.get("phase", {})
        return {
            "tier": "confirmed", "accepted": True,
            "reason": "confirmed_buy_point",
            "prospective_phase": phase.get("primary", ""),
            "prospective_sub_phase": phase.get("primary_sub_phase", ""),
        }
    signal = analysis.get("signal", {})
    event = signal.get("event", "")
    age = int(signal.get("age_bars", 0) or 0)
    confidence = float(signal.get("confidence", 0) or 0)
    mapping = {
        "spring": (PHASE_ACCUMULATION, SUB_SPRING),
        "sos": (PHASE_MARKUP, SUB_JAC),
    }
    if (signal.get("status") == "candidate" and event in mapping
            and age <= FORMING_SIGNAL_MAX_AGE
            and confidence >= FORMING_SIGNAL_MIN_CONFIDENCE):
        phase, sub_phase = mapping[event]
        return {
            "tier": "forming", "accepted": False,
            "reason": "awaiting_reclaim" if event == "spring" else "awaiting_hold",
            "prospective_phase": phase,
            "prospective_sub_phase": sub_phase,
        }
    return empty
```

When copying `active_event` into top-level `signal`, add `confidence`, `event_index`, `detected_index`, and `confirmation_delay_bars = detected_index - event_index`. Keep `is_buy_signal()` unchanged.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same command. Expected: exit 0.

- [ ] **Step 5: Commit the isolated contract**

```bash
git add .claude/skills/stock-trend/scripts/analysis/wyckoff.py .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: classify forming Wyckoff signals"
```

---

### Task 2: Split confirmed scoring from lightweight forming discovery

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1229-1271`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1274-1640`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:733-874`

- [ ] **Step 1: Write failing compatibility and include-forming tests**

```python
def _forming_wk(event="sos", confidence=0.65):
    result = _wk(phase="phase_unknown", sub="", conf=0.0, score=0.0)
    result["signal"] = {
        "status": "candidate", "event": event, "age_bars": 1,
        "confidence": confidence, "event_index": 58, "detected_index": 58,
    }
    return result


class TestWyckoffScanPolicy(unittest.TestCase):
    def test_default_policy_still_rejects_forming(self):
        self.assertFalse(sc.wyckoff_gate_pass(_forming_wk()))

    def test_include_forming_returns_nonaccepted_watch_decision(self):
        decision = sc.wyckoff_gate_decision(
            _forming_wk(), policy="confirmed_plus_forming")
        self.assertEqual(decision["tier"], "forming")
        self.assertTrue(decision["retain"])
        self.assertFalse(decision["accepted"])
```

Extend the existing `run_phase2` mocked-data test to assert that the default policy omits the forming row, while `wyckoff_policy="confirmed_plus_forming"` retains it with `wyckoff.signal_tier == "forming"`. Also assert that one confirmed row plus one forming row causes exactly one capital request and one fundamental request, `wyckoff_pass_count == 1`, and `wyckoff_forming_count == 1`.

Add a regression fixture containing confirmed and candidate signals, run it once with `confirmed_only` and once with `confirmed_plus_forming`, strip only the forming rows, and assert the remaining confirmed payloads are exactly equal, including order and scores.

- [ ] **Step 2: Run the scanner test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: FAIL because the policy argument and decision helper do not exist.

- [ ] **Step 3: Add an explicit scan policy**

Implement:

```python
def wyckoff_gate_decision(analysis, policy="confirmed_only"):
    decision = classify_buy_signal(analysis)
    retain = decision["tier"] == "confirmed" or (
        policy == "confirmed_plus_forming" and decision["tier"] == "forming")
    return {**decision, "retain": retain}


def wyckoff_gate_pass(analysis):
    decision = wyckoff_gate_decision(analysis, policy="confirmed_only")
    confidence = _safe_float(analysis.get("phase", {}).get("confidence")) \
        if analysis else 0.0
    return bool(decision["accepted"] and confidence >= WYCKOFF_MIN_CONFIDENCE)
```

Add `wyckoff_policy="confirmed_only"` to `run_phase2`. After the single K-line/Wyckoff pass, split candidates into `confirmed_candidates` and `forming_candidates`. Fetch capital and fundamentals only for `confirmed_candidates`; forming rows must never enter either external-data executor.

Build forming rows with a dedicated pure helper using only the candidate, K-line momentum/volume, sector strength, data date, and Wyckoff analysis:

```python
def build_forming_watch_item(candidate, kline, analysis, sector_score, as_of_date):
    return {
        "code": candidate["code"],
        "ts_code": candidate["ts_code"],
        "name": candidate["name"],
        "sector_code": candidate.get("sector_code", ""),
        "sector_name": candidate.get("sector_name", ""),
        "watch_score": round(
            score_momentum(candidate, kline) * 0.45
            + score_volume_price(candidate, kline) * 0.25
            + float(sector_score or 50) * 0.30,
            1,
        ),
        "recommendation_lane": "early_warning",
        "warning_quality": {
            "kline_fresh": latest_data_date(kline) == as_of_date,
            "data_date": latest_data_date(kline),
            "membership_current": candidate.get("membership_data_date") == as_of_date,
            "eligible": (
                latest_data_date(kline) == as_of_date
                and candidate.get("membership_data_date") == as_of_date
            ),
        },
    }
```

Store decisions by `ts_code`. Add these public fields under `item["wyckoff"]` for both lanes:

```python
"signal_tier": decision["tier"],
"signal_reason": decision["reason"],
"prospective_phase": decision["prospective_phase"],
"prospective_sub_phase": decision["prospective_sub_phase"],
"signal": wk.get("signal", {}),
```

Forming rows must not receive `composite_score`, `quality_adjusted_score`, or a Wyckoff score bonus; `watch_score` is discovery-only. Return confirmed and forming rows together for compatibility, marked by `recommendation_lane`. Change only `daily_candidates.scan_sectors()` to call:

```python
run_phase2(
    new_candidates,
    enable_wyckoff=True,
    wyckoff_policy="confirmed_plus_forming",
    as_of_date=as_of_date,
    source_health=source_health,
    metrics=metrics,
)
```

Update telemetry without changing existing counter meanings. Use cumulative updates because `scan_sectors()` processes multiple batches:

```python
metrics["wyckoff_pass_count"] = metrics.get("wyckoff_pass_count", 0) \
    + len(confirmed_candidates)
metrics["wyckoff_forming_count"] = metrics.get("wyckoff_forming_count", 0) \
    + len(forming_candidates)
metrics["capital_requests"] = metrics.get("capital_requests", 0) \
    + len(confirmed_candidates)
metrics["fundamental_requests"] = metrics.get("fundamental_requests", 0) \
    + len(confirmed_candidates)
```

Add a two-batch regression test proving counters are not overwritten.

Add an early-warning branch to `_rebind_primary_sector()`: update primary sector provenance, membership, `watch_score`, and `warning_quality`, then return before the generic `data_quality`/`quality_adjusted_score` path. Assert that rebinding a forming row does not create `composite_score` or `quality_adjusted_score`.

- [ ] **Step 4: Verify default scanner behavior and candidate-scan behavior**

Run the scanner test again. Expected: exit 0; existing confirmed-only tests remain unchanged.

- [ ] **Step 5: Commit the scanner policy**

```bash
git add .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "feat: retain forming signals for daily watch"
```

---

### Task 3: Add an exclusive early-watch recommendation bucket

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1124-1179`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1182-1346`

- [ ] **Step 1: Write failing bucket and compatibility tests**

Add `forming_candidate` beside the existing module-level `candidate` fixture. Add the two `test_*` methods below inside the existing `TestRecommendationPolicy(unittest.TestCase)` class.

```python
def forming_candidate(code="forming", sector_actionable=False):
    item = candidate(code, sector_actionable=sector_actionable)
    item.pop("composite_score", None)
    item.pop("quality_adjusted_score", None)
    item["recommendation_lane"] = "early_warning"
    item["watch_score"] = 72.0
    item["warning_quality"] = {
        "kline_fresh": True, "membership_current": True,
        "eligible": True, "data_date": "2026-08-18",
    }
    item["wyckoff"].update({
        "signal_tier": "forming",
        "signal_reason": "awaiting_hold",
        "signal": {"status": "candidate", "event": "sos", "age_bars": 1},
    })
    return item


def test_forming_signal_is_early_watch_only(self):
    policy = build_recommendation_policy(
        {"score": 85, "data_date": "2026-08-18", "capital_score": 50},
        "2026-08-18")
    buckets = classify_candidates([forming_candidate()], policy)
    self.assertEqual([row["code"] for row in buckets["early_watch"]], ["forming"])
    self.assertEqual(buckets["actionable"], [])
    self.assertEqual(buckets["waiting_trigger"], [])
    self.assertEqual(buckets["observation"], [])


def test_confirmed_signal_never_appears_in_early_watch(self):
    policy = build_recommendation_policy(
        {"score": 85, "data_date": "2026-08-18"}, "2026-08-18")
    buckets = classify_candidates([candidate("confirmed")], policy)
    self.assertEqual(buckets["early_watch"], [])
    self.assertEqual([row["code"] for row in buckets["actionable"]], ["confirmed"])
```

Extend JSON tests to verify all old keys remain, `early_watch` is additive, and a code appears in exactly one bucket.

- [ ] **Step 2: Run the candidate tests and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL because `early_watch` is absent.

- [ ] **Step 3: Classify forming rows before confirmed eligibility**

At the start of `classify_candidates`, build:

```python
early_watch = [
    item for item in candidates
    if item.get("warning_quality", {}).get("eligible", False)
    and item.get("wyckoff", {}).get("signal_tier") == "forming"
][:5]
early_codes = {item["code"] for item in early_watch}
```

Exclude `early_codes` from confirmed eligibility, `next_day_confirmation`, and observation. Keep `next_day_confirmation` for compatibility, but do not alias it to `early_watch`; its existing neutral-market semantics remain unchanged for non-forming rows.

Do not include `early_watch` in `_is_final_valid_candidate`, scan early-stop counts, `final_valid_count`, or `actionable_count`. Add only `early_watch_count` and `wyckoff_forming_count` as separate telemetry.

Update `select_candidate_pool()` so the existing `top` limit and score ordering apply only to confirmed rows, while at most five fresh forming rows are appended by descending `watch_score`:

```python
forming = sorted(
    [item for item in scored
     if item.get("recommendation_lane") == "early_warning"],
    key=lambda item: item.get("watch_score", 0),
    reverse=True,
)[:5]
confirmed = [
    item for item in scored
    if item.get("recommendation_lane") != "early_warning"
    and item.get("composite_score", 0) >= min_score
]
# Reuse the existing promotable-first/quality-score selection_key unchanged.
confirmed.sort(key=selection_key, reverse=True)
return confirmed[:top] + forming
```

Add a test proving a high `watch_score` does not displace any of the confirmed `top` rows.
Add a second test using code-sorted input to prove confirmed ordering remains promotable-first and then descending quality-adjusted score.

- [ ] **Step 4: Render the new tier with an explicit non-buy label**

Add a report section named `提前预警（非买入）` before `等待触发`. Render signal event, age, confirmation requirement, and the statement `仅用于收盘后跟踪，不计入推荐数量与仓位上限`. Add the same section to HTML and `early_watch` to `build_json_output`.

- [ ] **Step 5: Run tests and commit**

Expected: candidate tests exit 0, existing `next_day_confirmation` assertions still pass.

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: add non-actionable early watch tier"
```

---

### Task 4: Generate a deterministic per-stock execution plan

**Files:**

- Create: `.claude/skills/stock-trend/scripts/core/candidate_trade_plan.py`
- Create: `.claude/skills/stock-trend/tests/test_candidate_trade_plan.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1495-1638`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1035-1062`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1231-1323`

- [ ] **Step 1: Write failing pure-function tests**

```python
import unittest

from core.candidate_trade_plan import build_candidate_trade_plan


class TestCandidateTradePlan(unittest.TestCase):
    def test_forming_sos_waits_for_breakout_hold(self):
        plan = build_candidate_trade_plan(
            current_price=10.8,
            atr=0.4,
            trading_range={
                "support": 9.0, "resistance": 11.0, "range_height": 2.0,
            },
            signal={"status": "candidate", "event": "sos", "age_bars": 1},
            signal_tier="forming",
            sub_phase="jac",
        )
        self.assertEqual(plan["action"], "wait_breakout_hold")
        self.assertEqual(plan["trigger_price"], 11.12)
        self.assertEqual(plan["cancel_below"], 10.6)
        self.assertEqual(plan["targets"], [])
        self.assertEqual(plan["position_cap_pct"], 0)
        self.assertEqual(plan["validity_bars"], 2)

    def test_plan_refuses_nonpositive_risk(self):
        plan = build_candidate_trade_plan(
            current_price=10.0,
            atr=0.0,
            trading_range={},
            signal={},
            signal_tier="confirmed",
            sub_phase="lps",
        )
        self.assertEqual(plan["status"], "unavailable")
        self.assertEqual(plan["unavailable_reason"], "atr_or_range_missing")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement price rules as one pure module**

Use these rules:

```python
breakout_buffer = max(0.3 * atr, 0.005 * resistance)

# Forming SOS: confirmation/cancellation only; it is not an entry plan.
trigger = resistance + breakout_buffer
cancel_below = resistance - atr
targets = []
position_cap_pct = 0

# Confirmed SOS/JAC: enter only above the box and cap chase distance.
trigger = resistance + breakout_buffer
stop = resistance - atr
targets = [resistance + range_height, resistance + 2 * range_height]
max_chase = trigger + 0.5 * atr

# Spring/LPS/ST/PRE_MARKUP/BU: use support-zone entry and structural failure.
entry_low = support
entry_high = support + 0.5 * atr
trigger = entry_high
stop = support - 0.5 * atr
targets = [resistance, resistance + range_height]
max_chase = entry_high
```

Round prices to two decimals. For confirmed rows, calculate `risk_reward_main = (targets[1] - trigger) / (trigger - stop)` only when both numerator and denominator are positive. Set `validity_bars = max(0, 3 - age_bars)` for forming and `max(0, EVENT_MAX_AGE[sub_phase] - age_bars)` for confirmed. If required inputs are missing, return `status="unavailable"` and a reason instead of fabricated levels. A formal recommendation requires `risk_reward_main >= 2.0`; early-watch rows contain no targets, risk/reward, max-chase, or position allocation.

- [ ] **Step 4: Attach the plan using already-fetched K-line and Wyckoff data**

In `run_phase2`, calculate ATR14 from the existing K-line rows, call the pure helper, and add `item["trade_plan"]`. Do not start a second network or technical-analysis pipeline.

- [ ] **Step 5: Render and test all execution fields**

Add columns/fields for `行动`, `触发/入场区间`, `止损`, `目标1/目标2`, `R:R`, `最高追价`, `单股仓位上限`, and `有效期`. Actionable and waiting rows with `trade_plan.status != "ready"` or `risk_reward_main < 2.0` must be downgraded to observation with reason `trade_plan_unavailable` or `risk_reward_below_2`; early-watch rows may remain visible but must show the missing reason.

Apply this trade-plan eligibility predicate before slicing to `max_recommendations`. An invalid top-ranked row must be demoted first so the next valid row can backfill the actionable/waiting slot:

```python
trade_ready = [
    item for item in eligible
    if item.get("trade_plan", {}).get("status") == "ready"
    and item.get("trade_plan", {}).get("risk_reward_main", 0) >= 2.0
]
trade_rejected = [item for item in eligible if item not in trade_ready]
actionable = trade_ready[:limit] if policy.get("mode") == "actionable" else []
waiting = trade_ready[:limit] if policy.get("mode") == "waiting_trigger" else []
```

Add `trade_rejected` to observation with its explicit reason. Add a regression test where the highest-ranked row has `R:R=1.5`, the second has `R:R=2.2`, and the second row fills the only available recommendation slot.

After bucket limits are known, set a conservative per-stock cap without overriding the market cap:

```python
position_cap_pct = min(
    15,
    policy["max_portfolio_pct"] / max(1, len(promoted)),
)
```

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: all exit 0.

- [ ] **Step 6: Commit the execution-plan slice**

```bash
git add .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_candidate_trade_plan.py .claude/skills/stock-trend/tests/test_stock_scanner.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: add candidate execution plans"
```

---

### Task 5: Add continuous market/sector opportunity scoring in shadow mode

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:88-101`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:448-477`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:877-895`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1349-1430`

- [ ] **Step 1: Write failing factor-boundary and no-behavior-change tests**

Add both methods below inside the existing `TestRecommendationPolicy(unittest.TestCase)` class.

```python
def test_shadow_opportunity_score_is_bounded_and_separate(self):
    item = candidate("x", adjusted_score=80)
    item["sector_score"] = 75
    scored = dc.attach_opportunity_shadow(
        item, {"score": 70, "data_date": "2026-08-18"})
    self.assertEqual(scored["quality_adjusted_score"], 80)
    self.assertGreaterEqual(scored["market_factor_shadow"], 0.85)
    self.assertLessEqual(scored["market_factor_shadow"], 1.05)
    self.assertGreaterEqual(scored["sector_factor_shadow"], 0.90)
    self.assertLessEqual(scored["sector_factor_shadow"], 1.10)


def test_default_ranking_ignores_shadow_score(self):
    high_quality = candidate("quality", adjusted_score=80)
    high_quality["opportunity_score_shadow"] = 60
    high_shadow = candidate("shadow", adjusted_score=70)
    high_shadow["opportunity_score_shadow"] = 90
    selected = dc.select_candidate_pool(
        [high_quality, high_shadow], top=2, min_score=50,
        ranking_mode="current")
    self.assertEqual([row["code"] for row in selected], ["quality", "shadow"])
```

- [ ] **Step 2: Run the candidate test and verify RED**

Expected: FAIL because the shadow helper and ranking mode are absent.

- [ ] **Step 3: Implement bounded, interpretable shadow factors**

```python
def attach_opportunity_shadow(item, regime):
    market_score = max(0.0, min(100.0, float((regime or {}).get("score", 50))))
    sector_score = max(0.0, min(100.0, float(item.get("sector_score", 50) or 50)))
    market_factor = 0.85 + market_score / 100.0 * 0.20
    sector_factor = 0.90 + sector_score / 100.0 * 0.20
    quality = float(item.get("quality_adjusted_score", 0) or 0)
    item["market_factor_shadow"] = round(market_factor, 3)
    item["sector_factor_shadow"] = round(sector_factor, 3)
    item["opportunity_score_shadow"] = round(
        quality * market_factor * sector_factor, 1)
    return item
```

Add `--ranking-mode current|opportunity-shadow` with default `current`. In `opportunity-shadow`, display the alternative ordering but do not change `recommendations`, `waiting_trigger`, policy caps, or portfolio limits. Store the alternative list under `shadow_ranking`.

- [ ] **Step 4: Verify cliff safety**

Add tests for market scores 59, 60, 79, and 80 proving that policy modes/caps remain exactly as today while shadow scores change smoothly.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: add shadow opportunity ranking"
```

---

### Task 6: Measure confirmation delay and early-warning conversion

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py:91-130`
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py:224-400`

- [ ] **Step 1: Write failing paired-event metric tests**

```python
def test_timing_pair_records_delay_and_entry_gap():
    candidate = {
        "code": "600001", "event": "sos", "event_date": "20260801",
        "tier": "forming", "date": "20260801", "close": 10.0,
    }
    confirmed = {
        "code": "600001", "event": "sos", "event_date": "20260801",
        "tier": "confirmed", "date": "20260803", "close": 10.5,
        "confirmation_delay_bars": 2,
    }
    result = _build_timing_pairs([candidate, confirmed])
    assert result[0]["confirmation_delay_bars"] == 2
    assert result[0]["entry_price_gap_pct"] == 0.05
```

Add a synthetic integration test where one forming SOS confirms within three bars and another expires. Assert `forming_count`, `confirmed_count`, 1/3/5/10-day conversion, cancellation rate, median delay, and expired count exactly.

- [ ] **Step 2: Run the backtest test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: FAIL because timing pairs are not produced.

- [ ] **Step 3: Record daily observations without future leakage**

Add `--timing-study`, which forces `sample_interval=1`. For every as-of slice, record `forming` and `confirmed` decisions with event/detection dates and the as-of close. Pair only rows with the same `(ts_code, event, event_date)`; never match solely by phase or nearest date.

Output:

```python
"timing": {
    "forming_count": 0,
    "confirmed_count": 0,
    "expired_count": 0,
    "conversion_rate": None,
    "conversion_within_bars": {"1": None, "3": None, "5": None, "10": None},
    "cancellation_rate": None,
    "confirmation_delay_bars": {"mean": None, "median": None, "p90": None},
    "entry_price_gap_pct": {"mean": None, "median": None},
    "paired_forward_returns": {"forming": {}, "confirmed": {}},
}
```

Keep the existing default backtest output compatible when `--timing-study` is absent.

- [ ] **Step 4: Add evidence status**

Set:

```python
"evidence_status": "sufficient" if paired_count >= 30 else "evidence_insufficient"
```

Do not recommend threshold changes when fewer than 30 paired confirmations exist. Continue disclosing costs, stop-loss omission, and survivorship bias.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git add .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "feat: measure Wyckoff confirmation delay"
```

---

### Task 7: Persist immutable official snapshots and evaluate recommendation outcomes

**Files:**

- Create: `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py`
- Create: `.claude/skills/stock-trend/scripts/backtesting/recommendation_tracker.py`
- Create: `.claude/skills/stock-trend/tests/test_recommendation_tracker.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1432-end`

- [ ] **Step 1: Write failing immutability and maturity tests**

```python
import tempfile
import unittest
from pathlib import Path


class TestRecommendationTracker(unittest.TestCase):
    def test_official_snapshot_cannot_be_overwritten_with_different_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = save_official_snapshot(
                root, "2026-08-18", {"model_version": "v1"})
            self.assertTrue(path.exists())
            with self.assertRaises(SnapshotConflict):
                save_official_snapshot(
                    root, "2026-08-18", {"model_version": "v2"})

    def test_tracker_marks_unmatured_window_pending(self):
        snapshot = {
            "recommendation_date": "2026-08-18",
            "recommendations": [{
                "code": "600001", "ts_code": "600001.SH",
                "trade_plan": {"stop_loss": 9.0, "targets": [12.0, 14.0]},
            }],
        }
        kline_map = {
            "600001.SH": {"data": [
                {"date": f"202608{day:02d}", "open": 10.0,
                 "high": 10.5, "low": 9.8, "close": 10.2}
                for day in range(18, 25)
            ]},
        }
        result = evaluate_snapshot(
            snapshot, kline_map, eval_windows=(5, 10, 20))
        self.assertEqual(result["windows"]["5"]["status"], "complete")
        self.assertEqual(result["windows"]["20"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_recommendation_tracker.py
```

Expected: FAIL because snapshot/tracker modules do not exist.

- [ ] **Step 3: Save one official post-close snapshot per evidence date**

Write snapshots under:

```text
.cache/stock-trend/recommendation_history/YYYY-MM-DD.json
```

Required fields:

```python
{
    "schema_version": 1,
    "model_version": "daily-candidates-timing-v1",
    "recommendation_date": "YYYY-MM-DD",
    "generated_at": "ISO-8601",
    "market_regime": {},
    "sectors": [],
    "policy": {},
    "recommendations": [],
    "waiting_trigger": [],
    "early_watch": [],
    "observation": [],
    "shadow_ranking": [],
}
```

Save only for a non-intraday run whose expected date equals the current official evidence date. If the file exists with the same canonical JSON hash, return it unchanged; if the hash differs, raise `SnapshotConflict` and leave the original intact.

- [ ] **Step 4: Evaluate matured snapshots**

The tracker must calculate 5/10/20/60-day absolute return, HS300-relative return when index data is available, maximum favorable/adverse excursion, stop/target hits, and candidate-to-confirmed conversion. Use next-trading-day open as the default simulated entry, reject suspended or one-price-limit-up rows when those fields are available, and accept explicit `--commission-bps`, `--slippage-bps`, and `--stamp-duty-bps` parameters instead of embedding a possibly stale statutory fee. If stop and target are both touched in one daily bar, resolve the stop first. Group results by regime band, sector type, signal tier, Wyckoff sub-phase, and rank (`Top1`, `Top3`, `Top5`).

Add `--json` output and return `evidence_insufficient` unless both conditions hold:

```python
official_days >= 20 and matured_confirmed_signals >= 100
```

- [ ] **Step 5: Define the only allowed ranking promotion gate**

`opportunity_score_shadow` may become the default ordering only in a later, explicit change when an out-of-sample or forward snapshot comparison shows all of:

```text
matured_confirmed_signals >= 100
out-of-sample or forward holdout signals >= 30
20-day cost-adjusted alpha > 0
Top3 20-day alpha >= current-ranking Top3 alpha
confirmed win-rate degradation <= 5 percentage points
maximum adverse excursion no worse by more than 10%
```

This task records the verdict but does not switch the production default.

- [ ] **Step 6: Run tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_recommendation_tracker.py
git add .claude/skills/stock-trend/scripts/core/recommendation_snapshot.py .claude/skills/stock-trend/scripts/backtesting/recommendation_tracker.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_recommendation_tracker.py
git commit -m "feat: track recommendation outcomes"
```

---

### Task 8: Update contracts and run repository quality gates

**Files:**

- Modify: `.claude/skills/stock-trend/SKILL.md`
- Modify: `docs/daily-recommendation-optimization.md`
- Test: `.claude/skills/stock-trend/tests/test_stock_trend.py`
- Test: `.claude/skills/stock-trend/tests/test_golden.py`

- [ ] **Step 1: Document the additive output contract**

Update `/candidates` documentation to state:

```text
confirmed：仍是唯一可进入今日可执行/等待触发的维科夫信号
forming：只进入“提前预警（非买入）”，不占推荐数量和仓位
trade_plan：缺少有效 ATR/箱体/正向盈亏比时不得升级为正式推荐
opportunity_score_shadow：仅供验证，不改变默认排序
```

Document `--timing-study`, the immutable snapshot path, evidence thresholds, and the fact that ranking promotion requires a separate explicit change.

- [ ] **Step 2: Update optimization status without overstating validation**

Mark early warning, trade-plan output, timing study, and snapshot tracker as implemented only after their tests pass. Keep “production ranking edge” pending until the forward evidence gate passes.

- [ ] **Step 3: Run all targeted suites**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_recommendation_tracker.py
```

Expected: every command exits 0.

- [ ] **Step 4: Run both mandatory Python quality gates**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: both exit 0. Do not regenerate golden snapshots unless each intentional output change has been reviewed and documented.

- [ ] **Step 5: Check diff quality and commit docs**

```bash
git diff --check
git status --short
git add .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define recommendation timing rollout"
```

---

## Acceptance criteria

- On a fixed fixture, enabling early-watch leaves the confirmed code set, order, scores, actionable rows, and waiting rows byte-for-byte unchanged.
- Existing `is_buy_signal(candidate_event)` remains `False`; all existing confirmed-buy tests continue to pass.
- A Spring/SOS candidate is visible for at most three bars in `early_watch`, never in actionable/waiting/next-day-confirmation/observation, and never contributes to portfolio limits.
- Forming rows do not trigger capital/fundamental requests and do not count toward confirmed pass, final-valid, or scan early-stop metrics.
- Multi-batch telemetry accumulates confirmed/forming/request counts instead of overwriting earlier batches.
- A stock code occurs in exactly one primary bucket: actionable, waiting, early-watch, or observation.
- All old JSON keys remain present; consumers that ignore new fields continue to work.
- Actionable and waiting rows contain a valid trade plan with positive risk, positive reward, and explicit validity, or are downgraded with a reason.
- Formal recommendations expose two targets, `R:R >= 2.0`, and a per-stock cap that never exceeds the market portfolio cap.
- Trade-plan rejection happens before recommendation limits so lower-ranked valid rows backfill available slots.
- `quality_adjusted_score` remains a data-quality score; market/sector shadow factors use separate fields.
- Market policy behavior at 59/60/79/80 is unchanged in this plan.
- Default scanner behavior remains confirmed-only; only `/candidates` opts into retaining forming events.
- Timing study pairs events by stable identity, uses only as-of data, and reports delay/entry-gap evidence status.
- Official recommendation snapshots are immutable and do not overwrite an earlier run for the same evidence date.
- Production ranking remains `current` until a separate change cites sufficient forward evidence.
- Warm-cache and all-source-failure candidate runs remain within the existing 60-second budget; sector ranking is requested at most once and each confirmed stock/data-type at most once.
- All targeted tests plus `test_stock_trend.py` and `test_golden.py --diff` pass.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Early-watch is mistaken for a recommendation | Use a separate bucket, `accepted=false`, explicit “非买入”, and exclude it from caps |
| More forming rows increase capital/fundamental I/O | Keep forming on the K-line-only lane and assert capital/fundamental requests equal confirmed-lane size |
| Trade levels imply false precision | Derive only from ATR/range, round consistently, and emit `unavailable` when inputs are missing |
| Shadow factors double-count sector strength | Store separate fields, preserve raw/quality scores, and do not enable by default |
| Backtest pairs unrelated episodes | Pair by `(ts_code, event, event_date)` and require daily sampling for timing mode |
| Snapshot reruns rewrite history | Canonical hash plus conflict-on-difference behavior |
| Small samples drive threshold changes | Require explicit sample gates and a separate production-default change |
| Report expansion harms readability | Keep early-watch capped at five and support compact JSON consumers |

## Recommended execution order

Execute Tasks 1–3 first to deliver safe timing visibility. Execute Task 4 next for user actionability. Tasks 5–7 establish evidence and must remain shadow/forward-only. Task 8 is the release gate. Do not combine Tasks 1–4 with a production ranking switch in the same commit or release.

## Stop conditions

- Stop and keep forming signals out of output if the scanner cannot preserve confirmed-only default behavior.
- Stop and downgrade a row if trade-plan inputs cannot produce positive risk and reward.
- Stop ranking promotion when evidence is insufficient or any outcome guardrail fails.
- Stop release if either mandatory quality gate fails; do not regenerate snapshots to bypass the failure.

## Self-review checklist

- [x] Every recommendation-timing requirement maps to Tasks 1–3 or 6.
- [x] Every actionability requirement maps to Task 4.
- [x] Market/sector smoothing remains shadow-only until Tasks 5 and 7 provide evidence.
- [x] Backward compatibility and no-future-data requirements have explicit tests.
- [x] Mandatory repository quality gates are included.
- [x] No source implementation is performed by this planning document.

## Disclaimer

本方案及其后续生成的分析仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
