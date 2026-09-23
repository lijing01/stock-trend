# 投资胜率证据链修复计划

状态：P5 已实施；策略调权与发布仍需达到样本门槛并人工审核。日期：2026-09-23。

## 目标与边界

目标不是用短样本直接“调高胜率”，而是先修复市场基线、研究快照、前向收益与沪深300超额收益的证据闭环，使后续权重、买点奖励和市场阈值调整都有可复现的样本外依据。

本计划保持现有正式安全门不变：市场数据 `partial` 或评分低于 60 时不产生正式可执行推荐；板块持续性不足、买点未确认、数据质量不合格时继续降级。修复期间不修改六维权重、60/80 市场阈值、0/2/5 推荐上限或 `+1/+3/+2` 买点奖励，也不自动发布实验策略。

## 已确认现状

- 2026-09-23 收盘市场环境为 41.2 分，正式结论为弱势；成交额和涨停情绪因历史基线不足被标记为 `partial`。成交额和涨停基线分别在 `market_regime.py:332-402` 评分，收盘历史只在 `market_regime.py:594-619,1192-1204` 写入。
- 市场历史当前只有 2026-09-22、2026-09-23 两个有效日期。成交额可尝试从指数历史 K 线重建，但涨停历史不能用当前值或未来数据伪造；相关历史过滤与重建入口位于 `market_regime.py:464-591,963-1014`。
- 最近 11 个正式推荐日中，仅 2026-09-21 进入 `waiting_trigger`，其余均因弱市、数据部分缺失、过期或缺失而只观察。这意味着目前没有足够的正式交易样本计算可信“胜率”。
- 截至 2026-09-22，20 日主窗口为 0 个成熟日期、0 个去重成熟事件；监控记录存在大量缺失超额收益。候选收益计算和沪深300 alpha 位于 `recommendation_attribution.py:270-329`，成熟度、覆盖率和去重位于 `recommendation_attribution.py:693-917`。
- 研究人口在 `research_link_status` 为 `missing/mismatch/unverified` 时会被安全排除，见 `recommendation_attribution.py:1002-1033`；这是正确的防未来函数行为，修复不能退回旧候选集合冒充冻结研究人口。
- 正式市场门控和候选分桶已具备保守规则，见 `daily_candidates.py:3302-3356,3416-3510`；板块持续性和可执行资格位于 `daily_candidates.py:1460-1575`。现阶段主要问题是证据不足，不是门控过严。

## 修复顺序

### P0：冻结基线并建立缺口审计

1. 新增只读诊断输出，按交易日汇总：市场历史可用天数、成交额/涨停基线天数、正式推荐状态、研究快照链接状态、各窗口 `pending/data_error/complete` 数量、沪深300 alpha 缺失原因和去重事件数。
2. 将 2026-09-08 至 2026-09-23 的现有事实冻结为测试 fixture；历史缺失、链接缺失和 `partial` 必须保留为缺口，不能补默认值。
3. 明确分母：正式推荐表现、冻结研究候选表现、观察池表现分别统计，不允许混成一个“胜率”。

涉及文件：

- `.claude/skills/stock-trend/scripts/analysis/evolution_job.py:114-220,272-355`
- `.claude/skills/stock-trend/scripts/analysis/recommendation_diagnostics.py:136-501`
- `.claude/skills/stock-trend/tests/test_evolution_job.py`
- `.claude/skills/stock-trend/tests/test_recommendation_diagnostics.py`

验收：同一缓存重复运行诊断，内容 hash 和各状态计数完全一致；报告能区分“尚未成熟”“数据错误”“基准缺失”“研究链接缺失”。

### P1：修复市场基线与数据来源证明

1. 扩展指数成交额诊断：逐指数记录 `provider/data_date/fetched_at/record_count/amount_available_days`，明确是腾讯降级不提供历史成交额、单边市场缺失，还是日期无法对齐。
2. 仅当上证与深证成交额在同一历史交易日都完整时，允许形成成交额基线；复用 `complete_market_amounts()`，不以单边数据或估算值提升 `data_status`。
3. 收盘历史条目持久化组件级证据：来源、数据日期、采集时间、完整性和使用资格；`freshness=unknown` 继续阻止“新鲜”声明，但不与组件缺失混为一谈。
4. 涨停历史采用“收盘后逐日积累”作为首选修复；若增加历史接口，只接受带明确交易日且能做时点校验的来源。禁止用当前涨停数回填过去日期。
5. 对历史写入增加原子性和冲突检测：同日相同内容幂等，不同内容保留冲突证据而不静默覆盖；盘中数据继续禁止进入正式基线。
6. 把收盘采集纳入统一入口或明确的日终任务，使市场历史和板块快照在每个完成交易日持续累积；失败日保留缺口并在次日报告中显示。

涉及文件：

- `.claude/skills/stock-trend/scripts/analysis/market_regime.py:332-402,464-619,963-1014,1096-1140,1192-1204`
- `.claude/skills/stock-trend/scripts/analysis/sector_snapshot_job.py:1-220`
- `.claude/skills/stock-trend/scripts/bridge/run_today.py:300-340`
- `.claude/skills/stock-trend/tests/test_market_regime.py:494-532`
- `.claude/skills/stock-trend/tests/test_sector_snapshot_job.py`

验收：连续 5 个完成交易日后，成交额与涨停情绪在来源完整时由 `partial` 自动变为 `good`；任何盘中、缺半边市场、日期不一致或来源时间未知的证据都不能伪装成完整历史。

### P2：修复研究快照链接和前向评价完整度（已实施）

1. 审计正式推荐快照与研究快照的稳定关联，逐日核对 `recommendation_date`、`official_snapshot.content_sha256`、`research_run_id`、`research_snapshot_sha256`、`selection_scope` 和评估合同 ID。
2. 对 2026-09-08 以后具备同日冻结证据的记录修复可确定的链接；旧日期缺少原始研究人口时保持 `missing/mismatch`，不得从当前候选或正式 Top-N 反推。
3. 在行情加载器中把个股与沪深300序列作为同一评价单元校验：交易日历、入场日、退出日和价格字段全部齐全才生成 alpha；否则写入稳定原因码，如 `hs300_entry_missing`、`hs300_exit_missing`、`calendar_mismatch`。
4. 修正覆盖率口径：`pending` 不算失败，研究人口缺口不算已评价，基准缺失单独统计；只有完整20日收益且存在沪深300 alpha 的事件进入有效成熟分母。
5. 保留现有20日主窗口、事件区间去重和100事件门槛；5日、10日仅作质量诊断，不能触发调权或发布。

涉及文件：

- `.claude/skills/stock-trend/scripts/core/candidate_research_snapshot.py:133-263`
- `.claude/skills/stock-trend/scripts/analysis/recommendation_attribution.py:270-329,527-620,693-917,1002-1033,1330-1384`
- `.claude/skills/stock-trend/scripts/analysis/recommendation_diagnostics.py:136-501`
- `.claude/skills/stock-trend/tests/test_recommendation_attribution.py:183-224`
- `.claude/skills/stock-trend/tests/test_recommendation_diagnostics.py`

实施结果：正式/研究快照按日期与内容哈希解析；已有哈希证据但旧状态标记不一致时仅内存修复链接，缺少 `selection_scope` 或原始人口证据的旧日期保持 `unverified`。评价结果逐条携带官方快照、研究运行、研究哈希、冻结范围和评价合同 ID；沪深300缺失行/缺价/日历错位写入稳定原因码，缺基准不进入有效 alpha 成熟分母，`pending` 与研究人口缺口继续独立统计。

验收：每个候选记录在每个窗口都有且只有一个稳定终态或 `pending`；完整窗口的 `hs300_alpha` 可由冻结价格独立复算；研究链接缺失不会产生候选评价人口；重复运行不产生重复成熟事件。

### P3：验证门控，不放松门控（已实施）

1. 为市场门、数据质量门、板块持续性门、买点健康门分别输出阻断计数和影子通过名单，便于判断“没有推荐”究竟来自弱市还是数据问题。
2. 锁定以下不变量：`partial` 市场数据只观察；市场分低于60只观察；60–79最多2只等待触发；80以上最多5只；板块历史不足或单日脉冲不提升为正式推荐。
3. 增加跨日测试：市场由 `partial → good` 只能解除数据阻断，不能绕过弱市、板块、资金背离或维科夫门槛。
4. 连续验证板块快照至少覆盖2个交易日后再允许 `emerging`，达到既有主线条件后才允许 `mainline`；历史不足保持 `history_insufficient`。

涉及文件：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:1280-1580,1721-2010,3302-3356,3416-3518`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py:2841-2969`
- `.claude/skills/stock-trend/tests/test_sector_snapshot_job.py`

验收：所有正式提升都能列出通过的市场、数据、板块、资金和买点证据；单独补齐某一维数据不会越过其他硬门；现有0/2/5上限与盘中 provisional 行为不变。

实施结果：新增 `recommendation-gates/v1` 门控审计，逐次输出市场、数据质量、板块持续性、资金证据和买点健康的阻断计数、阻断代码、独立影子通过名单及限额前全门交集；JSON、Markdown、HTML 和 stderr 性能审计均携带该证据。门控判定仍复用既有正式策略：`partial`、弱市、资金背离、历史不足、单日脉冲和不健康买点不会因补齐另一维数据而升级。补充跨日回归测试，并修复旧适配器缺少 `as_of_date` 时丢失板块上下文的问题；移除已废弃主题工作流的失效测试入口。

### P4：恢复消融研究和参数候选生成（已实施）

1. 每日继续冻结六维消融 Top1/3/5，但只做影子排序，不改变正式推荐。
2. 评价严格按 `(record_id, research_snapshot_sha256, evaluation_contract_id)` 关联20日 alpha；缺任一身份字段即排除并计数。
3. 只有同时满足以下条件才生成“可进入人工审核”的参数建议：至少20个成熟配对日期、至少100个去重有效 alpha 事件、每个比较覆盖率不低于90%，并通过既有多分区样本外验证。
4. 报告同时展示绝对收益、沪深300超额、胜率、MAE、日期分布和市场环境分层；若绝对收益为负但相对收益为正，只能结论为“相对选股有效、市场择时仍需约束”。

涉及文件：

- `.claude/skills/stock-trend/scripts/analysis/factor_ablation.py:430-555`
- `.claude/skills/stock-trend/scripts/backtesting/recommendation_experiments.py:700-1005,1112-1225`
- `.claude/skills/stock-trend/tests/test_factor_ablation.py`
- `.claude/skills/stock-trend/tests/test_recommendation_experiments.py`

验收：未达样本门槛统一返回 `continue_accumulating`；任一维度移除实验都使用相同日期、相同冻结人口和相同评价合同；没有自动写入正式策略指针。

实施结果：每日消融继续只读取同一次正式扫描的冻结六维研究快照，生成移除单维后的 Top1/3/5 影子排序；评价严格绑定 `(record_id, research_snapshot_sha256, evaluation_contract_id)`。20 日主窗口现在额外输出绝对收益、沪深300超额、胜率、MAE、日期分布和弱/中/强市场分层，并对每个移除比较的 Top1/3/5 覆盖率执行 90% 门槛；样本不足统一保持 `continue_accumulating`。已有 `recommendation_experiments` 的三段样本外、净化窗口、100 事件和 20 成熟日期门控继续作为发布前验证，不改变正式权重。

### P5：人工发布与持续监控（已实施）

1. 仅当 P4 证据通过后生成版本化实验定义和完整 release evidence；人工审核后才能 publish。
2. 发布后持续监控20日主窗口覆盖率、平均沪深300超额、绝对收益、MAE和数据失败率；合同异常允许安全回退，普通统计波动只提示人工复核。
3. 保留原基线作为对照，发布版本必须可一键回滚且不改写历史快照。

涉及文件：

- `.claude/skills/stock-trend/scripts/core/evolution_registry.py`
- `.claude/skills/stock-trend/scripts/analysis/evolution_job.py:272-355`
- `.claude/skills/stock-trend/tests/test_evolution_job.py:128-155,569-638`

验收：没有完整 release evidence 无法发布；回滚只切换原子指针，不修改正式推荐历史、研究快照或评价结果。

实施结果：发布仍要求注册实验、验证结果、影子结果、最终 holdout 和 release evidence，必须显式人工调用；监控合同补充 20 日绝对收益、胜率、MAE 及各自覆盖率，同时保留数据失败率、沪深300超额和契约异常安全回退。回滚只切换 `active_policy.json` 原子指针，历史快照与评价结果保持不可变。

## 总体验收标准

1. 市场基线连续积累且每条记录有可审计来源；盘中和部分数据不污染收盘历史。
2. 研究快照、正式快照和评价结果身份一一对应；旧数据无法证明时明确缺口，不做推断性回填。
3. 20日主窗口达到至少20个成熟日期、100个去重有效 alpha 事件前，不输出“胜率提升”结论。
4. 正式推荐与研究候选、观察池的指标分开；报告同时展示绝对收益和相对沪深300收益。
5. 市场、板块、资金、数据质量和维科夫门控行为保持兼容；修复数据不能绕过策略安全门。
6. 所有新增 artifact 幂等、可复算、带内容 hash；重复运行不改变历史结论。

## 验证顺序

1. 目标单测：`test_market_regime.py`、`test_sector_snapshot_job.py`、`test_recommendation_attribution.py`、`test_recommendation_diagnostics.py`、`test_daily_candidates.py`、`test_factor_ablation.py`、`test_evolution_job.py`。
2. 离线集成：用冻结 fixture 跑 `market → candidates → close → weekly → monitor → factor_ablation_evaluation`，断言状态迁移和身份 hash。
3. 联网烟测：收盘后运行一次统一入口，确认市场历史、行业快照、正式快照、研究快照和后台评价均绑定同一依据日。
4. Python 文件修改后的仓库强制质量门：

   ```bash
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

5. 不为消除失败而重生成 golden；只有确认数值或报告契约变化符合预期后才更新，并记录原因。

## 风险与缓解

- **把冷启动误判为代码故障：** P0先区分“自然未成熟”“任务未运行”“数据错误”，只修可证明的缺陷。
- **历史回填引入未来函数：** 只接受带交易日、来源和当时可见性证明的数据；无法证明的旧日期保持缺失。
- **为了增加推荐数量而放松安全门：** P3明确锁定现有门槛，所有实验先走影子层。
- **大量候选掩盖有效样本不足：** readiness 使用去重事件和成熟日期，不使用原始行数替代。
- **相对收益掩盖绝对亏损：** 发布判断同时要求展示绝对收益、alpha、MAE和市场分层，不以单一 alpha 代替投资结果。

## 停止条件

工程修复完成的条件是证据链完整、状态可审计、质量门通过；策略优化完成的条件则更严格：20日主窗口样本达到门槛、消融比较具备足够覆盖并完成样本外审核。在后者满足前，系统保持 `continue_accumulating`，不修改正式权重或门槛。

本计划仅供学习与研究参考，不构成投资建议。
