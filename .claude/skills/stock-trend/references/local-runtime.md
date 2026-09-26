# Python 环境与本机运行说明

Python 运行要求为 >=3.10；工作流文档中的 `python3` 指满足要求的解释器。

依赖声明以仓库根目录 `pyproject.toml` 为准。项目安装只注册依赖元数据，分析脚本和 skill 资源仍从仓库运行。

## 解释器选择

从仓库根目录运行 `bash tools/python.sh <Python 参数或脚本>`。包装器按以下优先级选择并校验 Python >=3.10：

1. 显式设置的 `STOCK_TREND_PYTHON`（可执行路径或 PATH 中的命令名）。空值、无效路径或旧版本立即报错，不回退。
2. 仓库 `.venv/bin/python`。已有但损坏的 `.venv` 立即报错，不使用全局解释器掩盖问题。
3. PATH 中的 `python3`。

所选路径和版本写入 stderr，不污染 JSON stdout。包装器不会自动安装依赖或改动全局环境。其他工作流文档中的 `python3 <script>` 可等价替换为 `bash tools/python.sh <script>`。提交钩子也使用该选择器。

## 创建与安装

先选择一个满足版本要求的 Python 创建虚拟环境；系统 python3 不满足要求时，在该命令中换用已安装的新解释器。以下命令在仓库根目录执行：

```bash
python3 -m venv .venv
bash tools/python.sh -m pip install --upgrade 'pip==26.1.1'
```

已提交的锁定文件适用于 **CPython 3.10、macOS arm64**，并记录生成主机的系统 release。每个依赖固定版本和下载归档 SHA256；不能将这些单平台归档哈希当作跨系统锁。核心锁还包含构建工具，保证本地项目可关闭构建隔离安装。

```bash
# 最小计算/东方财富 HTTP 环境；先安装核心及构建工具
bash tools/python.sh -m pip install --require-hashes --no-build-isolation -r requirements/macos-arm64-py310-core.lock
# 日常市场数据、所有备用数据源和测试工具
bash tools/python.sh -m pip install --require-hashes --no-build-isolation -r requirements/macos-arm64-py310-all.lock
# 本地项目只注册元数据，不重新解析依赖或安装隔离构建工具
bash tools/python.sh -m pip install --no-deps --no-build-isolation '.[all]'
bash tools/python.sh -m pip check
```

只使用核心环境时，省略完整锁安装，并将最后的 `'.[all]'` 改为 `.`。核心环境没有 AKShare，市场环境中的该来源会按既有规则降级，不能声称拥有完整市场数据能力。

其他平台/Python 版本可先按声明解析安装 `'.[all]'`，验证后生成自己的目标锁；此方式不使用已提交锁，不能声称复现本次已验证版本。单独启用能力可使用 `'.[market]'`（AKShare）、`'.[tushare]'`、`'.[baostock]'`、`'.[hk]'`（yfinance）或 `'.[test]'`。依赖安装不代表凭据权限或实时接口可用。

## 更新和检查锁定文件

生成器使用 pip >=23 的安装报告，Python 3.10 解析 TOML 需 tomli，离线检查直接依赖版本需 packaging。完整环境已包含这些工具；核心维护环境先安装 `'.[test]'`，再生成锁。**干净 Python 3.10 的 bootstrap 路径**是先升级 pip 并安装 `'.[all]'`，再运行生成器，不能直接在只有 venv 的环境中生成锁。

```bash
# 新平台/更新依赖：用干净 venv，先解析 pyproject 声明
bash tools/python.sh -m pip install '.[all]'
bash tools/python.sh tools/lock_environment.py --profile core --output requirements/macos-arm64-py310-core.lock
bash tools/python.sh tools/lock_environment.py --profile all --output requirements/macos-arm64-py310-all.lock
# 不联网：核对声明摘要、目标、profile、直接依赖版本和锁格式
bash tools/python.sh tools/lock_environment.py --profile core --output requirements/macos-arm64-py310-core.lock --check
bash tools/python.sh tools/lock_environment.py --profile all --output requirements/macos-arm64-py310-all.lock --check
```

其他目标使用不同文件名，不覆盖本机锁。`--check` 不重新解析传递依赖，不证明下载归档可用；完整验证仍需在干净 venv 中按哈希锁重建并运行 pip check/回归测试。锁包含源包时，其构建仍受编译工具和操作系统影响，不能声称二进制产物完全相同。

## 验证命令

```bash
bash tools/python.sh -m unittest discover -s tests -v
bash tools/python.sh .claude/skills/stock-trend/tests/test_stock_trend.py
bash tools/python.sh .claude/skills/stock-trend/tests/test_golden.py --diff
```

根目录 tests 为离线环境工具测试；主门禁含联网抓取，需要按下面的实时接口运行契约执行。Golden 比较不自动刷新快照。

## 提交钩子

```bash
bash .githooks/install-hooks.sh
```

安装器将当前仓库的 `core.hooksPath` 设置为相对路径 `.githooks`，从仓库子目录运行也可生效；已有其他 hooksPath 时拒绝覆盖。安装后可用 `git config --get core.hooksPath` 核对。本次代码修改不会自动安装钩子。

hook 从 Git index 导出临时 skill 树，检查暂存 Python（含全部子目录）、Skill 元数据与本地引用。脚本、模板、skill 测试或关键环境配置变化时，还检查当前模块接口、scores 合法/非法输入、模板真实渲染及 Golden `--diff`。所需模块、模板或 Golden 入口缺失会拒绝提交。模板校验覆盖当前样例启用的分支；可选分支仍需要专项测试。

业务检查不读取工作区未暂存内容，不修改真实 index、缓存或报告；不联网、不安装依赖，也不在每次提交时运行联网主门禁。hook 启动入口、解释器选择器与导出暂存树的外层检查器仍从工作区加载，这些工具自身的未暂存损坏可能导致启动失败。环境需事先按上述说明准备。只有无关路径变更时跳过 skill 检查。

只有已记录的外部阻塞才使用 `STOCK_TREND_SKIP_GOLDEN=1 git commit ...`；输出会明确标注 Golden 未验证。它不跳过其他检查，也不允许缺失 Golden 入口。临时仓库回归测试可运行 `bash tools/python.sh -m unittest discover -s tests -p test_pre_commit.py -v`。

## 东方财富 / 同花顺实时接口运行契约

东方财富和同花顺的实时行情接口在受限沙盒中可能出现 DNS 失败。凡是运行会访问这些来源的 fetcher 或工作流（包括 `market_regime.py`、`daily_candidates.py`、行业扫描，以及 K 线、资金流 fetcher），Agent 必须直接在**沙盒外**执行，且仅对该次命令设置 `NO_PROXY` 和 `no_proxy`：

```bash
NO_PROXY="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
no_proxy="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
bash tools/python.sh <script> [args]
```

- 对 Codex 工具调用，这意味着使用 `require_escalated` 启动该命令；不要先在沙盒内重试这些实时接口。
- 仅追加本次进程的代理绕过名单，不修改 shell profile、全局代理或系统 DNS 设置。`eastmoney.com` / `.eastmoney.com` 覆盖东方财富子域，`10jqka.com.cn` / `.10jqka.com.cn` 覆盖同花顺子域。
- 外部直连仍失败时，记录失败来源与原因，按既有缓存/降级规则继续；报告必须标注 `degraded`、`cached` 或数据缺失，绝不能称为实时数据。
