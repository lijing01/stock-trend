# 日线弱转强：扫描识别与今日推荐独立表格实施计划

日期：2026-10-27。状态：独立审查通过，首期已实施。原计划及其假设保留如下，实施验证记录见文末。

## 1. 目标与范围

在现有股票扫描取到日线时识别“上涨后调整 → 缩量守住结构 → 放量收盘突破”，并在今日推荐 HTML、Markdown 与 JSON 中提供同一份独立结果。既能显示缩量待突破，也能显示本依据日首次确认的弱转强。

输入依据：`/Users/jing.li7/Downloads/daily_weak_to_strong_plan_v2.md`，SHA256：`64897c66b20c091e54f42bc358f413126f4a1d96d2fb86476ea3a05a9c1a0165`。以下显式列出的边界解释属于实施决定，不能伪称原文已完整定义。

首期交付：纯日线判定、扫描接入、独立信号留痕、每日推荐独立表格、离线回归。范围为当前扫描实际触达的普通 A 股股票池，并非全市场；池选择、板块窗口和每板块股票数量沿用当前入口配置。股票类型无法核实时标记不支持/证据不足，不推断交易资格。

原文 T+1 订单、仓位、成交和持仓退出留作后续独立交易研究。首期不具备账户或成交事实，因此不得输出 `HOLDING`、已买入、实盘仓位、保证成交或策略收益结论。此次“可执行 plan”指代码实施计划。

## 2. 已核对的现有实现

| 事实 | 代码依据（实施前按符号重新定位行号） | 对本需求的影响 |
|---|---|---|
| 板块扫描以 Phase 1 成分股票为输入，调用 `run_phase2(enable_wyckoff=True)` | `.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2274`、`:2474` | 新信号的股票池应记录为 Phase 1 实际输入集合 |
| `run_phase2` 先抓取日线，再按维科夫门槛 `continue` 丢弃股票 | `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:2533`、`:2705`、`:2724` | 弱转强必须在该过滤之前独立计算；不能只扫描正式 Top N 或 `scored` |
| 当前公共取数链要求 60 根，并将不足根数的返回日线清空 | `stock_scanner.py:1215`、`:1373`；`fetchers/kline_eastmoney.py:578` | 纯判定器可支持 20 根，但首期在线覆盖仍受 60 根公共取数契约约束；短历史明确报告不足，不承诺可检出 |
| `kline_artifacts` 只在维科夫过滤之后写入，随后还删除被筛掉股票的 artifact | `stock_scanner.py:2726`；`daily_candidates.py:2520` | 独立信号证据不能依赖该 artifact 存活 |
| 全局报告补抓可能刷新 K 线，随后清空 artifact | `daily_candidates.py:5405`、`:5413` | 后续较新的同日 K 线判定必须覆盖初次判定，包括由命中变为未命中 |
| 日线可用性和收盘证据已有统一实现 | `stock_scanner.py:1110` 附近的 `_kline_close_confirmation`、`:1198`、`:2533` | 复用来源、日期和收盘契约，另补本信号所需窗口完整性校验 |
| 日期覆盖函数允许最新日期晚于依据日 | `.claude/skills/stock-trend/scripts/core/kline_utils.py:48` | 本模块必须先截断到 `as_of_date`，再要求最后一根恰为依据日 |
| 东方财富及腾讯/BaoStock 日线链路采用前复权 OHLC | `.claude/skills/stock-trend/scripts/fetchers/kline_eastmoney.py:103`、`:253`、`:634` | 使用同一 payload 的一致价格尺度与成交量口径，保留来源；历史回放不能用今天复权后的数据冒充当时输入 |
| 报告已按正式可执行、等待触发、次日确认、观察池等分节 | `daily_candidates.py:4566`、`:4922`、`:5188` | 新增专用表格，而非挤入既有候选表 |
| JSON 有独立影子模块扩展模式 | `daily_candidates.py:5203` | 新增顶层 `weak_to_strong`，保留既有顶层字段语义 |
| 统一今日入口透传候选 JSON | `.claude/skills/stock-trend/scripts/bridge/run_today.py:261` | 不新增第二次扫描；验证新字段能透传即可 |
| 正式与研究快照具有内容哈希、关联和选择范围契约 | `.claude/skills/stock-trend/scripts/core/recommendation_snapshot.py:255`；`core/candidate_research_snapshot.py:199` | 新信号使用独立旁路快照，避免改变正式政策身份和研究选样范围 |
| 当前报告契约禁止展示交易计划价格及仓位 | `.claude/skills/stock-trend/references/today-and-candidates.md` 的 `/candidates` 第 7 条 | 本期只展示信号结构与证据，不把源文档中的订单区间塞入候选报告 |

## 3. 首期判定合同 `daily-weak-to-strong/v1`

### 3.1 数据资格与计算方式

- 纯函数建议接口：`analyze_daily_weak_to_strong(kline_payload, *, as_of_date, close_evidence, calendar_evidence, config)`，仅输入计算，无网络、无文件写入、无账户数据。交易日历由每日入口使用的权威本地日历解析后传入；纯函数不自行加载或联网。
- 按交易日期升序处理；先剔除依据日之后的 K 线，再判定。日期非法、重复日期内容冲突、非有限数、OHLC 关系非法、窗口内成交量不为正均返回明确的 `data_insufficient`。相同重复记录可去重但须记录诊断。
- 最后一根有效日线必须对应 `as_of_date`；不以“取数时间晚于收盘”单独证明日线已经完成。使用现有收盘证据对象；模块为正式确认要求 `confirmed=true`，不能把非正式入口的 `not_required` 当作已确认。
- MA10 使用当日及之前 10 根同尺度收盘价简单平均。至少 20 根历史 K 线；此外 D0 上涨段起点、其前置比较日和 MA10 预热必须可知，左侧截断时仍返回不足，不能把最早可见日强行当成上涨段起点。
- 当真实市场日历在所需窗口内显示缺交易日，且无法区分停牌与缺失时，标记 `calendar_gap_or_suspension`，本轮不确认；不能用自然日计数替代交易日。日历缺失也须明确资格不足。
- OHLC 使用同一来源/明确一致的复权尺度，量比使用同口径成交量。未知价格复权列为 `basis_unknown`；跨来源拼接存在不可解释跳变则不足，不猜测校正。当前 payload 未提供 `volume_unit`：同一 provider、同一 payload、同字段的成交量可以计算比值，股/手绝对单位未知仅记为审计信息，不阻断信号；跨源拼接、字段语义变化或明确单位冲突才返回 `volume_basis_conflict`。
- 纯判定器不依赖维科夫的最小 K 线长度、不使用资金/基本面数据，也不额外抓取日线。当前扫描公共取数链至少要求 60 根，不足会清空日线；首期保留该取数/缓存合同，20–59 根样本仅在独立离线判定器可评估，在在线扫描中记录 `shared_fetch_history_insufficient`。如需支持短上市历史股票，后续单独设计原始取数与多消费者资格分离，不能通过本次改动降低正式候选的 60 根门槛。新模块的数据完整性与“现有六维覆盖率”分别显示。

### 3.2 需要明确的原文歧义及本计划默认值

| 项目 | 本计划采用的可测试定义 | 原因及影响 |
|---|---|---|
| 连续上涨段 | 至少连续 2 个 `Close_i > Close_(i-1)` 的交易日；段内仅包含这几个上涨日，V0 为其日成交量算术均值；D0 是最后一日 | 原文没有最少长度和均量起点；以配置 `min_up_days=2` 明确初值，未经回测不声称最优 |
| 调整开始 | 上涨段终止后的首日 `Close_D1 <= Close_D0`，相等也结束连续上涨 | 把“未创新高”明确为收盘价，避免最高价/收盘价混用 |
| 调整窗口 | 优先原文明确的“D5 是最后突破机会”，配置 `max_days_after_d0=5`；突破可发生 D2–D5，突破前完整调整根数 n 为 1–4；D5 仍未突破即本轮超时 | 原文同时写 n≤5 与 D5 截止，二者冲突；本期选择较短明确期限，不暗中把 D6 当有效。若希望完整 5 日调整后允许 D6，需新合同版本 |
| 突破前结构 | 每个候选 T 只用 D1…T−1 重算 H/L/Va、末日缩量条件；不要求昨日日报曾写入 S2 | 支持从冻结历史无状态重放，且排除 T 日参与压力线与均量 |
| 不合格突破 | T 日收盘已 `>H`，但放量、收盘强度、幅度上限或一字线过滤未通过时，本轮为 `INVALID/breakout_unqualified` | 原文只说不产生信号，未规定如何延续；本期不将失败突破日重新塞进调整区间以制造后续信号 |
| 锚点重选 | 当前周期的 D0 冻结至确认或失效；不能每天滑动 D0 逃避超时/超回撤。终止后从下一根开始重新积累新的上涨段 | 保证逐日前推与整段回放一致，旧结构不复活 |
| 已确认信号 | 仅当 `signal_date == as_of_date` 才进入“当日收盘确认”；历史 S3 不跨日显示成新信号 | 防止过期信号反复推荐；不推断次日是否成交 |

这些是规划默认值，需在实现的常量、测试、报告参数摘要和工作流文档中一致落地；首期不开大量调参 CLI。

### 3.3 状态与阈值

正式日线状态为 `WAIT → PULLBACK → SUPPLY_DRY → BREAKOUT_CONFIRMED`，另有 `INVALID`；盘中形态命中使用独立 `PROVISIONAL_BREAKOUT`。数据资格为独立字段，不能用 `WAIT` 掩盖取数失败。

| 判断 | 精确定义 |
|---|---|
| 结构回撤 | `1 - L/High_D0 <= 0.12`，只检查突破前调整区间 |
| MA10 保护 | 每个调整日 `Close >= 0.98 * MA10` |
| 平均缩量 | `Va/V0 <= 0.8` |
| 末日缩量 | n≥2 时 `Vlast/Va <= 0.9`；n=1 明确豁免 |
| 价格突破 | `Ct > H`，等于 H 不通过 |
| 突破放量 | `Vt/Va >= 1.2` |
| 收盘强度 | `High_t > Low_t` 且 `(Ct-Low_t)/(High_t-Low_t) >= 0.75` |
| 幅度上限 | `Ct/H <= 1.05` |
| 一字 K 线 | `High_t == Low_t` 明确排除常规确认；不计算零分母 |
| 正式确认 | 上述全部通过、依据日一致、日线已收盘确认、数据资格 ready |

计算使用未展示舍入的数值比较阈值；边界用一致的十进制定值或已规定容差，展示舍入不能改变判定。未满足缩量且尚未突破/失效时保留 `PULLBACK`；结构破坏及期限耗尽为 `INVALID`。

盘中先重放此前已完成日线，再对当前未收盘 K 线做临时投影：若条件暂时满足，状态为 `PROVISIONAL_BREAKOUT`、`confirmation=provisional`，展示“盘中候选，未收盘确认”；否则保留前一完成日状态并附 `provisional_match=false`。盘中投影不触发周期终止、超时或重新锚定，也不能使用 `BREAKOUT_CONFIRMED`。未收盘但没有可靠盘中身份的旧缓存只记证据不足。临时结果不得进入正式确认数组或落入正式日终快照。休市日使用统一入口解析的最近完成依据日，不创造新的信号日期。

逐根重放采用以下确定性顺序；统计均只使用已确认日线：

```text
先用足够历史建立 MA10；输入左边界若正处上涨段，标记该段左截断，
直到观察到一个 Close_i <= Close_(i-1) 的边界才允许开始完整新段。
无活动周期：累计边界之后连续上涨日；涨日不含其前一比较日。
首次出现非上涨日时：此前上涨日数>=2则冻结末日为D0，本日为D1；
否则清空上涨计数，本日只作为后续比较基准。
有活动周期：用D1..T-1检验缩量结构，再检验T日突破；
  若合格突破 -> 本日输出S3，周期终止；
  若不合格价格突破 -> 本日输出INVALID，周期终止；
  否则将T纳入调整，检查结构破坏/D5超时，再输出S1/S2或INVALID。
终止当根只作为下一根的收盘比较基准，不能算作新上涨段中的上涨日；
下一根若上涨，可计为新段的第1日。
多个历史周期按上述顺序依次处理，只输出as-of当日状态；
旧周期S3保留在诊断事件中，但不会当成今日新信号。
```

## 4. 输出、扫描范围与留痕

### 4.1 单票与整次结果

每只股票记录：

```text
schema_version, detector_version, parameter_hash
code, ts_code, name, sector_memberships, as_of_date
state, confirmation, evidence_status, reason_codes
cycle_id, d0_date, up_start_date, up_days, adjustment_days, signal_date
H, L, d0_high, V0, Va, Vlast, Ct, Vt
drawdown_ratio, va_v0_ratio, last_va_ratio
breakout_volume_ratio, close_strength, breakout_ratio
checks: {各判定值、门槛、通过/失败/不适用}
kline_evidence: {provider, basis, volume_unit, data_date, fetched_at,
                 close_confirmation, calendar_coverage, input_sha256}
context: {market_status, sector_persistence_status,
          formal_bucket_or_not_selected, formal_blockers}
```

`cycle_id` 由证券、D0、合同版本和参数身份确定；同一周期跨日可关联，`signal_id` 另加入突破日与输入身份。历史输入一旦改变，不覆盖旧证据。

顶层 `weak_to_strong` 包含：`schema_version`、`as_of_date`、参数、`scope`、`summary`、`confirmed`、`watching`、`provisional`、`diagnostics`、`tracking`。所有格式消费同一冻结对象。

`scope` 至少记录选定板块范围、实际 Phase 1 去重股票清单及哈希、attempted/evaluated/data_insufficient/confirmed/watching 数量、未完成范围、扫描模式、`scope_complete`。当前代码的 `fixed_top_120` 是范围策略名，不能直接解释为最终只有 120 只股票；数量以实际集合为准。

### 4.2 扫描传递方式

在 `run_phase2` 增加可选的独立输出容器，例如 `weak_to_strong_results=None`，保持现有列表返回值与 `scan_sectors` 二元组兼容。容器由 `main` 创建、逐层传入；仅今日推荐路径启用，通用扫描调用方未传则行为不变。

日历传递路径为 `daily_candidates.main → scan_sectors / enrich_global_report_scope → run_phase2 → detector`。由现有 `_load_authoritative_trading_dates`（`daily_candidates.py:5301` 已导入）生成只读 `calendar_evidence`，包含 provider/身份哈希、交易日期集合、coverage_start/end 和 as-of；首期不额外联网。缺失返回 `calendar_missing`，未覆盖输入所需区间返回 `calendar_coverage_incomplete`，均不得正式确认。只校验参与当前周期识别与MA预热的必要区间，不要求日历覆盖无关远古日线。

1. K 线获取及日期截断完成后，对每个 Phase 1 实际输入股票调用纯判定器，位置在 `len(records)<20`、维科夫最小历史数及 `wyckoff_gate_pass` 的所有 `continue` 之前。
2. 即使不足或未命中，也登记该股票的评估状态，以便区分零信号与未扫描。
3. 输出容器按 `(ts_code, as_of_date)` 保存最新结果；跨板块合并 memberships，同股票同一日线输入哈希只计算一次。
4. `enrich_global_report_scope` 的第二次 `run_phase2` 也传入同容器；输入被刷新时重新判定，旧命中可被 `WAIT/INVALID/data_insufficient` 覆盖。不得仅合并正向信号。该补抓只覆盖原维科夫 `scored` 报告范围；被过滤的弱转强股票保持初次扫描证据，不承诺享有补抓，逐票记录 `refresh_stage=initial/global` 与最后输入身份。
5. 当前 `kline_artifacts` 清理逻辑保持其职责；新容器保存所需的独立计算证据与输入窗口，不保留所有无关原始数据。
6. 若兼容旧桩函数而未支持新参数，显式标记 `unavailable/collector_not_supported`，不能把空列表报告成成功扫描无信号。参考当前 `TypeError` 兼容分支，不吞掉模块内部真实异常。

### 4.3 独立冻结文件

新增 `.cache/stock-trend/weak_to_strong/daily/<依据日>/<内容哈希>.json`，包含截至当时的完整可用输入日线 payload（通常约 250 根）、来源元信息、交易日历证据、参数、代码版本、原始结果、扫描范围和同次市场上下文身份。先明确左侧预热/初始化规则再处理，不能仅保存 H/L/Va 或删去状态初始化所需历史。保留所有实际评估股票的状态；展示列表之外的失败也可审计。

复用 `canonical_json`、内容哈希及原子写入已有工具，避免新依赖。同一身份重跑幂等，不同输入另存；盘中结果存独立 `provisional/` 子目录。保存失败用 `tracking.status=write_failed` 明示，仍生成报告，不宣称已可复现。

独立旁路快照承担弱转强的复现；不把缺少维科夫资格的新股票强加到既有正式推荐/因子消融研究人群，不改变旧快照的字段投影或发布指针。报告可记录同次正式快照哈希用于上下文关联，但新信号不以正式快照是否写成功为前提。

## 5. 每日推荐展示合同

新增一个独立表格，位置在现有“今日可执行/等待触发/次日确认”之后、“观察池”之前。标题为 **日线弱转强**，说明为“独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制”。空结果保留标题及明确空态。

| 代码/名称 | 板块 | 信号状态 | 调整天数 | 调整压力 H / 支撑 L | 调整量比 Va/V0 | 突破量比 Vt/Va | 收盘强度 | 依据日 / 数据资格 | 市场限制及正式推荐状态 |
|---|---|---|---|---|---|---|---|---|---|
| 示例占位，不是真实股票 | … | 当日收盘确认 / 缩量待突破 / 盘中未确认 | … | 结构参考值 | … | … | … | … | 仅观察 / 等待 / 原正式可执行 / 未进入正式候选；附原因 |

- 主表显示 `confirmed`、`watching(SUPPLY_DRY)` 和盘中临时命中；`PULLBACK/WAIT/INVALID/data_insufficient` 计入诊断摘要，明细在 HTML 折叠区、Markdown 诊断区及 JSON 中保留。
- 排序固定为当日正式收盘形态确认、缩量待突破、盘中临时；同组按代码升序，首版不创造未经验证的综合强度分。全部命中展示，不受正式 `--top` 截断。
- 结构 H/L 是调整区间压力/支撑，不标为入场价或止损委托；不展示 Pmin/Pmax、仓位、收益或账户风险额。
- 弱市或市场数据缺失仍可显示形态，但限制列明确“仅观察”，不能写“次日可执行”。未进入原维科夫人群的股票写明“未进入正式候选”，不得默认资格通过。
- 六维覆盖率缺失不自动抹去 OHLCV 形态；同时，形态确认不提升原推荐分桶、买点奖励或今日可执行数量。
- 同票可在新表与原表各出现一次；新表按代码去重、板块信息合并。HTML 文本必须 escape；零值和缺失值分别显示，禁止将 unknown 格式化成 0。
- HTML/Markdown/JSON 使用相同依据日、参数和结果；统一入口直接透传，不等待每日复盘观察列表后台任务。首期不把该表追加到每日复盘 HTML。

## 6. 分阶段实施任务

| 阶段 | 文件与交付 | 依赖及验收 |
|---|---|---|
| P0 固化规则 | 新建 `references/daily-weak-to-strong.md`；更新 `references/today-and-candidates.md` 的独立表格说明及 `.claude/specs/stock-trend-skill.md` 对应条目 | 完成本计划第 3 节全部歧义定义；建立合同版本和参数常量表 |
| P1 纯判定器 | 新建 `scripts/analysis/daily_weak_to_strong.py`、`tests/test_daily_weak_to_strong.py`，构造无真实证券依赖的日线 fixture | P0；状态、阈值、窗口、未来数据截断和日线资格测试通过 |
| P2 并行信号收集 | 修改 `scripts/scans/stock_scanner.py:2458`；`scripts/scans/daily_candidates.py:2274`、`:2639`、`:5380`；扩展 `test_stock_scanner.py`、`test_daily_candidates.py` | P1；维科夫淘汰票仍命中、补抓后命中可撤销、无额外网络调用、旧调用兼容 |
| P3 冻结与覆盖审计 | 新建 `scripts/core/weak_to_strong_snapshot.py`、`tests/test_weak_to_strong_snapshot.py`；在 `daily_candidates.main` 保存旁路快照 | P2；内容身份、日期、参数、范围、幂等和保存失败语义可验证 |
| P4 三格式表格 | 新建 `scripts/reporting/weak_to_strong.py`；扩展 `generate_report`、`_generate_html`、`build_json_output` 以及所有调用分支 | P2/P3；独立表格、空态、降级、HTML 转义、JSON 透传、同一结果三格式一致 |
| P5 集成验收 | 扩展统一入口现有测试（按 `run_today` 定位）；注册新测试到现有主门禁聚合清单；执行第 8 节验证 | P0–P4；主门禁和 Golden 差异可解释，冻结输入可离线复现；汇总修改文件、证据和剩余限制 |

可以在 P1 结果字段冻结后并行实施 P2 和 P4 的纯 renderer；`daily_candidates.py` 的实际编辑由同一负责人集成，避免共享文件冲突。无需新增依赖。默认配置在每日推荐流程启用；先用离线集成 fixture 验证，实时生成报告只在用户要求或实施验收范围已明确包括时执行。

## 7. 可测试的验收条件

1. 原文算例在明确提供有效 D0 上涨段和 MA10 预热后，输出 `BREAKOUT_CONFIRMED`，H=11.70、L=10.90、Va≈523.333、突破量比≈1.624、收盘强度≈0.8667；原文只给一根 D0，不能单凭原表证明 V0 的上涨段均量。
2. D2、D5 合法确认；D1 无调整历史不能确认；D5 未突破和 D6 试图使用旧周期均不确认。n=1 不检查末日缩量，n≥2 必须检查。
3. 12%、0.98、0.8、0.9、1.2、0.75、1.05 的等值及两侧各一例；Ct=H 不通过，Ct>H 才可通过。零振幅、零成交量、NaN、缺字段不产生正式信号。
4. T 日高点及成交量改变不能影响 T 日使用的 H/L/Va；向输入追加未来 K 线后，查询历史 as-of 的结果完全相同。逐日滚动与一次回放到同一 as-of 的周期/状态一致。
5. 存在一只维科夫不通过、弱转强通过且公共取数合格的股票时，独立表格有该股票；原正式候选/分桶完全保持基线结果。纯判定器以 20–59 根完整 fixture 可评估；在线取数链不足 60 根则明确显示覆盖不足，不能声称已评估且未命中。
6. 同票跨板块、多个扫描批次或补抓仅有一条最终记录；同 hash 不重复计算，新日线使信号失败时旧表格行消失或变为诊断。
7. K 线过期、盘中缓存、日期未知、复权未知、窗口内交易日缺失均不能进入正式收盘确认；盘中命中使用 `PROVISIONAL_BREAKOUT` 写在临时数组并显示警告。单源完整 payload 的成交量绝对单位未知不阻断比值，跨源单位冲突必须阻断。日历缺失与覆盖不足分别返回固定 reason code。
8. 市场评分 <60、市场证据不完整或板块仅单日脉冲时，新表可以保留形态观察，但不会新增正式可执行或等待触发数量；未增强资金/基本面的票不被伪造为六维质量通过。
9. 空股票池与完整扫描零命中显示“本次已评估 X 只，无满足条件形态”；deadline/取数错误/兼容桩未接入显示覆盖不足，绝不冒充完整扫描零命中。
10. 30 只正式输出外的一只命中股票仍在新表；独立表格命中条数、JSON 数组与冻结快照一致。HTML、Markdown 和 JSON 均含新模块且现有字段兼容。
11. 新模块开启/关闭时，同输入原正式 buckets、原评分排序、政策参数、正式快照决策内容和因子消融输入身份相同；允许报告性能审计多出模块耗时。
12. 同输入重复保存同路径；不同输入不覆盖；写盘失败仅降级 tracking；用冻结日线/参数重新运行可逐字段复现形态结果；旁路快照不更改既有发布指针。
13. 在固定 200 只 × 250 根合成日线 fixture 上，复用一次取数，本模块计算的 3 次中位数新增耗时目标 ≤2 秒；记录机器/解释器和结果。新增 HTTP 请求数必须为 0，现有取数预算不扩大。

## 8. 验证顺序与完成标准

本次仅写计划；以下为实施时的必做步骤。

1. 开始修改任何 `scripts/` 下 Python 前，说明受影响模块和意图；确认工作区状态，按 P0–P5 小步修改。
2. 使用 `bash tools/python.sh`，先跑新增纯判定器、快照、renderer 测试，再跑受影响的 `test_stock_scanner.py`、`test_daily_candidates.py` 及今日入口定向测试。依照现有文件的 unittest 入口运行，不猜测包布局。
3. 执行仓库必需门禁：

   ```bash
   bash tools/python.sh .claude/skills/stock-trend/tests/test_stock_trend.py
   bash tools/python.sh .claude/skills/stock-trend/tests/test_golden.py --diff
   git diff --check
   ```

   主门禁可能联网；东方财富/同花顺调用必须按 `references/local-runtime.md` 在沙盒外运行，仅为该进程追加 NO_PROXY/no_proxy。网络失败记录来源、失败门禁和替代离线证据，不声称全部通过。
4. Golden 不自动重建。若候选展示 fixture 新增独立区块属于预期，逐项说明变化；原数值或正式分桶变化属于回归，先修复再考虑快照更新。
5. 使用冻结/合成输入贯通扫描 → collector → 旁路快照 → JSON/Markdown/HTML → `run_today` 透传，检查表格存在、覆盖数与零信号/失败区分。不自动打开浏览器。
6. 验证完成后交付修改文件、各验收条件结果、质量门禁输出和明确的限制；没有已核验成交和成本模型时，不报告收益或胜率改善。

## 9. 风险、取舍与后续

- **选样遗漏**：本方案覆盖现有 Phase 1 股票，而非全市场和全部行业成分；保存实际清单，表格注明范围。扩大选股池是独立需求。
- **规则歧义带来统计差异**：D0 最小上涨长度、D5 截止、不合格突破终止均冻结版本；修改规则需版本升级和对照 fixture，不能原地替换历史解释。
- **未来数据或复权变化**：检测按 as-of 截断，历史依据只用当时冻结 payload；未知输入尺度明确不足，历史回放和当日实时证据分别标记。
- **旁路被误认正式推荐**：固定独立标题、观察标识和市场/资格限制；不使用“次日可执行候选”命名形态状态，不与原分桶混算。
- **扫描性能与补抓重入**：共享一次日线取数，用输入哈希去重，旁路结果最新覆盖，禁止仅追加命中；预算耗尽如实展示覆盖不足。
- **持仓/交易功能失真**：S4 依赖真实成交或明确研究撮合记录，首期不从 S3 推断。后续若接入原文 T+1 和退出规则，单建交易合同、历史证券规则、价格档位、开盘过滤、T+1、停牌涨跌停和费用模型，并明确收盘失效后的下一可交易日退出，而非套用现有 LPS 触价止损合同。

首期完成条件：第 7 节验收逐条有证据，第 8 节门禁通过或外部阻塞明确，今日推荐可稳定输出独立表格。规则阈值仅为待验证初值；回测和真实成交研究为后续任务，不作为本期软件接入结果的盈利证明。

## 10. 计划审查记录

独立 critic 首轮要求修订，已完成：公共 60 根取数门槛与纯函数 20 根下限分离；补齐日历输入与传递；单源量比不因绝对单位未知而失效；盘中状态与正式确认分离；加入确定性逐根重放；限定实际补抓范围。复核结论为 **APPROVED**。本次只新增本计划文档，未执行实现或业务测试。

## 11. 首期实施记录

已接入独立日线判定、扫描前置收集、JSON/Markdown/HTML 表格、内容寻址旁路快照和文档。2026-10-27 最终主门禁为 1019 通过、0 失败、1 跳过；跳过项为股票资金流向接口网络超时。Golden 对比为 21 通过、0 失败、2 条警告，未重建快照。独立判定与集成套件 18 项通过；200 只 × 250 根合成日线判定的三次中位数为 0.358 秒（Python 3.10，arm64）。未进行交易收益回测或生成新实时推荐报告。

**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**
