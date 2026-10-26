# 昨日强势股今日反馈修复方案

状态：规划完成；Architect → Critic已依次APPROVE。范围为规划，不实施。评审与交接记录见strong-stock-feedback-handoff.json。

## 目标与边界

解决两条独立路径：当日个股行情缺失且原因不可追踪；指数数据滞后将反馈误当历史任务阻断。成功不以强制100%覆盖定义，而以全名单保留、每个缺口可解释、可用来源可恢复且无时间污染定义。

不修改正式市场评分和推荐资格，不用今日赢家重建昨日名单，不把盘中缓存当收盘，不重写已有报告/artifact/首次冻结名单。

## 原则、驱动与方案比较

原则：来源日期与请求日期分开；仅合格原生日涨跌幅进入原统计；同一运行冻结输入可重放；失败有可审计诊断；复用现有工具且总延迟有界。

主要驱动：数据口径正确、故障可恢复、30秒预算下的覆盖公平性。

| 方案 | 优点 | 代价/结论 |
|---|---|---|
| A 仅增加东财重试与诊断，日期不解耦 | 改动最小、先恢复可观测性 | 指数滞后仍阻断当日反馈；作为第一阶段而非完整交付 |
| B 诊断＋有界多源＋反馈日期独立核验 | 覆盖两个故障路径，保持评分资格 | 需扩展绑定/展示/重放契约；推荐 |
| C 放宽日期或拿旧缓存/复权收益补齐 | 表面覆盖高 | 混入旧价、盘中价或不同收益口径；排除 |

## 实施顺序

### 1. 先补诊断和针对性回归

文件：fetchers/market_feedback.py、analysis/market_feedback_job.py、reporting/market_feedback.py及对应三组测试。

- 每只股票保存provider_attempts：提供方/节点、尝试起止、请求日、最新返回交易日、原因分类、成功/失败、脱敏且限长的错误；禁止记录token/完整带参数URL。
- 原因区分 provider_error、timeout、missing_target_session、invalid_quote、unsupported_market、budget_exhausted/not_started；有未知也明确unknown。
- artifact保存规范化errors及逐股证据；state保留简洁计数与行级原因。区分最终缺失与已通过备用源恢复的告警，全部恢复可为complete并附advisory。
- HTML/MD/JSON共用冻结诊断，0/33用“全部样本行情缺失”，逐股可展开看失败类别。旧artifact缺字段显示“旧记录未保存详细原因”，不猜测、不联网。
- 先增加模拟33只全失败、错日期、缺字段等回归用例，再修改生产代码。现有测试已覆盖部分时间和名单规则，复用而非复制。

### 2. 在原预算内增强来源恢复

文件：fetchers/market_feedback.py，必要时复用core/eastmoney_utils.py；新适配逻辑尽量局限本模块，不调用CLI main或改公共K线语义。

- 先复用现有东财节点轮换，限制实际节点次数，保留attempt证据。使用相同deadline，内部HTTP/代理/curl重试也计入，不能每层重置预算。
- 采用两阶段公平调度：主源先覆盖所有昨日代码；预留约10秒给仍缺失代码的备用源，29秒内部预算/30秒外层硬上限，最多4路请求。时间用monotonic，调用前按剩余额度缩短超时；预算不足不再排队。
- 30秒定义为主调用响应上限，并非所有不可中断系统调用均已停止。外层/池/主源/备用源共享同一绝对deadline和取消/启动闸门；外层返回时关闭闸门，任何嵌套回退不得继续启动请求。已在途调用可耗尽其受限timeout，但晚到结果不能修改返回对象或artifact。测试允许最多0.2秒调度误差，不能以此重置预算。
- 当日涨停池独立预算上限建议5秒，失败不阻断昨日名单的反馈；它仍只决定今日名单能否冻结。具体主源/备用分配由有界fixture验证后固定为常量。
- 备用源首选腾讯原生当日行情：实施前先核验供应方原始响应/官方资料中的证券身份、日期/事件时刻、原生pct、价格尺度、量单位与明确停牌标识；保存最小脱敏响应fixture。不得直接复用fetch_tencent_a_stock推算的qfq pct_chg。
- 只有原生pct、有效价格/量、证券身份和目标交易日一致才合格；收盘还需请求模式及源事件时刻/最终日线证据确认。缺源时刻不能仅凭本机15:10后取到响应升级旧盘中行情。
- 未经验证的市场（含BJ）保持逐股unsupported/missing，不推断10%涨停，不伪造停牌。若腾讯原生字段无法验证，关闭该adapter，保留东财恢复与明确缺口，不能把派生收益写入原统计。
- 同一代码首次合格结果胜出，失败源不能覆盖成功源；晚于deadline返回的数据不进入冻结artifact。失败覆盖完整名单，成功覆盖不作无证据承诺。
- BaoStock暂不纳入：会话/串行约束及硬超时需另行评估，避免首轮增加第三条复杂链路。

### 3. 解耦市场证据日与反馈依据日

文件：analysis/market_regime.py、analysis/market_feedback_job.py、analysis/market_feedback.py、reporting/market_feedback.py；审计所有market_feedback_binding消费者及测试。

- 原ctx.data_date继续表示评分证据日，原评分/推荐的stale/partial限制保持。
- 明确实现resolve_feedback_session(calendar, report_as_of, runtime_start, refresh_mode)：返回feedback_basis_date、market_basis_date由调用方另附、mode与资格/拒绝原因。仅live_refresh模式且两个时刻属于相同上海自然日、日历覆盖该日且确为交易日才允许昨日名单反馈报价采集。report_as_of决定目标日与盘中/收盘；runtime_start验证实际采集日；evidence_as_of只记录完成时间。15:09启动而15:10后结束仍为盘中。no_refresh不调用网络；historical模式保持只读并拒绝实时补采。当日涨停池获取/冻结独立于该解析器：live_refresh且两个时刻属于同一上海自然日，池自身满足当日日期、完整覆盖、收盘口径、采集时刻时，即使日历失效仍可抓取并冻结，但不能据此选择历史相邻名单。明确原因包括calendar_invalid、calendar_current_session_missing、non_trading_session、historical_live_collection_forbidden及market_context_stale（仅评分提示、不自动否决已独立合格反馈）。
- 新增显式feedback_basis_date与market_basis_date。只有正常实时刷新、实际上海当天为权威日历确认交易日、日历覆盖完整时，反馈可独立使用当天；15:10前仍为intraday，之后需独立验证收盘证据。不得用请求日顶替供应方日期。
- 报告市场依据日落后而反馈日为当天时，在区块顶部清楚展示两个日期，标“市场评分证据滞后；反馈单独采集”。相邻名单与比较都按feedback_basis_date确定，禁止跳过缺日。
- 日历缺失/当日未覆盖/假期/显式历史运行：不根据系统日期强行跑反馈；返回可解释原因。保持原允许在日历故障下独立冻结供应方合格当日涨停名单的行为。
- 扩展新绑定版本为显式携带market_basis_date、feedback_basis_date、report/evidence截止和摘要；读取旧绑定时维持旧日期相等规则。新_load_bound逐项核验日期、路径所属目录、schema、摘要和截止，不简单删除原日期检查。
- 固定版本迁移：新绑定market-feedback-binding/v2、新artifact market-feedback-artifact/v2、反馈state market-feedback/v2、规则strong-stock-next-session-feedback/v2；新close记录market-feedback-close/v2。v2 artifact目录按feedback_basis_date；收盘路径使用规则版本子目录，避免同日旧v1首次不可覆盖记录挡住v2（旧v1原路径只读）。读取按版本分派：v1保持旧data_date相等、原schema/目录/哈希；v2分别校验两个日期、完整时钟与规则版本。未知版本拒绝；v1/v2收盘反馈不可比较，不自动迁移或重写旧artifact。纯诊断第一阶段可向v1兼容追加字段，启用多源或日期解耦前必须整体切换v2契约。
- report JSON/state/绑定读取/原子持久化使用同一结果；--no-refresh只读精确绑定，不选其他运行最新artifact，也不触发网络。新版本字段与旧版解析策略需覆盖测试和文档。
- 在历史_live guard前先识别调用模式；只有明确当日刷新能独立采集，显式历史截止即使运行在当天也不能借新路径绕过校验。

### 4. 完成展示、回归与当前日验证

- 在references/daily-review.md记录日期独立语义、来源口径、错误类别、预算和兼容行为。
- 先运行test-spec的离线专项，再运行必需质量门禁。Golden失败先核对意图，不自动更新快照。
- 当前实际交易日可做一次沙盒外、进程级NO_PROXY的定向反馈取数，保存新的不可变artifact/新报告；验证覆盖、来源、采集时间及失败原因。历史10/23报告和artifact保持原始状态；若实施跨日，只做冻结fixture回放，不称为恢复当时实时数据。
- 可用原生备用源尚未核验或外部源故障时，明确验收缺口；不把fixture通过等同实盘覆盖恢复。

## 验收与风险

详细矩阵见test-spec-strong-stock-feedback-repair.md。核心：33入33出；0/33有逐股原因且统计null；备用成功可恢复且不混收益口径；指数旧/个股新可并列显示各自日期且评分仍受限；历史/离线无网；并发≤4、总预算≤30秒＋测试允许调度误差。

风险：备用源原生字段未验证→能力闸门拒绝启用；日期解耦导致历史污染→新绑定版本/精确截止与历史无网测试；嵌套重试超预算→统一deadline与晚到拒收；不完整覆盖产生幸存者偏差→完整列表与有效样本分母明确，跨日比较仍沿用原合格标准。

## ADR

决定B分阶段落地，A先形成诊断基础。选择驱动是正确性、独立故障隔离和时延；排除C因为会更换统计含义。代价是绑定与日期展示升级以及备用原生接口核验；后续只在确有需要时评估BaoStock、额外来源或异步补采。原33条底层错误已经丢失，不能追溯恢复。

## 执行交接建议

可用角色包括explore、executor、test-engineer、architect、critic、verifier；本任务建议单一executor顺序修改共享日期/绑定代码，test-engineer与verifier分别负责回归设计和验收审查。使用各角色默认模型/推理配置，不强制覆盖。

普通实施可将本计划作为$ultragoal后续输入；如选择团队，主executor负责抓取与日期绑定，一名test-engineer负责测试文件，最后verifier验证artifact/HTML/MD/JSON一致性。示例：`$team 按 .omx/plans/prd-strong-stock-feedback-repair.md 实施并完成 test-spec 验证`。tmux OMX CLI可使用`omx team 2:executor "按上述方案实施，明确分配抓取/测试所有权并由leader整合日期绑定"`；Codex App优先原生有界代理，不假设tmux可用。关闭团队前需归档专项/双质量门禁结果、一次当日取数证据或外部缺口、工作区diff与未改写旧artifact证明；Ultragoal据此记录完成检查点。$ralph仅作为用户明确选择的持续单人验证回退；本轮不启动执行或目标。

## 审查修订记录

架构首轮要求的三个缺口已补齐：双时钟及显式refresh_mode解析函数；binding/artifact/state/rule/close v2的兼容、路径和比较隔离；统一响应deadline与后台启动闸门。A→B阶段交付保留。

架构后续澄清：日历只控制昨日名单反馈，独立保留当日池冻结；最终APPROVE。Critic最终APPROVE；实施时核验主质量入口纳入反馈专项测试。
