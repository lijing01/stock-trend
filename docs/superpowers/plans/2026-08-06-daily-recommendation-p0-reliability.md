# Daily Recommendation P0 Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重做评分模型的前提下，让 `/candidates` 只把数据有效、市场环境允许且通过绝对门槛的候选升级为“今日推荐”，并先修正维科夫回测口径。

**Architecture:** 将落地拆成四个可独立发布的 P0 批次：回测正确性、候选数据质量、市场环境门控、热点与扩池修正。新增一个纯函数模块统一评估候选数据质量；`stock_scanner` 保留原始复合分并附加质量元数据；`daily_candidates` 负责推荐政策和行动分层，保持 `candidates` JSON 字段向后兼容。

**Tech Stack:** Python 3.11+、标准库 `datetime/json/unittest`、现有自定义测试运行器、Markdown/HTML 报告

---

## 交付优先级与边界

| 发布批次 | 优先级 | 交付结果 | 前置依赖 | 本计划覆盖 |
|---|---:|---|---|---|
| R0-A | P0 | 修正前向收益与基准样本，回测结果可信 | 无 | 是 |
| R0-B | P0 | 候选携带数据日期、覆盖率、资格和降级原因 | R0-A | 是 |
| R0-C | P0 | 市场环境决定可执行/等待/观察，不再只显示警告 | R0-B | 是 |
| R0-D | P0 | 热点使用绝对门槛，按过滤后有效数量扩池 | R0-C | 是 |
| R1-A | P1 | 入场区间、止损、目标、R:R、仓位和有效期 | R0-D | 否，单独计划 |
| R1-B | P1 | 龙头/中军/低位转强宇宙及板块分散约束 | R0-D | 否，单独计划 |
| R1-C | P1 | 完整 Top 3/Top 5 生产链回测 | R1-A、R1-B | 否，单独计划 |
| R2-A | P2 | 每日推荐快照与 5/10/20/60 日归因 | R1-C | 否，单独计划 |

P0 明确不做以下变化：

- 不调整动量、量价、资金、基本面、板块和维科夫权重。
- 不新增新闻、事件日历或组合优化器。
- 不删除现有 `candidates` 输出；新增行动分层字段保持消费者兼容。
- 不把缺失数据改成负分；P0 只影响是否能升级为推荐。

## 文件结构

### 新增文件

- `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`：候选数据日期、覆盖率和推荐资格的纯函数。
- `.claude/skills/stock-trend/tests/test_recommendation_quality.py`：数据质量边界测试。
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`：市场门控、热点过滤、扩池停止条件和输出分层测试。

### 修改文件

- `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`：修正收益窗口和全样本基准。
- `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`：增加精确收益与无信号基准测试。
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`：接受 `as_of_date`，附加 `data_quality`。
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`：推荐政策、行动分层、绝对热点门槛和有效候选扩池。
- `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`：同时保留绝对热度和相对热度。
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`：验证质量元数据集成且复合分不变。
- `.claude/skills/stock-trend/tests/test_stock_trend.py`：接入新增测试套件和维科夫回测测试。
- `.claude/skills/stock-trend/SKILL.md`：更新 `/candidates` 行动分层和降级规则。
- `docs/daily-recommendation-optimization.md`：标记 P0 已落实的条目和最终接口。

---

### Task 1: 修正精确前向收益日期

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py:76-91`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py:96-106`

- [ ] **Step 1: 写精确收益失败测试**

将现有 `test_forward_return()` 替换为：

```python
def test_forward_return():
    rows = [
        {"date": f"202601{i + 1:02d}", "close": float(100 + i)}
        for i in range(12)
    ]
    result = _forward_return(rows, 0, rows[10]["date"])
    expected = round((110.0 - 100.0) / 100.0, 6)
    test("WBT-07: target date close is used", result == expected,
         f"got={result}, expected={expected}")
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: `WBT-07` FAIL，实际值使用索引 9 的收盘价。

- [ ] **Step 3: 修正 `_forward_return`**

用以下完整函数替换原实现：

```python
def _forward_return(kline, now_idx, target_date):
    """Return close-to-close performance at the first bar on/after target_date."""
    c_now = _safe_close(kline[now_idx])
    if not c_now or c_now <= 0:
        return None
    target = str(target_date).replace("-", "")
    for i in range(now_idx + 1, len(kline)):
        if _kline_date(kline[i]) >= target:
            c_fut = _safe_close(kline[i])
            if c_fut and c_fut > 0:
                return round((c_fut - c_now) / c_now, 6)
            return None
    return None
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: `WBT-07` PASS，测试套件零失败。

- [ ] **Step 5: 提交 R0-A 的第一部分**

```bash
git add .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "fix(backtest): use target-date close"
```

---

### Task 2: 让基准覆盖全部可计算股票日

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py:257-285`
- Test: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py:133-158`

- [ ] **Step 1: 写无可识别阶段时仍有基准的失败测试**

在测试文件顶部增加：

```python
from unittest.mock import patch
```

在 `test_run_backtest_synthetic()` 后增加：

```python
def test_baseline_does_not_depend_on_phase_detection():
    km = {
        "600519.SH": {"data": _mk_kline(1, 100.0)},
        "000001.SZ": {"data": _mk_kline(2, 50.0)},
    }
    stocks = [
        {"code": "600519", "ts_code": "600519.SH", "name": "t1"},
        {"code": "000001", "ts_code": "000001.SZ", "name": "t2"},
    ]
    with patch("backtesting.wyckoff_backtest.analyze_kline_dict",
               return_value={"meta": {"error": "unclassified"}}):
        result = run_backtest(
            stocks,
            km,
            lookback_days=80,
            eval_windows=(5,),
            sample_interval=5,
        )
    baseline = result["summary"]["5"]["baseline"]
    test("WBT-I08: baseline survives zero classifications",
         baseline is not None and baseline["count"] > 0,
         f"baseline={baseline}")
    test("WBT-I09: zero classifications produce zero signals",
         result["meta"]["signal_count"] == 0)
```

把后续测试编号顺延，并在 `run_wyckoff_backtest_tests()` 中调用该测试。

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: 新基准测试 FAIL，`baseline` 为 `None`。

- [ ] **Step 3: 将基准计算移到阶段识别之前**

用以下循环主体替换 `run_backtest()` 中 `for sidx in sample_indices:` 对应部分：

```python
    for sidx in sample_indices:
        date = all_dates[sidx]
        for s in valid:
            rows = rows_by_code[s["ts_code"]]
            sliced = slice_kline(rows, date)
            if len(sliced) < MIN_KLINES:
                continue

            now_idx = len(sliced) - 1
            fwd = {}
            for w in eval_windows:
                if sidx + w >= len(all_dates):
                    continue
                r_ = _forward_return(rows, now_idx, all_dates[sidx + w])
                if r_ is not None:
                    fwd[str(w)] = r_
                    baseline[str(w)].append(r_)
            if not fwd:
                continue

            analysis = analyze_kline_dict({"meta": {}, "data": sliced})
            sig = _classify_signal(analysis, min_confidence)
            if sig is None:
                continue
            if is_buy_point(sig["phase"], sig["sub_phase"]) \
                    and sig["confidence"] >= min_confidence:
                per_stock_raw[s["ts_code"]].append((sidx, date, sig, fwd))
```

- [ ] **Step 4: 运行维科夫回测测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: 全部 PASS，零信号时 `baseline.count > 0`。

- [ ] **Step 5: 提交 R0-A 的第二部分**

```bash
git add .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "fix(backtest): decouple baseline from signals"
```

---

### Task 3: 新增候选数据质量评估模块

**Files:**
- Create: `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`
- Create: `.claude/skills/stock-trend/tests/test_recommendation_quality.py`

- [ ] **Step 1: 写数据质量失败测试**

创建测试文件：

```python
#!/usr/bin/env python3
"""Tests for candidate recommendation data quality."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.recommendation_quality import assess_candidate_data, latest_data_date


def payload(rows, quality="good"):
    return {"summary": {"data_quality": quality}, "data": rows}


class TestRecommendationQuality(unittest.TestCase):
    def test_latest_date_accepts_trade_date_and_date(self):
        data = {"data": [{"trade_date": "20260805"}, {"date": "2026-08-06"}]}
        self.assertEqual(latest_data_date(data), "2026-08-06")

    def test_fresh_kline_and_one_secondary_dimension_are_eligible(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=None,
            as_of_date="2026-08-06",
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["coverage"], 0.8)

    def test_stale_kline_is_never_eligible(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260805"}]),
            capital=payload([{"date": "20260806"}]),
            fundamental=payload([], quality="good"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertIn("kline_stale", result["reasons"])

    def test_missing_secondary_dimensions_fail_coverage(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=None,
            fundamental=None,
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["coverage"], 0.55)
        self.assertIn("coverage_below_70pct", result["reasons"])

    def test_fundamental_error_is_missing(self):
        result = assess_candidate_data(
            kline=payload([{"trade_date": "20260806"}]),
            capital=None,
            fundamental=payload([], quality="error"),
            as_of_date="2026-08-06",
        )
        self.assertFalse(result["dimensions"]["fundamental"]["available"])


def run_recommendation_quality_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationQuality)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), \
        len(result.failures) + len(result.errors)


if __name__ == "__main__":
    _, failed = run_recommendation_quality_tests()
    raise SystemExit(1 if failed else 0)
```

- [ ] **Step 2: 运行测试确认模块不存在**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py
```

Expected: FAIL with `ModuleNotFoundError: core.recommendation_quality`。

- [ ] **Step 3: 创建纯函数模块**

创建实现文件：

```python
#!/usr/bin/env python3
"""Data-quality policy for promoting scan candidates to recommendations."""
from datetime import datetime


WEIGHTS = {"kline": 0.55, "capital": 0.25, "fundamental": 0.20}
MIN_COVERAGE = 0.70


def _iso_date(value):
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def latest_data_date(payload):
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    dates = [
        _iso_date(row.get("trade_date") or row.get("date"))
        for row in rows
        if isinstance(row, dict)
    ]
    valid = [value for value in dates if value]
    return max(valid) if valid else ""


def _dimension(payload, expected_date="", require_date=False):
    available = isinstance(payload, dict) and bool(payload)
    quality = "missing"
    if available:
        summary = payload.get("summary", {})
        quality = summary.get("data_quality") or "good"
        if quality == "error":
            available = False
    data_date = latest_data_date(payload)
    fresh = available
    if require_date:
        fresh = available and bool(data_date) and data_date >= expected_date
    return {
        "available": available,
        "fresh": fresh,
        "data_date": data_date,
        "quality": quality,
    }


def assess_candidate_data(kline, capital, fundamental, as_of_date=""):
    normalized_as_of = _iso_date(as_of_date)
    kline_date = latest_data_date(kline)
    expected = normalized_as_of or kline_date
    dimensions = {
        "kline": _dimension(kline, expected, require_date=True),
        "capital": _dimension(capital, expected, require_date=True),
        "fundamental": _dimension(fundamental),
    }
    coverage = round(sum(
        WEIGHTS[name]
        for name, status in dimensions.items()
        if status["available"] and (name == "fundamental" or status["fresh"])
    ), 2)
    reasons = []
    if not dimensions["kline"]["fresh"]:
        reasons.append("kline_stale")
    if coverage < MIN_COVERAGE:
        reasons.append("coverage_below_70pct")
    eligible = dimensions["kline"]["fresh"] and coverage >= MIN_COVERAGE
    return {
        "as_of_date": expected,
        "coverage": coverage,
        "eligible": eligible,
        "dimensions": dimensions,
        "reasons": reasons,
    }
```

- [ ] **Step 4: 运行模块测试确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py
```

Expected: 5 tests PASS。

- [ ] **Step 5: 提交 R0-B 的质量模块**

```bash
git add .claude/skills/stock-trend/scripts/core/recommendation_quality.py .claude/skills/stock-trend/tests/test_recommendation_quality.py
git commit -m "feat(candidates): assess recommendation data quality"
```

---

### Task 4: 将数据质量附加到候选结果

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:20-30,580-735`
- Test: `.claude/skills/stock-trend/tests/test_stock_scanner.py:88-165`

- [ ] **Step 1: 写复合分不变、资格随质量变化的失败测试**

在 `TestRunPhase2Funnel` 中增加：

```python
    def test_quality_metadata_does_not_change_composite_score(self):
        self.orig_analyze = sc.analyze_kline_dict
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts: {
            **_make_kline(60, ts),
            "data": [
                {**row, "trade_date": "20260806"}
                for row in _make_kline(60, ts)["data"]
            ],
        }
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }
        sc._fetch_fundamental = lambda ts: None
        baseline = sc.run_phase2(
            [_make_candidate("600001")], enable_wyckoff=True
        )[0]
        assessed = sc.run_phase2(
            [_make_candidate("600001")],
            enable_wyckoff=True,
            as_of_date="2026-08-06",
        )[0]
        self.assertEqual(assessed["composite_score"], baseline["composite_score"])
        self.assertTrue(assessed["data_quality"]["eligible"])
        self.assertEqual(assessed["data_quality"]["coverage"], 0.8)
```

另增加过期 K 线测试：

```python
    def test_stale_kline_remains_observable_but_not_eligible(self):
        sc.analyze_kline_dict = lambda kline: _wk(sub="lps", conf=0.6)
        sc._fetch_kline = lambda ts: {
            **_make_kline(60, ts),
            "data": [
                {**row, "trade_date": "20260805"}
                for row in _make_kline(60, ts)["data"]
            ],
        }
        sc._fetch_capital_flow = lambda ts: {
            "data": [{"date": "20260806", "main_net_inflow": 0}]
        }
        result = sc.run_phase2(
            [_make_candidate("600001")],
            enable_wyckoff=True,
            as_of_date="2026-08-06",
        )
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["data_quality"]["eligible"])
        self.assertIn("kline_stale", result[0]["data_quality"]["reasons"])
```

- [ ] **Step 2: 运行测试确认接口不存在**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py -v
```

Expected: FAIL，`run_phase2()` 不接受 `as_of_date`。

- [ ] **Step 3: 集成质量评估但保持原始评分**

增加导入：

```python
from core.recommendation_quality import assess_candidate_data
```

修改函数签名：

```python
def run_phase2(candidates, max_workers=4, enable_wyckoff=False,
               as_of_date=""):
```

在 `item` 字典创建前计算：

```python
        data_quality = assess_candidate_data(
            kline=kline,
            capital=cap,
            fundamental=fund,
            as_of_date=as_of_date,
        )
```

在 `item` 中增加字段，保留原有 `composite_score` 计算：

```python
            "data_quality": data_quality,
```

- [ ] **Step 4: 运行扫描器测试确认通过**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py -v
```

Expected: 全部 PASS；原复合分断言保持不变。

- [ ] **Step 5: 提交 R0-B 集成**

```bash
git add .claude/skills/stock-trend/scripts/scans/stock_scanner.py .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "feat(candidates): attach data quality metadata"
```

---

### Task 5: 建立市场环境推荐政策和行动分层

**Files:**
- Create: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:77-199,235-276`

- [ ] **Step 1: 写推荐政策失败测试**

创建测试文件：

```python
#!/usr/bin/env python3
"""Tests for /candidates recommendation policy."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans.daily_candidates import (
    build_recommendation_policy,
    classify_candidates,
    generate_report,
)


def candidate(code, eligible=True):
    return {
        "code": code,
        "name": f"测试{code}",
        "sector_name": "测试板块",
        "composite_score": 80.0,
        "wyckoff": {"sub_phase": "LPS", "confidence": 0.6},
        "signals": {},
        "data_quality": {
            "eligible": eligible,
            "coverage": 0.8 if eligible else 0.55,
            "reasons": [] if eligible else ["coverage_below_70pct"],
        },
    }


class TestRecommendationPolicy(unittest.TestCase):
    def test_missing_regime_allows_observation_only(self):
        policy = build_recommendation_policy(None, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertEqual(policy["max_recommendations"], 0)

    def test_stale_regime_allows_observation_only(self):
        regime = {"score": 90, "data_date": "2026-08-05"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("regime_stale", policy["reasons"])

    def test_weak_regime_allows_observation_only(self):
        regime = {"score": 59, "data_date": "2026-08-06"}
        self.assertEqual(
            build_recommendation_policy(regime, "2026-08-06")["mode"],
            "observation",
        )

    def test_intraday_output_is_provisional_observation(self):
        regime = {"score": 90, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True
        )
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_neutral_regime_limits_waiting_list_to_two(self):
        regime = {"score": 70, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2"), candidate("3")], policy
        )
        self.assertEqual(policy["mode"], "waiting_trigger")
        self.assertEqual(len(buckets["waiting_trigger"]), 2)
        self.assertEqual(len(buckets["observation"]), 1)

    def test_strong_regime_never_promotes_ineligible_candidate(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2", eligible=False)], policy
        )
        self.assertEqual([item["code"] for item in buckets["actionable"]], ["1"])
        self.assertEqual([item["code"] for item in buckets["observation"]], ["2"])

    def test_report_renders_all_buckets_and_full_disclaimer(self):
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        buckets = {
            "actionable": [candidate("1")],
            "waiting_trigger": [],
            "observation": [candidate("2", eligible=False)],
        }
        report = generate_report(
            buckets["actionable"] + buckets["observation"],
            [("BK1", "测试板块", 80)],
            1.0,
            policy,
            buckets,
        )
        self.assertIn("## 今日可执行", report)
        self.assertIn("## 等待触发", report)
        self.assertIn("## 观察池", report)
        self.assertIn("股市有风险，投资需谨慎", report)


def run_daily_candidates_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationPolicy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), \
        len(result.failures) + len(result.errors)


if __name__ == "__main__":
    _, failed = run_daily_candidates_tests()
    raise SystemExit(1 if failed else 0)
```

- [ ] **Step 2: 运行测试确认函数不存在**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL with missing imports。

- [ ] **Step 3: 实现推荐政策纯函数**

在 `load_regime_context()` 后增加：

```python
def build_recommendation_policy(regime, expected_date, market_open=False):
    if market_open:
        return {
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "reasons": ["intraday_provisional"],
        }
    if not regime or regime.get("score") is None:
        return {
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "reasons": ["regime_missing"],
        }
    if regime.get("data_date") != expected_date:
        return {
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "reasons": ["regime_stale"],
        }
    score = float(regime["score"])
    if score < 60:
        return {
            "mode": "observation",
            "max_recommendations": 0,
            "max_portfolio_pct": 0,
            "reasons": ["regime_weak"],
        }
    if score < 80:
        return {
            "mode": "waiting_trigger",
            "max_recommendations": 2,
            "max_portfolio_pct": 30,
            "reasons": [],
        }
    return {
        "mode": "actionable",
        "max_recommendations": 5,
        "max_portfolio_pct": 60,
        "reasons": [],
    }


def classify_candidates(candidates, policy):
    eligible = [
        item for item in candidates
        if item.get("data_quality", {}).get("eligible", False)
    ]
    ineligible = [item for item in candidates if item not in eligible]
    limit = policy.get("max_recommendations", 0)
    actionable = []
    waiting = []
    if policy.get("mode") == "actionable":
        actionable = eligible[:limit]
    elif policy.get("mode") == "waiting_trigger":
        waiting = eligible[:limit]
    promoted_codes = {item["code"] for item in actionable + waiting}
    observation = [
        item for item in candidates
        if item.get("code") not in promoted_codes
    ]
    return {
        "actionable": actionable,
        "waiting_trigger": waiting,
        "observation": observation,
    }
```

- [ ] **Step 4: 将政策接入主流程与 JSON**

在模块导入区增加：

```python
from core.cache_utils import is_trading_hours
```

在 `main()` 获取候选前增加：

```python
    regime = load_regime_context()
    expected_date = datetime.now().strftime("%Y-%m-%d")
    policy = build_recommendation_policy(
        regime, expected_date, market_open=is_trading_hours()
    )
```

调用扫描器时传入市场日期；市场环境无效时仍使用当前日期检查 K 线，但政策保持观察：

```python
    scored = scan_sectors(
        [c[0] for c in sector_codes],
        min_candidates=args.min_candidates,
        as_of_date=expected_date,
    )
```

排序截断后增加：

```python
    buckets = classify_candidates(candidates, policy)
```

在 JSON `out` 中保留 `candidates`，并新增：

```python
            "policy": policy,
            "recommendations": buckets["actionable"],
            "waiting_trigger": buckets["waiting_trigger"],
            "observation": buckets["observation"],
```

在 `_signal_text()` 后增加 Markdown 表格助手：

```python
def _append_candidate_table(lines, title, items, empty_text):
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append(f"> {empty_text}")
        return
    lines.extend([
        "| # | 名称(代码) | 板块 | 买点 | 置信度 | 综合分 | 覆盖率 | 信号 |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        lines.append(
            f"| {index} | {item['name']}({item['code']}) | "
            f"{item['sector_name']} | {wyckoff.get('sub_phase', '-')} | "
            f"{wyckoff.get('confidence', 0):.0%} | "
            f"{item['composite_score']:.1f} | "
            f"{quality.get('coverage', 0):.0%} | "
            f"{_signal_text(item.get('signals', {}))} |"
        )
```

用以下完整函数替换 `generate_report()`：

```python
def generate_report(candidates, sector_codes, elapsed, policy, buckets):
    lines = [
        "# 每日候选股",
        "",
        f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"扫描板块 {len(sector_codes)} 个 | 候选 {len(candidates)} 只 | "
        f"耗时 {elapsed:.0f}s",
        "",
        f"**推荐模式**: {policy['mode']} | "
        f"推荐上限 {policy['max_recommendations']} 只 | "
        f"组合仓位上限 {policy['max_portfolio_pct']}%",
    ]
    regime = load_regime_context()
    if regime and regime.get("score") is not None:
        lines.extend([
            "",
            f"**市场环境**: {regime['score']} {regime['label']} "
            f"(数据 {regime['data_date']}) — {regime.get('advice', '')}",
        ])
    if policy.get("reasons"):
        lines.extend([
            "",
            f"> ⚠️ 推荐降级: {', '.join(policy['reasons'])}",
        ])
    _append_candidate_table(
        lines, "今日可执行", buckets["actionable"], "今日无可执行推荐。"
    )
    _append_candidate_table(
        lines, "等待触发", buckets["waiting_trigger"], "暂无等待触发标的。"
    )
    _append_candidate_table(
        lines, "观察池", buckets["observation"], "观察池为空。"
    )
    lines.extend([
        "",
        "---",
        "",
        "*候选为维科夫买点与多维排序结果；只有“今日可执行”具备推荐资格。*",
        "",
        "**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**",
    ])
    return "\n".join(lines)
```

在 `_generate_html()` 中增加以下助手：

```python
def _html_candidate_rows(items):
    if not items:
        return '<tr><td colspan="8">无</td></tr>'
    rows = []
    for index, item in enumerate(items, 1):
        wyckoff = item.get("wyckoff", {})
        quality = item.get("data_quality", {})
        rows.append(
            f"<tr><td>{index}</td><td><strong>{item['name']}</strong><br>"
            f"<span style='color:#86868b;font-size:12px'>{item['code']}</span></td>"
            f"<td>{item['sector_name']}</td>"
            f"<td><span class='buy'>{wyckoff.get('sub_phase', '-')}</span></td>"
            f"<td>{wyckoff.get('confidence', 0):.0%}</td>"
            f"<td><strong>{item['composite_score']:.1f}</strong></td>"
            f"<td>{quality.get('coverage', 0):.0%}</td>"
            f"<td>{_signal_text(item.get('signals', {}))}</td></tr>"
        )
    return "".join(rows)
```

将 `_generate_html()` 签名改为：

```python
def _generate_html(candidates, sector_codes, elapsed, ts, policy, buckets):
```

删除原先单一候选表的 `rows` 构造，在返回模板前构造三个表体：

```python
    actionable_rows = _html_candidate_rows(buckets["actionable"])
    waiting_rows = _html_candidate_rows(buckets["waiting_trigger"])
    observation_rows = _html_candidate_rows(buckets["observation"])
    policy_note = (
        f"推荐模式 {policy['mode']} | 推荐上限 "
        f"{policy['max_recommendations']}只 | 组合仓位上限 "
        f"{policy['max_portfolio_pct']}%"
    )
```

用以下片段替换模板中的单一“维科夫买点候选”表：

```html
<p class="dt">{policy_note}</p>
<h2 style="font-size:18px;margin:18px 0 8px">今日可执行</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{actionable_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">等待触发</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{waiting_rows}</tbody></table>
<h2 style="font-size:18px;margin:18px 0 8px">观察池</h2>
<table><thead><tr><th>#</th><th>名称</th><th>板块</th><th>买点</th><th>置信度</th><th>综合分</th><th>覆盖率</th><th>信号</th></tr></thead><tbody>{observation_rows}</tbody></table>
```

主流程调用改为：

```python
    report = generate_report(candidates, sector_codes, elapsed, policy, buckets)
    html = _generate_html(
        candidates, sector_codes, elapsed, ts, policy, buckets
    )
```

- [ ] **Step 5: 运行政策测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 7 tests PASS。

- [ ] **Step 6: 提交 R0-C**

```bash
git add .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat(candidates): gate recommendations by regime"
```

---

### Task 6: 增加绝对热点门槛并按有效候选扩池

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetchers/sector_data.py:264-279`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:35-61,235-243`
- Test: `.claude/skills/stock-trend/tests/test_daily_candidates.py`

- [ ] **Step 1: 写绝对热点与扩池失败测试**

在 `test_daily_candidates.py` 增加导入：

```python
from unittest.mock import patch
from scans import daily_candidates as dc
```

在测试类中增加：

```python
    def test_pick_hot_sectors_uses_absolute_threshold(self):
        rankings = {"sectors": [
            {"code": "BK1", "name": "弱中最强", "change_pct": -1.0,
             "main_force_net": -1e8, "up_count": 2, "down_count": 8},
            {"code": "BK2", "name": "更弱", "change_pct": -3.0,
             "main_force_net": -2e8, "up_count": 2, "down_count": 8},
        ]}
        with patch("fetchers.sector_data.get_sector_rankings", return_value=rankings):
            picked = dc.pick_hot_sectors(top_n=20, min_hot=45, min_stocks=1)
        self.assertEqual(picked, [])

    def test_scan_expands_until_eligible_count_reaches_target(self):
        calls = []

        def fake_gather(batch, top_n_per_sector):
            calls.append(list(batch))
            return {"candidates": [{"code": batch[0]}]}

        results = {
            "BK1": [{"code": "1", "composite_score": 80,
                      "data_quality": {"eligible": False}}],
            "BK2": [{"code": "2", "composite_score": 80,
                      "data_quality": {"eligible": True}}],
        }

        def fake_phase2(candidates, enable_wyckoff, as_of_date):
            return results[candidates[0]["code"]]

        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            scored = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=1,
                min_score=50, as_of_date="2026-08-06",
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual({item["code"] for item in scored}, {"1", "2"})
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 热点阈值和新扫描参数测试 FAIL。

- [ ] **Step 3: 同时保存绝对分与相对分**

在 `rank_hot_sectors()` 中用以下代码替换打分与归一化部分：

```python
    for sector in sectors:
        absolute = compute_hot_score(sector)
        sector["absolute_hot_score"] = absolute
        sector["hot_score"] = absolute

    sectors.sort(key=lambda item: item.get("absolute_hot_score", 0), reverse=True)

    if sectors:
        scores = [item["absolute_hot_score"] for item in sectors]
        lo, hi = min(scores), max(scores)
        if hi > lo:
            for sector in sectors:
                sector["hot_score"] = round(
                    (sector["absolute_hot_score"] - lo) / (hi - lo) * 100,
                    1,
                )
    return sectors[:top_n]
```

- [ ] **Step 4: 让 `min_hot` 使用绝对热度**

用以下完整函数替换 `pick_hot_sectors()`：

```python
def pick_hot_sectors(top_n=20, min_hot=45, min_stocks=10):
    """Pick sectors that pass an absolute heat floor, then keep relative order."""
    from fetchers.sector_data import get_sector_rankings, rank_hot_sectors
    rankings = get_sector_rankings()
    ranked = rank_hot_sectors(rankings, top_n=top_n, min_stocks=min_stocks)
    qualified = [
        sector for sector in ranked
        if sector.get("absolute_hot_score", 0) >= min_hot
    ]
    return [
        (sector["code"], sector["name"], sector.get("hot_score", 0))
        for sector in qualified
    ]
```

- [ ] **Step 5: 按过滤后有效数量决定是否停止扩池**

用以下完整函数替换 `scan_sectors()`：

```python
def scan_sectors(sector_codes, batch_size=4, per_sector=25,
                 min_candidates=20, min_score=50, as_of_date=""):
    """Expand sectors until enough score-qualified, data-eligible names exist."""
    all_scored = {}
    for i in range(0, len(sector_codes), batch_size):
        batch = sector_codes[i:i + batch_size]
        try:
            phase1 = gather_candidates(batch, top_n_per_sector=per_sector)
        except Exception as exc:
            print(f"  ⚠️ 板块 {batch} 汇聚失败: {exc}", file=sys.stderr)
            continue
        if not phase1["candidates"]:
            continue
        scored = run_phase2(
            phase1["candidates"],
            enable_wyckoff=True,
            as_of_date=as_of_date,
        )
        for item in scored:
            all_scored[item["code"]] = item
        eligible_count = sum(
            1 for item in all_scored.values()
            if item["composite_score"] >= min_score
            and item.get("data_quality", {}).get("eligible", False)
        )
        print(
            f"  批次完成,候选 {len(all_scored)} 只,有效 {eligible_count} 只",
            file=sys.stderr,
        )
        if eligible_count >= min_candidates:
            break
    return list(all_scored.values())
```

主流程调用增加 `min_score=args.min_score`。

- [ ] **Step 6: 运行每日候选测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 9 tests PASS。

- [ ] **Step 7: 提交 R0-D**

```bash
git add .claude/skills/stock-trend/scripts/fetchers/sector_data.py .claude/skills/stock-trend/scripts/scans/daily_candidates.py .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "fix(candidates): enforce absolute heat floor"
```

---

### Task 7: 接入质量门并更新契约文档

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_stock_trend.py:1319-1332,1469-1475`
- Modify: `.claude/skills/stock-trend/SKILL.md:271-288`
- Modify: `docs/daily-recommendation-optimization.md`

- [ ] **Step 1: 将新增测试接入主测试入口**

在 `run_backtest_integration_tests()` 后增加：

```python
def run_daily_recommendation_tests():
    """Run recommendation quality, daily policy, and Wyckoff backtest tests."""
    global PASSED, FAILED
    from test_recommendation_quality import run_recommendation_quality_tests
    from test_daily_candidates import run_daily_candidates_tests
    from test_wyckoff_backtest import run_wyckoff_backtest_tests

    for runner in (
        run_recommendation_quality_tests,
        run_daily_candidates_tests,
        run_wyckoff_backtest_tests,
    ):
        passed, failed = runner()
        PASSED += passed
        FAILED += failed
```

在主流程的 backtest tests 后调用：

```python
        run_daily_recommendation_tests()
```

- [ ] **Step 2: 更新 `/candidates` 输出契约**

将 `SKILL.md` 的 `/candidates` 输出说明更新为：

```markdown
4. 输出按行动等级分层：`recommendations`（强势环境、最多5只）、
   `waiting_trigger`（中性环境、最多2只）、`observation`（弱市、过期或
   数据不足）；原 `candidates` 字段保留兼容。
5. K线必须覆盖报告日期且数据覆盖率≥70%才具备推荐资格；市场环境缺失、
   过期或低于60分时允许输出“今日无推荐”。
6. 候选为维科夫买点与多维排序结果，正式行动仍需满足报告中的触发和
   风险条件。
```

在 `docs/daily-recommendation-optimization.md` 的实施顺序后增加 P0 状态表，四个批次未执行时统一标记“计划完成，待实施”。

- [ ] **Step 3: 运行针对性测试**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py -v
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: 全部测试零失败。

- [ ] **Step 4: 运行仓库强制质量门**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 两条命令退出码均为 0；不得为了消除失败而重生成 golden 快照。

- [ ] **Step 5: 核对 JSON 契约的单元测试覆盖**

Run:

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py -v
python3 .claude/skills/stock-trend/tests/test_recommendation_quality.py -v
```

Expected:

- 强势策略最多提升 5 只且不会提升 `eligible=false` 的候选。
- 中性策略只产生最多 2 只 `waiting_trigger`。
- 弱势、过期和缺失环境的 `mode` 均为 `observation`。
- 数据质量测试覆盖 `as_of_date/coverage/eligible/reasons`。

- [ ] **Step 6: 提交 P0 文档和测试入口**

```bash
git add .claude/skills/stock-trend/tests/test_stock_trend.py .claude/skills/stock-trend/SKILL.md docs/daily-recommendation-optimization.md
git commit -m "docs(candidates): define recommendation gates"
```

---

## P0 完成定义

只有同时满足以下条件，P0 才能标记完成：

- [ ] 精确前向收益测试验证目标交易日收盘价。
- [ ] 全样本基准不依赖维科夫阶段识别成功。
- [ ] 候选输出包含数据日期、覆盖率、资格和原因。
- [ ] 原始 `composite_score` 在 P0 中不因质量模块改变。
- [ ] 市场环境缺失、过期或低于 60 分时正式推荐为空。
- [ ] 中性环境最多 2 只等待触发，强势环境最多 5 只可执行。
- [ ] 相对最强但绝对弱势的板块不能通过热点门槛。
- [ ] 扩池停止条件使用最低分和数据资格过滤后的数量。
- [ ] 现有 `candidates` JSON 字段保持兼容。
- [ ] 两条仓库质量门均通过，且 golden 快照未被无理由重生成。

## 后续计划拆分

P0 合并后再分别创建以下计划，避免与可信度修复耦合：

1. `daily-recommendation-trade-plan`：从技术支撑/压力生成入场区间、止损、三级目标、R:R、仓位和有效期。
2. `daily-recommendation-universe-diversification`：龙头/中军/低位转强三类宇宙、多板块归属和行业相关性约束。
3. `daily-recommendation-production-backtest`：历史时点宇宙重建、次日可成交价、成本、止损和 Top K 组合指标。
4. `daily-recommendation-performance-tracking`：不可变推荐快照及 5/10/20/60 日自动归因。

本计划及其产出的股票分析仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
