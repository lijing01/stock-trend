# 同花顺数据集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate 同花顺 DDX/超级资金 and 龙虎榜 data to enhance longtou leader scoring and sentiment analysis.

**Architecture:** Add two optional enhancement layers between Phase 2 and Phase 3 of the existing pipeline. DDX data rescales leader scoring (30% weight). 龙虎榜 data injects institutional/retail signals into sentiment dimension. Both degrade gracefully on failure.

**Tech Stack:** Python 3, urllib (no new dependencies), regex-based HTML parsing for 同花顺 public pages.

---

### Task 1: fetch_ddx.py — DDX & 超级资金 fetching

**Files:**
- Create: `.claude/skills/stock-trend/scripts/fetch_ddx.py`
- Test: `tests/test_longtou.py` (Task 6)

- [ ] **Step 1: Write `fetch_ddx_data()` function signature and docstring**

```python
#!/usr/bin/env python3
"""Fetch DDX/DDY/DDZ and super-large order ratio from 同花顺 DDE ranking data.

Public functions:
    fetch_ddx_data(codes: list[str]) -> dict[str, dict]
    compute_ddx_score(ddx: dict) -> float
    compute_super_order_score(ddx: dict) -> float

Usage:
    ddx_map = fetch_ddx_data(["002415", "600519"])
    score = compute_ddx_score(ddx_map.get("002415", {}))
"""

import re
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any

# 同花顺 DDE 排行页面 — shows DDX/DDY/DDZ for actively traded stocks
THS_DDX_URL = "http://data.10jqka.com.cn/financial/ddx/opendata/"

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://data.10jqka.com.cn/financial/ddx/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FETCH_TIMEOUT = 10  # seconds per request


def fetch_ddx_data(codes: list[str]) -> dict[str, dict]:
    """Fetch DDX/DDY/DDZ/super_order_ratio for given stock codes from 同花顺.

    Args:
        codes: list of 6-digit A-share stock codes.

    Returns:
        Dict mapping code -> {ddx, ddx_days, ddy, ddz, super_order_ratio, fetch_time}.
        Only codes found in the DDE ranking page are included.
        Returns empty dict on any fetch/parse failure (graceful degradation).
    """
    # TODO: implement
    pass


def compute_ddx_score(ddx_data: dict) -> float:
    """Compute DDX score (0-100) for leader scoring weight.

    Anchors:
        ddx >= 0.5 + ddx_days >= 3  -> 100  (持续资金布局)
        ddx >= 0.5                   ->  90
        ddx >= 0.2                   ->  80
        0 < ddx < 0.2                ->  interpolated 50-80
        ddx <= 0                     ->  max(0, 50 + ddx * 100)

    Args:
        ddx_data: dict with ddx (float), ddx_days (int).

    Returns:
        Score 0-100.
    """
    # TODO: implement
    pass


def compute_super_order_score(ddx_data: dict) -> float:
    """Compute super-large order ratio score (0-100).

    Anchors:
        ratio >= 15%  -> 100  (机构主导)
        ratio >= 8%   ->  80
        ratio >= 5%   ->  60
        ratio < 5%    ->  50  (散户特征)

    Args:
        ddx_data: dict with super_order_ratio (float, 0-1 or 0-100).

    Returns:
        Score 0-100.
    """
    # TODO: implement
    pass
```

- [ ] **Step 2: Write tests for score computation (no network)**

Add to `.claude/skills/stock-trend/tests/test_longtou.py`:

```python
def test_ddx_score_computation():
    """Test compute_ddx_score() and compute_super_order_score()."""
    from fetch_ddx import compute_ddx_score, compute_super_order_score

    # Strong DDX consecutive
    test("DDX-01: ddx>=0.5 + ddx_days>=3 → 100",
         compute_ddx_score({"ddx": 0.6, "ddx_days": 5}) == 100)

    # High DDX without consecutive
    test("DDX-02: ddx>=0.5 alone → 90",
         compute_ddx_score({"ddx": 0.5, "ddx_days": 1}) == 90)

    # Moderate DDX
    test("DDX-03: ddx=0.2 → 80",
         compute_ddx_score({"ddx": 0.2, "ddx_days": 0}) == 80)

    # Slightly positive DDX
    score = compute_ddx_score({"ddx": 0.1, "ddx_days": 0})
    test("DDX-04: ddx=0.1 interpolated 50-80",
         50 < score < 80, f"score={score}")

    # Zero DDX
    test("DDX-05: ddx=0 → 50",
         compute_ddx_score({"ddx": 0, "ddx_days": 0}) == 50)

    # Negative DDX
    test("DDX-06: ddx=-0.3 → max(0,20)=20",
         compute_ddx_score({"ddx": -0.3, "ddx_days": 0}) == 20)

    # Strong negative DDX
    test("DDX-07: ddx=-0.6 → clamped to 0",
         compute_ddx_score({"ddx": -0.6, "ddx_days": 0}) == 0)

    # Empty input
    test("DDX-08: empty dict → 50",
         compute_ddx_score({}) == 50)

    # Super order ratio tests
    test("DSO-01: ratio>=15% → 100",
         compute_super_order_score({"super_order_ratio": 0.15}) == 100)
    test("DSO-02: ratio>=8% → 80",
         compute_super_order_score({"super_order_ratio": 0.08}) == 80)
    test("DSO-03: ratio=6% → 60",
         compute_super_order_score({"super_order_ratio": 0.06}) == 60)
    test("DSO-04: ratio=3% → 50",
         compute_super_order_score({"super_order_ratio": 0.03}) == 50)
    test("DSO-05: ratio=25% → 100",
         compute_super_order_score({"super_order_ratio": 0.25}) == 100)
    test("DSO-06: empty → 50",
         compute_super_order_score({}) == 50)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "DDX\|DSO"`
Expected: FAIL for all (function not defined/returns None)

- [ ] **Step 4: Implement `fetch_ddx_data()`**

```python
def _fetch_page(url: str, timeout: int = FETCH_TIMEOUT) -> str | None:
    """Fetch HTML page with timeout, return text or None."""
    try:
        req = urllib.request.Request(url, headers=THS_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_ddx_table(html: str) -> dict[str, dict]:
    """Parse 同花顺 DDE ranking HTML table.

    Looks for a table containing DDX data rows with columns:
    代码, 名称, DDX, DDY, DDZ, 连续红柱, 超级资金占比.

    Returns dict mapping 6-digit code -> ddx data dict.
    """
    records = {}

    # Pattern: match table rows with numeric stock codes
    # Typical row: <tr><td>1</td><td>002415</td><td>海康威视</td><td>0.873</td>...
    # Simpler approach: find all <tr> blocks and extract cells
    row_pattern = re.compile(
        r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL
    )
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
    code_pattern = re.compile(r'^\d{6}$')

    for tr_match in row_pattern.finditer(html):
        cells = cell_pattern.findall(tr_match.group(1))
        # Need at least 8 cells for full DDX data
        if len(cells) < 8:
            continue
        code = re.sub(r'<[^>]+>', '', cells[1]).strip()
        if not code_pattern.match(code):
            continue

        def _clean(v: str) -> str:
            return re.sub(r'<[^>]+>', '', v).strip().replace(',', '')

        try:
            ddx_str = _clean(cells[3]) if len(cells) > 3 else "0"
            ddy_str = _clean(cells[4]) if len(cells) > 4 else "0"
            ddz_str = _clean(cells[5]) if len(cells) > 5 else "0"
            days_str = _clean(cells[6]) if len(cells) > 6 else "0"
            super_str = _clean(cells[7]) if len(cells) > 7 else "0%"

            ddx = float(ddx_str) if ddx_str and ddx_str != "--" else 0.0
            ddy = float(ddy_str) if ddy_str and ddy_str != "--" else 0.0
            ddz = float(ddz_str) if ddz_str and ddz_str != "--" else 0.0
            ddx_days = int(re.search(r'\d+', days_str).group()) if re.search(r'\d+', days_str) else 0
            # super_order_ratio: "12.34%" or "12.34" -> 0.1234
            super_match = re.search(r'([\d.]+)', super_str)
            super_ratio = float(super_match.group(1)) / 100 if super_match else 0.0

            records[code] = {
                "ddx": round(ddx, 4),
                "ddy": round(ddy, 4),
                "ddz": round(ddz, 2),
                "ddx_days": ddx_days,
                "super_order_ratio": round(super_ratio, 4),
                "fetch_time": datetime.now().strftime("%Y%m%d-%H%M%S"),
            }
        except (ValueError, IndexError):
            continue

    return records


def fetch_ddx_data(codes: list[str]) -> dict[str, dict]:
    """Fetch DDX data for given codes from 同花顺 DDE ranking page.

    Since the ranking page shows top stocks by DDX, we fetch it and
    extract data only for codes in our candidate list.
    """
    if not codes:
        return {}
    html = _fetch_page(THS_DDX_URL)
    if html is None:
        return {}
    all_records = _parse_ddx_table(html)
    # Filter to only our target codes
    code_set = set(codes)
    return {k: v for k, v in all_records.items() if k in code_set}
```

- [ ] **Step 5: Implement score computation functions**

```python
def compute_ddx_score(ddx_data: dict) -> float:
    """Compute DDX score (0-100) for leader scoring."""
    ddx = ddx_data.get("ddx")
    if ddx is None:
        return 50.0  # neutral default

    ddx_days = ddx_data.get("ddx_days", 0) or 0

    if ddx >= 0.5 and ddx_days >= 3:
        return 100.0
    if ddx >= 0.5:
        return 90.0
    if ddx >= 0.2:
        return 80.0
    if ddx >= 0:
        # Linear interpolation: 0->50, 0.2->80
        return round(50.0 + (ddx / 0.2) * 30, 1)
    # ddx < 0
    return max(0.0, round(50.0 + ddx * 100, 1))


def compute_super_order_score(ddx_data: dict) -> float:
    """Compute super-large order ratio score (0-100)."""
    ratio = ddx_data.get("super_order_ratio")
    if ratio is None:
        return 50.0  # neutral default

    # Normalize: if ratio is already 0-100 (percentage), convert to 0-1
    if ratio > 1:
        ratio = ratio / 100.0

    if ratio >= 0.15:
        return 100.0
    if ratio >= 0.08:
        return 80.0
    if ratio >= 0.05:
        return 60.0
    return 50.0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "DDX\|DSO"`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_ddx.py
git add .claude/skills/stock-trend/tests/test_longtou.py
git commit -m "feat: add DDX data fetching and scoring functions

New module fetch_ddx.py for 同花顺 DDE ranking data.
Includes DDX/DDY/DDZ/super-order-ratio parsing and score computation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: fetch_longhubang.py — 龙虎榜 fetching

**Files:**
- Create: `.claude/skills/stock-trend/scripts/fetch_longhubang.py`
- Test: `tests/test_longtou.py` (Task 6)

- [ ] **Step 1: Write `fetch_longhubang_data()` function signature**

```python
#!/usr/bin/env python3
"""Fetch 龙虎榜 (Dragon & Tiger Board) data from 同花顺.

Public functions:
    fetch_longhubang_data(codes: list[str]) -> dict[str, dict]

Usage:
    lhb_map = fetch_longhubang_data(["002415", "600519"])
"""

import re
import urllib.request
from datetime import datetime
from typing import Any

# 同花顺 龙虎榜 page
THS_LHB_URL = "http://data.10jqka.com.cn/financial/longhubang/"

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://data.10jqka.com.cn/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

FETCH_TIMEOUT = 15  # seconds


def fetch_longhubang_data(codes: list[str]) -> dict[str, dict]:
    """Fetch 龙虎榜 data for given stock codes from 同花顺.

    Args:
        codes: list of 6-digit A-share stock codes.

    Returns:
        Dict mapping code -> {
            is_on_board: bool,
            net_buy_total: float (元),
            buy_seats: list[{"name": str, "amount": float, "type": str}],
            sell_seats: list[{"name": str, "amount": float, "type": str}],
            has_institution_buy: bool,
            has_institution_sell: bool,
            has_floating_capital: bool,
            floating_capital_net_buy: bool,
            retail_dominated: bool,
            risk_level: "low" | "medium" | "high",
        }.
        Returns empty dict on any fetch/parse failure.
    """
    # TODO: implement
    pass
```

- [ ] **Step 2: Write tests for 龙虎榜 data**

Add the following test functions to test_longtou.py:

```python
def test_longhubang_risk_analysis():
    """Test 龙虎榜 risk level classification."""
    from fetch_longhubang import _classify_risk_level

    # Institution heavy buying -> low risk
    inst_buy = {
        "has_institution_buy": True,
        "has_institution_sell": False,
        "retail_dominated": False,
        "has_floating_capital": False,
    }
    test("LHB-01: 机构净买入→low风险",
         _classify_risk_level(inst_buy) == "low",
         f"risk={_classify_risk_level(inst_buy)}")

    # Retail dominated -> high risk
    retail = {
        "has_institution_buy": False,
        "has_institution_sell": False,
        "retail_dominated": True,
        "has_floating_capital": False,
    }
    test("LHB-02: 散户主导→high风险",
         _classify_risk_level(retail) == "high",
         f"risk={_classify_risk_level(retail)}")

    # Mixed: institution buy + 游资 sell -> medium
    mixed = {
        "has_institution_buy": True,
        "has_institution_sell": True,
        "retail_dominated": False,
        "has_floating_capital": True,
        "floating_capital_net_buy": False,
    }
    test("LHB-03: 机构+游资分歧→medium风险",
         _classify_risk_level(mixed) == "medium",
         f"risk={_classify_risk_level(mixed)}")

    # Pure 游资 (no institution) -> medium
    youzi = {
        "has_institution_buy": False,
        "has_institution_sell": False,
        "retail_dominated": False,
        "has_floating_capital": True,
        "floating_capital_net_buy": True,
    }
    test("LHB-04: 纯游资→medium风险",
         _classify_risk_level(youzi) == "medium",
         f"risk={_classify_risk_level(youzi)}")

    # No board data -> low (default)
    test("LHB-05: 未上榜→low风险",
         _classify_risk_level({}) == "low")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "LHB-"`
Expected: FAIL for all

- [ ] **Step 4: Implement fetch_longhubang.py**

```python
def _fetch_page(url: str, timeout: int = FETCH_TIMEOUT) -> str | None:
    """Fetch HTML page with timeout."""
    try:
        req = urllib.request.Request(url, headers=THS_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_lhb_table(html: str) -> dict[str, dict]:
    """Parse 同花顺 龙虎榜 HTML table.

    Extracts: stock code, total net buy/sell, institution/游资 participation.
    """
    records = {}
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
    code_pattern = re.compile(r'^\d{6}$')

    for tr_match in row_pattern.finditer(html):
        cells = cell_pattern.findall(tr_match.group(1))
        if len(cells) < 6:
            continue
        # Cell 1 typically contains stock code in a link
        code_html = cells[1] if len(cells) > 1 else ""
        code_match = re.search(r'(\d{6})', code_html)
        if not code_match:
            continue
        code = code_match.group(1)

        def _clean(v: str) -> str:
            return re.sub(r'<[^>]+>', '', v).strip().replace(',', '')

        # Parse net buy total (元)
        net_raw = _clean(cells[4]) if len(cells) > 4 else "0"
        net = _parse_amount(net_raw)

        # Determine institution/游资 participation
        detail_raw = _clean(cells[5]) if len(cells) > 5 else ""
        has_inst_buy = "机构买入" in detail_raw or "机构专用" in html[cells[5].find("买入"):cells[5].find("买入")+50] if "买入" in cells[5] else False
        has_inst_sell = "机构卖出" in detail_raw or "机构专用" in html[cells[5].find("卖出"):cells[5].find("卖出")+50] if "卖出" in cells[5] else False
        has_youzi = "游资" in detail_raw or "拉萨" in detail_raw or "宁波" in detail_raw

        records[code] = {
            "is_on_board": True,
            "net_buy_total": net,
            "buy_seats": [],
            "sell_seats": [],
            "has_institution_buy": has_inst_buy,
            "has_institution_sell": has_inst_sell,
            "has_floating_capital": has_youzi,
            "floating_capital_net_buy": net > 0 if has_youzi else False,
            "retail_dominated": False,
            "risk_level": "low",
        }

    return records


def _parse_amount(val: str) -> float:
    """Parse amount string to yuan. Handles 万/亿 suffixes."""
    if not val:
        return 0.0
    val = val.strip().replace(',', '')
    if '亿' in val:
        num = re.search(r'([\d.]+)', val)
        return float(num.group(1)) * 1e8 if num else 0.0
    if '万' in val:
        num = re.search(r'([\d.]+)', val)
        return float(num.group(1)) * 1e4 if num else 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0


def _classify_risk_level(lhb_data: dict) -> str:
    """Classify risk level based on 龙虎榜 seat composition.

    Returns: "low", "medium", or "high".
    """
    if not lhb_data.get("is_on_board"):
        return "low"

    has_inst_buy = lhb_data.get("has_institution_buy", False)
    has_inst_sell = lhb_data.get("has_institution_sell", False)
    retail = lhb_data.get("retail_dominated", False)
    youzi = lhb_data.get("has_floating_capital", False)
    youzi_net_buy = lhb_data.get("floating_capital_net_buy", False)

    # 散户主导买入 → high risk (接盘)
    if retail:
        return "high"

    # 机构净买入 ≥ 2家 + no 游资 selling → low risk
    if has_inst_buy and not has_inst_sell and not youzi:
        return "low"

    # 机构净卖出 → high risk
    if has_inst_sell and not has_inst_buy:
        return "high"

    # 机构+游资同时出现 → medium (分歧)
    if has_inst_buy and has_inst_sell:
        return "medium"
    if has_inst_buy and youzi:
        return "medium"
    if youzi:
        return "medium"

    return "low"


def fetch_longhubang_data(codes: list[str]) -> dict[str, dict]:
    """Fetch 龙虎榜 data for given codes."""
    if not codes:
        return {}
    html = _fetch_page(THS_LHB_URL)
    if html is None:
        return {}

    records = _parse_lhb_table(html)
    code_set = set(codes)

    result = {}
    for code, data in records.items():
        if code in code_set:
            data["risk_level"] = _classify_risk_level(data)
            result[code] = data

    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "LHB-"`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_longhubang.py
git add .claude/skills/stock-trend/tests/test_longtou.py
git commit -m "feat: add 龙虎榜 data fetching and risk classification

New module fetch_longhubang.py for 同花顺 龙虎榜 data.
Includes seat parsing, institution/游资 detection, risk level classification.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Add DDX rescoring to fetch_sector_data.py

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/fetch_sector_data.py` (append after `filter_core_stocks()`)
- Test: `tests/test_longtou.py` (Task 6)

- [ ] **Step 1: Write test for rescore_leaders_with_ddx()**

Add to `test_longtou.py`:

```python
def test_rescore_leaders_with_ddx():
    """Test rescore_leaders_with_ddx() DDX-enhanced leader scoring."""
    from fetch_sector_data import rescore_leaders_with_ddx

    stocks = [
        {"code": "600001", "name": "高DDX龙头", "change_pct": 9.5, "amount": 5e8},
        {"code": "600002", "name": "低DDX龙头", "change_pct": 7.2, "amount": 3e8},
        {"code": "600003", "name": "负DDX跟风", "change_pct": 6.0, "amount": 2e8},
    ]

    ddx_data = {
        "600001": {"ddx": 0.8, "ddx_days": 5, "super_order_ratio": 0.18},
        "600002": {"ddx": 0.1, "ddx_days": 1, "super_order_ratio": 0.04},
        "600003": {"ddx": -0.4, "ddx_days": 0, "super_order_ratio": 0.02},
    }

    rescored = rescore_leaders_with_ddx(stocks, ddx_data)
    test("RS-01: 高DDX股排首位", rescored[0]["code"] == "600001",
         f"top={rescored[0]['name']} score={rescored[0]['leader_score']}")

    # Despite lowest change_pct, 负DDX should be last
    test("RS-02: 负DDX排最后", rescored[-1]["code"] == "600003",
         f"last={rescored[-1]['name']} score={rescored[-1]['leader_score']}")

    # Without DDX data, rescore should keep existing order
    no_ddx = rescore_leaders_with_ddx(stocks, {})
    test("RS-03: 无DDX数据保持排序", no_ddx[0]["code"] == "600001",
         f"top={no_ddx[0]['name']}")

    # Empty list
    empty = rescore_leaders_with_ddx([], {"600001": {}})
    test("RS-04: 空列表不抛异常", len(empty) == 0)

    # Partial DDX coverage
    partial = rescore_leaders_with_ddx(stocks[:2], {"600001": ddx_data["600001"]})
    test("RS-05: 部分DDX覆盖正常工作", len(partial) == 2,
         f"count={len(partial)}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "RS-"`
Expected: FAIL (function not defined)

- [ ] **Step 3: Implement `rescore_leaders_with_ddx()` in fetch_sector_data.py**

After `filter_core_stocks()` (line ~345), add:

```python
def rescore_leaders_with_ddx(leaders: list[dict],
                              ddx_data: dict[str, dict]) -> list[dict]:
    """Re-score leader stocks with DDX data enhancement.

    Uses new formula: change*30% + amount*20% + ddx_score*30% + super_order_score*20%.
    Stocks without DDX data keep their existing leader_score.

    Args:
        leaders: list of stock dicts with leader_score.
        ddx_data: dict mapping code -> {ddx, ddx_days, super_order_ratio, ...}.

    Returns:
        Re-sorted leaders list with updated leader_score.
    """
    if not leaders:
        return []

    # Lazy import to avoid circular dependency at module level
    from fetch_ddx import compute_ddx_score, compute_super_order_score

    for s in leaders:
        ddx = ddx_data.get(s["code"])
        if ddx:
            ddx_raw = ddx.get("ddx")
            ddx_days = ddx.get("ddx_days", 0) or 0

            change_score = min(100, max(0, 50 + (s.get("change_pct") or 0) * 5))
            amount_score = min(100, _parse_amount(s.get("amount")) / 1e7)
            ddx_s = compute_ddx_score(ddx)
            super_s = compute_super_order_score(ddx)

            s["leader_score"] = round(
                change_score * 0.30 + amount_score * 0.20
                + ddx_s * 0.30 + super_s * 0.20,
                1,
            )
            s["ddx_data"] = ddx  # attach for report reference

    leaders.sort(key=lambda x: x.get("leader_score", 0), reverse=True)
    return leaders
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v 2>&1 | grep "RS-"`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/fetch_sector_data.py
git add .claude/skills/stock-trend/tests/test_longtou.py
git commit -m "feat: add DDX-enhanced leader rescoring

rescore_leaders_with_ddx() in fetch_sector_data.py uses
DDX*30% + super_order*20% weights alongside price/volume.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Modify compute_scores.py — 龙虎榜 sentiment injection

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/compute_scores.py`

- [ ] **Step 1: Understand current sentiment scoring**

The existing automated sentiment scoring (lines 860-915) already reads `capital_flow_data` for northbound/margin signals. The 龙虎榜 data will live in its own file `longhubang.json` under the code's cache directory.

- [ ] **Step 2: Add 龙虎榜 loading + sentiment adjustment logic**

After the existing automated sentiment scoring block (after line 915), insert:

```python
    # ── 龙虎榜 sentiment adjustment ──────────────────────────────────
    # Reads longhubang.json from data_dir when available
    if data_dir and args.sentiment_score is None:
        lhb_data, _ = find_data_file(data_dir, "longhubang.json")
        if lhb_data and isinstance(lhb_data, dict):
            lhb_adjustment = _compute_lhb_sentiment(lhb_data)
            if lhb_adjustment != 0:
                scores["sentiment"] = round(scores["sentiment"] + lhb_adjustment, 2)
                scores["sentiment"] = max(-3, min(3, scores["sentiment"]))
                automated_sources["sentiment_longhubang"] = lhb_adjustment
```

And near the end of the file (before `main()`), add the helper function:

```python
def _compute_lhb_sentiment(lhb_data: dict) -> float:
    """Compute sentiment adjustment from 龙虎榜 data.

    Returns adjustment in [-1.0, +0.8] range on sentiment score scale.

    Adjustments from design spec:
        机构净买入 ≥ 2家                    → +0.8
        机构净卖出 ≥ 2家                    → -1.0
        纯游资主导, 无机构                   → -0.3
        散户主导买入                         → -1.0
        游资净买入 + 机构净卖出              → -0.5 (分歧)
        上榜但机构交易额 < 20%               → -0.3
    """
    adjustment = 0.0

    if not lhb_data.get("is_on_board"):
        return 0.0

    # Count signals (multiple may apply, sum them)
    has_inst_buy = lhb_data.get("has_institution_buy", False)
    has_inst_sell = lhb_data.get("has_institution_sell", False)
    retail = lhb_data.get("retail_dominated", False)
    youzi = lhb_data.get("has_floating_capital", False)
    risk_level = lhb_data.get("risk_level", "low")

    # 散户主导 → worst signal
    if retail:
        adjustment -= 1.0

    # 机构净卖出 → strong negative
    if has_inst_sell and not has_inst_buy:
        adjustment -= 1.0

    # 机构净买入 → strong positive
    if has_inst_buy and not has_inst_sell:
        adjustment += 0.8

    # 分歧: 机构+游资 or 机构买+卖同时
    if has_inst_buy and has_inst_sell:
        adjustment -= 0.5
    if has_inst_buy and youzi:
        adjustment -= 0.3

    # 纯游资, 无机构
    if youzi and not has_inst_buy and not has_inst_sell:
        adjustment -= 0.3

    # Clamp and round
    adjustment = max(-1.0, min(0.8, adjustment))
    return round(adjustment, 2)
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/stock-trend/scripts/compute_scores.py
git commit -m "feat: add 龙虎榜 sentiment injection to compute_scores.py

Auto-loads longhubang.json from data_dir when available.
Adjusts sentiment score based on institution/游资/retail composition.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Modify market_leader.py — DDX + 龙虎榜 orchestration

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/market_leader.py`

- [ ] **Step 1: Understand the orchestration flow in `main()`**

Current flow (lines ~505-525):
1. Phase 2: `sectors_analyzed = run_phase2(hot_sectors)`
2. Collect candidates
3. Phase 3: `pipeline_results = run_phase3(candidates, roles)`

New insertion point: after step 1, before step 2 (enhanced candidates).

- [ ] **Step 2: Insert DDX enhancement after Phase 2**

After `sectors_analyzed = run_phase2(...)` (line 506) and before the candidate collection block (line 509), add:

```python
    # ── DDX Enhancement: fetch DDX data and rescore leaders ──
    try:
        from fetch_ddx import fetch_ddx_data
        from fetch_sector_data import rescore_leaders_with_ddx

        # Collect all candidate codes
        all_codes = list(dict.fromkeys(
            s["code"]
            for sec in sectors_analyzed
            for s in sec.get("leaders", []) + sec.get("core_stocks", [])
        ))

        if all_codes:
            print(f"[DDX] Fetching DDX data for {len(all_codes)} candidates...")
            ddx_data = fetch_ddx_data(all_codes)
            if ddx_data:
                print(f"  Found DDX data for {len(ddx_data)} stocks")
                # Rescore leaders per sector
                for sec in sectors_analyzed:
                    leaders = sec.get("leaders", [])
                    if leaders:
                        rescored = rescore_leaders_with_ddx(leaders, ddx_data)
                        sec["leaders"] = rescored
                        sec["has_ddx_enhanced"] = True
                        # Log downgraded leaders
                        for s in rescored:
                            ddx = ddx_data.get(s["code"], {})
                            if ddx.get("ddx", 0) < 0:
                                print(f"    ⚠ {s['name']}({s['code']}) DDX={ddx.get('ddx')}")
            else:
                print("  No DDX data available (degraded)")
    except Exception as e:
        print(f"  [DDX] Enhancement skipped: {e}")
```

- [ ] **Step 3: Insert 龙虎榜 enhancement after DDX rescoring**

After the DDX block and before Phase 3, add:

```python
    # ── 龙虎榜 Enhancement: fetch and cache per-stock ──
    lhb_data_global = {}
    try:
        from fetch_longhubang import fetch_longhubang_data

        # Collect final candidate codes (after DDX rescoring)
        all_codes = list(dict.fromkeys(
            s["code"]
            for sec in sectors_analyzed
            for s in sec.get("leaders", []) + sec.get("core_stocks", [])
        ))

        if all_codes:
            print(f"[LHB] Fetching 龙虎榜 data for {len(all_codes)} candidates...")
            lhb_data_global = fetch_longhubang_data(all_codes)
            if lhb_data_global:
                print(f"  Found 龙虎榜 data for {len(lhb_data_global)} stocks")
                # Write per-stock longhubang.json to cache dir (for compute_scores.py)
                for code, lhb in lhb_data_global.items():
                    code_cache = CACHE_DIR / code
                    code_cache.mkdir(parents=True, exist_ok=True)
                    (code_cache / "longhubang.json").write_text(
                        json.dumps(lhb, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                # Attach risk tips
                for code, lhb in lhb_data_global.items():
                    if lhb.get("risk_level") == "high":
                        name = ""
                        for sec in sectors_analyzed:
                            for s in sec.get("leaders", []) + sec.get("core_stocks", []):
                                if s["code"] == code:
                                    name = s.get("name", "")
                                    break
                        tip = f"{name}({code}): 龙虎榜风险 — 散户主导买入"
                        if tip not in output["risk_tips"]:
                            output["risk_tips"].append(tip)
            else:
                print("  No 龙虎榜 data available (degraded)")
    except Exception as e:
        print(f"  [LHB] Enhancement skipped: {e}")
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v`
Expected: All existing tests PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py -v`
Expected: All unit tests pass, network tests may be skipped

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/stock-trend/scripts/market_leader.py
git commit -m "feat: add DDX + 龙虎榜 orchestration to market_leader.py

DDX enhancement: fetch after Phase 2, rescore leaders, demote negative-DDX.
龙虎榜 enhancement: fetch for final candidates, write per-stock cache,
inject risk tips for retail-dominated boards.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropie.com>"
```

---

### Task 6: DDX parse + 龙虎榜 parse + degradation tests

**Files:**
- Modify: `.claude/skills/stock-trend/tests/test_longtou.py`

- [ ] **Step 1: Write DDX HTML parse test**

```python
def test_ddx_html_parse():
    """Test _parse_ddx_table() with mock HTML."""
    from fetch_ddx import _parse_ddx_table

    mock_html = """<html><body>
    <table>
        <tr><td>1</td><td><a href="/stock/002415/">002415</a></td><td>海康威视</td><td>0.873</td><td>0.234</td><td>18.50</td><td>5天</td><td>12.34%</td></tr>
        <tr><td>2</td><td><a href="/stock/600519/">600519</a></td><td>贵州茅台</td><td>0.120</td><td>0.050</td><td>5.20</td><td>2天</td><td>3.50%</td></tr>
        <tr><td>3</td><td><a href="/stock/000001/">000001</a></td><td>平安银行</td><td>-0.250</td><td>-0.100</td><td>-2.80</td><td>--</td><td>1.20%</td></tr>
    </table>
    </body></html>"""

    result = _parse_ddx_table(mock_html)

    test("DP-01: 解析3只股票", len(result) == 3, f"count={len(result)}")

    test("DP-02: 002415 DDX=0.873",
         result.get("002415", {}).get("ddx") == 0.873)
    test("DP-03: 002415 DDX天数=5",
         result.get("002415", {}).get("ddx_days") == 5)
    test("DP-04: 002415 超级资金占比=0.1234",
         abs(result.get("002415", {}).get("super_order_ratio", 0) - 0.1234) < 0.001)

    test("DP-05: 600519 DDX=0.120",
         result.get("600519", {}).get("ddx") == 0.120)
    test("DP-06: 600519 超级资金占比=0.035",
         abs(result.get("600519", {}).get("super_order_ratio", 0) - 0.035) < 0.001)

    test("DP-07: 000001 DDX=-0.250",
         result.get("000001", {}).get("ddx") == -0.250)
    test("DP-08: 000001 DDX天数=0(缺失)",
         result.get("000001", {}).get("ddx_days") == 0)

    # Empty HTML
    test("DP-09: 空HTML返回空", len(_parse_ddx_table("")) == 0)

    # No table
    test("DP-10: 无表格返回空", len(_parse_ddx_table("<html></html>")) == 0)
```

- [ ] **Step 2: Write 龙虎榜 HTML parse test**

```python
def test_longhubang_html_parse():
    """Test _parse_lhb_table() with mock HTML."""
    from fetch_longhubang import _parse_lhb_table

    mock_html = """<html><body>
    <table>
        <tr><td>2026-05-27</td><td><a href="/stock/002415/">002415</a></td><td>海康威视</td><td>5000万</td><td>2000万</td><td>3000万</td><td>机构买入</td></tr>
        <tr><td>2026-05-27</td><td><a href="/stock/600519/">600519</a></td><td>贵州茅台</td><td>1.2亿</td><td>0.8亿</td><td>0.4亿</td><td>游资主导</td></tr>
        <tr><td>2026-05-27</td><td><a href="/stock/000001/">000001</a></td><td>平安银行</td><td>300万</td><td>5000万</td><td>-4700万</td><td>散户接盘</td></tr>
    </table>
    </body></html>"""

    result = _parse_lhb_table(mock_html)

    test("LHP-01: 解析3只股票", len(result) == 3, f"count={len(result)}")

    test("LHP-02: 002415 在榜", result.get("002415", {}).get("is_on_board") == True)
    test("LHP-03: 002415 机构买入",
         result.get("002415", {}).get("has_institution_buy") == True)

    test("LHP-04: 600519 游资参与",
         result.get("600519", {}).get("has_floating_capital") == True)

    test("LHP-05: 000001 净买入为负",
         result.get("000001", {}).get("net_buy_total", 0) < 0)

    # Empty HTML
    test("LHP-06: 空HTML返回空", len(_parse_lhb_table("")) == 0)
```

- [ ] **Step 3: Write degradation test**

```python
def test_ddx_degradation():
    """Test graceful degradation when DDX fetch fails."""
    from fetch_ddx import fetch_ddx_data, compute_ddx_score, compute_super_order_score

    # Simulate empty return (fetch failure)
    result = fetch_ddx_data.__wrapped__ if hasattr(fetch_ddx_data, "__wrapped__") else fetch_ddx_data
    # Test with empty codes
    empty = fetch_ddx_data([])
    test("DG-01: 空列表返回空", len(empty) == 0)

    # Score computation with empty data should return neutral scores
    test("DG-02: 空数据DDX分=50", compute_ddx_score({}) == 50)
    test("DG-03: 空数据超级资金分=50", compute_super_order_score({}) == 50)


def test_longhubang_degradation():
    """Test graceful degradation when 龙虎榜 fetch fails."""
    from fetch_longhubang import fetch_longhubang_data, _classify_risk_level

    empty = fetch_longhubang_data([])
    test("LHG-01: 空列表返回空", len(empty) == 0)

    # Risk classification with empty data -> low
    test("LHG-02: 空数据风险=low", _classify_risk_level({}) == "low")
```

- [ ] **Step 4: Wire all new tests into main()**

Add to the test suite's `main()` function, after existing test groups:

```python
    if not args.unit_only:
        # ... existing network tests ...

        print("\n📡 DDX 数据源测试")
        print("=" * 40)
        # (DDX network test would go here if we add one)

        print("\n📡 龙虎榜数据源测试")
        print("=" * 40)
        # (龙虎榜 network test would go here)

    # Always-run new tests
    print("\n📐 DDX评分计算测试")
    print("=" * 40)
    test_ddx_score_computation()

    print("\n🏆 DDX龙头重评分测试")
    print("=" * 40)
    test_rescore_leaders_with_ddx()
    test_ddx_degradation()

    print("\n📋 龙虎榜解析测试")
    print("=" * 40)
    test_longhubang_risk_analysis()
    test_longhubang_html_parse()

    print("\n🌐 龙虎榜HTML解析测试")
    print("=" * 40)
    test_ddx_html_parse()

    print("\n🛡️ 降级测试")
    print("=" * 40)
    test_ddx_degradation()
    test_longhubang_degradation()
```

- [ ] **Step 5: Run all tests to verify**

Run: `python3 .claude/skills/stock-trend/tests/test_longtou.py --unit-only -v`
Expected: All tests PASS (count ~88 + new ~30 = ~118)

- [ ] **Step 6: Run stock-trend test suite**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
Expected: All PASS (no regression)

- [ ] **Step 7: Run golden test**

Run: `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
Expected: No diff or manageable golden changes

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/stock-trend/tests/test_longtou.py
git commit -m "test: add DDX/龙虎榜 parsing, rescoring, and degradation tests

30+ new tests covering: DDX score computation, HTML parsing,
leader rescoring, 龙虎榜 risk classification, degradation paths.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
