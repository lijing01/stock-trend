# YAML 观察列表接入今日推荐六维分析

## 需求摘要

将每日复盘 HTML 底部的“观察列表”恢复为手工维护的
`.claude/skills/stock-trend/data/observation_list.yaml` 所列标的，并让每一只标的
使用“今日推荐”候选股的六维评分口径分析：`momentum`、`volume_price`、`capital`、
`fundamental`、`sector_strength`、`wyckoff`。它不是候选扫描结果中的
`buckets.observation`，也不改变正式推荐、等待触发或候选扫描的资格、排序和快照。

已确认的现状：指定的复盘页实际读取同依据日的
`.cache/stock-trend/observation_pools/<data_date>.json`，该文件仅冻结候选扫描的
`observation` 分桶；`market_regime.load_observation_list()` 已能读取 YAML，然而没有
调用方。

## 验收标准

1. 对一次依据日为 `D` 的今日复盘，最终 HTML 的“观察列表”行数、顺序和 `code` 与
   YAML 的 `observation_list` 完全一致；不得出现仅因其位于候选扫描观察池而加入的标的。
2. 每个 YAML 标的都有六维原始分、综合分、数据质量/缺失原因、分析依据日和配置中的
   `date`、`entry_phase`；六维权重与
   `stock_scanner.composite_from_dimensions()` 的含 `wyckoff` 分支一致。
3. YAML 标的即使不满足维科夫买点、市场门槛或正式推荐门槛，也必须保留为观察行，并标
   明其结构/数据/资格原因；绝不能被提升为“今日可执行”或写入推荐历史。
4. YAML 缺失、格式错误、重复/无效代码、无证券元数据、行业映射缺失、数据源失败或数据
   日期不覆盖 `D` 时，报告给出逐项或区块级降级诊断；不回退到候选扫描的观察池，也不把
   过期缓存标为当日分析。
5. `run_today` 仍先创建原路径的 pending 复盘页；异步更新仅替换标记区块，失败不影响候
   选 JSON/HTML、正式推荐快照、后台研究或原复盘其他内容。独立 `/daily-review` 也能生成
   同一语义的 YAML 分析区块。
6. 指定历史页 `reports/lists/daily-review-20260922-001728.html` 只在支持按其
   `data_date` 重放后才重建该区块；重放必须使用该页的依据日，而不能用运行日行情覆盖历史
   内容。

## 实施步骤

1. **定义独立的数据契约和缓存位置。**
   在 `scripts/analysis/` 新增一个受限的 YAML 观察分析协调器（建议
   `observation_list_analysis.py`），以现有 YAML loader 的校验语义读取配置、保留 YAML
   顺序，并将结果冻结到
   `.cache/stock-trend/observation_analyses/<data_date>.json`。定义新的 schema（例如
   `yaml-observation-analysis/v1`），包含 `data_date`、生成时间、配置文件/内容摘要、每个
   条目的输入字段、六维结果、质量证据和明确状态。不要复用
   `candidate-observation-pool/v1`，以免把“手工观察对象”和“候选扫描观察池”重新混淆。

2. **复用今日推荐的六维计算，不复用其入选漏斗。**
   在 `scripts/scans/stock_scanner.py` 将当前 `run_phase2()` 中“获取 K 线 → 维科夫分析 →
   资金/基本面增强 → 六维评分 → 数据质量”拆出或参数化为可复用的、显式的观察分析模式。
   默认路径必须继续保持现有 `require_wyckoff_gate=True` 的候选行为；观察模式关闭“维科夫
   买点过滤”，使非买点标的仍得到六维/诊断结果，但绝不进入候选选择或推荐分桶。使用同一
   `composite_from_dimensions()`、`assess_candidate_data()`、行业成员质量及数据日期契约；
   不复制或另写一套权重。

3. **为 YAML 代码建立可审计的候选输入。**
   协调器为每个代码解析证券代码、名称与实际板块成员关系，并从与市场上下文一致的板块
   排行/持续性证据构造 `stock_scanner` 所需的 candidate/membership 输入。行业/概念映射需
   保留来源、已知日期和质量；不能可靠映射时，将 `sector_strength` 与综合分标记为不可
   用或降级，并在行级原因中说明，不能静默赋予“热点板块”资格。按 `data_date` 传递
   `as_of_date` 和资金预期日期，避免使用当前日期替代历史复盘依据日。

4. **单独渲染 YAML 六维观察区块。**
   在 `market_regime.py` 新增 YAML 分析结果的加载、schema/日期/配置摘要校验和 HTML
   renderer；将 `render_observation_list_html()` 与
   `update_observation_list_html()` 的数据源切换为新 artifact。表格明确列出：观察对象
   （名称/代码）、加入日期/加入时阶段、六维分、综合/质量调整分、维科夫结构、数据质量和
   观察原因，并标注“仅供学习参考、非正式推荐”。移除候选观察池的十列表头、候选报告链接、
   新闻影子分与“超出当日推荐数量上限”等候选专属文案。保留稳定 HTML 标记和原子替换，因
   此其他复盘内容和原链接不变。

5. **改造触发与后台状态，而不耦合推荐结果。**
   更新 `bridge/run_today.py` 和 `bridge/today_background.py`：市场刷新后的初版仍显示
   pending；后台任务以本次报告的 `data_date`、YAML 路径/摘要和分析 artifact 路径执行
   YAML 六维分析后替换区块。该任务不读取 `daily_candidates` 的 `observation_pool_artifact`，
   也不等待或改变候选结果。为独立 `market_regime.py` 调用增加受控的同步或同一任务入口，
   确保它不会展示旧 artifact；缺数据时显示“本依据日 YAML 观察分析未生成”的明确状态。
   删除或迁移已经无消费者的 `load_observation_pool()` / 候选观察池交接代码前，先确认没有
   其他消费者；若仍要保留候选池用于研究，改名并隔离，不能再被 daily-review 使用。

6. **测试、文档和受控历史复盘。**
   扩展 `tests/test_market_regime.py` 覆盖 YAML 顺序、HTML 转义、新 schema 同日期验证、空/坏
   YAML、artifact 缺失及原子替换；扩展 `tests/test_stock_scanner.py` 覆盖观察模式保留非买点
   标的、六维/权重一致、默认候选漏斗不变；扩展 `tests/test_run_today.py` 覆盖 pending →
   YAML 分析完成、后台失败隔离、绝不消费候选观察池。更新
   `.claude/skills/stock-trend/SKILL.md` 的 `/daily-review` 与 `/today-recommendation` 合同，
   明确 YAML 是该区块唯一标的来源。最后以固定 fixture 和历史 `data_date` 重放来验证指定
   HTML；只有逐项验证后才替换该历史文件的观察区块。

## 验证步骤

1. 用隔离的临时 cache、固定 YAML 和 mock 数据运行观察分析，断言 artifact 的 schema、
   `data_date`、四个 YAML 代码顺序、六维字段、权重结果及每条数据证据。
2. 对同一 fixture 运行候选扫描，断言其 `buckets.observation` 与 YAML 结果可不同，且
   daily-review HTML 只出现 YAML 集合。
3. 运行 `run_today` 集成测试，断言初版链接可用、替换后的路径不变、替换前后区块外内容
   不变，以及分析失败不改变候选输出/正式快照。
4. 执行仓库质量门（使用 Python 3.10）：

   ```bash
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_stock_trend.py
   /Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/tests/test_golden.py --diff
   ```

   不因预期外输出而重生成 golden；若 HTML 快照变化是批准行为，先人工审阅并记录原因。

## 风险与缓解

- **口径漂移：** 单股 `scores.py` 的传统五维/宏观口径不同于今日推荐六维。协调器必须调用
  `stock_scanner` 的六维逻辑，不能用 `pipeline/runner.py + scores.py` 替代。
- **维科夫门导致漏行：** 候选模式的 gate 会过滤非买点；新增观察模式并以测试锁定默认值，
  防止影响正式选股。
- **行业维度证据不足：** 手工代码未必属于当前热点板块；保留该标的和证据状态，不将未知板
  块伪装成中性/热点。
- **历史数据污染：** artifact 必须绑定 `data_date` 和配置摘要；历史报告只允许按该日期重放，
  不得取当日实时行情回填。
- **后台开销或失败：** YAML 列表规模小但仍需设定与现有源健康机制一致的请求上限/超时；失败
  降级为可见状态，不阻断报告或正式推荐。

## 停止条件

上述测试与两项必需质量门通过，复盘区块的来源、六维口径和失败状态均可从 artifact 与 HTML
验证，且候选扫描的观察池不再参与 daily-review 渲染。
