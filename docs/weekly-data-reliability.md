# 周报数据可靠性与故障排查

周报同时使用行业热力、市场持续性快照和龙虎榜机构快照。网络请求失败时，报告会保留历史数据（如有），并在 `meta` 与 Markdown/HTML 概览中披露来源、状态和覆盖范围；旧缓存不会被标记为本次实时成功。

## 每日龙虎榜采集

周报本身只读取历史龙虎榜快照，不会自动补齐历史采集。部署定时任务每日运行：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py --snapshot-only
```

成功时写入：

```text
.cache/stock-trend/lhb_snapshots/YYYY-MM-DD.json
```

每次尝试都会写入状态 sidecar：

```text
.cache/stock-trend/lhb_snapshots/status/YYYY-MM-DD.json
```

状态含义：

- `live_success`：本次请求拿到龙虎榜明细并完成板块聚合，快照可作为机构验证信号。
- `no_data`：请求正常完成，但目标日及最多 4 个前一日历日均没有明细，常见于非交易日。
- `error`：DNS、连接、HTTP 或解析失败；不会伪造空快照。
- `mapping_error`：拿到明细，但没有可用板块映射；旧映射若被显式使用会带 `mapping_stale=true`。
- `legacy_snapshot`：有旧 JSON 但没有 sidecar，只可参与历史统计，不能证明当天运行过采集。
- `not_run`：窗口内没有快照也没有状态记录。

周报 JSON 的 `meta` 重点字段：`lhb_available_days`（可用快照天数）、`lhb_attempted_days`（有 sidecar 的尝试天数）、`lhb_status_days`（逐日状态）、`lhb_failure_reasons` 和 `mapping_stale_days`。

没有有效龙虎榜快照时，LHB 的 10% 权重会移除，其余 90% 分量重归一；报告会明确写明“本周无有效龙虎榜验证”，不把中性分当作机构资金确认。

## 行业热力来源

行业热力优先使用 `ths_akshare`。同花顺请求失败或为空时，周报会尝试独立的 `eastmoney_push2` 行业排行，并在 `meta.industry_source` 保留实际来源。两者均失败时：

- `industry_status` 为 `error` 或 `no_data`；
- `industry_errors` 保留供应商错误；
- 有市场持续性历史时仍可生成周报，但语义降级为历史参考。

东方财富兜底字段与同花顺原始字段并非完全等价，使用时必须保留 `industry_source` 标签。交易时段外、非交易日或供应商尚未更新时，实时行业数据也可能是 `no_data`。

## DNS/HTTPS 排查

以下命令只读取 DNS/HTTPS 连通性和已存在的代理路由，不打印代理用户名、密码或完整 URL 凭据：

```bash
python3 -c 'import socket; hosts=("q.10jqka.com.cn","datacenter-web.eastmoney.com"); print([(h, socket.gethostbyname_ex(h)[2]) for h in hosts])'
curl -fsS -I --max-time 10 https://q.10jqka.com.cn/thshy/index/
curl -fsS -I --max-time 10 https://datacenter-web.eastmoney.com/api/data/v1/get
python3 -c 'import os, urllib.parse; keys=("HTTP_PROXY","HTTPS_PROXY","NO_PROXY"); print({k: (("<set; host={}>".format(urllib.parse.urlsplit(os.environ[k]).hostname or "")) if k != "NO_PROXY" and os.environ.get(k) else os.environ.get(k, "<unset>")[:300]) for k in keys})'
```

若同花顺域名 DNS 失败，按运行环境选择修复 DNS、修正 `NO_PROXY` 路由或让请求经可用代理；代码不会持久化代理配置，也不会写入代理凭据。东方财富兜底只能缓解行业热力故障，不能替代龙虎榜采集任务或板块映射。

## 恢复验证顺序

```bash
python3 .claude/skills/stock-trend/scripts/analysis/lhb_tracker.py --snapshot-only
python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py --weeks 1 --json
python3 .claude/skills/stock-trend/scripts/analysis/weekly_report.py --weeks 1 --html
```

检查 JSON 的 `meta.industry_source`、`meta.lhb_available_days`、`meta.lhb_status_days`，并确认 HTML/Markdown 概览显示相同状态。最后检查：

```bash
git status --short
```

运行结果中的缓存与报告产物应属于预期输出；仓库代码不因一次网络故障修改全局 shell 配置。
