# Codex Skills 兼容性评估

> 评估日期：2026-08-05  
> 工程路径：`/Users/jing.li7/personal/stock-trend`  
> Codex CLI：`codex-cli 0.146.0`

> 实施状态（2026-08-05）：本评估提出的 Codex 发现与适配层已经落地：新增 `.agents/skills/stock-trend`、根目录 `AGENTS.md`、Codex 兼容 frontmatter 及 `agents/openai.yaml`。第 1～6 节保留的是迁移前评估与诊断基线；业务测试失败和可选数据源依赖仍需单独处理。

## 1. 结论

评估时 Codex 本身可以正常运行，但工程中的 `stock-trend` skill **尚不能被 Codex 原生发现和调用**。发现与适配问题现已按上方实施状态解决。

底层 Python 脚本大部分可以运行；联网授权后，东方财富、腾讯港股和周线数据源均可用。不过工程仍缺少部分可选依赖和 Tushare Token，且现有测试并非全绿。因此当前状态应判断为：

- Codex CLI：正常。
- Codex 自动发现 `stock-trend`：不正常，当前未发现。
- Skill 底层脚本：部分可运行。
- 实时行情：需要 Codex 网络授权，授权后主要降级数据源可用。
- 工程质量门禁：未通过。

## 2. Codex 技能发现状态

### 2.1 当前目录不属于 Codex repo skill 搜索路径

当前 skill 位于：

```text
.claude/skills/stock-trend/SKILL.md
```

工程中不存在以下 Codex repo skill 目录：

```text
.agents/skills/
```

Codex 官方规则是从当前目录到仓库根目录逐级扫描 `.agents/skills`。Codex 支持 skill 目录符号链接，因此可以保留 Claude Code 目录作为实现源，再从 `.agents/skills/stock-trend` 链接到现有目录。

参考：[OpenAI - Build skills](https://learn.chatgpt.com/docs/build-skills)

### 2.2 当前会话未加载 `stock-trend`

本次 Codex 会话公布的可用 skills 中没有 `stock-trend`。这与项目缺少 `.agents/skills` 的情况一致，说明它不是单纯的显示问题，而是当前确实没有进入 Codex 技能发现结果。

### 2.3 项目指令没有迁移到 `AGENTS.md`

工程当前有 `CLAUDE.md`，但没有 `AGENTS.md`。

`CLAUDE.md` 中定义了：

- 股票分析角色与用户画像；
- 报告目录读取限制；
- Python 脚本修改流程；
- 必须执行的主测试和 Golden diff；
- 主要命令与架构说明。

这些内容不会作为 Codex 的标准仓库级持久指令自动加载。需要迁移或整理到根目录 `AGENTS.md`。

## 3. `SKILL.md` 兼容性

### 3.1 基础 YAML 有效

现有 frontmatter 可以被 YAML 解析，并包含 Codex skill 所需的两个基本字段：

```yaml
name: stock-trend
description: A股/港股/ETF日趋势判断，输出结构化报告
```

因此 skill 内容不是语法完全无效，而是主要受目录发现和平台语义差异影响。

### 3.2 包含 Claude Code 专属字段和工具名

现有 frontmatter 还包含：

```yaml
triggers:
argument-hint:
allowed-tools:
```

`allowed-tools` 中使用了以下 Claude Code 风格名称：

- `Read`
- `Write`
- `Bash(...)`
- `WebSearch`
- `WebFetch`
- `mcp__web-search__...`

这些权限声明不会自动转换成当前 Codex 会话中的工具或沙箱权限。尤其是：

- Python 行情脚本需要网络访问时，Codex 仍可能要求用户批准；
- `open` 和 `open -a "Google Chrome"` 属于 GUI 操作，需要额外授权；
- Claude MCP 工具名只有在 Codex 配置了同名 MCP server 时才可能存在；
- Codex 的文件、Shell、Web 工具名称与 Claude Code 不完全一致。

### 3.3 Slash command 调用方式不兼容

当前 skill 把以下形式作为触发入口：

```text
/stock-trend
/etf-scan
/portfolio
/etf-backtest
/longtou
/market-theme
...
```

Codex 原生 skill 的显式调用方式是 `$stock-trend`，也可以通过 `/skills` 选择。Claude Code 自定义 slash commands 不会仅凭 `triggers` 字段直接变成 Codex slash commands。

Codex CLI 支持 `/import` 导入 Claude Code 的 skills、slash commands、指令和部分配置。导入后仍需检查权限、工具名和参数占位符。

参考：[OpenAI - Import from another agent](https://learn.chatgpt.com/docs/import)

### 3.4 Description 覆盖范围不足

当前 description 只描述“A股/港股/ETF日趋势判断”，但同一个 skill 实际还承载：

- ETF 扫描；
- 持仓管理；
- ETF 回测；
- 龙头和中军扫描；
- 市场主线分析；
- 龙虎榜跟踪；
- 每周报告；
- 每日复盘；
- 每日候选股；
- 整合扫描。

Codex 主要根据 `name` 和 `description` 决定是否隐式加载 skill。当前描述难以覆盖这些分支。例如用户要求“查看持仓预警”时，Codex 未必会把它与“日趋势判断”匹配起来。

### 3.5 Skill 规模偏大

当前 `SKILL.md` 统计结果：

```text
501 行
约 1,460 词
约 25 KB
```

它实际上是多个相对独立的工作流集合。官方建议一个 skill 聚焦一个明确的用户目标，并将重型参考资料放到 `references/`。建议至少拆分为：

- `stock-trend`：单标的趋势分析；
- `etf-scan`：ETF 扫描；
- `portfolio`：持仓管理；
- `market-theme`：市场主线、龙头和候选股；
- `stock-backtest`：ETF/选股模型回测。

拆分不是 Codex 能否加载的硬性前提，但能改善隐式触发准确率、上下文占用和维护边界。

## 4. 脚本运行验证

### 4.1 基础环境

```text
Python 3.10.0
numpy 2.2.6
pandas 2.3.3
PyYAML、requests 可导入
```

代码解析烟测成功：

```text
513180 -> 513180.SH
```

### 4.2 缺失依赖和配置

诊断发现：

```text
tushare：未安装
baostock：未安装
TUSHARE_TOKEN：未配置
```

这不会完全阻止 A 股和港股分析，因为脚本具有东方财富、腾讯等降级链路；但会降低数据源冗余和部分能力的稳定性。

### 4.3 网络和沙箱行为

在 Codex 默认沙箱内运行诊断时，外部行情域名解析失败。获得联网授权后重新诊断，结果为：

```text
东方财富：正常，243 条记录
腾讯港股：正常，251 条记录
周线数据：正常，104 条记录
```

说明第一次失败主要来自 Codex 网络沙箱，并非行情实现整体失效。实际使用该 skill 时，应预期实时行情抓取会触发网络授权。

## 5. 测试结果

### 5.1 主测试套件

执行：

```bash
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
```

结果：

```text
123 passed, 5 failed, 0 skipped
```

失败项：

1. `TF-07`：沙箱内腾讯港股数据获取失败。
2. `TF-RPT-STALE-01`：K 线失败时报告仍出现格式不正确的当前价占位。
3. `TF-RPT-STALE-04`：无有效 K 线 close 时仍未满足“不回退旧当前价”的断言。
4. `TF-RPT-STALE-05`：有效 K 线当前价优先级断言失败。
5. `TG-golden-diff`：Golden snapshot diff 未通过。

其中第 1 项可由联网授权解决；第 2～4 项属于报告生成逻辑或测试预期问题，不能归因于 Codex 技能发现机制。

### 5.2 Golden diff

执行：

```bash
python3 .claude/skills/stock-trend/tests/test_golden.py --diff
```

结果：

```text
15 passed, 8 failed, 0 warnings
```

存在差异的快照：

- `600519/capital_flow.json`
- `600519/macro_snapshot.json`
- `600519/scores.json`
- `513180/macro_snapshot.json`
- `513180/etf_data.json`
- `513180/scores.json`
- `00700/fundamental.json`
- `00700/macro_snapshot.json`

需要逐项判断这些差异是数据口径变化、缓存变化、当前日期变化，还是代码回归；在确认之前不应直接执行 `--regenerate`。

## 6. 问题分级

| 优先级 | 问题 | 影响 |
|---|---|---|
| P0 | 缺少 `.agents/skills/stock-trend` | Codex 无法发现和显式调用 skill |
| P0 | 缺少 `AGENTS.md` | Codex 不会自动遵循现有工程规则和测试门禁 |
| P1 | Claude 工具权限与 slash command 未转换 | 加载后仍可能选择错误工具或无法按原命令执行 |
| P1 | 3 个陈旧行情/当前价报告测试失败 | 存在输出错误价位或错误回退的风险 |
| P1 | Golden diff 有 8 项失败 | 当前输出未通过既定回归基线 |
| P2 | 缺少 Tushare、BaoStock、Token | 数据源冗余不足，部分场景只能依赖降级源 |
| P2 | Description 与工作流范围不匹配 | Codex 隐式触发可靠性较低 |
| P3 | 单个 skill 过大 | 上下文占用高，维护和触发边界不清晰 |

## 7. 建议迁移方案

### 阶段一：让 Codex 能发现 skill

优先选择以下一种方式：

1. 在 Codex CLI 中执行 `/import`，选择 Claude Code 工程并导入 skills、slash commands 和指令；或
2. 创建 repo 级 `.agents/skills/stock-trend` 符号链接，指向 `.claude/skills/stock-trend`。

同时新增根目录 `AGENTS.md`，迁移 `CLAUDE.md` 中对 Codex 同样适用的工程约束。

### 阶段二：建立 Codex 适配层

- 使用 `$stock-trend` 作为主入口；
- 将子工作流写入 description，或拆分成多个 skills；
- 审查并移除/替换 Claude 专属 frontmatter；
- 将工具描述改为平台无关的行为指令；
- 明确网络、GUI 和外部数据源需要授权时的降级策略；
- 对必须依赖 MCP 的步骤声明 Codex MCP 配置或提供 Web/Shell 降级路径。

### 阶段三：恢复质量门禁

- 修复 `TF-RPT-STALE-01/04/05`；
- 在联网和离线两种模式下分别测试数据获取；
- 解释并处理 8 项 Golden 差异；
- 确保主测试和 Golden diff 均为零失败；
- 再用代表性提示验证显式触发、隐式触发和错误输入处理。

## 8. 推荐验收标准

完成迁移后，应满足：

1. Codex `/skills` 能显示 `stock-trend`。
2. `$stock-trend 513180` 能正确加载完整 `SKILL.md`。
3. “分析 513180 趋势”能隐式触发该 skill。
4. ETF 扫描、持仓和回测请求能触发正确的独立 skill 或明确路由。
5. 无网络授权时给出清晰降级提示，不误用旧行情。
6. 联网授权后东方财富、港股和周线诊断通过。
7. 主测试套件零失败。
8. Golden diff 零失败或所有变化均经过确认并重新生成。

## 9. 本次评估范围

本次仅执行只读检查、诊断和测试，没有修改 skill、配置或业务代码。测试产生的内容位于已有缓存或系统临时目录，评估完成时 Git 工作区保持干净。
