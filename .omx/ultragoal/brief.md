# 今日推荐数据完整性修复计划

## 目标

修复 2026-09-14 盘中运行中暴露的三项数据完整性问题：市场环境刷新以空日期上下文覆盖有效收盘上下文；候选扫描与入口使用不同评价日期；资金增强在上游陈旧后只暴露“未调用”而未完整解释熔断前的失败链。保持现有安全语义：不完整、陈旧或来源不可用的数据不得产生可执行推荐。

本计划不放宽数据资格阈值、不把缓存或估算资金数据提升为实时数据、不改变评分权重或新闻影子层，也不重写历史报告。

## 现状证据

- `market_regime.collect_context()` 在没有有效指数日期时设置空 `data_date`，仍组装并返回上下文：[market_regime.py:960](../../.claude/skills/stock-trend/scripts/analysis/market_regime.py#L960)、[market_regime.py:1071](../../.claude/skills/stock-trend/scripts/analysis/market_regime.py#L1071)。其 CLI 会无条件 `save_context(ctx)`：[market_regime.py:1170](../../.claude/skills/stock-trend/scripts/analysis/market_regime.py#L1170)。
- 今日入口仅校验子进程输出和磁盘内容彼此一致，不校验市场上下文是否有可用于目标交易日的有效日期和质量；随后立即扫描候选：[run_today.py:64](../../.claude/skills/stock-trend/scripts/bridge/run_today.py#L64)、[run_today.py:208](../../.claude/skills/stock-trend/scripts/bridge/run_today.py#L208)。
- 入口在候选扫描之后才从权威交易日历计算 `as_of`：[run_today.py:223](../../.claude/skills/stock-trend/scripts/bridge/run_today.py#L223)；候选 CLI 则独立基于本机时间推导 `expected_date`：[daily_candidates.py:3953](../../.claude/skills/stock-trend/scripts/scans/daily_candidates.py#L3953)。因此盘中可能发生“入口按上一完整交易日后处理、扫描按今日要求数据”的日期漂移。
- 正式策略发现 `regime.data_date != expected_date` 时会进入观察模式且上限为零：[daily_candidates.py:2975](../../.claude/skills/stock-trend/scripts/scans/daily_candidates.py#L2975)。板块缓存只有日期、完整性和提供方均与评价日匹配才是 `same_day_verified`，否则标为 `degraded`：[daily_candidates.py:1640](../../.claude/skills/stock-trend/scripts/scans/daily_candidates.py#L1640)。
- 资金 fetcher 对东方财富、Tushare 和 K 线估算均要求覆盖 `expected_date`，过旧数据被统一归类为 `stale_data`：[capital_flow.py:304](../../.claude/skills/stock-trend/scripts/fetchers/capital_flow.py#L304)、[capital_flow.py:333](../../.claude/skills/stock-trend/scripts/fetchers/capital_flow.py#L333)、[capital_flow.py:360](../../.claude/skills/stock-trend/scripts/fetchers/capital_flow.py#L360)。连续失败达到硬阈值后，运行级状态转为 `unavailable`：[source_health.py:341](../../.claude/skills/stock-trend/scripts/core/source_health.py#L341)。

## 决策

建立单一、显式的 `as_of` 合同：入口在任何行情请求前依据权威交易日历确定评价日，并把它传给市场环境与候选扫描。市场环境只允许原子替换为“可覆盖该评价日且质量可用”的上下文；刷新失败时保留已验证的同日上下文，否则中止候选扫描。资金熔断仍保留，但在报告和 JSON 中同时展示“熔断前已发起的失败”和“熔断后未调用的队列”，避免把调度结果误解为根因。

## 实施步骤

### 1. 先锁定日期与上下文契约的回归测试

修改：

- `.claude/skills/stock-trend/tests/test_run_today.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_market_explanation.py`

增加固定时钟和交易日历 fixture，覆盖：

1. 15:10 前入口确定上一已完成交易日，并将同一个 `as_of` 传给市场刷新、候选扫描与后台任务。
2. 市场刷新返回空日期、`missing`、`unknown` 或 `partial` 上下文时，入口不调用候选扫描；工作流明确为 `market_context_invalid`，而不是 `market=completed`。
3. 若磁盘已有同一 `as_of` 的完整、已验证市场上下文，失败刷新不得覆盖它；入口可使用该上下文继续生成仅由原有门控决定的报告。
4. 已有上下文日期不匹配时，入口跳过扫描并标记陈旧；不得把旧上下文静默当成新上下文。
5. 候选 CLI 接到 `--as-of 2026-09-11` 时，政策、板块缓存、K 线和资金的新鲜度判断均使用该日期，不再依赖本机当前日期。

验收：这些测试先失败，且既有 `market_context_not_persisted`、`market_refresh_failed` 和 `regime_stale` 测试继续表达原有安全边界。

### 2. 将评价日期提升为入口与扫描的显式参数

修改：

- `.claude/skills/stock-trend/scripts/bridge/run_today.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- 必要时 `.claude/skills/stock-trend/scripts/analysis/market_regime.py`

实施：

1. 在 `run_today()` 开始、市场子进程之前调用 `_completed_sessions(now)`；无可用权威交易日历时跳过市场和候选，并返回清晰的 `calendar_unavailable` 状态。
2. 用入口得到的最近完成交易日作为唯一 `as_of`；盘中不得让候选扫描自行把当天当作已完成收盘日。
3. 为 `daily_candidates.py` 增加 `--as-of YYYY-MM-DD`，严格校验 ISO 日期和交易日历覆盖。指定时覆盖其内部时间推导；未指定时保留现有独立 CLI 行为。
4. 入口调用候选扫描时追加 `--as-of <as_of>`。`workflow.as_of` 从一开始即写入，市场、候选、板块缓存和后台 manifest 都引用同一值。
5. 市场环境为评价日增加受限接口或内部 helper：返回的上下文必须带有 `data_date`、`data_quality` 和评价日匹配证明。若市场环境只支持实时采集，则该 helper 必须在盘中选择上一完整收盘上下文，而不伪造当日日期。

验收：在 2026-09-14 09:50 且日历最后完成日为 2026-09-11 的 fixture 中，候选扫描收到 `--as-of 2026-09-11`；同日东方财富完整板块缓存显示 `same_day_verified`，不再因入口/扫描日期漂移被降为 `degraded`。

### 3. 防止空或低质量市场刷新破坏已验证上下文

修改：

- `.claude/skills/stock-trend/scripts/analysis/market_regime.py`
- `.claude/skills/stock-trend/scripts/bridge/run_today.py`
- `.claude/skills/stock-trend/tests/test_run_today.py`
- `.claude/skills/stock-trend/tests/test_market_explanation.py`

实施：

1. 提取 `validate_market_context(ctx, expected_date)` 纯函数，返回结构化 verdict：`valid`、`reason`、`data_date`、缺失/部分组件和上下文质量。至少要求非空严格日期、与 `expected_date` 一致、完整度不是 `missing/unknown/partial`，以及 `market_explanation.blocking_reasons` 不含数据缺失类原因。
2. `market_regime` 在写入前执行该验证。无效实时结果不得覆盖缓存；保留原缓存时，在 JSON 中返回 `refresh_status=preserved_verified_context` 或 `refresh_status=invalid_no_fallback`，包含实时采集的结构化失败原因。
3. 使用原缓存的前提是：缓存通过同一 validator、日期等于 `expected_date`，且能证明为完整上下文；不能用更早日期或仅有分数的旧缓存。
4. `run_today._run_script()` 除原有原子持久化比对外，还要验证返回/保留的上下文对入口 `as_of` 可用。失败原因需区分 `market_context_not_persisted`、`market_context_invalid`、`market_context_stale`，并保持候选扫描跳过。
5. 报告的市场栏写明“使用已验证收盘上下文”或“市场刷新缺口，候选扫描已跳过”；不再把空上下文显示为普通“completed”。

验收：网络失败后，原有 2026-09-11 完整上下文保持字节内容不变；空上下文没有备份时不写入并且不扫描；有同日验证缓存时可继续报告，报告显示缓存使用和来源日期。

### 4. 让资金失败链与熔断后的调度状态同时可审计

修改：

- `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py`
- `.claude/skills/stock-trend/scripts/core/source_health.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_capital_flow.py`
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`

实施：

1. 保留每次资金 fallback 的 `failure_chain`，为 `stale_data` 增加每个来源的 `latest_data_date`（未知时显式为 `null`），不得只保留字符串错误。
2. `RunSourceHealth` 在 `circuit_opened` 事件记录触发它的失败原因、已启动请求数、失败次数和目标评价日期；不改变现有失败阈值或并发上限。
3. 汇总指标拆分为 `capital_live_failure_reasons`、`capital_scheduler_omissions`、`capital_stale_sources` 与 `capital_circuit`。保留既有 `capital_failure_reasons` 以维护 JSON 兼容，但它只统计已实际尝试的 provider 失败。
4. 单候选诊断继续将 `source_unavailable` 表达为“本候选未调用”；报告级数据源审计新增一句因果摘要，例如“资金源在 9 次 `stale_data` 后熔断；其后 N 个队列项未调用”。
5. 如所有首批资金请求都是同一 `expected_date` 的 `stale_data`，只在证据证明所有 fallback 均返回同一旧截止日期时，允许增加一次运行级“该评价日不可获得”的短路；默认先只记录证据，不以猜测减少探测。

验收：构造东方财富、Tushare、K 线估算均只到 T-1 的 fixture；JSON 同时显示目标日期、各 fallback 最新日期、实际失败次数、一次熔断和后续未调用数量。现有“未调用不计入 provider failure”测试保持通过。

### 5. 收紧板块缓存的来源与日期展示

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py`

实施：

1. 将 `ranking_provenance` 标准化为 `requested_as_of`、`selected_data_date`、`source`、`provider`、`complete`、`same_day_verified`、`quality`、`live_failure_reason`。
2. 只有缓存元数据明确 `complete=True`、`provider=eastmoney` 且日期等于 `requested_as_of` 时标为 `same_day_verified`；这类缓存允许作为收盘评价日的板块证据。其他缓存维持 `degraded` 或 `cross_source_observation`，且不会产生可执行推荐。
3. 报告将“实时拉取失败”与“选用的缓存质量”分别渲染，避免“来源 cache”被理解为缓存本身不可信。
4. 覆盖盘中（上一完整交易日缓存）、盘后（当日完整缓存）、周末/节假日（最近完成交易日缓存）和缓存日期不匹配四个矩阵。

验收：同一完整东方财富缓存对正确 `as_of` 输出 `same_day_verified`；仅当日期/完整性/提供方任一不符时才输出 `degraded`；分类和推荐资格不因展示字段变化而放宽。

### 6. 端到端验证与输出契约

运行：

```bash
python3 .claude/skills/stock-trend/tests/test_run_today.py
python3 .claude/skills/stock-trend/tests/test_capital_flow.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_market_explanation.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
make check
```

再运行三组无公网 fixture：

1. 盘中，评价日为上一完整交易日，验证入口/市场/扫描/板块缓存日期一致。
2. 市场实时采集空结果，验证同日完整上下文被保留；无备份时候选扫描跳过。
3. 资金三层 fallback 陈旧，验证所有根因、熔断和后续调度遗漏可同时审计，且可执行推荐仍为零。

不更新 golden，除非输出字段的变化经过逐项审查、属于计划内契约变更；届时在提交说明写明原因。

## 风险与缓解

- 盘中语义变化可能影响已有使用者：只由统一入口强制 `--as-of`；独立候选 CLI 保留默认推导，且新增参数为可选。
- 保留旧市场上下文可能掩盖过期数据：只允许同一权威评价日且通过完整性验证的缓存；否则硬跳过。
- 记录资金失败链可能增加 JSON 体积：保留每来源最后一次/汇总证据，候选级明细限制为已有 top 候选，完整原始响应不写入报告。
- 缓存日期正确却仍有业务质量风险：`same_day_verified` 只证明来源与日期资格，维持现有市场分、资金、板块持续性和候选数据质量门控。

## 完成标准

1. 今日推荐一次运行只有一个可追溯的 `as_of`，并贯穿市场、候选、资金、板块与后处理。
2. 无效市场刷新不能覆盖有效上下文，也不能被标成 `market=completed` 后继续扫描。
3. 资金报告能区分“上游返回陈旧数据导致的已调用失败”与“熔断后的未调用”，并展示来源数据截止日。
4. 对正确评价日的完整东方财富板块缓存不再误标 `degraded`。
5. 所有定向测试、两项强制质量门和 `make check` 通过。
