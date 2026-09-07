# PR 1 / PR 2 市场解释与风格旁路实施计划

> **For agentic workers:** 使用 `subagent-driven-development` 或 `executing-plans` 按任务执行；先完成 PR 1 验收，再集成 PR 2。以下为计划，本次不修改生产代码、不发起远端 PR。

**Goal:** 解释现有市场分与限制原因，建立独立、可复算的风格观察数据，同时保持正式候选资格和排序不变。

**Architecture:** 现有 `market_regime` 是正式评分唯一来源。增加解释对象，渲染器消费同一对象；风格计算和实验历史走独立命令与存储，候选扫描只读取冻结的旁路输入。正式推荐快照继续保存旧字段投影。

**Tech Stack:** Python，现有 fetcher、unittest、JSON、Markdown/HTML；不引入依赖。

## 0. 已核对的实现与范围

- `scripts/analysis/market_regime.py`：`TREND_INDEX_CODES` 仅上证、沪深300、深成；`AMOUNT_INDEX_CODES` 单独用于成交额。`score_index_trend` 为 0/40/60/100 四档，`compute_regime` 权重为 25/20/25/20/10。
- `scripts/scans/daily_candidates.py`：`load_regime_context` 丢弃组件细节；`build_recommendation_policy` 遇到 partial 提前返回；`generate_report`、`_generate_html`、`build_json_output` 是输出入口。
- `scripts/core/recommendation_snapshot.py`：正式快照按交易日保护已有内容，变更内容进入冲突审计。新增展示字段不能意外进入正式内容哈希。
- 本文 `scripts/`、`tests/` 均相对于 `.claude/skills/stock-trend/`；文档路径相对于仓库根目录。
- 两个 PR 都不改变正式权重、阈值、候选宇宙、排序、扩池和盘中门控；不新增价格计划字段，不修改历史报告。
- PR 2 交付风格状态与诊断性分桶，不试验弱市放行。新的放行规则及收益评价属于后续 PR。

## 1. PR 1 数据契约

新增 `market_explanation`，旧 `regime`、`policy.reasons` 原样保留：

```json
{
  "schema_version": "market-explanation/v1",
  "basis_date": "2026-09-07",
  "basis_generated_at": "2026-09-07 15:04:32",
  "mode": "close",
  "score": 53.4,
  "components": [
    {"id": "index_trend", "score": 13.3, "weight": 0.25,
     "contribution": 3.325, "detail": "三指数MA20状态平均"}
  ],
  "blocking_reasons": ["regime_data_partial", "regime_weak"],
  "quality_notes": ["capital_alternative_metric"],
  "reconciliation": "matched"
}
```

示例组件仅示意一行；实现固定输出五个组件。贡献显示至少三位小数，合计使用正式评分相同的归一化和舍入规则；不能用显示舍入后的贡献反算正式分。

每个组件新增 evidence：

```json
{
  "completeness": "complete",
  "freshness": "unknown",
  "source_kind": "alternative",
  "metric": "market_main_force_net_inflow",
  "provider": "unknown",
  "data_date": null,
  "fetched_at": null,
  "usage": "reference_only",
  "reasons": ["source_timestamp_missing"]
}
```

枚举固定：completeness=complete/partial/missing；freshness=fresh/stale/unknown；source_kind=primary/alternative/estimate/unknown；usage=scorable/reference_only/unavailable。

这些字段是新证据资格，不反写旧 `data_status`。完整返回不意味着来源等价；无来源时间不得凭全局日期标 fresh。旧缓存只有 legacy 状态时保守标 unknown，不伪造采集时间。指数部分成功要同时记录 expected/available counts。

资金明确区分 northbound_net_buy 与 market_main_force_net_inflow；北向缺失但主力资金有效，可显示“替代指标，旧模型仍因 partial 受限”，不能将其显示成资金完全缺失。

盘中解释必须保存 anchor_score、blend_weight、projected_score；展示混合公式。缺少锚证据时 reconciliation=unavailable，显示已存分，不声称组件合计可以解释混合分。收盘也要保留归一化分母与缺失项；53.4–53.4 不标为统计置信区间。

## 2. PR 1 任务与验收

### P1.1 锁定基线与新增纯解释函数

**文件：** 新建 `scripts/analysis/market_explanation.py`、`tests/test_market_explanation.py`；修改 `scripts/analysis/market_regime.py`、`tests/test_market_regime.py`。

- [ ] 固定最小测试输入：五项 13.3/37.7/62.5/100/69.4，capital=partial，三指数状态 false/true、false/false、false/false。
- [ ] 先写以下契约测试并运行，确认新增模块缺失导致失败：

```python
def test_close_score_reconciles(self):
    ctx = fixture_close_context()
    result = build_market_explanation(ctx, "2026-09-07")
    self.assertEqual(result["score"], 53.4)
    self.assertEqual(result["reconciliation"], "matched")
    self.assertAlmostEqual(sum(x["contribution"] for x in result["components"]), 53.43)
    self.assertIn("regime_weak", result["blocking_reasons"])
    self.assertIn("regime_data_partial", result["blocking_reasons"])
```

`fixture_close_context()` 在该测试文件内返回上述五组件、2026-09-07 日期、intraday=false、regime={score:53.4,label:弱势,data_quality:partial}，不读取运行缓存。

- [ ] 实现 `build_market_explanation(ctx, expected_date)` 纯函数；从组件、指数证据和冻结上下文生成解释，禁用网络与文件读取。
- [ ] 将正式权重提升为 `market_regime.py` 的共享常量，原 `compute_regime` 继续消费相同数值；解释消费同一常量，避免两份公式。
- [ ] 在 `collect_context` 保存计算时已有的指数 MA20、日期、来源和盘中混合证据；`save_context` 和 JSON 显式映射同步补齐。
- [ ] 对缺失/过期/partial/弱势收集完整诊断列表，保留旧策略提前返回逻辑及原 reason 顺序。
- [ ] 增加反例：旧缓存、部分指数失败、非有限数、日期错误、59.9/60/79.9/80边界、盘中混合、输入深拷贝前后相等。
- [ ] 运行 `python3 .claude/skills/stock-trend/tests/test_market_explanation.py` 和 `python3 .claude/skills/stock-trend/tests/test_market_regime.py`，全部通过。

### P1.2 打通报告，固定正式快照投影

**文件：** 修改 `scripts/scans/daily_candidates.py`、`tests/test_daily_candidates.py`；扩展 `tests/test_recommendation_snapshot.py`。新增 `scripts/reporting/market_explanation.py` 只负责 MD/HTML 展示。

- [ ] 先写测试：加载后更改缓存，三个输出仍使用入口冻结的解释；HTML转义板块/来源文本；解释缺失时仍可读旧缓存。
- [ ] `load_regime_context` 携带 market_explanation；旧缓存通过纯函数补充未知证据，不补抓网络。
- [ ] 新渲染函数 `render_market_explanation(explanation, format)` 只接受 markdown/html，固定输出五维贡献、指数名单、质量状态、全部限制原因。
- [ ] 候选三个输出入口以及复盘 MD/HTML 消费同一对象；候选报告不需要重新计算市场环境。
- [ ] 在 `_save_recommendation_snapshot` 前按原 `load_regime_context` 返回字段建立显式 legacy 投影，仅排除新扩展。不能通过全局忽略未知字段来改变通用哈希语义。
- [ ] 测试新增解释前后正式快照 content_sha256 相等，原同日冲突保护仍有效；新解释可以保存于当前 market_regime 和新报告，但不回写旧历史。
- [ ] 测试83分且partial仍 observation，53.4且good仍 observation；正式 buckets、顺序、原因与固定旧输入一致。
- [ ] 运行 daily_candidates、recommendation_snapshot 两个测试脚本；检查三种格式的业务字段一致。

### P1.3 文档与交付

**文件：** 修改 `.claude/skills/stock-trend/SKILL.md`，说明市场分与证据资格分开、替代资金仍受旧门控约束。

- [ ] 运行本文公共质量门禁。
- [ ] 以离线fixture在临时目录生成样例MD/HTML/JSON，确认53.4和完整原因，不覆盖用户报告。
- [ ] 审查 `git diff`，只提交本PR文件；建议提交 `feat: explain market regime evidence`。
- [ ] PR描述附53.4样例、旧策略等价测试、兼容缺口；声明没有放宽partial。

## 3. PR 2 数据契约和固定实验边界

依赖 PR 1。采用独立命令 `scripts/analysis/market_style.py`，以 `--context <market_regime.json> --json` 执行。候选入口新增 `--style-shadow <文件>` 显式开启读取，默认关闭；现有市场复盘不承担额外指数网络延迟。

指数代码固定：沪深300=000300.SH，中证500=000905.SH，中证1000=000852.SH，创业板指=399006.SZ，科创50=000688.SH。使用既有 fetch_index_kline 链路；实施时用提供方返回名称/代码校验映射，不把空数据归为零分。

旁路计算收盘有效样本要求：指数最后日期等于冻结basis_date，至少21个有效交易日且日期唯一升序，价格为有限正数；未来记录拒绝，缺失与停牌不能前向填充冒充新行情。盘中可展示 provisional，但不得进入正式收盘实验集。周末依据日使用已有推荐日期解析规则。

每风格单独保存MA20四档分及5/20日收益、MA20距离、MA20斜率（复用现有 `_index_metrics` 定义）。不把五指数平均成新全市场分。

```json
{
  "schema_version": "market-style-shadow/v1",
  "model_version": "style-ma20/v1",
  "parameter_version": "style-observer/v1",
  "basis_date": "2026-09-07",
  "snapshot_type": "formal",
  "legacy_context_sha256": "sha256-of-frozen-business-input",
  "styles": {},
  "status": "partial",
  "formal_policy_affected": false
}
```

业务哈希包含公式版本、参数、用于计算的K线、成分输入与上下文；排除请求耗时和采集时刻等运行噪声，另保存采集元数据。原始有效输入随实验记录保存，以支持未来复算。

候选指数归属输入采用显式 `--memberships <JSON>` 可选参数，格式为 records 数组，每条包含 index_code/member_code/effective_from/effective_to/known_at/source；必须 known_at 不晚于信号时间且有效期覆盖basis_date。缺少可信成分输入时全部 unknown，不能用行业名或当前成分倒推历史。

PR 2 不新增未验证的成分抓取服务：交付输入校验、日期匹配、持久化和未知状态。实际生产来源未接入时仅交付市场风格面板，不声称已覆盖个股风格。

诊断性候选状态固定：旧bucket原样保存；matched_style_state=strong(100)/mixed(40或60)/weak(0)/unknown。多重归属保留全部，状态不一致标 mixed，不选最高分；该字段不称为“新模型可执行”。shadow_bucket与legacy_bucket相同，action_changed=false。该数据用于后续定义门控实验，不能据此评价新增放行效果。

## 4. PR 2 任务与验收

### P2.1 风格采集与计算

**文件：** 新建 `scripts/analysis/market_style.py`、`tests/test_market_style.py`；复用现有 `scripts/analysis/market_regime.py` fetcher及均线函数。

- [ ] 先写纯计算测试 `compute_style_observations(context, rows_by_code)`：三种升降序fixture验证0/40/60/100四状态、21日边界、NaN、重复日期、旧日期、未来日期。
- [ ] 实现上述纯函数，返回styles每项score/status/reasons/metrics/source evidence；缺数据返回null和unavailable。
- [ ] CLI先读取并冻结context，再用最多2个并发采集任务获取5指数；总预算90秒、单指数30秒。借用已有进程隔离与超时模式；不能仅Future超时但留后台任务继续写缓存。
- [ ] 任一失败只降低旁路status；5个全部失败仍输出结构化unavailable。CLI非法参数返回非零，数据不可用以结构化状态交付。
- [ ] 网络使用mock验证调用上限、超时回收、无context修改；真实源试验单独记录，失败不阻断离线算法测试。
- [ ] 运行 `python3 .claude/skills/stock-trend/tests/test_market_style.py`。

### P2.2 成分匹配与独立实验历史

**文件：** 新建 `scripts/core/market_shadow_snapshot.py`、`tests/test_market_shadow_snapshot.py`；在market_style模块实现 `match_candidate_styles(code, records, basis_time)`。

- [ ] 先写测试：同输入幂等、同日不同输入并存、并发同内容无损、临时文件失败可清理、正式推荐目录完全不变。
- [ ] 实现 `save_shadow_run(payload, root)`，复用canonical_json/content_sha256与现有原子发布模式；新root为 `.cache/stock-trend/market_shadow_history/`。
- [ ] 路径固定 `<formal|provisional>/<basis_date>/<input_digest>.json`；不用会被覆盖的latest指针。不同运行与当日正式推荐的关联另存hash和path。
- [ ] 实现成分校验和时点匹配：双归属、未知、未来known_at、有效区间冲突、代码格式不符、无来源均有独立测试；未知不视为无成分。
- [ ] 输入证据不足可保存诊断记录，但ready=false，后续评价不得混为合格样本。
- [ ] 运行 `python3 .claude/skills/stock-trend/tests/test_market_shadow_snapshot.py`。

### P2.3 候选旁路接入与展示

**文件：** 修改 `scripts/scans/daily_candidates.py`、`scripts/reporting/market_explanation.py`、`tests/test_daily_candidates.py`、`tests/test_market_explanation.py`。

- [ ] 先测试旁路开/关正式policy、排序、扩池次数、buckets、正式快照哈希逐项相等。
- [ ] 在候选入口读取指定旁路文件一次，检查basis_date、context哈希、schema/model版本；不匹配显示stale/mismatch，不刷新或改写上下文。
- [ ] 在正式select/classify后对深拷贝候选计算诊断注释；不得把style字段回写传入正式快照的候选字典。
- [ ] 为比较记录保存所有本次已评分候选、旧分数、资格和入选状态，明确sample_scope=scanned_population以及扫描是否截断；不能把Top30当完整候选宇宙。
- [ ] MD/HTML新增五风格状态表与“实验观察，不参与推荐”说明；JSON新增shadow对象。无成分证据时明确“个股风格未知”。
- [ ] 候选对照写入同一独立root的 `candidate_runs/<basis_date>/<input_digest>.json`，包含市场实验hash、候选集合hash、旧bucket、风格状态、数据质量和正式快照追踪状态。
- [ ] 将保存失败、正式快照conflict保留为追踪状态；报告照常输出，不重跑扫描来消除冲突。
- [ ] 运行daily_candidates、market_explanation、market_shadow_snapshot测试，全部通过。

### P2.4 离线端到端与运维说明

**文件：** 新建 `tests/test_market_shadow_integration.py`；修改 `.claude/skills/stock-trend/SKILL.md`。

- [ ] 固定context+5指数K线+候选fixture，全程mock网络并把输出重定向临时目录；开关旁路各跑一次。
- [ ] 断言正式决策完全相同、实验记录可复算、MD/HTML/JSON的时间和业务字段一致。
- [ ] 测试收盘/盘中目录隔离，两个日期的输入不能混用；缺5指数、无成分、写盘失败仍不影响正式报告。
- [ ] 文档提供顺序：生成复盘context→独立style命令→候选命令指定输出文件；JSON stdout只输出JSON，状态/路径写stderr。
- [ ] 执行公共门禁，审查是否引入隐式网络和正式哈希变更；建议提交 `feat: observe market style in shadow mode`。
- [ ] PR描述披露真实数据源可用性、成分覆盖率、运行时间与尚未验证收益，不称“已优化推荐”。

## 5. 公共质量门禁与交付顺序

修改scripts下Python前先说明影响范围。各PR运行针对性测试后必须运行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

PR 2另运行 `python3 .claude/skills/stock-trend/tests/test_market_shadow_integration.py`。预期全部退出0；golden如涉及新增展示，逐项审核预期变化再更新，不更新任何意外数值变化。仅计划文档无需运行Python测试。

按P1.1→P1.2→P1.3→P2.1→P2.2→P2.3→P2.4顺序交付。P2.1与P2.2在契约确定后可独立实施，由主执行者合并并验证；避免两个执行者同时修改daily_candidates。

PR 1完成定义：53.4可解释、数据分类保守真实、完整原因可见、旧结果兼容。PR 2完成定义：五风格可计算或明确不可用、旁路可独立复算、正式结果等价、历史不受污染；真实成分输入缺失是明确的个股覆盖限制。

后续5/20/60日评价工具、交易成交假设、新权重和新放行门槛均不在这两个PR内。实验记录保留信号时间和证据，是后续评价输入，不代表已有收益验证。
