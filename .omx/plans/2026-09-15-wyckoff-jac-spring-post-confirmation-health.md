# JAC / Spring 确认后健康与粘性失效修复计划

## 需求摘要

目标是消除“历史买点在后续结构明显走坏后仍被当作当前有效买点”的剩余缺口，把 LPS 已采用的“历史确认与当前健康分离”模式扩展到 JAC 和 Spring。

- LPS 现有语义必须保持：confirmed_holding 可用；follow_through_weakened、failed_breakout、state_unknown 取消买点等级与可执行资格；硬失效不会因后续价格收回而复活。现有实现见 .claude/skills/stock-trend/scripts/analysis/wyckoff.py:443-529。
- JAC 必须建立与事件原箱体绑定的确认后健康状态，不再只依赖当前选中箱体和最后一根 K 线的 _tr_state（wyckoff.py:1684-1714）。首个 JAC 仍须等待 LPS 后再确认，现有 post_lps_reconfirmation 合同不变（wyckoff.py:1605-1623、233-258）。
- Spring 保留“下穿支撑后最多 3 根内收回才 confirmed”的确认合同（wyckoff.py:1388-1403），保留最多 2 根 K 的执行时效和追高距离门控（wyckoff.py:164-184、261-375），新增确认后跌破 Spring 结构的专属粘性失效。
- 历史事件记录不得改写或删除；当前健康、买点等级和可执行性是独立派生结果。
- 不调整买点奖励、执行时效、追高阈值、事件检测窗口或回测收益统计口径。

## 设计决策

采用“统一健康协议，事件类型自带规则”。输出继续使用 event_health、short_term.current_state 和 signal.current_state；内部由 LPS、SOS/JAC、Spring 的独立评估函数产生相同的基础字段，不能把 LPS 的价格锚点直接套到其他事件。

| 状态 | 适用事件 | 含义 | 买点/可执行 |
| --- | --- | --- | --- |
| confirmed_holding | LPS/JAC/Spring | 确认后结构仍健康 | 继续交给时效和距离门控 |
| retest_pending | JAC | 回到原箱顶接纳区，尚未恢复强势接纳 | 不可执行，观察池 |
| follow_through_weakened | LPS | 跌破 LPS 触发低点但未破原箱体结构底 | 不可执行，观察池 |
| failed_breakout | LPS/JAC | 收盘进入原箱体失效区；历史上发生一次即粘性失效 | 不可执行，观察池 |
| structure_invalidated | Spring | 确认后收盘再次跌破 Spring 低点；粘性失效 | 不可执行，观察池 |
| state_unknown | LPS/JAC/Spring | 缺少原事件、所属箱体、索引或 ATR | 不可执行，观察池 |

结构规则：

1. LPS 完全沿用当前算法和 reason code，作为零回归基准。
2. JAC 以 SOS 事件的 range_id、原箱顶 resistance 和事件时冻结的 breakout_atr 为锚。当前收盘在箱顶接纳区为 retest_pending；确认后任一收盘进入失效区为粘性 failed_breakout；否则为 confirmed_holding。阈值复用现有 ATR 缓冲意图，但必须以事件时 ATR 冻结，避免后续波动变化让失效线漂移。
3. Spring 以事件 K 线低点作为专属结构底，只用确认后的收盘判定硬失效；影线短暂跌破不触发硬失效，与 LPS 的收盘口径一致。
4. 粘性通过扫描 detected_index + 1 之后所有可见收盘实现，不维护跨运行内存状态。旧事件失效后，只有新的 confirmed 事件能恢复资格。

## 验收标准

1. 新鲜且健康的 Spring 仍可获得一级买点；确认后任一收盘跌破 Spring 事件低点后，current_state=structure_invalidated、classify_buy_point_level 返回 None、is_buy_signal 为 False、entry_timing.executable 为 False。
2. Spring 硬失效后，即使最新收盘重回支撑之上，旧 Spring 仍失效；仅影线跌破但收盘未破不得触发硬失效。
3. 符合“LPS 之后再确认”的 JAC 只在 confirmed_holding 且通过时效/距离门控时获得三级买点。
4. JAC 回到原箱顶接纳区后输出 retest_pending，不得进入今日可执行或次日确认；任一确认后收盘进入原箱体失效区后输出粘性 failed_breakout。
5. JAC 一旦 failed_breakout，后续重新站回箱顶也不复活旧 JAC；必须由新 confirmed SOS/JAC 恢复资格。
6. JAC/Spring 缺少所属箱体或结构锚点时 fail closed 为 state_unknown，不能因历史 status=confirmed 获得买点等级或可执行资格。
7. LPS 当前健康、转弱、粘性失效、影线容忍和未知状态用例全部保持通过，现有 reason code 不变。
8. 三类事件的非健康状态都取消 strict buy level 和奖励，阻断回测信号计数，并在候选流程中落入观察池且携带事件专属 reason code。
9. 完全缺少 event_health/current_state 的旧缓存保持现有兼容；新鲜引擎输出对 confirmed LPS/JAC/Spring 必须显式生成健康状态。
10. 维科夫定向测试从当前 82 项基线增加 JAC/Spring 生命周期用例后全部通过；项目强制质量门和 golden diff 均通过。

## 实施步骤

### 1. 先用失败测试锁定 JAC / Spring 生命周期

修改：

- .claude/skills/stock-trend/tests/test_wyckoff.py:49-228
- .claude/skills/stock-trend/tests/test_wyckoff.py:713-831
- .claude/skills/stock-trend/tests/test_wyckoff.py:971-1055

工作项：

- Spring：健康持有、收盘跌破事件低点、影线跌破容忍、历史跌破后收回仍失效、缺失事件箱体时 state_unknown。
- JAC：箱顶上方持续接纳、回踩 pending、收盘深入原箱体、历史失败后收回仍失效、缺失原箱体时 state_unknown。
- 表驱动断言 Spring/JAC/LPS 所有非健康状态均在 classify_buy_point_level、is_buy_signal 和 build_entry_timing 层 fail closed。
- 不删除现有 LPS 测试，不放宽阈值。首次运行应只因新合同尚未实现而失败。

### 2. 把 LPS-only 状态门扩展为通用事件健康协议

修改 .claude/skills/stock-trend/scripts/analysis/wyckoff.py:185-408、410-529。

- 引入通用规则版本和非可执行状态集，替代下游对 LPS_NON_HEALTHY_STATES 的语义依赖；如存在外部导入，保留兼容 alias。
- 保持 read_current_event_state 的读取优先级，使 classify_buy_point_level、is_buy_signal、is_executable_buy_signal 和 build_entry_timing 对所有事件非健康状态一致阻断。
- 增加事件专属 reason code：wyckoff_jac_retest_pending、wyckoff_jac_failed_breakout、wyckoff_spring_structure_invalidated，以及安全的 unknown 代码；健康阻断继续优先于过期和距离门控。
- 区分“整个新协议不存在的旧快照”和“新协议存在但必需数据缺失”：前者保持兼容，后者必须 unknown 阻断。

### 3. 实现 Spring 和 JAC 的路径型健康评估

修改 .claude/skills/stock-trend/scripts/analysis/wyckoff.py:423-529、1388-1427、1684-1714。

- 提取只负责公共 payload 与输入校验的小 helper；LPS/JAC/Spring 的价格规则保留在独立函数中。
- Spring payload 记录 event_type、event_range_id、structural_floor（事件低点）、current_close、breach_date 和 evaluated_through，扫描确认后全部收盘保证粘性。
- JAC payload 记录原箱顶、接纳/失效线、事件冻结 ATR、首次失效日期和最新评估日期，扫描确认后全部收盘判断是否曾发生硬失败。
- _tr_state 保留为当前箱体关系的辅助输出，不再是 JAC 生命周期的唯一事实源；结果冲突时，以原事件所属箱体的粘性健康结果为准。

### 4. 在分析主流水中计算并传播统一健康状态

修改 .claude/skills/stock-trend/scripts/analysis/wyckoff.py:1837-1910、1981-2017、2051-2083。

- 对当前 active confirmed lps、sos、spring 定位其 range_id 对应原箱体并运行专属评估。
- 统一回填 short_term.current_state、signal.current_state、entry_timing.current_state，以及顶层和 short_term.event_health。
- JAC retest_pending / failed_breakout 对 phase、sub-phase、status 的现有降级保留，但改由原事件健康结果驱动；禁止 _tr_state 在旧 JAC 已粘性失效后将其复活。
- confirmed_event 和 event_history 保持历史事实不变。

### 5. 对齐扫描、排序、观察分桶和报告语义

修改：

- .claude/skills/stock-trend/scripts/scans/stock_scanner.py:1894-1921、2505-2530、3160-3190
- .claude/skills/stock-trend/scripts/scans/daily_candidates.py:110-118、158-255、2453-2475、2606-2636、3229-3354
- .claude/skills/stock-trend/scripts/reporting/report.py:375-455、995-1031

工作项：

- stock_scanner.wyckoff_gate_pass 对历史 confirmed 但当前不健康的 LPS/JAC/Spring 保留进入研究/观察流水的能力，但不得把它们视为当前买点。当前特殊保留仅覆盖 LPS（stock_scanner.py:1911-1920）。
- daily_candidates 取消 JAC/Spring 的严格等级、优先奖励和推荐资格，放入 observation，不占 actionable、waiting-trigger、next-day-confirmation 名额，并输出事件专属中文原因。
- 报告同时展示“历史事件已确认”和“当前健康/失效状态”；JAC/Spring 不复用 LPS 专属文案，失效后明确要求新结构重新确认。

### 6. 锁定生产候选与回测口径

修改：

- .claude/skills/stock-trend/tests/test_stock_scanner.py:1652-1697
- .claude/skills/stock-trend/tests/test_daily_candidates.py:1620-1688、3316-3365、3587-3636
- .claude/skills/stock-trend/tests/test_wyckoff_backtest.py:1-150
- .claude/skills/stock-trend/tests/test_stock_trend.py:782-829 及相关报告集成用例

工作项：

- 对 Spring structure_invalidated、JAC retest_pending、JAC 粘性 failed_breakout、两者 state_unknown 做表驱动分桶断言。
- 验证这些事件虽保留 confirmed_event，但 buy_point_level=None、奖励为 0、只落 observation，证据链保留 event_type、结构线和首次失效日期。
- 回测增加反例：确认后已失效的 Spring/JAC 不得进入 signal_pairs，确认历史仍可在分析 payload 中审计。
- 增加报告文案断言，防止 JAC/Spring 失效被误标为 LPS 转弱或仍显示买点 badge。

### 7. 更新合同文档并完成全量验证

修改 .claude/specs/stock-trend-skill.md:84-100、docs/wyckoff-analysis-design.md:205-230；如用户面合同同步变化，再更新 .claude/skills/stock-trend/SKILL.md:194-205、321-335。

- 明确三类 confirmed 事件都必须通过当前健康门，历史确认不等于当前可执行。
- 不重生 golden 来掩盖失败；仅在逐项确认属于预期展示变化后更新快照。

按以下顺序验证，使用仓库要求的 Python 3.10：

    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff.py
    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
    /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff

## 风险与缓解

- 失效阈值过紧：JAC 明确分离箱顶接纳区和深回箱失效区；使用事件冻结 ATR，并为边界值、小数误差和影线单独建测。
- 失效阈值过松：用明确的收盘路径反例证明跌破后立即失去买点等级、执行性与回测计数资格。
- 粘性被最新 K 线覆盖：所有硬失效用确认后完整 close path 评估，测试必须包含“中间跌破、最后收回”。
- 原箱体选错：只允许 range_id 精确匹配；不按当前价格猜测，匹配不到时 state_unknown。
- 下游只识别 LPS 原因：共享非健康状态/原因映射，并以引擎、扫描、候选、报告、回测的表驱动用例锁定。
- 旧缓存行为突变：明确区分旧协议缺失和新协议数据不完整；只有前者保留旧兼容。
- golden 掩盖逻辑回归：先跑纯逻辑定向测试，再跑集成与 golden diff；逐项审查任何预期快照变化。

## 两步执行拆分

建议按“先形成安全闭环，再补齐展示与证据链”拆分，不按 JAC/Spring 各拆一步；两者共用健康状态协议和下游资格门。

### 第一步：安全闭环

范围：

- .claude/skills/stock-trend/scripts/analysis/wyckoff.py
- .claude/skills/stock-trend/scripts/scans/stock_scanner.py
- .claude/skills/stock-trend/scripts/scans/daily_candidates.py
- .claude/skills/stock-trend/tests/test_wyckoff.py
- .claude/skills/stock-trend/tests/test_stock_scanner.py
- .claude/skills/stock-trend/tests/test_daily_candidates.py

执行内容：

1. 先补 Spring/JAC 确认后健康、粘性失效和 fail-closed 测试。
2. 实现通用健康协议、Spring/JAC 专属评估器、原箱体绑定和完整 close path 扫描。
3. 将非健康状态接入 is_buy_signal、is_executable_buy_signal、classify_buy_point_level 和 build_entry_timing。
4. 让候选扫描至少完成硬阻断：失效事件只能进入 observation，不得进入 actionable 或 waiting-trigger。
5. 保持新增字段 additive，旧缓存兼容；报告暂时允许使用通用 fallback 文案，但不能误显示为可执行买点。

第一步出口条件：

- Spring/JAC 失效后没有买点等级、奖励或执行资格。
- JAC 回踩输出 retest_pending，深回箱输出粘性 failed_breakout。
- 中间跌破、最后收回仍保持失效。
- LPS 现有 82 项回归和新增生命周期测试全部通过。

建议提交：fix(wyckoff): add confirmed event health gates

### 第二步：完整交付

范围：

- .claude/skills/stock-trend/scripts/reporting/report.py
- .claude/skills/stock-trend/scripts/backtesting/wyckoff_backtest.py
- .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
- 报告、候选分桶和集成测试
- .claude/specs/stock-trend-skill.md
- docs/wyckoff-analysis-design.md
- 必要时 .claude/skills/stock-trend/SKILL.md

执行内容：

1. 增加 JAC/Spring 专属 reason code、中文状态文案和首次失效日期展示。
2. 验证失效事件保留 confirmed_event 历史，但不占推荐名额、不获得奖励。
3. 回测确认失效的 JAC/Spring 不进入 signal_pairs，同时保留审计字段。
4. 增加报告 badge、候选 observation、回测和旧缓存兼容的集成断言。
5. 更新规格/设计文档；逐项确认预期输出变化后再运行 golden diff。

第二步出口条件：

- 引擎、扫描、候选、报告和回测对健康状态的解释一致。
- 失效信号不会被误标为 LPS 转弱或仍显示可执行买点。
- 定向测试、test_stock_trend.py 和 test_golden.py --diff 全部通过。

建议提交：docs(test): complete Wyckoff lifecycle integration

两步必须按顺序执行。第一步完成后系统已经具备安全阻断能力；第二步主要补齐可观测性、统计证据和文档合同，独立回滚边界清晰。

## 完成标志

- JAC、Spring、LPS 三类 confirmed 事件都有显式当前健康结果。
- 所有软阻断、硬失效、未知状态在买点等级、执行时机、候选分桶和回测中结论一致。
- LPS 语义零回归，旧缓存兼容边界有显式测试。
- 定向套件、仓库两个强制质量门和 golden diff 全部通过，且没有为消除失败而未经审查重生快照。

## 当前证据

- 2026-09-15 实测 python3 .claude/skills/stock-trend/tests/test_wyckoff.py：Ran 82 tests，OK。
- 核心缺口：analyze_kline_dict 只对 active confirmed LPS 计算健康状态（wyckoff.py:1879-1892）；JAC 只做当前 _tr_state 降级（wyckoff.py:1893-1907）；Spring 无确认后健康分支。
- 下游缺口：通用买点/时机函数仍依赖 LPS_NON_HEALTHY_STATES（wyckoff.py:185-189、233-258、309-322、1334-1345）；候选观察原因映射只覆盖 LPS 与部分 JAC 状态（daily_candidates.py:3229-3242）。

---

本计划及其后续产出仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
