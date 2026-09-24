# 今日推荐：Phase D/LPS 观察视图与 30 分钟确认实施计划

## 目标与边界

落实 2026-09-23 讨论的两项优化：从现有每日扫描中单独呈现 Phase D → confirmed SOS → BU/LPS 的日线观察对象；为日线 LPS 增加可复核的连续回踩证据，并在有合格 30 分钟数据时给出二次确认状态。目标是为 1–6 个月波段交易提供候选与入场时机复核，不生成日内 T+0 策略。

第一版为影子观察。保持正式 `recommendations`、`waiting_trigger`、`observation`、买点奖励、市场/数据/板块硬门槛、推荐历史快照及发布指针的结果不变。影子状态不能把正式观察对象升级为推荐。市场环境不允许时，影子视图仍可列出研究对象，但逐行标明正式阻断原因。

依据：用户提供的 Wyckoff Phase D/SOS/LPS SOP（源文件未纳入仓库；SHA-256 `1030279e7562030e291debbcd4d9d2d0ab040eead1125d4d9e7120abdcf5cb2c`）。现有买点状态在 `analysis/wyckoff.py:1644-1810,2051-2376`；正式分桶在 `scans/daily_candidates.py:3609-3700`；JSON 与报告入口在 `scans/daily_candidates.py:2982,4225,4491`。现有 K 线 CLI 仅接受 D/W：`fetchers/kline.py:248`、`fetchers/kline_eastmoney.py:317`。

## 交付 1：独立 Phase D/LPS 观察视图

1. 在候选扫描完成、正式分桶冻结之后，从本次已扫描的 `scored` 宇宙计算 `phase_d_lps_shadow`。不复用 Top 30 作为唯一输入，避免高质量 LPS 因综合排名较低而消失；视图记录 `scanned_count`、`phase_d_count`、`sos_lps_count`、`shown_count` 和未完成资金/基本面增强的数量。仅展示前 20 个影子对象，按现有质量分、信号新鲜度、代码稳定排序；未完成增强者标 `evidence_incomplete`，不参与影子“已就绪”统计。接入位置：`scans/daily_candidates.py:4684-4823`。
2. 已确认 SOS 可进入视图的“等待回踩”层；只有同一 `range_id` 且绑定该 SOS 的 BU/LPS 才标为 `sos_lps`。Phase B/ST、Phase C/Spring、没有父 SOS 的 LPS、SOS candidate 仍保留在原候选池；影子视图分别给出 `out_of_scope` 或 `insufficient_evidence`，不伪称 Phase D。读取 `event_history`、`short_term`、`event_health` 和 `range`，不要从显示文案反向解析状态。接入位置：`analysis/wyckoff.py:1709-1749,2295-2376`。
3. 输出独立 `phase_d_lps_shadow` JSON 对象和 HTML/Markdown「Phase D/LPS 观察」区块。逐行显示：事件日/确认日、父 SOS、原 TR 上沿、LPS 候选区、当前回踩状态、供应证据、结构失效位、板块持续性与正式门控原因。所有价位来自同一复权口径和冻结交易日；缺失显示未知。区块标题固定标注“影子观察，不参与推荐”。接入位置：`scans/daily_candidates.py:2982-3030,4225-4487,4491-4518`。

## 交付 2：连续回踩证据与日线状态

4. 扩展 `analysis/wyckoff.py` 的 BU/LPS 事件证据，不改既有 confirmed 事件和买点识别阈值。对每个父 SOS 冻结 `range_id`、突破位、SOS 日 ATR/成交量，以及可见的回踩 K 线索引。新增 `pullback_evidence`：回踩窗口与完整性、逐日量/阴线实体/低点推进 ATR、回踩量相对 SOS 与 TR 中位数、是否跌回 TR、是否出现 Higher Low、确认后的需求恢复。现有 `_bu_candidate_evidence` 只检查回踩当根与若干量能基线（`analysis/wyckoff.py:1760-1808`），所以连续收敛另作证据，不用单根结果代替。
5. 定义只依赖截至评价日 K 线的影子状态：`sos_wait_pullback`、`lps_candidate`、`daily_ready_for_30m`、`invalidated`、`insufficient_evidence`。`daily_ready_for_30m` 至少要求同箱体 confirmed SOS、健康的 confirmed LPS、当前未失效、回踩位置与供应证据均可计算；证据不足时降为 `insufficient_evidence`。连续回踩至少需要 3 根可见回踩 K 线；不足 3 根时记录单根量价事实，但连续收敛为 `unknown`。首次阈值只用于影子状态，集中定义并版本化；不得根据单日案例调阈值。结构失效以原箱体和事件健康规则为准（`analysis/wyckoff.py:520-620`）。
6. 使用逐日截断重放验证没有未来函数：SOS 当日不得看到后续确认；BU 当日不得显示 confirmed LPS；LPS 确认之前不得成为 `daily_ready_for_30m`；之后跌回原 TR 的旧事件必须失效。现有测试基础在 `tests/test_wyckoff.py:552-635`。

## 交付 3：30 分钟二次确认

7. 先做数据源适配小步验证：沿用仓库已有 fetcher/缓存和网络契约，不新增依赖。验证可获取至少近期 A 股 30 分钟 OHLCV，且时间戳、交易日、午休边界、复权口径、停牌与缺失数据可判定。若现有来源无法稳定满足契约，交付显式 `not_evaluated/source_unavailable`，不以日线替代 30 分钟结果。新增模块建议为 `fetchers/kline_30m.py`，独立于现有 D/W CLI，以免改变日线缓存语义。
8. 正式候选报告先完成日线影子视图；30 分钟分析作为同次统一入口的独立后台补充任务运行，写入带 `as_of`、输入指纹与规则版本的影子 sidecar 和补充报告。原候选 HTML 给出补充报告链接与“待更新”状态；后台任务只更新该影子区块或独立补充报告，不改正式 JSON/MD、推荐快照及三分桶。单次最多分析前 20 只 `daily_ready_for_30m`，每只超时和总超时显式配置，超时者记 `not_evaluated`。
9. 以日线报告 `as_of` 当日 15:00 与后台任务决策时点两者较早者截断，仅使用已收完的 30 分钟 K 线。统一归一到上海时间的 K 线结束时间，允许的结束时间为 10:00、10:30、11:00、11:30、13:30、14:00、14:30、15:00；拒绝形成中、午休、跨日和重复 K 线。状态机为 `awaiting_30m` → `small_sos_confirmed` → `small_lps_confirmed` → `restrengthened`，另有 `invalidated`、`not_evaluated`。独立分析模块建议为 `analysis/phase_d_lps_30m.py`。
10. 冻结日线 LPS 区间后，影子版 30 分钟规则使用下列可复现条件，阈值集中定义为 `phase-d-lps-30m/v1`：以前 8 根已完成 30 分钟 K 线形成局部区间，并用至少 14 根更早的 K 线计算 ATR；小 SOS 收盘突破该区间高点至少 0.1 根 30 分钟 ATR、收盘位处于当根振幅上 30%、成交量不少于前 8 根中位量的 1.2 倍；其后 1–4 根内回踩不跌破日线 LPS 下沿且成交量不超过小 SOS 的 0.8 倍，记小 LPS；再于后续 1–4 根中收盘站上小 LPS K 线高点，记 `restrengthened`。数据不足、ATR 不可算或日线区间缺失均为 `not_evaluated`。这些数值仅用于影子观察，需经后续回测检验，不能改变正式推荐。
11. 补充报告把 `daily_ready_for_30m` 表述为“日线条件通过，待 30 分钟确认”，把 `restrengthened` 表述为“30 分钟二次确认已出现，仍需按正式市场/板块门槛复核”。每次转换记录 30 分钟 K 线结束时间、触发价、区域上下沿和证据。不要给影子对象使用“今日可执行”标签；不改变正式三个分桶。30 分钟数据过期、来源失败或复权口径不一致时为 `not_evaluated`，不得默认通过。

## 验收标准

- 同一冻结扫描输入下，正式三分桶的代码、顺序、分数、推荐历史快照内容及发布指针与改动前逐项一致；只新增影子字段/区块。
- 用合成样本构造 Phase B/ST、Phase C/Spring、SOS candidate、confirmed SOS、BU candidate、confirmed LPS、失效 LPS；只有符合事件链的对象进入 Phase D/LPS 视图，且每一项有明确状态和原因。
- 1–2 根回踩不会伪造“持续缩量/跌幅收敛”；3 根以上样本能区分缩量收敛、放量下跌、跌回 TR、Higher Low 缺失；任意截断日的输出不读取未来 K 线。
- 30 分钟数据在对应 K 线收盘前不能产生确认；缺失、停牌、时戳越界、跨日错配、午休伪 K 线和复权不一致均返回 `not_evaluated`/`invalidated` 及原因。30 分钟确认只改变影子状态。
- 正式 JSON、Markdown、HTML 的日线影子状态和证据一致；30 分钟补充 JSON/HTML 彼此一致，引用相同日线 `as_of`、输入指纹与规则版本。两份报告均明确标“影子观察，不参与推荐”。
- 性能预算：只抓 `daily_ready_for_30m` 的前 20 只，每只抓取超时和总超时显式配置；30 分钟任务不得阻塞正式候选报告就绪。实际耗时在诊断中记录，超预算后保留未评估计数。

## 验证与发布顺序

1. 先锁定既有正式分桶、事件链和报告快照的回归样本，再实现交付 1；核对 2026-09-23 冻结扫描中的 5 个 Phase D/LPS 对象均出现在影子视图，并仍受市场与板块门控约束。
2. 实现交付 2，运行 `tests/test_wyckoff.py`、`tests/test_daily_candidates.py` 中的针对性测试；做逐日截断历史重放，并在影子记录中保存证据版本。
3. 完成 30 分钟数据契约后实现交付 3；先用合成 K 线和冻结历史数据验证状态机及时间边界，再做一次盘后真实来源烟测。来源不合格时只交付“待 30 分钟人工复核”的状态，不启用自动确认。
4. 连续积累至少 20 个成熟交易日、100 个去重事件，再比较“现有 LPS”与“连续回踩通过/30 分钟确认”两组在 5/10/20 日的前向收益、胜率、MAE/MFE 和样本覆盖。证据不足维持影子；正式门槛或排序改动另开决策。
5. 任何 `.claude/skills/stock-trend/scripts/` 下 Python 修改完成后，必须运行 `python3 .claude/skills/stock-trend/tests/test_stock_trend.py` 与 `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`（使用本仓库要求的 Python 3.10 解释器）。golden 差异逐项审查，不为消除失败直接重生成。

## 风险与处理

- **影子范围和原 Top 30 不同**：显示 `scanned_count` 与数据增强覆盖，未增强者不可标日线就绪；正式报告排序保持原样。
- **连续收敛样本短**：小于 3 根显示 `unknown`，不把“未知”当成供应衰竭；阈值版本与原始逐日证据一起保留。
- **30 分钟数据权限或可靠性不足**：先通过数据契约验收，失败时稳定降级为待人工复核；不修改日线确认或正式推荐结果。
- **回测选择偏差**：按当日可见宇宙与时间戳重放，记录未覆盖与失效样本，样本门槛之前不声称策略收益改善。
