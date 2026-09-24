# Stock Trend 测试清单（2026-09-24）

盘点范围是清理前除 `test_stock_trend.py` 与 `test_golden.py` 外的 34 个
`test_*.py` 模块。`主门禁` 表示由
`python3 .claude/skills/stock-trend/tests/test_stock_trend.py` 的
`run_daily_recommendation_tests()` 调用；`独立离线` 表示可直接运行且测试使用
fixture、mock 或临时目录；`专项` 表示应显式运行，避免把实时网络或额外测试框架
依赖加入主门禁。

| 模块 | 清理前接入主门禁 | 执行与依赖 | 覆盖入口 | 结论 |
| --- | --- | --- | --- | --- |
| `test_candidate_news.py` | 是 | 主门禁；mock/临时目录 | 候选新闻证据 | 保留在主门禁 |
| `test_candidate_research_snapshot.py` | 否 | `python3 tests/test_candidate_research_snapshot.py`；离线 | 研究快照构建/保存 | 保留为独立离线测试 |
| `test_candidate_trade_plan.py` | 是 | 主门禁；离线 | 候选交易计划 | 保留在主门禁 |
| `test_capital_flow.py` | 是 | 主门禁；mock/临时目录 | 资金流、缓存、预期日期 | 保留在主门禁 |
| `test_daily_candidates.py` | 是 | 主门禁；mock/临时目录 | 每日候选策略、报告、CLI | 保留在主门禁 |
| `test_daily_candidates_syntax.py` | 否 | 单一 `compile()` 断言 | `daily_candidates.py` 语法 | 删除；主门禁导入 `test_daily_candidates` 时已编译同一生产模块 |
| `test_daily_recommendation_performance.py` | 否 | `python3 tests/test_daily_recommendation_performance.py`；离线 | 性能预算与来源健康 | 保留为独立离线测试 |
| `test_evolution_job.py` | 是 | 主门禁；mock/临时目录 | 推荐演进任务 | 保留在主门禁 |
| `test_evolution_storage.py` | 是 | 主门禁；临时目录 | 演进数据存储 | 保留在主门禁 |
| `test_factor_ablation.py` | 否 | `python3 tests/test_factor_ablation.py`；离线 | 因子消融 | 保留为独立离线测试 |
| `test_golden_regressions.py` | 否 | `python3 tests/test_golden_regressions.py`；mock | Golden 差异规则与网络回退 | 保留为 Golden 专项测试 |
| `test_kline_utils.py` | 否 | `python3 tests/test_kline_utils.py`；离线 | K 线载荷与命令构建 | 保留为独立离线测试 |
| `test_market_explanation.py` | 否 | `python3 tests/test_market_explanation.py`；离线 | 市场解释计算 | 保留为独立离线测试 |
| `test_market_explanation_reporting.py` | 否 | `python3 tests/test_market_explanation_reporting.py`；mock/临时目录 | 市场解释渲染与快照边界 | 保留为独立离线测试 |
| `test_market_regime.py` | 否 | `python3 tests/test_market_regime.py`；末尾含实时数据检查 | 市场环境计算、持久化、实时加载 | 保留为显式专项测试 |
| `test_market_shadow_integration.py` | 否 | `python3 tests/test_market_shadow_integration.py`；mock/临时目录 | 风格影子与候选报告集成 | 保留为独立离线测试 |
| `test_market_shadow_snapshot.py` | 否 | `python3 tests/test_market_shadow_snapshot.py`；临时目录 | 风格影子快照原子性 | 保留为独立离线测试 |
| `test_market_style.py` | 否 | `python3 tests/test_market_style.py`；离线 | 风格计算与候选注释 | 保留；覆盖 strong/mixed/weak/unknown 行为锁 |
| `test_observation_list_analysis.py` | 否 | `python3 tests/test_observation_list_analysis.py`；mock/临时目录 | 观察列表分析 | 保留为独立离线测试 |
| `test_pipeline_runner_read_json.py` | 否 | `python3 tests/test_pipeline_runner_read_json.py`；临时目录 | Pipeline JSON 读取边界 | 保留为独立离线测试 |
| `test_recommendation_attribution.py` | 是 | 主门禁；离线 | 推荐归因 | 保留在主门禁 |
| `test_recommendation_diagnostics.py` | 是 | 主门禁；离线 | 推荐诊断 | 保留在主门禁 |
| `test_recommendation_experiments.py` | 是 | 主门禁；临时目录 | 推荐实验 | 保留在主门禁 |
| `test_recommendation_lifecycle.py` | 是 | 主门禁；临时目录 | 推荐生命周期 | 保留在主门禁 |
| `test_recommendation_quality.py` | 是 | 主门禁；离线 | 推荐质量门 | 保留在主门禁 |
| `test_recommendation_snapshot.py` | 是 | 主门禁；临时目录 | 正式推荐快照 | 保留在主门禁 |
| `test_research_events.py` | 是 | 主门禁；离线 | 研究事件 | 保留在主门禁 |
| `test_run_today.py` | 是 | 主门禁；mock/临时目录 | 今日推荐编排 | 保留在主门禁 |
| `test_scores_wyckoff_mode.py` | 否 | `python3 tests/test_scores_wyckoff_mode.py`；本地子进程/临时目录 | 维科夫评分模式 | 保留为独立离线测试 |
| `test_sector_akshare.py` | 否 | `python3 -m pytest -q tests/test_sector_akshare.py`；含实时 AKShare | 板块备选数据源与缓存降级 | 保留为显式网络专项测试 |
| `test_sector_mapping.py` | 否 | `python3 -m pytest -q tests/test_sector_mapping.py`；离线、pytest fixture | 板块映射配置与陈旧缓存 | 保留为显式 pytest 测试 |
| `test_sector_snapshot_job.py` | 否 | `python3 tests/test_sector_snapshot_job.py`；mock/临时目录 | 板块快照任务 | 保留为独立离线测试 |
| `test_stock_scanner.py` | 是 | 主门禁；mock/合成数据 | 扫描、漏斗与数据质量 | 保留在主门禁 |
| `test_wyckoff.py` | 否 | `python3 tests/test_wyckoff.py`；离线 | 维科夫计算与买点判定 | 保留为独立离线测试 |

## 门禁结论

- 清理前有 15 个模块由主门禁点名，19 个未被点名。未接入本身不构成删除依据。
- 主门禁继续保持现有范围，避免加入 `market_regime`、`sector_akshare` 的实时网络波动，
  也避免强制引入 pytest。
- `AGENTS.md` 与 `CLAUDE.md` 都把 `test_stock_trend.py` 和
  `test_golden.py --diff` 列为两条独立质量命令，因此主门禁内部不再重复启动 Golden。
- 独立离线测试保留明确的直接执行命令；功能改动应按覆盖入口选择相应专项测试。

表中的 `tests/...` 路径相对 `.claude/skills/stock-trend/`。
