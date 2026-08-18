# Intraday-to-Close Recommendation Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/candidates` conservative and explainable across intraday and post-close runs by separating stable constituent evidence from volatile sector rankings, preventing provisional intraday rows from looking executable, and recording run-to-run drift before tuning score formulas.

**Architecture:** Keep stock signal scoring, market policy, sector evidence, and source health as separate decisions. Same-session constituent caches may remain valid only when their live origin and completeness are proven; cached sector rankings remain observation-only because market heat is time-sensitive. Store compact candidate-run audit snapshots and compare same-day runs, but never freeze the morning universe when a healthy closing scan shows genuine afternoon rotation.

**Tech Stack:** Python 3.10+, existing `unittest` test modules, JSON cache artifacts under `.cache/stock-trend/`, existing Markdown/HTML report renderers.

---

## Why this plan is narrower than the first diagnosis

The 2026-08-18 pair exposed three distinct conditions:

1. The market regime changed from an intraday estimate of 81.4 to a closing score of 61.2.
2. Closing sector ranking and membership requests failed and fell back to cache.
3. The report showed `数据维度覆盖率 100%` while the funnel said `数据合格 0`.

These should not be solved by one score penalty. Constituent membership normally changes slowly, sector ranking changes intraday, and market-regime projection has a separate estimation error. This plan therefore does not hard-code a new intraday blend curve, does not freeze the morning candidate universe, and does not introduce arbitrary Wyckoff signal-age decay. Those changes require paired-run evidence first.

This plan complements `docs/superpowers/plans/2026-08-18-daily-recommendation-timing-actionability.md`; it does not implement that plan's forming-signal or trade-plan scope.

## Behavioral decisions

- Intraday scans may expose `盘中候选（收盘确认）`, but never `今日可执行`; machine-readable `recommendations` and `waiting_trigger` remain empty until a post-close run.
- A same-session constituent cache is eligible only when the cache records a successful live origin, collection time, non-empty record count, and completeness. Legacy or unknown-origin caches remain observation-only.
- A cached sector ranking is never sufficient for a post-close recommendation, even if its calendar date is today. It may still support a degraded observation report.
- A healthy post-close run is allowed to replace morning candidates. Candidate overlap is audited and explained rather than forcibly stabilized.
- Signal age is reported and backtested in shadow mode. No production score decay is enabled under this plan.

## File map

| File | Responsibility |
|---|---|
| `.claude/skills/stock-trend/scripts/fetchers/sector_data.py` | Persist verified constituent-cache provenance and completeness |
| `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` | Classify constituent evidence without conflating it with stock-dimension coverage |
| `.claude/skills/stock-trend/scripts/scans/daily_candidates.py` | Intraday watch-only policy, volatile ranking gate, audit integration, report semantics |
| `.claude/skills/stock-trend/scripts/core/candidate_run_audit.py` | Pure snapshot and same-day run comparison logic |
| `.claude/skills/stock-trend/scripts/analysis/market_regime.py` | Persist projection inputs needed for later calibration |
| `.claude/skills/stock-trend/scripts/backtesting/intraday_regime_calibration.py` | Measure intraday-to-close score error by time bucket without changing production scoring |
| `.claude/skills/stock-trend/tests/test_stock_scanner.py` | Constituent provenance and eligibility contracts |
| `.claude/skills/stock-trend/tests/test_daily_candidates.py` | Ranking gate, intraday buckets, report wording, snapshot integration |
| `.claude/skills/stock-trend/tests/test_candidate_run_audit.py` | Candidate overlap and reason attribution |
| `.claude/skills/stock-trend/tests/test_market_regime.py` | Projection-input persistence contract |
| `.claude/skills/stock-trend/tests/test_intraday_regime_calibration.py` | Pairing, sample gate, MAE and bias calculations |
| `.claude/skills/stock-trend/SKILL.md` | Updated `/candidates` intraday and data-quality contract |
| `docs/daily-recommendation-optimization.md` | Delivery status, evidence gate, and operational interpretation |

---

### Task 1: Separate constituent provenance from stock-dimension coverage

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:397`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1204`
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [ ] **Step 1: Write failing tests for verified same-session cache and unknown cache**

Add tests that make the intended distinction explicit:

```python
def test_same_session_verified_membership_cache_keeps_base_eligibility(self):
    base = {
        "eligible": True, "coverage": 1.0, "coverage_factor": 1.0,
        "freshness_factor": 1.0, "reasons": [],
    }
    membership = {
        "membership_source": "cache",
        "membership_data_date": "2026-08-18",
        "membership_quality": "verified_cache",
        "membership_cache_origin": "realtime",
        "membership_cached_at": "2026-08-18T10:44:00",
        "membership_record_count": 25,
        "membership_complete": True,
    }
    result = sc.apply_membership_quality(
        base, membership, as_of_date="2026-08-18")
    self.assertTrue(result["eligible"])
    self.assertEqual(result["membership_evidence"], "verified_cache")
    self.assertEqual(result["coverage"], 1.0)


def test_unknown_origin_membership_cache_is_observation_only(self):
    base = {
        "eligible": True, "coverage": 1.0, "coverage_factor": 1.0,
        "freshness_factor": 1.0, "reasons": [],
    }
    membership = {
        "membership_source": "cache",
        "membership_data_date": "2026-08-18",
        "membership_quality": "degraded",
    }
    result = sc.apply_membership_quality(
        base, membership, as_of_date="2026-08-18")
    self.assertFalse(result["eligible"])
    self.assertEqual(result["membership_evidence"], "unverified_cache")
    self.assertIn("sector_membership_unverified", result["reasons"])
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: FAIL because cache-origin/completeness metadata and `membership_evidence` do not exist.

- [ ] **Step 3: Save provenance with live constituent snapshots**

Change the constituent cache payload to this additive shape:

```python
payload = {
    "cached_at": datetime.now().isoformat(),
    "origin_source": "realtime",
    "record_count": len(stocks),
    "complete": bool(stocks),
    "stocks": stocks,
}
```

When loading a cache, propagate:

```python
row.update({
    "membership_source": "cache",
    "membership_quality": (
        "verified_cache"
        if payload.get("origin_source") == "realtime"
        and payload.get("complete")
        and payload.get("record_count", 0) > 0
        else "degraded"
    ),
    "membership_cache_origin": payload.get("origin_source", "unknown"),
    "membership_cached_at": payload.get("cached_at", ""),
    "membership_record_count": payload.get("record_count", 0),
    "membership_complete": bool(payload.get("complete", False)),
})
```

Do not infer `verified_cache` for legacy payloads missing provenance.

- [ ] **Step 4: Classify membership evidence without changing dimension coverage**

Update `apply_membership_quality` so only realtime-good and verified same-date caches preserve base eligibility:

```python
verified_realtime = (
    source == "realtime"
    and membership_quality == "good"
    and not date_mismatch
)
verified_cache = (
    source == "cache"
    and membership_quality == "verified_cache"
    and membership.get("membership_cache_origin") == "realtime"
    and membership.get("membership_complete") is True
    and membership.get("membership_record_count", 0) > 0
    and not date_mismatch
)
quality["membership_evidence"] = (
    "realtime" if verified_realtime
    else "verified_cache" if verified_cache
    else "stale" if date_mismatch
    else "unverified_cache"
)
if not (verified_realtime or verified_cache):
    quality["eligible"] = False
    reason = (
        "sector_membership_stale"
        if date_mismatch else "sector_membership_unverified"
    )
    if reason not in quality["reasons"]:
        quality["reasons"].append(reason)
```

Do not multiply `freshness_factor` merely because a verified constituent snapshot came from cache. Sector ranking freshness is handled in Task 2.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run the same command. Expected: exit 0.

- [ ] **Step 6: Commit the provenance contract**

```bash
git add .claude/skills/stock-trend/scripts/fetchers/sector_data.py .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "fix: distinguish verified sector caches"
```

---

### Task 2: Gate recommendations on volatile sector-ranking evidence and fix report semantics

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:354`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:913`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1194`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1277`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: Write failing tests for cached-ranking promotion and report fields**

```python
def test_cached_sector_ranking_never_promotes_post_close_candidate(self):
    item = candidate("600001")
    item.update({
        "ranking_source": "cache",
        "ranking_quality": "degraded",
        "ranking_data_date": "2026-08-18",
        "sector_ranking_evidence": "degraded_cache",
    })
    buckets = classify_candidates([item], {
        "mode": "actionable", "max_recommendations": 5,
        "max_portfolio_pct": 60, "reasons": [],
    })
    self.assertEqual(buckets["actionable"], [])
    self.assertIn(
        "sector_ranking_unconfirmed",
        buckets["observation"][0]["observation_reasons"],
    )


def test_report_separates_dimension_coverage_from_recommendation_eligibility(self):
    item = candidate("600001", eligible=False)
    item["data_quality"].update({
        "coverage": 1.0,
        "membership_evidence": "unverified_cache",
        "reasons": ["sector_membership_unverified"],
    })
    report = generate_report(
        [item], [("BK1", "测试板块", 70)], 1.0,
        {"mode": "observation", "max_recommendations": 0,
         "max_portfolio_pct": 0, "reasons": []},
        {"actionable": [], "waiting_trigger": [],
         "next_day_confirmation": [], "observation": [item]},
    )
    self.assertIn("数据维度覆盖率", report)
    self.assertIn("板块证据", report)
    self.assertIn("推荐资格", report)
    self.assertIn("100%", report)
    self.assertIn("否", report)
```

- [ ] **Step 2: Run the daily-candidate tests and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL because `sector_ranking_evidence` is not a promotion gate and the report lacks separate evidence/eligibility columns.

- [ ] **Step 3: Add explicit volatile-ranking evidence**

When sector rankings are loaded, assign:

```python
sector["sector_ranking_evidence"] = (
    "realtime"
    if sector.get("ranking_source") == "realtime"
    and sector.get("ranking_quality") == "good"
    and sector.get("ranking_data_date") == as_of_date
    else "degraded_cache"
    if sector.get("ranking_source") == "cache"
    else "unavailable"
)
```

Propagate the field into each candidate. Update both `_is_final_valid_candidate` and `classify_candidates` to require `sector_ranking_evidence == "realtime"` for scan early-stop eligibility, `actionable`, and `waiting_trigger`. Add `sector_ranking_unconfirmed` to observation reasons otherwise so scan stopping and final classification cannot disagree.

- [ ] **Step 4: Replace the ambiguous funnel and row labels**

Render these separate counts:

```python
dimension_complete = sum(
    item.get("data_quality", {}).get("coverage", 0) >= 0.70
    for item in candidates
)
recommendation_eligible = sum(
    item.get("data_quality", {}).get("eligible", False)
    and item.get("sector_actionable", True)
    and item.get("score_eligible", True)
    and item.get("sector_ranking_evidence") == "realtime"
    for item in candidates
)
```

Use this report wording:

```text
筛选漏斗：板块 N → 候选 N → 维科夫买点 N → 维度完整 N → 推荐资格 N → 可执行 N/等待 N
```

Add `板块证据` and `推荐资格` columns while retaining `数据维度覆盖率` for backward-readable diagnostics. The JSON fields `coverage` and `eligible` remain backward compatible.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the same command. Expected: exit 0.

- [ ] **Step 6: Commit the ranking gate and report correction**

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "fix: separate coverage from recommendation eligibility"
```

---

### Task 3: Make intraday output watch-only for a swing-trading workflow

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1095`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1134`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: Replace the current provisional-actionable tests with watch-only tests**

```python
def test_intraday_strong_regime_is_watch_only(self):
    policy = build_recommendation_policy(
        {"score": 85, "data_date": "2026-08-18"},
        "2026-08-18", market_open=True,
    )
    self.assertEqual(policy["mode"], "intraday_watch")
    self.assertEqual(policy["max_recommendations"], 0)
    self.assertEqual(policy["max_portfolio_pct"], 0)
    self.assertEqual(policy["projected_close_mode"], "actionable")


def test_intraday_candidates_never_enter_compatibility_recommendation_fields(self):
    policy = {
        "mode": "intraday_watch", "projected_close_mode": "actionable",
        "max_recommendations": 0, "max_portfolio_pct": 0,
        "reasons": ["intraday_provisional"], "provisional": True,
    }
    buckets = classify_candidates([candidate("600001")], policy)
    self.assertEqual(buckets["actionable"], [])
    self.assertEqual(buckets["waiting_trigger"], [])
    self.assertEqual([row["code"] for row in buckets["intraday_watch"]],
                     ["600001"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run the daily-candidate test module. Expected: FAIL because intraday currently preserves actionable/waiting modes.

- [ ] **Step 3: Preserve projected mode but zero execution authority**

After calculating the score-based close mode, convert market-open policy to:

```python
if market_open:
    projected = policy["mode"]
    policy.update({
        "mode": "intraday_watch",
        "projected_close_mode": projected,
        "max_recommendations": 0,
        "max_portfolio_pct": 0,
        "provisional": True,
        "reasons": list(dict.fromkeys(
            (policy.get("reasons") or []) + ["intraday_provisional"]
        )),
    })
```

Add `intraday_watch` to the bucket payload. It contains only candidates that would otherwise pass stock, data, ranking and sector gates; rows failing those gates remain in observation.

- [ ] **Step 4: Render a non-executable intraday section**

Use the heading `盘中候选（收盘确认，非推荐）`. Do not render intraday candidates under `今日可执行` or `等待触发`. Keep compatibility JSON fields empty and add the new additive `intraday_watch` field.

- [ ] **Step 5: Update the skill contract**

Change `/candidates` documentation to state that intraday scoring still computes the projected close tier, but recommendations and portfolio authority remain zero until a post-close `/daily-review` plus `/candidates` run confirms them.

- [ ] **Step 6: Run tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py .claude/skills/stock-trend/SKILL.md
git commit -m "fix: make intraday candidates watch only"
```

Expected: tests pass; no intraday row appears as executable.

---

### Task 4: Record and explain same-day candidate drift without freezing the universe

**Files:**

- Create: `.claude/skills/stock-trend/scripts/core/candidate_run_audit.py`
- Create: `.claude/skills/stock-trend/tests/test_candidate_run_audit.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1394`
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: Write failing pure comparison tests**

```python
class CandidateRunAuditTest(unittest.TestCase):
    def test_compare_runs_reports_overlap_adds_drops_and_source_change(self):
        prior = {
            "run_id": "20260818-104552", "session": "intraday",
            "ranking_source": "realtime",
            "candidates": [
                {"code": "600189", "quality_score": 70.1},
                {"code": "600191", "quality_score": 67.8},
                {"code": "000553", "quality_score": 66.9},
            ],
        }
        current = {
            "run_id": "20260818-174838", "session": "close",
            "ranking_source": "cache",
            "candidates": [
                {"code": "600189", "quality_score": 56.3},
                {"code": "600191", "quality_score": 54.3},
                {"code": "600129", "quality_score": 60.5},
            ],
        }
        result = compare_runs(prior, current)
        self.assertEqual(result["overlap_count"], 2)
        self.assertEqual(result["union_count"], 4)
        self.assertEqual(result["jaccard"], 0.5)
        self.assertEqual(result["added_codes"], ["600129"])
        self.assertEqual(result["dropped_codes"], ["000553"])
        self.assertEqual(result["primary_cause"], "source_degradation")
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_run_audit.py
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement compact immutable snapshots and comparison**

The module must expose:

```python
AUDIT_DIR = CACHE_DIR / "candidate_runs"


def build_run_snapshot(run_id, as_of_date, session, regime, candidates,
                       performance):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "as_of_date": as_of_date,
        "session": session,
        "regime": regime,
        "ranking_source": performance.get(
            "ranking_snapshot_source", "unknown"),
        "source_health": performance.get("source_health", {}),
        "candidates": [{
            "code": row["code"],
            "raw_score": row.get("raw_composite_score",
                                 row.get("composite_score", 0)),
            "quality_score": row.get("quality_adjusted_score", 0),
            "sector_code": row.get("sector_code", ""),
            "ranking_evidence": row.get("sector_ranking_evidence", ""),
            "membership_evidence": row.get("data_quality", {}).get(
                "membership_evidence", ""),
            "observation_reasons": row.get("observation_reasons", []),
        } for row in candidates],
    }
```

`compare_runs` computes intersection, union, Jaccard, added/dropped codes and median overlapping score delta. Set `primary_cause=source_degradation` when prior ranking is realtime and current ranking is not; otherwise use `market_or_sector_rotation`.

Write snapshots atomically to `.cache/stock-trend/candidate_runs/YYYY-MM-DD/<run_id>.json`. Retain 30 trading dates.

- [ ] **Step 4: Integrate comparison into report generation**

After candidates are frozen, load only the latest earlier snapshot from the same date. Add a `盘中—盘后变化审计` section containing overlap, Jaccard, additions/removals, score delta, prior/current source and `primary_cause`.

This section is diagnostic only. It must not alter candidate scores, ordering or buckets.

- [ ] **Step 5: Run focused tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_run_audit.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
git add .claude/skills/stock-trend/scripts/core/candidate_run_audit.py .claude/skills/stock-trend/tests/test_candidate_run_audit.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: audit same-day candidate drift"
```

Expected: both modules pass and degraded-source drift is explained without freezing the candidate universe.

---

### Task 5: Measure intraday market-regime error before changing the blend formula

**Files:**

- Modify: `.claude/skills/stock-trend/scripts/analysis/market_regime.py:820`
- Modify: `.claude/skills/stock-trend/tests/test_market_regime.py`
- Create: `.claude/skills/stock-trend/scripts/backtesting/intraday_regime_calibration.py`
- Create: `.claude/skills/stock-trend/tests/test_intraday_regime_calibration.py`

- [ ] **Step 1: Write failing tests for projection metadata and paired calibration**

```python
def test_intraday_context_exposes_projection_inputs():
    ctx = _collect_intraday_fixture(now=datetime(2026, 8, 18, 10, 45))
    projection = ctx["regime_projection"]
    self.assertEqual(projection["method"], "last_close_blend_v1")
    self.assertGreater(projection["elapsed_fraction"], 0)
    self.assertIn("anchor_score", projection)
    self.assertIn("extrapolated_score", projection)
    self.assertIn("blend_weight", projection)


def test_calibration_reports_bias_and_mae_by_time_bucket():
    rows = [
        {"date": "2026-08-01", "minute": 75,
         "projected": 81.4, "close": 61.2},
        {"date": "2026-08-02", "minute": 80,
         "projected": 70.0, "close": 66.0},
    ]
    result = calibrate(rows, minimum_sessions=20)
    bucket = result["buckets"]["61-90"]
    self.assertEqual(bucket["sample_count"], 2)
    self.assertAlmostEqual(bucket["mean_error"], 12.1)
    self.assertAlmostEqual(bucket["mae"], 12.1)
    self.assertEqual(result["status"], "evidence_insufficient")
```

- [ ] **Step 2: Run both focused tests and verify RED**

Expected: FAIL because projection metadata and the calibration module do not exist.

- [ ] **Step 3: Persist projection inputs without altering production score**

Add this object to intraday context:

```python
ctx["regime_projection"] = {
    "method": "last_close_blend_v1",
    "elapsed_fraction": round(fraction, 4),
    "elapsed_minutes": int(round(fraction * 240)),
    "anchor_score": round(_safe_float(anchor_score), 1),
    "extrapolated_score": round(ext_regime["score"], 1),
    "blend_weight": round(w, 4),
    "projected_score": regime["score"],
}
```

Post-close context uses `regime_projection = None`. Include this field in candidate-run audit snapshots through the existing regime payload.

- [ ] **Step 4: Implement evidence-gated calibration**

`calibrate(rows, minimum_sessions=20)` must pair intraday projections with the same date's close score and report time-bucket sample count, mean error, MAE, RMSE, over-80 false-positive rate and 80-th percentile absolute error. Return `evidence_insufficient` until at least 20 distinct paired sessions exist overall and at least 10 exist in a bucket.

The script prints JSON and performs no writes other than an explicitly supplied `--output` path. It does not modify `_blend_weight`.

- [ ] **Step 5: Run focused tests and commit**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_market_regime.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_intraday_regime_calibration.py
git add .claude/skills/stock-trend/scripts/analysis/market_regime.py .claude/skills/stock-trend/tests/test_market_regime.py .claude/skills/stock-trend/scripts/backtesting/intraday_regime_calibration.py .claude/skills/stock-trend/tests/test_intraday_regime_calibration.py
git commit -m "feat: measure intraday regime projection error"
```

Expected: tests pass; production market score remains unchanged.

---

### Task 6: Document the evidence gate and run complete verification

**Files:**

- Modify: `docs/daily-recommendation-optimization.md`
- Modify: `.claude/skills/stock-trend/SKILL.md`
- Verify all files changed above

- [ ] **Step 1: Document what is intentionally deferred**

Record these explicit gates:

```text
- Intraday blend parameters may change only after >=20 paired sessions overall
  and >=10 sessions in the affected time bucket.
- Wyckoff signal-age penalties remain shadow-only until each proposed age bin
  has >=50 matured signals and improves 5/10/20-day out-of-sample outcomes.
- Candidate-universe overlap is diagnostic; it is never a target to maximize
  when both scans have healthy realtime sources.
```

- [ ] **Step 2: Run targeted tests**

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_candidate_run_audit.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_market_regime.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_intraday_regime_calibration.py
```

Expected: all commands exit 0.

- [ ] **Step 3: Run required stock-trend quality gates**

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: both commands exit 0. Do not regenerate golden snapshots merely to accept changed wording or numbers; inspect every diff and update snapshots only when the intended contract change is confirmed.

- [ ] **Step 4: Run static diff checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only plan-scoped files are modified.

- [ ] **Step 5: Commit documentation and verification state**

```bash
git add .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs: define candidate consistency evidence gates"
```

---

## Final acceptance criteria

- A report can no longer show `覆盖率 100%` as if that alone implied recommendation eligibility.
- Same-session verified constituent caches do not incorrectly zero stock-data eligibility.
- Cached/degraded sector rankings cannot produce executable or waiting recommendations.
- Intraday candidates are visibly watch-only and carry zero portfolio authority.
- Same-day candidate churn is quantified and attributed to source degradation or healthy market/sector rotation.
- Intraday blend changes remain evidence-gated; no arbitrary coefficient or signal-age penalty enters production.
- Existing JSON recommendation fields remain backward compatible, with new fields only additive.
- Required stock-trend and golden-diff gates pass.

本计划用于提升系统可靠性与学习参考质量，不构成任何投资建议。
