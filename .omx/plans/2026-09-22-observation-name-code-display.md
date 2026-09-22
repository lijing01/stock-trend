# 观察列表显示股票名称与代码：实现计划

## 需求摘要

`reports/lists/daily-review-20260922-102756.html` 的“观察对象”单元格当前显示为 `301489 / 301489`。HTML 渲染器已经尝试输出 `name + code`，但当同一依据日没有行业成分快照时，分析输入在 [observation_list_analysis.py:151-158](../../.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py#L151-L158) 将名称回退为代码，最终 artifact 的 `name` 与 `code` 相同。

目标是让有效观察标的显示“股票名称 + 股票代码”（例如 `思泉新材 / 301489`），同时保持 `observation_list.yaml` 作为观察标的唯一宇宙与顺序来源，不读取今日推荐候选池，不改变六维评分或质量门控。

## 设计决策

1. 不把名称手工写入 YAML：YAML 目前只维护代码、加入日期、加入阶段；名称属于可变市场元数据，避免与配置哈希和代码清单混在一起。
2. 在观察分析阶段补充“身份元数据解析”，而不是在 HTML 层猜名称：名称会写入同日 artifact，独立于评分维度，并保留来源/时间/降级状态。
3. 解析优先级：
   - 同依据日行业成分快照中的 `stock.name`（现有路径）；
   - 东方财富证券报价/证券目录接口按代码解析名称，复用 [eastmoney_utils.py:119-165](../../.claude/skills/stock-trend/scripts/core/eastmoney_utils.py#L119-L165) 的节点轮换和现有 quote 字段路径；
   - 同日身份缓存（仅作展示元数据，不参与评分）。历史重放不得用未来 K 线或未来板块证据；名称缓存若不是同日证据，必须标记为 `identity_only`/`stale`，不能提升数据质量。
4. 对无效代码（当前 `60336`）不伪造名称：保留代码，显示“名称未提供”，并继续显示“无效 A 股代码”诊断。
5. HTML 继续使用一个紧凑单元格，但标题改为“股票名称 / 代码”，名称与代码分别渲染；当旧 artifact 中 `name == code` 时，不再重复输出两次代码，而是显示“名称未获取”提示。

## 验收标准（可测试）

- 对 2026-09-22 的有效代码，HTML 中分别出现正确名称和代码，且每个代码在对应观察行的身份单元格只出现一次：
  - `思泉新材 / 301489`
  - `联科科技 / 001207`
  - `赢合科技 / 300457`
- `60336` 仍保留在 YAML 顺序中，显示代码及“名称未提供/无效 A 股代码”，不得被静默删除或映射成其他证券。
- 名称解析失败时，报告仍成功生成；该行只降级身份显示，不影响其它五/六维分数、综合分计算或观察列表顺序。
- artifact 记录 `name_source`、名称证据日期/时间及 `name_quality`（如 `same_date`、`identity_only`、`unavailable`）；加载旧 v1 artifact 仍兼容，不能因缺少这些可选字段而整块不可用。
- 统一入口和后台 HTML 更新均使用同一解析逻辑；YAML 哈希变化、依据日校验和原子替换行为保持不变。
- 目标报告重新生成后，“观察对象”不再出现连续重复的 `301489 301489` 文本。

## 实施步骤

### 1. 增加代码到名称的身份解析边界

修改 [observation_list_analysis.py:99-181](../../.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py#L99-L181)：

- 保留现有同日成分快照名称作为最高优先级。
- 对 `candidate.name` 为空、等于代码或仅来自 `unavailable` fallback 的有效代码，调用一个可注入、可批量的身份解析器；解析器使用 `resolve_suffix()` 构造 secid，复用 EastMoney host rotation，返回名称和证据元数据。
- 对名称结果做白名单校验（非空、不是纯代码、代码匹配），防止错误接口字段污染显示。
- 将 `name_source`、`name_data_date`、`name_fetched_at`、`name_quality` 写入 candidate/row；这些字段只属于身份展示，不进入 `run_phase2` 的候选评分输入。
- 对无效 A 股代码不发起名称请求，保留原有错误诊断。

### 2. 保持 artifact 与后台流程兼容

修改 [observation_list_analysis.py:184-228](../../.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py#L184-L228) 及 [313-328](../../.claude/skills/stock-trend/scripts/analysis/observation_list_analysis.py#L313-L328)：

- `_row()` 从 candidate/scored 透传身份字段；`name` 仍可为空，但不得把代码当作真实名称。
- 不改变 schema 的宇宙、顺序、YAML `config_sha256` 和依据日绑定；新增字段保持向后兼容。
- 若引入身份缓存，缓存采用与分析 artifact 相同的原子写入策略，并记录 provider、证据日期和失败原因。
- 确认 [run_today.py:160-186](../../.claude/skills/stock-trend/scripts/bridge/run_today.py#L160-L186) 与 [today_background.py:216-243](../../.claude/skills/stock-trend/scripts/bridge/today_background.py#L216-L243) 的同步/后台路径都调用同一 analyzer，不另造名称解析分支。

### 3. 修正 HTML 身份单元格

修改 [market_regime.py:773-818](../../.claude/skills/stock-trend/scripts/analysis/market_regime.py#L773-L818)：

- 将表头改成“股票名称 / 代码”。
- 名称有效时渲染为名称在上、代码在下（或等价的 `名称（代码）`），代码只输出一次。
- `name` 为空或等于 `code` 时显示“名称未获取”，仍输出代码；不得再产生 `code<br>code`。
- HTML 转义、待分析状态、降级状态和 13 列表格结构保持不变。

### 4. 补充回归测试与报告重生成

- 在 [test_observation_list_analysis.py](../../.claude/skills/stock-trend/tests/test_observation_list_analysis.py) 增加：同日快照缺失但身份解析成功、解析失败降级、无效代码不请求解析、身份字段写入/加载兼容测试。
- 在 [test_market_regime.py:394-446](../../.claude/skills/stock-trend/tests/test_market_regime.py#L394-L446) 增加：名称/代码各出现一次、旧 artifact `name == code` 不重复、HTML 转义和无效代码提示测试。
- 用固定的 resolver mock 验证三只有效代码名称，避免测试依赖实时网络；必要时为 quote 请求适配器增加独立单元测试。
- 运行报告生成流程，更新 `daily-review-20260922-102756.html`（或生成新的同日时间戳报告），检查 YAML 顺序、六维分数和身份诊断均未改变。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 实时名称接口失败或限流 | 有界重试/节点轮换；失败不阻断报告，显示“名称未获取”并记录证据状态 |
| 历史重放借用未来信息 | 名称仅作身份展示；评分仍严格按 `as_of_date` 截止；非同日名称缓存不得标记为 fresh 或提升质量 |
| 旧 artifact 无新字段 | 新字段全部可选；渲染器对旧数据做 `name == code` 防重复兼容 |
| 名称变更或错误映射 | 校验代码与接口返回代码一致，记录 provider/时间；不允许由名称反推代码或改变 YAML 宇宙 |
| 每只股票一次请求拖慢后台 | 解析器批量化或按缺失名称去重，设置总超时；名称解析只影响展示，不延长六维分析的核心超时预算 |

## 验证步骤

1. 离线运行新增观察分析和 HTML 单元测试，确认固定 mock 下三只股票名称正确、`60336` 保留且不请求名称。
2. 运行项目质量门：
   - `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
3. 执行一次同日复盘生成/更新，检查报告中有效行显示 `名称 / 代码`，代码不重复，六维列和评分与改动前一致。
4. 检查 `git diff --check`、报告存在性、artifact 的 `data_date`/YAML 哈希/名称证据字段；确认 `git status` 只包含预期源代码与测试变更。

## 停止条件

名称解析失败不能阻止复盘报告交付；若实时和缓存身份源均不可用，则交付带“名称未获取”的降级报告，并保留可追踪的诊断，不得用代码字符串冒充股票名称。
