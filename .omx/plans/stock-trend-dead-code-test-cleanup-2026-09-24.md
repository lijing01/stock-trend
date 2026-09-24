# Stock Trend 无用代码与测试整理计划

## 目标与范围

在保持分析结果、推荐分桶、历史文件处理和既有命令行输出不变的前提下，清理仓库内无调用的函数及确定冗余的分支；梳理独立测试与规定质量门禁之间的关系。此次计划不以“未进入主测试脚本”为理由删除测试文件。

涉及生产代码：`scripts/analysis/market_style.py`、`scripts/core/eastmoney_utils.py`、`scripts/core/resolve_code.py`、`scripts/analysis/market_regime.py`、`scripts/backtesting/recommendation_experiments.py`。涉及测试入口：`tests/test_stock_trend.py`、`tests/test_golden.py` 以及独立 `test_*.py` 文件。下文路径均相对 `.claude/skills/stock-trend/`。

## 已确认的依据

- `scripts/analysis/market_style.py:321-325`：`_shadow_bucket()` 的两个分支均返回 `legacy_bucket`；`annotate_candidates_for_shadow()` 仅在同文件第 379 行调用它。`tests/test_market_style.py:160-199` 和 `tests/test_market_shadow_integration.py:82-99` 已检查影子分桶不改变正式分桶。
- `scripts/core/eastmoney_utils.py:364,425`、`scripts/core/resolve_code.py:285`、`scripts/analysis/market_regime.py:973`、`scripts/backtesting/recommendation_experiments.py:1605`：仓库内搜索只找到这五个函数的定义，没有调用。该证据不能排除仓库外调用。
- `AGENTS.md:21-25`：修改脚本后必须运行 `test_stock_trend.py` 和 `test_golden.py --diff`。`tests/test_stock_trend.py:1783-1828` 手动接入一部分独立测试；按文件名引用核对，另有 19 个测试模块未被该入口点名。它们可被单独调用，未接入不代表无用。
- `tests/test_stock_trend.py:1893-1907` 会执行一次 `test_golden.py --diff`，而 `AGENTS.md:24-25` 又要求单独执行一次，故规定流程重复执行 Golden 检查。

## 实施顺序

### 1. 固定基线与测试清单

1. 保存当前工作树状态，记录两条规定质量命令的基线结果。若 Golden 因实时数据不可用失败，保存具体失败项，不更新快照来掩盖差异。
2. 为现有 34 个独立 `test_*.py` 模块建一张清单：是否被 `test_stock_trend.py` 调用、是否可被 `unittest`/`pytest` 发现、是否依赖实时网络或本地缓存、覆盖的生产入口。19 个未接入模块逐一归类为“保留并纳入门禁”“保留为显式专项测试”或“待删除”；每个待删除项必须给出生产功能已退役或覆盖完全重复的证据。
3. 检查 `market_style` 现有测试是否覆盖 `strong/mixed/weak/unknown` 状态下 `shadow_bucket == legacy_bucket`。缺失的状态先补回归断言，再编辑生产函数。对于拟删除的其他函数，只补保护仍在使用的模块行为的测试；不为无调用函数复制实现式测试。

### 2. 清理确定冗余的生产代码

1. 在 `market_style.py` 中去掉 `_shadow_bucket()` 及唯一调用，直接把 `legacy_bucket` 写入 `shadow_bucket`。保持字典键、值、候选列表和正式分桶不变。
2. 再次全仓搜索引用后，分别删除 `piecewise_linear_clamped()`、`bollinger_bands()`、`code_to_ts_code()`。每删除一个函数就检查相应模块的现有测试和导入是否仍通过；若发现导入方，停止该项删除并记录调用契约。
3. 将 `quarantine_invalid_history_dates()` 和 `save_shadow_evaluation()` 暂列为条件清理项。先核对仓库内任务配置、文档、脚本入口及可取得的仓库外自动化引用；确认没有调用后再单独删除。无法确认外部调用时保留，并在清单中记录原因。

### 3. 整理测试入口

1. 对步骤 1 的 19 个模块，优先把确定性、无网络依赖且覆盖现行功能的测试接入统一的可执行命令或明确写入项目质量命令。尤其保留 `test_wyckoff.py`、`test_market_regime.py`、`test_factor_ablation.py` 等对现行功能有覆盖的测试。不要仅为缩小测试文件数而删测试。
2. 对确实退役或完全重复的测试，先指出被替代的测试及断言，再逐个删除。`test_daily_candidates_syntax.py` 只检查源码能否编译，可在主入口已有等价编译检查并覆盖同一文件时考虑删除。
3. 保留 `AGENTS.md` 规定的两条外部命令；从 `test_stock_trend.py:1893-1907` 去掉内部重复的 Golden 调用，并同步调整该文件的计数与说明。若主测试脚本被其他自动化独立使用并依赖内嵌 Golden 检查，则先记录该契约，再决定是否保留重复运行。

## 验收条件

1. `market_style` 对所有现有状态生成的 `shadow_bucket` 与清理前一致，`action_changed` 仍为 `False`，正式分桶及输入对象不变。
2. 所有删除的函数在仓库内没有剩余引用，生产入口的导入和命令行行为保持正常；条件清理项有明确的“删除/保留”结论与依据。
3. 每个独立测试模块在清单中有明确归属；保留的测试有可执行命令，删除的测试有覆盖替代证据。不得把“未被主入口调用”单独作为删除依据。
4. 规定的两条质量命令均执行并记录结果：`python3 .claude/skills/stock-trend/tests/test_stock_trend.py`、`python3 .claude/skills/stock-trend/tests/test_golden.py --diff`。Golden 快照只在逐项确认预期数值或输出变化后更新。
5. 对改动文件运行适用的 lint、类型检查及静态检查；如仓库未配置相应工具，记录可用的替代检查。`git diff --check` 无错误，工作树只有计划内改动。

## 风险与处理

- **仓库外导入**：无仓库内引用不等于绝无使用。条件清理项在外部自动化无法核对时保留；其余函数删除前检查公开文档和入口。
- **测试门禁变慢或受网络波动影响**：新增门禁前先标记纯本地与实时数据测试，实时数据测试保留为显式专项命令，不混入稳定的日常门禁。
- **Golden 差异来源不明**：保存差异和数据源状态；不通过刷新快照消除失败。
- **一次性改动过大**：按“行为保护与分支简化”“无调用函数”“测试入口”分批提交或审查，每批独立验收。

## 完成判定

上述验收条件全部满足，且条件清理项的保留或删除结论已记录，即可结束清理。执行结果见下节。

## 执行记录（2026-09-24）

- 已删除 `_shadow_bucket()`、`piecewise_linear_clamped()`、`bollinger_bands()`、`code_to_ts_code()`；`shadow_bucket` 直接使用正式 `legacy_bucket`。全仓引用复核没有剩余调用。
- `quarantine_invalid_history_dates()` 与 `save_shadow_evaluation()` 仍无仓库内调用，但无法从仓库证据排除外部自动化使用，因此保留。
- `test_market_style.py` 已覆盖 `strong/mixed/weak/unknown` 四种状态且针对性测试 9/9 通过；删除冗余的 `test_daily_candidates_syntax.py`；`test_stock_trend.py` 不再内嵌 Golden 检查。独立测试归属和显式命令见 `docs/stock-trend-test-inventory.md`。
- 最终强制门禁：`test_stock_trend.py` 为 678 passed、0 failed、0 skipped；`test_golden.py --diff` 为 21 passed、0 failed、2 warnings。Golden 快照未更新。
- 仓库没有配置或安装 ruff、mypy；对改动的五个 Python 文件执行 AST 解析并运行 `git diff --check`，均通过。
