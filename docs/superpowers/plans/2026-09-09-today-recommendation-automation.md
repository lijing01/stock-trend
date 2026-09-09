# 今日推荐自动流程 Implementation Plan

**Goal:** 用户说“今日推荐”即完成市场刷新、候选扫描、历史评价、每周研究和监控，并在回复中获知策略变化。

**Architecture:** 新增 `scripts/bridge/run_today.py`，复用现有脚本与 evolution_job 的业务函数。保留候选 JSON 接口，追加 workflow 和 notifications；使用真实交易日、上海时间与已有原子写入工具。每日评价可重试，每周成功记录去重；显式发布规则保持不变，现有监控安全恢复必须通知。

**Tech Stack:** Python 标准库、现有 stock-trend 数据与研究模块。

## 实施与验证

- [x] 新增 `tests/test_run_today.py`：覆盖顺序、上游失败、盘中/休市日期、日历不可用、每周去重与重试、策略切换/恢复通知、dry-run 无副作用。
- [x] 新增 `scripts/bridge/run_today.py`：只在市场刷新成功后扫描；以最近完成交易日评价并保存任务，自动 weekly/monitor；记录并输出策略变化。
- [x] 将测试接入 `tests/test_stock_trend.py` 的今日推荐测试组。
- [x] 更新 `SKILL.md` 自然语言路由与 `docs/today-recommendation-evolution-usage.md`。
- [ ] 执行定向测试、完整 `test_stock_trend.py`、`test_golden.py --diff`、CLI dry-run、编译检查与 diff 检查；独立审查后修复具体问题。

范围：通知在调用时的对话输出中呈现；不安装后台任务、不接入外部消息服务、不自动发布实验、不改变选股规则。此次验证使用隔离临时目录和模拟行情，不实际运行市场扫描或切换正式策略。

验证证据：使用本机 Python 3.10（默认 python3 为 3.9.6）；定向测试 23 项通过；完整测试 651 passed / 0 failed / 0 skipped；Golden diff 21 passed / 0 failed / 2 warnings，未重建快照。JSON/文本 CLI dry-run、py_compile 和 git diff --check 通过。补充验证了市场 CLI 混合输出解析，以及市场上下文落盘失败不能继续扫描的约束。
