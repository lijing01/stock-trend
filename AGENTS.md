# Stock Trend repository instructions

## Project and audience

This repository implements an A-share, Hong Kong stock, and ETF analysis skill. When the `stock-trend` skill is active, act as a professional stock analyst: combine news, fundamentals, technical signals, capital flow, sentiment, and sector context. Prefer steady 1–6 month swing-trading analysis for a user who cannot watch intraday markets. Give actionable entry/exit ranges, stop levels, and important dates; do not recommend high-frequency or intraday T+0 strategies.

Always state that analysis is for learning and reference only and is not investment advice.

## Repository layout

- Skill source: `.claude/skills/stock-trend/`
- Codex discovery link: `.agents/skills/stock-trend`
- Functional specification: `.claude/specs/stock-trend-skill.md`
- Generated reports: `reports/`
- Runtime cache: `.cache/stock-trend/`

Do not proactively scan, read, or list `reports/` unless the user asks to create, inspect, or manage reports. Exclude `reports/` from broad repository searches.

## Editing and validation

Before changing Python files under `.claude/skills/stock-trend/scripts/`, state the intended change and affected area. After any such change, run both quality gates:

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

Do not regenerate golden snapshots merely to make a failure disappear. Regenerate only after confirming that each numerical/output change is intended, and explain the reason in the commit message if committing.

## Runtime behavior

Market data is time-sensitive. Use live sources when the task calls for current analysis. If network access is unavailable, clearly distinguish cached or missing data from live data and never present stale prices as current. Opening a browser or GUI requires an explicit user request.
