# 今日推荐重复扫描最小修复方案

状态：已实施（2026-09-29）。

## 目标与范围

减少盘后正式扫描中同一股票的 K 线获取与维科夫分析重复工作，同时保持候选资格、排序、分桶、来源证据及正式快照决策不变。仅增加单次 `daily_candidates.main()` 生命周期内的内存复用；不增加依赖，不改评分权重、资金/基本面补充策略或持久化缓存。

## 现状依据

- `daily_candidates.py:2425`：板块扩展的每个批次调用 `run_phase2(..., defer_enrichment=True)`，仍完成 K 线和维科夫分析。
- `daily_candidates.py:2568-2605`：全局 `top + CAPITAL_TOPUP_LIMIT` 范围再次调用 `run_phase2()`，用于统一资金/基本面补充和最终评分。
- `stock_scanner.py:2331-2484`：`run_phase2()` 每次新建 `kline_data`、`analysis_by_ts`，没有单次运行内的复用入口。
- `stock_scanner.py:2890-2969`：最终分数依赖资金、基本面和板块上下文，不能直接复用首轮完整评分结果。
- `source_health.py:20-23`：扫描有 450 秒总预算和 110 秒 K 线阶段预算。

## 实施步骤

1. **先锁定行为。** 在 `tests/test_daily_candidates.py` 和必要的 `tests/test_stock_scanner.py` 中用固定 K 线/资金/基本面夹具记录当前两阶段的候选代码顺序、质量分、分桶、K 线来源证据和快照决策摘要。覆盖同股出现在多个板块、缓存失效、盘中扫描与阶段二补充资金后排名变化。
2. **增加单次运行的可选工件字典。** 由 `daily_candidates.main()` 创建，传给板块扩展与全局补充；`run_phase2()` 仅在盘后正式模式、相同 `ts_code` 与 `as_of_date`、首轮 K 线通过现有可用性检查且无刷新错误时，保存并复用截断后的 K 线、原始来源证据及 `analyze_kline_dict()` 的结果。失败、过期、不完整或未知日期的工件走现有获取路径。复用时不再次记一次 live 请求或文件缓存命中。
3. **保留第二轮业务逻辑。** 全局补充仍运行资金/基本面探测、优先队列、数据质量评估、六维评分、板块重绑定与最终排序；只跳过可复用的 K 线获取和维科夫分析。新增 `kline_reused_count`、`wyckoff_reused_count` 供性能审计，不改变正式资格字段。

## 验收标准

- 在固定夹具中，可复用股票第二轮的 K 线获取和维科夫分析调用次数均为零；缺失或失效工件仍执行原路径。
- 新旧实现的正式候选代码与顺序、`quality_adjusted_score`、推荐分桶、来源证据和决策摘要一致；资金/基本面更新仍可改变最终评分和排序。
- 盘中扫描保持原路径；失败或过期 K 线不会因复用而获得推荐资格。
- 性能审计能区分实际 provider 调用、文件缓存命中和本轮内存复用；用固定夹具比较两阶段耗时，并报告节省量，不以外部接口波动作为唯一验收依据。

## 验证

先运行新增目标测试，再运行 `test_daily_candidates.py`、`test_stock_scanner.py`、`test_daily_recommendation_performance.py`。修改脚本后必须运行仓库门禁：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

若正式快照或 golden 输出发生变化，先定位差异；本修复不以更新快照消除失败。`git diff --check` 作为最终静态检查。

实施验证：固定夹具确认第二轮对有效股票跳过重复 K 线请求和维科夫分析，资金数据仍能改变重算后的分数；过期 K 线继续重试，买点门槛失败的分析不保留在内存。主门禁 680 passed / 0 failed / 1 skipped；Golden 21 passed / 0 failed / 2 warnings。未做实时全市场耗时对比，线上节省量以报告中的本轮复用计数和阶段耗时继续观察。

## 风险与边界

盘中 K 线可能在两次调用间变化，因此本方案只对盘后正式扫描启用。工件只保存在当次进程内，不跨日期、不跨运行；现有持久化缓存与失败重试逻辑保留。若实测重复工作主要来自资金/基本面，另行评估，不扩大本次改动。
