# Stock Trend 工程结构优化实施计划

## Goal

在不改变推荐公式、市场门槛、数据源选择、报告语义和四个正式用户入口的前提下，修复当前工程的验证盲区，建立稳定的 Python 包边界，拆分候选扫描巨型模块，并使依赖、运行状态和文档结构可以持续维护。

完成后的工程应满足：

1. 任意 `scripts/**/*.py` 修改都会经过语法、完整单元测试、综合测试和 Golden diff；
2. 生产模块和测试不再依赖散落的 `sys.path.insert()`；
3. 领域规则不依赖 CLI、报告、在线扫描或回测编排；
4. `daily_candidates.py`、`stock_scanner.py` 等入口只负责参数解析和工作流编排；
5. 用户持仓、缓存和报告均不作为版本化源码；
6. 当前架构文档与仓库实际结构一致。

## Protected behavior

本计划必须持续保护以下入口和行为：

| 用户意图 | 稳定入口 |
|---|---|
| 今日推荐 | `scripts/bridge/run_today.py` |
| 股票、港股、ETF 单标的分析 | `scripts/pipeline/runner.py` |
| ETF 扫描 | `scripts/scans/etf_scanner.py` |
| 持仓管理 | `scripts/portfolio/manager.py` |

以下内容不属于本次结构优化范围：

- 不调整评分权重、候选门槛、维科夫判断和风险收益参数；
- 不切换或新增行情供应商；
- 不改变正式推荐快照和实验发布协议；
- 不重新生成 Golden 快照来消除失败；
- 不读取、移动或清理 `reports/`；
- 不清理 `.cache/stock-trend/` 中的历史运行数据；
- 不在结构重构 PR 中混入策略或展示改版。

## Target architecture

保留现有脚本路径作为兼容入口，在技能目录内建立可导入的 `stock_trend` 包：

```text
.claude/skills/stock-trend/
├── SKILL.md
├── pyproject.toml
├── stock_trend/
│   ├── domain/                  # 纯数据合同、规则、评分和资格判断
│   ├── analytics/               # 技术、维科夫、市场环境等纯分析
│   ├── providers/               # 东方财富、同花顺、AkShare 等外部数据适配器
│   ├── services/                # 扫描、推荐、持仓和回测用例编排
│   ├── storage/                 # 缓存、快照、研究事件和原子写入
│   └── reporting/               # Markdown、HTML、图表上下文和渲染
├── scripts/                     # 向后兼容的薄 CLI 包装层
│   ├── bridge/run_today.py
│   ├── pipeline/runner.py
│   ├── scans/daily_candidates.py
│   ├── scans/stock_scanner.py
│   ├── scans/etf_scanner.py
│   └── portfolio/manager.py
└── tests/
    ├── unit/
    ├── integration/
    ├── architecture/
    └── golden/
```

允许的依赖方向：

```text
domain
  ↑
analytics / providers / storage
  ↑
services
  ↑
scripts / reporting / background jobs
```

额外规则：

- `domain` 不得导入网络、文件系统、CLI、报告、扫描器或回测模块；
- `analytics` 不得导入 `scripts`、`reporting` 或在线扫描工作流；
- `backtesting` 复用领域规则和评分 API，不导入 CLI 扫描器；
- `reporting` 只消费冻结结果，不主动获取行情；
- subprocess 只用于需要进程隔离或保持稳定 CLI 协议的边界；模块内部优先调用显式 API；
- 迁移期间不允许新代码继续增加 `sys.path.insert()`。

## Phase 0：冻结基线和变更边界

### Task 0.1：记录基线

- [ ] 检查工作区，只将本计划相关变更纳入后续提交。
- [ ] 记录 Python 文件数、测试文件数、主要巨型模块行数和现有 `sys.path` 注入点。
- [ ] 记录四个稳定入口的 `--help` 或 `--dry-run` 输出。
- [ ] 运行当前仓库要求的两个质量门并保存结果；既有失败单独记录，不归因于后续重构。
- [ ] 尝试运行完整测试发现，列出当前未被正式质量门覆盖或无法发现的测试。

执行：

```bash
git status --short
find .claude/skills/stock-trend/scripts -name '*.py' -not -path '*/__pycache__/*' -exec wc -l {} + | sort -nr | head -30
find .claude/skills/stock-trend/tests -maxdepth 1 -name 'test_*.py' | sort
rg -n 'sys\.path\.insert' .claude/skills/stock-trend/scripts .claude/skills/stock-trend/tests -g '*.py'
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --dry-run --json
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --help
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py --help
python3 .claude/skills/stock-trend/scripts/portfolio/manager.py --help
python3 -m unittest discover -s .claude/skills/stock-trend/tests -p 'test_*.py'
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

退出条件：基线和既有失败已记录，后续 PR 有可比较的测试结果。Phase 0 只生成记录，不修改 Golden。

## Phase 1：修复工程安全网

建议提交：`build(test): align quality gates with current package layout`

### Task 1.1：修复 Git hook 的递归匹配

**Modify:**

- `.githooks/pre-commit`

**实施：**

- [ ] 将 Python 暂存文件匹配从 `scripts/[^/]+\.py` 改为覆盖 `scripts/**/*.py`。
- [ ] 使用当前模块路径替换 `analyze_technical.py`、`compute_scores.py`、`generate_report.py` 等旧引用。
- [ ] 模板一致性检查指向 `scripts/reporting/report.py`。
- [ ] 评分 schema 检查指向 `analysis.scores` 的当前公共函数；若不存在稳定公共函数，删除这项脆弱的文本检查，以专项测试替代。
- [ ] 当应检查的模块不存在时直接失败，不再静默跳过。
- [ ] 保留 `STOCK_TREND_SKIP_GOLDEN=1` 仅用于已有文档记录的外部阻塞。

### Task 1.2：建立统一检查入口

**Add:**

- `Makefile`

**Modify:**

- `AGENTS.md`
- `CLAUDE.md`
- `.githooks/pre-commit`

**实施：**

- [ ] 增加 `make test-unit`：发现并运行全部 `test_*.py`。
- [ ] 增加 `make test-integration`：运行 `test_stock_trend.py`。
- [ ] 增加 `make test-golden`：运行 `test_golden.py --diff`。
- [ ] 增加 `make check`：依次运行语法检查、完整单元测试、综合测试、Golden diff 和 `git diff --check`。
- [ ] Git hook 调用同一组底层命令，避免 hook 与文档再次分叉。
- [ ] 更新仓库文档，把 `make check` 作为首选入口，同时保留两个强制质量门的明确命令。

### Task 1.3：验证 hook 对真实路径生效

- [ ] 用临时暂存或独立测试 fixture 验证子目录 Python 文件会进入语法检查。
- [ ] 验证不存在的关键模块会使 hook 失败。
- [ ] 验证无 Python 变更时不会无故运行昂贵检查。
- [ ] 验证 Python 变更时 Golden diff 必定执行。

验收：

```bash
bash -n .githooks/pre-commit
make check
```

退出条件：40 个现有测试文件都有明确执行路径；hook 不再引用重构前文件名；不得减少既有测试覆盖。

## Phase 2：依赖、状态和包基础设施

建议拆为两个提交：

```text
build(python): declare stock trend runtime and test dependencies
refactor(config): separate user state from versioned defaults
```

### Task 2.1：增加 `pyproject.toml`

**Add:**

- `.claude/skills/stock-trend/pyproject.toml`

**实施：**

- [ ] 声明 `requires-python = ">=3.10"`。
- [ ] 从真实 import 清单区分基础依赖和可选供应商依赖。
- [ ] 基础依赖至少核对 `pandas`、`numpy`、`PyYAML`、`requests`。
- [ ] 将 `akshare`、`tushare`、`baostock`、`yfinance` 按供应商 extras 分组，避免安装一个可选数据源才能运行全部离线测试。
- [ ] 集中配置测试发现、编码风格和静态检查排除项。
- [ ] 不凭当前机器环境生成无依据的精确版本锁；先声明已验证的兼容范围，再通过独立任务生成可复现锁文件。

### Task 2.2：集中路径和运行配置

**Add:**

- `.claude/skills/stock-trend/stock_trend/__init__.py`
- `.claude/skills/stock-trend/stock_trend/config.py`
- `.claude/skills/stock-trend/tests/test_runtime_config.py`

**Modify first consumers:**

- `scripts/core/cache_utils.py`
- `scripts/portfolio/manager.py`
- `scripts/analysis/market_regime.py`
- `scripts/scans/etf_scanner.py`

**实施：**

- [ ] 在 `config.py` 唯一定义 `PROJECT_ROOT`、`SKILL_ROOT`、`CACHE_ROOT`、`REPORT_ROOT`、`PORTFOLIO_PATH` 和 `WATCHLIST_PATH`。
- [ ] 保留现有环境变量覆盖行为，路径解析不得依赖当前工作目录。
- [ ] 给环境变量覆盖、默认根目录和临时目录写离线测试。
- [ ] 分批替换各模块重复的 `Path(__file__).parents[...]` 计算，每批不混入业务逻辑变化。

### Task 2.3：将真实持仓移出版本控制

**Modify:**

- `.gitignore`
- `.claude/skills/stock-trend/data/portfolio.example.yaml`
- `docs/usage-guide.md`

**Repository state:**

- `.claude/skills/stock-trend/data/portfolio.yaml`

**实施：**

- [ ] 先确认 `portfolio.example.yaml` 不含真实持仓，并覆盖所有必需字段。
- [ ] 将 `portfolio.yaml` 加入 `.gitignore`。
- [ ] 从 Git 索引移除 `portfolio.yaml`，保留用户工作区文件，不删除本地持仓。
- [ ] 首次运行缺少真实文件时，从示例结构初始化空组合，不能自动写入示例持仓。
- [ ] 增加缺失文件、环境变量覆盖和并发写入的测试。

验收：

```bash
python3 .claude/skills/stock-trend/tests/test_runtime_config.py
python3 .claude/skills/stock-trend/tests/test_portfolio.py
python3 .claude/skills/stock-trend/tests/test_market_regime.py
python3 .claude/skills/stock-trend/tests/test_etf_scanner.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

退出条件：路径来源唯一；真实持仓留在用户工作区但不再被 Git 跟踪；稳定入口和输出不变。

## Phase 3：建立领域边界并消除依赖环

建议提交：`refactor(core): establish one-way domain dependencies`

### Task 3.1：增加架构测试

**Add:**

- `.claude/skills/stock-trend/tests/test_architecture.py`

**实施：**

- [ ] 用 AST 扫描生产模块 import，不依赖运行时导入成功。
- [ ] 禁止 `stock_trend.domain` 导入 providers、services、reporting、scripts 或 backtesting。
- [ ] 禁止 `stock_trend.analytics` 导入 CLI 和 reporting。
- [ ] 禁止 backtesting 导入 `scripts.scans.*`。
- [ ] 禁止新增 `sys.path.insert()`；迁移前已有位置以明确 allowlist 管理，allowlist 只能减少。
- [ ] 对同名模块建立检查或显式允许清单，防止继续出现含义模糊的模块名。

### Task 3.2：抽取候选领域合同

**Move or extract from:**

- `scripts/core/candidate_trade_plan.py`
- `scripts/core/candidate_score_ledger.py`
- `scripts/core/recommendation_quality.py`
- `scripts/scans/daily_candidates.py`

**Add under:**

- `stock_trend/domain/candidates/`

**Target modules:**

- `contracts.py`：候选、数据质量、交易计划和推荐分桶合同；
- `qualification.py`：数据资格和正式推荐门控；
- `ranking.py`：稳定排序键和分数账本；
- `trade_plan.py`：风险有界交易计划；
- `policy.py`：市场分数对应的推荐数量和状态规则。

**实施：**

- [ ] 先用 characterization tests 冻结当前输入输出，再移动纯函数。
- [ ] 将 `candidate_trade_plan.py → analysis.technical` 的反向依赖改为参数化指标输入，或把共同纯计算移到 `analytics.technical`。
- [ ] 不在抽取过程中调整常量值或默认门槛。
- [ ] 旧 import 路径提供一个版本周期的兼容 re-export，并标注删除条件。

### Task 3.3：解耦回测和在线扫描

**Modify:**

- `scripts/backtesting/engine.py`
- `scripts/backtesting/wyckoff_backtest.py`
- `scripts/backtesting/recommendation_experiments.py`
- `scripts/analysis/evolution_job.py`

**实施：**

- [ ] 把 ETF 评分、候选分类、代码解析等可复用纯逻辑放入 domain/analytics API。
- [ ] 回测模块导入纯 API，不再导入 `scans.etf_scanner`、`scans.stock_scanner` 或 `scans.daily_candidates`。
- [ ] 将 evolution 工作流放入 service 层；实验计算保持无在线取数副作用。
- [ ] 用相同冻结 fixture 比较迁移前后评分、分桶和实验结果完全一致。

验收：

```bash
python3 .claude/skills/stock-trend/tests/test_architecture.py
python3 .claude/skills/stock-trend/tests/test_candidate_trade_plan.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_backtest.py
python3 .claude/skills/stock-trend/tests/test_wyckoff_backtest.py
python3 .claude/skills/stock-trend/tests/test_recommendation_experiments.py
python3 .claude/skills/stock-trend/tests/test_evolution_job.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

退出条件：架构测试通过；`core/domain` 不再向上依赖；回测不再导入在线扫描器；冻结结果无变化。

## Phase 4：拆分每日候选工作流

建议分成三个小提交，每次只迁移一个业务阶段：

```text
refactor(candidates): extract sector selection service
refactor(candidates): extract enrichment and qualification services
refactor(candidates): isolate snapshots and rendering
```

### Task 4.1：拆板块选择和成分获取

**Extract from:**

- `scripts/scans/daily_candidates.py`
- `scripts/scans/stock_scanner.py`
- `scripts/bridge/sector_feeder.py`

**Add:**

- `stock_trend/services/candidates/sector_selection.py`
- `stock_trend/services/candidates/membership.py`

- [ ] 冻结自动板块、手动板块、缓存降级、供应商切换和盘后固定范围 fixture。
- [ ] 抽取 `pick_hot_sectors`、持续性判定和 membership 合并逻辑。
- [ ] 以显式输入传入交易日、截止时间、provider 和缓存，不读取全局状态。
- [ ] 保留当前 performance 和 provenance 字段。

### Task 4.2：拆扫描、增强和资格判断

**Add:**

- `stock_trend/services/candidates/collection.py`
- `stock_trend/services/candidates/enrichment.py`
- `stock_trend/services/candidates/workflow.py`

- [ ] 把候选收集、K 线计算、资金/基本面增强拆成独立步骤。
- [ ] 将预算、deadline、并发数和 provider 次数放入不可变运行配置对象。
- [ ] 保持 source health、重试、缓存探测和 live enrichment 的计数语义。
- [ ] 将当前约 947 行的 `run_phase2` 缩为编排函数；每个子步骤返回结构化结果和诊断，不通过共享 dict 隐式传递错误。
- [ ] 保留进程隔离确有必要的数据源调用；记录保留理由和超时所有者。

### Task 4.3：拆快照和报告渲染

**Add:**

- `stock_trend/services/candidates/finalization.py`
- `stock_trend/reporting/candidates/context.py`
- `stock_trend/reporting/candidates/markdown.py`
- `stock_trend/reporting/candidates/html.py`

- [ ] 将正式快照、冲突记录、研究快照、影子结果分别封装为端口。
- [ ] 构建一次冻结的 report context，JSON、Markdown、HTML 只消费该 context。
- [ ] 渲染层不得修改候选分数、分桶、资格或持久化状态。
- [ ] 保留现有报告路径和文件命名规则。

### Task 4.4：收缩 CLI

**Modify:**

- `scripts/scans/daily_candidates.py`
- `scripts/bridge/run_today.py`

- [ ] `daily_candidates.py` 只保留参数解析、服务调用、结果输出和退出码映射。
- [ ] `run_today.py` 继续保持“先刷新市场环境，再扫描候选，再启动后台任务”的稳定协议。
- [ ] `_run_script` 的 JSON 提取和错误映射写专项契约测试，不在本阶段顺便改协议。

验收：

```bash
python3 .claude/skills/stock-trend/tests/test_sector_feeder.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_daily_candidates.py
python3 .claude/skills/stock-trend/tests/test_run_today.py
python3 .claude/skills/stock-trend/tests/test_candidate_research_snapshot.py
python3 .claude/skills/stock-trend/tests/test_recommendation_snapshot.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

退出条件：离线 fixture 的候选集合、排序、分桶、正式快照哈希和三种报告数据一致；`daily_candidates.py` 成为薄入口；无新增网络调用。

## Phase 5：拆分单标的、ETF 和报告链

### Task 5.1：拆技术计算与 CLI IO

**Refactor:**

- `scripts/analysis/technical.py`
- `scripts/analysis/scores.py`
- `scripts/analysis/wyckoff.py`

**Add under:**

- `stock_trend/analytics/technical/`
- `stock_trend/domain/scoring/`

- [ ] 将 pandas 输入标准化、指标计算、信号生成、风险收益计算和序列化分开。
- [ ] `main()` 只处理 argparse、文件 IO 和结构化输出。
- [ ] 对每个公共计算函数保留数值回归 fixture，明确浮点容差。
- [ ] 不改变指标窗口、缺失值处理和评分权重。

### Task 5.2：拆 ETF 扫描器

**Refactor:**

- `scripts/scans/etf_scanner.py`

**Add:**

- `stock_trend/domain/etf/scoring.py`
- `stock_trend/services/etf_scan.py`
- `stock_trend/reporting/etf_scan.py`

- [ ] 抽取 ETF 评分、矛盾检测、趋势阶段和交易计划纯函数。
- [ ] 服务层负责 watchlist、数据获取、并发和降级。
- [ ] 报告层只负责 compact、JSON 和 HTML 输出。
- [ ] 回测与在线 ETF 扫描共用 domain API。

### Task 5.3：拆单标的报告上下文

**Refactor:**

- `scripts/reporting/report.py`
- `scripts/reporting/chart.py`
- `scripts/pipeline/runner.py`

**Add:**

- `stock_trend/reporting/stock/context.py`
- `stock_trend/reporting/stock/markdown.py`
- `stock_trend/reporting/stock/html.py`
- `stock_trend/services/stock_analysis.py`

- [ ] 将约 506 行的 `build_context` 按行情、技术、资金、基本面、ETF 专项和风险章节拆分。
- [ ] pipeline 使用结构化 step result，避免通过 stdout/stderr 文本推断内部成功状态。
- [ ] 继续保留顶层 CLI 的 JSON、退出码和失败隔离协议。
- [ ] 对 A 股、港股和 ETF 三类 Golden fixture 分别验收。

验收：

```bash
python3 .claude/skills/stock-trend/tests/test_wyckoff.py
python3 .claude/skills/stock-trend/tests/test_scores_wyckoff_mode.py
python3 .claude/skills/stock-trend/tests/test_etf_scanner.py
python3 .claude/skills/stock-trend/tests/test_backtest.py
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
git diff --check
```

退出条件：三个稳定分析入口输出兼容；Golden 无非预期数值或语义变化；回测和在线分析使用同一纯规则实现。

## Phase 6：移除迁移兼容层和重复抽象

建议提交：`refactor(package): remove legacy import shims`

### Task 6.1：移除 `sys.path` 注入

- [ ] 将生产和测试 import 统一为 `stock_trend.*`。
- [ ] 删除兼容期 allowlist 中已经迁移的位置。
- [ ] 最终 `rg 'sys\.path\.insert'` 对生产代码无结果；若 CLI 包装因直接文件执行必须保留一个 bootstrap，集中到一个共享入口并写明原因。
- [ ] 子包全部具有明确的 `__init__.py`，但不通过 `__init__.py` 大量 re-export 私有实现。

### Task 6.2：处理重复和闲置模块

- [ ] `core/candidate_news.py` 与 `fetchers/candidate_news.py` 分别更名为 policy/provider，或合并重复职责。
- [ ] 两个 `market_explanation.py` 分别更名为 model/renderer，避免同名歧义。
- [ ] 检查 `BaseFetcher` 的实际使用：若至少三个 fetcher 可在保持 CLI 和缓存协议的前提下复用，则正式落地；否则删除未使用抽象。
- [ ] 将重复 `_run_script` 包装统一为一个进程边界适配器；不同错误协议不得强行合并。
- [ ] 删除已无调用者的兼容 re-export，并用 `rg` 和架构测试确认。

验收：

```bash
rg -n 'sys\.path\.insert' .claude/skills/stock-trend/stock_trend .claude/skills/stock-trend/scripts .claude/skills/stock-trend/tests -g '*.py'
rg -n 'from (core|analysis|scans|fetchers|reporting|backtesting)\.' .claude/skills/stock-trend -g '*.py'
python3 .claude/skills/stock-trend/tests/test_architecture.py
make check
```

退出条件：旧顶层 import 无生产调用者；同名模块已消歧；闲置抽象已处理；所有兼容入口仍可运行。

## Phase 7：更新当前文档并归档历史计划

建议提交：`docs(architecture): document current stock trend boundaries`

### Task 7.1：重写当前架构说明

**Modify:**

- `CLAUDE.md`
- `AGENTS.md`
- `docs/stock-trend-internal-maintenance.md`
- `docs/scripts-refactoring-plan.md`

**Add:**

- `docs/architecture.md`

- [ ] 用实际目录和入口替换重构前的平铺脚本说明。
- [ ] 删除不存在的 `references/`、旧脚本名和退役路由。
- [ ] 记录模块职责、允许依赖方向、运行状态目录和质量门。
- [ ] 将旧 `scripts-refactoring-plan.md` 标记为已被本计划取代，或移入 `docs/archive/`。
- [ ] 文档中的命令逐条执行验证，不能保留无法运行的示例。

### Task 7.2：整理计划文档

- [ ] 保留仍在执行的计划于 `docs/superpowers/plans/`。
- [ ] 将已完成且不再用于当前操作的计划移入按年份组织的 archive 索引。
- [ ] 增加 `docs/superpowers/plans/README.md`，标明 active、completed、superseded 状态。
- [ ] 不改写历史结论；通过索引说明其状态和替代文档。

验收：

```bash
rg -n 'run_pipeline\.py|generate_report\.py|compute_scores\.py|analyze_technical\.py|references/' CLAUDE.md AGENTS.md docs/architecture.md docs/stock-trend-internal-maintenance.md
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --dry-run --json
python3 .claude/skills/stock-trend/scripts/pipeline/runner.py --help
python3 .claude/skills/stock-trend/scripts/scans/etf_scanner.py --help
python3 .claude/skills/stock-trend/scripts/portfolio/manager.py --help
git diff --check
```

退出条件：当前文档只描述真实存在的入口和结构；历史计划状态可追溯；文档整理不改生产行为。

## 每个 Python PR 的固定交付清单

凡修改 `.claude/skills/stock-trend/scripts/` 或新 `stock_trend/` 包的 Python 代码，必须逐项完成：

- [ ] 开始前记录 `git status --short`，不覆盖用户已有修改；
- [ ] 先写或固定 characterization test，再移动实现；
- [ ] 运行本任务列出的定向测试；
- [ ] 运行 `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`；
- [ ] 运行 `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`；
- [ ] 运行 `git diff --check`；
- [ ] 检查没有意外读取或提交 `reports/`、`.cache/stock-trend/`、真实持仓和凭据；
- [ ] 对比稳定 CLI 的参数、退出码、JSON schema 和默认输出路径；
- [ ] Golden 出现差异时解释每项业务含义，不通过 regenerate 掩盖回归；
- [ ] PR 描述写明迁移了什么、保留了什么协议、测试结果和剩余兼容层。

## PR 顺序和停止条件

| PR | 内容 | 依赖 | 主要停止条件 |
|---|---|---|---|
| 1 | Git hook 与统一测试入口 | 无 | 不能稳定执行全部现有测试 |
| 2 | pyproject、路径配置、持仓状态隔离 | PR 1 | 路径或持仓兼容测试失败 |
| 3 | 领域边界与依赖环解除 | PR 1–2 | 冻结评分、分桶或回测结果变化 |
| 4 | 每日候选工作流拆分 | PR 3 | 候选集合、顺序、快照哈希变化 |
| 5 | 技术、ETF、单标的报告拆分 | PR 3 | A/H/ETF Golden 出现非预期差异 |
| 6 | 移除兼容层和重复抽象 | PR 4–5 | 仍存在旧 import 调用者 |
| 7 | 当前文档和历史计划整理 | PR 1–6 | 文档命令无法执行 |

任一 PR 出现无法解释的数值、推荐资格、快照哈希或报告 schema 变化时，停止后续阶段，将行为变化拆为单独的业务变更计划。结构 PR 不接受“顺便修正”策略结果。

## Definition of done

- [ ] `make check` 一条命令覆盖全部质量门并通过；
- [ ] Git hook 对所有 `scripts/**/*.py` 生效且无旧文件名；
- [ ] Python 版本和依赖在 `pyproject.toml` 中可审计；
- [ ] 真实 `portfolio.yaml` 不再由 Git 跟踪；
- [ ] 生产代码没有散落的 `sys.path.insert()`；
- [ ] 架构测试阻止新的反向依赖和层间环；
- [ ] 回测不导入在线扫描器；
- [ ] 候选、ETF、单标的 CLI 均为薄入口；
- [ ] 推荐规则、评分、分桶、快照哈希和 Golden 结果无非预期变化；
- [ ] `CLAUDE.md`、`AGENTS.md` 和架构文档与实际目录一致；
- [ ] 四个正式用户入口通过离线兼容验证；
- [ ] 没有清理或提交报告、缓存、凭据和用户运行数据。
