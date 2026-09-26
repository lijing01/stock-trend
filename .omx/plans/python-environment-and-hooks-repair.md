# Python 环境与提交钩子修复计划

状态：计划完成，尚未实施。
范围：上一轮审查第 1 项（提交钩子失效检查）和第 2 项（Python 环境可复现性）。

## 目标与边界

让新环境能按明确声明安装依赖；让提交钩子使用符合版本要求的解释器，并实际检查当前目录结构下的代码。

- 保留现有脚本 CLI、分析算法、数据源降级和 Golden 数值输出。
- 本轮不拆分扫描器、不重构包导入、不修复缓存、不迁移整个测试框架。
- 不修改全局 Python、shell profile、代理或系统 DNS；不提交凭据。
- 不主动读取 reports/；不生成报告验证环境。
- 不在规划阶段安装依赖、安装 Git hooks 或执行源代码修改。

## 已确认的依据

1. `.githooks/pre-commit:22` 的 PY_STAGED 只匹配 scripts 下一级 Python 文件，而生产模块已迁入 analysis、core、fetchers 等子目录。
2. `.githooks/pre-commit:24,76,132–134,250` 附近仍使用 generate_report.py、analyze_technical.py、compute_scores.py 旧路径；test_import 在目标不存在时直接返回成功。
3. 当前对应接口位于 `scripts/analysis/technical.py:887`、`scripts/analysis/scores.py:233,524`、`scripts/reporting/report.py:41`。
4. `.githooks/pre-commit:47,123,201` 直接调用 python3；`references/local-runtime.md:3–5` 要求 Python >=3.10，并记录维护环境默认 python3 为 3.9.6。这是文档记录，实施时需重新探测实际环境。
5. 当前未发现 pyproject.toml 或 requirements 配置。生产代码直接依赖 numpy/pandas，另有 AKShare、Tushare、BaoStock、requests、PyYAML 等导入，完整依赖范围需由静态导入盘点确认。
6. `scripts/diagnose.py:43–54` 仅检查 tushare、baostock、numpy、pandas；未覆盖全部已使用依赖。
7. `.githooks/pre-commit:72,79,153,183` 使用 grep -P；实现需兼容 macOS 默认工具，不依赖 GNU grep。
8. 现有 SKILL.md 已是引用路由入口；旧的 Step 连续性检查不足以证明引用有效。

## 实施步骤

### A. 固化环境声明与解释器选择

涉及：新建 `pyproject.toml`、依赖锁定文件、更新 `references/local-runtime.md` 和必要的环境工具。

1. 盘点生产/测试中的第三方导入、可选降级分支及当前已验证版本；不把宿主机所有已安装包当作项目依赖。
2. 在 pyproject.toml 声明 `requires-python >=3.10`、核心依赖、数据源可选依赖和测试依赖；按调用链确定归属，确保默认日常工作流所需依赖有明确安装组合。
3. 第一版使用标准 venv + pip，不要求迁移到 uv/Poetry；依赖声明以 pyproject.toml 为源。明确配置构建/安装范围，避免 setuptools 自动打包 reports、缓存或仓库杂项，也不借此迁移现有 scripts 模块。
4. 在干净虚拟环境中解析安装，验证后生成精确版本锁定文件；记录 Python 版本、平台和生成命令。若不同平台存在差异，分开锁定，不能声称一份本机锁定文件覆盖所有系统。
5. 提供可复跑的锁定生成/一致性检查方法；从已锁定依赖安装项目时避免再次无约束解析依赖。用 pip check 验证最终安装组合。
6. 定义统一解释器优先级：显式 STOCK_TREND_PYTHON → 仓库 .venv/bin/python → 当前可用 python3。选中后验证 >=3.10；显式配置无效必须失败，不静默回退。日志显示所选解释器及版本。
7. 从共享运行说明移除个人绝对路径作为强制要求；可保留为本机补充示例。保留实时接口进程级代理设置和现有权限契约。
8. 若需要共享解释器选择逻辑，用一个小型辅助脚本供 hook/安装说明复用；不在多个入口复制检测逻辑。

完成条件：干净环境可按文档安装，pip check 成功，不依赖个人 pyenv 路径；Python 3.9 会被明确拒绝。

### B. 先锁定钩子行为，再修复失效检查

涉及：`.githooks/pre-commit`、`.githooks/install-hooks.sh`，必要的小型检查器和离线测试。

1. 使用临时 Git 仓库构造钩子回归测试，不操作真实仓库 index，不产生真实提交。
2. 基础覆盖：子目录脚本语法错误、路径含空格、没有相关变更、解释器不合格、模块缺失、Golden 失败，以及已暂存内容与工作区内容不一致。
3. 从 git diff --cached --name-only -z 取得暂存路径，处理新增/修改/重命名/删除，覆盖 scripts 所有层级。语法检查读取暂存版本，避免工作区修复掩盖即将提交的错误。
4. 用当前模块替换旧引用：analysis.technical、analysis.scores、reporting.report；强制模块/接口不存在时失败。
5. 导入、模板与 Golden 检查使用同一个暂存树视图，在临时目录保留实际结构；避免检验与提交内容不同的工作区。不改变工作区、不 stash 用户修改。
6. 模板检查指向 reporting/report.py；优先验证真实渲染行为，避免用字典键正则作为唯一证明。scores 的输入验证继续覆盖合法与非法输入，更新导入和触发路径。
7. 移除 GNU grep -P 依赖；结构性解析使用 Python 标准库或可移植 shell 工具。
8. Skill 检查覆盖 SKILL.md、references/ 与被引用本地脚本；验证必需元数据与路径存在。删除失去意义的入口 Step 编号检查，不增加固定措辞约束。
9. scripts 或关键依赖配置变更触发必要的导入/Golden 检查；所需检查器或 Golden 文件缺失必须失败，不能默认跳过。保留现有显式 Golden 跳过机制，但输出醒目原因与验证缺口。
10. 所有检查使用 A 中的同一个解释器；hook 不联网、不自动安装依赖。不扩展为每次提交都运行联网主测试。
11. 安装脚本使用可移植的仓库相对 hooksPath，并验证实际生效路径；在临时仓库测试，实施时不覆盖用户未知的现有 hook 配置。

完成条件：嵌套脚本错误、缺失模块和 Golden 失败均拒绝提交；有效暂存内容通过；对真实工作区与 index 无副作用。

### C. 对齐环境诊断与维护文档

涉及：`scripts/diagnose.py`、`references/local-runtime.md`、`docs/usage-guide.md`、测试说明。

1. 修改 diagnose.py 前说明：此次只补齐 Python 版本和依赖检查，不修改行情抓取逻辑。
2. 诊断输出区分核心依赖缺失、可选数据源未安装和凭据未配置；版本与依赖归属和 pyproject.toml 保持一致，避免维护两套互相漂移的清单。
3. 离线依赖检查必须读取当前环境，不能被旧行情诊断缓存遮蔽；不输出 token 或使用真实 token 验证。
4. 文档明确：创建 venv、按锁定文件安装、解释器覆盖、核心/完整数据源/测试安装组合、hook 安装和两条既有质量命令。
5. 不重写 stock-trend 工作流和报告规范。

完成条件：新用户按一份运行说明完成安装与验证；缺失可选依赖不被误报为整个工程无法运行。

## 验收矩阵

| 场景 | 期望结果 |
|---|---|
| macOS 默认 grep 无 -P | hook 检查仍正常工作 |
| Python <3.10 或显式解释器路径无效 | 非零退出，给出明确修复提示 |
| scripts/core 或 scripts/analysis 下语法错误 | 暂存该错误时 hook 非零退出 |
| 工作区修复了已暂存错误 | hook 仍拒绝暂存错误 |
| 工作区有未暂存错误，暂存内容合法 | 按暂存树结果判断，不误检未提交内容 |
| 必检模块、符号或测试文件缺失 | 非零退出，禁止静默跳过 |
| Golden 返回失败 | hook 非零退出，不刷新快照 |
| 只提交无关文件或正常嵌套代码 | 无误阻断，不联网 |
| 新建干净 venv，按锁定配置安装 | pip check、核心导入和既有质量门禁成功 |
| 可选数据源未安装 | 诊断说明缺少的能力和安装组合 |
| 修改依赖声明但没有同步锁定文件 | 一致性检查失败 |
| 运行 hook 测试 | 原仓库 index、工作区、Git 配置不变 |

## 验证与停止条件

1. 对 shell 文件运行 bash -n；运行 hook 临时仓库离线回归测试。
2. 验证干净环境中的核心/完整数据源安装组合、pip check、Python 版本选择及关键模块导入。
3. 新增或修改生产 Python 后，使用符合要求的统一解释器执行：
   - `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`
   - `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`
4. 主测试存在联网抓取路径：按运行契约执行，不能把网络失败归类为离线回归通过；若受外部接口阻塞，列出具体缺口与已完成离线证据。
5. Golden 不应有数值变化；出现差异先查依赖版本/浮点行为/暂存树路径，不自动重新生成快照。
6. 运行 git diff --check，核对实际修改只属于本计划范围。
7. 全部验收完成才报告修复完成；计划阶段仅交付本文件。

## 风险与缓解

- 数据源库可能不再兼容 Python 3.10：先验证可安装版本，不能直接锁定最新版，也不在本轮擅自提高最低版本。
- 新依赖组合可能影响数值结果：依赖候选以 Golden 与现有测试通过为准，不从宿主环境全量 freeze。
- 暂存树检查依赖路径和 fixture：保持完整目录结构；使用临时缓存，避免写真实缓存和报告。
- 模板正则检查可能误报：以真实渲染用例和已定义必需字段为准，不仅提取源码字符串。
- 环境工具重复配置：依赖声明单一来源，锁定文件为生成产物；解释器检测单一实现。

建议交付拆成两组可审查变更：A（环境基础）；B+C（hook 与诊断文档），依次验证。不需要协调多代理实施。
