# Stock Trend 日常功能收敛与低频能力清理计划

## Goal

将 `stock-trend` 面向日常使用的能力收敛为四类：

1. “今日推荐”统一入口；
2. A 股、港股和 ETF 单标的趋势分析；
3. ETF 扫描与比较；
4. 持仓、预警和仓位管理。

先缩减技能路由和用户文档，再删除与保留功能没有生产调用关系的低频代码。每一步都必须保持“今日推荐”、单标的分析、ETF 扫描和仓位管理可用，并保留独立回滚点。

本计划不调整评分公式、候选门槛、维科夫规则、市场环境模型、ETF 排名或仓位算法，也不清理 `reports/` 和 `.cache/stock-trend/` 中的历史数据。

## Target surface

精简后的用户入口为：

| 用户意图 | 路由 | 实现 |
|---|---|---|
| 今日推荐、今天买什么、今日候选 | `/today-recommendation` | `scripts/bridge/run_today.py` |
| 股票/港股/ETF 单标的分析 | `/stock-trend` | `scripts/pipeline/runner.py` + 评分和报告链 |
| ETF 扫描、ETF 推荐、ETF 对比 | `/etf-scan` | `scripts/scans/etf_scanner.py` |
| 持仓、预警、仓位、凯利分析 | `/portfolio` | `scripts/portfolio/manager.py` |

`/candidates` 不再作为面向用户的独立路由，只作为“今日推荐”的内部候选阶段保留。`/daily-review` 不再作为主入口展示，但 `market_regime.py` 继续作为今日推荐和单标的市场背景的内部能力。

## Protected dependencies

以下文件即使其独立入口从文档中消失，也不能在本轮删除：

| 文件或模块 | 保留原因 |
|---|---|
| `scripts/scans/stock_scanner.py` | `daily_candidates.py` 直接导入扫描、K 线和打分能力 |
| `scripts/analysis/market_regime.py` | `run_today.py` 每次先刷新市场环境 |
| `scripts/analysis/market_style.py` | `daily_candidates.py` 当前存在顶层导入；影子功能需另行解耦后才能删除 |
| `scripts/analysis/sector_snapshot_job.py` | 为候选板块持续性积累正式收盘历史 |
| `scripts/bridge/sector_feeder.py` | `daily_candidates.py` 的板块输入回退仍会导入 |
| `scripts/analysis/evolution_job.py` 及推荐归因、诊断、实验模块 | 今日推荐后台 `close → weekly → monitor` 直接调用 |
| `scripts/backtesting/engine.py` | 生成 `backtest_stats.json`，供仓位管理校准凯利参数 |
| `scripts/backtesting/wyckoff_backtest.py` | 运行时虽不导入，但仍是今日推荐买点奖励的离线验证工具；第一轮仅隐藏 |
| `scripts/fetchers/longhubang.py` | 单标的资金分析仍可能读取龙虎榜数据 |
| `scripts/core/ths_utils.py` | `ddx.py`、`longhubang.py` 等保留模块共用 |

执行任何删除前，必须再次用 `rg` 验证这些依赖没有变化。

## Phase 0：冻结基线

### 0.1 记录当前范围

- [ ] 保存 `git status --short`，确认没有把用户现有改动纳入清理提交。
- [ ] 记录 `SKILL.md` 行数、当前用户入口和待删除文件行数。
- [ ] 用静态搜索重新确认候选删除模块没有被四个保留入口导入。

执行：

```bash
git status --short
wc -l .claude/skills/stock-trend/SKILL.md
rg -n --glob '*.py' 'lhb_tracker|ths_theme|run_integrated|integrated_report|market_leader|quality_gate|longhubang_agg|zt_replay' \
  .claude/skills/stock-trend/scripts
```

### 0.2 建立功能基线

- [ ] 今日推荐 dry-run 能返回计划且不联网、不写入。
- [ ] 单标的分析、ETF 扫描和仓位管理 CLI 的 `--help` 正常。
- [ ] 两个仓库质量门通过。

执行：

```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --dry-run --json
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --help
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py --help
python3 .claude/skills/stock-trend/scripts/portfolio/manager.py --help
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

退出条件：所有基线命令成功。若基线已有失败，先记录为既有问题；不得把该失败归因于后续清理，也不得通过更新 golden 快照掩盖。

## Phase 1：收缩技能路由和用户文档

这一阶段不删除 Python，先让 Agent 和用户只看到保留功能。

### 1.1 精简 `SKILL.md`

修改：

- `.claude/skills/stock-trend/SKILL.md`
- `.claude/skills/stock-trend/agents/openai.yaml`

执行内容：

- [ ] 将 frontmatter 描述收敛为今日推荐、股票/ETF 分析、ETF 扫描和持仓管理。
- [ ] 分支路由只保留 `/today-recommendation`、`/stock-trend`、`/etf-scan`、`/portfolio`。
- [ ] 删除 `/lhb-tracker`、`/ths-theme`、`/integrated-scan`、`/etf-backtest`、`/wyckoff-backtest`、`/longtou`、`/market-theme`、`/stock-scanner` 的用户入口章节。
- [ ] 将 `/candidates` 压缩为“今日推荐内部候选合同”，保留市场门控、数据质量、板块持续性、新闻影子和正式快照规则。
- [ ] 将 `/daily-review` 压缩为“内部市场环境合同”，保留今日推荐依赖的评分和数据资格规则。
- [ ] 将市场风格影子、推荐演进高级 CLI、人工发布和手工快照命令移入内部维护文档，不再占据主技能路由。
- [ ] 保留联网契约、实时/缓存标识、GUI 限制和免责声明。
- [ ] 修正失效的 `/Users/jing.li7/.../python3` 硬编码，改为“使用当前环境中满足 Python >=3.10 的解释器”；示例统一使用 `python3`。
- [ ] 将 `openai.yaml` 的短描述同步为四类保留能力。

目标：`SKILL.md` 控制在 300 行以内，且不丢失保留流程的决策合同。

### 1.2 重写使用指南

修改：

- `docs/usage-guide.md`

新增：

- `docs/stock-trend-internal-maintenance.md`

执行内容：

- [ ] 使用指南目录只保留今日推荐、单标的分析、ETF 扫描、仓位管理、数据源和免责声明。
- [ ] 在今日推荐章节说明候选扫描和市场复盘已经内置，无需分别调用。
- [ ] 内部维护文档收纳 `--status`、`--resume`、`--postprocess sync`、`sector_snapshot_job.py`、演进 CLI、回测和影子实验。
- [ ] 对低频热点/龙虎榜/龙头功能标记为已弃用，指向 Git 历史，不继续提供日常命令示例。
- [ ] 历史设计计划保留原样；它们记录已完成设计，不作为当前使用说明。

### 1.3 文档验收

执行：

```bash
rg -n '/lhb-tracker|/ths-theme|/integrated-scan|/longtou|/market-theme|/stock-scanner|/etf-backtest|/wyckoff-backtest' \
  .claude/skills/stock-trend/SKILL.md \
  .claude/skills/stock-trend/agents/openai.yaml \
  docs/usage-guide.md
rg -n '/today-recommendation|/stock-trend|/etf-scan|/portfolio' \
  .claude/skills/stock-trend/SKILL.md docs/usage-guide.md
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --dry-run --json
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

验收标准：第一条搜索无结果，第二条四个入口均有结果，所有测试通过，golden 没有非预期变化。

建议提交边界：

```text
docs(skill): focus stock trend on daily workflows
```

## Phase 2：验证精简入口

在删除代码前完成一次无网络验证和一次真实日常使用验证。

### 2.1 无网络验证

- [ ] “今日推荐”路由到 `run_today.py`，而不是独立 `/candidates`。
- [ ] 股票代码和 ETF 代码都路由到 `/stock-trend`。
- [ ] “ETF 推荐/扫描”路由到 `/etf-scan`。
- [ ] “查看持仓/仓位建议/预警”路由到 `/portfolio`。
- [ ] “龙虎榜跟踪/龙头扫描/同花顺热力”不再被描述为正式入口。

### 2.2 真实使用验证

使用正常日常请求完成以下四类调用，各至少一次：

- [ ] 今日推荐；
- [ ] 一只 A 股或港股的单标的分析；
- [ ] 一只 ETF 的单标的分析或一次 ETF 扫描；
- [ ] 一次持仓状态或预警检查。

检查每次输出：

- 使用了实时数据，或明确标记 `cached`、`degraded`、数据缺失；
- 没有自动打开浏览器；
- 今日推荐仍包含市场门控和三层候选结果；
- 仓位管理能读取 ETF 扫描，凯利统计缺失时能明确降级；
- 所有输出带免责声明。

退出条件：四类保留能力均成功，且不需要恢复已隐藏入口。若发现遗漏，只修正文档路由；此阶段仍不删除 Python。

## Phase 3：删除独立热点/龙虎榜/龙头代码簇

仅当 Phase 2 通过后执行。本阶段预计删除约 4200 行生产代码及其专项测试。

### 3.1 删除生产代码

删除：

- `.claude/skills/stock-trend/scripts/analysis/lhb_tracker.py`
- `.claude/skills/stock-trend/scripts/analysis/ths_theme.py`
- `.claude/skills/stock-trend/scripts/analysis/quality_gate.py`
- `.claude/skills/stock-trend/scripts/bridge/run_integrated.py`
- `.claude/skills/stock-trend/scripts/bridge/integrated_report.py`
- `.claude/skills/stock-trend/scripts/scans/market_leader.py`
- `.claude/skills/stock-trend/scripts/fetchers/longhubang_agg.py`
- `.claude/skills/stock-trend/scripts/fetchers/zt_replay.py`

不得删除：

- `scripts/fetchers/longhubang.py`
- `scripts/core/ths_utils.py`
- `scripts/bridge/sector_feeder.py`
- `config/sector_mapping.yaml`

### 3.2 删除专项测试

删除：

- `.claude/skills/stock-trend/tests/test_lhb_tracker.py`
- `.claude/skills/stock-trend/tests/test_ths_theme.py`
- `.claude/skills/stock-trend/tests/test_integrated_report.py`
- `.claude/skills/stock-trend/tests/test_longtou.py`
- `.claude/skills/stock-trend/tests/test_market_leader_integration.py`
- `.claude/skills/stock-trend/tests/test_longhubang_agg.py`
- `.claude/skills/stock-trend/tests/test_zt_replay.py`

若测试聚合器显式导入上述文件，同一提交中移除相应注册；不要删减其他测试。

### 3.3 处理当前规格和文档

- [ ] 将 `.claude/specs/ths-theme-longtou-integration.md` 移到 `docs/archive/`，文件顶部注明已于本次清理退役。
- [ ] 删除当前使用指南中的残留命令和失效链接。
- [ ] 不修改 `docs/superpowers/plans/` 中的历史实施计划。
- [ ] 不清理历史报告和缓存；避免把代码清理扩展成数据删除。

### 3.4 删除后静态检查

执行：

```bash
rg -n --hidden -g '!reports/**' -g '!.git/**' \
  'lhb_tracker|ths_theme|run_integrated|integrated_report|market_leader|quality_gate|longhubang_agg|zt_replay' \
  .claude/skills/stock-trend docs/usage-guide.md
rg -n --glob '*.py' \
  'from fetchers\.longhubang|from core\.ths_utils|from bridge\.sector_feeder' \
  .claude/skills/stock-trend/scripts
```

第一条只允许命中归档说明或明确的历史兼容注释；第二条必须证明受保护模块仍有合法调用。

### 3.5 删除后测试

删除 Python 文件属于 Python 范围变更，必须运行两个仓库质量门，并补充保留入口专项测试：

```bash
python3 .claude/skills/stock-trend/tests/test_run_today.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_etf_scanner.py
python3 .claude/skills/stock-trend/tests/test_portfolio.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

验收标准：所有测试通过；不得重新生成 golden 快照来消除失败。

建议提交边界：

```text
refactor(skill): remove retired market scanning workflows
```

## Phase 4：处理剩余低频能力

该阶段分为默认动作和可选动作。

### 4.1 默认动作：只隐藏，保留实现

继续保留：

- `analysis/market_theme.py`：独立市场主线工具；当前无生产导入，但板块历史逻辑仍可辅助排障。
- `backtesting/engine.py`：ETF 回测统计是仓位管理的可选校准来源。
- `backtesting/wyckoff_backtest.py`：用于验证今日推荐买点优先级。
- `analysis/market_style.py`：候选脚本当前顶层导入。
- `analysis/sector_snapshot_job.py`：积累板块持续性历史。

这些能力只记录在内部维护文档，不作为自然语言日常路由。

### 4.2 可选动作 A：删除独立市场主线工具

若确认不再需要单独生成市场主线报告：

- [ ] 再次确认生产代码没有导入 `analysis.market_theme`。
- [ ] 删除 `analysis/market_theme.py` 和 `tests/test_market_theme.py`。
- [ ] 从 `tests/test_daily_candidates.py` 中移除只测试该独立模块的用例；不得删除候选自己的板块持续性测试。
- [ ] 保留 `fetchers/sector_kline.py`，因为推荐归因仍使用它。
- [ ] 运行两个仓库质量门。

建议单独提交：

```text
refactor(skill): remove standalone market theme report
```

### 4.3 可选动作 B：缩减影子实验

只有在准备修改 `daily_candidates.py` 时执行：

- [ ] 将 `market_style.py` 顶层导入改为显式参数启用时的延迟导入。
- [ ] 删除 `--style-shadow`、`--memberships`、`--strategy-shadow` 及对应报告段和快照副本。
- [ ] 保留默认新闻影子层；它属于今日推荐当前流程。
- [ ] 证明正式排序、分桶、快照和 `workflow` JSON 不变。
- [ ] 更新对应测试后运行两个仓库质量门。

这属于独立重构，不和 Phase 3 的文件删除混在一个提交中。

### 4.4 暂不执行的删除

- 不删除 ETF 回测引擎，除非先决定仓位管理不再使用历史胜率校准，并为 Kelly 降级行为建立明确合同。
- 不删除维科夫回测，除非今日推荐的买点奖励改为不再要求离线证据验证。
- 不删除推荐演进链，除非同时从 `run_today.py` 移除后台后处理；这会改变“今日推荐”的产品行为，需要另立计划。

## Phase 5：最终验收与收尾

### 5.1 功能验收矩阵

| 能力 | 验收方式 | 预期 |
|---|---|---|
| 今日推荐 | `run_today.py --dry-run --json` + 一次真实运行 | 正确刷新市场、生成候选、返回 workflow |
| 股票分析 | A 股或港股代码运行管线 | 有技术、资金、基本面/适用降级和风险结论 |
| ETF 单标的分析 | ETF 代码运行管线 | 含 ETF 专属数据或明确降级 |
| ETF 扫描 | `etf_scanner.py` 专项测试或真实扫描 | 排名、排除项和数据状态完整 |
| 仓位管理 | `manager.py status` 或专项测试 | 持仓、预警、ETF 对比和 Kelly 降级正常 |

### 5.2 最终静态检查

```bash
rg -n '/today-recommendation|/stock-trend|/etf-scan|/portfolio' \
  .claude/skills/stock-trend/SKILL.md docs/usage-guide.md
rg -n '/lhb-tracker|/ths-theme|/integrated-scan|/longtou|/market-theme|/stock-scanner|/etf-backtest|/wyckoff-backtest' \
  .claude/skills/stock-trend/SKILL.md docs/usage-guide.md
git diff --check
git status --short
```

预期：保留入口存在；退役入口在主技能和用户指南中不存在；diff 无空白错误；工作区只包含本计划范围内的变更。

### 5.3 最终质量门

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

只有在两个质量门通过、四个保留入口验收完成后，清理才算完成。

## Rollback strategy

每个 Phase 使用独立提交：

1. Phase 1 只改路由和文档，可单独回退；
2. Phase 3 删除独立代码簇，可从上一提交恢复；
3. Phase 4 的每个可选重构各用一个提交。

如果 Phase 3 后任一保留能力失败，立即回退 Phase 3 的删除提交，保留 Phase 1 的入口收缩，再重新分析隐藏依赖。不要通过放宽测试、删除断言或更新 golden 快照绕过失败。

## Definition of done

- [ ] `SKILL.md` 不超过 300 行，只暴露四类日常能力。
- [ ] `docs/usage-guide.md` 与技能路由一致。
- [ ] 内部维护命令有独立文档，不污染日常入口。
- [ ] 热点/龙虎榜/龙头代码簇及专项测试已经删除，或明确记录未删除原因。
- [ ] 今日推荐、股票/ETF 单标的分析、ETF 扫描和仓位管理全部通过验收。
- [ ] 两个仓库质量门通过，golden 无非预期变化。
- [ ] 没有修改或删除 `reports/` 与 `.cache/stock-trend/` 中的用户数据。

所有分析和输出继续附带：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
