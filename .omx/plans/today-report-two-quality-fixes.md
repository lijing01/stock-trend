# 今日推荐报告两个质量问题修复计划

## Requirements Summary

范围只包含上次报告暴露的两个问题，不纳入板块标签体系重构，也不改变 Spring/LPS/JAC、正式候选排序或仓位策略：

1. 修复盘中市场环境虽然五个组件均可评分，却被 `/today-recommendation` 标记为“市场环境数据质量未知”的契约丢失问题。
2. 修复新闻影子层将“连续涨停 + 风险提示/相关业务未开展”等明显风险证据判为 `neutral / risk none` 的问题。

必须保持现有安全边界：市场数据缺失、部分、过期时继续观察；新闻层仍是影子观察，不直接改变正式推荐、正式快照或自动发布结果。

## Root Cause and Intended Behavior

### 1. 盘中市场质量状态丢失

`compute_regime()` 已根据五个组件的 `score/data_status` 生成 `data_quality`、`missing_components`、`partial_components`、分数上下界和归一化信息，[market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/analysis/market_regime.py:406)。但盘中混合分支随后用只有 `score/label/advice/intraday` 的新字典覆盖结果，丢失上述字段，[market_regime.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/analysis/market_regime.py:1038)。

`load_regime_context()` 对缺失字段默认返回 `unknown`，[daily_candidates.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2591)；`build_recommendation_policy()` 再把它变成 `regime_data_quality_unknown` 硬门控，[daily_candidates.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/scans/daily_candidates.py:2996)。这导致报告的主要原因与冻结 `market_explanation.blocking_reasons` 不一致。

预期行为：

- 盘中 `regime` 保留从实际盘中组件计算出的完整质量字段。
- 若五项组件均为 `good`，政策原因应按真实分数显示 `regime_weak`，而不是 `regime_data_quality_unknown`。
- `freshness=unknown` 与 `completeness` 保持正交：缺少来源时间戳只能显示“采集时间未记录”，不能伪装成数据缺失，也不能标为 `fresh`。
- 对已经存在的同日旧缓存，允许从冻结组件保守重建缺失的质量字段；不得联网补写或修改原缓存。

### 2. 新闻风险漏判与聚合降级

`classify_article()` 只按固定短语表分类；现有词表没有覆盖“提示风险”（逆序表达）、“相关业务未开展”“不存在相关业务”“连续 N 个涨停”“股票交易异常波动”等组合语义，[candidate_news.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/candidate_news.py:18)。未命中时直接落到 `neutral/0/none`，[candidate_news.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/candidate_news.py:94)。

此外，文章级 `medium` 风险在候选聚合时没有被保留；聚合结果只有 `critical/high/none`，[candidate_news.py](/Users/jing.li7/personal/stock-trend/.claude/skills/stock-trend/scripts/core/candidate_news.py:180)。因此即使媒体风险词被命中，也可能在候选摘要中显示 `risk none`。

预期行为：

- 单独“连续涨停”只表示价格/情绪异常，标为 `medium`，不触发 shadow veto。
- “异常波动/连续涨停”与“风险提示、业务未开展、不存在相关业务、不涉及相关业务”等组合出现时，至少标为负向高风险。
- 只有来自受信官方来源、且满足明确重大风险组合规则的证据才允许进入 `critical` 并触发既有 shadow veto；媒体报道只标记风险并要求公告核验。
- 聚合风险等级完整保留 `critical > high > medium > none`。
- “撤销风险警示、解除质押、不减持、相关业务并非未开展”等否定/解除表达不得误判。

## Recommended Design

采用“修复数据契约 + 小型可审计组合规则”，不引入 NLP 依赖：

1. 盘中分支以 `ext_regime = compute_regime(ext_components)` 为基底，只覆盖混合后的 `score/label/advice/intraday`；不得重新构造一个丢字段的 regime。
2. 在读取旧的同日盘中缓存时，仅当 `regime.data_quality` 缺失，使用冻结 `components` 调用同一 `compute_regime()` 推导质量字段，并保留原有混合分数。
3. `market_explanation.quality_notes` 继续承载时间戳诊断；报告把 `source_timestamp_unknown` 渲染为清晰中文说明，与政策阻断原因分开展示。
4. 新闻分类增加命名明确的模式组和组合判定函数，输出 `matched_rules` 或等价审计字段；不要把所有“异常波动”或“连续涨停”直接加入 critical 词表。
5. 新闻聚合使用显式风险顺序保留 `medium`，shadow veto 仍只由 `critical` 产生。

## Acceptance Criteria

1. 盘中五项组件均为 `good` 时，`collect_context().regime.data_quality == "good"`，且 `missing_components == []`、`partial_components == []`。
2. 同一盘中上下文经 `load_regime_context()` 后质量字段不丢失；`build_recommendation_policy()` 不产生 `regime_data_quality_unknown`。
3. 盘中混合分低于 60 且组件质量良好时，唯一市场硬门控为 `regime_weak`，报告原因显示“市场环境评分偏弱”。
4. 任一组件为 `partial` 或 `missing` 时，盘中混合不能把它升级为 `good`；政策仍分别产生 `regime_data_partial` 或 `regime_data_missing`。
5. 同日旧盘中缓存缺失 `regime.data_quality`、但冻结组件状态完整时，可在内存中推导出 `good`；原文件内容和时间戳不被修改。
6. `freshness=unknown` 继续保留，报告明确显示“来源采集时间未记录”，且不称为 fresh、不自动转成 `regime_data_quality_unknown`。
7. 媒体标题“连续4个涨停”至少得到 `risk_level=medium`、负向或风险观察标签、非正分，且 `shadow_veto=false`。
8. 标题同时包含“连续涨停/异常波动”和“提示风险/相关业务未开展/不存在相关业务”时，得到 `risk_level >= high`；若只有媒体来源，则要求官方核验且不得触发 veto。
9. 受信官方来源的明确重大风险组合可得到 `critical`、分数 `-3`、`shadow_veto=true`；普通《股票交易异常波动公告》不得仅凭标题自动 critical。
10. 候选新闻聚合必须保留 `medium`，报告不得再把存在 medium 文章的候选显示为 `risk none`。
11. 现有解除风险/否定表达测试继续通过；新增“相关业务并非未开展”或等价反例不产生高风险误报。
12. 新闻变化只影响 `news_analysis/news_shadow`；正式 candidate 顺序、正式分桶、正式推荐快照保持不变。

## Implementation Steps

### 1. 先补回归测试，锁定两个故障

修改：

- `.claude/skills/stock-trend/tests/test_market_regime.py`
- `.claude/skills/stock-trend/tests/test_daily_candidates.py`
- `.claude/skills/stock-trend/tests/test_market_explanation_reporting.py`
- `.claude/skills/stock-trend/tests/test_candidate_news.py`

增加固定 fixture：

- 盘中完整、盘中 partial、盘中 missing 三种组件组合。
- 旧盘中缓存缺质量字段的加载场景。
- 媒体“连续涨停”、媒体“连续涨停 + 业务未开展”、官方重大风险组合、普通异常波动公告和否定/解除表达。
- 报告级断言同时检查 policy reason、市场质量说明、文章级风险和候选聚合风险。

先运行新增测试并确认其在旧实现上分别复现：市场测试得到 `unknown`，新闻测试得到 `neutral/none` 或 medium 聚合丢失。

### 2. 修复盘中 regime 契约

修改 `.claude/skills/stock-trend/scripts/analysis/market_regime.py`：

- 保留 `compute_regime(ext_components)` 的全部质量和审计字段。
- 只将 `score` 替换为锚分与外推分的混合值，并重新计算 `label/advice`，追加 `intraday=True`。
- 明确质量来自实际 `ext_components`，不能从混合分数反推。
- 检查开盘前不外推分支：历史正式收盘组件只在来源确认为已保存正式历史时视为有效；没有锚证据时保持现有保守诊断。

### 3. 增加旧缓存的只读兼容归一化

修改 `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`：

- 在 `load_regime_context()` 内增加窄范围兼容：仅针对同日、`intraday=true`、`regime.data_quality` 缺失且存在冻结组件的上下文，调用 `compute_regime(components)` 补齐质量字段。
- 保留缓存原 `score/label/advice/intraday`，只补 `data_quality/missing_components/partial_components/score_lower/score_upper/normalization_denominator/raw_weighted_total`。
- 不在此处写回缓存；组件无法可靠推导时继续 fail closed 为 `unknown`。
- 让 policy reason 与 `market_explanation.blocking_reasons` 使用同一冻结质量事实，避免报告顶部和解释表互相矛盾。

### 4. 澄清市场报告文案

修改：

- `.claude/skills/stock-trend/scripts/reporting/market_explanation.py`
- `.claude/skills/stock-trend/scripts/scans/daily_candidates.py`

要求：

- 把 `source_timestamp_unknown` 显示为“部分来源未记录采集时间；完整度与评分资格按冻结组件状态判定”。
- 保持 `freshness=unknown` 的原始审计字段，不伪造时间戳。
- “主要门槛”只列真实 policy reason；弱市且数据完整时不得显示“市场环境数据质量未知”。

### 5. 实现新闻组合风险规则

修改 `.claude/skills/stock-trend/scripts/core/candidate_news.py`：

- 将规则拆为可审计组，例如价格异常、风险提示、业务否认、正式重大风险、解除/否定表达。
- 新增组合判定函数，优先级固定为：否定/解除归一化 → 明确 critical → 组合 high → 单项 medium → 原有正负面规则。
- 使用正则覆盖“连续 4 个涨停/连续四板/股票交易异常波动”等变体，但规则输出必须记录命中的规则名和短语。
- 不把普通价格上涨当作基本面 critical；媒体风险增加 `needs_official_confirmation=true` 或等价字段。
- 保持现有官方来源白名单与域名校验，不允许任意来源文本自升级为 official。

### 6. 修复候选新闻风险聚合与展示

继续修改 `candidate_news.py` 及 `daily_candidates.py`：

- 用显式等级顺序聚合 `critical/high/medium/none`。
- `shadow_veto` 仍严格等于是否存在 critical 证据。
- 候选诊断和新闻明细显示 `medium`、命中规则以及“待官方公告核验”。
- 保持 `formal_policy_affected=false`，并验证正式快照继续剔除新闻影子字段。

### 7. 更新规格与说明

修改：

- `.claude/specs/stock-trend-skill.md`
- `.claude/skills/stock-trend/SKILL.md`

补充契约：

- 盘中混合不得丢失市场数据质量字段。
- `completeness` 与 `freshness` 是独立维度；时间戳未知不得标为 fresh，但不等同于组件缺失。
- 新闻风险组合、媒体待核验、四级聚合和 critical-only shadow veto 的边界。

## Risks and Mitigations

- 风险：为了消除“unknown”而把真实 partial 数据升级。缓解：质量只能由冻结组件的 `data_status` 经 `compute_regime()` 生成，无法推导时继续 unknown。
- 风险：把所有连续涨停或异常波动都视为重大利空。缓解：单项只标 medium；只有业务否认/风险提示等组合才升级，critical 还要求受信官方来源和明确重大语义。
- 风险：否定词处理误删真正风险。缓解：为解除风险、双重否定和普通异常波动公告建立反例测试；规则返回命中证据便于审计。
- 风险：新闻 high/medium 意外改变正式推荐。缓解：保持影子层边界和正式快照剥离测试，只有现有 shadow 排序/展示发生变化。
- 风险：旧缓存兼容逻辑长期掩盖上游问题。缓解：仅处理同日盘中且字段缺失的精确形态，添加兼容原因码，后续新缓存必须原生携带完整字段。

## Verification Steps

实现前需明确说明将修改市场环境、候选报告和新闻影子相关 Python 文件。实现后按顺序执行：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_candidate_news.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_market_explanation.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_market_explanation_reporting.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_market_regime.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_daily_candidates.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_stock_trend.py

/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 \
  .claude/skills/stock-trend/tests/test_golden.py --diff
```

再用固定离线 fixture 生成一次盘中候选 JSON/HTML，验证：

- 顶部主要门槛为 `regime_weak`，市场解释仍显示 `freshness=unknown` 及非阻断说明。
- 闽东电力式媒体标题至少显示风险和待公告核验，不再是 `neutral / risk none`。
- 正式推荐分桶与未加新闻层时一致。

如需要运行一次联网 smoke，仅在上述离线测试全部通过后执行；联网结果只验证契约，不作为回归测试基线。

## Stop Condition

新增回归测试、定向测试、`test_stock_trend.py` 和 `test_golden.py --diff` 全部通过；盘中质量字段不再丢失，报告两个区域使用一致的市场阻断原因，新闻 medium 风险不再聚合为 none，且正式推荐结果未被新闻影子层改变。
