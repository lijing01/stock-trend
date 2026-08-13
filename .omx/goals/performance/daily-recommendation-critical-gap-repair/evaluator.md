# Performance Evaluator: daily-recommendation-critical-gap-repair

## Objective
Close the critical daily recommendation performance gaps in the approved plan while preserving recommendation quality and compatibility

## Evaluator Command
```sh
python3 .claude/skills/stock-trend/tests/test_daily_recommendation_performance.py && python3 .claude/skills/stock-trend/tests/test_daily_candidates.py && python3 .claude/skills/stock-trend/tests/test_stock_scanner.py && python3 .claude/skills/stock-trend/tests/test_stock_trend.py && python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

## Pass/Fail Contract
PASS only when deterministic request/deadline/ordering/cache-quality assertions pass, targeted suites pass, both repository quality gates pass, JSON compatibility is preserved, and P2 remains out of scope

This evaluator must exist and produce concrete pass/fail evidence before the performance goal can be completed.
