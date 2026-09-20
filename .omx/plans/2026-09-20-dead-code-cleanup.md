# 无用代码清理执行计划

## 目标

在不改变股票扫描、候选推荐和报告输出行为的前提下，删除仓库内已有充分证据支持的死代码，并将“测试去重”和“历史文档整理”留到独立批次。

本轮不增加依赖、不修改评分或数据契约、不调整兼容字段，也不重新生成 golden snapshots。

## 已确认范围

### 本轮删除

1. 删除 `.claude/skills/stock-trend/scripts/scans/stock_scanner.py:2222` 开始的 `_run_phase2_legacy()` 完整定义。
   - 当前仓库只有定义，没有生产或测试调用。
   - 正式实现位于同文件 `run_phase2()`（当前约 2697 行），CLI 和测试均使用正式实现。
   - 函数名以下划线开头，不属于已记录的 CLI 或公共入口。

2. 删除 `.claude/skills/stock-trend/scripts/core/ths_utils.py` 整个模块。
   - `fetch_page()`、`fetch_page_with_retry()`、`extract_table_rows()`、`parse_amount()` 均无仓库内调用。
   - `fetch_page_with_retry()` 明确是旧兼容别名。
   - 原先使用这类能力的同花顺主题、龙虎榜和市场龙头工作流已在历史提交中移除；当前板块实现位于 `scripts/fetchers/sector_akshare.py`。

### 本轮不处理

- 不删除 `scripts/core/base_fetcher.py`：仍存在契约测试，外部 API 边界不充分明确。
- 不合并两个 `candidate_news.py`：两者分别负责抓取与评分。
- 不合并 `analysis/market_explanation.py` 和 `reporting/market_explanation.py`：两者分别负责计算契约与渲染。
- 不删除 legacy JSON/cache 兼容逻辑：这些分支仍有明确测试。
- 不重构大型测试文件；测试 fixture 去重作为第二批独立变更。
- 不删除历史计划文档；只在后续批次增加归档标识或移动到 archive。

## 验收标准

1. 仓库内不存在 `_run_phase2_legacy`、`ths_utils`、`fetch_page_with_retry`、`extract_table_rows`、`parse_amount` 的有效代码引用。
2. `run_phase2()` 的签名、返回结构、数据源调度、候选评分及 CLI 调用保持不变。
3. `test_stock_scanner.py` 全部通过。
4. 仓库规定的两个质量门全部通过：
   - `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
5. `test_golden.py --diff` 不产生需要接受的新快照；若输出发生变化，停止清理并定位依赖关系，不更新 golden 文件。
6. `git diff --check` 无空白或补丁格式错误。
7. 最终 diff 只包含计划内的删除，以及删除导致的必要注释/文档引用修正。

## 执行步骤

### 1. 建立清理前基线

记录当前工作树，避免覆盖用户已有修改：

```bash
git status --short
git diff -- .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  .claude/skills/stock-trend/scripts/core/ths_utils.py
```

重新确认引用事实：

```bash
rg -n --glob '!reports/**' --glob '!.cache/**' --glob '!**/__pycache__/**' \
  '_run_phase2_legacy|ths_utils|fetch_page_with_retry|extract_table_rows|parse_amount' .
```

运行最相关的扫描器测试作为修改前基线：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

停止条件：如果 `_run_phase2_legacy` 或 `ths_utils` 出现新的实际调用点，先从删除范围中移除对应候选，不做兼容性猜测。

### 2. 删除孤立的旧扫描实现

在 `.claude/skills/stock-trend/scripts/scans/stock_scanner.py` 中删除 `_run_phase2_legacy()` 的完整函数体，保留紧随其后的正式 `run_phase2()` 及其上下文不变。

删除后检查：

```bash
rg -n '_run_phase2_legacy' .claude/skills/stock-trend --glob '!**/__pycache__/**'
python3 -m py_compile .claude/skills/stock-trend/scripts/scans/stock_scanner.py
python3 .claude/skills/stock-trend/tests/test_stock_scanner.py
```

停止条件：若测试表明正式 `run_phase2()` 隐式依赖旧函数中的共享状态或初始化逻辑，恢复这一批删除并先提取共享逻辑；不得把旧函数整体重新接入运行路径。

### 3. 删除孤立的 THS 工具模块

删除 `.claude/skills/stock-trend/scripts/core/ths_utils.py`。删除前后各执行一次全仓引用搜索，确认没有字符串导入、动态模块名或文档化公共入口。

```bash
rg -n --glob '!reports/**' --glob '!.cache/**' --glob '!**/__pycache__/**' \
  'ths_utils|fetch_page_with_retry|extract_table_rows|parse_amount' .
```

如果仅剩历史文档文字，不在本批次顺手大规模改写文档；只修正会让用户误以为该模块仍是当前入口的说明。

### 4. 运行强制质量门

由于本轮修改 `scripts/` 下的 Python 文件，必须运行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

然后执行静态收尾检查：

```bash
git diff --check
git status --short
git diff --stat
git diff -- .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  .claude/skills/stock-trend/scripts/core/ths_utils.py
```

### 5. 审查与交付

审查重点：

- diff 是否为纯删除或必要引用修正；
- `run_phase2()` 是否没有非预期变更；
- 是否误删了数据源、评分、缓存或输出兼容逻辑；
- golden 输出是否完全稳定；
- 工作树中原有的无关修改是否保持原样。

交付报告应包含：删除文件/函数、减少的代码行数、测试结果、golden diff 结果和剩余未处理候选。

## 第二批建议：测试代码去重

在第一批合并后单独执行，避免与生产代码删除混合：

1. 为 `tests/test_market_explanation.py:27` 与 `tests/test_market_explanation_reporting.py:20` 提取共享基础 fixture。
2. 明确保留 `good` 与 `partial` 数据质量差异，只共享公共指数、日期和组件分数。
3. 先保持所有原测试断言不变，再进行 fixture 替换。
4. 运行两个测试文件以及两个仓库质量门。

这批目标是减少重复，不删除行为覆盖。

## 第三批建议：历史文档归档

独立整理仍引用已删除模块的旧计划，包括：

- `docs/scripts-refactoring-plan.md`
- `docs/sector-persistence-coverage-fix-plan.md`
- `docs/superpowers/plans/2026-08-28-weekly-market-data-reliability.md`
- `docs/superpowers/plans/2026-05-31-ths-theme-longtou-integration.md`

默认采用“移动到 archive 或增加历史状态页首说明”，不直接删除，以保留设计决策和 Git 之外的阅读上下文。

## 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| 外部脚本直接导入 `ths_utils` | 删除前检查 `SKILL.md`、CLI 入口和全仓引用；若项目承诺第三方 Python API，则将该模块标记 deprecated 一个发布周期，而不是立即删除 |
| `_run_phase2_legacy` 被动态调用 | 搜索字符串和属性访问；其私有命名、零调用及正式替代实现共同作为删除依据 |
| 删除大函数时误伤相邻正式实现 | 使用结构化补丁只删除函数定义；立即运行 `py_compile` 和扫描器测试 |
| 清理导致 golden 变化 | 不接受或重生成 snapshot；停止并定位隐藏行为依赖 |
| 清理与用户现有修改冲突 | 执行前检查目标文件 diff；只修改明确目标，不回退无关工作树内容 |

## 完成定义

只有在目标符号全部消失、扫描器测试通过、两个强制质量门通过、golden 无变化、diff 审查确认没有行为修改后，本轮清理才算完成。
