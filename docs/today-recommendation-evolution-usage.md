# “今日推荐”统一入口使用说明

运行要求：Python >=3.10，工作目录为仓库根目录。本文命令中的 `python3` 指满足该要求的解释器。本环境默认 `python3` 为 3.9.6，Agent 必须选用已安装的 `/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3` 执行下列命令，无需修改全局环境。

用户说“今日推荐”时，直接运行统一入口（本环境可执行命令）：

```bash
/Users/jing.li7/.pyenv/versions/3.10.0/bin/python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --json
```

该命令在本次调用中依次完成市场环境刷新、候选扫描、历史推荐评价、每周研究和策略监控；不安装后台定时器，不向外部服务推送通知，也不自动打开 GUI 或浏览器。

## 日常使用

候选扫描的常用参数可以直接透传：

```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py \
  --top 30 --min-candidates 20 --no-html --json
```

只查看执行计划，不联网、不写文件：

```bash
python3 .claude/skills/stock-trend/scripts/bridge/run_today.py --dry-run --json
```

`--json` 保留原候选扫描的 JSON 字段，并追加：

- `workflow`：各阶段的执行、跳过、失败和恢复结果。
- `notifications`：本次调用以及跨调用检测到的策略版本或参数变化；首次建立基线不报“变更”。`invalid_pointer_fallback` 等安全回退也会明确通知。

调用者应在当前对话中醒目呈现 `notifications`；它们不是短信、邮件或其他外部推送。

## 执行规则

1. 先刷新 `market_regime`，成功后才运行 candidates。市场刷新失败时停止候选扫描，不得沿用陈旧市场上下文继续推荐。
2. 交易日期从仓库已有的权威交易日历解析，统一使用上海时区。15:10 前评价上一已完成交易日；15:10 后可评价当天；休市日使用最近已知完成交易日。日历只提供历史区间时，仍评价已知区间，并输出 `calendar.status=historical_only` 与 `coverage_end`；未覆盖日期不直接解释为休市。
3. 按评价日期所属 ISO 周执行 weekly；只有同周已有成功且 `input.research_snapshots > 0` 的有效任务记录才跳过。失败或没有研究样本的成功空跑，都允许同周后续调用再次尝试。
4. monitor 使用真实交易日。交易日历缺失时明确跳过依赖交易日的后处理，不把自然日伪装成交易日。
5. monitor 遇到接口或契约异常时可按现有机制安全恢复到已验证策略，并在 `workflow` 和 `notifications` 中说明；普通统计退化只标记人工复核，不自动恢复。
6. publish 始终要求显式人工审核，不由“今日推荐”自动执行。

`/candidates` 仍是独立候选扫描入口，适合只需要候选结果、不运行评价、周研究和监控时使用。

## 高级研究与发布 CLI

日常无需手动串联以下命令；研究排障、实验回放和人工发布时可直接使用 `evolution_job.py`。

每日评价：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  close --as-of 2026-09-09 --json
```

每周研究或实验回放：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  weekly --as-of 2026-09-09 --dry-run --json

python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  weekly --as-of 2026-09-09 --experiment-id <实验ID> --json
```

手动监控：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  monitor --trading-sessions trading_sessions.json --json
```

数据不足返回 `insufficient_data` 时，不应直接解读为策略失败。

只有实验已进入 `eligible` 且完成人工审核后，才可显式发布。`release_evidence.json` 必须是通过既有 `verify_release_evidence` 验证的完整 `evolution-release-evidence/v2` 证据，且引用的验证、留出和影子结果须能从受控存储解析并通过校验。不得用任意 `summary/reviewer/checks` 摘要代替完整证据。契约以 `scripts/core/evolution_registry.py` 的 `verify_release_evidence` 及 `tests/test_evolution_job.py` 的发布测试为准（路径均相对 `.claude/skills/stock-trend/`）。

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  publish --experiment-id <实验ID> --evidence release_evidence.json --json
```

需要显式回滚时，`--evidence` 接收纯文本原因，CLI 将其作为 `summary`，不会读取文件：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  rollback --evidence '监控发现策略异常，人工回滚' --json
```

发布和回滚会改变当前策略版本指针，但不会修改既有正式推荐历史。

详细契约见 `.claude/skills/stock-trend/SKILL.md` 和 `.claude/specs/today-recommendation-evolution-plan.md`。

**本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。**
