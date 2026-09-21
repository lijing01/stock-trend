# 今日推荐后自动运行六维分数贡献对照

日期：2026-09-21。状态：实施计划；本计划不修改正式推荐策略。

## 目标与范围

每次通过统一“今日推荐”入口生成盘后正式推荐后，自动在后台用同一次扫描冻结的研究快照运行六维单项消融，并留下可复现的影子排名。`close` 更新前向结果后，自动刷新已成熟样本的配对诊断。盘中临时结果、未关联正式快照以及证据不完整的记录不得进入收益比较。结果只用于研究观察，不改变正式权重、资格门槛、推荐报告结论或发布指针。

当前六维权重为动量 25%、量价 15%、资金 15%、基本面 10%、板块强度 10%、维科夫结构 25%（`scripts/scans/stock_scanner.py:1949-1960`）。现有分数账本记录原始维度、权重、质量因子和买点奖励（`scripts/core/candidate_score_ledger.py:49-134`）；盘后候选扫描会先保存正式快照，再保存含完整阶段二研究总体的冻结研究快照（`scripts/scans/daily_candidates.py:4505-4563`）。

制定计划时的只读回放发现：2026-09-18 的 704 条研究记录中，469 条带有 `final_status=phase2_filtered`、资格未知且没有六维分数；仅把这类已终止记录从排名重放中排除后，剩余 235 条的基线 Top 30 与正式快照完全一致。2026-09-08 至 09-11 的旧冻结记录重放次序不完全一致，2026-09-14 是零记录日。更关键的是生产入口会先按 `global_scope_codes` 缩小最终可选总体（`scripts/scans/daily_candidates.py:4482-4492`），旧研究快照未保存该范围；即使某天基线恰好相同，也只供审计，不得用作正式消融收益样本。新实验从补齐冻结范围后的未来正式快照开始积累。

## 接入决策

复用当前后台任务，不增加系统定时器。现有统一入口在报告就绪后创建后台任务（`scripts/bridge/run_today.py:127-152,237-251`），后台目前执行 `close → weekly → monitor`，有任务 ID、状态、超时和续跑机制（`scripts/bridge/today_background.py:166-255`）。新增两个研究阶段：

1. `factor_ablation_daily`：报告就绪后、`close` 前运行纯本地冻结输入重排；单独限时 10 秒，失败或超时记录独立状态并继续 `close → weekly → monitor`。
2. `factor_ablation_evaluation`：`close` 和 `monitor` 后读取已落盘的同契约前向结果，更新本周研究诊断；单独限时 20 秒，结果不足时写 `continue_accumulating`，不视为任务失败。原有三个阶段保留独立的 300 秒总预算；任务最长预算为 10+300+20 秒，不让研究阶段挤占原有阶段。核心阶段未完成时评价记 `deferred_core_incomplete`，等待续跑。

每阶段完成后立即以阶段名与输入摘要写入检查点；`--resume` 校验摘要后跳过已完成阶段，只执行失败、递延或未开始的阶段。研究阶段异常不能沿用 `today_background.py:166-255` 当前会中断整条任务的超时传播方式，必须在阶段边界捕获并记录。相同任务重复调用仍返回同一任务及其已完成产物。

显式 `--postprocess sync` 走相同两个研究阶段，以便诊断。未能生成正式研究快照、哈希关联不符或交易日历不可用时，阶段写明 `skipped` 及原因；不能用报告中的 Top 30 代替冻结总体。`workflow` 和 `--status <task_id>` 应显示各研究阶段状态与产物路径。原有市场刷新、正式推荐和监控成功与否不受研究阶段影响。

## 实施步骤

1. **冻结真实选择范围。** 在 `scripts/scans/daily_candidates.py:4482-4492` 将实际参与最终选择的 `global_scope_codes` 或全量标志、排序范围代码摘要及 `top/min_score` 写入研究快照；扩展 `scripts/core/candidate_research_snapshot.py:133-225` 的输入清单和每条记录的 `selection_scope_included`。全量模式必须有显式标志，不能把空范围与全量混淆。新回放只消费经签名输入清单验证的范围；旧快照缺该字段时标记 `scope_unverified`，不以事后推断补齐。
2. **定义冻结实验契约。** 新建 `scripts/analysis/factor_ablation.py`，从 `scripts/analysis/recommendation_diagnostics.py:352-382` 使用的 `formal/primary.json` 索引读取研究快照，验证 `official_snapshot.link_status=linked`、正式快照哈希、推荐依据日、模型版本、范围摘要和每条记录的 `record_id`。实验定义固定六维名称、基线权重、单项消融公式、Top-K 与评价窗口，并生成定义及输入摘要。仅有明确、可信的 `phase2_filtered` 终止状态允许在重放前排除，并单列计数；范围内其余记录若缺失六维 `raw_dimensions`、质量因子或资格证据，整日降级为 `input_incomplete`，不把缺失分数填零、回退到五维公式或静默丢弃。
3. **实现每日同池重排。** 基线优先复用 `scripts/backtesting/recommendation_experiments.py:174-279` 的冻结输入校验及生产选择器，输入限定为冻结的真实选择范围，并与正式快照的全部候选代码、次序和推荐层级比较；不一致标记 `baseline_mismatch`。对范围内每条可评分记录先逐项核对：`round(Σ 六维原始分×权重, 1)` 等于冻结原始综合分；`round(原始综合分×coverage_factor×freshness_factor, 1)` 等于冻结质量分；按生产顺序加入冻结的、经入场时效门控后的严格买点奖励并以 100 封顶，等于冻结优先分。容差仅覆盖浮点运算误差，不容忍 0.1 分档差异。任一核对失败则整日不可比较。

   在同一资格与推荐层级中，每次只移除一个维度，把其余权重除以 `1 - 被移除权重`，按同样的逐步 `round(..., 1)`、质量因子、冻结买点奖励和封顶顺序算独立 `shadow_rank_score`。基线 `composite_score`、`quality_adjusted_score` 和冻结资格仍决定全部硬门槛，处理组分数只改变层内排序。沿用生产的入场时效优先顺序、同分代码排序和各层额度截取；Top 1/3/5 仅从实际有推荐资格的层取，观察池单列排名而不称为推荐。板块入池作用和维科夫硬门槛作用另列后续实验。输出六个影子 Top-K、与基线交集和换股率、各层样本数、预期过滤数、异常缺失数、无可执行推荐日计数。
4. **持久化与自动触发。** 在 `scripts/bridge/today_background.py` 加入两个受限阶段及阶段检查点，在 `scripts/bridge/run_today.py` 接入同步路径和状态展示。每日结果写入 `.cache/stock-trend/evolution/factor_ablation/daily/<依据日>/<输入摘要>.json`；评价写入 `weekly/<ISO周>/<输入摘要>.json`。原子写入、相同输入幂等、不同输入不覆盖；产物包含基线版本、正式快照哈希、研究快照哈希、实验定义、选择范围摘要、资格覆盖率、运行状态及错误类别。研究失败不调用 `publish` 或 `rollback`，不改 `active_policy.json`。
5. **接入成熟收益评价。** 复用 `scripts/analysis/recommendation_attribution.py:270-330` 的 5/10/20/60 日候选信号结果和 `scripts/core/research_events.py` 的重叠事件去重，以推荐日为配对单位比较同日基线与六个影子 Top-K。结果必须以 `(record_id, research_snapshot_sha256, evaluation_contract_id)` 严格关联，拒绝重复或冲突身份；只读取 `evaluation_as_of` 不晚于本轮诊断截止日的结果，不直接使用仅按日期/代码索引的宽松辅助函数（参见 `scripts/analysis/recommendation_diagnostics.py:262-282,384-500`）。只有同日基线与处理组 Top-K 均有完整、有限的沪深300超额收益时，该日才进入收益差；其余日期仅计入覆盖率与缺失原因。5 日仅作质量与异常排查；20 日报告日等权沪深300超额收益差、胜率差、中位数、MAE 尾部及缺失/待成熟数；60 日作方向确认。按市场档位与板块分组只作描述性诊断，样本不足显示 `insufficient_data`。所有检验使用同一冻结候选与评价契约，不调用实时接口重新生成旧日特征。
6. **限定研究结论。** 在实验定义中预先登记六个比较、主指标、20 日成熟门槛、时间切分、60 日确认与多重比较处理。未达到至少 20 个有效成熟日期和 100 个去重有效超额收益事件时，只输出覆盖率与 `continue_accumulating`（现有准备门槛见 `scripts/analysis/recommendation_attribution.py:878-910`）。即使达到门槛，也须经过时间顺序样本外及保留样本验证；当前发布注册器只接受买点奖励实验（`scripts/core/evolution_registry.py:134-149`），本项目不扩展其发布能力。
7. **文档与观测。** 更新 `.claude/skills/stock-trend/SKILL.md`、`docs/today-recommendation-evolution-usage.md` 和功能规格，说明自动阶段、结果口径、状态查询与研究边界。后台状态区分 `completed`、`continue_accumulating`、`skipped`、`scope_unverified`、`baseline_mismatch`、`input_incomplete`、`failed`、`deferred_core_incomplete`；报告返回仍只代表正式推荐就绪。

## 验收标准

- 盘后正式“今日推荐”一次调用后，无需额外命令即可生成对应依据日的每日消融产物；默认后台和显式同步模式均覆盖。盘中临时、缺交易日历、研究快照未关联或哈希不符均有准确跳过原因。
- 同一输入重复运行得到相同实验 ID、同一影子排序和同一产物；不得覆盖旧正式快照或改变 `active_policy.json`。输入或模型版本变化生成新身份并在状态中可见。
- 新正式快照的生产选择范围代码集与研究快照记录的 `selection_scope_included` 逐项一致；旧快照缺范围字段时显示 `scope_unverified`，即使基线 Top-K 恰好相同也不进入有效实验样本。
- 基线重放的全部候选代码、顺序和层级与冻结正式快照一致；不一致时六个处理组均不得声称有收益改善。2026-09-08 至 09-11 的旧快照显示 `scope_unverified` 或 `baseline_mismatch`，不得自动补齐。每个处理组只改变一维的层内排序；资格门槛、层级和其余权重保持冻结。
- 每条范围内可评分记录的原始分、质量分和优先分均按正式舍入顺序复原到 0.1 分；五维回退记录和非法数值不进入六维对照。
- 对全部六个处理组报告可比较记录数、因字段缺失而退出数、Top-K 差异；遇到全观察池或零可执行推荐日时不捏造 Top-K 业绩。
- 前向结果未成熟时状态为 `continue_accumulating`；成熟后按同日、同评价契约配对，缺失沪深300收益的事件不计有效 alpha，重叠事件不重复计数。5 日结果不会触发改权重或发布。
- 强制每日研究超时 10 秒、评价超时 20 秒时，原有 `close → weekly → monitor` 仍获得独立 300 秒预算并按原顺序运行；强制进程中断后 `--resume` 跳过摘要匹配的已完成阶段，只补做未完成阶段。

## 验证

先为冻结范围校验、六维重排、逐项舍入、基线一致性、五维回退拒绝、严格评价身份与截止日过滤添加定向测试；再扩展 `tests/test_run_today.py`、`tests/test_evolution_job.py`、`tests/test_recommendation_experiments.py` 的后台顺序、强制超时、进程中断续跑、幂等与评价样例。用一日含可执行/等待/观察的固定夹具验证基线与处理组；用真实冻结快照做只读回放并确认不会写正式推荐历史。修改 `scripts/` 下 Python 文件前先声明影响范围；修改后运行仓库要求的两项门禁：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

随后运行相关定向测试、`git diff --check`；只有核实数值或展示变化符合设计时才考虑更新 golden。

## 风险与处理

- **每日消融变成重复计权结论**：只测试固定候选池内排序；板块入池与维科夫资格独立研究，结果标明研究总体。
- **选择偏差与不完整快照**：正式哈希、研究哈希、真实选择范围和基线重放四重绑定；覆盖率不足或不一致直接降级为审计结果。
- **多次试参造成过拟合**：仅六个预登记单项处理，采用时间切分、保留样本及多重比较处理；不得依据少数 5 日样本调权。
- **后台预算影响原任务**：10 秒每日研究与 20 秒收益评价使用独立预算，原有阶段保留 300 秒；失败隔离、阶段检查点和断点续跑须由定向测试证明。统计产物不参与正式推荐或自动发布。

本计划仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
