# 今日推荐：有效候选统计与后台监控预算修复计划

日期：2026-09-30。状态：已实施并验证。执行方式：按下列依赖顺序完成三个独立改动批次，先补回归测试，再修改实现。

## 目标与边界

1. P0：`final_valid_count` 使用扫描停止条件、候选选择所用的同一冻结 `policy` 和资格谓词。
2. P1：取得后台执行锁后，close、weekly 的失败或超时不能耗尽或绕过 monitor 的独立预算；监控读取本次 close 的真实终态。
3. 兼容现有任务查询、续跑和旧 manifest；后台同周 weekly 复用规则与同步入口一致。

本次不修改正式选股门槛、排序、推荐名额、评价收益口径或策略发布规则；不新增依赖、不安装定时器、不回写历史正式推荐。计划阶段只创建本文，不运行行情、不读取 reports/。

## 已确认依据

以下代码路径均相对于 `.claude/skills/stock-trend/`，行号为本计划创建时的位置：

| 证据 | 当前行为 | 修复含义 |
| --- | --- | --- |
| `scripts/scans/daily_candidates.py:322`、`:363`、`:2524` | 同一资格谓词已读取资金证明、等待触发模式；扫描停止条件已传 policy | 复用既有谓词，不另造统计门槛 |
| `scripts/scans/daily_candidates.py:571`、`:611`、`:5336` | `_complete_performance` 未接收 policy，最终有效数调用未传 policy | 增加可选关键字参数并从 main 传入冻结策略 |
| `scripts/scans/daily_candidates.py:3655` | 正式分桶额外按市场模式和名额截取 | 有效资格数与最终推荐条数是不同指标 |
| `tests/test_daily_candidates.py:294` | 已测基础有效数一致性，未覆盖策略敏感情况 | 增补资金证明、等待回踩和名额边界测试 |
| `scripts/bridge/today_background.py:296`、`:401`、`:447` | 锁、close、weekly、monitor 共用 deadline，阶段超时直接结束任务 | 拆开锁预算和各阶段预算，将阶段超时落成可续跑结果 |
| `scripts/bridge/today_background.py:77`、`:339`、`:357` | PID 失活判断用旧总预算；weekly/monitor 检查点不绑定 close 结果 | 同步调整生命周期判断和依赖指纹 |
| `scripts/analysis/evolution_job.py:185` | run_close 的 `except Exception` 可以捕获普通 TimeoutError | 阶段截止信号不能使用会被底层普通异常处理吞掉的异常 |
| `scripts/analysis/evolution_job.py:70`、`:243`、`:314` | monitor 读取持久化 close 任务；缺失/failed/upstream_gap 进入数据失败统计 | close 超时或异常必须写入任务记录，不能只写后台状态 |
| `scripts/bridge/run_today.py:93`、`:321` | 同步路径已验证同周成功、非空样本和摘要后跳过 weekly | 抽出共享判断供后台使用，保留既有规则 |
| `references/today-and-candidates.md:15` | 已规定 close/weekly/monitor 各自 300 秒 | 选择独立阶段预算，恢复文档契约 |
| `docs/today-recommendation-evolution-usage.md:47` | 使用说明仍称核心后处理总共 300 秒 | 同步修正文档，明确后台最长等待增加 |

## 设计决策

### A. 有效候选数的明确含义

定义：在本次输出候选集合内，按冻结 policy 通过 `_is_final_valid_candidate(item, min_score, policy)` 的条数；统计发生在推荐名额截取之前。

- `final_candidate_count` / `output_candidate_count` 仍表示输出候选数。
- `final_valid_count` 仍表示候选资格数；不改成 actionable 和 waiting_trigger 的长度之和。
- 今日可执行、等待触发条数仍从已有 buckets 读取。
- 弱市的最终推荐可以为零，而候选资格数仍非零；不为使数字看起来一致而额外引入市场模式过滤。
- `_complete_performance(..., *, policy=None)` 保持旧调用可用；正式 main 调用必须显式 `policy=policy`。缺省路径保留历史行为，仅作兼容。

### B. 独立阶段预算与锁

选择独立阶段预算，保留现有 `daily → close → weekly → monitor → evaluation` 顺序。比较过保留300秒总预算并预留少量监控时间的方案：该方案仍会压缩历史评价/研究，且无法依据现有证据给 monitor 确定足够的小额预算，与技能文档的各自300秒契约不符。

新任务使用 `today-recommendation-background/v2`，冻结以下预算：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `lock_wait_budget_seconds` | 30 | 仅获取 postprocess 锁的等待预算 |
| `close_budget_seconds` | 300 | close 回调及其任务记录持久化预算 |
| `weekly_budget_seconds` | 300 | weekly 回调及其任务记录持久化预算 |
| `monitor_budget_seconds` | 300 | monitor 回调及其任务记录持久化预算 |
| `factor_daily_budget_seconds` | 10 | 沿用每日消融预算 |
| `factor_evaluation_budget_seconds` | 20 | 沿用成熟评价预算 |

新增一个共享的预算解析函数，run_task 和 read_status 都使用它。v2要求显式提供表中锁/close/weekly/monitor四个字段；研究字段可沿用默认值。所有预算必须是有限正整数（拒绝bool、零、负数和非数值），非法或缺失必需字段返回reason=`invalid_budget_config`，未知schema返回reason=`unsupported_schema`，不能静默改成无期限。新任务在ensure_task/launch前校验，失败返回workflow.postprocess=`launch_failed`及上述reason，不创建无法运行的任务；已存在任务遇到相同配置问题时，run_task写status/result=`failed`及上述reason。read_status返回同样的明确failed诊断，不把有效存在但配置非法的任务返回missing，也不让其永久running。

- `STOCK_TREND_BACKGROUND_BUDGET` 在新任务中作为三个核心阶段各自的默认预算；v2 manifest 同时写显式阶段字段和兼容 `budget_seconds`。环境变量语义变化必须写入使用说明。
- v1 或未标版本的旧 manifest 在内存解析：三个核心阶段各自取原 `budget_seconds`（缺失取300），锁取 `min(30, budget_seconds)`，研究预算沿用旧字段/默认值。不修改原 manifest、task_id 或历史文件。
- 默认各项预算相加为960秒，代替旧330秒；这是配置预算和，不是包含所有无定时器磁盘写入的硬实时总时限。报告仍先返回，后台可能更久。
- 锁预算从尝试获取时起算；阶段预算从阶段开始执行时起算。锁持续保护核心阶段，不能为保证 monitor 运行而移除锁，因为监控可触发策略安全恢复。
- 锁未取得：任务 `timed_out`，明确 reason=`postprocess_lock_timeout`，monitor 显式 `skipped`/reason=`postprocess_lock_unavailable`，可续跑。监控执行保证仅适用于成功取得锁且状态存储可用的任务。
- 当前支持 macOS/Linux 的 setitimer 路径；无截止机制的平台必须明确标记不支持，不能把预算当作已强制执行。

### C. 超时终态、持久化与续跑

1. 为 `_stage_timeout` 引入专用私有截止异常（继承 BaseException），仅由核心/研究阶段边界捕获；避免 run_close 和依赖库的 `except Exception` 吞掉阶段截止。兼容测试/回调主动抛出的普通 TimeoutError；不捕获 KeyboardInterrupt/SystemExit。
2. 核心阶段边界捕获截止，返回 `{status: timed_out, reason: background_stage_timeout}` 并保存 checkpoint；普通异常返回 failed。close 或 weekly 的这些结果都继续进入 monitor。
3. 明确阶段接口为 `_stage(task_id, root, name, callback, job_root, budget_seconds, *, as_of, attempt_id, attempt_started_at)`；run_task每次实际启动生成 `uuid.uuid4().hex` 作为attempt_id，并冻结 `_now()` 为attempt_started_at，传入各核心阶段。成功/失败回调返回的content都通过 `_package(name, {**content, background_task_id, attempt_id, attempt_started_at, as_of})` 重新封装；保留回调原status/failure_class/reason等业务字段。
4. 截止或回调普通异常未产出包时，新增私有失败包构造函数 `_stage_failure_package(name, *, as_of, task_id, attempt_id, attempt_started_at, reason)`，返回既有 `_package` 结构：`status=failed`、`failure_class=runtime`、上述身份字段、reason=`background_stage_timeout`或异常类型。阶段对外仍显示timed_out/failed。阶段顺序必须为“构造包 → `_save`写入job_root/<name> → 返回含persistence的阶段结果 → checkpoint → 后续阶段”，不能只写checkpoint。close超时不能伪装成contract/interface错误，不能因此自动恢复策略。
5. 包括成功在内的每次实际核心执行都带本次attempt身份；已有失败包保留原业务失败类型再封装。否则在“原成功→新失败→重试得到相同成功content”时，`_save`会返回unchanged并保留早于失败的completed_at，monitor仍误读失败。检查点命中不产生新执行包，复用已有已验证记录。
6. close持久化成功但业务不成功：weekly跳过`close_not_complete`；monitor仍运行；evaluation标记`deferred_core_incomplete`。weekly超时：close保留，monitor仍运行，evaluation沿用原close/monitor依赖条件。
7. close包的 `_save` 失败（含回调成功后保存失败、合成失败包保存失败）：close=`failed`/reason=`persistence_failed`；weekly=`skipped`/`current_close_unavailable`，monitor=`skipped`/`current_close_unavailable`，evaluation=`deferred_core_incomplete`；任务最终=`failed`/`current_close_persistence_failed`。禁止调用真实monitor，避免读取更早close并输出错误健康结论。后台状态若还能写入则保存上述诊断；否则向worker.log/stderr报告失败，状态读取最终按失活规则处理，不承诺状态成功落盘。
8. 前置阶段业务失败/超时且记录已保存，即使monitor健康，任务也只能partial。monitor超时同样partial；锁超时保持任务timed_out；close持久化失败按第7项failed。其他核心包持久化失败标记该阶段failed并使任务partial；weekly记录保存失败不阻止读取已验证close的monitor。
9. 恢复时成功close可复用；weekly/monitor的checkpoint输入增加固定 `dependency_version=2` 和 `close_execution_job_id=close.persistence.job_id`，使用成功保存后的不可变包job_id，不能只绑定close.status。close从失败变成功后，旧monitor即使healthy/insufficient_data也必须重跑；仅研究阶段重试且close未变时，保留现有核心检查点复用。close没有有效持久化引用时，按第7项屏障处理，不构造可成功复用的monitor检查点。
10. 旧weekly/monitor检查点缺新依赖字段时作为cache miss，不回写旧输入；close保持兼容复用，但必须核对其persistence引用存在且内容摘要有效。条件性跳过`close_not_complete`、`deferred_core_incomplete`、`current_close_unavailable`、锁不可用不能当成功复用。
11. `read_status`的PID失活宽限按同一解析函数的预算总和加现有5秒计算；保留“旧result.json不掩盖新running”的既有规则。

### D. 周研究复用

将 run_today 的 `_weekly_completed` 判断移动到 `analysis/evolution_job.py` 的共享函数，旧私有入口可保留薄包装以兼容既有测试。后台取得锁后、close 完成后，按同一规则检查：同 ISO 周、日期不晚于 as_of、kind=weekly、status=completed、research_snapshots>0、内容摘要正确。

满足时返回 weekly `skipped`/`already_completed_this_week`，带可验证的来源 job_id/摘要；仅此类有依据的跳过可视为 weekly 完成。不能把所有 skipped 加入 CORE_DONE。失败、成功空跑、损坏摘要均继续允许重试。

本次沿用既有按周策略，不扩展到按每日成熟样本或输入集合重算；该变化另行研究，避免预算修复引入研究频率变更。

## 实施顺序与文件责任

### 批次1：P0 统计口径

文件：`scripts/scans/daily_candidates.py`、`tests/test_daily_candidates.py`、必要时 `tests/test_daily_recommendation_performance.py`。

1. 在现有有效数测试旁添加可失败的策略回归；用明确候选样例和预期常数断言，不能仅重复实现谓词。
2. `_complete_performance` 加可选关键字 policy；main 显式传入；统计调用复用现有谓词。
3. 核对输出 JSON/HTML/MD 性能区使用同一完成后的 performance；不修改推荐集合/顺序。
4. 完成专项测试与两个项目质量门禁后，保留独立可审阅 diff，再进入批次2。

### 批次2：P1 预算、终态与续跑

文件：`scripts/bridge/today_background.py`、`scripts/bridge/run_today.py`、`tests/test_run_today.py`；失败任务持久化复用 `analysis/evolution_job.py` 的既有能力。

1. 先补核心超时、锁、旧 manifest、状态读取和 close 改变后的监控重跑回归。
2. 加预算解析、v2 manifest；分离锁和阶段 deadline；read_status 同步使用解析结果。
3. 引入不会被普通 Exception 吞掉的截止异常，并调整核心/研究边界；记录 close 失败尝试，超时后继续 monitor。
4. 更新阶段终态聚合和检查点依赖指纹；把重试行为与既有研究失败续跑行为一起验证。
5. 先通过专项和两个项目门禁，再进入批次3。

### 批次3：后台 weekly 去重与文档

文件：`scripts/analysis/evolution_job.py`、`scripts/bridge/run_today.py`、`scripts/bridge/today_background.py`、`tests/test_run_today.py`、`docs/today-recommendation-evolution-usage.md`、`references/today-and-candidates.md`。

1. 提取共享按周复用判断，后台在锁内调用；覆盖两种入口成功复用、空跑、失败、损坏记录场景。
2. 为 weekly 的有依据 skipped 添加阶段专属完成判断，不扩大其他阶段成功状态集合。
3. 更新预算字段、默认960秒预算和、环境变量语义、旧任务续跑、锁不可用及partial含义；记录有效资格数与最终推荐条数的区别。
4. 运行最终验证，检查 source diff 和文档内部链接后交付。

实施前按 AGENTS.md 说明实际将改动的 Python 区域。无需并行编辑共享 bridge 文件；可在最后交给独立 reviewer 检查预算与恢复语义。

## 验收矩阵

| ID | 输入/故障 | 必须观察到的结果 |
| --- | --- | --- |
| A1 | 同样合格候选，资金证明要求打开/关闭，证据未验证 | final_valid_count 分别0/1；证明有效时为1 |
| A2 | 已确认等待回踩候选，waiting_trigger/actionable 两种策略 | 有效数分别1/0，main 实际传入相同冻结 policy |
| A3 | 6只资格合格候选，actionable 名额5 | 有效数6、最终推荐5；弱市/缺省策略/空候选各有明确回归 |
| A4 | 低质量分、资金错误、数据过期、单日板块、失效买点 | 沿用现有拒绝行为；统计保持int且非负 |
| A5 | main mock策略敏感样例，冻结输出 | JSON与HTML/MD性能指标一致，候选代码/排序和正式分桶不被统计修复改变 |
| B1 | fake clock 消耗close全阶段预算 | weekly按依赖跳过；monitor获得完整自身预算；close失败包、checkpoint、任务partial存在 |
| B2 | weekly截止或普通异常 | monitor仍调用且获得完整预算，close只执行一次；resume只重跑未完成/依赖失效阶段 |
| B3 | callback内部 `except Exception` | 专用截止仍能逃出callback并被阶段边界捕获，不能被run_close转换成普通完成路径 |
| B4 | 锁耗时接近30秒后成功；锁完全超时 | 前者不压缩各阶段预算；后者明确monitor不可执行原因，不写监控成功，可续跑 |
| B5 | monitor自身超时 | 任务partial，monitor checkpoint=timed_out；resume保留close/weekly成功结果并重试monitor |
| B6 | close失败后monitor已有结果；resume close成功；先前成功content与重试相同 | close新摘要使旧monitor失效，新成功包的attempt身份和completed_at晚于失败；真实monitor读取新成功，不复用旧结论 |
| B7 | v1/无schema manifest，v2必需预算缺失/非法/未知schema | 旧任务按内存兼容预算运行且manifest/task_id不变；新建任务launch_failed、已有任务failed，reason分别invalid_budget_config/unsupported_schema；read_status不能返回missing或永久running |
| B8 | PID不可见，虚拟时间330秒与960秒附近；旧result存在 | v2在总预算宽限内保持running，超过预算且PID不存活才interrupted；旧result不掩盖续跑 |
| B9 | close超时实际落盘后调用真实monitor（mock数据加载/安全恢复） | 本次close作为failed计入失败率；无contract恢复；不把更早成功记录误当本次成功 |
| B10 | close成功包/合成失败包持久化报错；磁盘保留更早成功包 | 任务failed/current_close_persistence_failed；monitor skipped/current_close_unavailable且真实monitor调用次数为0；不覆盖历史正式推荐 |
| C1 | 同周两个不同task；weekly成功且非空、摘要正确 | 第二个不重复weekly；monitor仍运行；有依据weekly skipped可使任务正常完成 |
| C2 | 周任务失败/空跑/损坏/跨周/日期晚于as_of | 不复用，允许重新weekly；不把close_not_complete跳过当成功 |
| C3 | 既有研究阶段首次超时再resume | 仅失败研究重跑、无关成功核心结果可复用；evaluation保持close依赖 |

时间推进用fake clock/注入异常，状态与任务记录放临时目录；补一个毫秒级实际setitimer测试验证异常不会被except Exception吞掉。不等待数分钟，不联网，不访问真实发布指针。

## 验证命令与停止条件

仓库根目录运行，使用本机说明要求的包装器选择Python>=3.10：

```bash
bash tools/python.sh .claude/skills/stock-trend/tests/test_daily_candidates.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_daily_recommendation_performance.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_run_today.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_evolution_job.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_stock_trend.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

主门禁含实时来源：执行时遵守 `references/local-runtime.md` 的沙盒外执行与仅进程级NO_PROXY/no_proxy规则；沙盒或网络阻塞须如实报告，不能以专项通过代替主门禁。Golden仅比较，不为消除失败重建快照。若出现预期统计数字变化，核对样例policy后说明具体原因；只有确认全部差异预期且真正需要更新基线时才另行处理。

项目没有已配置的ruff/mypy门禁，不安装新工具。专项测试、主门禁、Golden和diff检查之外，人工审查所有 `_complete_performance` 调用、异常捕获边界、manifest解析与状态读取是否使用同一预算、阶段完成判定是否限定到weekly。

完成条件：验收矩阵覆盖并通过；两个项目门禁通过或清楚记录不可验证原因；新旧任务兼容与失败续跑证据齐全；变更文件/统计差异/后台等待增加写入交付说明。任一可恢复失败继续修复，不自动发布策略。

## 风险与控制

- 后台最长预算增加：保持先交付报告，显式说明默认预算和960秒；保留锁以保护安全恢复副作用，锁等待单独有界。
- 截止异常控制流变化：只引入私有截止异常，不捕获其他BaseException；研究与核心一起验证，避免破坏取消。
- 失败任务覆盖最新成功观感：以有时间和尝试身份的失败记录表达实际本次终态，resume成功后再产生新记录；不得伪造成功或覆盖旧证据。
- 续跑误用旧monitor：close依赖摘要变化必须失效；旧格式依赖不明的检查点保守重跑。
- 周度成功复用造成永久partial：采用stage-aware完成判断；只允许有验证来源的同周成功复用跳过。
- 验证触发真实策略恢复：专项/集成测试使用临时存储、mock安全恢复接口；本任务不运行真实monitor或publish。
- 回退：三个批次独立可撤回；v1/v2 manifest解析保留，已有历史证据不迁移、不删除。代码回退不能忽略v2任务的预算含义，须停止未完成v2任务或保留兼容解析后再回退执行逻辑。

本计划是工程修复计划，不验证投资胜率。分析仅供学习参考，不构成任何投资建议。

## 独立审查修订

审查发现P1原稿缺少失败包构造接口、close持久化失败后的确定控制流、非法预算终态与具体依赖摘要，已补齐阶段签名、attempt身份、先保存任务记录再checkpoint的顺序，以及禁止监控读取更早close的失败分支。补充全部核心执行的attempt身份，避免重试成功content相同而被旧job_id去重；补充未知schema/非法预算的明确终态及验收用例；checkpoint依赖固定为版本2及已持久化close的job_id。P0范围保持不变。


## 实施结果（2026-09-30）

- P0已传递冻结policy，最终资格数保留名额截取前语义；未改变选股门槛或排序。
- P1已实现v2独立预算、旧manifest内存兼容、私有截止信号、核心attempt身份与失败包落盘、close保存失败屏障、close依赖指纹及续跑。
- 同步与后台共用周研究成功记录验证；文档已同步默认960秒预算和环境变量语义。
- 专项：候选200/200、推荐性能22/22、统一入口及后台52/52、演进任务26/26通过。
- 最终主门禁：702通过、0失败、0跳过；Golden：21通过、0失败、2警告，未刷新快照。
- Python编译与git diff --check通过。独立P1审查通过；不支持定时器/执行锁的平台均保留明确失败原因。
- 未启动真实后台监控、未发布策略；本节记录提交前的实施与验证结果。
