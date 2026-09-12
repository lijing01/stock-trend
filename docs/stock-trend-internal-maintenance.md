# Stock Trend 内部维护指南

本文面向技能维护、排障和研究，不作为日常功能菜单。用户正常使用时从 `docs/usage-guide.md` 的四个入口进入。

## 今日推荐后台任务

日常调用默认使用 `run_today.py --json` 的后台后处理。报告成功只代表候选报告已就绪，不代表 `close`、`weekly` 和 `monitor` 都已完成。

```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --status <task_id> --json
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --resume <task_id>
```

`--resume` 用于显式恢复未完成任务。只有排障或兼容旧同步流程时才使用 `--postprocess sync`。后台状态、冻结输入、日志和结果保存在 `.cache/stock-trend/evolution/background/<task_id>/`。交易日历不可用时，依赖交易日的后处理会跳过；若日历只覆盖历史区间，检查 `workflow.calendar.coverage_end`。

周任务按评价日期所属 ISO 周归档。只有同周成功且有研究样本的任务才应被视为已完成。监控遇到接口或契约异常时遵循安全恢复合同；普通统计退化只要求人工复核。

## 候选扫描实现

`daily_candidates.py` 是统一今日推荐调用的内部实现，不再作为日常路由单独呈现。必要时可在测试或维护环境执行：

```bash
python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py --json --no-html
```

统一入口会刷新市场上下文、追加 `--news` 并将 JSON 接入 `workflow`。直接运行候选脚本不会代替统一入口的市场刷新、交易日历、后台演进和通知。不要用旧 `market_regime.json` 冒充本次刷新结果。

可选研究参数：

- `--news-file <JSON>`：注入含明确来源和发布时间的可复现新闻证据。
- `--no-news`：仅用于新闻层诊断降级。
- `--style-shadow <FILE>` 与 `--memberships <FILE>`：生成市场风格和历史成分观察，不参与正式推荐。
- `--strategy-shadow <FILE>`：运行已冻结买点奖励的影子排序，不改变正式排序或快照。

影子实验结果必须标注为观察，不得据单日结果调整正式推荐或策略。新闻层是否提升准确性必须由成熟的前向样本决定。

## 板块持续性数据

无 Tushare 权限时，可独立积累收盘后东方财富行业与概念完整截面：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --status --json
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --json
python3 .claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py --dry-run --json
```

正式采集应在 15:10 后运行。`status=saved` 表示快照已写入；`validated` 表示 dry-run 通过；失败日保留缺口，不用 AKShare 行业摘要、BK 历史 K 线或当前成分反推完整历史。该任务是候选板块持续性的数据维护方式，不会替代今日推荐。

## 推荐策略研究与发布

以下 CLI 用于实验回放、排障和人工审核，不属于日常推荐的额外手工步骤：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py close --as-of YYYY-MM-DD --json
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py weekly --as-of YYYY-MM-DD --dry-run --json
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py monitor --json
```

`publish` 和 `rollback` 必须由人工明确发起。发布要求通过注册表验证的完整证据文件；不能通过摘要或单次运行替代证据审核。今日推荐统一入口不会自动发布或向外部渠道推送策略变更。

## 离线回测

ETF 回测引擎生成的 `backtest_stats.json` 可供仓位管理作凯利参数校准，因此在没有确认仓位管理不再使用它之前不得删除：

```bash
python3 .claude/skills/stock-trend/scripts/backtesting/engine.py --lookback-days 120 --eval-windows 5,10,20
```

维科夫回测用于验证候选买点奖励和分级证据，单独执行：

```bash
python3 .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py --from-candidates <candidates.json> --output-html
```

回测为研究工具，不更改候选门槛或奖励。证据不足时只能继续积累，不能据此自动放大买点奖励。

## 退役的独立扫描入口

龙虎榜跟踪、同花顺热力、整合扫描、龙头扫描、市场主线独立报告及独立选股入口不再属于日常路由。其代码按 [清理计划](superpowers/plans/2026-09-12-stock-trend-scope-cleanup.md) 分阶段退役；历史设计记录继续保留在 Git 中。共享数据 fetcher 或候选内部依赖是否保留，以实际生产调用关系为准，不因入口退役而一并删除。
