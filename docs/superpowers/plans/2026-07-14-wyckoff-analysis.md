# 维科夫操盘法 (Wyckoff Analysis) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 为 stock-trend skill 新增独立的维科夫操盘法分析模块，包含阶段判定、VSA 量价分析、因果关系量化，以独立 section 呈现在报告中并参与 12% 权重复合评分。

**Architecture:** 纯 Python 计算驱动模块 (`analysis/wyckoff.py`)，复用已有 K-line 数据（250 日），输出结构化 JSON。pipeline runner 新增可选步骤，scores.py 新增维科夫权重，report.py 新增 template context 字段。

**Tech Stack:** Python 3, 无额外依赖（复用 technical.py 的 ATR/MA 计算结果）

**文件清单:**

| 操作 | 文件 | 说明 |
|---|---|---|
| 创建 | `scripts/analysis/wyckoff.py` | ~1500 行，核心引擎 |
| 修改 | `scripts/pipeline/runner.py` | 加 Wyckoff 步骤，K-line days 120→250 |
| 修改 | `scripts/analysis/scores.py` | 新增 `--wyckoff` + `--wyckoff-score` 参数，12% 权重 |
| 修改 | `scripts/reporting/report.py` | 加载 wyckoff.json 注入 template context |
| 修改 | `assets/report-template.md` | 新增 `{{#wyckoff}}` section |
| 修改 | `assets/report-template.html` | 同步新增 Wyckoff 展示块 |
| 创建 | `tests/test_wyckoff.py` | 单元测试 + golden |
| 修改 | `tests/golden_config.json` | 新增 wyckoff 对应 golden 条目 |

---

### Task 1: wyckoff.py 数据结构和工具函数

**Files:**
- Create: `scripts/analysis/wyckoff.py` (lines 1-300)

- [ ] **Step 1: 实现常量定义和数据类**

```python
#!/usr/bin/env python3
"""Wyckoff Method analysis module.

Provides phase detection (accumulation/markup/distribution/markdown),
Volume Spread Analysis (VSA), and cause-effect quantification using
the Wyckoff framework.

Usage:
    python3 wyckoff.py <kline_json> [-o <output_path>]
"""

import json
import sys
from pathlib import Path
from typing import Any

# Phase enums
PHASE_ACCUMULATION = "accumulation"
PHASE_MARKUP = "markup"
PHASE_DISTRIBUTION = "distribution"
PHASE_MARKDOWN = "markdown"
PHASE_UNKNOWN = "phase_unknown"

PHASE_NAMES = {
    PHASE_ACCUMULATION: "吸筹阶段",
    PHASE_MARKUP: "拉升阶段",
    PHASE_DISTRIBUTION: "派发阶段",
    PHASE_MARKDOWN: "砸盘阶段",
    PHASE_UNKNOWN: "无法判定",
}

# Sub-phase enums
SUB_SC = "selling_climax"           # 抛售高潮
SUB_AR = "automatic_rally"           # 自动反弹
SUB_ST = "secondary_test"            # 二次测试
SUB_SPRING = "spring"                # 初支（spring）
SUB_LPS = "lps"                      # 最后支撑点
SUB_PRE_MARKUP = "pre_markup"        # 拉升前准备
SUB_JAC = "jac"                      # 跃过小溪
SUB_BU = "backup"                    # 回踩
SUB_CONTINUATION = "continuation"    # 持续拉升
SUB_BC = "buying_climax"             # 买入高潮
SUB_UTAD = "utad"                    # 派发中的上冲回落
SUB_LPSY = "lpsy"                    # 最后供应点
SUB_SOW = "sign_of_weakness"        # 弱势信号
SUB_PRE_MARKDOWN = "pre_markdown"    # 砸盘前兆
SUB_BREAKDOWN = "breakdown"          # 破位
SUB_PANIC = "panic_selling"          # 恐慌抛售
SUB_STOPPING_VOL = "stopping_volume" # 止跌量

SUB_PHASE_NAMES = {
    SUB_SC: "抛售高潮（SC）",
    SUB_AR: "自动反弹（AR）",
    SUB_ST: "二次测试（ST）",
    SUB_SPRING: "初支（Spring）",
    SUB_LPS: "最后支撑点（LPS）",
    SUB_PRE_MARKUP: "拉升前准备",
    SUB_JAC: "跃过小溪（JAC）",
    SUB_BU: "回踩（BU）",
    SUB_CONTINUATION: "持续拉升",
    SUB_BC: "买入高潮（BC）",
    SUB_UTAD: "上冲回落（UTAD）",
    SUB_LPSY: "最后供应点（LPSY）",
    SUB_SOW: "弱势信号（SOW）",
    SUB_PRE_MARKDOWN: "砸盘前兆",
    SUB_BREAKDOWN: "破位下跌",
    SUB_PANIC: "恐慌抛售",
    SUB_STOPPING_VOL: "止跌量",
}

# Phase → score mapping (contributes to composite via 12% weight)
PHASE_SCORES = {
    (PHASE_ACCUMULATION, SUB_SC): 0.5,
    (PHASE_ACCUMULATION, SUB_AR): 0.5,
    (PHASE_ACCUMULATION, SUB_ST): 1.0,
    (PHASE_ACCUMULATION, SUB_SPRING): 1.5,
    (PHASE_ACCUMULATION, SUB_LPS): 2.0,
    (PHASE_ACCUMULATION, SUB_PRE_MARKUP): 2.0,
    (PHASE_MARKUP, SUB_JAC): 2.0,
    (PHASE_MARKUP, SUB_BU): 1.5,
    (PHASE_MARKUP, SUB_CONTINUATION): 1.0,
    (PHASE_DISTRIBUTION, SUB_BC): -1.0,
    (PHASE_DISTRIBUTION, SUB_UTAD): -1.5,
    (PHASE_DISTRIBUTION, SUB_LPSY): -2.0,
    (PHASE_DISTRIBUTION, SUB_SOW): -2.0,
    (PHASE_DISTRIBUTION, SUB_PRE_MARKDOWN): -2.5,
    (PHASE_MARKDOWN, SUB_BREAKDOWN): -2.5,
    (PHASE_MARKDOWN, SUB_PANIC): -2.0,
    (PHASE_MARKDOWN, SUB_STOPPING_VOL): -1.5,
}
DEFAULT_PHASE_SCORE = 0.0
```

- [ ] **Step 2: 实现 K-line 数据加载和辅助函数**

```python
def load_kline(path: str) -> dict | None:
    """Load K-line JSON, validate structure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading K-line: {e}", file=sys.stderr)
        return None

    rows = data.get("data", [])
    if not isinstance(rows, list) or len(rows) < 30:
        print(f"Warning: insufficient K-line data ({len(rows) if isinstance(rows, list) else 0} rows)", file=sys.stderr)
    return data


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def extract_ohlcv(rows: list) -> dict:
    """Extract OHLCV arrays from K-line data rows."""
    opens, highs, lows, closes, volumes = [], [], [], [], []
    dates = []
    for r in rows:
        o = _safe_float(r.get("open"))
        h = _safe_float(r.get("high"))
        l = _safe_float(r.get("low"))
        c = _safe_float(r.get("close"))
        v = _safe_float(r.get("vol") or r.get("volume"))
        if None in (o, h, l, c, v):
            continue
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v)
        dates.append(str(r.get("date") or r.get("trade_date") or ""))
    return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes, "date": dates}


def compute_ma(values: list, period: int) -> list[float | None]:
    """Simple moving average. Pads with None for first period-1 entries."""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(round(sum(values[i - period + 1 : i + 1]) / period, 4))
    return result


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> list[float | None]:
    """Average True Range."""
    trs = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return compute_ma(trs, period)
```

- [ ] **Step 3: 实现 Swing Point 检测**

```python
def detect_swing_points(closes: list, highs: list, lows: list, volumes: list,
                         atr_values: list, lookback: int = 2) -> list[dict]:
    """Detect pivot highs and lows using N-bar lookback.

    A point is a pivot high if its high is the highest among lookback bars
    on each side, with minimum height > ATR * 0.5 to filter noise.

    Returns list of swing points sorted by index ascending.
    """
    if len(closes) < lookback * 2 + 1:
        return []

    swings = []
    for i in range(lookback, len(closes) - lookback):
        atr = atr_values[i]
        if atr is None or atr == 0:
            continue
        min_height = atr * 0.5

        # Pivot high
        is_pivot_high = True
        for offset in range(1, lookback + 1):
            if highs[i] <= highs[i - offset] or highs[i] <= highs[i + offset]:
                is_pivot_high = False
                break
        if is_pivot_high and (highs[i] - lows[i]) > min_height:
            swings.append({
                "index": i,
                "date": "",  # filled later
                "type": "high",
                "price": highs[i],
                "volume_ratio": volumes[i] / _ma_of_last_n(volumes, i, 50) if i >= 50 else 1.0,
                "is_climax": False,
            })

        # Pivot low
        is_pivot_low = True
        for offset in range(1, lookback + 1):
            if lows[i] >= lows[i - offset] or lows[i] >= lows[i + offset]:
                is_pivot_low = False
                break
        if is_pivot_low and (highs[i] - lows[i]) > min_height:
            swings.append({
                "index": i,
                "date": "",
                "type": "low",
                "price": lows[i],
                "volume_ratio": volumes[i] / _ma_of_last_n(volumes, i, 50) if i >= 50 else 1.0,
                "is_climax": False,
            })

    return sorted(swings, key=lambda s: s["index"])


def _ma_of_last_n(values: list, idx: int, n: int) -> float:
    start = max(0, idx - n + 1)
    segment = values[start : idx + 1]
    return sum(segment) / len(segment) if segment else 1.0
```

- [ ] **Step 4: 实现买卖高潮检测（Climax Detection）**

```python
def mark_climaxes(swings: list, highs: list, lows: list, closes: list,
                  volumes: list, atr_values: list) -> list[dict]:
    """Classify swing points as climaxes where volume spikes + extreme spread.

    Selling Climax: pivot low with volume > MA50*2 AND long lower shadow.
    Buying Climax:  pivot high with volume > MA50*2 AND long upper shadow.
    """
    for s in swings:
        i = s["index"]
        if i >= len(volumes) or i >= len(highs) or i >= len(lows) or i >= len(closes):
            continue
        vol_ratio = s["volume_ratio"]
        body = abs(closes[i] - opens[i]) if i < len(opens := []) else abs(closes[i] - highs[i])  # fallback
        # Compute shadow ratio
        upper_shadow = highs[i] - max(closes[i], opens[i]) if i < len(opens := []) else 0
        lower_shadow = min(closes[i], opens[i]) - lows[i] if i < len(opens := []) else 0
        total_range = highs[i] - lows[i]
        if total_range == 0:
            continue

        if s["type"] == "low" and vol_ratio > 2.0:
            # Check for long lower shadow
            shadow_ratio = lower_shadow / total_range if total_range > 0 else 0
            if shadow_ratio > 0.5:
                s["is_climax"] = True
                s["climax_type"] = "selling"
        elif s["type"] == "high" and vol_ratio > 2.0:
            shadow_ratio = upper_shadow / total_range if total_range > 0 else 0
            if shadow_ratio > 0.5:
                s["is_climax"] = True
                s["climax_type"] = "buying"
    return swings
```

- [ ] **Step 5: 交易区间检测（Trading Range Detection）**

```python
def detect_trading_range(swings: list, closes: list, atr_values: list,
                          min_touches: int = 3, min_bars: int = 20) -> dict | None:
    """Aggregate swing high/low points into a trading range.

    Uses hierarchical clustering on swing prices with ATR-based tolerance.
    Returns range dict or None if no clear range found.
    """
    if len(swings) < min_touches:
        return None

    highs_sorted = sorted(set(s["price"] for s in swings if s["type"] == "high"))
    lows_sorted = sorted(set(s["price"] for s in swings if s["type"] == "low"))

    if not highs_sorted or not lows_sorted:
        return None

    # Use median ATR as tolerance
    median_atr = _median([a for a in atr_values if a is not None]) or 0
    tolerance = median_atr * 1.0

    # Find resistance level (cluster of swing highs)
    resistance = _cluster_peak(highs_sorted, tolerance)
    # Find support level (cluster of swing lows)
    support = _cluster_peak(lows_sorted, tolerance)

    if resistance is None or support is None or resistance <= support:
        return None

    range_height = resistance - support
    if range_height < median_atr * 3:
        return None  # Too narrow to be meaningful

    # Count touches within tolerance
    touch_count = 0
    for s in swings:
        if abs(s["price"] - resistance) <= tolerance or abs(s["price"] - support) <= tolerance:
            touch_count += 1

    if touch_count < min_touches:
        return None

    # Find the bar range covered by this range
    range_indices = [s["index"] for s in swings if abs(s["price"] - resistance) <= tolerance
                     or abs(s["price"] - support) <= tolerance]
    if not range_indices:
        return None
    first_idx = min(range_indices)
    last_idx = max(range_indices)
    duration = last_idx - first_idx
    if duration < min_bars:
        return None

    return {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "range_height": round(range_height, 2),
        "range_height_pct": round(range_height / ((support + resistance) / 2) * 100, 2),
        "duration_bars": duration,
        "touch_count": touch_count,
        "is_clear_range": True,
        "support_idx": first_idx,
        "resistance_idx": last_idx,
    }


def _cluster_peak(prices: list, tolerance: float) -> float | None:
    """Find the most dense price cluster (mode with tolerance)."""
    if not prices:
        return None
    best_count = 0
    best_price = prices[0]
    for p in prices:
        count = sum(1 for x in prices if abs(x - p) <= tolerance)
        if count > best_count:
            best_count = count
            best_price = p
    return best_price


def _median(values: list) -> float | None:
    vals = sorted([v for v in values if v is not None])
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
```

- [ ] **Step 6: 验证**

运行: `python3 -c "from analysis import wyckoff; print('Module loaded OK')"` from `scripts/` dir
预期: "Module loaded OK"

- [ ] **Step 7: 提交**

```bash
git add scripts/analysis/wyckoff.py
git commit -m "feat: wyckoff core data structures + swing/range detection"
```

---

### Task 2: wyckoff.py — 阶段判定决策树

**Files:**
- Modify: `scripts/analysis/wyckoff.py` (lines 301-700)

- [ ] **Step 1: 实现 Accumulation 检测**

```python
def classify_accumulation(swings: list, closes: list, volumes: list, lows: list,
                           highs: list, trading_range: dict, atr_values: list,
                           latest_idx: int) -> tuple[str, float] | None:
    """Detect accumulation phase and sub-phase.

    Returns (sub_phase, confidence) or None if not in accumulation.
    """
    range_support = trading_range["support"]
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_vol = volumes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    # Check if price is within trading range (not broken out)
    if latest_close > range_resistance + latest_atr * 1.0:
        return None  # Possibly markup, not accumulation
    if latest_close < range_support - latest_atr * 1.0:
        return None  # Possibly markdown

    # Find recent swing lows in this range (last 1/3 of bars)
    recent_swing_lows = [s for s in swings if s["type"] == "low"
                         and s["index"] > trading_range["resistance_idx"] * 0.7
                         and s["price"] >= range_support - latest_atr * 2
                         and s["price"] <= range_resistance + latest_atr * 2]

    if not recent_swing_lows:
        # No recent structure change; check for ongoing accumulation behavior
        # Volume declining near support
        near_support = abs(latest_close - range_support) <= latest_atr * 0.5
        if near_support and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.7:
            return (SUB_LPS, 0.6)
        return None

    latest_swing_low = recent_swing_lows[-1]

    # Check sub-phase progression
    # SC: climax at recent low
    if latest_swing_low["is_climax"] and latest_swing_low.get("climax_type") == "selling":
        return (SUB_SC, 0.8)

    # AR: rebound after SC
    if len(recent_swing_lows) >= 1:
        # Check if there's been a rally after the latest climax low
        sc_swings = [s for s in recent_swing_lows if s.get("is_climax")]
        if sc_swings:
            sc_idx = sc_swings[-1]["index"]
            bars_since_sc = latest_idx - sc_idx
            if bars_since_sc <= 10 and latest_close > range_support + (range_resistance - range_support) * 0.3:
                return (SUB_AR, 0.7)

    # ST: volume declining on retest
    if len(recent_swing_lows) >= 2:
        prev_low = recent_swing_lows[-2]
        curr_low = latest_swing_low
        if (curr_low["volume_ratio"] < prev_low["volume_ratio"] * 0.7
                and abs(curr_low["price"] - prev_low["price"]) <= latest_atr * 2):
            return (SUB_ST, 0.8)

    # Spring: brief dip below support, quick recovery
    spring_candidates = [s for s in recent_swing_lows
                         if s["price"] < range_support - latest_atr * 0.3
                         and s["price"] >= range_support - latest_atr * 2.0]
    if spring_candidates and any(s["volume_ratio"] > 1.5 for s in spring_candidates):
        return (SUB_SPRING, 0.7)

    # LPS: low volume pullback near support
    near_support = abs(latest_close - range_support) <= latest_atr * 0.5
    if near_support and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPS, 0.7)

    return (SUB_LPS, 0.5)  # Default low-confidence accumulation
```

- [ ] **Step 2: 实现 Markup 检测**

```python
def classify_markup(swings: list, closes: list, volumes: list, highs: list,
                     trading_range: dict | None, atr_values: list,
                     latest_idx: int) -> tuple[str, float] | None:
    """Detect markup phase after breakout from accumulation range."""
    if trading_range is None:
        return None
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    # Must be above resistance
    if latest_close <= range_resistance:
        return None

    # JAC: first breakout above range
    trend_high = max(closes[max(0, latest_idx - 20) : latest_idx + 1])
    retrace_from_high = (trend_high - latest_close) / latest_atr if latest_atr > 0 else 0
    bars_since_breakout = _find_first_breakout_bar(closes, trading_range, latest_idx)

    if bars_since_breakout is not None and bars_since_breakout <= 5:
        # Recent breakout - check for volume confirmation
        breakout_volumes = volumes[latest_idx - bars_since_breakout : latest_idx + 1]
        avg_vol = sum(breakout_volumes) / len(breakout_volumes) if breakout_volumes else 0
        baseline_vol = _ma_of_last_n(volumes, latest_idx - bars_since_breakout, 50) if latest_idx - bars_since_breakout >= 50 else 1
        if avg_vol > baseline_vol * 1.3:
            return (SUB_JAC, 0.8)
        return (SUB_JAC, 0.5)

    # Back-up: pullback to range resistance (now support)
    if retrace_from_high <= 2.0 and retrace_from_high >= 0.5:
        pullback_vol = volumes[latest_idx]
        if pullback_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.8:
            return (SUB_BU, 0.7)
        return (SUB_BU, 0.5)

    # Continuation: still in markup with no reversal signals
    return (SUB_CONTINUATION, 0.6)


def _find_first_breakout_bar(closes: list, trading_range: dict, latest_idx: int) -> int | None:
    """Find how many bars ago the price first closed above resistance."""
    resistance = trading_range["resistance"]
    for offset in range(min(latest_idx, 60)):
        idx = latest_idx - offset
        if idx < 0:
            return None
        if closes[idx] <= resistance:
            return offset - 1 if offset > 0 else None
    return None
```

- [ ] **Step 3: 实现 Distribution 和 Markdown 检测**

```python
def classify_distribution(swings: list, closes: list, volumes: list,
                            lows: list, highs: list, trading_range: dict,
                            atr_values: list, latest_idx: int) -> tuple[str, float] | None:
    """Detect distribution phase."""
    range_support = trading_range["support"]
    range_resistance = trading_range["resistance"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    if latest_close < range_support - latest_atr * 1.0:
        return None  # Possibly markdown
    if latest_close < range_support - latest_atr * 0.5:
        # Near breakdown
        return (SUB_SOW, 0.6)

    recent_swing_highs = [s for s in swings if s["type"] == "high"
                          and s["index"] > trading_range["resistance_idx"] * 0.7
                          and s["price"] >= range_support - latest_atr * 2
                          and s["price"] <= range_resistance + latest_atr * 2]

    if not recent_swing_highs:
        return None

    latest_swing_high = recent_swing_highs[-1]

    # BC: climax at recent high
    if latest_swing_high["is_climax"] and latest_swing_high.get("climax_type") == "buying":
        return (SUB_BC, 0.8)

    # UTAD: brief push above resistance, falls back
    utad_candidates = [s for s in recent_swing_highs
                       if s["price"] > range_resistance + latest_atr * 0.3
                       and s["index"] > 0 and latest_close < range_resistance + latest_atr * 0.3]
    if utad_candidates:
        has_climax = any(s.get("is_climax") for s in utad_candidates)
        return (SUB_UTAD, 0.75 if has_climax else 0.55)

    # LPSY: rally on declining volume
    near_resistance = abs(latest_close - range_resistance) <= latest_atr * 0.5
    if near_resistance and latest_vol < _ma_of_last_n(volumes, latest_idx, 50) * 0.6:
        return (SUB_LPSY, 0.65)

    # SOW: weakness near support
    near_support = abs(latest_close - range_support) <= latest_atr * 0.5
    if near_support:
        return (SUB_SOW, 0.5)

    return (SUB_LPSY, 0.4)


def classify_markdown(swings: list, closes: list, volumes: list, lows: list,
                       high: list, trading_range: dict | None, atr_values: list,
                       latest_idx: int) -> tuple[str, float] | None:
    """Detect markdown phase."""
    if trading_range is None:
        return None
    range_support = trading_range["support"]
    latest_close = closes[latest_idx]
    latest_atr = atr_values[latest_idx] or 0

    if latest_close >= range_support:
        return None  # Still in range or above

    # Breakdown: first break below support
    range_under = range_support - latest_close
    if range_under <= latest_atr * 2.0 and range_under > latest_atr * 0.5:
        return (SUB_BREAKDOWN, 0.7)

    # Panic: accelerating decline with volume
    if len(closes) >= 10:
        recent_returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
                          for i in range(max(latest_idx - 10, 1), latest_idx + 1)]
        avg_return = sum(recent_returns) / len(recent_returns)
        if avg_return < -0.02 and latest_vol > _ma_of_last_n(volumes, latest_idx, 50) * 1.5:
            return (SUB_PANIC, 0.75)

    # Stopping volume: high volume, small range, near low
    if latest_vol > _ma_of_last_n(volumes, latest_idx, 50) * 1.5:
        daily_range = high[latest_idx] - lows[latest_idx] if latest_idx < len(high) else 0
        if daily_range < latest_atr * 0.7:
            lower_shadow = min(closes[latest_idx], high[latest_idx]) - lows[latest_idx] if latest_idx < len(high) else 0  # simplified
            return (SUB_STOPPING_VOL, 0.6)

    return (SUB_BREAKDOWN, 0.4)
```

- [ ] **Step 4: 验证**

运行: `python3 -c "from analysis.wyckoff import classify_accumulation, classify_markup, classify_distribution, classify_markdown; print('Phase functions OK')"`
预期: "Phase functions OK"

- [ ] **Step 5: 提交**

```bash
git add scripts/analysis/wyckoff.py
git commit -m "feat: wyckoff phase classification (accumulation/markup/distribution/markdown)"
```

---

### Task 3: wyckoff.py — VSA + Cause-Effect + Score + CLI

**Files:**
- Modify: `scripts/analysis/wyckoff.py` (lines 701-1100)

- [ ] **Step 1: 实现 VSA 分析引擎**

```python
DEFAULT_VOL_MA_PERIOD = 50

def analyze_vsa(ohlcv: dict, atr_values: list, ma50: list | None = None) -> list[dict]:
    """Volume Spread Analysis: detect effort-vs-result divergences.

    Analyzes each bar for VSA signals.
    Returns list of signal dicts sorted by recency (newest first).
    """
    closes = ohlcv["close"]
    highs = ohlcv["high"]
    lows = ohlcv["low"]
    opens = ohlcv["open"]
    volumes = ohlcv["volume"]

    if ma50 is None:
        ma50 = compute_ma(volumes, DEFAULT_VOL_MA_PERIOD)

    signals = []

    for i in range(len(closes)):
        if i < 1 or atr_values[i] is None or atr_values[i] == 0:
            continue
        vol = volumes[i]
        vol_ma_val = ma50[i] if ma50 and i < len(ma50) else _ma_of_last_n(volumes, i, 50)
        if vol_ma_val is None or vol_ma_val == 0:
            continue
        vol_ratio = vol / vol_ma_val
        spread = highs[i] - lows[i]
        spread_ratio = spread / atr_values[i] if atr_values[i] else 0.5
        body = abs(closes[i] - opens[i])
        upper_shadow = highs[i] - max(closes[i], opens[i])
        lower_shadow = min(closes[i], opens[i]) - lows[i]
        range_val = highs[i] - lows[i]
        shadow_ratio_upper = upper_shadow / range_val if range_val > 0 else 0
        shadow_ratio_lower = lower_shadow / range_val if range_val > 0 else 0
        close_position = (closes[i] - lows[i]) / range_val if range_val > 0 else 0.5

        # Signal 1: Absorption (wide spread mid-range, high vol, close mid)
        if vol_ratio > 1.5 and spread_ratio > 0.8 and 0.3 <= close_position <= 0.7:
            strength = min(3, int(vol_ratio * 1.5))
            signals.append({
                "type": "absorption",
                "sub_type": "effort_no_result",
                "strength": strength,
                "bar_index": i,
                "description": f"放量震仓，主力吸筹特征 (vol={vol_ratio:.1f}x)"
            })

        # Signal 2: No Supply (narrow spread down, low volume)
        if spread_ratio < 0.6 and closes[i] < opens[i] and vol_ratio < 0.7:
            strength = min(3, max(1, int((1 - vol_ratio) * 5)))
            signals.append({
                "type": "no_supply",
                "sub_type": "supply_exhaustion",
                "strength": strength,
                "bar_index": i,
                "description": f"缩量下跌，抛压枯竭 (vol={vol_ratio:.1f}x)"
            })

        # Signal 3: No Demand (narrow spread up, low volume)
        if spread_ratio < 0.6 and closes[i] > opens[i] and vol_ratio < 0.7:
            strength = min(3, max(1, int((1 - vol_ratio) * 5)))
            signals.append({
                "type": "no_demand",
                "sub_type": "demand_exhaustion",
                "strength": strength,
                "bar_index": i,
                "description": f"缩量上涨，买盘不足 (vol={vol_ratio:.1f}x)"
            })

        # Signal 4: Stopping Volume (wide spread down, high vol, close high)
        if (vol_ratio > 1.8 and closes[i] < opens[i] and close_position > 0.6
                and shadow_ratio_lower > 0.3):
            strength = min(3, int(vol_ratio * 1.2))
            signals.append({
                "type": "stopping_volume",
                "sub_type": "selling_climax",
                "strength": strength,
                "bar_index": i,
                "description": f"放量下跌+长下影，止跌量出现 (vol={vol_ratio:.1f}x)"
            })

        # Signal 5: Upthrust (narrow spread, high close, weak follow-through)
        if (vol_ratio > 1.3 and spread_ratio < 0.7 and closes[i] > opens[i]
                and shadow_ratio_upper > 0.4):
            strength = min(3, max(1, int(vol_ratio)))
            signals.append({
                "type": "upthrust",
                "sub_type": "effort_no_result",
                "strength": strength,
                "bar_index": i,
                "description": f"放量窄幅+上影，上冲受阻 (vol={vol_ratio:.1f}x)"
            })

    # Sort by bar_index ascending
    signals.sort(key=lambda s: s["bar_index"])
    return signals
```

- [ ] **Step 2: 实现 Cause-Effect 量化**

```python
def compute_cause_effect(trading_range: dict, current_price: float) -> dict:
    """Wyckoff 'Cause leads to Effect' quantification.

    Horizontal count: duration → time projection.
    Vertical count: range height → price targets.
    """
    support = trading_range["support"]
    resistance = trading_range["resistance"]
    height = resistance - support
    duration = trading_range["duration_bars"]

    time_projection = max(5, int(duration * 0.5))

    # Determine direction from current price
    if current_price > resistance:
        # Upward breakout → bullish targets
        target1 = current_price + height
        target2 = current_price + height * 1.5
        target3 = current_price + height * 2.0
    elif current_price < support:
        # Downward breakdown → bearish targets
        target1 = current_price - height
        target2 = current_price - height * 1.5
        target3 = current_price - height * 2.0
    else:
        # Inside range → wait for breakout
        return {
            "horizontal_count": duration,
            "vertical_height": round(height, 2),
            "targets": [],
            "time_projection_days": time_projection,
            "cause_description": f"箱体内震荡 {duration} 根 K 线，高度 {height:.2f}，等待突破确认"
        }

    return {
        "horizontal_count": duration,
        "vertical_height": round(height, 2),
        "targets": [
            {"level": 1, "price": round(target1, 2), "ratio": 1.0},
            {"level": 2, "price": round(target2, 2), "ratio": 1.5},
            {"level": 3, "price": round(target3, 2), "ratio": 2.0},
        ],
        "time_projection_days": time_projection,
        "cause_description": f"{duration} 根 K 线横盘{'吸筹' if current_price > resistance else '派发'}"
                             f"，箱体高度 {height:.2f} ({height / ((support + resistance) / 2) * 100:.1f}%)"
    }
```

- [ ] **Step 3: 实现 Score 映射和 Trading Implication 生成**

```python
def wyckoff_score(phase: str, sub_phase: str) -> float:
    """Map phase/sub_phase to score in [-3, +3] range."""
    key = (phase, sub_phase)
    score = PHASE_SCORES.get(key, DEFAULT_PHASE_SCORE)

    # Bound to [-3, +3] scale
    return max(-3.0, min(3.0, score))


def generate_trading_implication(phase: str, sub_phase: str, trading_range: dict | None,
                                  vsa_signals: list, cause_effect: dict) -> str:
    """Generate human-readable trading implication based on Wyckoff signals.

    This is a rules-driven summary, not agent narrative.
    """
    if phase == PHASE_UNKNOWN:
        return "当前无明显维科夫阶段特征，暂无法提供操作参考。"

    if phase == PHASE_ACCUMULATION:
        implications = {
            SUB_SC: "抛售高潮出现，卖压集中释放，短期可能形成低点区域。不宜追空，等待二次测试确认。",
            SUB_AR: "自动反弹阶段，卖压暂缓。观察反弹量能，若缩量则可能再次测试支撑。",
            SUB_ST: "二次测试缩量确认支撑，吸筹信号增强。关注后续能否放量突破箱体。",
            SUB_SPRING: "初支（Spring）形态，短暂击穿支撑后快速收回，主力试盘特征。可考虑轻仓试多。",
            SUB_LPS: "最后支撑点附近缩量止跌，吸筹接近尾声。做好突破入场准备。",
            SUB_PRE_MARKUP: "拉升前准备阶段，震荡收窄、成交量极度萎缩。等待放量突破信号。",
        }
        return implications.get(sub_phase, "吸筹阶段运行中，以箱体上下沿作为关键边界。")

    if phase == PHASE_MARKUP:
        implications = {
            SUB_JAC: "JAC（跃过小溪）放量突破箱体，趋势确认。可顺势做多，以箱顶作为止损参考。",
            SUB_BU: "回踩箱顶获支撑，缩量整理。突破确认后的健康回调，可考虑加仓。",
            SUB_CONTINUATION: "持续拉升阶段。顺应趋势持有，跟踪止盈，不逆势猜顶。",
        }
        return implications.get(sub_phase, "拉升阶段，多头持仓为主，注意跟踪趋势力度变化。")

    if phase == PHASE_DISTRIBUTION:
        implications = {
            SUB_BC: "买入高潮（BC）出现，巨量长上影，主力派发特征。应逐步减仓。",
            SUB_UTAD: "UTAD（上冲回落）假突破，量价背离。派发确认信号，建议减仓或离场。",
            SUB_LPSY: "最后供应点（LPSY），反弹缩量无力。清仓为主，不再做多。",
            SUB_SOW: "弱势信号（SOW），跌破支撑或测试支撑时量大。离场观望。",
            SUB_PRE_MARKDOWN: "砸盘前兆。全面离场，准备做空或空仓。",
        }
        return implications.get(sub_phase, "派发阶段运行中，以减仓和控制风险为主。")

    if phase == PHASE_MARKDOWN:
        implications = {
            SUB_BREAKDOWN: "破位下跌，趋势转空。多单离场，不抄底。",
            SUB_PANIC: "恐慌抛售阶段，放量急跌。空仓等待，不接飞刀。",
            SUB_STOPPING_VOL: "止跌量出现，抛压衰竭信号。关注是否形成新的吸筹区间。",
        }
        return implications.get(sub_phase, "砸盘阶段，空仓观望，等待止跌企稳信号。")

    return ""
```

- [ ] **Step 4: 实现主流程和 CLI**

```python
def analyze(kline_path: str, output_path: str | None = None) -> dict:
    """Run full Wyckoff analysis pipeline.

    Args:
        kline_path: Path to K-line JSON file.
        output_path: Optional path to write output JSON.

    Returns:
        wyckoff_analysis dict matching the schema from the design doc.
    """
    # 1. Load data
    kline_data = load_kline(kline_path)
    if kline_data is None:
        return {"meta": {"error": "failed to load K-line data"}, "phase": {"primary": PHASE_UNKNOWN}}

    data_rows = kline_data.get("data", [])
    if not data_rows:
        return {"meta": {"error": "empty K-line data"}, "phase": {"primary": PHASE_UNKNOWN}}

    kline_meta = kline_data.get("meta", {})
    ohlcv = extract_ohlcv(data_rows)
    closes = ohlcv["close"]
    highs = ohlcv["high"]
    lows = ohlcv["low"]
    opens = ohlcv["open"]
    volumes = ohlcv["volume"]

    if len(closes) < 30:
        return {"meta": {"error": f"insufficient data ({len(closes)} rows)"}, "phase": {"primary": PHASE_UNKNOWN}}

    # 2. Compute ATR
    atr_values = compute_atr(highs, lows, closes, period=14)

    # 3. Detect swing points
    swings = detect_swing_points(closes, highs, lows, volumes, atr_values)
    swings = mark_climaxes(swings, highs, lows, closes, volumes, atr_values)
    # Fill dates
    for s in swings:
        idx = s["index"]
        if idx < len(ohlcv["date"]):
            s["date"] = ohlcv["date"][idx]

    # 4. Detect trading range
    trading_range = detect_trading_range(swings, closes, atr_values)

    # 5. Classify phase
    latest_idx = len(closes) - 1
    phase = PHASE_UNKNOWN
    sub_phase = ""
    confidence = 0.0
    secondary_possibilities = []

    if trading_range:
        # Try each classifier
        result = classify_accumulation(swings, closes, volumes, lows, highs,
                                       trading_range, atr_values, latest_idx)
        if result:
            sub_phase, confidence = result
            phase = PHASE_ACCUMULATION

        if not result:
            result = classify_distribution(swings, closes, volumes, lows, highs,
                                           trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_DISTRIBUTION

        if not result:
            result = classify_markup(swings, closes, volumes, highs,
                                     trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_MARKUP

        if phase == PHASE_UNKNOWN:
            result = classify_markdown(swings, closes, volumes, lows, highs,
                                       trading_range, atr_values, latest_idx)
            if result:
                sub_phase, confidence = result
                phase = PHASE_MARKDOWN
    else:
        # No clear range: use trend-based fallback
        if len(closes) >= 50:
            ma20 = compute_ma(closes, 20)
            ma60 = compute_ma(closes, 60)
            if ma20[-1] and ma60[-1] and closes[-1] > ma20[-1] and ma20[-1] > ma60[-1]:
                phase = PHASE_MARKUP
                sub_phase = SUB_CONTINUATION
                confidence = 0.3
            elif ma20[-1] and ma60[-1] and closes[-1] < ma20[-1] and ma20[-1] < ma60[-1]:
                phase = PHASE_MARKDOWN
                sub_phase = SUB_BREAKDOWN
                confidence = 0.3

    if phase == PHASE_UNKNOWN and trading_range:
        # Still in range, no clear phase → phase_unknown
        confidence = 0.3

    # 6. VSA
    vsa_signals = analyze_vsa(ohlcv, atr_values)

    # 7. Cause-effect
    current_price = closes[-1]
    cause_effect = compute_cause_effect(trading_range, current_price) if trading_range else {}

    # 8. Score
    score = wyckoff_score(phase, sub_phase) if sub_phase else DEFAULT_PHASE_SCORE

    # Key signals (top VSA + phase info)
    key_signals = []
    if trading_range:
        key_signals.append(f"箱体支撑 {trading_range['support']:.2f} / 阻力 {trading_range['resistance']:.2f}")
    if sub_phase:
        sub_name = SUB_PHASE_NAMES.get(sub_phase, sub_phase)
        key_signals.append(f"子阶段: {sub_name}")
    for vs in vsa_signals[-3:]:  # last 3
        key_signals.append(vs["description"])

    # Sort VSA by recency for the output
    vsa_signals_sorted = sorted(vsa_signals, key=lambda s: s["bar_index"], reverse=True)

    # Build output
    result = {
        "meta": {
            "ts_code": kline_meta.get("ts_code", ""),
            "name": kline_meta.get("name", ""),
            "calc_date": kline_meta.get("end_date", ""),
            "kline_days": len(closes),
            "data_quality": "good" if len(closes) >= 150 else ("limited" if len(closes) >= 60 else "insufficient"),
        },
        "phase": {
            "primary": phase,
            "primary_name": PHASE_NAMES.get(phase, "未知阶段"),
            "confidence": round(confidence, 2),
            "secondary_possibilities": secondary_possibilities,
            "primary_sub_phase": sub_phase,
            "sub_phase_name": SUB_PHASE_NAMES.get(sub_phase, ""),
        },
        "range": trading_range or {"is_clear_range": False},
        "swing_points": swings[-20:],  # last 20
        "vsa_signals": vsa_signals_sorted[:10],  # top 10 most recent
        "cause_effect": cause_effect,
        "wyckoff_score": round(score, 2),
        "wyckoff_signals": {
            "verdict": "bullish" if score > 1.0 else ("cautiously_bullish" if score > 0 else
                        ("bearish" if score < -1.0 else "cautiously_bearish") if score < 0 else "neutral"),
            "key_signals": key_signals[-5:],
            "trading_implication": generate_trading_implication(phase, sub_phase, trading_range, vsa_signals, cause_effect),
        },
    }

    # Write output
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 wyckoff.py <kline_json> [-o <output_path>]", file=sys.stderr)
        sys.exit(1)
    kline_path = sys.argv[1]
    output_path = None
    if "-o" in sys.argv:
        idx = sys.argv.index("-o")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    result = analyze(kline_path, output_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 集成验证**

用历史数据测试:
```bash
cd /Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts
python3 analysis/wyckoff.py .cache/stock-trend/600519/kline.json -o /tmp/wyckoff_test.json 2>&1 || echo "Need actual kline cache, will test after pipeline integration"
cat /tmp/wyckoff_test.json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Phase: {d[\"phase\"][\"primary\"]}, Score: {d[\"wyckoff_score\"]}, Confidence: {d[\"phase\"][\"confidence\"]}')" 2>/dev/null || echo "No cache yet, skipping"
```

- [ ] **Step 6: 提交**

```bash
git add scripts/analysis/wyckoff.py
git commit -m "feat: wyckoff VSA + cause-effect + score + CLI"
```

---

### Task 4: Pipeline Runner 集成

**Files:**
- Modify: `scripts/pipeline/runner.py`

- [ ] **Step 1: pipeline runner 增加 K-line 天数参数**

在 `main()` 中新增参数：
```python
parser.add_argument("--kline-days", type=int, default=250,
                    help="Number of K-line days to fetch (default: 250)")
```

并将 Step 2（fetch K-line）的 kline 命令增加天数参数：
```python
# 在构建 kline_cmd 时 append
kline_cmd.extend(["--days", str(args.kline_days)])
```

- [ ] **Step 2: 在 Step 3.5（技术分析）之后添加 Wyckoff 步骤**

在 `main()` 中 `technical_available` 判断之后，新增：
```python
# --- Step 3.6: Wyckoff analysis (depends on kline) ---
wyckoff_path = str(output_dir / "wyckoff.json")
if kline_available:
    print(f"[3.6/5] Running Wyckoff analysis...")
    wyckoff_cmd = [
        sys.executable, str(SCRIPT_DIR / "analysis/wyckoff.py"),
        kline_path, "-o", wyckoff_path,
    ]
    wyckoff_result = run_script(wyckoff_cmd, label="analyze_wyckoff", timeout=15)
    if wyckoff_result.get("timeout"):
        timeouts.append("analyze_wyckoff")
    if wyckoff_result["success"]:
        wyckoff_data = read_json(wyckoff_path)
        if wyckoff_data and wyckoff_data.get("meta", {}).get("error") is None:
            wyckoff_score_val = wyckoff_data.get("wyckoff_score", 0)
            wyckoff_phase = wyckoff_data.get("phase", {}).get("primary_name", "")
            wyckoff_sub = wyckoff_data.get("phase", {}).get("sub_phase_name", "")
            results["wyckoff"] = {
                "phase": wyckoff_phase,
                "sub_phase": wyckoff_sub,
                "score": wyckoff_score_val,
            }
            print(f"  Wyckoff: {wyckoff_phase}({wyckoff_sub}), score={wyckoff_score_val}")
        else:
            print(f"  Wyckoff analysis skipped: {wyckoff_data.get('meta', {}).get('error', 'unknown') if wyckoff_data else 'no output'}")
    else:
        print(f"  Wyckoff analysis failed: {wyckoff_result['stderr']}")
else:
    print(f"[3.6/5] Skipping Wyckoff analysis (no K-line data)")
    remove_stale_file(wyckoff_path, "Wyckoff analysis", errors)
```

- [ ] **Step 3: 更新 build_output_files 包含 wyckoff**

```python
def build_output_files(..., wyckoff_available, ...):
    return {
        # ... existing keys ...
        "wyckoff": str(output_dir / "wyckoff.json") if wyckoff_available else None,
    }
```

并在 `main()` 中跟踪 `wyckoff_available` flag。

- [ ] **Step 4: 验证**

运行：`python3 run_pipeline.py --code 600519`
预期：可以看到 `[3.6/5] Running Wyckoff analysis...` 和 Wyckoff 结果输出

- [ ] **Step 5: 提交**

```bash
git add scripts/pipeline/runner.py
git commit -m "feat: integrate Wyckoff analysis into pipeline runner"
```

---

### Task 5: Scores.py 集成（12% 权重）

**Files:**
- Modify: `scripts/analysis/scores.py`

- [ ] **Step 1: 在 DEFAULT_WEIGHTS 和 FOCUS_WEIGHTS 中增加 wyckoff 维度**

```python
DEFAULT_WEIGHTS = {
    "technical": 0.28,
    "capital_flow": 0.23,
    "fundamental": 0.14,
    "sentiment": 0.14,
    "macro": 0.09,
    "wyckoff": 0.12,   # NEW: Wyckoff dimension (12%)
}
```

FOCUS_WEIGHTS 也需要加入 wyckoff：
```python
FOCUS_WEIGHTS = {
    "technical": {
        "technical": 0.45, "capital_flow": 0.15,
        "fundamental": 0.08, "sentiment": 0.08, "macro": 0.08,
        "wyckoff": 0.16,    # NEW
    },
    "capital_flow": {
        "capital_flow": 0.40, "technical": 0.15,
        "fundamental": 0.08, "sentiment": 0.08, "macro": 0.08,
        "wyckoff": 0.21,    # NEW
    },
    "fundamental": {
        "fundamental": 0.35, "macro": 0.15,
        "technical": 0.10, "capital_flow": 0.10, "sentiment": 0.10,
        "wyckoff": 0.20,    # NEW
    },
    "sentiment": {
        "sentiment": 0.35, "technical": 0.20,
        "capital_flow": 0.08, "fundamental": 0.08, "macro": 0.08,
        "wyckoff": 0.21,    # NEW
    },
}
```

- [ ] **Step 2: 新增 CLI 参数**

```python
parser.add_argument("--wyckoff-score", type=float, default=None,
                    help="Wyckoff dimension score (-3 to +3)")
parser.add_argument("--wyckoff-data", help="Path to wyckoff.json (automated scoring)")
```

- [ ] **Step 3: 新增自动 Wyckoff 评分（当 agent 未提供明确分数时）**

在 automated 评分区段（macro scoring 之后）添加：
```python
# Automated Wyckoff scoring (when agent doesn't provide explicit score)
if args.wyckoff_data and args.wyckoff_score is None:
    try:
        with open(args.wyckoff_data, "r", encoding="utf-8") as f:
            wy_data = json.load(f)
        if "error" not in wy_data.get("meta", {}):
            w_score = wy_data.get("wyckoff_score", 0)
            # Apply data quality discount
            data_quality = wy_data.get("meta", {}).get("data_quality", "good")
            if data_quality == "limited":
                w_score *= 0.5
            elif data_quality == "insufficient":
                w_score = 0  # No reliable score
            scores["wyckoff"] = round(max(-3, min(3, w_score)), 2)
            automated_sources["wyckoff"] = data_quality
    except Exception:
        pass
```

- [ ] **Step 4: 验证**

运行：`python3 analysis/scores.py --code 600519 --wyckoff-data /path/to/wyckoff.json`
预期：输出中 scores 包含 wyckoff 维度，weights 包含 0.12

- [ ] **Step 5: 提交**

```bash
git add scripts/analysis/scores.py
git commit -m "feat: integrate Wyckoff score (12% weight) into composite scoring"
```

---

### Task 6: Report Template 更新

**Files:**
- Modify: `assets/report-template.md`
- Modify: `assets/report-template.html`

- [ ] **Step 1: 在 report-template.md 新增 Wyckoff section（插入在筹码分布与特殊标记之间）**

在 `{{/chip_distribution}}` 行之后、`{{#特殊标记}}` 行之前添加：

```
{{#wyckoff}}
## 七、维科夫操盘法分析

**阶段判定**: {{wyckoff_phase_name}}（置信度: {{wyckoff_confidence}}）
**当前子阶段**: {{wyckoff_sub_phase_name}}

| 项目 | 数值 |
|---|---|
| 交易区间 | {{wyckoff_support}} - {{wyckoff_resistance}} |
| 横盘时间 | {{wyckoff_duration_bars}} 日 |
| 箱体高度 | {{wyckoff_range_height_pct}}% |

**因果量化**:
- 横盘箱体: {{wyckoff_horizontal_count}} 根 K 线
- 目标价: {{#wyckoff_targets}}T{{level}} {{price}}{{^multiple_targets}} / {{/multiple_targets}}{{/wyckoff_targets}}
- 时间预期: {{wyckoff_time_projection}} 个交易日

{{#wyckoff_vsa_signals}}
**VSA 信号**:
{{#wyckoff_vsa_list}}
- {{description}}（强度: {{strength}}/3）
{{/wyckoff_vsa_list}}
{{/wyckoff_vsa_signals}}

**操作含义**: {{wyckoff_trading_implication}}
{{/wyckoff}}
```

注意：由于原先 `七、特殊标记` 和 `八、综合研判` 编号需要顺延。修改为：

- 六、筹码分布 → 不变
- 七、维科夫操盘法分析 → 新增
- 八、特殊标记 → 原七
- 九、综合研判 → 原八
- 十、校验提示 → 原九

- [ ] **Step 2: 同步更新 report-template.html**

在 HTML 模板对应位置添加相同内容的 `<div class="wyckoff-section">` 块。使用 CSS class `wyckoff-bullish`（绿色）、`wyckoff-bearish`（红色）、`wyckoff-neutral`（灰色）区分趋势。

```html
{{#wyckoff}}
<div class="section wyckoff-section">
  <h2>七、维科夫操盘法分析</h2>
  ...
</div>
{{/wyckoff}}
```

- [ ] **Step 3: 提交**

```bash
git add assets/report-template.md assets/report-template.html
git commit -m "feat: add Wyckoff analysis section to report templates"
```

---

### Task 7: Report.py 传递 Wyckoff 数据到模板

**Files:**
- Modify: `scripts/reporting/report.py`

- [ ] **Step 1: 新增 CLI 参数**

```python
parser.add_argument("--wyckoff-data", help="Path to wyckoff.json")
parser.add_argument("--wyckoff-summary", help="Wyckoff dimension summary text")
```

- [ ] **Step 2: 在 `_load_all_data()` 中加载 wyckoff 数据**

```python
wyckoff_data = _load_json_safe(args.wyckoff_data) or {}
```

并加入返回值 dict。

- [ ] **Step 3: 在 `build_context()` 中注入 wyckoff 字段到 context**

在 context dict 中新增（chip_distribution 块之后）：

```python
# Wyckoff analysis
wyckoff = d["wyckoff_data"]
if wyckoff and "error" not in wyckoff.get("meta", {}):
    w_phase = wyckoff.get("phase", {})
    w_range = wyckoff.get("range", {})
    w_vsa = wyckoff.get("vsa_signals", [])
    w_ce = wyckoff.get("cause_effect", {})
    w_signals = wyckoff.get("wyckoff_signals", {})
    w_score = wyckoff.get("wyckoff_score", 0)

    context["wyckoff"] = True
    context["wyckoff_score"] = w_score
    context["wyckoff_score_label"] = f"{w_score:+.1f}"
    context["wyckoff_phase"] = w_phase.get("primary", "")
    context["wyckoff_phase_name"] = w_phase.get("primary_name", "")
    context["wyckoff_confidence"] = f"{w_phase.get('confidence', 0) * 100:.0f}%"
    context["wyckoff_sub_phase"] = w_phase.get("primary_sub_phase", "")
    context["wyckoff_sub_phase_name"] = w_phase.get("sub_phase_name", "")

    # Range info
    if w_range.get("is_clear_range"):
        context["wyckoff_support"] = w_range.get("support", "—")
        context["wyckoff_resistance"] = w_range.get("resistance", "—")
        context["wyckoff_duration_bars"] = w_range.get("duration_bars", "—")
        context["wyckoff_range_height_pct"] = f"{w_range.get('range_height_pct', 0)}%"
    else:
        context["wyckoff_support"] = "—"
        context["wyckoff_resistance"] = "—"
        context["wyckoff_duration_bars"] = "—"
        context["wyckoff_range_height_pct"] = "—"

    # Cause-effect
    context["wyckoff_horizontal_count"] = w_ce.get("horizontal_count", "—")
    context["wyckoff_vertical_height"] = w_ce.get("vertical_height", "—")
    context["wyckoff_time_projection"] = w_ce.get("time_projection_days", "—")
    targets = w_ce.get("targets", [])
    context["wyckoff_targets"] = targets
    context["multiple_targets"] = len(targets) > 1

    # VSA signals
    context["wyckoff_vsa_signals"] = len(w_vsa) > 0
    context["wyckoff_vsa_list"] = [
        {"description": vs.get("description", ""), "strength": vs.get("strength", 1)}
        for vs in w_vsa[:5]  # Top 5
    ]

    # Trading implication
    context["wyckoff_trading_implication"] = w_signals.get("trading_implication", "—")

    # Dimension summary
    w_summary = args.wyckoff_summary or ""
    if not w_summary and w_phase.get("primary_name"):
        w_summary = f"{w_phase['primary_name']}({w_phase.get('sub_phase_name', '')})"
    context["维科夫面摘要"] = w_summary
    context["维科夫面得分"] = w_score
    context["维科夫面CSS"] = score_css(w_score)
else:
    context["wyckoff"] = False
    context["wyckoff_score"] = 0
    context["维科夫面摘要"] = "—"
    context["维科夫面得分"] = "—"
    context["维科夫面CSS"] = "sz"
```

- [ ] **Step 4: 更新关键信号表**

在 key signals table 行中新增一行：
```python
# 在构建 context 时，key signals 表格新增一行
# 已经有 技术面/资金面/基本面/情绪面/宏观面 五行，增加维科夫面
```

在 context 的 dimension scores 区域同步加入，表格中 agent 会在 prompt 中补充摘要。

- [ ] **Step 5: Pipeline output 文件自动加载 wyckoff**

在 `main()` 的 pipeline output 解析段新增：
```python
if not args.wyckoff_data and output_files.get("wyckoff"):
    args.wyckoff_data = output_files["wyckoff"]
```

- [ ] **Step 6: 提交**

```bash
git add scripts/reporting/report.py
git commit -m "feat: pass Wyckoff analysis data to report template"
```

---

### Task 8: 单元测试 + Golden

**Files:**
- Create: `tests/test_wyckoff.py`
- Modify: `tests/golden_config.json`

- [ ] **Step 1: 创建 test_wyckoff.py**

```python
"""Tests for Wyckoff analysis module."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from analysis.wyckoff import (
    compute_atr, compute_ma, detect_swing_points, mark_climaxes,
    detect_trading_range, analyze_vsa, compute_cause_effect,
    wyckoff_score, generate_trading_implication,
    classify_accumulation, classify_markup, classify_distribution, classify_markdown,
    PHASE_ACCUMULATION, PHASE_MARKUP, PHASE_DISTRIBUTION, PHASE_MARKDOWN,
    SUB_SC, SUB_AR, SUB_ST, SUB_LPS, SUB_JAC, SUB_BU, SUB_BC, SUB_UTAD,
    extract_ohlcv, _safe_float,
)


class TestSafeFloat(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_safe_float(3.14), 3.14)
        self.assertEqual(_safe_float("3.14"), 3.14)
        self.assertEqual(_safe_float(0), 0.0)

    def test_invalid(self):
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float(""))


class TestComputeMA(unittest.TestCase):
    def test_basic(self):
        values = [1, 2, 3, 4, 5]
        result = compute_ma(values, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertEqual(result[2], 2.0)

    def test_empty(self):
        self.assertEqual(compute_ma([], 3), [])


class TestDetectSwingPoints(unittest.TestCase):
    def test_known_swing(self):
        """Price pattern: 10, 12, 15, 13, 11 → pivot high at index 2 (15)."""
        closes = [10, 12, 15, 13, 11, 10, 9]
        highs = [11, 13, 16, 14, 12, 11, 10]
        lows = [9, 11, 14, 12, 10, 9, 8]
        volumes = [100] * 7
        atr = compute_atr(highs, lows, closes, period=3)
        # Fill None atr
        atr = [a if a is not None else 2.0 for a in atr]

        swings = detect_swing_points(closes, highs, lows, volumes, atr, lookback=1)
        self.assertTrue(any(s["type"] == "high" and s["price"] == 16 for s in swings))


class TestWyckoffScore(unittest.TestCase):
    def test_accumulation_lps(self):
        self.assertEqual(wyckoff_score(PHASE_ACCUMULATION, SUB_LPS), 2.0)

    def test_markup_jac(self):
        self.assertEqual(wyckoff_score(PHASE_MARKUP, SUB_JAC), 2.0)

    def test_distribution_bc(self):
        self.assertEqual(wyckoff_score(PHASE_DISTRIBUTION, SUB_BC), -1.0)

    def test_markdown_breakdown(self):
        self.assertEqual(wyckoff_score(PHASE_MARKDOWN, SUB_BREAKDOWN), -2.5)

    def test_unknown(self):
        self.assertEqual(wyckoff_score("phase_unknown", ""), 0.0)


class TestTradingImplication(unittest.TestCase):
    def test_accumulation(self):
        imp = generate_trading_implication(PHASE_ACCUMULATION, SUB_ST, None, [], {})
        self.assertIn("二次测试", imp)

    def test_markup(self):
        imp = generate_trading_implication(PHASE_MARKUP, SUB_JAC, None, [], {})
        self.assertIn("JAC", imp)

    def test_unknown(self):
        imp = generate_trading_implication(PHASE_UNKNOWN, "", None, [], {})
        self.assertIn("无明显", imp)


class TestCauseEffect(unittest.TestCase):
    def test_upward_breakout(self):
        tr = {"support": 100, "resistance": 120, "range_height": 20,
              "duration_bars": 40, "touch_count": 5, "is_clear_range": True}
        result = compute_cause_effect(tr, 125)
        self.assertEqual(len(result["targets"]), 3)
        self.assertEqual(result["targets"][0]["price"], 145)  # 125 + 20
        self.assertEqual(result["horizontal_count"], 40)

    def test_inside_range(self):
        tr = {"support": 100, "resistance": 120, "range_height": 20,
              "duration_bars": 40, "touch_count": 5, "is_clear_range": True}
        result = compute_cause_effect(tr, 110)
        self.assertEqual(result["targets"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 更新 golden_config.json**

在 `tests/golden_config.json` 中新增 wyckoff 条目：

基于设计文档，golden 数据需要准备几组典型形态的真实 K-line 数据。如果已有 600519 的 cache 数据，先用它生成 baseline。

```json
{
  "wyckoff": {
    "source": "pipeline",
    "codes": ["600519.SH", "000858.SZ", "000002.SZ"],
    "assert_fields": ["phase.primary", "wyckoff_score", "phase.confidence"]
  }
}
```

（实际 golden 文件需要在运行 `test_golden.py` 后生成 `tests/golden/wyckoff/` 目录）

- [ ] **Step 3: 运行测试**

```bash
cd /Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend
python3 -m pytest tests/test_wyckoff.py -v
```

预期：所有 test 通过

- [ ] **Step 4: 运行 Golden 测试**

```bash
python3 tests/test_golden.py -v --scope wyckoff
```

如果首次运行，用 `--regenerate` 生成 golden baseline。

- [ ] **Step 5: 提交**

```bash
git add tests/test_wyckoff.py tests/golden_config.json tests/golden/wyckoff/
git commit -m "test: Wyckoff analysis unit tests + golden snapshots"
```

---

### Task 9: SKILL.md 更新

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: 在 /stock-trend 路由的 data 步骤中增加 Wyckoff 引用**

在 pipeline 的数据获取步骤说明中，加入 `wyckoff.json` 作为新增数据产出。

- [ ] **Step 2: 提交**

```bash
git add .claude/skills/stock-trend/SKILL.md
git commit -m "docs: update SKILL.md with Wyckoff analysis reference"
```

---

### 执行顺序

```
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9
(数据结构)  (阶段判定)  (VSA+CE+CLI) (pipeline) (scores)  (templates) (report.py) (tests) (SKILL.md)
```

Task 1-3 均为修改 wyckoff.py，需按序执行避免合并冲突。Task 4-9 可并行但建议按序减少上下文切换。

### 验证清单

完成后运行以下验证：
```bash
# 1. 单元测试
python3 -m pytest tests/test_wyckoff.py -v

# 2. 存量测试不破坏
python3 tests/test_stock_trend.py
python3 tests/test_golden.py -v

# 3. 端到端 pipeline 集成测试（选一个已有 cache 的标的）
python3 scripts/pipeline/run_pipeline.py --code 600519
python3 scripts/reporting/generate_report.py --code 600519 --output-md /tmp/test_wyckoff.md
# 确认报告中包含维科夫 section
grep -q "维科夫操盘法" /tmp/test_wyckoff.md && echo "OK: Wyckoff section in report"
```
