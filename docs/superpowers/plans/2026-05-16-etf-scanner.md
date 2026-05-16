# ETF Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `/etf-scan` slash command that scans 30-50 A-share ETFs daily, ranks them by quick score + deep score, and outputs a ranked report in the conversation.

**Architecture:** New `etf_scanner.py` orchestrator that calls existing scripts via subprocess (zero modifications to existing code). Two-phase approach: Phase 1 runs lightweight quick_score on all ETFs (~60s), Phase 2 runs full pipeline on top N (~120s). Outputs structured JSON for Claude to render.

**Tech Stack:** Python 3, yaml, subprocess (calling existing stock-trend scripts), ThreadPoolExecutor for parallelism.

---
## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/watchlist.yaml` | **Create** | ETF pool config: categories, codes, settings |
| `scripts/etf_scanner.py` | **Create** | Core orchestrator: Phase 1/2/3 + quick_score |
| `tests/test_etf_scanner.py` | **Create** | Unit + integration tests |
| `SKILL.md` | **Modify** | Register `/etf-scan` command + allowed-tools |

**Zero modifications** to existing .py files (fetch_kline_eastmoney.py, run_pipeline.py, compute_scores.py, etc.)

---

### Task 1: Create watchlist.yaml

**Files:**
- Create: `.claude/skills/stock-trend/scripts/watchlist.yaml`

- [ ] **Step 1: Write watchlist.yaml**

```yaml
# A 股 ETF 精选扫描池
# 按板块分类，支持 --focus 指定板块
categories:
  - name: 宽基指数
    etfs:
      - code: 510050    # 上证50ETF
      - code: 510300    # 沪深300ETF
      - code: 510500    # 中证500ETF
      - code: 512100    # 中证1000ETF
      - code: 588000    # 科创50ETF
      - code: 159915    # 创业板ETF
      - code: 159949    # 创业板50ETF

  - name: 科技
    etfs:
      - code: 512760    # 芯片ETF
      - code: 512480    # 半导体ETF
      - code: 515050    # 5G ETF
      - code: 513180    # 恒生科技ETF
      - code: 513130    # 恒生科技ETF华泰柏瑞
      - code: 513050    # 中概互联ETF
      - code: 513330    # 恒生互联网ETF

  - name: 金融
    etfs:
      - code: 512880    # 证券ETF
      - code: 510230    # 金融ETF

  - name: 消费医药
    etfs:
      - code: 512010    # 医药ETF
      - code: 159928    # 消费ETF
      - code: 515680    # 消费ETF

  - name: 制造周期
    etfs:
      - code: 512660    # 军工ETF
      - code: 515030    # 新能源汽车ETF
      - code: 516160    # 新能源ETF
      - code: 515700    # 光伏ETF
      - code: 516970    # 基建ETF

  - name: 商品跨境
    etfs:
      - code: 518880    # 黄金ETF
      - code: 513100    # 纳指ETF
      - code: 513090    # 港股通ETF

settings:
  top_n: 10               # Phase 2 深度分析数量
  quick_kline_days: 60    # Phase 1 K 线天数
  phase2_timeout: 45      # 单只深度分析超时秒数
  min_amount: 10000000    # 最低成交额过滤（元）
  min_scale: 200000000    # 最低规模过滤（元）
  quick_score_weights:
    momentum: 30
    volume: 20
    capital_flow: 20
    shares_trend: 15
    iopv: 15
```

- [ ] **Step 2: Verify YAML parses correctly**

Run: `python3 -c "import yaml; d=yaml.safe_load(open('.claude/skills/stock-trend/scripts/watchlist.yaml')); print(len(d['categories']), 'categories,', sum(len(c['etfs']) for c in d['categories']), 'ETFs')"`
Expected: `6 categories, 28 ETFs`

---

### Task 2: Implement etf_scanner.py — Core Framework + Phase 1 (Quick Score)

**Files:**
- Create: `.claude/skills/stock-trend/scripts/etf_scanner.py`

**Logic:** The scanner is an orchestrator. Phase 1 runs three data fetchers per ETF (kline, capital flow, ETF data) via subprocess, then computes quick_score.

- [ ] **Step 1: Write the initial framework**

```python
#!/usr/bin/env python3
"""ETF Scanner — scan watchlist and rank A-share ETFs for daily trend analysis.

Usage:
    python3 etf_scanner.py [--top N] [--focus <category>] [--output compact|full]

Outputs JSON to stdout for Claude Code to render.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
DEFAULT_WATCHLIST = SKILL_DIR / "watchlist.yaml"
CACHE_DIR = PROJECT_ROOT / ".cache" / "stock-trend"


def code_to_ts_code(code: str) -> str:
    """Convert raw ETF code to ts_code for existing scripts."""
    code = code.strip()
    if code.startswith(("5", "15")):
        return f"{code}.SH"
    elif code.startswith("159"):
        return f"{code}.SZ"
    elif code.startswith(("51", "56", "58")):
        return f"{code}.SH"
    return f"{code}.SH"


def get_exchange(ts_code: str) -> str:
    """Derive exchange suffix from ts_code."""
    return ts_code.split(".")[-1]


def load_watchlist(path: Optional[Path] = None) -> dict:
    """Load ETF watchlist from YAML config."""
    path = path or DEFAULT_WATCHLIST
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
```

- [ ] **Step 2: Implement data fetching helpers**

```python
# Phase 1 helpers: call existing scripts via subprocess

def run_script(script_name: str, args: list[str], timeout: int = 30) -> Optional[dict]:
    """Run an existing stock-trend script and return parsed JSON output."""
    script_path = SKILL_DIR / script_name
    cmd = [sys.executable, str(script_path)] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def fetch_quick_kline(code: str, days: int = 60) -> Optional[list]:
    """Fetch K-line data for Phase 1 quick score via eastmoney."""
    ts_code = code_to_ts_code(code)
    # fetch_kline_eastmoney.py returns {"meta": {...}, "data": [...]}
    # but the script takes the raw code too - let me check how it handles it
    # Actually it takes ts_code as positional arg, and the code-to-suffix mapping is handled inside
    raw = run_script("fetch_kline_eastmoney.py", [ts_code], timeout=20)  # no --no-cache: rely on existing 5-min TTL
    if raw and raw.get("meta", {}).get("data_source") != "error":
        return raw.get("data", [])
    return None


def fetch_quick_capital_flow(code: str) -> Optional[dict]:
    """Fetch capital flow for Phase 1 (main force net flow)."""
    ts_code = code_to_ts_code(code)
    raw = run_script("fetch_capital_flow.py", [ts_code], timeout=20)
    if raw and raw.get("meta", {}).get("data_source") != "error":
        return raw
    return None


def fetch_quick_etf_data(code: str) -> Optional[dict]:
    """Fetch ETF-specific data for Phase 1 (scale, shares, IOPV)."""
    raw = run_script("fetch_etf_data.py", [code], timeout=20)
    if raw and raw.get("meta", {}).get("data_source") != "error":
        return raw
    return None
```

Note: The `run_script` calls above follow the pattern used in the existing codebase. Each script outputs JSON to stdout. `--no-cache` forces fresh data; remove it to use cache for subsequent calls.

- [ ] **Step 3: Implement quick_score sub-scores**

```python
# --- Quick Score Functions ---

def _ma(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def _rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_val = 50.0
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rsi_val = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return rsi_val


def _macd_direction(prices: list) -> float:
    """Return MACD histogram direction: positive=bullish, negative=bearish."""
    if len(prices) < 26:
        return 0.0
    ema12 = sum(prices[:12]) / 12
    ema26 = sum(prices[:26]) / 26
    alpha12, alpha26 = 2 / 13, 2 / 27
    for p in prices[12:]:
        ema12 = ema12 * (1 - alpha12) + p * alpha12
    for p in prices[26:]:
        ema26 = ema26 * (1 - alpha26) + p * alpha26
    return ema12 - ema26


def score_momentum(kline: list) -> float:
    """Score momentum dimension: MA trend + RSI + MACD. Returns 0-100."""
    closes = [r["close"] for r in kline]
    if len(closes) < 20:
        return 50.0

    ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
    rsi_val = _rsi(closes, 14)
    macd_val = _macd_direction(closes)

    score = 50.0
    # MA trend (0-40)
    if ma5 > ma20 > ma60:
        score += 30
    elif ma5 > ma20 and len(closes) >= 60 and ma20 > ma60:
        score += 20
    elif ma5 > ma20:
        score += 8
    elif ma5 < ma20 and ma20 < ma60:
        score -= 15
    elif ma5 < ma20:
        score -= 5
    # RSI (0-30)
    if 40 <= rsi_val <= 60:
        score += 15
    elif 30 <= rsi_val < 40 or 60 < rsi_val <= 70:
        score += 5
    elif rsi_val > 80 or rsi_val < 20:
        score -= 8
    # MACD direction (0-30)
    if macd_val > 0:
        score += 10
    else:
        score -= 5

    return max(0.0, min(100.0, score))


def score_volume(kline: list) -> float:
    """Score volume activity dimension. Returns 0-100."""
    if len(kline) < 10:
        return 50.0
    volumes = [r.get("volume", 0) or 0 for r in kline]
    recent_avg = sum(volumes[-5:]) / 5
    long_avg = sum(volumes) / len(volumes)
    ratio = recent_avg / long_avg if long_avg > 0 else 1.0

    score = 50.0
    if ratio > 1.5:
        score += 30
    elif ratio > 1.2:
        score += 15
    elif ratio < 0.6:
        score -= 20
    elif ratio < 0.8:
        score -= 10
    # Volume absolute check (use turnover if available)
    turnover = [r.get("amount", 0) or 0 for r in kline[-1:]]
    if turnover and turnover[0] > 1000000000:  # > 1B
        score += 10
    return max(0.0, min(100.0, score))


def score_capital_flow(flow_data: Optional[dict]) -> float:
    """Score capital flow from main force net flow. Returns 0-100."""
    if not flow_data:
        return 50.0
    data = flow_data.get("data", [])
    if not data:
        return 50.0
    # Sum main force net flow over available days
    net_flows = []
    for row in data:
        # East Money capital flow: f60 = main_force_net, f61 = main_force_net_pct
        if isinstance(row, dict):
            net_pct = row.get("main_force_net_pct") or row.get("f61")
            if net_pct is not None:
                net_flows.append(float(net_pct))
    if not net_flows:
        return 50.0
    avg_net = sum(net_flows) / len(net_flows)
    if avg_net > 2.0:
        return 85.0
    elif avg_net > 0.5:
        return 65.0
    elif avg_net > -0.5:
        return 40.0
    elif avg_net > -2.0:
        return 20.0
    return 10.0


def score_shares_trend(etf_data: Optional[dict]) -> float:
    """Score shares outstanding trend. Returns 0-100."""
    if not etf_data:
        return 50.0
    shares = etf_data.get("data", {}).get("shares_trend")
    if not shares or not isinstance(shares, dict):
        return 50.0
    # Expected format: {"1m_change": 5.2} (percent)
    change_1m = float(shares.get("1m_change", 0) or 0)
    if change_1m > 5:
        return 85.0
    elif change_1m > 1:
        return 65.0
    elif change_1m > -1:
        return 40.0
    elif change_1m > -5:
        return 20.0
    return 10.0


def score_iopv(etf_data: Optional[dict]) -> float:
    """Score IOPV discount/premium. Returns 0-100."""
    if not etf_data:
        return 50.0
    data = etf_data.get("data", {})
    iopv = data.get("iopv_premium")
    if iopv is None:
        return 50.0
    premium = float(iopv)
    if -0.5 < premium <= -0.1:
        return 85.0
    elif -0.1 < premium <= 0:
        return 65.0
    elif premium <= -0.5:
        return 40.0
    elif 0 < premium <= 0.3:
        return 30.0
    return 10.0
```

- [ ] **Step 4: Implement Phase 1 orchestration**

```python
# --- Phase 1: Quick Scan ---

def scan_single_etf(code: str, settings: dict) -> dict:
    """Run Phase 1 scan for a single ETF. Returns result dict or error."""
    result = {"code": code, "ts_code": code_to_ts_code(code), "error": None,
              "kline": None, "capital_flow": None, "etf_data": None}
    try:
        kline = fetch_quick_kline(code, settings.get("quick_kline_days", 60))
        if not kline or len(kline) < 10:
            result["error"] = "kline_insufficient"
            return result
        result["kline"] = kline
        result["capital_flow"] = fetch_quick_capital_flow(code)
        result["etf_data"] = fetch_quick_etf_data(code)
    except Exception as e:
        result["error"] = str(e)
    return result


def compute_quick_score(result: dict, weights: dict) -> dict:
    """Compute quick score for a single ETF result. Returns scored result."""
    if result.get("error") or not result.get("kline"):
        return {"code": result["code"], "ts_code": result["ts_code"],
                "quick_score": None, "error": result.get("error", "no_data")}

    kline = result["kline"]
    cap_flow = result.get("capital_flow")
    etf_data = result.get("etf_data")

    dims = {}
    dims["momentum"] = score_momentum(kline)
    dims["volume"] = score_volume(kline)
    dims["capital_flow"] = score_capital_flow(cap_flow)
    dims["shares_trend"] = score_shares_trend(etf_data)
    dims["iopv"] = score_iopv(etf_data)

    # Weighted sum with missing-dimension handling
    total_weight = 0
    weighted_score = 0.0
    for dim, w in weights.items():
        # Treat 50 (neutral) as "no signal" but still weight it
        weight = w
        weighted_score += dims.get(dim, 50) * weight
        total_weight += weight

    quick_score = round(weighted_score / total_weight, 1) if total_weight > 0 else None

    return {"code": result["code"], "ts_code": result["ts_code"],
            "quick_score": quick_score, "dimensions": dims}


def build_phase1_etf_list(watchlist: dict, focus: Optional[str] = None) -> list:
    """Build flat list of ETF codes from watchlist, optionally filtered by category."""
    etfs = []
    for cat in watchlist["categories"]:
        if focus and cat["name"] != focus:
            continue
        for etf in cat["etfs"]:
            etfs.append({"code": etf["code"], "category": cat["name"]})
    return etfs
```

- [ ] **Step 5: Implement Phase 1 runner**

```python
def run_phase1(watchlist: dict, settings: dict, focus: Optional[str] = None,
               max_workers: int = 8) -> tuple[list, list]:
    """Run Phase 1 quick scan on all ETFs.

    Returns (results_by_code, ranked_list).
    """
    etf_list = build_phase1_etf_list(watchlist, focus)
    weights = settings.get("quick_score_weights", {})

    # Fetch data in parallel
    raw_results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(scan_single_etf, e["code"], settings): e for e in etf_list}
        for fut in as_completed(fut_map):
            e = fut_map[fut]
            try:
                raw_results[e["code"]] = fut.result()
            except Exception as ex:
                raw_results[e["code"]] = {"code": e["code"], "ts_code": code_to_ts_code(e["code"]),
                                          "error": str(ex), "kline": None}

    # Compute quick scores and rank
    scored = []
    for e in etf_list:
        res = raw_results.get(e["code"], {})
        score_result = compute_quick_score(res, weights)
        score_result["category"] = e["category"]
        scored.append(score_result)

    # Filter valid (non-error) and sort by quick_score descending
    valid = [s for s in scored if s["quick_score"] is not None]
    valid.sort(key=lambda x: x["quick_score"], reverse=True)

    # Apply rank
    for i, s in enumerate(valid):
        s["rank"] = i + 1

    return raw_results, valid
```

- [ ] **Step 6: Test Phase 1 standalone**

```bash
cd .claude/skills/stock-trend/scripts
python3 -c "
import json
from etf_scanner import load_watchlist, run_phase1
wl = load_watchlist()
raw, ranked = run_phase1(wl, wl['settings'])
print(f'Total: {len(ranked)}')
for r in ranked[:5]:
    print(json.dumps(r, ensure_ascii=False))
"
```

Expected: Script runs without import errors, prints top 5 rankings.

---

### Task 3: Implement Phase 2 (Deep Analysis) + Phase 3 (Output)

- [ ] **Step 1: Implement Phase 2 — deep analysis for top N**

```python
def get_cached_pipeline_output(code: str) -> Optional[dict]:
    """Read existing pipeline_output.json from cache."""
    path = CACHE_DIR / code / "pipeline_output.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def get_cached_scores(code: str) -> Optional[dict]:
    """Read existing scores.json from cache."""
    path = CACHE_DIR / code / "scores.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def run_deep_analysis(code: str, settings: dict) -> dict:
    """Run full pipeline + scoring for one ETF. Returns deep score result."""
    result = {"code": code, "ts_code": code_to_ts_code(code)}

    pipeline_result = get_cached_pipeline_output(code)
    if pipeline_result:
        result["pipeline_source"] = "cache"
    else:
        result["pipeline_source"] = "fresh"
        pipeline_cmd = [sys.executable, str(SKILL_DIR / "run_pipeline.py"),
                        "--code", code]
        try:
            subprocess.run(pipeline_cmd, capture_output=True, text=True,
                         timeout=settings.get("phase2_timeout", 45))
        except subprocess.TimeoutExpired:
            result["error"] = "pipeline_timeout"
            return result

    # Run scoring
    scores_result = get_cached_scores(code)
    if not scores_result:
        scores_cmd = [sys.executable, str(SKILL_DIR / "compute_scores.py"),
                      "--code", code]
        try:
            subprocess.run(scores_cmd, capture_output=True, text=True,
                         timeout=30)
            scores_result = get_cached_scores(code)
        except subprocess.TimeoutExpired:
            result["error"] = "scores_timeout"
            return result

    if scores_result:
        result["deep_score"] = scores_result.get("summary", {}).get("total_score")
        result["verdict"] = scores_result.get("summary", {}).get("trend")
        result["confidence"] = scores_result.get("summary", {}).get("confidence")

        # Extract dimension scores
        dims = scores_result.get("dimensions", {})
        result["dimension_scores"] = {
            "technical": dims.get("technical"),
            "capital_flow": dims.get("capital_flow"),
            "fundamental": dims.get("fundamental"),
            "sentiment": dims.get("sentiment"),
            "macro": dims.get("macro"),
        }
        result["risks"] = scores_result.get("summary", {}).get("risks", [])
        result["stop_loss"] = scores_result.get("summary", {}).get("stop_loss")
        result["targets"] = {
            "conservative": scores_result.get("summary", {}).get("target_conservative"),
            "moderate": scores_result.get("summary", {}).get("target_moderate"),
        }

    return result


def run_phase2(top_candidates: list, settings: dict, max_workers: int = 4) -> list:
    """Run deep analysis on top N ETF codes in parallel."""
    codes = [c["code"] for c in top_candidates]
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(run_deep_analysis, code, settings): code
                   for code in codes}
        for fut in as_completed(fut_map):
            code = fut_map[fut]
            try:
                results[code] = fut.result()
            except Exception as e:
                results[code] = {"code": code, "error": str(e)}
    return results
```

- [ ] **Step 2: Implement Phase 3 — combine results and output JSON**

```python
# --- Phase 3: Aggregate Output ---

def build_combined_ranking(phase1_ranked: list, phase2_results: dict,
                           settings: dict) -> list:
    """Merge Phase 1 and Phase 2 results into combined ranking."""
    combined = []
    for p1 in phase1_ranked:
        code = p1["code"]
        p2 = phase2_results.get(code, {})
        entry = {
            "code": code,
            "ts_code": p1["ts_code"],
            "name": p2.get("name", ""),
            "category": p1.get("category", ""),
            "quick_score": p1["quick_score"],
            "deep_score": p2.get("deep_score"),
            "verdict": p2.get("verdict"),
            "confidence": p2.get("confidence"),
            "dimensions": p1.get("dimensions", {}),
            "deep_dimensions": p2.get("dimension_scores", {}),
            "risks": p2.get("risks", []),
            "stop_loss": p2.get("stop_loss"),
            "targets": p2.get("targets", {}),
        }

        # Combined score: weighted average if deep exists
        if entry["deep_score"] is not None:
            entry["combined_score"] = round(
                0.3 * entry["quick_score"] + 0.7 * entry["deep_score"], 1
            )
        else:
            entry["combined_score"] = entry["quick_score"]
        combined.append(entry)

    # Sort by combined_score descending
    combined.sort(key=lambda x: x["combined_score"] or 0, reverse=True)
    for i, c in enumerate(combined):
        c["rank"] = i + 1
        c["stars"] = 3 if c["combined_score"] and c["combined_score"] >= 80 else \
                     2 if c["combined_score"] and c["combined_score"] >= 65 else \
                     1 if c["combined_score"] and c["combined_score"] >= 50 else 0

    return combined


def build_top_picks(combined: list) -> list:
    """Extract top picks with brief logic."""
    picks = combined[:5]
    result = []
    for p in picks:
        logic_parts = []
        dims = p.get("dimensions", {})
        if dims.get("momentum", 0) >= 70:
            logic_parts.append("动量强势")
        elif dims.get("momentum", 0) >= 55:
            logic_parts.append("动量偏强")
        if dims.get("capital_flow", 0) >= 65:
            logic_parts.append("主力资金流入")
        if dims.get("shares_trend", 0) >= 65:
            logic_parts.append("份额持续增长")
        if dims.get("iopv", 0) >= 65:
            logic_parts.append("折价安全边际")
        if not logic_parts:
            logic_parts.append("综合评分居前")
        result.append({
            "code": p["code"],
            "name": p["name"],
            "combined_score": p["combined_score"],
            "logic": "，".join(logic_parts),
        })
    return result


def build_excluded(scored_all: list) -> list:
    """Build list of low-score ETFs with reasons."""
    excluded = []
    for s in scored_all:
        if s["quick_score"] is not None and s["quick_score"] < 40:
            reasons = []
            dims = s.get("dimensions", {})
            if dims.get("momentum", 50) < 40:
                reasons.append("动量弱")
            if dims.get("capital_flow", 50) < 30:
                reasons.append("资金流出")
            if dims.get("shares_trend", 50) < 30:
                reasons.append("份额缩水")
            if dims.get("volume", 50) < 30:
                reasons.append("量能不足")
            excluded.append({
                "code": s["code"],
                "name": s.get("name", ""),
                "quick_score": s["quick_score"],
                "reason": " ".join(reasons) if reasons else "综合评分偏低",
            })
    return excluded


def build_sector_summary(combined: list) -> dict:
    """Build sector-level strength summary."""
    from collections import defaultdict
    sector_scores = defaultdict(list)
    for c in combined:
        cat = c.get("category", "其他")
        sector_scores[cat].append(c.get("combined_score") or 0)

    strong, weak = [], []
    for sector, scores in sector_scores.items():
        avg = sum(scores) / len(scores)
        if avg >= 70:
            strong.append({"name": sector, "avg_score": round(avg, 1)})
        elif avg < 50:
            weak.append({"name": sector, "avg_score": round(avg, 1)})

    return {"strong": sorted(strong, key=lambda x: x["avg_score"], reverse=True),
            "weak": sorted(weak, key=lambda x: x["avg_score"])}


def build_output(watchlist: dict, phase1_ranked: list, phase2_results: dict,
                 settings: dict, args: argparse.Namespace, elapsed: float) -> dict:
    """Build final JSON output."""
    combined = build_combined_ranking(phase1_ranked, phase2_results, settings)

    # Count valid ETFs
    valid_count = len(phase1_ranked)
    total_count = sum(len(c["etfs"]) for c in watchlist["categories"])

    output = {
        "meta": {
            "scan_time": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "total_etfs": total_count,
            "valid_etfs": valid_count,
            "duration_seconds": round(elapsed, 1),
            "market_state": "closed",  # simplified
        },
        "combined_ranking": combined,
        "top_picks": build_top_picks(combined),
        "excluded": build_excluded(phase1_ranked),
        "sector_summary": build_sector_summary(combined),
    }

    return output
```

- [ ] **Step 3: Implement main() entry point**

```python
def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="ETF Scanner — scan watchlist and rank A-share ETFs")
    parser.add_argument("--top", type=int, default=None,
                        help="Number of ETFs for deep analysis (default: from config)")
    parser.add_argument("--focus", type=str, default=None,
                        help="Scan only specific category (e.g. 宽基指数, 科技)")
    parser.add_argument("--output", choices=["compact", "full"], default="full",
                        help="Output format")
    parser.add_argument("--watchlist", type=str, default=None,
                        help="Custom watchlist path")
    parser.add_argument("--no-deep", action="store_true",
                        help="Skip Phase 2 deep analysis")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None):
    """ETF Scanner main entry point."""
    args = parse_args(argv)
    start = time.time()

    # Load config
    watchlist = load_watchlist(Path(args.watchlist) if args.watchlist else None)
    settings = watchlist.get("settings", {})

    # Apply CLI overrides
    if args.top is not None:
        settings["top_n"] = args.top

    # Phase 1
    raw_results, phase1_ranked = run_phase1(watchlist, settings, args.focus)

    # Phase 2
    phase2_results = {}
    if not args.no_deep and phase1_ranked:
        top_n = settings.get("top_n", 10)
        top_candidates = phase1_ranked[:top_n]
        phase2_results = run_phase2(top_candidates, settings)

    # Phase 3
    elapsed = time.time() - start
    output = build_output(watchlist, phase1_ranked, phase2_results, settings, args, elapsed)

    # Print JSON
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
```

Important: The `.cache/stock-trend/<code>/` directories store pipeline outputs indexed by the raw ETF code (not ts_code). The `run_pipeline.py --code <code>` handles resolution internally. The scores.json is written to `.cache/stock-trend/<code>/scores.json` by `compute_scores.py`. The get_cached_* functions read these files directly.

- [ ] **Step 4: Verify etf_scanner.py imports cleanly**

```bash
cd .claude/skills/stock-trend/scripts
python3 -c "from etf_scanner import load_watchlist, code_to_ts_code, score_momentum; print('imports OK')"
```

Expected: `imports OK`

- [ ] **Step 5: Quick test with --help**

```bash
cd .claude/skills/stock-trend/scripts
python3 etf_scanner.py --help
```

Expected: Prints usage with --top, --focus, --output, --watchlist, --no-deep options.

---

### Task 4: Error Handling & Edge Cases

- [ ] **Step 1: Add robust error handling to Phase 1**

Add to `scan_single_etf()`:
- Handle network timeouts (each subprocess has its own timeout)
- Handle empty kline data (len < 10 → skip)
- Handle missing capital_flow or etf_data (→ None, neutral score)
- Handle subprocess crashing (return None → neutral score)
- Handle YAML parse errors in watchlist

The implementation in Task 2 already handles most of these. Verify by checking:

```python
# Key error handling paths (already in code):
# 1. run_script() catches TimeoutExpired, JSONDecodeError, FileNotFoundError → None
# 2. scan_single_etf() catches Exception → {"error": str(e)}
# 3. compute_quick_score() handles None data → neutral score 50
# 4. Missing kline → {"error": "kline_insufficient"}
# 5. ThreadPoolExecutor exceptions caught in run_phase1()
```

- [ ] **Step 2: Handle edge cases in Phase 1 results**

```python
def apply_filters(etf_list: list, raw_results: dict, settings: dict) -> list:
    """Filter out ETFs that don't meet minimum criteria."""
    filtered = []
    for e in etf_list:
        code = e["code"]
        raw = raw_results.get(code, {})
        kline = raw.get("kline")
        if raw.get("error") == "kline_insufficient":
            continue
        # Volume filter
        if kline and len(kline) > 5:
            recent_volumes = [r.get("amount", 0) or 0 for r in kline[-5:]]
            avg_volume = sum(recent_volumes) / len(recent_volumes)
            if avg_volume < settings.get("min_amount", 10000000):
                continue
        filtered.append(e)
    return filtered
```

Insert this filter after data fetch in `run_phase1()`:

```python
# In run_phase1(), after fetching all data:
# Filter by min criteria
etf_list = apply_filters(etf_list, raw_results, settings)
```

---

### Task 5: Write Tests

**Files:**
- Create: `.claude/skills/stock-trend/tests/test_etf_scanner.py`

- [ ] **Step 1: Write unit tests for quick_score functions**

```python
"""Tests for ETF Scanner quick_score functions."""
import sys
import json
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from etf_scanner import (
    code_to_ts_code, score_momentum, score_volume,
    score_capital_flow, score_shares_trend, score_iopv,
    compute_quick_score, build_combined_ranking,
)


def test_code_to_ts_code_shanghai():
    """5xxxxx codes map to .SH"""
    assert code_to_ts_code("513180") == "513180.SH"
    assert code_to_ts_code("510050") == "510050.SH"
    assert code_to_ts_code("588000") == "588000.SH"


def test_code_to_ts_code_shenzhen():
    """159xxx codes map to .SZ"""
    assert code_to_ts_code("159915") == "159915.SZ"
    assert code_to_ts_code("159949") == "159949.SZ"


def test_score_momentum_bullish():
    """Bullish kline (prices uptrend) should score > 60"""
    kline = []
    price = 100.0
    for i in range(80):
        price += 1.0 + (i % 3) * 0.5  # steady uptrend
        kline.append({"close": price, "volume": 1000000, "amount": price * 1000000})
    s = score_momentum(kline)
    assert s > 60, f"Expected >60, got {s}"


def test_score_momentum_bearish():
    """Bearish kline (prices downtrend) should score < 40"""
    kline = []
    price = 100.0
    for i in range(80):
        price -= 1.0 + (i % 3) * 0.5  # steady downtrend
        kline.append({"close": max(price, 10), "volume": 1000000, "amount": 1000000})
    s = score_momentum(kline)
    assert s < 45, f"Expected <45, got {s}"


def test_score_momentum_insufficient_data():
    """Fewer than 20 klines should return neutral 50"""
    kline = [{"close": 100.0, "volume": 1000, "amount": 100000}] * 10
    s = score_momentum(kline)
    assert s == 50.0


def test_score_volume_high():
    """High volume ratio should score high"""
    kline = ([{"close": 100, "volume": 1000000, "amount": 100000000}] * 55 +
             [{"close": 101, "volume": 2000000, "amount": 200000000}] * 5)
    s = score_volume(kline)
    assert s > 60, f"Expected >60, got {s}"


def test_score_capital_flow_positive():
    """Positive main force net flow should score > 50"""
    data = {
        "data": [
            {"f61": 3.5},  # main_force_net_pct
            {"f61": 2.1},
            {"f61": 1.8},
        ]
    }
    s = score_capital_flow(data)
    assert s > 60, f"Expected >60, got {s}"


def test_score_capital_flow_none():
    """Missing capital flow data should return neutral 50"""
    assert score_capital_flow(None) == 50.0


def test_score_shares_trend_growth():
    """Positive shares growth should score > 60"""
    data = {"data": {"shares_trend": {"1m_change": 8.5}}}
    s = score_shares_trend(data)
    assert s > 70, f"Expected >70, got {s}"


def test_score_iopv_discount():
    """Moderate discount should score high"""
    data = {"data": {"iopv_premium": -0.3}}
    s = score_iopv(data)
    assert s > 60, f"Expected >60, got {s}"


def test_score_iopv_premium():
    """Premium should score low"""
    data = {"data": {"iopv_premium": 0.8}}
    s = score_iopv(data)
    assert s < 40, f"Expected <40, got {s}"


def test_compute_quick_score_normal():
    """Normal case: weights + kline = numeric score"""
    kline = [{"close": 100 + i, "volume": 1000000, "amount": 100000000}
             for i in range(80)]
    result = {"code": "513180", "ts_code": "513180.SH",
              "kline": kline, "capital_flow": None, "etf_data": None}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    assert scored["quick_score"] is not None
    assert 0 <= scored["quick_score"] <= 100


def test_compute_quick_score_no_kline():
    """No kline data should return None quick_score"""
    result = {"code": "513180", "ts_code": "513180.SH",
              "error": "kline_insufficient"}
    weights = {"momentum": 30, "volume": 20, "capital_flow": 20,
               "shares_trend": 15, "iopv": 15}
    scored = compute_quick_score(result, weights)
    assert scored["quick_score"] is None


def test_build_combined_ranking():
    """Combined ranking with deep scores should weight correctly"""
    p1 = [{"code": "A", "quick_score": 80, "dimensions": {}, "category": "科技"},
          {"code": "B", "quick_score": 60, "dimensions": {}, "category": "宽基"}]
    p2 = {"A": {"deep_score": 90, "verdict": "up", "confidence": "high",
                 "name": "ETF_A", "risks": []},
          "B": {"deep_score": 50, "verdict": "neutral", "confidence": "low",
                 "name": "ETF_B", "risks": []}}
    combined = build_combined_ranking(p1, p2, {})
    assert combined[0]["code"] == "A"  # higher combined score
    assert combined[0]["combined_score"] == round(0.3 * 80 + 0.7 * 90, 1)
    assert combined[1]["code"] == "B"
```

- [ ] **Step 2: Run unit tests**

```bash
cd .claude/skills/stock-trend
python3 -m pytest tests/test_etf_scanner.py -v 2>&1 | head -40
```

Expected: All tests pass.

- [ ] **Step 3: Add integration test (runs on first 3 real ETFs)**

```python
def test_phase1_real_etfs():
    """Integration test: run Phase 1 on first 3 ETFs from watchlist."""
    from etf_scanner import load_watchlist, run_phase1, build_output
    import tempfile

    # Minimal 3-ETF watchlist
    test_wl = {
        "categories": [{"name": "测试", "etfs": [{"code": "510050"}, {"code": "512880"}, {"code": "513180"}]}],
        "settings": {
            "top_n": 3,
            "quick_kline_days": 60,
            "phase2_timeout": 45,
            "min_amount": 0,  # no filter for test
            "min_scale": 0,
            "quick_score_weights": {"momentum": 30, "volume": 20, "capital_flow": 20, "shares_trend": 15, "iopv": 15}
        }
    }

    raw, ranked = run_phase1(test_wl, test_wl["settings"])
    assert len(ranked) <= 3
    assert len(ranked) >= 1  # at least 1 ETF has data
    for r in ranked:
        assert r["quick_score"] is not None
        assert 0 <= r["quick_score"] <= 100
        print(f"  {r['code']}: quick_score={r['quick_score']} dims={r['dimensions']}")
```

- [ ] **Step 4: Run integration test**

```bash
cd .claude/skills/stock-trend
python3 -m pytest tests/test_etf_scanner.py::test_phase1_real_etfs -v -s 2>&1
```

Expected: 1+ ETFs score between 0-100 (may take 30-60s for API calls on first run).

- [ ] **Step 5: Commit test file**

```bash
git add .claude/skills/stock-trend/tests/test_etf_scanner.py
git commit -m "test: add ETF scanner unit and integration tests"
```

---

### Task 6: Register /etf-scan in SKILL.md

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: Add etf_scanner.py to allowed-tools**

In the `allowed-tools` section of SKILL.md frontmatter, add:

```yaml
  - Bash(python3 .claude/skills/stock-trend/scripts/etf_scanner.py *)
```

Place it after the existing `fetch_kline_eastmoney.py` line (line 14), maintaining alphabetical-ish order.

- [ ] **Step 2: Add /etf-scan command documentation**

Add after the existing command block (after the "## Commands" section or similar, before "## Step 1"):

```markdown
### /etf-scan [--top N] [--focus <板块>] [--output compact|full]

扫描精选 ETF 池，输出当日趋势排名和投资建议。

参数：
- `--top N`    深度分析 ETF 数量，默认 10
- `--focus <板块>`  只扫描指定板块：宽基指数、科技、金融、消费医药、制造周期、商品跨境
- `--output compact|full`  输出简版/完整版，默认 full

执行流程：
1. 配置文件读取：`.claude/skills/stock-trend/scripts/watchlist.yaml`
2. Phase 1 快速扫描：对所有 ETF 并行获取 K 线、资金流向和 ETF 数据，计算速评分
3. Phase 1 筛选：排除流动性不足和规模过小的 ETF，按速评分排名取 Top N
4. Phase 2 深度分析：对 Top N 分别运行完整管线 + 评分（复用 `/stock-trend` 的 run_pipeline.py + compute_scores.py）
5. Phase 3 聚合输出：合并排名、Top Picks 投资逻辑、排除原因、板块强弱总结

```bash
# 全量扫描
python3 .claude/skills/stock-trend/scripts/etf_scanner.py

# 只扫描科技板块，深度分析 5 只
python3 .claude/skills/stock-trend/scripts/etf_scanner.py --top 5 --focus 科技

# 简版输出（仅排名，无详细逻辑）
python3 .claude/skills/stock-trend/scripts/etf_scanner.py --output compact
```

输出为 JSON，Claude Code 解析后按模板在对话中呈现排名表 + 投资逻辑。
```

- [ ] **Step 3: Commit SKILL.md changes**

```bash
git add .claude/skills/stock-trend/SKILL.md
git commit -m "feat: register /etf-scan command in SKILL.md"
```

---

### Task 7: End-to-End Verification

- [ ] **Step 1: Run full test suite**

```bash
cd .claude/skills/stock-trend
python3 scripts/test_stock_trend.py
```

Expected: All existing tests pass (0 failures). This ensures no regression.

- [ ] **Step 2: Run golden diff test**

```bash
python3 tests/test_golden.py --diff
```

Expected: No unexpected golden diffs. If there are, the scanner implementation didn't affect existing pipeline data.

- [ ] **Step 3: Test /etf-scan with --no-deep (Phase 1 only)**

```bash
cd .claude/skills/stock-trend/scripts
python3 etf_scanner.py --no-deep --top 3 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('Valid ETFs:', d['meta']['valid_etfs'])
print('Top 3:')
for r in d['combined_ranking'][:3]:
    print(f\"  #{r['rank']} {r['code']} score={r['combined_score']}\")
print('Excluded:', len(d['excluded']), 'ETFs')
"
```

Expected: JSON parsed successfully, shows 3 ranked ETFs with scores.

- [ ] **Step 4: Run full scan (Phase 1 + Phase 2 on top 3)**

```bash
cd .claude/skills/stock-trend/scripts
python3 etf_scanner.py --top 3 2>&1 | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('Duration:', d['meta']['duration_seconds'], 's')
for r in d['combined_ranking'][:3]:
    ds = r.get('deep_score')
    print(f\"#{r['rank']} {r['code']} quick={r['quick_score']} deep={ds} verdict={r.get('verdict')}\")
print('Top picks:', [p['code'] for p in d['top_picks']])
print('Strong sectors:', [s['name'] for s in d['sector_summary']['strong']])
"
```

Expected: Full scan completes, shows both quick and deep scores.
Note: First run may take 2-5 minutes due to cold cache. Subsequent runs with cache are much faster.

- [ ] **Step 5: Commit all remaining files**

```bash
git add .claude/skills/stock-trend/scripts/etf_scanner.py .claude/skills/stock-trend/scripts/watchlist.yaml
git commit -m "feat: implement ETF scanner with two-phase scan and ranking"
```

---

## Tasks Not Covered (YAGNI)

The following are intentionally excluded from this plan:
- Historical scan result tracking (compare today vs yesterday) — out of scope
- Scheduled/cron execution — user chose on-demand via slash command
- Web UI for scan results — output is in Claude conversation
- Email/push notification — out of scope
- Alert for specific ETF conditions (e.g., "send alert if 513180 enters overbought") — out of scope
