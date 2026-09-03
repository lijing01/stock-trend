# 今日推荐买点分级优先级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一级、二级、三级维科夫买点以小幅、可解释、可回测的方式影响 `/candidates` 同一推荐层级内的排序，同时保证市场环境、数据质量、板块持续性和最低质量分仍是不可绕过的硬门槛。

**Architecture:** 将买点等级识别收敛到 `analysis/wyckoff.py` 的单一事实源，移除 `stock_scanner.py` 对晚期子阶段的隐式统一加分，并在 `daily_candidates.py` 增加独立的执行优先分。质量分继续决定资格，执行优先分只决定已经通过资格检查的候选顺序；回测新增精确的 `by_buy_level` 和不利/有利波动统计，用于验证并约束后续调参。

**Tech Stack:** Python 3.10、标准库 `unittest`、现有 JSON/Markdown/HTML 报告链路、现有维科夫历史重放回测。

---

## Scope and behavior contract

- 买点优先级顺序固定为：二级 LPS > 三级 JAC/BU 后再确认 > 一级 Spring/Test。
- 初始加分固定为：一级 `+1.0`、二级 `+3.0`、三级 `+2.0`；未严格分级的信号 `+0.0`。
- 三级只接受 `short_term.sub_phase == "jac"`、`signal_status == "confirmed"` 且 `post_lps_reconfirmation is True`；首次 confirmed JAC 不得获得三级加分。
- 买点等级必须满足现有事件新鲜度上限；缺失 `signal_age_bars` 的旧 payload 按 `0` 兼容，超龄信号不分级、不加分。
- `quality_adjusted_score` 保持原义，不写入买点奖励；新增 `execution_priority_score` 保存排序结果。
- `score_eligible`、`_is_final_valid_candidate()` 和最低分门槛只读取 `quality_adjusted_score`，不允许 49 分候选靠二级买点 `+3` 越过 50 分门槛。
- 市场环境 `<60`、板块不可执行、数据质量不合格、资金背离门控、`retest_pending` 和 `failed_breakout` 的行为保持不变。
- 买点加分只影响同一分桶内排序，不改变 `actionable`、`waiting_trigger`、`observation` 的资格定义和数量上限。
- JSON 保留 `raw_composite_score`、`composite_score`、`quality_adjusted_score`；新增字段必须可选，旧快照和旧消费者继续工作。
- 不增加依赖，不重构维科夫识别引擎，不改变 Spring/LPS/JAC 的识别阈值。

## File structure

- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
  - 提供共享的严格买点等级识别与固定等级元数据。
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
  - 删除晚期子阶段统一 `+5` 的隐式奖励，只保留结构本身的维科夫分。
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
  - 分离资格分与排序分，物化买点等级、奖励和执行优先分，并在报告中展示。
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`
  - 在历史重放中保存严格买点等级，输出分级收益、MAE/MFE 和证据状态。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`
  - 锁定共享等级识别、三级严格条件和新鲜度。
- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
  - 锁定维科夫基础分不再包含晚期阶段奖励。
- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
  - 锁定资格不变、有限加分、排序顺序、分桶隔离和报告字段。
- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
  - 锁定精确等级聚合和路径风险统计。
- Modify: `.claude/skills/stock-trend/SKILL.md`
  - 记录质量分与执行优先分的不同职责及初始奖励。
- Modify: `.claude/specs/stock-trend-skill.md`
  - 固化推荐排序合同、回测验收和回滚条件。

### Task 1: 建立共享的严格买点等级分类

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff.py`
- Modify: `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`

- [ ] **Step 1: 写入共享分类的失败测试**

在 `test_wyckoff.py` 增加以下测试；测试必须覆盖三个等级、首次 JAC、未确认状态和超龄状态：

```python
def test_classify_buy_point_level_is_strict_and_fresh(self):
    classify = wyckoff_module.classify_buy_point_level

    def payload(sub_phase, *, status="confirmed", age=0, reconfirmed=False):
        return {
            "short_term": {
                "sub_phase": sub_phase,
                "signal_status": status,
                "signal_age_bars": age,
                "post_lps_reconfirmation": reconfirmed,
            }
        }

    self.assertEqual(classify(payload("spring"))["number"], 1)
    self.assertEqual(classify(payload("lps"))["number"], 2)
    self.assertEqual(
        classify(payload("jac", reconfirmed=True))["number"], 3)
    self.assertIsNone(classify(payload("jac", reconfirmed=False)))
    self.assertIsNone(classify(payload("lps", status="candidate")))
    self.assertIsNone(classify(payload("spring", age=9)))
    self.assertIsNone(classify(payload("lps", age=11)))
    self.assertIsNone(
        classify(payload("jac", age=9, reconfirmed=True)))
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: FAIL，提示 `analysis.wyckoff` 尚无 `classify_buy_point_level`。

- [ ] **Step 3: 实现共享分类函数和固定元数据**

在 `BUY_SUB_PHASES` 后增加：

```python
BUY_POINT_LEVELS = {
    SUB_SPRING: {
        "number": 1,
        "name": "一级",
        "label": "Spring/Test",
        "priority_bonus": 1.0,
    },
    SUB_LPS: {
        "number": 2,
        "name": "二级",
        "label": "SOS 后 LPS",
        "priority_bonus": 3.0,
    },
    SUB_JAC: {
        "number": 3,
        "name": "三级",
        "label": "JAC/BU 后再确认",
        "priority_bonus": 2.0,
    },
}


def classify_buy_point_level(wyckoff: dict | None) -> dict | None:
    """Return a strict, fresh execution level for a short-term payload."""
    if not isinstance(wyckoff, dict):
        return None
    short = wyckoff.get("short_term") or {}
    sub_phase = str(short.get("sub_phase") or "").strip().lower()
    status = str(short.get("signal_status") or "").strip().lower()
    try:
        age = int(short.get("signal_age_bars", 0) or 0)
    except (TypeError, ValueError):
        return None
    level = BUY_POINT_LEVELS.get(sub_phase)
    if level is None or status != "confirmed":
        return None
    if age < 0 or age > EVENT_MAX_AGE.get(sub_phase, 0):
        return None
    if sub_phase == SUB_JAC and short.get("post_lps_reconfirmation") is not True:
        return None
    return dict(level)
```

该函数不读取中文展示字段，不把 `secondary_test`、`st`、`pre_markup`、`bu` 或首次 JAC 映射为一二三级。

- [ ] **Step 4: 运行测试确认 GREEN**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff.py
```

Expected: PASS，现有维科夫识别测试和新增等级测试全部通过。

- [ ] **Step 5: 提交共享分类**

```bash
git add \
  .claude/skills/stock-trend/scripts/analysis/wyckoff.py \
  .claude/skills/stock-trend/tests/test_wyckoff.py
git commit -m "feat: centralize buy point levels"
```

### Task 2: 移除隐式晚期阶段奖励，避免重复计分

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`

- [ ] **Step 1: 将现有加分测试改成基础结构分合同**

把 `TestScoreWyckoff.test_base_normalized` 改为：

```python
def test_base_normalized_does_not_embed_buy_level_bonus(self):
    self.assertAlmostEqual(
        sc.score_wyckoff(_wk(sub="lps", score=2.0, conf=0.6)),
        83.3333,
        places=3,
    )
    self.assertAlmostEqual(
        sc.score_wyckoff(_wk(sub="jac", score=2.0, conf=0.9)),
        83.3333,
        places=3,
    )
    self.assertAlmostEqual(
        sc.score_wyckoff(
            _wk(phase="distribution", sub="lpsy", score=-2.0, conf=0.7)),
        16.6667,
        places=3,
    )
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: FAIL，LPS/JAC 仍返回约 `88.3333`。

- [ ] **Step 3: 删除 `score_wyckoff()` 的晚期阶段 `+5`**

将函数收敛为：

```python
def score_wyckoff(analysis):
    """Return structural Wyckoff strength without execution-level bonuses."""
    if not analysis:
        return 50.0
    score = _safe_float(analysis.get("wyckoff_score"), 0.0)
    return normalize_wyckoff_score(score)
```

同步移除不再使用的 `SUB_LPS`、`SUB_PRE_MARKUP`、`SUB_JAC` 导入；不得改变维科夫硬漏斗、维度权重或质量调分公式。

- [ ] **Step 4: 运行 scanner 测试确认 GREEN**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_stock_scanner.py
```

Expected: PASS；`quality_adjusted_score` 仍等于基础复合分乘覆盖率和新鲜度因子。

- [ ] **Step 5: 提交去重计分**

```bash
git add \
  .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  .claude/skills/stock-trend/tests/test_stock_scanner.py
git commit -m "refactor: separate buy level ranking bonus"
```

### Task 3: 分离资格分与执行优先分

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

- [ ] **Step 1: 扩展候选 fixture 并写排序失败测试**

给测试 fixture 增加规范短线字段：

```python
"short_term": {
    "sub_phase": "lps",
    "signal_status": "confirmed",
    "signal_age_bars": 0,
    "post_lps_reconfirmation": False,
},
```

新增以下核心测试：

```python
def _set_buy_level(item, sub_phase, reconfirmed=False, status="confirmed"):
    item["wyckoff"]["short_term"] = {
        "sub_phase": sub_phase,
        "signal_status": status,
        "signal_age_bars": 0,
        "post_lps_reconfirmation": reconfirmed,
    }
    return item


def test_buy_level_bonus_orders_only_nearby_eligible_candidates(self):
    plain = candidate("plain", adjusted_score=80.0)
    plain["wyckoff"]["short_term"] = {
        "sub_phase": "pre_markup", "signal_status": "confirmed",
        "signal_age_bars": 0,
    }
    level_two = _set_buy_level(candidate("l2", adjusted_score=78.0), "lps")
    level_three = _set_buy_level(
        candidate("l3", adjusted_score=78.0), "jac", reconfirmed=True)
    level_one = _set_buy_level(candidate("l1", adjusted_score=78.0), "spring")

    picked = dc.select_candidate_pool(
        [plain, level_one, level_three, level_two], top=4, min_score=50)

    self.assertEqual(
        [row["code"] for row in picked], ["l2", "plain", "l3", "l1"])
    self.assertEqual(level_two["execution_priority_score"], 81.0)
    self.assertEqual(level_three["execution_priority_score"], 80.0)
    self.assertEqual(level_one["execution_priority_score"], 79.0)


def test_buy_level_bonus_cannot_cross_quality_eligibility_gate(self):
    item = _set_buy_level(candidate("low", adjusted_score=49.0), "lps")
    picked = dc.select_candidate_pool([item], top=1, min_score=50)

    self.assertFalse(picked[0]["score_eligible"])
    self.assertEqual(picked[0]["quality_adjusted_score"], 49.0)
    self.assertEqual(picked[0]["execution_priority_score"], 52.0)


def test_unreconfirmed_jac_has_no_priority_bonus(self):
    item = _set_buy_level(
        candidate("jac", adjusted_score=78.0), "jac", reconfirmed=False)
    picked = dc.select_candidate_pool([item], top=1, min_score=50)

    self.assertIsNone(picked[0]["buy_point_level"])
    self.assertEqual(picked[0]["buy_point_priority_bonus"], 0.0)
    self.assertEqual(picked[0]["execution_priority_score"], 78.0)
```

- [ ] **Step 2: 写硬门控不变的失败测试**

增加以下断言，证明买点奖励不能跨分桶升级：

```python
def test_buy_level_priority_does_not_override_market_or_sector_gates(self):
    level_two = _set_buy_level(candidate("l2", adjusted_score=90.0), "lps")
    weak_policy = {
        "mode": "observation", "max_recommendations": 0,
        "reasons": ["regime_weak"],
    }
    self.assertEqual(classify_candidates([level_two], weak_policy)["actionable"], [])

    unverified = _set_buy_level(
        candidate("sector", adjusted_score=90.0, sector_actionable=False),
        "lps",
    )
    strong_policy = {"mode": "actionable", "max_recommendations": 5,
                     "reasons": []}
    self.assertEqual(classify_candidates([unverified], strong_policy)["actionable"], [])
```

- [ ] **Step 3: 运行测试确认 RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL，候选尚无执行优先字段，排序仍只使用质量分。

- [ ] **Step 4: 实现资格分、优先分与字段物化**

导入共享分类函数，并替换当前单一分值函数：

```python
from analysis.wyckoff import classify_buy_point_level


def candidate_quality_score(item):
    """Return the score used by hard eligibility gates."""
    return float(
        item.get("quality_adjusted_score", item.get("composite_score", 0)) or 0)


def apply_buy_point_priority(item):
    """Materialize an auditable within-bucket execution priority score."""
    level = classify_buy_point_level(item.get("wyckoff"))
    bonus = float(level["priority_bonus"]) if level else 0.0
    quality = candidate_quality_score(item)
    item["buy_point_level"] = level["number"] if level else None
    item["buy_point_level_name"] = level["name"] if level else ""
    item["buy_point_priority_bonus"] = bonus
    item["execution_priority_score"] = round(min(100.0, quality + bonus), 1)
    return item


def candidate_rank_score(item):
    """Return within-bucket execution priority with legacy fallback."""
    return float(item.get("execution_priority_score", candidate_quality_score(item)) or 0)
```

将 `_is_final_valid_candidate()`、`score_eligible` 和最低分判断改为 `candidate_quality_score(item) >= min_score`。在 `select_candidate_pool()` 对每个进入候选池的条目先调用 `apply_buy_point_priority(item)`，排序仍使用 `candidate_rank_score(item)`。

- [ ] **Step 5: 让现有展示分类复用共享事实源**

将 `_wyckoff_buy_level()` 改成薄包装，保留既有 CSS 字段和报告文案：

```python
def _wyckoff_buy_level(wyckoff):
    level = classify_buy_point_level(wyckoff)
    if level is None:
        return None
    return {
        **level,
        "css_class": f"wyckoff-buy-level-{level['number']}",
    }
```

不得恢复顶层中文字段回退；生产排序与展示必须读取同一个严格分类结果。

- [ ] **Step 6: 运行 daily candidates 测试确认 GREEN**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: PASS；二级、三级、一级在相同质量分下按 `+3/+2/+1` 排序，普通 JAC 无奖励，硬门控结果不变。

- [ ] **Step 7: 提交排序分离**

```bash
git add \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: rank candidates by strict buy levels"
```

### Task 4: 在 JSON、Markdown 和 HTML 中公开排序审计字段

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- Modify: `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

- [ ] **Step 1: 写报告和 JSON 合同测试**

新增：

```python
def test_outputs_keep_quality_score_and_expose_execution_priority(self):
    item = _set_buy_level(candidate("l2", adjusted_score=78.0), "lps")
    dc.apply_buy_point_priority(item)
    policy = {"mode": "actionable", "max_recommendations": 5,
              "reasons": []}
    buckets = classify_candidates([item], policy)

    payload = build_json_output([item], ["BK1"], 0.1, policy, buckets)
    row = payload["candidates"][0]
    self.assertEqual(row["quality_adjusted_score"], 78.0)
    self.assertEqual(row["buy_point_level"], 2)
    self.assertEqual(row["buy_point_priority_bonus"], 3.0)
    self.assertEqual(row["execution_priority_score"], 81.0)

    markdown = generate_report([item], ["BK1"], 0.1, policy, buckets)
    html = dc._generate_html([item], ["BK1"], 0.1, "20260903-170000",
                             policy, buckets)
    self.assertIn("优先分", markdown)
    self.assertIn("81.0", markdown)
    self.assertIn("优先分", html)
    self.assertIn("质量分 + 严格买点奖励", html)
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: FAIL，报告尚无“优先分”和公式说明。

- [ ] **Step 3: 增加可审计展示**

在 Markdown/HTML 候选表的“质量分”后增加“优先分”，值使用 `candidate_rank_score(item)`。在报告说明区增加：

```text
优先分 = 质量分 + 严格买点奖励；一级 +1、二级 +3、三级 +2。
优先分仅用于同一推荐层级内排序，不改变市场、数据、板块和最低质量分门槛。
```

不要删除原始分、质量分或数据覆盖率；不要把观察池文案改成“买入”或“推荐”。

- [ ] **Step 4: 运行报告测试确认 GREEN**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
```

Expected: PASS；JSON 保持向后兼容，Markdown/HTML 能同时审计质量分和优先分。

- [ ] **Step 5: 提交报告审计字段**

```bash
git add \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
git commit -m "feat: expose candidate priority score"
```

### Task 5: 为买点等级增加精确回测与路径风险统计

**Files:**

- Modify: `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- Modify: `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`

- [ ] **Step 1: 写严格三级与路径统计失败测试**

在 `test_wyckoff_backtest.py` 增加：

```python
def test_classify_signal_keeps_strict_buy_level():
    analysis = {
        "meta": {},
        "phase": {"primary": "markup", "primary_sub_phase": "jac",
                  "confidence": 0.9},
        "signal": {"status": "confirmed", "age_bars": 0},
        "short_term": {
            "sub_phase": "jac", "signal_status": "confirmed",
            "signal_age_bars": 0, "post_lps_reconfirmation": True,
        },
        "wyckoff_score": 2.0,
    }
    signal = wb._classify_signal(analysis, 0.3)
    self.assertEqual(signal["buy_point_level"], 3)

    analysis["short_term"]["post_lps_reconfirmation"] = False
    signal = wb._classify_signal(analysis, 0.3)
    self.assertIsNone(signal["buy_point_level"])


def test_forward_excursion_measures_mae_and_mfe_from_entry_close():
    rows = [
        {"date": "20260101", "close": 10.0, "high": 10.0, "low": 10.0},
        {"date": "20260102", "close": 10.5, "high": 11.0, "low": 9.0},
        {"date": "20260103", "close": 10.8, "high": 12.0, "low": 9.5},
    ]
    result = wb._forward_excursion(rows, 0, "20260103")
    self.assertEqual(result, {"mae": -0.1, "mfe": 0.2})
```

- [ ] **Step 2: 写聚合合同失败测试**

构造一级、二级、严格三级和未分级 JAC 信号，调用 `_build_result()`，断言：

```python
self.assertEqual(result["by_buy_level"]["5"]["level_1"]["count"], 1)
self.assertEqual(result["by_buy_level"]["5"]["level_2"]["count"], 1)
self.assertEqual(result["by_buy_level"]["5"]["level_3"]["count"], 1)
self.assertEqual(result["by_buy_level"]["5"]["ungraded"]["count"], 1)
self.assertEqual(result["risk_by_buy_level"]["5"]["level_2"]["avg_mae"], -0.04)
self.assertEqual(result["evidence"]["status"], "evidence_insufficient")
```

- [ ] **Step 3: 运行测试确认 RED**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: FAIL，结果尚无严格等级和路径风险字段。

- [ ] **Step 4: 保存等级和 MAE/MFE**

导入 `classify_buy_point_level`，让 `_classify_signal()` 增加：

```python
level = classify_buy_point_level(analysis)
return {
    "phase": phase,
    "sub_phase": sub,
    "confidence": round(float(conf), 3),
    "score_100": round(
        normalize_score_100(float(analysis.get("wyckoff_score", 0))), 1),
    "buy_point_level": level["number"] if level else None,
}
```

增加路径函数：

```python
def _forward_excursion(kline, now_idx, target_date):
    entry = _safe_close(kline[now_idx])
    if not entry or entry <= 0:
        return None
    path = []
    target = str(target_date).replace("-", "")
    for row in kline[now_idx + 1:]:
        path.append(row)
        if _kline_date(row) >= target:
            break
    if not path or _kline_date(path[-1]) < target:
        return None
    lows = [float(row.get("low", row.get("close"))) for row in path]
    highs = [float(row.get("high", row.get("close"))) for row in path]
    return {
        "mae": round(min(lows) / entry - 1, 6),
        "mfe": round(max(highs) / entry - 1, 6),
    }
```

历史重放为每个窗口同时保存 `returns` 与 `excursions`；扁平化后的单条 signal 必须保留 `buy_point_level` 和 `excursions`。

- [ ] **Step 5: 聚合分级收益、风险和证据状态**

在 `_build_result()` 增加：

```python
def _level_key(signal):
    level = signal.get("buy_point_level")
    return f"level_{level}" if level in (1, 2, 3) else "ungraded"


by_buy_level = _bucket_stats(signal_pairs, _level_key)
```

同时按窗口和等级输出：

```python
risk_by_buy_level[window][level] = {
    "count": len(paths),
    "avg_mae": round(sum(path["mae"] for path in paths) / len(paths), 6),
    "avg_mfe": round(sum(path["mfe"] for path in paths) / len(paths), 6),
}
```

证据状态固定使用以下门槛：

```python
level_counts = {
    level: sum(1 for signal in signals if _level_key(signal) == level)
    for level in ("level_1", "level_2", "level_3")
}
evidence = {
    "minimum_signals_per_level": 100,
    "counts": level_counts,
    "status": (
        "ready" if all(count >= 100 for count in level_counts.values())
        else "evidence_insufficient"
    ),
}
```

返回对象新增 `by_buy_level`、`risk_by_buy_level` 和 `evidence`，保留现有 `by_sub_phase` 兼容字段。

- [ ] **Step 6: 运行 backtest 测试确认 GREEN**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: PASS；首次 JAC 归入 `ungraded`，只有再确认 JAC 归入 `level_3`。

- [ ] **Step 7: 提交回测证据链**

```bash
git add \
  .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py \
  .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
git commit -m "feat: backtest strict buy point levels"
```

### Task 6: 更新策略合同与完成全量验证

**Files:**

- Modify: `.claude/skills/stock-trend/SKILL.md`
- Modify: `.claude/specs/stock-trend-skill.md`

- [ ] **Step 1: 更新 `/candidates` 排序合同**

在两份文档中加入以下规则，术语必须保持一致：

```text
候选资格继续使用 quality_adjusted_score；同一推荐层级内使用
execution_priority_score = quality_adjusted_score + buy_point_priority_bonus 排序。
严格一级/二级/三级奖励分别为 +1/+3/+2；普通 JAC、未确认或过期信号不奖励。
买点优先分不能越过市场环境、数据质量、板块持续性、资金背离或最低质量分门槛。
```

文档同时说明 `evidence.status != ready` 时奖励属于保守先验，只允许通过后续回测缩小、归零或调整，不允许自动放大。

- [ ] **Step 2: 运行四个 focused suites**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_stock_scanner.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
```

Expected: 四个测试文件全部 PASS。

- [ ] **Step 3: 运行仓库规定的两个 Python 质量门禁**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_golden.py --diff
```

Expected: 全部 PASS；golden diff 不得通过重生成快照规避。若报告新增“优先分”造成预期差异，先逐项确认字段和文案，再按仓库流程更新对应快照并在提交信息中解释。

- [ ] **Step 4: 用固定候选 JSON 做离线 smoke test**

Run:

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py \
  --from-candidates /private/tmp/official-candidates-20260903.json \
  --lookback-days 120 \
  --eval-windows 5,10,20 \
  --output /private/tmp/wyckoff-level-priority-validation.json
jq '{meta, evidence, by_buy_level, risk_by_buy_level}' \
  /private/tmp/wyckoff-level-priority-validation.json
```

Expected: 30 只标的均可回放；输出严格分离 `level_1/level_2/level_3/ungraded`。在任一级少于 100 个信号时，`evidence.status` 必须为 `evidence_insufficient`。

- [ ] **Step 5: 验证弱市和强市分桶不被奖励改写**

用测试 fixture 分别构造市场分 `31.8` 和 `80`：

```text
31.8 弱市：actionable=0，所有买点奖励只改变观察池内部顺序。
80 强市：只有原本通过质量、板块和短线状态门控的候选进入 actionable；同层按执行优先分排序。
```

Expected: 买点奖励不会让 observation 跨层进入 waiting/actionable。

- [ ] **Step 6: 做静态差异审查**

Run:

```bash
git diff --check
git diff -- \
  .claude/skills/stock-trend/scripts/analysis/wyckoff.py \
  .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py \
  .claude/skills/stock-trend/tests/test_wyckoff.py \
  .claude/skills/stock-trend/tests/test_stock_scanner.py \
  .claude/skills/stock-trend/tests/test_daily_candidates.py \
  .claude/skills/stock-trend/tests/test_wyckoff_backtest.py \
  .claude/skills/stock-trend/SKILL.md \
  .claude/specs/stock-trend-skill.md
```

Expected: 没有改动市场三档、推荐上限、数据覆盖率、板块持续性或快照幂等逻辑；没有新依赖；`git diff --check` exit code 0。

- [ ] **Step 7: 提交文档和最终验证结果**

```bash
git add \
  .claude/skills/stock-trend/SKILL.md \
  .claude/specs/stock-trend-skill.md
git commit -m "docs: define buy point priority contract"
```

## Acceptance criteria

1. 相同质量分下，严格二级、三级、一级依次获得 `+3/+2/+1`，普通 JAC 为 `+0`。
2. `quality_adjusted_score` 数值和语义不含买点奖励；`execution_priority_score` 可从 JSON 和报告审计。
3. 质量分低于最低门槛的候选不能因买点奖励获得资格。
4. 弱市、数据不合格、板块未验证或短线状态异常的候选不能因买点奖励跨分桶。
5. 现有 `stock_scanner` 晚期子阶段 `+5` 被删除，买点不会同时在维科夫维度和最终排序重复奖励。
6. 回测精确区分一级、二级、严格三级和未分级信号，并输出 5/10/20 日收益、MAE、MFE、样本数和证据状态。
7. 原有 JSON 字段与快照消费者保持兼容；新增字段缺失时按质量分排序。
8. 两个仓库质量门禁通过，且没有为了消除失败而无审查地重生成 golden。

## Calibration and rollback

- 初始 `+1/+3/+2` 是保守的小幅排序先验，不是“三级一定优于二级”的收益结论。
- 每个等级累计至少 100 个历史信号后，比较 5/10/20 日胜率、平均收益、MAE/MFE；正式推荐历史累计至少 20 个交易日后，再比较净收益和不可成交率。
- 若任一等级在两个主要窗口同时满足“平均收益低于未分级基线且 MAE 更差”，将该等级奖励降为 `0`，不改变买点展示和硬门控。
- 若整体 Top-K 相对旧排序的 10 日平均收益下降、最大不利波动扩大，回滚范围仅包括 `execution_priority_score` 排序和报告优先分列；保留共享严格分类和回测统计，便于继续积累证据。
- 不通过放宽市场环境、数据质量、板块持续性或最低分门槛来补偿买点奖励效果。
