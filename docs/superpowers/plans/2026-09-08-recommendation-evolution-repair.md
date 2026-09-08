# 今日推荐 AI 自进化闭环修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. A、B 批次（任务 1–7）已执行；C 批次（任务 8–9）尚未执行，不发布策略。

**Goal:** 修复研究数据、统计、重放、晋级和监控之间的断点，使不完整或不合格证据无法形成调参结论或进入正式策略。

**Architecture:** 沿用正式快照、研究快照、独立评价和追加式注册表。在现有模块内修复入口与契约，只抽出一份共享研究事件计算模块；生产排序与实验重放共用现有确定性选择逻辑。所有新评价与实验使用新版本，旧正式记录保持不可变。

**Tech Stack:** Python、unittest、JSON、现有本地文件存储；不引入新依赖、数据库或常驻 Agent。

---

## 1. 范围与交付边界

依据：`.claude/specs/today-recommendation-evolution-plan.md` 和用户提供的 `/Users/jing.li7/Downloads/stock_ai_self_evolution_plan.md`。2026-09-08 已静态检查核心实现，并复现总体仅 30 个事件、1 个成熟日期仍接受提案的问题。未核查历史报告、实际缓存样本量或已发布版本。

本轮只修复现有闭环。保留 20 日候选超额收益主指标、60 日确认、无自动交易、不放宽资格门控、显式发布等边界。不增加新评分公式、模式挖掘、市场状态调权或自动代码进化。

状态分开记录：`module_delivered`（代码交付）、`integration_verified`（闭环验收）、`research_ready`（数据成熟）、`release_eligible`（实验合格）。旧计划的“已实施”不作为后三项的证明。

## 2. 核实的问题与对应任务

| 问题 | 证据位置 | 任务 |
|---|---|---|
| 周任务显式读取旧研究目录 | `analysis/evolution_job.py:run_weekly` | 1 |
| 周任务 as-of 未过滤输入和结果 | 同上及 diagnostics loaders | 1、2 |
| Outcome 仅覆盖正式 candidates，遗漏研究池淘汰对象 | `analysis/recommendation_attribution.py:track_attribution` | 2 |
| 诊断、实验、监控去重与权重不同 | diagnostics / experiments / evolution_job | 3 |
| 总体未成熟仍接受格式合格的提案 | `analysis/evolution_proposals.py:build_proposal_run` | 4 |
| 每次最多三条不等于每周总预算 | proposals 生成和保存入口 | 4 |
| 重放自行全局排序，未证明同层排序等价 | `recommendation_experiments.py:_replay_date` | 5 |
| 名为时间块抽样，实际逐日独立抽样 | `recommendation_experiments.py:_bootstrap` | 6 |
| 无 60 日晋级条件、无保留集消费状态 | experiments / registry | 6、7 |
| 任意非空 evidence 可推进 eligible | `evolution_registry.py:transition` | 7 |
| 哈希文件名被当作运行时间、空数据可 healthy | `evolution_job.py:monitoring_snapshot` | 8 |
| 实验测试使用无效日期与零长度标签 | `tests/test_recommendation_experiments.py` | 6 |

代码路径如未写前缀，均相对 `.claude/skills/stock-trend/scripts/`。

## 3. 先冻结的修复契约

### 数据与时间

- CLI `--as-of YYYY-MM-DD` 表示上海时区该日结束时的评价截止，不改变原候选收益端点。
- 分开保存 `decision_at`、来源 `known_at`、`captured_at`、`evaluation_as_of`、`label_end`。不得用统一的 15:00 时间冒充来源发布时间；旧记录缺来源时间时标记 unknown。
- 截止之后的推荐排除；标签端点晚于截止日保持 pending。不得仅筛选推荐日期而保留后来成熟的收益。
- 新结果保存为 `evaluations/<contract_id>/v2/<evaluation_as_of>/<recommendation_date>.json`，同输入幂等，不覆盖旧版本。相同键的不同内容留冲突，不静默替换。
- 严格历史重放使用截止时可用的输入归档和结果版本；若只有后来补算的数据，标记 `retrospective`，可描述但不作为当时可用证据。不推断旧记录缺失的时点信息。
- 未被选中的可投资对象也生成信号 Outcome。硬过滤的非目标对象只保留状态，不计入可投资对照组；缺失、未到期和不可评价均不能记零收益。

### 研究事件与评价

- 正式记录身份保持为日期、市场及证券代码；每个窗口分别形成事件集。
- 同股同窗口的重叠区间保留最早推荐为事件锚点，结束日相同也视为重叠。先按冻结推荐及交易日端点分配事件，再附加结果；不得因某条结果成功或失败改变事件归属。
- 候选总体、各策略臂和分组分别报告原始记录数、事件数、成熟日期数、有效 alpha 数和缺失状态。分组继承事件锚点的特征，不能重复计算同事件的后续信号。
- 日度效果按当日已选股票等权，再按日期等权；事件去重用于独立事件门槛和事件诊断。两个估计对象分别命名，不用去重记录冒充每日可用推荐。
- 第一版提案总体门槛沿用 20 个成熟日期、100 个去重成熟事件；引用分组至少 30 个去重成熟事件。必须有有限的主指标数值和合法端点才能计为有效成熟事件。
- 配对比较中，任一臂已选对象缺有效结果，则该日期不进入收益配对，单列缺失；不对双方各自剩下的股票直接求均值比较。成熟日期中可完整评价日期占比每臂至少 95%，否则不晋级。该 95% 是新工程约定，须在首个 v2 实验登记前冻结。

### 实验与发布

- 仍只支持冻结的买点奖励 `+1/+3/+2` 对照 `0/0/0`。资格、分层、Top N、其他排序条件全部不变。
- 固定规则实验称为“冻结规则分时段比较”；本轮不伪造训练器。未来参数搜索另行设计。
- 验证日历、分区、主窗口、60 日确认、抽样配置、覆盖阈值均进入实验定义及哈希。按交易日和实际标签端点隔离，不按研究文件数量估算交易日。
- 至少三个有效样本外验证区间，各至少 20 个完整配对成熟日期；另有冻结后才允许消费的最终保留区间。60 日参与确认，区间隔离覆盖 60 日。每个区间的日期必须在运行前明确登记。
- 每臂主窗口至少 100 个去重事件；主指标差的时间块 95% 区间下界 >0；推荐日期覆盖不低于基线 90%；MAE 5% 尾部不恶化超过 1 个百分点。上述现有条件不能靠合并两臂事件数量或空区间通过。
- 60 日确认新增明确口径：每臂至少 100 个成熟事件、20 个完整配对日期，日期等权平均超额收益差 >=0。不把它宣称为独立显著性检验；不足只允许保留 shadow。
- 最终保留区间至少 20 个完整配对成熟日期，主指标差为正，且满足覆盖、数据完整性和 MAE 条件。保留区间用于确认，不用于生成/修复提案；同一研究批次的候选共享最多三个尝试的预算，登记失败和零变化。
- 以上新增门槛是首版保守工程约定，实施前写入版本化契约；数据不足的正确结果为继续积累，不为完成工程而降低门槛。
- 发布凭证绑定 `experiment_id`、定义哈希、输入清单哈希、评价契约、验证结果、60 日确认、保留集消费记录和真实 shadow 结果。仅有字符串或自报 `passed=true` 不能晋级。

## 4. 文件责任与任务顺序

新增代码仅 `scripts/core/research_events.py`，承载共享事件分配和日度汇总；新增测试 `tests/test_research_events.py`。其余在现有 attribution、diagnostics、proposals、experiments、registry、job 及对应测试中修复。

依赖：任务 1 → 2 → 3；3 → 4；2、3 → 5 → 6 → 7；1、3、7 → 8；全部 → 9。默认顺序实施。若采用原生子代理，仅在任务 3 完成后将任务 4 与任务 5 分配给不同负责人；共享注册表和入口仍由主执行者整合。

每任务执行循环：先增加下列具体验收用例并确认旧行为失败，再最小实现，再跑目标测试与两项质量门禁。下列代码片段定义修复契约和核心分支，不替代现有模块的完整上下文。不得顺手重构无关代码。

### Task 1：研究入口与截止时间

**Modify:** `scripts/analysis/evolution_job.py`、`scripts/analysis/recommendation_diagnostics.py`。
**Test:** `tests/test_evolution_job.py`、`tests/test_evolution_storage.py`。

- [x] 增加仅在 `evolution/research` 写正式样本的临时目录夹具：默认 weekly 必须读到它；同日新旧目录都有样本时只取新记录，自定义 root 不扫描其他目录。
- [x] 增加历史 cutoff 用例：未来推荐排除；截止后才成熟的标签不能进入诊断。dry-run 不创建目录、不调用模型、不写结果。
- [x] 默认入口直接复用 diagnostics 的 `DEFAULT_RESEARCH_ROOT`，loaders 显式接收 `as_of`；保留旧目录只读兼容。截止过滤核心：

```python
if recommendation_date > as_of:
    continue
if label_end > as_of:
    window = {"status": "pending", "reason": "after_evaluation_cutoff"}
```

- [x] 运行 `python3 .claude/skills/stock-trend/tests/test_evolution_job.py` 和 `python3 .claude/skills/stock-trend/tests/test_evolution_storage.py`，预期全部通过。
- [x] 运行第 9 节质量门禁；通过后纳入本次 A 批次提交。

### Task 2：完整研究池 Outcome 与评价版本

**Modify:** `scripts/analysis/recommendation_attribution.py`、`scripts/core/evolution_contract.py`、`scripts/core/evolution_storage.py`、`scripts/core/candidate_research_snapshot.py`、`scripts/scans/daily_candidates.py`、`scripts/analysis/evolution_job.py`、`scripts/analysis/recommendation_diagnostics.py`。
**Test:** `tests/test_recommendation_attribution.py`、`tests/test_evolution_storage.py`、`tests/test_evolution_job.py`、`tests/test_recommendation_snapshot.py`。

- [x] 夹具设正式 Top 1=A、完整可投资池=A/B、硬排除=C：A/B 都获得 Outcome，C 保留 excluded 状态；给 B 注入取数失败，A 仍成功，B 可单独重试。
- [x] 增加版本用例：同样输入重试不新增；同日不同内容留冲突；不同评价截止写不同版本；旧正式快照字节摘要不变；读取旧 sidecar 不伪造 point-in-time 资格。
- [x] 收盘任务加载关联正式研究快照，以冻结对象集合计算信号结果，复用 `evaluate_candidate_signal` 和现有 series loader；交易模拟仍只处理有相应交易输入的对象。对象身份和输入哈希进入评价契约。

```python
evaluation_identity = {
    "contract_id": contract["contract_id"],
    "research_run_id": research["run_id"],
    "recommendation_date": recommendation_date,
    "evaluation_as_of": evaluation_as_of,
    "population_kind": "frozen_investable_research_population",
}
```

- [x] 删除研究时点的伪精确语义：15:00 仅可表示规定的决策基准；真实抓取时间另存，来源未知显式 unknown。写入失败仍不影响原候选展示。
- [x] 正式当日记录缺失时保留 gap，但继续评价此前已存在的研究事件；非交易日不伪造推荐。返回当日采集与历史到期评价两个阶段状态。
- [x] 运行上述四个测试文件及质量门禁；通过后纳入本次 A 批次提交。

### Task 3：统一事件与日期等权统计

**Create:** `scripts/core/research_events.py`、`tests/test_research_events.py`。
**Modify:** `scripts/analysis/recommendation_attribution.py`、`scripts/analysis/recommendation_diagnostics.py`、`scripts/backtesting/recommendation_experiments.py`、`scripts/analysis/evolution_job.py`、`tests/test_stock_trend.py`。
**Test:** 新测试以及现有 attribution、diagnostics、experiments、job 测试。

- [x] 新增真实交易日夹具：同股连续十次推荐产生十条原记录、一个重叠事件；端点之后的新信号形成新事件；跨市场同代码不合并；缺端点不可用于就绪门槛。
- [x] 增加选择偏差用例：最早推荐结果缺失而后续推荐有结果，不能把后续记录升级为新的独立事件。
- [x] 新模块定义两个公开接口，迁移现有 `_completed_primary_events` 调用为兼容包装，不改变旧外部字段：

```python
# 输入为已冻结、已按窗口补齐交易日端点的记录；输出稳定事件归属。
def assign_research_events(records, window):
    events, mapping, invalid, active = [], {}, [], {}
    ordered = sorted(records, key=lambda r: (
        r.get("market", ""), r.get("code", ""),
        r.get("entry_date", ""), r.get("recommendation_date", "")))
    for record in ordered:
        required = ("market", "code", "entry_date", "exit_date", "record_id")
        if any(not record.get(key) for key in required):
            invalid.append(record)
            continue
        key = (record["market"], record["code"])
        previous = active.get(key)
        if previous and record["entry_date"] <= previous["exit_date"]:
            mapping[record["record_id"]] = previous["event_id"]
            continue
        event_id = f"{window}:{record['record_id']}"
        event = {**record, "event_id": event_id, "window": window}
        events.append(event)
        active[key] = event
        mapping[record["record_id"]] = event_id
    return {"events": events, "record_to_event": mapping, "invalid": invalid}

# rows 每项含 recommendation_date、code、hs300_alpha、evaluation_status。
def summarize_daily_alpha(rows):
    import math
    grouped, missing = {}, 0
    for row in rows:
        value = row.get("hs300_alpha")
        if (row.get("evaluation_status") != "complete"
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            missing += 1
            continue
        grouped.setdefault(row["recommendation_date"], []).append(value)
    daily = {day: sum(values) / len(values) for day, values in grouped.items()}
    return {"daily": daily, "mature_dates": len(daily),
            "mean_alpha": sum(daily.values()) / len(daily) if daily else None,
            "missing_records": missing}
```

调用前使用现有日期解析和交易日历验证日期及窗口端点、拒绝倒置区间，并由冻结记录产生唯一 `record_id`；相同记录重试先幂等归并。配对比较须先执行任务 5 的完整性检查再调用日均函数。所有消费者使用同一实现，不复制不同版本的去重循环。

- [x] 增加日权重断言：第一日十只各 +10%，第二日一只 -10%，日期等权平均必须为 0，不能为约 +8.18%。
- [x] 将新测试 runner 接入 `test_stock_trend.py`；运行 `python3 .claude/skills/stock-trend/tests/test_research_events.py`、四个现有目标测试与质量门禁。
- [x] 通过后纳入本次 A 批次提交。

### Task 4：提案成熟度与周预算

**Modify:** `scripts/analysis/evolution_proposals.py`、`scripts/analysis/evolution_job.py`。
**Test:** `tests/test_recommendation_diagnostics.py`、`tests/test_evolution_job.py`。

- [x] 加入已复现失败用例：overall=30 事件/1 日期、分组 eligible、格式合法提案，必须返回 continue_accumulating、proposals=[]。
- [x] 对 99/20、100/19、100/20、分组 29/30 分别测试边界；拒绝空 values、bool、NaN、inf 和额外修改字段。
- [x] 总体门槛先于模型调用和提案接收：

```python
ready = (overall["valid_alpha_events"] >= 100
         and overall["alpha_mature_dates"] >= 20)
if not ready:
    valid, errors = [], ["overall_research_not_ready"]
    status = "continue_accumulating"
else:
    valid, errors = validate_proposals(diagnostics, response)
    status = "proposed" if valid else "no_valid_proposal"
```

- [x] ISO 周预算使用现有原子文件/锁模式保存每周唯一生成尝试和最多一次格式修复；调用前领取尝试号，崩溃后不得自动重复模型调用。响应可按尝试号幂等补录；每周累计最多三条，不同诊断包不能刷新预算。
- [x] 对重复生成、同周新诊断、并发领取、失败重跑写测试；usage 记录真实调用状态，不伪造 token 数。无模型仍完成统计。
- [x] 运行 diagnostics/job 目标测试与质量门禁；通过后纳入本次 A 批次提交。

### Task 5：生产选择与实验重放一致

**Modify:** `scripts/backtesting/recommendation_experiments.py`、`scripts/scans/daily_candidates.py`、`scripts/core/candidate_research_snapshot.py`。
**Test:** `tests/test_recommendation_experiments.py`、`tests/test_recommendation_snapshot.py`、`tests/test_recommendation_quality.py`。

- [x] 增加两层候选、并列分数、门控拒绝、Top N 边界夹具；同样输入与奖励下，重放选择代码及顺序必须逐项等于生产 `select_candidate_pool`/`classify_candidates` 的结果。
- [x] 复用现有确定性选择函数，先确认其无取数或写入副作用；仅传冻结 candidate、policy、top、min_score 和奖金。不要继续在 `_replay_date` 维护另一套分数排序。

```python
selected = select_candidate_pool(
    frozen_candidates, top, min_score,
    policy=frozen_policy, priority_bonuses=bonuses,
)
buckets = classify_candidates(selected, frozen_policy)
```

- [x] 研究快照保存重放所需的 preselection 资格、层级和参数。字段缺失、候选记录损坏、资格 unknown 时拒绝该日期并报具体原因，不能跳过坏记录后声称输入完整。
- [x] 明确新排序可能选 B 替代 A 的用例：B 的 Outcome 必须来自任务 2；若 B 缺结果，整日不进入收益配对，数据覆盖下降；不把“有推荐”覆盖当作“可评价”覆盖。
- [x] 运行三个目标测试及质量门禁；通过后保存独立提交 `fix: replay production candidate selection`。

### Task 6：真实时间分区与区间估计

**Modify:** `scripts/backtesting/recommendation_experiments.py`、`scripts/core/evolution_registry.py`。
**Test:** `tests/test_recommendation_experiments.py`。

- [x] 替换 `2026-01-32` 一类日期和 `exit_date=day` 的伪标签。使用显式历史交易日列表，覆盖春节休市；20/60 日端点由同一日历计算并以固定日期断言锁定。
- [x] 实验定义冻结 discovery、三个 validation 和 final_holdout 的具体起止日期；命令缺完整分区时拒绝，不从最新数据长度自动推导分区。检查分区顺序、标签越界、60 日隔离、freeze 时间早于保留区间。
- [x] 测试三个区间中只有一个有结果，不能满足 three_valid_oos_blocks；训练/发现区间不得出现在验证结果；不足跨度明确继续积累。
- [x] 将逐日独立抽样替换为连续交易日块抽样。20 日主指标 block_length=20、seed=20260907、draws=2000；各分区独立抽连续块，不跨隔离边界；缺失日期不能压缩成相邻交易日。元数据记录有效块数；不足两个完整块不能形成晋级置信区间。

```python
bootstrap_contract = {
    "method": "moving_trading_session_block_bootstrap",
    "block_length": 20,
    "seed": 20260907,
    "draws": 2000,
    "confidence": 0.95,
    "cross_partition_blocks": False,
}
```

- [x] 测试采样索引必须为连续交易日块、种子复现、零差异不晋级；不得仅断言输出 method 字符串。
- [x] 按第 3 节冻结条件输出逐臂、逐区间的实际计数和 gate；补齐 60 日成熟度、最终保留集确认与缺失覆盖。测试“20 日全通过但 60 日 pending”最多只能进入 shadow。
- [x] 运行 experiments 目标测试及质量门禁；通过后保存独立提交 `fix: validate evolution time partitions`。

### Task 7：证据驱动的晋级与保留集管理

**Modify:** `scripts/core/evolution_registry.py`、`scripts/analysis/evolution_job.py`、`scripts/backtesting/recommendation_experiments.py`。
**Test:** `tests/test_recommendation_experiments.py`、`tests/test_evolution_job.py`。

- [x] 先让旧测试中的 `transition(..., "eligible", {"result": "ok"})` 明确失败；补合法证据发布、缺 60 日、缺 shadow、其他实验结果、摘要不符、篡改 gate 和旧 schema 的拒绝用例。
- [x] 增加证据加载/验证入口：从现有实验和 shadow 存储解析 ID，检查内容哈希、定义和输入一致性，以实际指标重算全部 gate；不信任调用方传来的布尔值。状态迁移与 publish 都调用同一验证入口。

```python
release_evidence = {
    "schema_version": "evolution-release-evidence/v2",
    "experiment_id": experiment_id,
    "validation_result_id": validation_result_id,
    "holdout_result_id": holdout_result_id,
    "shadow_result_id": shadow_result_id,
    "contract_id": contract_id,
}
```

- [x] shadow 必须消费真实的独立影子快照，累计至少 20 个完整配对且 20 日已成熟的正式推荐日期；记录输入一致、生产历史未写入、数据覆盖符合阈值及平均差值非负。历史重放结果不能冒充前瞻 shadow。
- [x] 按研究批次登记 holdout 首次消费：先原子登记结果任务 ID 再评价；同一冻结输入的中断可幂等恢复，修改参数后再次使用相同 holdout 拒绝。消费记录和失败尝试均追加保存。
- [x] 新发布要求 v2 完整证据；旧注册与结果只读保留，不自动赋予 eligible。合法旧活动版本不在本次修复中擅自撤回；显式标记 legacy_unverified，并保留已授权回滚路径。
- [x] 保留人工显式 publish；成功后原子更新指针。回滚不受新策略研究门槛阻塞，也不改旧正式记录。
- [x] 运行 experiments/job 目标测试及质量门禁；通过后保存独立提交 `fix: verify evidence before strategy release`。

### Task 8：监控、重试与故障状态

**Modify:** `scripts/analysis/evolution_job.py`、`scripts/core/evolution_registry.py`。
**Test:** `tests/test_evolution_job.py`。

- [ ] 用哈希名顺序与日期相反的五个任务夹具验证：最近运行按 `as_of` 和实际完成时间排序；同一天多次尝试归并为最终阶段状态，失败后成功不重复增加失败日。
- [ ] 无任务、无成熟收益、缺覆盖分母返回 insufficient_data，不能 healthy；上游失败与未成熟分开计数。
- [ ] 监控数据失败率使用最近五个预期交易日，收益使用最近 60 个交易日内已成熟的推荐 cohort 并分实际策略版本汇总；主指标调用任务 3 的日期等权函数，附事件成熟度。样本不足仅描述。
- [ ] 实际计算推荐日期覆盖、研究记录完整率及到期 Outcome 覆盖；与版本登记的阈值比较。统计退化只标记 review_required，接口/契约故障停用受影响实验并恢复已验证前版本，前版本不可用则使用内置基线。

```python
if contract_failure:
    status = "fallback_required"
elif not sufficient_monitoring_data:
    status = "insufficient_data"
elif degradation_reasons:
    status = "review_required"
else:
    status = "healthy"
```

- [ ] 用临时 release root 测试故障回退、重复回退幂等、原正式快照未变；普通收益下滑不得触发自动调参。
- [ ] 运行 job 目标测试与质量门禁；通过后保存独立提交 `fix: monitor evolution by business date`。

### Task 9：端到端验收与文档校正

**Modify:** `tests/test_evolution_job.py`、`tests/test_stock_trend.py`、`.claude/specs/today-recommendation-evolution-plan.md`。

- [ ] 临时目录内完成一个合成闭环：冻结完整研究池 → 多截止版本评价 → 去重诊断 → 有/无模型提案 → 登记实验 → 时间外验证 → 保留集 → shadow → 显式发布 → 回滚。行情使用注入的离线夹具，不访问网络、不触碰生产缓存。
- [ ] 验收两个终态：证据充分的合成样本能显式发布并回滚；现实冷启动形状在缺样本/60 日未成熟时稳定停止于继续积累。测试成功不等于真实策略有效。
- [ ] 验证所有新结果关联输入摘要；同输入重跑稳定，旧正式快照摘要不变；故障日志含阶段、对象和原因且不含凭据。
- [ ] 每次 scripts Python 修改后，目标测试通过，再运行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

预期：主入口和 golden 均零失败，diff 无空白错误。不能复用旧计划中的 557/21 结果宣称通过；记录本次实际数量。若 golden 变化，逐项解释是否属于预期，不能为消除失败直接重生成。

- [ ] 更新原实施计划的完成记录，明确哪些约束在 v2 才得到落实，增加新增门槛、旧数据资格和实际运行证据；本文件各任务打勾仅依据新验证结果。
- [ ] 保存文档与验收独立提交 `docs: record evolution repair validation`。最终交付说明改动、测试、兼容边界和真实研究成熟度未知项；本计划不包含对真实策略执行 publish。

## 5. 批次完成条件

| 批次 | 任务 | 必须证明的结果 |
|---|---|---|
| A：数据可研究 | 1–4 | 新样本进入周任务、淘汰可投资对象有评价、截止可信、统计统一、未成熟不出提案 |
| B：实验可审查 | 5–7 | 重放等价、实际时间块统计、有效分区、60 日及 shadow 证据、不能绕过发布门槛 |
| C：运行可维护 | 8–9 | 无数据不报健康、重试可恢复、故障可回退、离线全链路及质量门禁通过 |

工程完成允许没有任何真实实验晋级。交易日自然成熟、来源历史时间戳不足或缺完整研究历史，均需如实显示，不能用随机分割、当前成分回填或放宽阈值替代。

本计划仅供学习参考，不构成投资建议。

## A 批次执行记录（2026-09-08）

- 任务 1–4 已完成代码实现与集成验证：默认研究入口切换为 `evolution/research`，周任务及诊断按 `evaluation_as_of` 过滤；冻结研究池中的可投资候选（含未入选 Top N 对象）均产生独立信号 Outcome，硬排除项保留 `excluded` 状态；评价契约升级为 v2 并绑定记录、研究快照和人群身份。
- 事件去重与日度 alpha 汇总统一复用 `core/research_events.py`：同市场同代码重叠区间只保留最早事件锚点，跨市场不合并，缺失/非法端点不计入成熟事件；日度先股票等权、再日期等权。
- 提案门控改为有限 alpha 的 20 个成熟日期及 100 个去重事件；每周生成/修复尝试和最多 3 条提案使用原子锁与输入摘要绑定，响应支持按尝试号幂等恢复，跨诊断包不会重放旧响应。
- 新增来源时间、决策时间、评价截止、冲突和研究链接缺口状态；缺口只进入内存审计，不写入正常评价 sidecar。`run_close` 同时返回当日采集与历史成熟度阶段，交易模拟资格与候选信号资格分开。
- 验证证据：A 定向测试 attribution 23、diagnostics 28、job 7、events 4、snapshot 6 全部通过；全量门禁 `590 passed, 0 failed, 1 skipped`，golden `21 passed, 0 failed, 2 warnings`，`git diff --check` 通过。
- 已验证状态：`module_delivered`、`integration_verified`。当前未声称 `research_ready` 或 `release_eligible`；真实样本成熟度仍需后续运行确认。C 任务（监控与端到端发布验证）保持未执行。

## B 批次执行记录（2026-09-08）

- 任务 5 已完成：实验重放直接调用生产 `select_candidate_pool` 与 `classify_candidates`，冻结 preselection 资格、层级和选择参数；缺失、冲突、损坏或重复的研究记录拒绝整日重放，跨市场代码按市场隔离，Outcome 缺失不伪造覆盖。
- 任务 6 已完成：实验定义要求显式 trading-session calendar、discovery、三个 validation block、final holdout、freeze_at 及至少 60 个交易日 purge；20/60 日标签端点按同一日历校验，连续交易日 moving block bootstrap 固定为 block length 20、seed 20260907、draws 2000，跨分区或缺交易日不能压缩采样；60 日确认不足时只保留 shadow。
- 任务 7 已完成：注册表迁移与 publish 共用 v2 证据校验，重算实际 gate 并校验结果内容哈希、结果 ID、定义、输入清单、独立 shadow 与 holdout 消费；裸 evidence、旧 schema、篡改摘要、缺 shadow/60 日或重复消费均拒绝。holdout 首次消费原子登记，同输入幂等，参数变更写入失败尝试并拒绝。
- B 初次实现提交：`36b07eb fix: replay production candidate selection`、`f993fc4 fix: validate evolution time partitions`、`90814d7 fix: verify evidence before strategy release`、`878d313 fix: harden evolution evidence inputs`。复审加固后的最终门禁见下方记录。
- 已验证状态仍为 `module_delivered`、`integration_verified`；B 代码具备审查级阻断能力，但未声称真实样本 `research_ready` 或 `release_eligible`，也未执行正式策略 publish。C 批次（监控、重试、端到端离线验收）留待后续。

### B 批次复审加固（2026-09-08）

- 注册入口现在强制冻结并校验完整交易日历、discovery/三个 validation/final_holdout 分区、freeze_at、60 日 purge；发布时将结果日历和分区与注册定义逐项比较。默认评价契约改为与 `recommendation_attribution` 的冻结研究池合同一致（`6b0d9d40299b003e`），Outcome loader 校验 payload 合同并把合同绑定注入每一行。
- validation/holdout 结果写入注册 `experiment_id`；holdout 消费凭证的输入摘要必须等于评价前由分区输入计算的派生摘要，不能覆盖规范摘要。结果持久化使用安全 ID、唯一临时文件和冲突即失败的幂等策略。
- 注册表从原始 event alpha 重算逐日 paired/60 日均值与差值，并按 `record_id/market/code/recommendation_date` 绑定 MAE；缺原始行、伪造摘要、错误端点、覆盖分母或 bootstrap 均拒绝。前瞻 shadow 聚合要求每个正式 source snapshot 恰对应一个 basis_date，paired/coverage/event 日期集合、来源摘要和 manifest 完全一致；source snapshot 的 digest、回放参数、两臂选择身份也必须逐项绑定。
- 复审新增回归：experiments `21`、job `19`、diagnostics `29` 全部通过；manifest 缺少生产候选输入、holdout 派生摘要不一致、shadow event 使用未入选证券、选中事件未成熟或身份集合缺失均有拒绝用例。最终全量门禁 `620 passed, 0 failed, 1 skipped`（唯一跳过项为资金流网络超时），golden `21 passed, 0 failed, 2 warnings`，`py_compile` 与 `git diff --check` 通过。仍只验证 `module_delivered`、`integration_verified`，不宣称真实样本成熟或执行 publish；C 批次保持未执行。
