# 盘后推荐固定板块范围实施计划

## Goal

保证同一交易日的盘后候选扫描使用固定的板块范围，完整处理该范围后再进行最终筛选，避免因为缓存命中、接口耗时或候选数量提前达标而产生不同候选名单。

本计划默认采用“盘后固定扫描热度合格板块前 120 个”的范围策略。120 是当前运行时和覆盖率之间的折中；后续可以把同一机制切换为全部合格板块，但不能恢复按候选数量动态停止。

本计划只解决盘后扫描范围一致性，不改变市场环境评分、维科夫条件、质量调整分、买点奖励或弱市推荐门控。

## Observed incident

2026-09-09 的两次报告使用相同的市场环境数据，但扫描范围不同：

| 指标 | 21:53 报告 | 22:19 报告 |
|---|---:|---:|
| 实际展开板块 | 120 | 60 |
| 原始/去重候选 | 913/649 | 407/407 |
| 板块成分尝试覆盖率 | 58% | 29% |
| 最终候选 | 30 | 30 |
| 市场评分 | 34.1，弱势 | 34.1，弱势 |

根因是 `scan_sectors()` 在有效候选达到 `min_candidates` 后提前结束。首轮窗口为 60，某次运行在首轮达到门槛即可停止；另一轮未达到门槛则继续扩展到 120。不同的实时/缓存数据状态还会改变“有效候选”数量，进一步放大范围漂移。

## Target behavior

盘后正式流程固定为：

```text
市场刷新
  → 固定板块排行快照
  → 固定前120板块范围
  → 完整扫描120个板块
  → 统一排序和质量筛选
  → 生成唯一正式结果
```

盘后正式模式必须满足以下约束：

- 先确定并记录板块范围，再启动成分股扫描；
- 无论前 60 个板块已经产生多少候选，都必须完成固定范围；
- 范围内所有批次使用相同的 `as_of_date`、参数和排序规则；
- 未完成固定范围时只能输出未定稿/降级报告，不能作为正式盘后结果；
- 同日重跑应读取同一正式输入或正式结果，不重新决定扫描范围。

## Implementation tasks

### 1. 增加盘后正式扫描模式

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

新增内部扫描模式，例如 `scan_mode="post_close_final"`。正式模式固定：

```python
scope_size = min(120, len(qualified_sectors))
scope = qualified_sectors[:scope_size]
```

要求：

- 按 `ranking_position`、`sector_code` 做稳定排序；
- `scan_sectors()` 在正式模式下不执行 `eligible_count >= min_candidates` 的提前 `break`；
- `initial_sector_window` 和 `sector_expansion_step` 只影响探索模式，不影响正式范围；
- 完成批次数量必须等于冻结范围数量；
- 保留 `max_sector_expansion` 作为固定上限，并在报告中明确显示实际范围是前 120 个，而不是全量 207 个；
- 对空板块、重复板块和成分股重复按代码稳定去重，不能依赖请求返回顺序。

不要通过简单地把 `min_candidates` 调大来规避提前停止。正式模式必须显式表达“范围完整性”，否则未来参数变化仍可能重新引入问题。

### 2. 固定正式入口的参数

修改：

- `.claude/skills/stock-trend/scripts/bridge/run_today.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

`run_today.py` 在交易日 15:10 后调用候选扫描时传入正式模式，并固定记录：

- `as_of_date`；
- `scan_mode=post_close_final`；
- `scope_policy=top_120`；
- `scope_expected`；
- `top`、`min_candidates`、`min_score`；
- `per_sector`、批次大小、维科夫开关；
- 当前策略版本和买点奖励参数。

盘中和人工探索调用继续使用现有自适应扩展逻辑，但必须标记为 `provisional` 或 `exploratory`，不能与盘后正式结果混用。

### 3. 增加范围完成度审计

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/reporting/`（如现有报告渲染逻辑需要拆分）

新增或统一以下审计字段：

```json
{
  "scope_policy": "top_120",
  "scope_expected": 120,
  "scope_completed": 120,
  "scope_failed": 0,
  "scope_complete": true,
  "scope_codes_sha256": "..."
}
```

报告必须同时展示：

- 合格板块总数；
- 正式固定范围数量；
- 已完成数量；
- 失败批次数量及代码；
- 是否提前停止；
- 是否允许正式发布。

`scope_completed < scope_expected` 时，报告只能标记为 `degraded` 或 `not_final`，不能显示为完整正式盘后扫描。

### 4. 固定范围内的失败处理

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/core/source_health.py`（如需补充批次状态）

对固定范围内的每个批次记录：

- 批次序号；
- 板块代码列表；
- 请求开始/结束时间；
- 成功、失败、超时数量；
- 失败原因；
- 是否使用同日有效缓存。

出现超时或接口失败时：

- 允许在正式截止时间内按同一固定批次重试；
- 重试仍失败则保留失败清单；
- 不得跳过失败板块并宣称范围完成；
- 不得因为已有足够候选而提前结束；
- 正式结果降级为未定稿，观察结果可以继续生成。

### 5. 固定排序和候选合并

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

在所有固定范围批次完成后再执行最终合并：

- 股票代码作为去重主键；
- 多板块归属按稳定规则合并；
- 主归属板块使用固定的板块评分、排名和代码作为确定性排序键；
- 相同分数使用股票代码作为最终 tie-breaker；
- 不使用异步完成顺序、缓存命中顺序或请求返回顺序作为排序因素。

候选选择应保持以下顺序：

```text
完整固定范围
  → 全部候选合并
  → 数据质量过滤
  → 综合分排序
  → 买点优先级排序
  → 三层分桶
```

不能在单个板块窗口内先截取 Top-N，再用窗口结果代替全范围结果。

### 6. 快照与报告绑定

修改：

- `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/bridge/run_today.py`

正式快照应记录范围契约和输入指纹：

- 市场环境内容 hash；
- 板块排行内容 hash；
- 固定板块代码列表 hash；
- 成分股集合 hash；
- 关键输入数据日期；
- 模型、参数和策略版本；
- `scope_complete` 和 `scan_status`。

HTML、MD、候选 JSON 和正式历史快照必须来自同一份最终对象。若正式快照已经存在，同日重跑默认读取并回放该快照，不再重新计算板块范围。

探索性重跑必须使用单独的草稿输出路径，并明确标记 `draft`，不能覆盖正式结果。

### 7. 兼容性与配置迁移

保留现有参数以兼容外部调用：

- `--max-sector-expansion` 继续可用；
- `--sectors` 手动板块模式继续可用；
- 盘中/测试调用仍可使用自适应扩展；
- 原有 JSON 字段继续保留。

新增字段应只扩展 JSON，不删除现有字段。建议将新增参数写入 `parameter_summary`，便于历史任务和演进流程识别正式范围。

## Tests

新增或更新：

- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_run_today.py`
- `.claude/skills/stock-trend/tests/test_recommendation_snapshot.py`
- 如范围审计拆出独立模块，新增对应单元测试。

必须覆盖：

1. 前 60 个板块已经达到 `min_candidates`，正式模式仍扫描完整 120 个板块。
2. 前 60 个板块不足门槛，正式模式仍只扫描冻结的 120 个板块，不动态扩到其他数量。
3. 同一固定板块列表重复运行，范围 hash 完全一致。
4. 异步批次完成顺序变化时，最终候选顺序不变。
5. 一个板块批次失败时，`scope_complete=false`，不能发布正式结果。
6. 固定范围内出现缓存命中或实时成功差异时，范围和排序仍保持确定。
7. `run_today.py` 的盘后入口传入正式模式，盘中入口不误用正式模式。
8. 同日已有正式快照时，重跑走回放路径，不重新扫描。
9. 草稿重跑不会修改正式快照或正式推荐历史。
10. 旧版调用方只消费原有字段时仍能正常解析。

## Verification

实现后按仓库质量门执行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

另需进行不联网的确定性回放测试：

1. 使用固定 fixture 连续运行两次；
2. 对调批次完成顺序；
3. 改变缓存命中标记但不改变冻结输入内容；
4. 验证候选代码、分数、排序、分桶和 `decision_hash` 全部一致。

Golden 快照只有在确认报告字段变化是本计划预期结果后才更新，不能通过重新生成快照掩盖失败。

## Rollout

分三步上线：

### Shadow

连续 3 个交易日并行记录旧模式和固定范围模式，但固定范围模式只生成影子结果，不改变正式推荐。

每日检查：

- 120/120 范围完成率；
- 批次失败和超时；
- 候选代码交集；
- 分数差异；
- 异步顺序重排后的结果稳定性；
- 扫描耗时和缓存命中率。

### Enforce

连续 3 日满足以下条件后，盘后入口切换到正式模式：

- 固定范围完成率 100%；
- 无未解释的批次失败；
- 同一输入回放结果一致；
- 正式报告均记录完整范围审计字段。

### Rollback

如果正式模式连续出现资源不足或接口异常：

- 保留固定范围配置和失败证据；
- 暂时只输出未定稿观察报告；
- 不回退到动态提前停止并发布正式结果；
- 修复资源或接口后从同一交易日输入快照重试。

## Acceptance criteria

本计划完成的判定标准：

- 同一盘后输入下，重复运行候选代码、评分和排序完全一致；
- 盘后正式扫描始终完成固定 120 个板块后才筛选最终结果；
- 前 60 个板块提前达到候选数量不会改变扫描范围；
- 范围未完成时不会产生可被误认为正式推荐的结果；
- HTML、MD、JSON 与正式快照来自同一决策对象；
- 同日重跑可回放，不再因实时/缓存状态产生第二套正式候选名单。

本计划仅用于改进数据一致性，不构成任何投资建议。股市有风险，投资需谨慎。
