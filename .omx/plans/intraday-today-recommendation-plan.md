# 盘中“今日推荐”计划

## 目标与范围

当上海时区的**今日**在权威交易日历中且尚未达到 15:10 时，`run_today.py` 仍以今日为候选计划日期，刷新今日市场上下文并运行候选扫描。输出必须是“盘中临时计划”，不能生成正式推荐快照、训练样本或对今日执行收盘后的评价任务。

收盘确认阈值、市场/候选数据质量门控、弱市不推荐规则、正式固定范围扫描和历史评价均保持不变。

## 已确认事实

- 统一入口 `_completed_sessions()` 在 15:10 前排除今日，随后 `run_today()` 将其最后日期同时用作扫描日期和历史评价日期（`.claude/skills/stock-trend/scripts/bridge/run_today.py:88-100`, `:241-301`）。这正是盘中仍扫描昨收的原因。
- 市场环境已支持同日盘中混合评分和临时提示（`.claude/skills/stock-trend/scripts/analysis/market_regime.py:1113-1317`）。
- 候选扫描已支持临时策略：`market_open=True` 会设置 `policy.provisional`，而正式快照写入器会跳过该快照（`.claude/skills/stock-trend/scripts/scans/daily_candidates.py:3181-3225`, `:3349-3390`）。
- 候选 CLI 目前仅用本机时钟的 09:30–15:00 窗口判断临时状态（`.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1132-1141`, `:4153-4185`）；入口需要显式传递“本次是同日临时计划”的语义，不能只依赖子进程时钟。
- 当前回归测试明确断言盘中使用前一日并执行 close/weekly/monitor（`.claude/skills/stock-trend/tests/test_run_today.py:127-131`），必须由新的双日期断言替换。

## 决策

采用“计划日期”和“已完成评价日期”分离的双日期模型：

- `plan_as_of`：候选和市场环境使用的交易日；若今日在权威日历且未到 15:10，则为今日。
- `completed_sessions` / `evaluation_as_of`：只含已完成交易日；盘中不包含今日，仅供 close、weekly、monitor 等回溯任务使用。
- `session_mode`：`intraday_provisional` 或 `post_close_formal`，写入 `workflow`，让 JSON 和文本摘要能够区分盘中计划与收盘定稿。

## 实施步骤

1. 在 `bridge/run_today.py` 提取单一的交易日解析函数，基于 `_load_authoritative_trading_dates(now)` 同时返回 `plan_as_of`、`completed_sessions`、`coverage_end` 和 `session_mode`。
   - 仅在日历**明确包含今日**且当前时间早于 15:10 时选择 `plan_as_of=today` 与 `session_mode=intraday_provisional`。
   - 非交易日、日历未覆盖今日、或 15:10 后沿用最新已完成交易日和 `post_close_formal`。
   - 保留“日历历史覆盖不足”状态，绝不把缺失日期推断为休市或交易日。

2. 调整 `run_today()` 只用 `plan_as_of` 调用 `market_regime.py --as-of` 和候选扫描；在盘中将明确的 `--provisional`（命名可采用 `--intraday-plan`，但需全链路一致）透传给 `daily_candidates.py`。
   - 盘中不得附加 `--post-close-final`；15:10 后仍自动附加它，保持固定范围正式扫描。
   - 在 `workflow` 中保留兼容性的 `as_of=plan_as_of`，新增 `session_mode`、`provisional`、`evaluation_as_of`、`completed_sessions`，避免调用方误把临时计划解释为正式收盘结果。
   - 实时数据未覆盖今日或关键组件不合格时继续 fail closed：报告降级/观察，不回退到旧上下文生成推荐。

3. 在 `scans/daily_candidates.py` 增加入口传递的临时计划标志，并将它与现有 `is_recommendation_session()` 合并为 `market_open` / `provisional` 判定。
   - 该标志只允许来自桥接入口的当日交易日请求；它使午间、收盘缓冲期或受注入时钟测试的同日计划稳定标记为临时。
   - 保持 `policy.provisional=True`、`snapshot_type=provisional`、`save_snapshot_if_official(...)=skipped_provisional` 和研究样本 `training_eligible=False` 的既有契约。
   - 不修改正式推荐配额、评分、新闻影子层或候选排序。

4. 盘中跳过本次 `close → weekly → monitor` 后处理，并在 `workflow.postprocess` 标记 `deferred_until_close`；不可为未完成交易日创建背景任务或写入当日正式评估。
   - 收盘后的同一入口调用使用 `post_close_formal`，照旧创建正式快照，并执行/启动既有后处理。
   - 文本摘要与候选报告必须显示“盘中临时计划，收盘后需重新运行确认”，而不是把候选包装成收盘定稿或即时交易指令。

5. 更新 `SKILL.md` 的今日推荐日期规则与 README/内部维护文档中对应说明（若存在），明确“盘中使用今日做临时计划；15:10 后使用正式结果；评估任务只处理已完成交易日”。

## 验收标准

- 在 2026-09-09 11:00（该日存在于测试日历）执行入口时：市场和候选均收到 `--as-of 2026-09-09`，候选收到临时标志，不收到 `--post-close-final`。
- 同一盘中运行：`workflow.session_mode == "intraday_provisional"`、`provisional is true`、`evaluation_as_of == "2026-09-08"`；不调用或启动 `close`、`weekly`、`monitor`，并报告 `deferred_until_close`。
- 盘中候选输出保留已有资格门控；正式快照跟踪为 `skipped_provisional`，研究样本不可训练。
- 2026-09-09 16:00 仍以 `--as-of 2026-09-09` 运行固定范围正式扫描，创建/复用正式快照并按当前逻辑处理后处理。
- 周末、工作日休市、日历不可用和仅历史日历覆盖继续选择最后已完成交易日或安全跳过；不会把今天误判为交易日。
- 若今日实时市场上下文缺失、过期或质量不足，候选扫描保持跳过/观察，绝不使用昨日缓存伪装成今日数据。

## 测试与验证

1. 修改 `.claude/skills/stock-trend/tests/test_run_today.py`：将现有盘中昨收断言改为双日期模型，并新增盘中不触发同步/后台后处理、临时参数透传、15:10 边界、休市与日历覆盖回归用例。
2. 修改 `.claude/skills/stock-trend/tests/test_daily_candidates.py`：覆盖显式临时标志在非连续竞价时段仍阻止正式快照，且不改变现有盘中候选分桶与报告提示。
3. 运行强制质量门：

   ```bash
   python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

4. 运行完整检查：

   ```bash
   make check
   ```

不重生成 golden 快照；若输出变化确属产品契约变更，单独审查其内容与原因。

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 盘中数据波动被误当作收盘结论 | 明确 `provisional`、禁止正式快照/训练/当天评价，并要求收盘重跑。 |
| 盘前或数据源尚未发布今日数据 | 只有日历确认今日时才尝试；上下文日期与质量校验继续 fail closed。 |
| 临时运行污染 evolution 统计 | 后处理只接收 `completed_sessions`，盘中返回 `deferred_until_close`。 |
| 旧调用方只读 `as_of` | 保留 `as_of` 作为计划日期，新增显式模式和评价日期字段，并在摘要中提示临时状态。 |

## ADR

- 决策：以双日期模型支持当日盘中临时推荐计划。
- 驱动因素：用户需要交易日当日计划；现有候选与市场层已支持临时语义；正式回测/训练必须只消费收盘确认数据。
- 备选方案：
  - 仅把入口的 `as_of` 改为今日：会错误触发 close/weekly/monitor，且可能让非交易时段生成正式快照。
  - 继续只用昨收：安全但不满足当日计划需求。
- 选择理由：双日期模型最小化改动面，同时保留正式研究数据的时间完整性。
- 后续：实现后用一次交易日盘中和一次 15:10 后的端到端运行核验 workflow 字段、快照状态和后处理任务。
