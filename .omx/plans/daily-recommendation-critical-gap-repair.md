# 每日推荐关键性能缺口修复计划

## 目标与边界

关闭 `docs/2026-08-12-daily-recommendation-performance-fix-plan.md` 中已确认未落地的 P0-2、P1-1、P1-2、P1-3 和完成证据缺口，同时保持推荐阈值、评分权重、日期新鲜度、市场/板块/覆盖率质量门、CLI 与 JSON 兼容性，以及“过期或不可用数据只进入观察池”的降级语义。

本轮不新增依赖，不读取或修改既有 `reports/`，不实施 P2 fetcher 去子进程化。只有完成本计划后，基准仍失败且证据明确指向子进程启动成本，才另立 P2 计划。

## 成功标准与停止条件

1. 同一股票的 K 线、资金、基本面在单次扫描中各最多获取一次。
2. 重复股票保留完整板块归属；交换板块遍历顺序不改变主板块、最终排序和板块派生评分。
3. 后续更优板块能更新主板块及所有板块派生字段，但不重新抓取个股数据。
4. 每类来源先有限探测；熔断后不再提交新的实时请求，只允许既有在途任务结束并让剩余任务走缓存路径。
5. 来源状态能审计失败分类、请求、缓存命中、失败、熔断和状态事件。
6. 错误来源、错误质量、过期日期或不足根数的缓存不能命中有效快速路径。
7. JSON、stderr、Markdown、HTML 都能审计阶段耗时、来源状态、缓存、熔断和漏斗计数。
8. 固定 20 板块、约 250 条原始股票、30% 重复 fixture 覆盖正常、慢响应、完全不可用三态。
9. 确定性断网 fixture 证明请求有界且理论等待预算不超过 45 秒；实际断网扫描不超过 45 秒。
10. 正常网络暖缓存不超过 30 秒；冷缓存记录规模、调用量和耗时基线。
11. 定向测试、`test_stock_trend.py`、`test_golden.py --diff` 全部通过。
12. 全部通过后停止，不启动 P2。

## RALPLAN-DR

### 原则

1. 质量门优先，性能优化不得扩大可执行推荐集合。
2. 运行级来源状态和计数使用单一事实源。
3. 请求必须有界且能通过确定性测试证明。
4. 新字段只做 additive change，保留现有 CLI、JSON 和降级链。
5. 测试先行、小步提交、每阶段验证。

### 主要驱动因素

1. 当前一次性提交所有 futures，达到失败阈值后仍无法阻止排队请求。
2. 重复股票首次遇到的板块永久成为主板块，且归属证据不完整。
3. 当前缓存校验和性能审计不足以证明数据质量或定位性能退化。

### 方案比较

#### 方案 A：共享 `RunSourceHealth` + 有界 in-flight 调度（采用）

优点：集中状态机、失败分类和指标；能证明熔断后的请求上限；五类来源共享契约。缺点：增加一个核心模块和并发调度测试。

#### 方案 B：保留裸字典，各阶段分别加入探测循环

优点：初始改动小。缺点：线程安全、失败原因和审计逻辑重复；排行需要独立实现；容易发生状态语义和计数漂移。

选择 A，但健康组件只负责运行级状态、记账和审计，不吸收 fetcher、缓存或评分业务。

## 实施阶段

### 阶段 0：测试先行，锁住兼容性和缺口

涉及：

- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_stock_scanner.py`
- 新增 `.claude/skills/stock-trend/tests/test_daily_recommendation_performance.py`

先增加失败测试：排行单次调用；同批/跨批重复归属；后续更强板块成为主板块；板块顺序不变性；重复股票不重复获取三类个股数据；DNS/timeout/HTTP/empty 分类；熔断请求上限；错误 K 线/基本面缓存不能命中；现有评分、权重和资格的重构前基线。

验收：新测试只因已知缺口失败，原有测试保持通过，不更新 golden。

### 阶段 1：完成 P0-2 主板块择优和完整归属

涉及：

- `stock_scanner.gather_candidates`
- `stock_scanner.run_phase2`
- `stock_scanner.score_sector_strength`
- `daily_candidates.scan_sectors`
- 建议私有 helper：`_build_sector_membership`、`_sector_preference_key`、`_merge_sector_membership`、`_rebind_primary_sector`

实施：

1. `gather_candidates()` 聚合同批重复股票的所有板块归属。
2. 每条归属至少保存板块 code/name、可执行资格、评分、持续性、相对强弱、`ranking_position`、排行来源/日期/质量，以及成分来源/日期/质量/缓存错误。`ranking_position` 必须来自本次运行复用的全市场排行快照位置，不得使用批内或发现顺序。
3. 使用稳定比较键选择主板块：`sector_actionable`（已经包含排行/成分 freshness 与 quality 判定）、板块评分、持续性评分、相对强弱按降序；排行位置按升序；板块代码按字典序作最终 tie-break。缺失数值按负无穷处理，缺失资格按不可执行处理，缺失排行位置排在已知位置之后。mixed fresh/stale 归属必须优先选择满足质量门的 actionable 板块，即使 stale 板块原始热度更高。
4. `scan_sectors()` 合并后续归属后重新选择主板块。
5. 扫描上下文为每个板块维护确定性的 peer-change cohort，cohort 定义为该板块 Phase 1 硬过滤后的 unique raw candidates；按股票 code 去重，保存 change_pct，不因 Wyckoff/数据质量/批次结果变化。板块上下文随新批次合并，并在评分或重绑时按 code 排序后调用同一个纯 `score_sector_strength`/relative-rank helper。后批 peer 到达时，对该板块已评分候选的板块派生分进行无 I/O 重算，保证批次顺序不影响结果。
6. 主板块变化只复用既有 K 线、资金、基本面和 Wyckoff，不复用已经叠加 membership 的最终质量判定。评分时保存不可变 `base_data_quality`（只含 K 线/资金/基本面）；初评和重绑都从它重新应用所选 membership 的 source/date/quality overlay，得到新的 `data_quality.eligible/reasons/freshness_factor`，再计算质量调整分。
7. 重算字段包括 `sector_*`、`dimensions.sector_strength`、板块内相对排名、`data_quality`、`composite_score/raw_composite_score` 和 `quality_adjusted_score`。
8. 将复合分、membership overlay 和板块强度计算分别提取为纯函数，初评和重绑共用。
9. 输入板块在扫描开始时必须按共享排行快照的 `ranking_position`、板块代码规范化顺序，并形成不可变 batch frontier；不接受调用方排列作为执行顺序。提前停止只允许发生在完整 batch 的 membership/cohort 合并、重绑重算和 `final_valid_count` 计算之后。相同排行快照、batch size 和数据 fixture 必须处理相同 frontier；测试需实际触发提前停止，并随机打乱输入板块顺序验证候选全集、cohort、主板块和排序一致。
6. 将现有复合分公式提取为纯函数，初次评分与重绑使用同一实现。

验收：同批/跨批仅保留一个候选；归属完整且稳定；后出现强板块可重绑；mixed fresh/stale 优先 actionable；交换输入顺序结果一致；后批 peer cohort 不同仍得到相同板块分；fresh→stale 与 stale→fresh 重绑均重新得到正确资格和原因；非板块维度和抓取次数不变。

### 阶段 2：完成 P1-1 运行级来源健康和有界调度

新增：

- `.claude/skills/stock-trend/scripts/core/source_health.py`

修改：

- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py`
- `.claude/skills/stock-trend/scripts/fetchers/sector_data.py`
- 对应测试

实现轻量、线程安全的 `RunSourceHealth`：`try_acquire_live_permit(source)`、`mark_started(token)`、`complete_success(token, live_attempt)`、`complete_failure(token, live_attempt)`、`release_unstarted(token, reason)`、`record_cache_result`、`snapshot`、`events`。独立管理 `sector_ranking`、`sector_membership`、`kline`、`capital`、`fundamental`。

`try_acquire_live_permit()` 必须在同一把锁内原子完成状态检查、在途额度检查和在途数递增，消除 TOCTOU；此时只记录内部 admission，不增加 `logical_live_requests`。任务函数实际开始时调用 `mark_started(token)`，在锁内且仅一次增加 `logical_live_requests`。token 只能通过 `complete_success(token, live_attempt)`、`complete_failure(token, live_attempt)` 或 `release_unstarted(token, reason)` 完成一次；完成时原子减少在途数，并从 `live_attempt.provider_attempts` 累加 provider 计数。executor submit 失败、future 启动前取消或任务被放弃时必须调用 `release_unstarted`，不得增加 logical request、failure 或 provider attempt，也不得泄漏 permit。scheduler 拥有 futures，健康组件只拥有 permit/状态/计数，fetcher 继续拥有 I/O。

状态语义固定为：`failure_threshold = 2` 个连续 live 失败；一次 live 成功将连续失败清零并恢复 `healthy`；cache-only 结果不恢复状态。每来源 `max_in_flight = 2`，因此完全失败时 live 请求上限为 `failure_threshold + max_in_flight - 1 = 3`。如实现阶段需要不同常量，必须先以相同公式更新测试和 ADR。

性能预算必须成为生产配置契约，而非 fixture 假设。新增集中常量（放在 `source_health.py` 或紧邻调度器）：`SCAN_DEADLINE_SECONDS = 45`、`FINALIZATION_RESERVE_SECONDS`、每类 `LIVE_ATTEMPT_TIMEOUT_SECONDS`、`MAX_PROVIDER_ATTEMPTS`。45 秒定义为候选扫描函数完成候选、指标和输出模型组装并返回的总 deadline。实现前先盘点现有 fetcher 的 HTTP、SDK、子进程 timeout 与 fallback 次数，随后填写关键路径预算表，覆盖 live I/O、cache-only 收尾、peer 重绑/评分、分类和 JSON/报告模型组装，满足 `live critical path + FINALIZATION_RESERVE_SECONDS <= SCAN_DEADLINE_SECONDS`；可并行阶段按最大分支计算。禁止继续使用无法证明总 deadline 的通用 30 秒子进程 timeout。

主流程从 monotonic start 计算绝对 scan deadline 和更早的 `live_deadline = scan_deadline - FINALIZATION_RESERVE_SECONDS`。取得 permit 和提交任务前检查 live 剩余预算；started fetch wrapper 把每个底层 timeout cap 到 live_deadline。到达 live_deadline 后停止 live 提交，未启动 token 走 `release_unstarted`，剩余项目 cache-only，并在固定 finalization reserve 内完成 peer 重绑/评分、分类和输出模型组装。已启动超时任务按 `timeout` 完成并释放 token，迟到结果不得改变本次候选或状态。scheduler 不得使用会等待迟到线程的 executor context-manager 退出路径；必须显式 shutdown/cancel pending，并确保底层网络/子进程 timeout 在 live_deadline 内返回，使主扫描不因迟到 future 阻塞超过总 deadline。

失败分类固定为 `dns`、`timeout`、`http`、`empty`、`parse`、`subprocess`、`unknown`；审计事件至少包含 `source_unavailable`、`cache_only`、`data_stale`、`circuit_opened`。

所有五类 live fetch 使用同一非破坏内部 attempt 契约：scheduler/fetch wrapper 返回 `{payload, live_attempt}`，其中 `live_attempt` 至少含 `attempted`、`reason`、`cache_used`、`stale`、`subprocess_started`、`provider_attempts`；`provider_attempts` 为非负整数，按实际 HTTP/SDK/子进程/fallback attempt 累计。现有公开函数/CLI 继续返回原 payload；可通过 companion wrapper 或可选内部返回模式提供 attempt，禁止破坏现有消费者。`sector_ranking`、`sector_membership`、`kline`、`capital`、`fundamental` 都必须覆盖此契约。

指标命名区分两层：`logical_live_requests` 是成功取得 permit 并启动一次 scanner 级 live fetch 的次数；`provider_attempts` 是 fetcher 内部 HTTP/SDK/子进程及 fallback 重试总数。熔断阈值和请求上限只基于前者；后者用于诊断并与 `live_attempt` 明细对账。保留旧 `requests` 字段时，明确映射为 `logical_live_requests`。

调度要求：

1. 先提交不超过探测窗口的任务。
2. 探测健康后逐步补充任务；只有取得原子 live permit 才能提交实时任务。
3. 熔断后不再提交新的实时任务，剩余任务走 cache-only。
4. 请求上限使用 `failure_threshold + max_in_flight - 1`，默认断言为 3。
5. 板块排行纳入同一运行上下文，并记录实时、缓存、过期缓存和失败原因。
6. 所有来源 wrapper 暴露结构化 live-attempt 结果，同时保持现有函数、CLI 和缓存 fallback 兼容。

验收：故障分类准确；熔断后无新增实时请求；cache-only 不重置熔断；五类来源互不污染；并发压力下计数准确且熔断事件只记录一次。

### 阶段 3：收紧 P1-2 缓存快速路径

涉及：

- `stock_scanner._fetch_kline`
- `stock_scanner._fetch_capital_flow`
- `stock_scanner._fetch_fundamental`
- `stock_scanner._usable_capital_payload`
- `stock_scanner._usable_fundamental_payload`
- `recommendation_quality._dimension`
- `recommendation_quality.assess_candidate_data`

建议新增纯 validator：`_validate_kline_cache`、`_validate_capital_cache`、`_validate_fundamental_cache`，返回结构化 verdict，而非只返回布尔值。

规则：

1. 主流程先计算唯一的 `expected_trading_date` 并传给三个 validator；validator 禁止自行用自然日推断。K 线必须结构合法、根数足够、覆盖 `expected_trading_date`、来源非空且非 error、无 payload/meta/refresh error、质量非 error。
2. 资金必须校验来源、质量、有效记录、交易日和盘中/盘后 TTL。
3. 基本面必须校验来源、summary、data_quality 和 30 分钟/16 小时 TTL。
4. `cache_only=True` 可返回无效缓存供诊断，但必须保留 stale/error verdict 并由质量门降为观察。
5. 补齐交易日盘中、盘后、周末、节假日、节前最后交易日测试。
6. 有效命中时 `run_script()` 必须未调用；无效缓存仍走原 fetcher。

验收：错误/过期缓存不算有效命中；盘后不误用 T-1；休市日使用最近有效交易日；旧缓存只进入观察池且原因可审计。

### 阶段 4：补齐 P1-3 指标与输出审计

涉及：

- `daily_candidates.main`
- `daily_candidates.scan_sectors`
- `daily_candidates.generate_report`
- `daily_candidates._generate_html`
- `daily_candidates.build_json_output`
- `stock_scanner.gather_candidates`
- `stock_scanner.run_phase2`

性能契约：

- 阶段：`sector_ranking_seconds`、`sector_membership_seconds`、`kline_seconds`、`wyckoff_seconds`、`capital_seconds`、`fundamental_seconds`、`report_seconds`、`total_seconds`。
- 漏斗：`batch_count`、`raw_candidate_count`、`unique_candidate_count`、`wyckoff_pass_count`、`final_candidate_count`、`final_valid_count`、`actionable_count`。
- 每类来源：`logical_live_requests`（兼容映射 `requests`）、`provider_attempts`、`cache_hits`、`failures`、`circuit_breaks`、`failure_reasons`、`state`。

实现：

1. 来源指标只由 `RunSourceHealth.snapshot()` 生成，renderer 只读取。
2. 保留现有 flat 字段；新的规范化 `sources` 结构为 additive change。
3. `sector_membership_seconds` 累计所有批次。
4. `final_valid_count` 与提前停止使用同一个资格谓词 helper。
5. stderr 输出稳定、可测试的摘要；Markdown/HTML 增加“性能与数据源审计”。
6. `report_seconds` 定义为报告/JSON 展示模型与正文组装耗时，不包含承载该字段的最终序列化、stdout 输出或文件写出；用单调时钟测量一次组装，随后注入最终 envelope，避免自计时循环。
7. JSON 模式输出完整 performance，但不写报告文件。
8. 开启指标不得改变评分、顺序或分类。

验收：字段和类型稳定；耗时非负且总和在明确余量内；现有 JSON 顶层字段保留；四种输出可审计；启用/禁用指标的结果完全一致。

### 阶段 5：确定性性能与故障证据

在 `test_daily_recommendation_performance.py` 建立固定 fixture：20 板块、约 250 条原始股票、30% 重复、固定 K 线有效比例和 Wyckoff 通过比例、正常/慢响应/完全不可用来源、mock 单调时钟和 fetcher，不依赖公网。

断言：排行一次；深度分析次数等于唯一股票数；资金/基本面不超过 Wyckoff 通过数；故障时实时请求不超过设计上限；熔断后剩余任务 cache-only；理论等待预算不超过 45 秒；慢响应不退化为候选数乘超时；指标与 mock 调用一致；板块顺序不影响结果；无效缓存不产生可执行候选。

确定性测试必须直接导入生产 deadline、finalization reserve、per-source timeout 和 provider-attempt 常量，按生产阶段串/并行拓扑计算最坏关键路径；断言 `live critical path + cache-only/finalization reserve <= 45`，不得用任意较短 mock delay构造通过结果。覆盖 deadline 前正常返回、单来源 timeout、多个来源依次不可用、live deadline 时未启动/已启动任务、cache-only/重绑/评分/输出收尾，以及迟到 future 被忽略且不阻塞主扫描返回。

验收：重复运行稳定，无 wall-clock 松散断言，测试输出能解释请求上限和等待预算。

### 阶段 6：真实基准、文档和完成判定

更新 `docs/2026-08-12-daily-recommendation-performance-fix-plan.md`：

1. 记录正常暖缓存（≤30 秒）、正常冷缓存（规模/调用量/耗时基线）、完全不可用（≤45 秒）三组基准。
2. 修正无效的 `python3 -m unittest .claude/...` 命令。
3. 只有测试与基准有证据时才把 P0-2/P1-1/P1-2/P1-3 标为完成。
4. 保留 P2 deferred，并记录启动门槛。

若断网仍超过 45 秒，先按审计字段定位阶段；探测或调度是瓶颈则继续修 P1-1。只有暖缓存证据明确指向子进程启动，才另立 P2 ADR。

## 验证命令

```bash
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_recommendation_performance.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
python3 -m py_compile \
  .claude/skills/stock-trend/scripts/core/source_health.py \
  .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  .claude/skills/stock-trend/scripts/scans/daily_candidates.py
```

实际基准命令：

```bash
python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py \
  --json --top 30 --min-candidates 20
```

验证顺序：定向测试与性能 fixture → 主质量门 → golden diff → 暖/冷/断网实际基准 → 对比 JSON 候选排序、资格和关键评分 → 更新计划状态。

## 风险与缓解

- 主板块重绑导致评分漂移：初次评分与重绑共用纯函数，并加顺序不变性测试。
- 并发状态竞争突破请求上限：状态加锁、有界补充 future、测试精确上限。
- fetcher fallback 隐藏真实失败：保留现有返回，同时增加结构化 live-attempt 诊断。
- 指标重复记账：健康组件独占来源记账，renderer 只读。
- 缓存规则过严导致重复刷新：用盘中/盘后/休市日矩阵锁定规则。
- 为通过测试放宽质量门：阈值与 golden 不得随逻辑提交修改，差异必须逐项解释。
- P2 范围蔓延：基准未证明必要时立即停止。

## ADR

### Decision

引入轻量、运行级、线程安全的 `RunSourceHealth`，配合有界 in-flight 调度；重复股票使用完整归属证据和确定性主板块比较键，主板块变化只重算板块派生评分。

### Drivers

- 并发环境下必须证明实时请求有界。
- 所有来源必须共享一致状态、失败分类和审计口径。
- 必须保持评分、质量门、CLI 和 JSON 兼容。
- 避免无证据地启动 P2 大范围重构。

### Alternatives considered

- 保留裸字典、各阶段分别补探测：改动少，但重复且容易漂移。
- 全部串行探测：上限容易证明，但正常网络性能退化。
- 立即实施 P2：不能解决请求无界、错误缓存和审计缺失，范围过大。
- 先收集全部板块再统一评分：主板块容易正确，但破坏提前停止语义。

### Why chosen

共享健康组件与有界调度能直接解决请求无界并保留并发；主板块重绑复用既有个股数据，避免重复 I/O 和大范围流程重构。

### Consequences

获得可证明、可审计的故障行为和一致的归属评分；代价是新增核心模块、并发测试，以及对扫描函数参数的兼容性扩展。

### Follow-ups

本计划通过后维持 P2 deferred。若暖缓存仍超标且证据指向子进程，再新建 P2 ADR。健康组件是否复用到其他扫描工作流另行评估。

## 提交拆分

1. 失败测试：主板块、缓存质量、熔断上限、性能 fixture。
2. P0-2：完整归属、主板块择优、板块派生分重绑。
3. P1-1：`RunSourceHealth`、结构化失败、有界调度。
4. P1-2：缓存 validator 与日期矩阵。
5. P1-3：指标、stderr、JSON/Markdown/HTML 审计。
6. 基准、文档状态和测试命令。

每个修改 scripts Python 的提交都运行两道仓库质量门；golden 更新不得与逻辑修改混在一起。

## Available-Agent-Types Roster 与执行建议

- `executor`（medium）：P0-2 或 P1-1/P1-2 的单一所有权实现。
- `test-engineer`（medium）：fixture、并发上限和日期矩阵。
- `verifier`（high）：独立验收测试、指标和基准。
- `code-reviewer`（high）：并发安全、兼容性和评分漂移审查。
- `architect`（xhigh）：仅在健康组件边界或评分重绑存在争议时介入。
- `git-master`（high）：提交边界和集成。
- `explore`（low）：补充符号定位。

建议顺序：test-engineer 先交失败测试；P0-2 executor 完成并验证；P1-1/P1-2 executor 完成并验证；单一 owner 收尾 P1-3 和文档；verifier 在集成状态验收。共享文件较多，不建议多个 lane 同时编辑 `daily_candidates.py` 或 `stock_scanner.py`。

## Goal-Mode Follow-up Suggestions

- 推荐 `$performance-goal`：本任务有明确的延迟、调用量和质量 evaluator。
- 若需把六个提交作为 durable 多目标管理，可用 `$ultragoal`；并行部分可结合 `$team`，由 Ultragoal 持有完成账本。
- `$autoresearch-goal` 不适用，本任务证据来自本地测试和基准。
- `$ralph` 仅作为无法使用 team runtime 时的显式单 owner 回退。

Team 启动提示：

```text
$team 按 .omx/plans/daily-recommendation-critical-gap-repair.md 执行；
P2 明确排除。test-engineer 先锁失败测试，executor 分阶段取得共享文件所有权，
verifier 独立运行定向测试、两道质量门和三类基准。
```

Team 在关闭前必须提供：fixture 请求上限、候选等价性、定向测试、主质量门、golden diff 和三类实际基准证据；Ultragoal 再将这些证据写入 durable checkpoint。

## Changelog

- 初稿：基于 2026-08-13 缺口审计建立修复边界、实现阶段、验收标准和执行门控。
- 架构审查修订：改用原子 permit；固定连续失败、恢复和在途上限；明确主板块缺失值/tie-break、统一交易日契约及 `report_seconds` 边界。
- Critic 修订：补齐五类来源 `live_attempt` 契约及两层请求计数；增加排行位置、mixed fresh/stale 规则、跨批 peer cohort；重绑从不可变基础质量重新应用 membership overlay。
- 二次架构修订：完成接口原子接收 provider attempts，并规定 submit/取消释放；扫描按共享排行形成确定性 frontier，只在完整批次边界提前停止。
- 最终架构修订：permit 只记录 admission，任务实际开始时 `mark_started` 计入 logical request；未启动释放不污染请求、失败或 provider 指标。
- 最终 Critic 修订：将 45 秒目标提升为生产 deadline/timeout/retry 常量和可计算关键路径预算，明确到期、取消、迟到结果与 cache-only 行为。
- Deadline 架构修订：45 秒定义为函数返回总预算，live I/O 提前在 finalization reserve 前截止，并证明迟到 future 不阻塞返回。

本计划只涉及系统可靠性和性能；股票分析结果仅供学习参考，不构成任何投资建议。
