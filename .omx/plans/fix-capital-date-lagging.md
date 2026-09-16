# 修复资金日期滞后误阻断与日期审计缺失

## Requirements Summary

- 修复单只候选资金数据过期后，将整个 `capital` 数据源立即置为 `date_lagging`，从而跳过后续候选实时获取的问题。
- 保持现有安全门槛：未覆盖目标交易日的资金数据仍不得计为 fresh、不得补足数据资格、不得进入正式推荐。
- 在资金 fetcher、调度证据、候选快照和报告中保留并展示：目标交易日、各回退源最新日期、失败链、是否实际调用、因何跳过。
- 区分“单标的日期滞后”和“已证实的数据源整体日期滞后”，避免候选级异常污染运行级状态。
- 保持今日推荐总预算、并发上限、缓存规则和正式推荐策略不变。

## Acceptance Criteria

1. 某一股票返回 `stale_data` 后，后续候选仍可在预算内申请资金实时请求；单次候选失败不得直接令 `capital` 进入全局 `date_lagging`。
2. 只有带明确运行级证据的日期滞后才能阻断后续请求：至少包含 `expected_date`、`latest_date`、影响范围和证据来源；不得仅凭通用 `reason=stale_data` 阻断。
3. 混合结果场景（部分股票资金数据覆盖目标日、部分股票落后）最终不得报告为全源 `date_lagging`；成功候选保持资金维度 fresh，失败候选单独失格。
4. 真正过期的候选资金数据继续满足：`available=false`、`fresh=false`、`eligible=false`，且 `freshness_factor` 不高于当前安全值 `0.5`。
5. 对已调用且过期的候选，报告明确显示“资金要求日期 YYYY-MM-DD、实际最新日期 YYYY-MM-DD、来源、接口已调用”。
6. 对被运行级阻断而未调用的候选，报告明确显示“未调用”，并附触发阻断的结构化日期证据；不再只显示空 `data_date` 与 `source_date_lagging`。
7. 推荐快照保留上述日期证据，后续只读分析无需依赖 stderr 或临时日志即可还原问题。
8. 现有资金回退顺序、总扫描时限、并发限制、有效缓存判定及弱市/板块持续性门槛不发生行为变化。
9. 两个仓库强制质量门通过：`test_stock_trend.py` 与 `test_golden.py --diff`；不得通过更新 golden 掩盖非预期输出变化。

## Implementation Steps

### 1. 扩充资金回退的结构化日期证据

修改 `.claude/skills/stock-trend/scripts/fetchers/capital_flow.py`。

- 在现有回退链构建处（约 273-380 行）为每个 provider failure 保存：
  - `source`
  - `reason`
  - `expected_date`
  - `latest_date`
  - `scope="item"`
- 对东方财富、Tushare、K 线估算分别调用现有 `latest_capital_date()`，不再只把具体日期写入不可机器消费的错误字符串。
- 在最终错误 `meta` 中新增稳定字段，例如 `expected_date`、`latest_date`、`date_lag_evidence`；保留现有 `error_type`、`stale_sources` 和 `failure_chain` 以兼容现有消费者。
- 不缓存失败或过期结果，沿用当前仅缓存通过 `is_valid_capital_result(..., min_date=expected_date)` 的行为。

依据：[capital_flow.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/fetchers/capital_flow.py:273)

### 2. 将候选级过期与数据源级过期分开建模

修改 `.claude/skills/stock-trend/scripts/core/source_health.py`。

- 扩充 `live_attempt()` 的可选证据字段，使日期、范围和失败链可完整传递到调度层。
- 将当前“任何 `stale_data` 立即置 `state=date_lagging`”的逻辑改为按 scope 处理：
  - `scope=item`：记录失败与日期证据，但保持源可继续调度；状态最多为 `degraded`，不得阻断后续不同股票。
  - `scope=source`：只有 adapter 明确提供数据源整体日期能力证据时，才置为 `date_lagging`。
- 若保留自动推断全源滞后，必须采用保守门槛：多个不同标的、相同目标日期、全部可用 provider 都停留在同一较早日期，且本轮没有目标日期成功记录；具体阈值写成命名常量并由测试锁定。
- 在 `snapshot()` 和阻断事件中保存 `expected_date`、`latest_date`、触发样本数及证据来源，供报告解释。
- 保持 timeout、DNS、HTTP 等瞬时错误的现有降级/熔断逻辑不变。

依据：[source_health.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/source_health.py:328)、[source_health.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/source_health.py:500)

### 3. 贯通 fetcher → 调度器 → 候选证据

修改 `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`。

- 在 `_evidenced_fetch` / `_normalize_source_evidence` 路径中完整保留 fetcher 返回的日期字段，而不是只压缩为 `reason=stale_data`。
- `_consume_live_results()` 对已调用过期结果记录 `status=item_date_lagging`（或等价稳定码）、`attempted=true`、实际最新日期与目标日期。
- 只有真正的运行级阻断使用 `source_date_lagging` 和 `attempted=false`。
- `bounded_source_map()` 留在当前预算和并发边界内；混合成功/过期批次应继续消费后续队列，不能因单标的过期提前结束资金增强。
- 性能汇总分别统计：候选级日期滞后数、源级阻断数、有效资金数，避免 `{"stale_data":8}` 同时承担多种含义。

依据：[stock_scanner.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/stock_scanner.py:3015)

### 4. 让质量模型保存各维度自己的目标日期

修改 `.claude/skills/stock-trend/scripts/core/recommendation_quality.py`。

- 在 `dimensions.capital` 中保存独立的 `expected_date=capital_expected_date`，不要只依赖顶层 K 线/推荐基准日 `as_of_date`。
- 对过期 payload 保存其 `data_date/latest_date`，即使该 payload 因过期而不可用于评分。
- 继续将过期资金判为 unavailable/fresh=false，并维持当前 coverage、freshness 和 eligibility 约束。
- 确保“未调用”“已调用但过期”“缓存过期”“源整体滞后”产生不同、稳定的原因码。

依据：[recommendation_quality.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/recommendation_quality.py:143)

### 5. 改进报告与快照诊断文本

修改 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`。

- 扩展 `_reason_detail()`，资金日期异常固定显示：目标日期、实际最新日期、provider、调用状态、调度阶段和失败链。
- 对候选级过期使用“资金数据最新至 X，早于要求 Y”；对源级跳过使用“前序证据确认数据源最新至 X，本候选未调用”。
- 在报告顶部数据源健康表中拆分候选级过期和源级阻断计数。
- 保证 `source_evidence` 和日期字段进入推荐快照及研究快照，使历史报告可审计。
- 不改变 `single_day_pulse`、`regime_weak` 等独立策略原因的判定或文案。

依据：[daily_candidates.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2268)

### 6. 增加回归测试，锁定安全与调度行为

扩充以下测试文件：

- `.claude/skills/stock-trend/tests/test_capital_flow.py`
  - 三个回退源分别过期时保存精确 `expected_date/latest_date`。
  - 混合 empty/stale/error 时失败链日期与来源不丢失。
  - 过期结果仍不写缓存。
- `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py`
  - 单个 item-scoped `stale_data` 不阻断下一候选。
  - 明确 source-scoped 日期滞后才阻断。
  - 4 个成功、8 个过期的并发混合批次仍继续调度后续可成功候选。
  - 请求数、provider attempts、item lag、source block 计数一致。
- `.claude/skills/stock-trend/tests/test_recommendation_quality.py`
  - 过期资金保留实际日期但仍不可用，freshness 继续降级。
  - `capital_expected_date` 与 K 线 `as_of_date` 不同时分别正确判断，覆盖盘中使用上一完成交易日的情形。
- 候选报告相关测试
  - 已调用过期和未调用阻断两种文案可区分。
  - JSON/快照包含完整日期证据，HTML 与 Markdown 呈现一致。

现有测试基线：[test_capital_flow.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/tests/test_capital_flow.py:87)、[test_daily_recommendation_performance.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py:134)、[test_recommendation_quality.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/tests/test_recommendation_quality.py:115)

## Verification Steps

1. 运行资金 fetcher 定向测试，确认失败元数据包含精确日期且失败结果不缓存。
2. 运行调度器定向测试，确认单标的过期不造成全源阻断，显式 source-scoped 证据仍可阻断。
3. 运行候选质量与报告定向测试，确认日期展示、原因码和资格门槛一致。
4. 使用固定 fixture 做一次今日推荐 dry/integration 回放：目标日 2026-09-16，混合 9 月 16 日成功与 9 月 15 日过期资金，验证后续 fresh 候选仍被调用并获得资格。
5. 执行仓库强制质量门：
   - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
6. 仅当预期文案变化导致 golden 差异时逐项审核；不得直接重生成快照消除失败。
7. 最后运行 `git diff --check` 并检查变更仅覆盖资金日期证据、调度判定、报告诊断和对应测试。

## Risks and Mitigations

- **请求量上升**：取消单标的全源阻断后可能发出更多资金请求。继续受现有 prefetch/top-up、并发和 deadline 控制；测试锁定最大请求数。
- **过期数据被误用**：保留日期仅用于审计，质量判定仍要求覆盖 `capital_expected_date`，测试锁定 unavailable/fresh=false。
- **错误码兼容性**：保留 `source_date_lagging` 给真正的源级阻断，新增 item 级状态时同步更新 schema、报告映射和消费者测试。
- **并发顺序导致状态不稳定**：测试使用不同完成顺序覆盖 success→stale、stale→success 和交错返回，最终状态必须确定。
- **报告体积增加**：候选行只展示摘要；完整 provider 日期链保留在 JSON/快照的结构化证据中。
- **盘中日期规则回归**：单独测试盘中资金目标为最近已完成交易日、收盘后目标为推荐日，不修改现有日期解析函数。

## Stop Condition

当上述验收标准全部通过、两项强制质量门无非预期差异、一次混合日期集成回放证明后续 fresh 候选不会被早期 stale 候选误阻断时，修复可视为完成。
