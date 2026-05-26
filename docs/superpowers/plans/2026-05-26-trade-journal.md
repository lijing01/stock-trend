# 交易日志与复盘系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a trade journaling system that records trades, detects behavioral error patterns, tracks AI recommendation accuracy, and generates weekly review reports — forming a closed-loop improvement cycle.

**Architecture:** Single `trade_journal.py` script with subcommands (`add`, `close`, `list`, `stats`, `save-ai-rec`, `backfill-outcomes`, `review`). JSON file storage under `data/trade_journal/`. Integration hooks into `portfolio_manager.py` (auto-close on remove) and stock-trend skill (auto-save AI recommendations). Error pattern detection via `TradeAnalyzer` class.

**Tech Stack:** Python 3 stdlib only (`json`, `datetime`, `argparse`, `pathlib`). No external dependencies.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/trade_journal.py` | **Create** | Main script: all subcommands, TradeAnalyzer class |
| `data/trade_journal/trades.json` | **Create (runtime)** | Trade records storage |
| `data/trade_journal/ai_recommendations.json` | **Create (runtime)** | AI recommendation snapshots |
| `data/trade_journal/reviews/` | **Create (runtime)** | Weekly/monthly review reports |
| `tests/test_trade_journal.py` | **Create** | Unit tests for all subcommands + error pattern detection |
| `scripts/portfolio_manager.py` | **Modify:~714-719** | Add auto-close hook in `cmd_remove()` |
| `SKILL.md` | **Modify** | Register `/trade` and `/trade-review` commands + allowed-tools |

---

## Phase 1: Core Functionality

### Task 1: Data Layer & Storage Helpers

**Files:**
- Create: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Test: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test for data helpers**

Create `tests/test_trade_journal.py`:

```python
#!/usr/bin/env python3
"""Tests for trade_journal.py"""
import sys
import json
import os
import tempfile
from pathlib import Path
from datetime import date

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Test result tracking
PASSED = 0
FAILED = 0
RESULTS = []


def test(name, condition, detail="", category="trade_journal"):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        status = "PASS"
    else:
        FAILED += 1
        status = "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail, "category": category})
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def run_data_layer_tests():
    """Test data loading/saving."""
    print("\n📁 数据层测试 (Data Layer)")
    print("=" * 50)

    from trade_journal import load_trades, save_trades, generate_trade_id

    # Use temp directory for isolation
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"

    # TJ-01: Load from non-existent file returns empty list
    data = load_trades(trades_path)
    test("TJ-01: load empty returns []", data == [], f"got {data}")

    # TJ-02: Save and reload preserves data
    records = [{"id": "T20260526-001", "code": "513180", "direction": "buy"}]
    save_trades(records, trades_path)
    loaded = load_trades(trades_path)
    test("TJ-02: save/load roundtrip", loaded == records, f"got {loaded}")

    # TJ-03: generate_trade_id format
    tid = generate_trade_id("2026-05-26", trades_path)
    test("TJ-03: trade id format", tid.startswith("T20260526-"), f"got {tid}")

    # TJ-04: generate_trade_id increments
    records = [{"id": "T20260526-001"}, {"id": "T20260526-002"}]
    save_trades(records, trades_path)
    tid = generate_trade_id("2026-05-26", trades_path)
    test("TJ-04: trade id increments", tid == "T20260526-003", f"got {tid}")


if __name__ == "__main__":
    run_data_layer_tests()
    print(f"\n{'='*50}")
    print(f"Total: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED > 0 else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'trade_journal'`

- [ ] **Step 3: Implement data layer**

Create `scripts/trade_journal.py`:

```python
#!/usr/bin/env python3
"""Trade journal — record trades, detect patterns, track AI accuracy, generate reviews.

Usage:
    python3 trade_journal.py add --code <code> --direction <buy|sell> --price <price> --qty <qty> --reason <reason> [--stop-loss <price>] [--target <price>] [--expected-days <n>] [--note <text>]
    python3 trade_journal.py close --id <trade_id> --price <close_price> [--note <text>]
    python3 trade_journal.py auto-close --code <code> --close-price <price> [--close-date <date>]
    python3 trade_journal.py list [--open] [--code <code>]
    python3 trade_journal.py stats
    python3 trade_journal.py save-ai-rec --code <code> --source <source> --direction <dir> --score <n> --dimensions <json> [--stop-loss <price>] [--targets <json>] [--report-path <path>]
    python3 trade_journal.py backfill-outcomes
    python3 trade_journal.py review [--period weekly|monthly]

Outputs JSON to stdout.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data" / "trade_journal"
TRADES_PATH = Path(os.environ.get("TRADE_JOURNAL_TRADES", str(DATA_DIR / "trades.json")))
AI_REC_PATH = Path(os.environ.get("TRADE_JOURNAL_AI_REC", str(DATA_DIR / "ai_recommendations.json")))
REVIEWS_DIR = DATA_DIR / "reviews"


# ── Data helpers ──────────────────────────────────────────────────────────


def load_trades(path: Optional[Path] = None) -> list:
    """Load trades from JSON file. Returns empty list if missing."""
    p = path or TRADES_PATH
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_trades(records: list, path: Optional[Path] = None):
    """Write trades list to JSON file."""
    p = path or TRADES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def load_ai_recs(path: Optional[Path] = None) -> list:
    """Load AI recommendations from JSON file."""
    p = path or AI_REC_PATH
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ai_recs(records: list, path: Optional[Path] = None):
    """Write AI recommendations to JSON file."""
    p = path or AI_REC_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def generate_trade_id(trade_date: str, path: Optional[Path] = None) -> str:
    """Generate next trade ID for a given date, e.g. T20260526-003."""
    p = path or TRADES_PATH
    existing = load_trades(p)
    date_prefix = "T" + trade_date.replace("-", "")
    same_day = [t for t in existing if t.get("id", "").startswith(date_prefix)]
    seq = len(same_day) + 1
    return f"{date_prefix}-{seq:03d}"


def generate_ai_rec_id(rec_date: str, path: Optional[Path] = None) -> str:
    """Generate next AI recommendation ID for a given date."""
    p = path or AI_REC_PATH
    existing = load_ai_recs(p)
    date_prefix = "AI" + rec_date.replace("-", "")
    same_day = [r for r in existing if r.get("id", "").startswith(date_prefix)]
    seq = len(same_day) + 1
    return f"{date_prefix}-{seq:03d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): add data layer with load/save/id-generation"
```

---

### Task 2: `add` Subcommand

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test for `add`**

Append to `test_trade_journal.py`:

```python
def run_add_tests():
    """Test add subcommand."""
    print("\n➕ 添加交易测试 (Add)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)

    # TJ-10: Basic add
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"),
           "add", "--code", "513180", "--direction", "buy",
           "--price", "0.850", "--qty", "10000", "--reason", "抄底",
           "--stop-loss", "0.780", "--target", "0.950"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-10: add exit code 0", result.returncode == 0, f"rc={result.returncode}")

    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-11: add returns trade_id", "trade_id" in output, f"keys={list(output.keys())}")
    test("TJ-12: add trade_id format", output.get("trade_id", "").startswith("T"), f"id={output.get('trade_id')}")

    # TJ-13: trade persisted
    trades = json.loads(trades_path.read_text()) if trades_path.exists() else []
    test("TJ-13: trade persisted", len(trades) == 1, f"count={len(trades)}")

    if trades:
        t = trades[0]
        test("TJ-14: code correct", t["code"] == "513180")
        test("TJ-15: direction correct", t["direction"] == "buy")
        test("TJ-16: price correct", t["price"] == 0.850)
        test("TJ-17: qty correct", t["qty"] == 10000)
        test("TJ-18: reason correct", t["reason"] == "抄底")
        test("TJ-19: stop_loss correct", t["stop_loss"] == 0.780)
        test("TJ-20: target correct", t["target"] == 0.950)
        test("TJ-21: status is open", t["status"] == "open")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: FAIL on TJ-10 (no `add` subcommand yet)

- [ ] **Step 3: Implement `add` subcommand**

Append to `trade_journal.py`:

```python
# ── Subcommands ───────────────────────────────────────────────────────────


def cmd_add(args):
    """Record a new trade."""
    trade_date = args.date or date.today().isoformat()
    trade_id = generate_trade_id(trade_date)

    record = {
        "id": trade_id,
        "code": args.code,
        "direction": args.direction,
        "price": float(args.price),
        "qty": int(args.qty),
        "date": trade_date,
        "reason": args.reason,
        "expected_days": int(args.expected_days) if args.expected_days else None,
        "stop_loss": float(args.stop_loss) if args.stop_loss else None,
        "target": float(args.target) if args.target else None,
        "note": args.note or "",
        "status": "open",
        "tags": [],
    }

    trades = load_trades()
    trades.append(record)
    save_trades(trades)

    result = {"status": "ok", "trade_id": trade_id, "message": f"已记录交易 {trade_id}"}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# ── CLI ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Trade journal")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--direction", required=True, choices=["buy", "sell"])
    p_add.add_argument("--price", required=True)
    p_add.add_argument("--qty", required=True)
    p_add.add_argument("--reason", required=True)
    p_add.add_argument("--date", default=None)
    p_add.add_argument("--expected-days", default=None)
    p_add.add_argument("--stop-loss", default=None)
    p_add.add_argument("--target", default=None)
    p_add.add_argument("--note", default=None)

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: All TJ-10 through TJ-21 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement add subcommand"
```

---

### Task 3: `close` Subcommand

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test for `close`**

Append to `test_trade_journal.py`:

```python
def run_close_tests():
    """Test close subcommand."""
    print("\n🔒 平仓测试 (Close)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)

    # Seed a trade
    seed = [{
        "id": "T20260526-001", "code": "513180", "direction": "buy",
        "price": 0.850, "qty": 10000, "date": "2026-05-26",
        "reason": "抄底", "status": "open", "stop_loss": 0.780,
        "target": 0.950, "tags": []
    }]
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(seed, ensure_ascii=False))

    # TJ-30: Close by ID
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"),
           "close", "--id", "T20260526-001", "--price", "0.920"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-30: close exit code 0", result.returncode == 0, f"rc={result.returncode}")

    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-31: close returns ok", output.get("status") == "ok")

    # TJ-32: Check persisted close data
    trades = json.loads(trades_path.read_text())
    t = trades[0]
    test("TJ-32: status closed", t["status"] == "closed")
    test("TJ-33: close_price set", t.get("close_price") == 0.920)
    test("TJ-34: pnl_pct calculated", abs(t.get("pnl_pct", 0) - 8.24) < 0.1, f"got {t.get('pnl_pct')}")
    test("TJ-35: pnl_amount calculated", abs(t.get("pnl") - 700.0) < 1, f"got {t.get('pnl')}")
    test("TJ-36: hold_days calculated", t.get("hold_days") is not None)

    # TJ-37: Close non-existent trade
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"),
           "close", "--id", "T99999999-999", "--price", "1.0"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = json.loads(result.stdout) if result.stdout else {}
    test("TJ-37: close unknown returns error", "error" in output)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: FAIL on TJ-30

- [ ] **Step 3: Implement `close` subcommand**

Add to `trade_journal.py`:

```python
def cmd_close(args):
    """Close an open trade by ID."""
    trades = load_trades()
    trade = next((t for t in trades if t["id"] == args.id), None)

    if not trade:
        json.dump({"error": f"未找到交易 {args.id}"}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    if trade["status"] != "open":
        json.dump({"error": f"交易 {args.id} 已平仓"}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    close_price = float(args.price)
    close_date = args.date or date.today().isoformat()
    buy_price = trade["price"]
    qty = trade["qty"]

    pnl = round((close_price - buy_price) * qty, 2)
    pnl_pct = round((close_price - buy_price) / buy_price * 100, 2)

    open_date = datetime.strptime(trade["date"], "%Y-%m-%d").date()
    close_dt = datetime.strptime(close_date, "%Y-%m-%d").date()
    hold_days = (close_dt - open_date).days

    trade["status"] = "closed"
    trade["close_price"] = close_price
    trade["close_date"] = close_date
    trade["pnl"] = pnl
    trade["pnl_pct"] = pnl_pct
    trade["hold_days"] = hold_days
    if args.note:
        trade["close_note"] = args.note

    save_trades(trades)

    result = {"status": "ok", "message": f"已平仓 {args.id}", "pnl": pnl, "pnl_pct": pnl_pct, "hold_days": hold_days}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
```

Add parser in `main()`:

```python
    # close
    p_close = sub.add_parser("close")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--price", required=True)
    p_close.add_argument("--date", default=None)
    p_close.add_argument("--note", default=None)
```

Add dispatch:

```python
    elif args.command == "close":
        cmd_close(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-30 through TJ-37 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement close subcommand with P&L calc"
```

---

### Task 4: `auto-close` Subcommand (Portfolio Integration)

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test for `auto-close`**

```python
def run_auto_close_tests():
    """Test auto-close (triggered by portfolio remove)."""
    print("\n🔗 自动平仓测试 (Auto-Close)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)

    # Seed two trades for same code (one closed, one open)
    seed = [
        {"id": "T20260520-001", "code": "513180", "direction": "buy",
         "price": 0.800, "qty": 5000, "date": "2026-05-20",
         "reason": "定投", "status": "closed", "tags": []},
        {"id": "T20260525-001", "code": "513180", "direction": "buy",
         "price": 0.850, "qty": 10000, "date": "2026-05-25",
         "reason": "加仓", "status": "open", "tags": []},
    ]
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(seed, ensure_ascii=False))

    # TJ-40: auto-close matches the open trade
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"),
           "auto-close", "--code", "513180", "--close-price", "0.920"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-40: auto-close exit 0", result.returncode == 0, f"rc={result.returncode}")

    trades = json.loads(trades_path.read_text())
    open_trades = [t for t in trades if t["status"] == "open" and t["code"] == "513180"]
    closed_new = [t for t in trades if t["id"] == "T20260525-001"]
    test("TJ-41: no open trades remain", len(open_trades) == 0)
    test("TJ-42: matched trade closed", closed_new[0]["status"] == "closed" if closed_new else False)
    test("TJ-43: close_price set", closed_new[0].get("close_price") == 0.920 if closed_new else False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: FAIL on TJ-40

- [ ] **Step 3: Implement `auto-close`**

```python
def cmd_auto_close(args):
    """Auto-close open trades for a code (called by portfolio remove)."""
    trades = load_trades()
    code = args.code
    close_price = float(args.close_price)
    close_date = args.close_date or date.today().isoformat()

    matched = [t for t in trades if t["code"] == code and t["status"] == "open"]
    if not matched:
        json.dump({"status": "noop", "message": f"无未平仓交易 {code}"}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    closed_ids = []
    for trade in matched:
        buy_price = trade["price"]
        qty = trade["qty"]
        pnl = round((close_price - buy_price) * qty, 2)
        pnl_pct = round((close_price - buy_price) / buy_price * 100, 2)
        open_date = datetime.strptime(trade["date"], "%Y-%m-%d").date()
        close_dt = datetime.strptime(close_date, "%Y-%m-%d").date()
        hold_days = (close_dt - open_date).days

        trade["status"] = "closed"
        trade["close_price"] = close_price
        trade["close_date"] = close_date
        trade["pnl"] = pnl
        trade["pnl_pct"] = pnl_pct
        trade["hold_days"] = hold_days
        closed_ids.append(trade["id"])

    save_trades(trades)

    result = {"status": "ok", "closed": closed_ids, "message": f"已自动平仓 {len(closed_ids)} 笔"}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
```

Add parser and dispatch for `auto-close`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-40 through TJ-43 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement auto-close for portfolio integration"
```

---

### Task 5: `list` and `stats` Subcommands

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing tests**

```python
def run_list_stats_tests():
    """Test list and stats subcommands."""
    print("\n📋 列表与统计测试 (List & Stats)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)

    seed = [
        {"id": "T20260520-001", "code": "513180", "direction": "buy",
         "price": 0.800, "qty": 5000, "date": "2026-05-20",
         "reason": "定投", "status": "closed", "pnl": 250.0, "pnl_pct": 6.25,
         "hold_days": 10, "tags": []},
        {"id": "T20260525-001", "code": "513180", "direction": "buy",
         "price": 0.850, "qty": 10000, "date": "2026-05-25",
         "reason": "加仓", "status": "open", "tags": []},
        {"id": "T20260522-001", "code": "510300", "direction": "buy",
         "price": 4.200, "qty": 2000, "date": "2026-05-22",
         "reason": "突破", "status": "closed", "pnl": -200.0, "pnl_pct": -2.38,
         "hold_days": 5, "tags": []},
    ]
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(seed, ensure_ascii=False))

    # TJ-50: list all
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"), "list"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-50: list returns trades", len(output.get("trades", [])) == 3)

    # TJ-51: list --open
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"), "list", "--open"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-51: list --open filters", len(output.get("trades", [])) == 1)

    # TJ-52: list --code
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"), "list", "--code", "510300"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-52: list --code filters", len(output.get("trades", [])) == 1)

    # TJ-60: stats
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"), "stats"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-60: stats total trades", output.get("total_closed") == 2)
    test("TJ-61: stats win rate", output.get("win_rate") == 50.0, f"got {output.get('win_rate')}")
    test("TJ-62: stats total pnl", output.get("total_pnl") == 50.0, f"got {output.get('total_pnl')}")
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL on TJ-50

- [ ] **Step 3: Implement `list` and `stats`**

```python
def cmd_list(args):
    """List trades with optional filters."""
    trades = load_trades()

    if args.open:
        trades = [t for t in trades if t["status"] == "open"]
    if args.code:
        trades = [t for t in trades if t["code"] == args.code]

    result = {"trades": trades, "count": len(trades)}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_stats(args):
    """Quick win-rate and P&L stats for closed trades."""
    trades = load_trades()
    closed = [t for t in trades if t["status"] == "closed"]

    if not closed:
        json.dump({"total_closed": 0, "message": "无已平仓交易"}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
    win_rate = round(len(wins) / len(closed) * 100, 1)
    avg_hold = round(sum(t.get("hold_days", 0) for t in closed) / len(closed), 1)
    avg_win = round(sum(t.get("pnl", 0) for t in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(t.get("pnl", 0) for t in losses) / len(losses), 2) if losses else 0
    profit_factor = round(abs(sum(t.get("pnl", 0) for t in wins)) / abs(sum(t.get("pnl", 0) for t in losses)), 2) if losses and sum(t.get("pnl", 0) for t in losses) != 0 else float("inf")

    result = {
        "total_closed": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_hold_days": avg_hold,
        "avg_win_pnl": avg_win,
        "avg_loss_pnl": avg_loss,
        "profit_factor": profit_factor,
        "open_count": len([t for t in load_trades() if t["status"] == "open"]),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
```

Add parsers and dispatch.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-50 through TJ-62 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement list and stats subcommands"
```

---

### Task 6: Error Pattern Detection (`TradeAnalyzer`)

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing tests for pattern detection**

```python
def run_pattern_tests():
    """Test error pattern detection."""
    print("\n🔍 错误模式检测测试 (Error Patterns)")
    print("=" * 50)

    from trade_journal import TradeAnalyzer

    analyzer = TradeAnalyzer()

    # TJ-70: early_exit — hold<3 days, positive pnl
    trade = {"id": "T1", "status": "closed", "hold_days": 2, "pnl": 100, "pnl_pct": 3.0,
             "date": "2026-05-20", "price": 1.0, "code": "513180", "direction": "buy", "tags": []}
    tags = analyzer.detect_patterns(trade, all_trades=[trade])
    test("TJ-70: early_exit detected", "early_exit" in tags, f"got {tags}")

    # TJ-71: bag_holding — hold>20 days, loss>10%
    trade = {"id": "T2", "status": "open", "date": "2026-05-01", "price": 1.0,
             "code": "510300", "direction": "buy", "tags": [],
             "hold_days": 25, "pnl_pct": -12.0}
    tags = analyzer.detect_patterns(trade, all_trades=[trade])
    test("TJ-71: bag_holding detected", "bag_holding" in tags, f"got {tags}")

    # TJ-72: revenge_trade — new trade within 2 days of a loss
    loss_trade = {"id": "T3", "status": "closed", "date": "2026-05-24",
                  "close_date": "2026-05-24", "pnl": -500, "code": "510050",
                  "direction": "buy", "price": 3.0, "tags": []}
    new_trade = {"id": "T4", "status": "open", "date": "2026-05-25",
                 "code": "510300", "direction": "buy", "price": 4.0, "tags": []}
    tags = analyzer.detect_patterns(new_trade, all_trades=[loss_trade, new_trade])
    test("TJ-72: revenge_trade detected", "revenge_trade" in tags, f"got {tags}")

    # TJ-73: ignored_stop — price hit stop_loss but sold 4+ days later
    trade = {"id": "T5", "status": "closed", "date": "2026-05-10",
             "close_date": "2026-05-20", "price": 1.0, "stop_loss": 0.90,
             "close_price": 0.85, "code": "513180", "direction": "buy",
             "hold_days": 10, "pnl": -150, "tags": [],
             "_stop_hit_date": "2026-05-14"}
    tags = analyzer.detect_patterns(trade, all_trades=[trade])
    test("TJ-73: ignored_stop detected", "ignored_stop" in tags, f"got {tags}")

    # TJ-74: no false positive — normal trade
    trade = {"id": "T6", "status": "closed", "date": "2026-05-10",
             "close_date": "2026-05-25", "price": 1.0, "close_price": 1.10,
             "code": "510300", "direction": "buy", "hold_days": 15,
             "pnl": 200, "pnl_pct": 10.0, "tags": []}
    tags = analyzer.detect_patterns(trade, all_trades=[trade])
    test("TJ-74: no false positive", len(tags) == 0, f"got {tags}")
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — `ImportError: cannot import name 'TradeAnalyzer'`

- [ ] **Step 3: Implement `TradeAnalyzer`**

```python
class TradeAnalyzer:
    """Detects behavioral error patterns in trades."""

    RULES = {
        "fomo": {"severity": "warning", "desc": "买入前连续≥3根阳线"},
        "early_exit": {"severity": "warning", "desc": "持仓<3天且正收益"},
        "bag_holding": {"severity": "critical", "desc": "持仓>20天且浮亏>10%"},
        "revenge_trade": {"severity": "critical", "desc": "亏损后2天内新开仓"},
        "ignored_stop": {"severity": "critical", "desc": "触止损后延后>3天卖出"},
        "over_concentrated": {"severity": "warning", "desc": "单票仓位>组合20%"},
        "chasing_high": {"severity": "warning", "desc": "买入价距10日高点<3%"},
    }

    def detect_patterns(self, trade: dict, all_trades: list = None, kline: list = None) -> list[str]:
        """Detect error patterns for a single trade. Returns list of pattern names."""
        tags = []
        all_trades = all_trades or []

        # early_exit: hold < 3 days + positive pnl
        if trade.get("status") == "closed":
            if trade.get("hold_days", 999) < 3 and trade.get("pnl", 0) > 0:
                tags.append("early_exit")

        # bag_holding: hold > 20 days + loss > 10%
        if trade.get("hold_days", 0) > 20 and trade.get("pnl_pct", 0) < -10:
            tags.append("bag_holding")

        # revenge_trade: new trade within 2 days of a loss
        if trade.get("status") == "open" or trade.get("date"):
            trade_date = trade.get("date", "")
            if trade_date:
                trade_dt = datetime.strptime(trade_date, "%Y-%m-%d").date()
                for t in all_trades:
                    if t["id"] == trade["id"]:
                        continue
                    if t.get("status") == "closed" and t.get("pnl", 0) < 0:
                        close_date = t.get("close_date", "")
                        if close_date:
                            close_dt = datetime.strptime(close_date, "%Y-%m-%d").date()
                            if 0 <= (trade_dt - close_dt).days <= 2:
                                tags.append("revenge_trade")
                                break

        # ignored_stop: had stop_loss, close_price < stop_loss, and held too long after stop hit
        if trade.get("status") == "closed" and trade.get("stop_loss"):
            close_price = trade.get("close_price", 0)
            if close_price < trade["stop_loss"]:
                # If we know the stop_hit_date, check delay
                stop_hit_date = trade.get("_stop_hit_date")
                if stop_hit_date:
                    stop_dt = datetime.strptime(stop_hit_date, "%Y-%m-%d").date()
                    close_dt = datetime.strptime(trade["close_date"], "%Y-%m-%d").date()
                    if (close_dt - stop_dt).days > 3:
                        tags.append("ignored_stop")
                else:
                    # Heuristic: if hold_days > expected reasonable stop window
                    tags.append("ignored_stop")

        return tags

    def analyze_all(self, trades: list, kline_data: dict = None) -> list:
        """Run pattern detection on all trades, return updated trades with tags."""
        for trade in trades:
            new_tags = self.detect_patterns(trade, all_trades=trades, kline=kline_data)
            trade["tags"] = list(set(trade.get("tags", []) + new_tags))
        return trades
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-70 through TJ-74 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement TradeAnalyzer error pattern detection"
```

---

## Phase 2: AI Recommendation Tracking

### Task 7: `save-ai-rec` Subcommand

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test**

```python
def run_ai_rec_tests():
    """Test AI recommendation save/backfill."""
    print("\n🤖 AI建议追踪测试 (AI Rec)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    ai_path = Path(tmpdir) / "ai_recommendations.json"
    env = os.environ.copy()
    env["TRADE_JOURNAL_AI_REC"] = str(ai_path)

    # TJ-80: save-ai-rec
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"),
           "save-ai-rec",
           "--code", "002371", "--source", "longtou",
           "--direction", "bullish", "--score", "2.5",
           "--dimensions", '{"technical":1.5,"capital_flow":1.0,"fundamental":0.8,"sentiment":1.0,"macro":0.5}',
           "--stop-loss", "285.0", "--targets", "[310.0, 335.0]",
           "--report-path", "reports/002371/20260526.md"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-80: save-ai-rec exit 0", result.returncode == 0, f"rc={result.returncode}, err={result.stderr[:100]}")

    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-81: returns rec_id", "rec_id" in output, f"keys={list(output.keys())}")

    # TJ-82: persisted correctly
    recs = json.loads(ai_path.read_text()) if ai_path.exists() else []
    test("TJ-82: rec persisted", len(recs) == 1)
    if recs:
        r = recs[0]
        test("TJ-83: direction", r["direction"] == "bullish")
        test("TJ-84: score", r["score"] == 2.5)
        test("TJ-85: dimensions", r["dimensions"]["technical"] == 1.5)
        test("TJ-86: outcome.filled is false", r["outcome"]["filled"] is False)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL on TJ-80

- [ ] **Step 3: Implement `save-ai-rec`**

```python
def cmd_save_ai_rec(args):
    """Save an AI recommendation snapshot."""
    rec_date = args.date or date.today().isoformat()
    rec_id = generate_ai_rec_id(rec_date)

    dimensions = json.loads(args.dimensions) if args.dimensions else {}
    targets = json.loads(args.targets) if args.targets else []

    record = {
        "id": rec_id,
        "code": args.code,
        "source": args.source,
        "date": rec_date,
        "direction": args.direction,
        "score": float(args.score),
        "dimensions": dimensions,
        "stop_loss": float(args.stop_loss) if args.stop_loss else None,
        "targets": targets,
        "report_path": args.report_path or "",
        "outcome": {
            "filled": False,
            "n_day_return": None,
            "hit_target": None,
            "hit_stop_loss": None,
            "direction_correct": None,
            "eval_date": None,
        },
    }

    recs = load_ai_recs()
    recs.append(record)
    save_ai_recs(recs)

    result = {"status": "ok", "rec_id": rec_id, "message": f"已保存AI建议 {rec_id}"}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
```

Add parser:

```python
    # save-ai-rec
    p_airec = sub.add_parser("save-ai-rec")
    p_airec.add_argument("--code", required=True)
    p_airec.add_argument("--source", required=True)
    p_airec.add_argument("--direction", required=True, choices=["bullish", "bearish", "neutral"])
    p_airec.add_argument("--score", required=True)
    p_airec.add_argument("--dimensions", default=None)
    p_airec.add_argument("--stop-loss", default=None)
    p_airec.add_argument("--targets", default=None)
    p_airec.add_argument("--report-path", default=None)
    p_airec.add_argument("--date", default=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-80 through TJ-86 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement save-ai-rec for AI recommendation tracking"
```

---

### Task 8: `backfill-outcomes` Subcommand

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test**

```python
def run_backfill_tests():
    """Test AI outcome backfill logic."""
    print("\n📊 回填测试 (Backfill)")
    print("=" * 50)

    from trade_journal import backfill_single_outcome

    # TJ-90: bullish + went up → direction_correct = True
    rec = {
        "id": "AI20260520-001", "code": "513180", "source": "stock-trend",
        "date": "2026-05-20", "direction": "bullish", "score": 2.0,
        "stop_loss": 0.780, "targets": [0.950],
        "outcome": {"filled": False}
    }
    # Simulate kline: price went from 0.85 to 0.90 (5.9% up in 5 days)
    kline_after = [{"close": 0.86}, {"close": 0.87}, {"close": 0.88}, {"close": 0.89}, {"close": 0.90}]
    result = backfill_single_outcome(rec, kline_after, eval_days=5)
    test("TJ-90: direction_correct true", result["direction_correct"] is True)
    test("TJ-91: n_day_return positive", result["n_day_return"] > 0)
    test("TJ-92: filled is True", result["filled"] is True)

    # TJ-93: bearish + went down → direction_correct = True
    rec2 = {
        "id": "AI20260520-002", "code": "510050", "source": "etf-scan",
        "date": "2026-05-20", "direction": "bearish", "score": -1.5,
        "stop_loss": None, "targets": [],
        "outcome": {"filled": False}
    }
    kline_down = [{"close": 3.0}, {"close": 2.95}, {"close": 2.90}, {"close": 2.85}, {"close": 2.80}]
    result2 = backfill_single_outcome(rec2, kline_down, eval_days=5)
    test("TJ-93: bearish direction_correct", result2["direction_correct"] is True)

    # TJ-94: hit_target detection
    rec3 = {
        "id": "AI20260520-003", "code": "510300", "source": "stock-trend",
        "date": "2026-05-20", "direction": "bullish", "score": 2.0,
        "stop_loss": 3.8, "targets": [4.2],
        "outcome": {"filled": False}
    }
    kline_hit = [{"close": 4.05}, {"close": 4.10}, {"close": 4.15}, {"close": 4.25}, {"close": 4.30}]
    result3 = backfill_single_outcome(rec3, kline_hit, eval_days=5)
    test("TJ-94: hit_target True", result3["hit_target"] is True)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — `cannot import name 'backfill_single_outcome'`

- [ ] **Step 3: Implement backfill logic**

```python
def backfill_single_outcome(rec: dict, kline_after: list, eval_days: int = 5) -> dict:
    """Evaluate a single AI recommendation against subsequent kline data.
    
    Args:
        rec: The AI recommendation record
        kline_after: List of kline dicts (with 'close' key) AFTER the recommendation date
        eval_days: Number of days to evaluate
    
    Returns:
        Updated outcome dict with filled=True
    """
    if not kline_after or len(kline_after) < eval_days:
        return rec.get("outcome", {"filled": False})

    # Use the first close as reference price (day after recommendation)
    ref_price = kline_after[0]["close"]
    eval_price = kline_after[eval_days - 1]["close"]
    n_day_return = round((eval_price - ref_price) / ref_price * 100, 2)

    direction = rec.get("direction", "neutral")
    if direction == "bullish":
        direction_correct = n_day_return > 0
    elif direction == "bearish":
        direction_correct = n_day_return < 0
    else:
        direction_correct = abs(n_day_return) < 2  # neutral: didn't move much

    # Check targets
    hit_target = False
    targets = rec.get("targets", [])
    if targets and direction == "bullish":
        max_price = max(k["close"] for k in kline_after[:eval_days])
        hit_target = max_price >= targets[0]
    elif targets and direction == "bearish":
        min_price = min(k["close"] for k in kline_after[:eval_days])
        hit_target = min_price <= targets[0]

    # Check stop loss
    hit_stop_loss = False
    stop_loss = rec.get("stop_loss")
    if stop_loss and direction == "bullish":
        min_price = min(k["close"] for k in kline_after[:eval_days])
        hit_stop_loss = min_price <= stop_loss
    elif stop_loss and direction == "bearish":
        max_price = max(k["close"] for k in kline_after[:eval_days])
        hit_stop_loss = max_price >= stop_loss

    return {
        "filled": True,
        "n_day_return": n_day_return,
        "hit_target": hit_target,
        "hit_stop_loss": hit_stop_loss,
        "direction_correct": direction_correct,
        "eval_date": date.today().isoformat(),
    }


def cmd_backfill_outcomes(args):
    """Backfill AI recommendation outcomes using kline data."""
    recs = load_ai_recs()
    today = date.today()
    updated = 0

    for rec in recs:
        if rec["outcome"].get("filled"):
            continue
        rec_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
        # ETF/index: 5 days, stocks: 10 days
        code = rec.get("code", "")
        eval_days = 5 if code.startswith("5") or code.startswith("1") else 10
        if (today - rec_date).days < eval_days + 1:
            continue

        # Fetch kline data
        try:
            from resolve_code import code_to_ts_code
            ts_code = code_to_ts_code(code)
            kline = _fetch_kline_for_backfill(ts_code, rec["date"], eval_days + 5)
            if kline:
                outcome = backfill_single_outcome(rec, kline, eval_days)
                rec["outcome"] = outcome
                updated += 1
        except Exception:
            continue

    save_ai_recs(recs)
    result = {"status": "ok", "updated": updated, "total": len(recs)}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _fetch_kline_for_backfill(ts_code: str, start_date: str, days: int) -> list:
    """Fetch kline data starting from day after start_date."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        out_path = f.name
    try:
        cmd = [sys.executable, str(SCRIPT_DIR / "fetch_kline_eastmoney.py"), ts_code, "-o", out_path]
        subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        with open(out_path, "r") as f:
            raw = json.load(f)
        os.unlink(out_path)
        records = raw if isinstance(raw, list) else raw.get("data", [])
        # Filter to records after start_date
        filtered = [r for r in records if r.get("date", r.get("trade_date", "")) > start_date]
        return filtered[:days] if filtered else None
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-90 through TJ-94 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement backfill-outcomes for AI accuracy tracking"
```

---

## Phase 3: Review Report & Integrations

### Task 9: `review` Subcommand

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/trade_journal.py`
- Modify: `.claude/skills/stock-trend/tests/test_trade_journal.py`

- [ ] **Step 1: Write failing test**

```python
def run_review_tests():
    """Test review report generation."""
    print("\n📝 复盘报告测试 (Review)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    ai_path = Path(tmpdir) / "ai_recommendations.json"
    reviews_dir = Path(tmpdir) / "reviews"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)
    env["TRADE_JOURNAL_AI_REC"] = str(ai_path)
    env["TRADE_JOURNAL_REVIEWS_DIR"] = str(reviews_dir)

    # Seed trades
    trades = [
        {"id": "T20260519-001", "code": "513180", "direction": "buy",
         "price": 0.85, "qty": 10000, "date": "2026-05-19", "reason": "抄底",
         "status": "closed", "close_price": 0.92, "close_date": "2026-05-23",
         "pnl": 700, "pnl_pct": 8.24, "hold_days": 4, "tags": ["early_exit"]},
        {"id": "T20260520-001", "code": "510300", "direction": "buy",
         "price": 4.20, "qty": 2000, "date": "2026-05-20", "reason": "追高",
         "status": "closed", "close_price": 4.05, "close_date": "2026-05-25",
         "pnl": -300, "pnl_pct": -3.57, "hold_days": 5, "tags": ["fomo"]},
    ]
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(trades, ensure_ascii=False))

    # Seed AI recs
    ai_recs = [
        {"id": "AI20260519-001", "code": "513180", "source": "stock-trend",
         "date": "2026-05-19", "direction": "bullish", "score": 2.5,
         "dimensions": {"technical": 1.5, "capital_flow": 1.0},
         "outcome": {"filled": True, "direction_correct": True, "n_day_return": 5.0,
                     "hit_target": False, "hit_stop_loss": False}},
        {"id": "AI20260520-001", "code": "510300", "source": "etf-scan",
         "date": "2026-05-20", "direction": "bullish", "score": 1.0,
         "dimensions": {"technical": 0.5, "capital_flow": 0.5},
         "outcome": {"filled": True, "direction_correct": False, "n_day_return": -3.5,
                     "hit_target": False, "hit_stop_loss": True}},
    ]
    ai_path.write_text(json.dumps(ai_recs, ensure_ascii=False))

    # TJ-100: generate review
    cmd = [sys.executable, str(SCRIPTS_DIR / "trade_journal.py"), "review", "--period", "weekly"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-100: review exit 0", result.returncode == 0, f"rc={result.returncode}, err={result.stderr[:200]}")

    output = json.loads(result.stdout) if result.returncode == 0 else {}
    test("TJ-101: has trades section", "trades" in output)
    test("TJ-102: has error_patterns", "error_patterns" in output)
    test("TJ-103: has ai_accuracy", "ai_accuracy" in output)
    test("TJ-104: has recommendations", "recommendations" in output)

    if "trades" in output:
        t = output["trades"]
        test("TJ-105: total=2", t.get("total") == 2)
        test("TJ-106: win_count=1", t.get("win_count") == 1)
        test("TJ-107: win_rate=50", t.get("win_rate") == 50.0)

    if "ai_accuracy" in output:
        ai = output["ai_accuracy"]
        test("TJ-108: ai total=2", ai.get("total_recommendations") == 2)
        test("TJ-109: ai correct=1", ai.get("correct") == 1)

    # TJ-110: review saved to file
    test("TJ-110: review file created", reviews_dir.exists() and len(list(reviews_dir.glob("*.json"))) > 0)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL on TJ-100

- [ ] **Step 3: Implement `review` subcommand**

```python
def cmd_review(args):
    """Generate a structured review report."""
    trades = load_trades()
    ai_recs = load_ai_recs()
    period = args.period or "weekly"

    # Determine date range
    today = date.today()
    if period == "weekly":
        start = today - timedelta(days=today.weekday() + 7)  # last Monday
        end = start + timedelta(days=6)
        period_label = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
    else:
        start = today.replace(day=1) - timedelta(days=1)
        start = start.replace(day=1)
        end = today.replace(day=1) - timedelta(days=1)
        period_label = start.strftime("%Y-%m")

    # Filter closed trades in period
    closed = [t for t in trades if t.get("status") == "closed"
              and start.isoformat() <= t.get("close_date", "") <= end.isoformat()]

    # If no trades in exact period, use all closed trades (for testing)
    if not closed:
        closed = [t for t in trades if t.get("status") == "closed"]

    # Trade stats
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_hold = round(sum(t.get("hold_days", 0) for t in closed) / len(closed), 1) if closed else 0
    avg_win = round(sum(t.get("pnl", 0) for t in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(t.get("pnl", 0) for t in losses) / len(losses), 2) if losses else 0
    loss_sum = abs(sum(t.get("pnl", 0) for t in losses))
    profit_factor = round(sum(t.get("pnl", 0) for t in wins) / loss_sum, 2) if loss_sum > 0 else None

    # Error patterns
    pattern_counts = {}
    for t in closed:
        for tag in t.get("tags", []):
            if tag not in pattern_counts:
                pattern_counts[tag] = {"count": 0, "total_pnl": 0}
            pattern_counts[tag]["count"] += 1
            pattern_counts[tag]["total_pnl"] += t.get("pnl", 0)

    error_patterns = [
        {"pattern": k, "count": v["count"], "total_pnl": round(v["total_pnl"], 2)}
        for k, v in pattern_counts.items()
    ]

    # AI accuracy
    filled_recs = [r for r in ai_recs if r.get("outcome", {}).get("filled")]
    correct = sum(1 for r in filled_recs if r["outcome"].get("direction_correct"))
    wrong = len(filled_recs) - correct
    pending = len([r for r in ai_recs if not r.get("outcome", {}).get("filled")])

    ai_accuracy = {
        "total_recommendations": len(filled_recs),
        "correct": correct,
        "wrong": wrong,
        "pending": pending,
        "hit_rate": round(correct / len(filled_recs) * 100, 1) if filled_recs else 0,
    }

    # Recommendations (simple rule-based)
    recommendations = []
    if pattern_counts.get("fomo", {}).get("count", 0) >= 1:
        recommendations.append("减少追高操作，追高交易容易亏损")
    if pattern_counts.get("early_exit", {}).get("count", 0) >= 1:
        recommendations.append("持仓周期偏短，建议延长持仓时间")
    if avg_hold < 10 and closed:
        recommendations.append(f"平均持仓{avg_hold}天偏短，中线建议30+天")
    if ai_accuracy.get("hit_rate", 100) < 60:
        recommendations.append("AI预测准确率偏低，建议降低信号权重")

    report = {
        "period": period_label,
        "generated_at": today.isoformat(),
        "trades": {
            "total": len(closed),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_hold_days": avg_hold,
            "avg_win_pnl": avg_win,
            "avg_loss_pnl": avg_loss,
            "profit_factor": profit_factor,
        },
        "error_patterns": error_patterns,
        "ai_accuracy": ai_accuracy,
        "recommendations": recommendations,
    }

    # Save to reviews dir
    reviews_dir = Path(os.environ.get("TRADE_JOURNAL_REVIEWS_DIR", str(REVIEWS_DIR)))
    reviews_dir.mkdir(parents=True, exist_ok=True)
    review_path = reviews_dir / f"{period_label}.json"
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-100 through TJ-110 PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/stock-trend/scripts/trade_journal.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): implement review subcommand with structured reports"
```

---

### Task 10: Portfolio Manager Integration

**Files:**
- Modify: `.claude/skills/stock-trend/scripts/portfolio_manager.py:702-720`

- [ ] **Step 1: Write failing integration test**

Add to `test_trade_journal.py`:

```python
def run_integration_tests():
    """Test portfolio_manager auto-close integration."""
    print("\n🔗 集成测试 (Integration)")
    print("=" * 50)

    import subprocess
    tmpdir = tempfile.mkdtemp()
    trades_path = Path(tmpdir) / "trades.json"
    portfolio_path = Path(tmpdir) / "portfolio.yaml"
    env = os.environ.copy()
    env["TRADE_JOURNAL_TRADES"] = str(trades_path)
    env["STOCK_TREND_PORTFOLIO"] = str(portfolio_path)

    # Seed portfolio
    import yaml
    portfolio = {
        "holdings": [{"code": "513180", "buy_price": 0.85, "quantity": 10000,
                      "buy_date": "2026-05-20", "status": "active", "name": "恒生科技ETF"}],
        "settings": {"alert_threshold_pct": 3.0}
    }
    portfolio_path.write_text(yaml.dump(portfolio, allow_unicode=True))

    # Seed trade journal
    seed = [{"id": "T20260520-001", "code": "513180", "direction": "buy",
             "price": 0.85, "qty": 10000, "date": "2026-05-20",
             "reason": "抄底", "status": "open", "tags": []}]
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(seed, ensure_ascii=False))

    # TJ-120: portfolio remove triggers auto-close
    cmd = [sys.executable, str(SCRIPTS_DIR / "portfolio_manager.py"),
           "remove", "--code", "513180", "--close-price", "0.920"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    test("TJ-120: portfolio remove exit 0", result.returncode == 0, f"rc={result.returncode}")

    # Check trade journal was updated
    trades = json.loads(trades_path.read_text()) if trades_path.exists() else []
    closed = [t for t in trades if t["status"] == "closed"]
    test("TJ-121: trade auto-closed", len(closed) == 1, f"closed={len(closed)}")
    if closed:
        test("TJ-122: close_price correct", closed[0].get("close_price") == 0.920)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL on TJ-121 (portfolio remove doesn't call trade_journal yet)

- [ ] **Step 3: Add auto-close hook to `portfolio_manager.py`**

Modify `cmd_remove()` in `portfolio_manager.py` (after line 716, before save):

```python
def cmd_remove(args):
    """Mark a holding as closed."""
    portfolio = load_portfolio()
    code = args.code
    holding = find_holding(portfolio.get("holdings", []), code)
    if not holding:
        result = {"error": f"未找到活跃持仓 {code}"}
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    holding["status"] = "closed"
    close_price = float(args.close_price) if args.close_price else None
    holding["close_price"] = close_price
    holding["close_date"] = args.close_date or date.today().isoformat()
    save_portfolio(portfolio)

    # Auto-close matching trade journal entries
    if close_price:
        _auto_close_trade_journal(code, close_price, holding["close_date"])

    result = {"status": "ok", "message": f"已平仓 {name_or_fallback(holding)}", "holding": holding}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _auto_close_trade_journal(code: str, close_price: float, close_date: str):
    """Call trade_journal.py auto-close (best-effort, non-blocking)."""
    try:
        cmd = [sys.executable, str(SCRIPT_DIR / "trade_journal.py"),
               "auto-close", "--code", code,
               "--close-price", str(close_price), "--close-date", close_date]
        subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                       env=os.environ)
    except Exception:
        pass  # Non-blocking: don't fail portfolio operation
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: TJ-120 through TJ-122 PASS

- [ ] **Step 5: Run existing portfolio tests to ensure no regression**

Run: `python3 .claude/skills/stock-trend/tests/test_portfolio.py`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/stock-trend/scripts/portfolio_manager.py .claude/skills/stock-trend/tests/test_trade_journal.py
git commit -m "feat(trade-journal): integrate auto-close with portfolio_manager remove"
```

---

### Task 11: SKILL.md Registration

**Files:**
- Modify: `.claude/skills/stock-trend/SKILL.md`

- [ ] **Step 1: Add `/trade` and `/trade-review` triggers and allowed-tools**

Add to SKILL.md `triggers:` section:

```yaml
triggers:
  - /stock-trend
  - /etf-scan
  - /longtou
  - /trade
  - /trade-review
  - /trade-stats
```

Add to `allowed-tools:` section:

```yaml
  - Bash(python3 .claude/skills/stock-trend/scripts/trade_journal.py *)
```

- [ ] **Step 2: Add `/trade` command documentation section**

Add new section to SKILL.md below existing commands:

```markdown
---

## /trade — 交易日志

记录和管理交易操作。

| 子命令 | 用途 |
|--------|------|
| `/trade add --code X --direction buy --price X --qty X --reason "..."` | 记录新交易 |
| `/trade close --id X --price X` | 手动平仓 |
| `/trade list [--open] [--code X]` | 查看交易历史 |
| `/trade-stats` | 快速胜率统计 |
| `/trade-review [--period weekly\|monthly]` | 生成复盘报告 |

### /trade 执行步骤

1. 解析子命令和参数
2. 调用 `python3 trade_journal.py <subcommand> <args>`
3. 解析 JSON 输出并以表格/结构化格式展示给用户
4. 对于 review，附加行为改进建议

### /trade-review 执行步骤

1. 运行 `python3 trade_journal.py backfill-outcomes` 更新 AI 建议回填
2. 运行 `python3 trade_journal.py review --period <period>` 生成报告
3. 以结构化格式展示报告：交易统计 → 错误模式 → AI 准确率 → 改进建议
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/stock-trend/SKILL.md
git commit -m "feat(trade-journal): register /trade and /trade-review commands in SKILL.md"
```

---

### Task 12: Run Full Test Suite & Verify

**Files:**
- No new files

- [ ] **Step 1: Run trade journal tests**

Run: `python3 .claude/skills/stock-trend/tests/test_trade_journal.py`
Expected: All tests PASS

- [ ] **Step 2: Run existing stock-trend tests (no regression)**

Run: `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
Expected: All existing tests PASS

- [ ] **Step 3: Run portfolio tests (no regression)**

Run: `python3 .claude/skills/stock-trend/tests/test_portfolio.py`
Expected: All existing tests PASS

- [ ] **Step 4: Run golden snapshot tests**

Run: `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
Expected: No unexpected failures. If golden changes are due to portfolio_manager.py modification, regenerate with `--regenerate` and note in commit.

- [ ] **Step 5: Final commit (if golden regenerated)**

```bash
git add .claude/skills/stock-trend/tests/golden/
git commit -m "test: regenerate golden snapshots after portfolio_manager auto-close hook"
```

---

## Summary of Deliverables

| Phase | What | Subcommands |
|-------|------|-------------|
| 1 | Core trade CRUD + error patterns | `add`, `close`, `auto-close`, `list`, `stats` |
| 2 | AI recommendation tracking | `save-ai-rec`, `backfill-outcomes` |
| 3 | Review reports + integration | `review`, portfolio hook, SKILL.md |

## Dependencies

```
Task 1 (data layer)
├── Task 2 (add) 
├── Task 3 (close)
│   └── Task 4 (auto-close)
├── Task 5 (list/stats)
├── Task 6 (TradeAnalyzer) ─── used by Tasks 3, 9
├── Task 7 (save-ai-rec)
│   └── Task 8 (backfill)
│       └── Task 9 (review)
├── Task 10 (portfolio integration) ─── depends on Task 4
└── Task 11 (SKILL.md) ─── depends on all above
    └── Task 12 (full verification)
```
