# today-recommendation-evolution 使用说明

`today-recommendation-evolution` 实际对应：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py
```

它不是选股命令，而是“今日推荐”策略的研究、验证、监控和版本发布闭环。

## 推荐使用流程

先完成收盘后的正式流程：

```bash
# 1. 生成市场环境和正式候选推荐
python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py --json
python3 .claude/skills/stock-trend/scripts/scans/daily_candidates.py --json
```

然后运行每日评价：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  close --as-of 2026-09-09 --json
```

`close` 会读取已有的正式推荐快照和历史行情，评价已经成熟的 20 日信号。它不会自动执行市场扫描；缺少正式快照时会保留为 `upstream_gap`，不会伪造数据。

## 每周研究

先用 dry-run 检查输入：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  weekly --as-of 2026-09-09 --dry-run --json
```

确认无误后正式生成诊断和提案：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  weekly --as-of 2026-09-09 --json
```

可选地回放已经登记的实验：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  weekly --as-of 2026-09-09 \
  --experiment-id <实验ID> \
  --json
```

## 监控当前策略

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  monitor \
  --trading-sessions '["2026-09-01","2026-09-02","2026-09-03","2026-09-04","2026-09-07","2026-09-08","2026-09-09"]' \
  --json
```

也可以传 JSON 文件：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  monitor --trading-sessions trading_sessions.json --json
```

监控关注：

- 数据失败率
- 推荐日期覆盖率
- 研究记录完整率
- 20 日成熟超额收益
- 是否需要人工复核或安全恢复

数据不足时会输出 `insufficient_data`，不应解读为策略失败。

## 发布或回滚实验

只有实验已经进入 `eligible`，并完成人工审核后，才发布：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  publish \
  --experiment-id <实验ID> \
  --evidence release_evidence.json \
  --json
```

发生策略版本问题时显式回滚：

```bash
python3 .claude/skills/stock-trend/scripts/analysis/evolution_job.py \
  rollback \
  --evidence '{"summary":"监控发现正式策略退化"}' \
  --json
```

发布和回滚会改变当前策略版本指针，但不会修改既有正式推荐历史。

## 当前环境检查结果

2026-09-09 在本仓库执行 dry-run 的结果：

- `close`：缺少正式推荐快照，状态为 `formal_snapshot_missing`。
- `weekly`：发现 3 个研究快照、684 条候选信号记录。
- `monitor`：状态为 `insufficient_data`，尚未形成足够成熟的监控样本。

详细规范见：

- `.claude/skills/stock-trend/SKILL.md`
- `.claude/specs/today-recommendation-evolution-plan.md`

以上仅用于学习和研究，不构成投资建议。
