# 候选增强与报告范围一致性修复计划

## 目标与范围

修复“最终展示的候选因未进入资金增强队列而只有 55% 数据覆盖”的可避免缺口。

保持以下不变：

- `recommendation_quality` 的 70% 覆盖率、资金 fresh 与二级数据的晋级硬门槛。
- 首轮资金增强最多 36 个、二轮最多 12 个，以及扫描的绝对时间预算。
- 数据不足的标的不得因本修复被提升到“今日可执行”。

本计划假定产品目标是：若实时预算仍充足，优先补全会进入最终输出列表的未增强候选；若预算不足，则保留观察状态并准确标明“预算截止”。

## 现状依据

- `.claude/skills/stock-trend/scripts/core/source_health.py:20-25`：全扫描 330 秒预算，首轮预取 36 个、二轮补全 12 个。
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1932-1998`：首轮按 provisional score 建立缓存未命中队列。
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:3293-3318`：二轮只在当时的 `latest_scored`/`report_scope_candidates` 中选取，且最多覆盖 `top` 个候选。
- `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:3425-3503`：未被选中者最终被标记为 `not_selected_for_enrichment`。
- `.claude/skills/stock-trend/scripts/core/recommendation_quality.py:5-12, 172-226`：K 线独自贡献 55%，且晋级要求资金 fresh 与覆盖率至少 70%。

## 实施步骤

1. 在 `stock_scanner.py` 抽出或扩展“最终输出候选范围”构造逻辑。
   - 使用与 `daily_candidates.py` 最终排序/截断一致的原始分、质量分、板块资格和代码稳定排序键。
   - 该范围仅作为二轮补全的候选源；不能更改正式分数、分桶或推荐资格。
   - 明确过滤：已具备有效资金缓存或已经处理的候选不再占用二轮名额。

2. 调整 `select_capital_topup_candidates` 的输入与调用点。
   - 首选“预计进入最终输出的未补全候选”，而非仅使用首轮 `latest_scored` 的局部列表。
   - 保持最多 `CAPITAL_TOPUP_LIMIT=12`，按当前报告排序优先级选择。
   - 当候选因全局名额、初轮截止或二轮时间不足而无法启动时，保持/设置 `not_started_deadline`；只有从未被选入任何补全范围时才使用 `not_selected_for_enrichment`。

3. 对基本面补全保持与资金补全的维度一致性。
   - 二轮中对需要资金补全的候选同时尝试基本面；若可用的同日 membership fallback 存在，沿用既有 fallback 规则。
   - 任何资金或基本面 provider 失败都继续按现有错误状态报告，不伪装成调度遗漏。

4. 完善运行指标与候选诊断。
   - 增加/明确“报告范围候选数、报告范围中未增强数、二轮覆盖数、未启动原因”指标。
   - 更新 `_candidate_diagnostic_text` 的断言与文案：队列外、预算截止、provider 失败三类必须互斥且可识别。

5. 添加回归测试并执行质量门。
   - 覆盖“显示候选初轮未选中但二轮补全成功”的正向场景。
   - 覆盖“12 个二轮名额耗尽”的边界场景，验证其标记为预算/截止而非 provider failure。
   - 覆盖“二轮补全后仍不符合市场/板块硬门槛”的场景，验证不会被错误提升。

## 验收标准

1. 构造 37 个以上缓存未命中候选时，位于最终输出范围且未在首轮处理的候选可由二轮补全；断言其 `capital.source_status` 为 `live_success` 或 `cache_valid`，不再是 `not_selected_for_enrichment`。
2. 二轮最多发起 12 个资金请求和 12 个基本面请求；运行指标精确反映选择数、启动数和有效数。
3. 没有资金与基本面的候选覆盖率仍为 0.55，且 `eligible=False`；补全后覆盖率按权重升至至少 0.80（K 线+资金）或 1.00（三维完整），但仍须通过其他硬门槛。
4. provider 失败、缓存过期、队列外、预算截止在 `source_evidence` 和用户诊断中分别保留各自状态，不能混淆。
5. 既有扫描行为保持：全量扫描不会因本修复超出 330 秒总预算，且默认请求量不超过首轮 36 + 二轮 12 的设计上限。

## 测试与验证

- 定向：`/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`
- 定向：`/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`
- 项目质量门：
  - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
  - `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
- 实时烟测：运行一次 `daily_candidates.py --json`，检查 `capital_topup_selected_count`、`capital_topup_live_started`、`capital_topup_valid_count` 与报告诊断的一致性；不更新 golden 快照，除非输出变化经人工确认属预期。

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 二轮追逐最终排名导致额外延迟 | 固定保留 12 个名额，复用现有 deadline/capacity 计算。 |
| 质量补全被误解为推荐放行 | 不变更 `assess_candidate_data` 与市场/板块/维科夫硬门槛；新增反向测试。 |
| 排序与展示范围定义漂移 | 复用单一排序函数或把排序键作为显式参数传递，测试代码顺序稳定性。 |
| 诊断仍把调度问题显示成数据源问题 | 对四类 source status 建立表驱动断言。 |

## 不采用的方案

- 将首轮从 36 扩展到全部 98 个维科夫候选：会放大实时请求并削弱 330 秒预算保护。
- 仅降低覆盖率阈值到 55%：会让缺资金数据进入推荐链，违背既有质量契约。
- 把 `not_selected_for_enrichment` 当作接口错误重试：根因是调度选择，不是 provider 故障，重试分类会误导运行审计。
