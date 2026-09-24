---
name: stock-trend
description: 分析 A股、港股和 ETF 的中线趋势并生成结构化报告；也用于今日推荐、ETF 扫描、观察列表、ETF/维科夫回测、每日复盘及候选股。用户提到股票或 ETF 趋势、代码分析、观察列表、选股、复盘或上述工作流时使用。
---

# 股票趋势判断

使用 `$stock-trend` 显式调用；也可直接用自然语言说明标的或工作流。下文保留的 `/stock-trend`、`/etf-scan` 等名称是工作流路由标识，不要求 Codex 提供同名 slash command。

运行 Python 脚本时以仓库根目录为工作目录。Python 要求 >=3.10，文档中的 `python3` 指当前环境中满足要求的解释器。执行任何脚本前，必须阅读 [本机运行说明](references/local-runtime.md)，按其中规则选择解释器；访问东方财富或同花顺实时接口时还必须遵守其沙盒外执行和进程级代理绕过契约。

行情和新闻属于时效性数据，必须使用可用的联网工具或脚本实时获取；网络受限时申请授权，若仍不可用则明确标注降级或数据缺失，不得把旧缓存冒充实时数据。仅在用户明确要求时打开 GUI 或浏览器。

## 工作流路由

根据用户意图只读取并执行对应的一级引用：

- 用户说“今日推荐”时，使用 `/today-recommendation` 统一入口；只需候选扫描时使用 `/candidates`。完整规则见 [今日推荐与候选股工作流](references/today-and-candidates.md)。除“今日推荐”的统一流程外，各流程独立。
- 用户要求 ETF 扫描时使用 `/etf-scan`；ETF 速评分回测使用 `/etf-backtest`；维科夫买点回测使用 `/wyckoff-backtest`；热点板块成分股筛选使用 `/stock-scanner`。完整规则见 [ETF 与扫描器工作流](references/etf-and-scanner-workflows.md)。
- 用户要求每日复盘或市场环境评分时使用 `/daily-review`。完整规则见 [每日复盘工作流](references/daily-review.md)。
- 用户要求分析单只股票或 ETF 的趋势时使用 `/stock-trend` Step 1–4。完整规则见 [个股 / ETF 趋势分析流程](references/stock-analysis.md)。

## 通用交付约束

- 所有输出必须附带：**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**
- 各工作流的 CLI 参数、数据资格门槛、人工复核、报告展示和持久化规则以对应一级引用为准，不得省略。

## 报告模板

- Markdown：[assets/report-template.md](assets/report-template.md)
- HTML：[assets/report-template.html](assets/report-template.html)
