# Plan: P0-1 持仓管理 + P0-3 回测验证

## Context

上班族用 etf-scan 选 ETF，但买了之后无持仓跟踪、无止损提醒、无模型验证。两个功能补齐"买入后管理"和"模型可信度"两个核心缺口。

---

## Phase A: 持仓管理 (`/portfolio`)

### A1. 数据文件

**新建** `.claude/skills/stock-trend/data/portfolio.example.yaml`（提交到 repo，模板）
**新建** `.claude/skills/stock-trend/data/portfolio.yaml`（用户数据，gitignore）

Schema:
```yaml
holdings:
  - code: "513180"
    ts_code: "513180.SH"
    name: "恒生科技ETF华夏"
    buy_price: 1.025
    buy_date: "2026-04-15"
    quantity: 2000
    stop_loss: 0.95
    targets: [1.15, 1.25]
    notes: "趋势初期建仓"
    status: "active"        # active | closed
    close_price: null
    close_date: null

settings:
  alert_threshold_pct: 3.0
  default_stop_loss_pct: 5.0
```

**修改** `.gitignore` — 添加 `data/portfolio.yaml`

### A2. 核心脚本 `portfolio_manager.py`

路径: `.claude/skills/stock-trend/scripts/portfolio_manager.py`

CLI:
```
python3 portfolio_manager.py list                    # 全部持仓 + 浮动盈亏
python3 portfolio_manager.py add --code 513180 --price 1.025 --date 2026-04-15 --qty 2000
python3 portfolio_manager.py remove --code 513180 [--close-price 1.10] [--close-date 2026-05-17]
python3 portfolio_manager.py update --code 513180 [--stop-loss 0.95] [--targets 1.15,1.25]
python3 portfolio_manager.py status                  # 盈亏 + 预警 + scan 对比
python3 portfolio_manager.py alerts                  # 仅预警
```

所有命令输出 JSON 到 stdout。

关键逻辑:
- **浮动盈亏**: 调用 `fetch_kline_eastmoney.py` 获取最新收盘价（subprocess，与 etf_scanner 模式一致）
- **预警**: 3 种条件 — 接近止损(critical)、突破止损(critical)、接近目标(info)
- **scan 对比**: 调用 `etf_scanner.py --no-deep` Phase 1，对比持仓在当前排名中的位置

### A3. SKILL.md 新增 `/portfolio` 触发段

在 `/etf-scan` 段之后添加，定义 3 步执行流程：
1. 运行 `portfolio_manager.py <command>`
2. 解析 JSON 呈现结果（表格 + 预警高亮）
3. `status` 命令时额外跑 etf-scan Phase 1 做排名对比

### A4. 测试 `tests/test_portfolio.py`

沿用 custom harness 模式（`test()` 函数 + `run_script()`）：

- TPF-01~05: 增删改 + alert 逻辑单元测试
- TPF-06~08: P&L 计算 + summary + 空组合边界
- TPF-I01~I03: subprocess 集成测试（add+list roundtrip, status）

---

## Phase B: 回测验证 (`/etf-backtest`)

### B1. 核心脚本 `backtest_engine.py`

路径: `.claude/skills/stock-trend/scripts/backtest_engine.py`

CLI:
```
python3 backtest_engine.py [--etf <code>] [--focus <板块>] [--lookback-days 120] [--eval-windows 5,10,20] [--top-n 10] [--sample-interval 5] [--output-html]
```

核心算法:
1. 批量拉取 watchlist 全部 ETF 的 K 线（最多 250 根，足够覆盖 120+20 天）
2. 对每个采样日期，切片 K 线到该日，直接 import etf_scanner 的纯函数跑 Phase 1 评分:
   - `score_momentum`, `score_volume`, `compute_quick_score`, `normalize_scores_by_cohort`
3. 计算各评估窗口的实际前向收益
4. 计算 IC（Spearman rank correlation，手动实现，不引入 scipy）
5. 计算命中率 + Top vs Bottom 收益差

数据限制处理:
- `capital_flow`, `shares_trend`, `iopv` 无历史数据 → 这几个维度返回 None
- `compute_quick_score` 已有权重重分配逻辑（None 维度权重按比例分给其他维度）
- 输出 `meta.data_notes.degraded_dims` 标注限制范围
- 回测主要验证 **momentum + volume** 两个有历史数据的维度

输出 JSON 结构:
- `meta`: 回测参数 + 数据限制说明
- `summary.ic_by_dimension`: 各维度 IC（mean, std, t_stat, p_value, significant）
- `summary.hit_rate`: Top-N 各窗口命中率
- `summary.return_distribution`: Top-N 和 Bottom-N 收益分布
- `summary.top_vs_bottom_spread`: 多空收益差
- `per_date`: 每个采样日的 top/bottom 明细
- `per_etf`: 每只 ETF 的汇总统计

### B2. SKILL.md 新增 `/etf-backtest` 触发段

3 步流程:
1. 运行 `backtest_engine.py`
2. 解析 JSON 呈现 IC 表格 + 命中率 + 收益差
3. 解读说明（IC > 0.05 且 p < 0.05 有预测力，命中率 > 55% 优于随机，差 > 2% 有经济意义）

### B3. 测试 `tests/test_backtest.py`

- TBT-01~05: K 线切片、前向收益、IC 计算、命中率、收益差单元测试
- TBT-06~08: 降级处理、采样间隔、边界情况
- TBT-I01~I02: 小规模集成测试（3 ETF, 20 天 lookback）

---

## Phase C: 集成

- 修改 `scripts/test_stock_trend.py` — 添加 portfolio 和 backtest 测试套件调用
- 最终全量测试: `test_stock_trend.py` + `test_golden.py --diff`

---

## 实施顺序

```
A1 数据文件 + gitignore
A2 portfolio_manager.py
A3 SKILL.md /portfolio 段
A4 test_portfolio.py
—— 验证 A ——
B1 backtest_engine.py
B2 SKILL.md /etf-backtest 段
B3 test_backtest.py
—— 验证 B ——
C  集成到 test_stock_trend.py
—— 全量验证 ——
```

## 关键文件清单

| 操作 | 文件 |
|------|------|
| 新建 | `.claude/skills/stock-trend/data/portfolio.example.yaml` |
| 新建 | `.claude/skills/stock-trend/scripts/portfolio_manager.py` |
| 新建 | `.claude/skills/stock-trend/scripts/backtest_engine.py` |
| 新建 | `.claude/skills/stock-trend/tests/test_portfolio.py` |
| 新建 | `.claude/skills/stock-trend/tests/test_backtest.py` |
| 修改 | `.gitignore` |
| 修改 | `.claude/skills/stock-trend/SKILL.md` |
| 修改 | `.claude/skills/stock-trend/scripts/test_stock_trend.py` |
| 复用 | `etf_scanner.py` — import score_momentum/score_volume/compute_quick_score/normalize_scores_by_cohort |
| 复用 | `fetch_kline_eastmoney.py` — subprocess 获取最新价格 |
| 复用 | `cache_utils.py` — 市场感知缓存 |
| 复用 | `watchlist.yaml` — ETF 宇宙 |

## 验证方式

1. `python3 .claude/skills/stock-trend/scripts/test_stock_trend.py` — 全过
2. `python3 .claude/skills/stock-trend/tests/test_golden.py --diff` — 无 fail
3. `python3 .claude/skills/stock-trend/tests/test_portfolio.py` — 全过
4. `python3 .claude/skills/stock-trend/tests/test_backtest.py` — 全过
5. 手动: `/portfolio add --code 513180 --price 1.025 --date 2026-05-01 --qty 2000` → `/portfolio list` → `/portfolio status`
6. 手动: `/etf-backtest --lookback-days 30` 确认输出 IC 表格