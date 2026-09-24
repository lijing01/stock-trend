# 本机运行说明

Python 运行要求为 >=3.10；工作流文档中的 `python3` 指满足要求的解释器。

当前维护环境默认 `python3` 为 3.9.6，必须将命令中的解释器替换为已安装的 `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3`，无需修改全局环境。

## 东方财富 / 同花顺实时接口运行契约

东方财富和同花顺的实时行情接口在受限沙盒中可能出现 DNS 失败。凡是运行会访问这些来源的 fetcher 或工作流（包括 `market_regime.py`、`daily_candidates.py`、行业扫描，以及 K 线、资金流 fetcher），Agent 必须直接在**沙盒外**执行，且仅对该次命令设置 `NO_PROXY` 和 `no_proxy`：

```bash
NO_PROXY="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
no_proxy="${NO_PROXY:+${NO_PROXY},}eastmoney.com,.eastmoney.com,10jqka.com.cn,.10jqka.com.cn" \\
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 <script> [args]
```

- 对 Codex 工具调用，这意味着使用 `require_escalated` 启动该命令；不要先在沙盒内重试这些实时接口。
- 仅追加本次进程的代理绕过名单，不修改 shell profile、全局代理或系统 DNS 设置。`eastmoney.com` / `.eastmoney.com` 覆盖东方财富子域，`10jqka.com.cn` / `.10jqka.com.cn` 覆盖同花顺子域。
- 外部直连仍失败时，记录失败来源与原因，按既有缓存/降级规则继续；报告必须标注 `degraded`、`cached` 或数据缺失，绝不能称为实时数据。
