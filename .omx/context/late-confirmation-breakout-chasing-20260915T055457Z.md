# Ralph Context Snapshot

## Task statement

执行已批准的修复计划，解决候选报告中的确认滞后和突破后追高问题。

## Desired outcome

将维科夫结构识别与当前入场资格分离；旧信号、首次 JAC 和价格明显偏离触发位的候选不得进入可执行或等待层，但仍保留在观察池并提供可审计原因。

## Known facts / evidence

- `wyckoff.py` 当前用 `EVENT_MAX_AGE` 同时表达结构事件窗口和买点新鲜度。
- 当前结构窗口为 Spring/ST/JAC 8 根、LPS/BU 10 根。
- 首次确认 JAC 可进入基础买点漏斗；`post_lps_reconfirmation` 主要影响三级奖励。
- `daily_candidates.py` 的最终候选门控未检查当前价相对触发价或 ATR 的偏离。
- 近期报告显示旧 LPS 以及已远离触发价的 JAC 仍位居候选前列。

## Constraints

- 只修改候选入场资格、排序、审计、报告和相关测试/回测。
- 不修改市场环境硬门控、板块持续性、新闻影子层或引入依赖。
- Python 改动后必须运行项目要求的 `test_stock_trend.py` 和 `test_golden.py --diff`。
- 必须避免前视偏差；决策时点只使用当时已有数据。
- 被门控候选继续保留在观察池。

## Unknowns / open questions

- 当前 K 线/维科夫 payload 是否在所有路径都提供事件触发收盘价和 ATR。
- 回测模块已有的 ATR/事件索引字段能否直接复用。
- HTML/JSON 报告字段的最佳兼容形态需要结合现有 renderer 和 tests 确认。

## Likely codebase touchpoints

- `.claude/skills/stock-trend/scripts/analysis/wyckoff.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/core/candidate_score_ledger.py`
- `.claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py`
- `.claude/skills/stock-trend/tests/test_wyckoff.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_wyckoff_backtest.py`
- `.claude/skills/stock-trend/tests/test_stock_trend.py`
- `.claude/skills/stock-trend/tests/test_golden.py`

