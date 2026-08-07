# Candidate Data Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the daily candidate workflow useful during transient sector API outages while ensuring stale, unknown-date, or partial cached data can never be promoted as a live actionable recommendation.

**Status:** Implemented and verified on 2026-08-07. Review hardening also covers sparse successful responses, partial AKShare sub-sources, verified cache dates, and separate ranking/constituent provenance in reports.

**Architecture:** Reuse the existing sector-ranking cache in `daily_candidates` and add a small per-sector constituent snapshot beside the existing stock-trend cache. Every fallback carries source, date, and quality metadata; `stock_scanner` propagates that metadata into candidate quality and forces prior-date membership data into the observation pool.

**Tech Stack:** Python 3.10, `unittest`, JSON file cache, existing stock-trend quality gates.

---

### Task 1: Sector-ranking fallback for daily candidates

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [x] **Step 1: Write the failing test**

```python
def test_pick_hot_sectors_uses_fresh_rankings_cache_when_live_sources_fail(self):
    row = {
        "code": "BK1", "name": "缓存板块", "change_pct": 2.0,
        "up_count": 9, "down_count": 1, "main_force_net": 1e8,
    }
    cached = {
        "cached_at": "2026-08-06T15:10:00",
        "rankings": {"meta": {"total_sectors": 1}, "sectors": [row]},
    }
    history = {
        date: [{"code": "BK1", "hot_score": 70, "net_flow": 1e8}]
        for date in ("2026-08-04", "2026-08-05", "2026-08-06")
    }
    with patch("fetchers.sector_data.get_sector_rankings",
               return_value={"meta": {"total_sectors": 0}, "sectors": []}), \
         patch("fetchers.sector_data.load_rankings_cache_full",
               return_value=cached), \
         patch("fetchers.sector_data.load_snapshot_history",
               return_value=history):
        sectors = dc.pick_hot_sectors(as_of_date="2026-08-06")
    self.assertEqual(sectors[0]["ranking_source"], "cache")
    self.assertEqual(sectors[0]["ranking_data_date"], "2026-08-06")
```

- [x] **Step 2: Run test to verify it fails**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: FAIL because `pick_hot_sectors` does not load or expose ranking cache metadata.

- [x] **Step 3: Write minimal implementation**

```python
def load_sector_rankings(as_of_date=""):
    rankings = get_sector_rankings()
    active = sum(
        1 for row in rankings.get("sectors", [])
        if (row.get("up_count", 0) or 0) > 0
        or (row.get("down_count", 0) or 0) > 0
    )
    if active:
        save_rankings_cache(rankings)
        append_daily_snapshot(rankings, override_date=as_of_date)
        return rankings, {"source": "realtime", "data_date": as_of_date,
                          "quality": "good"}
    payload = load_rankings_cache_full()
    if payload:
        return payload["rankings"], {
            "source": "cache",
            "data_date": payload["cached_at"][:10],
            "quality": "degraded",
        }
    return rankings, {"source": "error", "data_date": "", "quality": "error"}
```

Attach the metadata to every ranked sector. Existing persistence logic remains the authority for `sector_actionable`.

- [x] **Step 4: Run test to verify it passes**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`

Expected: all daily-candidate tests pass.

### Task 2: Per-sector constituent snapshot fallback

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`
- Test: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [x] **Step 1: Write the failing tests**

```python
def test_sector_stocks_live_response_is_saved_with_source_metadata(self):
    payload = {"rc": 0, "data": {"diff": [{
        "f12": "600001", "f14": "测试股份", "f3": 1.2,
        "f8": 2e8, "f20": 1e10, "f37": 20,
    }]}}
    with tempfile.TemporaryDirectory() as tmpdir, \
         patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
         patch.object(sd, "_fetch_json", return_value=payload):
        stocks = sd.get_sector_stocks("BK0001")
        cache_file = Path(tmpdir) / "BK0001.json"
        self.assertTrue(cache_file.exists())
    self.assertEqual(stocks[0]["membership_source"], "realtime")

def test_sector_stocks_falls_back_to_snapshot_after_live_failure(self):
    payload = {"rc": 0, "data": {"diff": [{
        "f12": "600001", "f14": "测试股份", "f3": 1.2,
        "f8": 2e8, "f20": 1e10, "f37": 20,
    }]}}
    with tempfile.TemporaryDirectory() as tmpdir, \
         patch.object(sd, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)):
        with patch.object(sd, "_fetch_json", return_value=payload):
            sd.get_sector_stocks("BK0001")
        with patch.object(sd, "_fetch_json", side_effect=RuntimeError("dns")):
            stocks = sd.get_sector_stocks("BK0001")
    self.assertEqual(stocks[0]["membership_source"], "cache")
    self.assertEqual(stocks[0]["membership_quality"], "degraded")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: FAIL because constituent snapshots and metadata do not exist.

- [x] **Step 3: Write minimal implementation**

```python
SECTOR_STOCKS_CACHE_DIR = CACHE_DIR / "sector_stocks"
SECTOR_STOCKS_MAX_AGE_HOURS = 24 * 30

def save_sector_stocks_cache(sector_code, stocks):
    payload = {"cached_at": datetime.now().isoformat(), "stocks": stocks}
    SECTOR_STOCKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = SECTOR_STOCKS_CACHE_DIR / f"{sector_code}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def load_sector_stocks_cache(sector_code):
    path = SECTOR_STOCKS_CACHE_DIR / f"{sector_code}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    age = datetime.now() - datetime.fromisoformat(payload["cached_at"])
    if age.total_seconds() > SECTOR_STOCKS_MAX_AGE_HOURS * 3600:
        return None
    return payload if payload.get("stocks") else None
```

On successful live fetch, return rows tagged `membership_source=realtime`, `membership_quality=good`, and save the untagged data. On live failure, return cached rows tagged `membership_source=cache`, `membership_quality=degraded`, and `membership_data_date` from `cached_at`; re-raise only when no usable snapshot exists.

- [x] **Step 4: Run tests to verify they pass**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: all stock-scanner tests pass.

### Task 3: Observation-only gate for degraded constituent membership

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- Test: `.claude/skills/stock-trend/tests/test_stock_scanner.py`

- [x] **Step 1: Write the failing test**

```python
def test_cached_sector_membership_is_observation_only(self):
    candidate = _make_candidate()
    candidate.update({"membership_source": "cache",
                      "membership_quality": "degraded",
                      "membership_data_date": "2026-08-05"})
    sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
    sc._fetch_kline = lambda ts: _make_dated_kline(60, ts)
    sc._fetch_capital_flow = lambda ts: {
        "data": [{"date": "20260806", "main_net_inflow": 0}]
    }
    result = sc.run_phase2(
        [candidate], enable_wyckoff=True, as_of_date="2026-08-06")
    self.assertFalse(result[0]["data_quality"]["eligible"])
    self.assertIn("sector_membership_stale",
                  result[0]["data_quality"]["reasons"])
```

- [x] **Step 2: Run test to verify it fails**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: FAIL because membership quality is not part of the candidate gate.

- [x] **Step 3: Write minimal implementation**

```python
membership_source = c.get("membership_source", "realtime")
if membership_source != "realtime":
    data_quality["eligible"] = False
    membership_date = c.get("membership_data_date", "")
    reason = (
        "sector_membership_stale"
        if as_of_date and membership_date != as_of_date
        else "sector_membership_cached"
    )
    data_quality["reasons"].append(reason)
    data_quality["freshness_factor"] *= 0.8
```

Propagate membership metadata from `gather_candidates` into `run_phase2` output.

- [x] **Step 4: Run tests to verify they pass**

Run: `/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`

Expected: all stock-scanner tests pass and degraded membership remains observable.

### Task 4: Full repository verification

**Files:**
- Verify all files changed above.

- [x] **Step 1: Run targeted tests**

```bash
/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

- [x] **Step 2: Run required stock-trend quality gates**

```bash
/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/jing.li7/.pyenv/shims/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

- [x] **Step 3: Check diff quality**

```bash
git diff --check
git status --short
```

Expected: all commands exit zero; no golden snapshots are regenerated.
