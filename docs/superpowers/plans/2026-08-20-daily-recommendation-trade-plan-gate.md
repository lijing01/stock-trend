# Daily Recommendation Trade-Plan Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Only label a stock as 今日可执行 when the recommendation date has a complete, internally consistent, risk-bounded trade plan; otherwise downgrade it with precise reasons.

**Architecture:** Add a pure A-share trade-plan module that orchestrates existing technical-analysis functions over K-lines already loaded by stock_scanner.run_phase2(). Attach an additive trade_plan before classification, apply the plan gate before recommendation limits, and preserve existing score, quality, candidate, and bucket fields.

**Tech Stack:** Python 3.10, unittest, pandas-backed existing technical analysis, existing Wyckoff output, JSON/Markdown/HTML.

---

## Scope and fixed decisions

- Scope is A-share /candidates; ETF plan behavior is unchanged.
- Do not reuse the ETF scanner's assumed 55% win-rate/Kelly sizing. Use a 0.5% portfolio-risk budget and the existing market portfolio cap.
- Formal 今日可执行 requires a complete plan. Waiting candidates may carry a numeric trigger but remain non-recommendations.
- Intraday recommendations must be empty. Preserve the score-derived target tier in policy.provisional_target_mode and mark all intraday rows intraday_provisional.
- v1 fields: basis date/price, entry zone, confirmation, invalidation, stop, three ordered targets, recomputed R:R, position cap, 20–120 session horizon, three-session validity, counterargument, and event-check status.
- Primary-target minimum R:R is 1.5. It is a versioned rule, not tuned in this change.
- Event integration is out of scope. Emit event_check.status=not_implemented and retain the manual announcement-review warning.

## File map

- Create: .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py — construction, validation, reason codes, policy constants.
- Create: .claude/skills/stock-trend/tests/test_candidate_trade_plan.py — pure contract tests.
- Modify: .claude/skills/stock-trend/scripts/scans/stock_scanner.py:1274-1640 — attach plans without new fetches.
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:96-102,799-941,1123-1607 — pass policy, gate, render, revise intraday semantics.
- Modify: .claude/skills/stock-trend/tests/test_stock_scanner.py and .claude/skills/stock-trend/tests/test_daily_candidates.py:38-1815 — integration and compatibility.
- Modify: .claude/skills/stock-trend/tests/test_stock_trend.py:1420-1440 — main gate registration.
- Modify: .claude/skills/stock-trend/SKILL.md:251-291 and docs/daily-recommendation-optimization.md:281-299,377-382 — public contract.

### Task 1: Lock the pure trade-plan contract

**Files:**
- Create: .claude/skills/stock-trend/tests/test_candidate_trade_plan.py

- [ ] **Step 1: Add deterministic fixtures**

~~~python
def make_kline(start=100.0, rows=80):
    data = []
    for i in range(rows):
        close = start + i * 0.12
        data.append({
            "trade_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
            "open": close - 0.15, "high": close + 0.8,
            "low": close - 0.8, "close": close,
            "pre_close": close - 0.12,
            "vol": 1_000_000 + i * 10_000,
        })
    return {"meta": {"adj": "qfq"}, "data": data}


def strong_policy():
    return {"mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "provisional": False}
~~~

- [ ] **Step 2: Test the complete schema and price relations**

~~~python
def test_complete_plan_has_stable_schema_and_price_ordering():
    plan = build_candidate_trade_plan(
        code="600000", kline=make_kline(),
        wyckoff={"sub_phase": "LPS", "confidence": 0.72},
        policy=strong_policy(), basis_date="2026-08-20",
        counterargument="跌破结构支撑且放量时逻辑失效")
    verdict = validate_trade_plan(plan, strong_policy(), "2026-08-20")
    assert verdict["complete"] is True
    assert verdict["reasons"] == []
    assert plan["schema_version"] == "candidate-trade-plan/v1"
    assert plan["entry"]["low"] <= plan["entry"]["high"]
    assert plan["stop_loss"]["price"] < plan["entry"]["low"]
    assert plan["entry"]["high"] < plan["targets"]["conservative"]
    assert plan["targets"]["conservative"] < plan["targets"]["primary"]
    assert plan["targets"]["primary"] < plan["targets"]["aggressive"]
    assert plan["risk_reward"]["recomputed"] >= 1.5
    assert 0 < plan["position"]["max_portfolio_pct"] <= 12
    assert plan["horizon"] == {"min_trading_days": 20, "max_trading_days": 120}
    assert plan["validity"]["trading_sessions"] == 3
~~~

- [ ] **Step 3: Test validation reason codes, finite numbers, and immutability**

Cover missing entry, NaN/Infinity, stop above entry, unordered/missing targets, stored R:R that disagrees with recomputation, position above policy, missing confirmation/invalidation/counterargument, wrong basis date, and action != buy. Deep-copy every input and assert validation does not mutate it.

Required reason codes:

~~~python
{
    "trade_plan_missing", "trade_plan_wrong_date",
    "trade_plan_missing_entry", "trade_plan_missing_confirmation",
    "trade_plan_missing_invalidation", "trade_plan_invalid_stop",
    "trade_plan_missing_targets", "trade_plan_targets_unordered",
    "trade_plan_rr_below_min", "trade_plan_position_over_policy",
    "trade_plan_missing_counterargument", "trade_plan_event_status_missing",
    "trade_plan_not_ready",
}
~~~

- [ ] **Step 4: Test risk-budget sizing**

~~~python
def test_position_uses_half_percent_risk_and_equal_weight_cap():
    assert calculate_position_pct(100, 95, 60, 5, 0.5) == 10.0
    assert calculate_position_pct(100, 99, 60, 5, 0.5) == 12.0
    assert calculate_position_pct(100, 100, 60, 5, 0.5) == 0.0
~~~

- [ ] **Step 5: Add run_candidate_trade_plan_tests() and verify RED**

Run:

~~~bash
python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
~~~

Expected: FAIL with ModuleNotFoundError: core.candidate_trade_plan.

- [ ] **Step 6: Commit failing tests**

~~~bash
git add .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
git commit -m "test: define candidate trade-plan contract"
~~~

### Task 2: Implement the pure builder and validator

**Files:**
- Create: .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py
- Test: .claude/skills/stock-trend/tests/test_candidate_trade_plan.py

- [ ] **Step 1: Define constants and numeric normalization**

~~~python
SCHEMA_VERSION = "candidate-trade-plan/v1"
MIN_PRIMARY_RR = 1.5
RISK_BUDGET_PCT = 0.5
VALID_TRADING_SESSIONS = 3
HORIZON = {"min_trading_days": 20, "max_trading_days": 120}


def _finite_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
~~~

- [ ] **Step 2: Implement risk-budget sizing**

~~~python
def calculate_position_pct(entry_price, stop_price, max_portfolio_pct,
                           max_recommendations, risk_budget_pct=RISK_BUDGET_PCT):
    entry = _finite_positive(entry_price)
    stop = _finite_positive(stop_price)
    if not entry or not stop or stop >= entry:
        return 0.0
    stop_pct = (entry - stop) / entry * 100
    risk_based = risk_budget_pct / stop_pct * 100
    equal_weight_cap = float(max_portfolio_pct) / max(1, int(max_recommendations))
    return round(max(0.0, min(risk_based, equal_weight_cap, 20.0)), 1)
~~~

- [ ] **Step 3: Build by orchestrating existing technical functions**

Use analysis.technical.calc_ma_signals, calc_rsi, calc_bollinger, calc_adx, calc_atr, calc_support_resistance, calc_risk_reward, and calc_entry_signals; do not copy their formulas.

~~~python
def build_candidate_trade_plan(code, kline, wyckoff, policy, basis_date,
                               counterargument):
    df = _normalized_frame(kline)
    indicators = {
        "ma": calc_ma_signals(df, [5, 10, 20, 60]),
        "rsi": calc_rsi(df),
        "bollinger": calc_bollinger(df),
        "adx": calc_adx(df),
    }
    indicators["summary"] = build_summary(
        indicators, patterns=[], data_points=len(df))
    atr = calc_atr(df)
    levels = calc_support_resistance(
        df, indicators["ma"], indicators["bollinger"],
        atr_pct=atr.get("atr_pct"), adx_value=indicators["adx"].get("adx"),
        atr_absolute=atr.get("atr"))
    risk = calc_risk_reward(df, atr, levels, direction="bullish", is_etf=False)
    timing = calc_entry_signals(
        df, indicators, rr_ratio=risk.get("risk_reward_ratio"), is_etf=False)
    return _assemble_plan(code, basis_date, df, atr, levels, risk, timing,
                          wyckoff, policy, counterargument)
~~~

Assembly rules:

- latest close is basis_price;
- entry zone is bounded to [close - 0.75*ATR, close + 0.25*ATR], preferring nearest valid support as lower edge; if that lower edge is not above the computed stop, lift it to stop + 0.25*ATR;
- use ordered technical targets; otherwise use entry_high + 1R/2R/3R;
- action=buy only for timing verdict ready; watch/wait becomes wait; all else avoid;
- size from worst entry (entry_high), stop, risk budget, and policy cap;
- store supplied and recomputed R:R; recomputed is authoritative.

- [ ] **Step 4: Implement validation by recomputing R:R**

~~~python
def validate_trade_plan(plan, policy, expected_date=None):
    reasons = []
    if not isinstance(plan, dict):
        return {"complete": False, "recomputed_rr": None,
                "reasons": ["trade_plan_missing"]}
    entry, targets = plan.get("entry") or {}, plan.get("targets") or {}
    low, high = _finite_positive(entry.get("low")), _finite_positive(entry.get("high"))
    stop = _finite_positive((plan.get("stop_loss") or {}).get("price"))
    conservative = _finite_positive(targets.get("conservative"))
    primary = _finite_positive(targets.get("primary"))
    aggressive = _finite_positive(targets.get("aggressive"))
    if expected_date and plan.get("basis_date") != expected_date:
        reasons.append("trade_plan_wrong_date")
    if not low or not high or low > high:
        reasons.append("trade_plan_missing_entry")
    if not plan.get("confirmation"):
        reasons.append("trade_plan_missing_confirmation")
    if not plan.get("invalidation"):
        reasons.append("trade_plan_missing_invalidation")
    if not stop or not low or stop >= low:
        reasons.append("trade_plan_invalid_stop")
    if not all((conservative, primary, aggressive)):
        reasons.append("trade_plan_missing_targets")
    elif not (high < conservative < primary < aggressive):
        reasons.append("trade_plan_targets_unordered")
    rr = round((primary - high) / (high - stop), 2) if high and stop and primary and high > stop else None
    if rr is None or rr < MIN_PRIMARY_RR:
        reasons.append("trade_plan_rr_below_min")
    position = _finite_positive((plan.get("position") or {}).get("max_portfolio_pct"))
    if not position or position > float(policy.get("max_portfolio_pct", 0)):
        reasons.append("trade_plan_position_over_policy")
    if not plan.get("counterargument"):
        reasons.append("trade_plan_missing_counterargument")
    event_status = (plan.get("event_check") or {}).get("status")
    if event_status not in {"not_implemented", "checked_no_known_event",
                            "checked_event_present"}:
        reasons.append("trade_plan_event_status_missing")
    if plan.get("action") != "buy":
        reasons.append("trade_plan_not_ready")
    return {"complete": not reasons, "recomputed_rr": rr,
            "reasons": list(dict.fromkeys(reasons))}
~~~

- [ ] **Step 5: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
git add .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
git commit -m "feat: build risk-bounded candidate trade plans"
~~~

Expected: all tests PASS.

### Task 3: Attach plans without new network calls

**Files:**
- Modify: .claude/skills/stock-trend/scripts/scans/stock_scanner.py:1274-1640
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:799-941,1532-1546
- Test: .claude/skills/stock-trend/tests/test_stock_scanner.py

- [ ] **Step 1: Test attachment and stable fetch counts**

~~~python
def test_phase2_attaches_plan_from_loaded_kline():
    with patch.object(sc, "_fetch_kline", return_value=good_kline()) as fetch,          patch.object(sc, "build_candidate_trade_plan", return_value=complete_plan()):
        result = sc.run_phase2(
            [candidate_fixture()], enable_wyckoff=True,
            as_of_date="2026-08-20",
            recommendation_policy=strong_policy())
    assert result[0]["trade_plan"]["schema_version"] == "candidate-trade-plan/v1"
    assert fetch.call_count == 1
~~~

Also test builder errors retain the candidate with trade_plan=None, trade_plan_status=error, and trade_plan_reasons=[trade_plan_build_error].

- [ ] **Step 2: Add optional policy plumbing**

Change the existing `run_phase2()` signature at `stock_scanner.py:1274` by
appending `recommendation_policy=None` after `metrics=None`. Add the same
optional parameter to `scan_sectors()` and pass it in both the primary and
compatibility calls. Existing callers that omit the argument must retain their
current output.

- [ ] **Step 3: Attach and validate after score construction**

~~~python
if recommendation_policy is not None:
    try:
        item["trade_plan"] = build_candidate_trade_plan(
            c["code"], kline, item.get("wyckoff", {}),
            recommendation_policy, as_of_date,
            _candidate_counterargument(item))
        verdict = validate_trade_plan(
            item["trade_plan"], recommendation_policy, as_of_date)
        item["trade_plan_status"] = "complete" if verdict["complete"] else "incomplete"
        item["trade_plan_reasons"] = verdict["reasons"]
    except (KeyError, TypeError, ValueError):
        item["trade_plan"] = None
        item["trade_plan_status"] = "error"
        item["trade_plan_reasons"] = ["trade_plan_build_error"]
~~~

- [ ] **Step 4: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "feat: attach plans to daily candidates"
~~~

Expected: tests PASS and K-line request counts are unchanged.

### Task 4: Gate formal actionability and intraday semantics

**Files:**
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:96-102,1175-1280
- Test: .claude/skills/stock-trend/tests/test_daily_candidates.py:38-69,1433-1610

- [ ] **Step 1: Extend the default test candidate with a complete plan**

Existing strong-market tests retain intent. Missing/invalid-plan tests delete or alter it explicitly.

- [ ] **Step 2: Add gate, limit-backfill, and immutability tests**

~~~python
def test_actionable_requires_complete_trade_plan():
    row = candidate("missing")
    row.pop("trade_plan")
    row["trade_plan_status"] = "missing"
    buckets = classify_candidates([row], strong_policy())
    assert buckets["actionable"] == []
    assert "trade_plan_missing" in buckets["observation"][0]["observation_reasons"]


def test_invalid_plan_does_not_consume_limit():
    rows = [candidate("bad")] + [candidate(str(i)) for i in range(1, 6)]
    rows[0]["trade_plan_status"] = "incomplete"
    rows[0]["trade_plan_reasons"] = ["trade_plan_rr_below_min"]
    buckets = classify_candidates(rows, strong_policy())
    assert [row["code"] for row in buckets["actionable"]] == [str(i) for i in range(1, 6)]
~~~

Deep-copy rows before classification and assert inputs/plans remain unchanged.

- [ ] **Step 3: Apply plan predicate before limits**

~~~python
def _trade_plan_promotable(item):
    return (item.get("trade_plan_status") == "complete"
            and isinstance(item.get("trade_plan"), dict)
            and item["trade_plan"].get("action") == "buy")
~~~

Add it to the existing eligible predicate; append trade_plan_reasons to observation reasons; slice eligible[:limit] only after all gates.

- [ ] **Step 4: Make intraday non-actionable**

After deriving the score tier:

~~~python
policy.update({
    "mode": "observation", "max_recommendations": 0,
    "max_portfolio_pct": 0, "provisional": True,
    "provisional_target_mode": previous_mode,
})
~~~

Update intraday tests so actionable/waiting are empty and candidates carry intraday_provisional.

- [ ] **Step 5: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: gate actionable picks on complete plans"
~~~

### Task 5: Render plans consistently

**Files:**
- Modify: .claude/skills/stock-trend/scripts/scans/daily_candidates.py:1123-1462
- Test: .claude/skills/stock-trend/tests/test_daily_candidates.py:1612-1815

- [ ] **Step 1: Test additive JSON, identical values, and HTML escaping**

Keep candidates/recommendations/waiting_trigger/observation keys. Assert recommendations carry the same plan. Put confirmation=收盘 > 10.5 & 量能确认 and assert HTML contains 收盘 &gt; 10.5 &amp; 量能确认.

- [ ] **Step 2: Add one shared compact renderer**

~~~python
def _trade_plan_text(item):
    plan = item.get("trade_plan") or {}
    entry, stop = plan.get("entry") or {}, plan.get("stop_loss") or {}
    targets, rr = plan.get("targets") or {}, plan.get("risk_reward") or {}
    position = plan.get("position") or {}
    return (
        f"入场{entry.get('low','-')}~{entry.get('high','-')} | "
        f"止损{stop.get('price','-')} | "
        f"目标{targets.get('conservative','-')}/{targets.get('primary','-')}/{targets.get('aggressive','-')} | "
        f"R:R {rr.get('recomputed','-')} | 仓位≤{position.get('max_portfolio_pct','-')}% | "
        f"有效{(plan.get('validity') or {}).get('trading_sessions','-')}个交易日")
~~~

Escape all free text with html.escape(). Deep-copy JSON outputs so renderers cannot mutate them.

- [ ] **Step 3: Run and commit**

~~~bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: render actionable trade plans"
~~~

### Task 6: Register gates and update docs

**Files:**
- Modify: .claude/skills/stock-trend/tests/test_stock_trend.py:1420-1440
- Modify: .claude/skills/stock-trend/SKILL.md:251-291
- Modify: docs/daily-recommendation-optimization.md:281-299,377-382

- [ ] **Step 1: Register run_candidate_trade_plan_tests() before daily candidate integration tests**

- [ ] **Step 2: Document formal/post-close actionability, schema, downgrade reasons, intraday observation-only behavior, manual event review, and JSON compatibility**

- [ ] **Step 3: Run mandatory gates**

~~~bash
python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
python3 -m py_compile .claude/skills/stock-trend/scripts/core/candidate_trade_plan.py .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py
git diff --check
~~~

Expected: all exit 0. Golden diff contains only intentional additive plan fields/text; do not regenerate snapshots to hide unrelated changes.

- [ ] **Step 4: Commit documentation and gate wiring**

~~~bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define formal recommendation actionability"
~~~

## Completion criteria

- Intraday output has no recommendations.
- Every formal recommendation has a valid candidate-trade-plan/v1.
- R:R is recomputed from entry-high, stop, and primary target and is at least 1.5.
- Invalid plans do not consume limits and expose stable reasons.
- Plan generation adds no market-data request.
- JSON remains additive; Markdown and HTML show identical values.
- Targeted, main, golden, compile, and diff gates pass.
