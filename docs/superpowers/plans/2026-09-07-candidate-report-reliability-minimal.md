# 候选报告可靠性最小实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户能辨认报告的扫描范围、成分可用程度和板块历史证据，避免将降级扫描或历史未匹配误读成完整收盘结论。

**Architecture:** 复用现有 performance 指标、ranking_meta 和 persistence_status，补齐扫描到 JSON/Markdown/HTML 的传递与展示。优先修复已确认的口径问题；保留来源隔离，不通过同名强行合并不同供应商的板块。

**Tech Stack:** Python、unittest、现有 HTML/Markdown 报告生成器；不新增依赖。

---

## 范围与交付边界

本期交付一个小版本，依次完成下列三个任务。只调整证据传递、历史资格判定及展示，不调整打分权重、扫描预算、买点模型或推荐门槛。

完整输入快照冻结与离线重放、跨供应商统一板块映射、自动逐股报告比较列入后续阶段。本期不承诺不同实时运行得到同一候选名单；承诺降级和不可比口径可见、未知历史不被解释为确定的冷淡表现。

现有 recommendation_snapshot 保存推荐结果及冲突记录，不等价于包含板块成分、K 线、资金等输入的重放快照；本期不修改其哈希或正式快照协议。

## 已核对的现状

- `scripts/scans/daily_candidates.py::_complete_performance` 已计算成分尝试覆盖率和可用覆盖率，但用 success 计数推导 unique 计数，需验证成功计数是否包含重试或缓存回退。
- Markdown 审计已显示两类成分覆盖率；HTML 审计仍突出单个 sector_scan_coverage，容易被误读。
- 页首已读取 sector_expanded_count；不能直接认定当前代码仍存在旧报告的 493/60 展示问题，应先写回归测试确认。
- `_persistence_observation` 对未匹配板块返回 None；`enrich_sector_context` 的 history_insufficient 却仅检查全局快照覆盖，不检查该板块的 sector_observed_days。
- AKShare 板块使用供应商隔离标识；历史无法匹配不能证明市场热点已经消失。

以下文件路径均相对于 `.claude/skills/stock-trend/`，执行命令则在仓库根目录运行。

## Task 1：统一覆盖率语义和三种输出

**Files:**
- Modify: `scripts/scans/daily_candidates.py`：`_complete_performance`、HTML/Markdown 审计、页首与漏斗。
- Test: `tests/test_daily_candidates.py`。
- 条件修改：`scripts/scans/stock_scanner.py`，仅当现有计数包含重试、无法表示唯一板块结果时，在成分获取结果汇总处补唯一 ID 计数；不改抓取策略。

- [ ] 新增 fixture：合格 57、尝试唯一板块 57、最终可用唯一板块 36、不可用 21。断言尝试覆盖 1.0、可用覆盖 round(36/57, 4)，扫描为 degraded。
- [ ] 新增 fixture：合格 493、实际展开 60；断言页首、漏斗、审计均显示实际展开 60，不从合格数回填；区分计划展开比例与实际尝试比例。
- [ ] 增加重复重试与缓存回退 fixture：同一板块只计一次尝试，最终返回非空有效成分计一次可用；缓存质量另外披露，不等同于实时。
- [ ] 增加合格板块数为零 fixture：分母为零显示“—”，不能显示 100% 或抛除零异常。
- [ ] 先运行定向测试记录失败；若页首已有测试通过，保留回归测试，不重复改写实现。
- [ ] 对已有字段补齐输出。统一展示“合格板块、实际尝试、成分可用、成分不可用、尝试覆盖率、成分可用覆盖率”；保留旧 JSON 字段兼容。百分比都由同一份冻结后的 performance 计算。
- [ ] 运行定向测试，确认 MD/HTML/JSON 数值一致。HTML 不再用无解释的“板块覆盖率”表示完整扫描。

验收样例：57 个合格板块全部尝试，36 个可用，应显示“尝试覆盖 100.0%；成分可用覆盖 63.2%；扫描降级”。该 fixture 不被宣称为旧报告全部计数的重新审计结果。

## Task 2：把未匹配历史显示为未知

**Files:**
- Modify: `scripts/scans/daily_candidates.py`：`enrich_sector_context`、`_sector_persistence_text`、相关候选诊断文案。
- Test: `tests/test_daily_candidates.py`。

- [ ] 构造三日完整且 good 的东方财富 BK 历史，当前板块使用不匹配的 THS ID；断言 sector_observed_days=0、不可执行、持续性状态为 history_unknown。
- [ ] 增加有效冷观察 fixture：同一 ID 在历史中明确存在但热度不足；确认可报告已知热点次数为零，不与历史未知混淆。
- [ ] 保留并运行现有缺失日期、完整快照覆盖、两日 emerging、三日 mainline 测试，防止有效历史分类回退。
- [ ] 在现有 persistence_status 增加 history_unknown：该板块没有有效观察时优先赋值；有观察但不足以验证时沿用 history_insufficient；有充分证据时沿用现有分类。
- [ ] 渲染 history_unknown 为“本板块历史未匹配，持续性未知”；热点出现/连续天数显示“—”。保留内部原始计数兼容，但不能在用户文案或诊断中将 unknown 解释为“单日脉冲”。所有资格消费者遇到 unknown 保持不可执行。
- [ ] 运行定向测试，检查主板块选择、观察池诊断及三种输出均携带状态。

最小实现仅新增一个状态并复用现有 None 观察逻辑，不引入跨来源模糊名称匹配。全局“快照覆盖 3 天”与“本板块观察 0 天”必须同时成立并被区分。

## Task 3：在报告首屏披露排行来源和口径

**Files:**
- Modify: `scripts/scans/daily_candidates.py`：板块排行获取后的 ranking_meta 汇总、JSON/Markdown/HTML 输出。
- Test: `tests/test_daily_candidates.py`。

- [ ] 添加 realtime/eastmoney、akshare/ths、cache、unknown 四组 fixture，包含零候选情形。即使没有候选行，也必须能看到本次排行来源。
- [ ] 在现有 performance 中新增 ranking_provenance 对象，从最终选用的 ranking_meta 填充 source、provider、data_date、quality、errors；不从 Top30 候选反推全局来源。
- [ ] JSON 保留对象原值，Markdown/HTML 首屏显示“排行供应商 / 获取方式 / 数据日期 / 质量”，缓存必须直接标注。未知日期显示“未知”，不得由报告生成时间代填。
- [ ] 固定提示：“不同供应商的板块范围可能不同，不能直接按生成时间判断准确性。”不声称已检测出与上一份报告发生切换，因为本期不新增历史比较机制。
- [ ] 运行测试，确认错误说明经过 HTML 转义，旧 fixture 缺字段时显示未知且不崩溃，正式推荐快照协议不变。

## 验证与交付

- [ ] 运行定向用例：`python3 .claude/skills/stock-trend/tests/test_daily_candidates.py`，预期全通过。
- [ ] 如修改 stock_scanner.py，同时运行：`python3 .claude/skills/stock-trend/tests/test_stock_scanner.py`。
- [ ] 运行仓库要求的质量门禁：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

- [ ] 检查 golden 输出差异，只接受经过确认的证据状态或文案变化；不自动重建快照消除失败。
- [ ] 使用离线 fixture 渲染一份验收 HTML 到临时目录，验证首屏来源、63.2% 可用率、未知历史三项同时可见；不覆盖用户两份原报告，不打开 GUI。
- [ ] 自审 diff：只含上述职责内改动；报告修改文件、测试结果和“实时重跑仍可能不同”的明确边界。提交由后续执行请求的授权范围决定。

## 下一阶段入口

本期通过后，再单独设计完整收盘输入快照：记录版本与代码参数，冻结实际消费的排行、成分、日线、资金、基本面和历史上下文；离线重放禁止联网，以相同输入得到相同候选、分数及顺序作为验收。结果文件复制或仅保存板块排行，不满足该验收标准。
