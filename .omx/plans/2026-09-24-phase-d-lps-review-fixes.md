# Phase D/LPS 影子视图审查问题修复计划

## 目标与范围

修复 `cc052f4` 的事件误匹配、临时上下文泄漏、证据展示和正式门控说明。维持正式推荐代码、顺序、分数、三分桶、快照内容和发布指针不变；只完善独立 `phase_d_lps_shadow` 及其 JSON/Markdown/HTML 展示。30 分钟确认与连续三日回踩规则仍按原计划的交付 2、3 实施，本次不引入新的交易阈值。

依据：本仓库 `.omx/plans/2026-09-23-phase-d-lps-observation-and-30m-confirmation.md` 的交付 1；`analysis/wyckoff.py:1760-1808` 的现有 BU 判定；`scans/daily_candidates.py:3823-4123,5050-5073` 的影子视图和集成点。原 SOP 的 LPS 区域是多来源判断，现有判定只有下限，不能把原箱体上沿或单根 BU 价格误报为完整的预判区间。

## 实施顺序

### 1. 锁定输出边界与回归样本

- 在 `tests/test_daily_candidates.py` 增加默认 `--news` 和 `--no-news` 的集成样本，固定输入和时钟，以功能提交前的 `d6286a0` 为基线，记录正式候选的代码、顺序、分数、三分桶及推荐快照的规范化内容哈希。测试同时检查 JSON 的每条候选、各分桶、研究快照均不含 `_phase_d_lps_context`。
- 在 `tests/test_stock_scanner.py` 增加独立 `stock_scanner` JSON 输出样本，锁定该私有字段不出现在 `rankings`。目前 `run_phase2` 无条件附带字段（`scans/stock_scanner.py:3018-3052`），而独立 CLI 直接序列化返回行（`:3629-3641`）。
- 回归样本覆盖已确认 SOS、已确认 LPS、BU candidate、Spring/ST、错误父事件、失效事件、同日不同箱体及缺失事件日期。后续各步骤只扩充这些样本，不为了通过测试更新基线。

### 2. 隔离临时事件上下文

- 给 `scans/stock_scanner.py:2279` 的 `run_phase2` 增加默认关闭的显式上下文开关；仅 `scans/daily_candidates.py:2202,2485,4990` 的每日推荐路径请求该投影，独立股票扫描器保持原公开 JSON 结构。
- 将影子构建放在任何新闻层深拷贝之前，或在深拷贝前删除所有输出对象上的私有字段。`core/candidate_news.py:248-270` 会复制候选，故 `daily_candidates.py:5066-5073` 目前只清理 `scored`/`research_population` 不够。对候选、分桶、研究输入和快照输入加边界断言。
- 验收：新闻开启和关闭时，正式 JSON、推荐历史和研究快照均不含 `_phase_d_lps_context`；独立 `stock_scanner` JSON 也不含该字段；正式字段投影的规范化内容哈希与步骤 1 的基线一致，新增内容仅在影子对象/区块中。

### 3. 严格绑定当前事件与父 SOS

- 在 `scans/stock_scanner.py:3022-3031` 从 `short_term`/`signal` 投影**同一条**当前事件的类型、事件日、确认日、箱体 ID、状态和索引。缺失或互相冲突时标记身份不完整，不从历史事件猜测。
- 重写 `scans/daily_candidates.py:3864-3884` 的查找：已给出的日期、箱体和事件类型必须同时匹配且结果唯一；找不到时为 `insufficient_evidence`，不退回最新同类型历史事件。LPS 必须同时满足 `parent_event == "sos"`、父索引相同、`range_id` 相同以及父 SOS 已确认（`:3916-3930`）。事件健康状态也只应用于身份匹配的事件。
- 先完成健康/失效判定，再计算 `phase_d_count` 与 `sos_lps_count`；两者只统计当前健康的 `sos_wait_pullback`/`sos_lps`，失效另计。逐日截断样本验证 SOS 当日、BU 当日、LPS 确认前和失效后的状态不会提前升级。
- 验收：同日不同箱体、缺日期、旧 SOS、错误 `parent_event`、父索引复用、健康状态属于另一事件等样本均不计入有效 Phase D；正确同箱体 SOS→LPS 样本仍可识别。

### 4. 补齐可复核证据与运行身份

- 在 `analysis/wyckoff.py:1760-1808` 的 BU 证据中保留评价日可见的实际 `low/high/close/volume`、SOS 成交量、SOS ATR、原 TR 上沿、5/10 日均量和 TR 中位量；报告计算实际量相对各基线的比率。只读 `event_index <= as_of` 的 K 线，缺值输出 `unknown`，不调整既有 BU/LPS 确认阈值。
- 给影子行增加结构化 `candidate_zone` 与 `pullback_state`。有 BU 时将该 K 线低高区间明确标为**已观察到的回踩区间**，并另列箱顶/突破位参考；仅有 SOS 时，由现有 ATR 下限给出部分参考、上沿未知，状态为 `partial`，不伪称完整预判区间。`event_health.state/reason_code`、实际成交量及比率在 JSON、Markdown、HTML 中一致呈现。当前 DTO 只有事件日期和量能基线（`daily_candidates.py:4008-4033`）。
- 给 `phase_d_lps_shadow` 增加 `as_of`、扫描池代码哈希/数量、扫描范围完整状态和证据规则版本（`daily_candidates.py:4044-4052`）；来源由同次冻结扫描参数传入，不从市场复盘日期推断。
- 验收：正确 LPS 样本能复算实际成交量与 5/10/TR/SOS 基线的比率；无 BU、缺 ATR、来源缺失的样本明确显示 `partial`/`unknown`；三种报告格式的证据字段和值一致，不能读取评价日之后的 K 线。

### 5. 统一正式阻断原因与展示顺序

- 让 `scans/daily_candidates.py:3823-3835` 使用正式分桶所依赖的同一套门控原因，而非原始 `scored` 上可能不存在的 `observation_reasons`。覆盖最低质量分、数据质量、板块持续性、资金背离、短线健康、`entry_wait_pullback`、失效和市场策略。前 30 名之外的影子对象也必须能独立得出原因。
- 排序先列当前有效 `sos_lps`/`sos_wait_pullback`，再列证据不足、失效及范围外诊断；每组按质量分降序、信号年龄升序、代码稳定排序。当前 `daily_candidates.py:4037-4043` 先按年龄截取前 20，可能让 ST/Spring 挤掉有效 Phase D。保持 `scanned_count` 为输入池数量，另给出有效、失效、范围外与展示数量。
- 验收：模拟 25 只新近 ST/Spring 加 1 只较旧有效 LPS 时，前 20 行包含 LPS；任何正式门控未通过的行均列出对应原因，不显示误导性的“无”。

### 6. 最终验证与文档

- 运行定向 `tests/test_daily_candidates.py`、`tests/test_stock_scanner.py`、`tests/test_wyckoff.py`，然后按 `AGENTS.md` 运行 `tests/test_stock_trend.py` 与 `tests/test_golden.py --diff`；审查每一处 Golden 数值/输出差异，不为消除失败重生成快照。
- 用同一冻结输入做 JSON、Markdown、HTML、独立扫描 CLI 的端到端检查；验证正式候选与快照指纹不变、影子状态及证据一致、无私有字段泄漏，并记录影子计算耗时及每股事件历史大小。最后执行 `git diff --check`。
- 把原计划中的个人 Downloads 绝对链接（`.omx/plans/2026-09-23-phase-d-lps-observation-and-30m-confirmation.md:9`）改为可共享的来源说明与内容哈希；若 SOP 可入库，再改用仓库相对路径。

## 完成标准

1. 五项审查发现均有对应回归用例，并在新闻开启/关闭、独立股票扫描和每日推荐入口下通过。
2. 正式推荐代码、顺序、分数、三分桶、推荐历史快照规范化内容及发布指针与固定基线逐项一致；仅影子字段与区块变化。
3. 每个 `sos_lps` 行能证明当前事件、同箱体父 SOS、健康状态及实际 BU 量价；无法证明的行降级为 `insufficient_evidence` 或 `unknown`。
4. 前 20 行优先展示有效 Phase D，正式阻断原因完整；JSON/Markdown/HTML 的状态、价位、量能和依据日一致。
5. 规定质量门通过；任何数据源或 Golden 差异逐项说明，工作区只保留预期文件。

## 风险与处理

- 上下文开关会经过两次 `run_phase2` 调用：用端到端用例覆盖首轮和全局增强轮，避免二轮覆盖后丢失影子证据。
- 原 SOP 没有完整自动化候选区上沿：使用 `partial/unknown` 和已观察 BU 区间的不同标签，不增加未经验证的价格阈值。
- 正式快照已可能存在同日版本：以固定输入比较规范化内容，不覆盖已发布快照；冲突按现有机制记录。
