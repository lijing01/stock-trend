# 持仓管理 + 回测验证 使用手册

## 概述

本文档涵盖两个功能模块的使用方式：

| 模块 | 触发命令 | 用途 |
|------|----------|------|
| 持仓管理 | `/portfolio` | 记录持仓、跟踪盈亏、止损预警、凯利仓位分析、与 etf-scan 排名对比 |
| 回测验证 | `/etf-backtest` | 验证 Phase 1 速评分模型的预测能力（IC/命中率/收益差） |

---

## 1. 持仓管理 `/portfolio`

### 1.1 数据文件

持仓数据存储在 `.claude/skills/stock-trend/data/portfolio.yaml`，**不纳入版本控制**（已 gitignore）。

首次使用时文件不存在，`/portfolio` 命令会自动创建。

### 1.2 命令列表

#### `list` — 查看全部持仓

```bash
python3 .claude/skills/stock-trend/scripts/portfolio_manager.py list
```

输出字段：
- `code` / `name` — ETF 代码和名称
- `buy_price` / `current_price` — 买入价和实时价（自动获取最新收盘价）
- `pnl_pct` / `pnl_amount` — 浮动盈亏百分比和金额
- `hold_days` — 持仓天数
- `stop_loss` / `targets` — 止损位和目标价
- `summary` — 总成本、总市值、总盈亏

> 当前价格通过 `fetch_kline_eastmoney.py` 获取，盘中 TTL 5 分钟，盘后 16 小时。

#### `add` — 新增持仓

```bash
python3 portfolio_manager.py add \
  --code 513180 \
  --name "恒生科技ETF华夏" \
  --price 1.025 \
  --date 2026-04-15 \
  --qty 2000 \
  --stop-loss 0.95 \
  --targets 1.15,1.25 \
  [--notes "趋势初期建仓"]
```

参数说明：
- `--code`（必填）：6 位 ETF 代码，自动映射市场后缀（159xxx → .SZ, 5xxxxx/15xxxx → .SH）
- `--price`（必填）：买入均价
- `--date`（必填）：买入日期，格式 YYYY-MM-DD
- `--qty`（必填）：持仓数量（份）
- `--name`（可选）：ETF 名称，不传则为空
- `--stop-loss`（可选）：止损价位
- `--targets`（可选）：目标价位，逗号分隔（最多 2 个）
- `--notes`（可选）：备注

#### `remove` — 平仓

```bash
python3 portfolio_manager.py remove --code 513180 [--close-price 1.10] [--close-date 2026-05-17]
```

将持仓标记为 `status: closed`，保留历史记录。不传 `--close-price` 则平仓价留空。

#### `update` — 更新止损/目标

```bash
python3 portfolio_manager.py update \
  --code 513180 \
  --stop-loss 0.93 \
  --targets 1.12,1.22
```

只传需要更新的字段，其余保持不变。

#### `alerts` — 预警检查

```bash
python3 portfolio_manager.py alerts
```

对每笔活跃持仓检查以下条件：

| 条件 | 计算方式 | 严重级别 |
|------|----------|----------|
| 跌破止损 | `当前价 <= 止损价` | `critical` |
| 接近止损 | `(当前价 - 止损价) / 止损价 * 100 < threshold` | `warning` |
| 接近目标 | `(目标价 - 当前价) / 当前价 * 100 < threshold` | `info` |
| 已达目标 | `当前价 >= 目标价` | `info` |
| 时间止损 | 持仓 > 90天且亏损 > 5%，或持仓 > 30天且盈利 < 2% | `warning` |
| 移动止损信号 | 盈利 ≥ 5% 建议上移止损至 MA20；盈利 ≥ 10% 建议追踪回撤 8% | `info` |
| 移动止损触发 | 已上移止损位但现价跌破 MA20 | `critical` |
| 组合回撤 >15% | 组合总市值从高点回撤超 15% | `critical` |
| 组合回撤 >10% | 组合总市值从高点回撤超 10% | `warning` |

`threshold` 默认 3%，可在 `portfolio.yaml` 的 `settings.alert_threshold_pct` 中修改。

#### `status [--skip-scan]` — 持仓总览

```bash
python3 portfolio_manager.py status          # 含 etf-scan 排名对比（耗时 30-60s）
python3 portfolio_manager.py status --skip-scan  # 跳过 scan，仅显示盈亏+预警
```

`status` = `list` + `alerts` + 凯利仓位分析 + scan 对比。scan 对比会运行 `etf_scanner.py --output compact` 获取全量排名，然后对比持仓在排名中的位置：

| 持仓排名情况 | 建议 |
|-------------|------|
| 排名 ≤ top_n 且评分 ≥ 70 | 仍在强势区，继续持有 |
| 评分 ≥ 55 | 评分中等，关注变化 |
| 评分 < 55 或排名靠后 | 考虑减仓 |

#### `kelly` — 凯利公式仓位分析

```bash
python3 portfolio_manager.py kelly
```

对比当前持仓比例与凯利公式计算的最优仓位。输出：

- 每只持仓的凯利最优比例（half-Kelly baseline × 评分乘数 × 波动乘数 × 市场状态乘数 × 反马丁格尔乘数）
- 当前比例与最优比例的偏差及加减仓建议
- 总仓位限制检查（总凯利比例 > 限制时等比例缩放）

判断标准：

| 偏差 | 建议 |
|------|------|
| 当前比例远超凯利建议 | 减仓至建议范围 |
| 当前比例低于凯利建议 | 可适当加仓 |
| 当前比例在凯利范围内 | 维持 |

### 1.3 输出格式示例

```
📁 持仓总览

┌─ 持仓明细 ──────────────────────────────────────────────────┐
│ 代码    名称            买入价   现价    盈亏%    天数  止损  │
│ ─────────────────────────────────────────────────────────── │
│ 513180  恒生科技ETF华夏  1.025   1.089   +6.24%   32d  0.95 │
│ 512880  证券ETF         0.92    0.86    -6.52%   18d  0.85 │
└───────────────────────────────────────────────────────────┘

╔═ 预警 ═══════════════════════════════════════════════════════╗
║ 🔴 [critical] 512880 证券ETF: 现价0.8600距止损0.8500仅1.2% ║
║ 🟡 [warning] 组合回撤: 总市值从高点回撤12.3%，建议减仓       ║
╚══════════════════════════════════════════════════════════════╝

┌─ 与 etf-scan 对比 ─────────────────────────────────────────┐
│ 513180  排名#2/85  评分78  仍在强势区，可继续持有              │
│ 512880  排名#45/85 评分38  排名靠后/评分偏低，考虑减仓          │
└───────────────────────────────────────────────────────────┘

┌─ 凯利仓位分析 ─────────────────────────────────────────────┐
│ 513180 当前20% 凯利22%[18-26] ✓ 维持                        │
│ 512880 当前15% 凯利 8%[ 4-12] ↑ 远超凯利建议，减仓             │
│ 总仓位: 当前35% 凯利建议30%(超限等比例缩放)                    │
└───────────────────────────────────────────────────────────┘
```

---

## 2. 回测验证 `/etf-backtest`

### 2.1 背景

etf-scan 的 Phase 1 速评分模型由 5 个维度组成（momentum 30%、volume 20%、capital_flow 20%、shares_trend 15%、iopv 15%）。

历史数据可用性：

| 维度 | 历史数据 | 说明 |
|------|----------|------|
| momentum | 有 | K 线派生 |
| volume | 有 | K 线派生 |
| shares_trend | 有 | 从 Data_flvol pingzhongdata 回填 |
| capital_flow | 无 | 仅实时 |
| iopv | 无 | 仅实时 |

capital_flow 和 iopv 评分时为 None，权重自动重新分配至有数据的维度。

### 2.2 基本用法

```bash
# 默认回测（120 个交易日，约半年）
python3 .claude/skills/stock-trend/scripts/backtest_engine.py

# 缩短回测区间
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --lookback-days 60

# 只回测单只 ETF
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --etf 513180

# 只回测指定板块
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --focus 科技

# 指定评估窗口
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --eval-windows 5,10,20

# 输出到文件
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --output /tmp/backtest.json

# 输出 JSON + HTML 报告
python3 .claude/skills/stock-trend/scripts/backtest_engine.py --output /tmp/backtest --output-html
```

### 2.3 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--lookback-days` | 120 | 回测天数（交易日），东方财富最多 250 根 K 线 |
| `--eval-windows` | 5,10,20 | 评估窗口（天），逗号分隔，对应短期/中期/长期 |
| `--top-n` | 10 | 每日取 top N ETF 计算命中率和收益 |
| `--sample-interval` | 5 | 采样间隔（交易日），避免数据点过密 |
| `--focus` | 全部 | 只回测指定板块，值对应 watchlist 分类名称 |
| `--etf` | 全部 | 只回测单只 ETF（6 位代码） |
| `--output` | stdout | JSON 输出文件路径 |
| `--output-html` | 否 | 同时输出 HTML 报告（与 `--output` 同路径） |

### 2.4 输出指标解读

#### IC（Information Coefficient，信息系数）

衡量评分与未来收益的秩相关性（Spearman rank correlation）：

```
IC 均值     = 全样本期内，每日评分与未来 N 日收益的 Spearman rho 的平均值
IC 标准差   = IC 序列的标准差，衡量稳定性
t 值        = mean_IC / (std_IC / sqrt(N))，判断 IC 是否显著非零
正向比率    = IC > 0 的天数占比
```

判断标准：
- **IC 均值 > 0.05 且 |t 值| > 1.96（5% 显著性）**：该维度有统计显著的预测力
- **正向比率 > 55%**：模型方向判断优于随机（50%）

#### 命中率

```
hit_rate = top-N 中未来 N 日收益为正的比例
- > 55%: 优于随机
- > 60%: 有实用价值
```

#### Top vs Bottom 收益差

```
spread = top-N 平均收益 - bottom-N 平均收益
- > 2%: 选股有经济意义（10 日窗口）
- > 3%: 区分度良好
```

### 2.5 输出格式示例

```
📊 ETF 速评分回测报告    2026-05-17T15:30:00

▸ 回测区间: 120个交易日
▸ 采样日期: 24个 (2026-01-15 ~ 2026-05-10)
▸ 测试ETF: 85只
▸ 数据限制: capital_flow/iopv 仅实时，shares_trend 已回填，验证 momentum+volume+shares_trend

┌─ IC (信息系数) ────────────────────────────────────────────────┐
│ 维度/窗口    IC均值   标准差    t值   正向比率   说明              │
│ ─────────────────────────────────────────────────────────────── │
│ quick_score-5d  0.071  0.13   2.72   68%        ★ 显著有预测力  │
│ quick_score-10d 0.058  0.14   2.01   62%        ★ 有预测力      │
│ quick_score-20d 0.042  0.15   1.35   58%                         │
└────────────────────────────────────────────────────────────────┘

命中率:
  Top10-5d:  62% (优于随机)
  Top10-10d: 58%
  Top10-20d: 55%

收益分布 (Top10):
          均值     中位数    标准差   最小    最大
  5日:    +1.2%   +0.8%    2.5%   -4.0%   +8.0%
  10日:   +1.8%   +1.2%    3.5%   -6.0%   +12.0%
  20日:   +2.5%   +1.8%    5.0%   -8.0%   +18.0%

Top vs Bottom 收益差:
  5日:   2.4%
  10日:  3.2%
  20日:  4.8%
```

### 2.6 注意事项

1. **数据限制**：回测覆盖 momentum + volume + shares_trend 三个维度。capital_flow 和 iopv 无历史数据（评分时权重自动重分配）。使用全部 5 个维度的实际效果可能优于回测，也可能因实时维度的噪声而劣化
2. **幸存者偏差**：回测只包含当前在 watchlist 中的 ETF，已退市或清盘的 ETF 不在池中
3. **样本内 vs 样本外**：回测使用全部历史数据，未区分训练集和测试集，IC 可能偏高
4. **交易成本未计入**：回测未考虑佣金、印花税、冲击成本

---

## 3. 常见问题

### Q: portfolio.yaml 不小心提交到 git 了怎么办？

```bash
# 确保 .gitignore 已包含 portfolio.yaml
echo ".claude/skills/stock-trend/data/portfolio.yaml" >> .gitignore
# 从 git 跟踪中移除（不删除本地文件）
git rm --cached .claude/skills/stock-trend/data/portfolio.yaml
git commit -m "chore: 停止跟踪 portfolio.yaml"
```

### Q: 回测时部分 ETF 数据为 0？

每只 ETF 至少需要 20 根 K 线才有意义，少于 20 根会被跳过。如果大量 ETF 被跳过，可以:
- 检查网络连接（东方财富 API 是否可用）
- 缩小 --lookback-days（避免超出数据范围）
- 用 `--etf` 参数单测特定 ETF

### Q: list 命令显示 current_price 为 null？

原因：`fetch_kline_eastmoney.py` 请求失败或超时。
排查：
```bash
# 直接测试 K 线获取
python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py 513180.SH
```
如果返回错误，检查网络连接或稍后重试（盘中 5 分钟 TTL，盘后 16 小时）。

### Q: status 命令的 scan 对比太慢？

`status` 会运行全量 etf-scan（约 30-60 秒）。如果只需要盈亏和预警：
```bash
python3 portfolio_manager.py status --skip-scan
```
或者先用 `list` + `alerts` 分步查看。
