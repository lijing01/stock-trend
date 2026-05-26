# 交易日志与复盘系统 — 设计 Spec

## 目标

建立交易纪律闭环：记录操作 → 识别模式 → 修正行为 → 验证改进。同时追踪 AI 建议准确性，量化各维度预测价值。

---

## 1. 数据模型

### 1.1 交易记录 (TradeRecord)

```yaml
id: "T20260526-001"
code: "513180"
name: "恒生科技ETF"
direction: "buy"       # buy | sell
price: 0.850
qty: 10000
date: "2026-05-26"     # 买入日期
reason: "抄底"          # 抄底/追高/突破/止损/止盈/调仓/定投
expected_days: 60       # 预期持仓天数
stop_loss: 0.780
target: 0.950
tags: []               # 自动标注的错误模式，如 fomo, bag_holding
```

### 1.2 平仓记录 (CloseRecord)

```yaml
trade_id: "T20260526-001"
close_date: "2026-06-20"
close_price: 0.920
pnl: 700.0             # 盈亏金额
pnl_pct: 8.24          # 盈亏百分比
hold_days: 25
tags: ["early_exit"]    # 自动检测
```

### 1.3 AI 建议快照 (AIRecommendation)

```yaml
id: "AI20260526-001"
code: "002371"
name: "北方华创"
source: "longtou"       # stock-trend | longtou | etf-scan
date: "2026-05-26"
direction: "bullish"    # bullish | bearish | neutral
score: 2.5
dimensions:
  technical: 1.5
  capital_flow: 1.0
  fundamental: 0.8
  sentiment: 1.0
  macro: 0.5
stop_loss: 285.0
targets: [310.0, 335.0]
report_path: "reports/002371/20260526-1030.md"

# N 日后回填（自动更新）
outcome:
  filled: false          # true 后才有以下字段
  n_day_return: 3.5     # 5 日后涨跌幅 %
  hit_target: false
  hit_stop_loss: false
  direction_correct: true  # 预测方向是否正确
  eval_date: "2026-06-02"
```

### 1.4 复盘报告 (ReviewReport)

```yaml
period: "2026-W21"       # ISO 周
generated_at: "2026-05-26"

trades:
  total: 8
  win_count: 5
  loss_count: 3
  win_rate: 62.5
  total_pnl: 3200.0
  avg_hold_days: 18
  avg_win_pnl: 850.0
  avg_loss_pnl: -420.0
  profit_factor: 1.7
  max_drawdown: -5.2

error_patterns:
  - pattern: "fomo"
    count: 2
    total_pnl: -350
    detail: "追高后回调，平均持仓5天即止损"
  - pattern: "early_exit"
    count: 1
    total_pnl: 120
    detail: "卖出后继续上涨，少赚约300"

ai_accuracy:
  total_recommendations: 12
  correct: 8
  wrong: 3
  pending: 1
  hit_rate: 72.7
  by_dimension:
    technical: {correct: 8, wrong: 2, hit_rate: 80.0}
    capital_flow: {correct: 6, wrong: 3, hit_rate: 66.7}
    fundamental: {correct: 5, wrong: 2, hit_rate: 71.4}
    sentiment: {correct: 4, wrong: 4, hit_rate: 50.0}
    macro: {correct: 3, wrong: 2, hit_rate: 60.0}

recommendations:
  - 减少追高操作，追高交易亏损率 100%
  - 中军股持仓周期偏短，建议延长至 30+
  - 情绪面评分预测力弱，可降低权重参阅
```

---

## 2. 错误模式检测规则

| 规则名 | 触发条件 | 严重度 |
|--------|---------|--------|
| `fomo` | 买入前连续≥3根阳线 | warning |
| `early_exit` | 持仓<3天 + 正收益 | warning |
| `bag_holding` | 持仓>20天 + 浮亏>10% | critical |
| `revenge_trade` | 亏损后2天内新开仓 | critical |
| `ignored_stop` | 股价触止损+延后>3天卖出 | critical |
| `over_concentrated` | 单票仓位>组合20% | warning |
| `chasing_high` | 买入价距10日高点<3% | warning |

---

## 3. 文件存储

```
.claude/skills/stock-trend/data/trade_journal/
├── trades.json           # 所有交易记录
├── ai_recommendations.json  # AI建议快照
└── reviews/
    └── 2026-W21.json     # 每周复盘
```

---

## 4. 命令接口

| 命令 | 功能 | 实现方式 |
|------|------|---------|
| `/trade add --code X --direction buy --price X --qty X --reason "..." [--note "..."]` | 记录交易 | `trade_journal.py add` |
| `/trade close --id X --price X [--note "..."]` | 平仓（自动关联 portfolio remove） | `trade_journal.py close` |
| `/trade list [--open] [--code X]` | 查看交易历史 | `trade_journal.py list` |
| `/trade-review [--period weekly\|monthly]` | 生成复盘报告 | `trade_journal.py review` |
| `/trade-stats` | 快速胜率 | `trade_journal.py stats` |

---

## 5. 自动集成

### 5.1 Portfolio 联动

`portfolio_manager.py remove` 执行时 → 调用 `trade_journal.py auto-close --code X --close-price X`，自动匹配未平仓记录并填平仓信息。

### 5.2 AI 建议自动存档

stock-trend skill 的 Step 4（生成报告）和 longtou 流程结束前，追加一步：

```python
python3 trade_journal.py save-ai-rec \
  --code <code> --source <stock-trend|longtou> \
  --direction <bullish|bearish|neutral> --score <总分> \
  --dimensions '{"technical":1.5,...}' \
  --stop-loss <价> --targets '[310,335]' \
  --report-path <path>
```

### 5.3 AI 建议回填（效果评估）

独立定时任务 /trade-review 内部：

1. 遍历 `ai_recommendations.json` 中 `outcome.filled == false` 且 `date` 距今 ≥ N 日（ETF/指数5日，个股10日）
2. 读取 K线数据计算 N 日涨跌幅
3. 回填 `direction_correct`、`hit_target`、`hit_stop_loss`

### 5.4 每周复盘

- 每周一通过 cron job 自动触发 `/trade-review --period weekly`
- 或手动执行 `/trade-review`

---

## 6. 技术选型

- Python 3，单一脚本 `trade_journal.py`
- JSON 文件存储（与 portfolio yaml 分开，避免混淆）
- 无外部依赖，使用标准库 `json`, `datetime`, `argparse`
- 错误模式检测逻辑封装在 `TradeAnalyzer` 类

---

## 7. 实现计划

### Phase 1: 核心功能

1. 实现 `trade_journal.py`：`add` / `close` / `list` / `stats` 子命令
2. 实现自动错误模式检测
3. 实现 `/trade` 命令路由
4. 写单元测试

### Phase 2: AI建议追踪

5. 实现 `save-ai-rec` / `backfill-outcomes` 子命令
6. 集成 stock-trend 和 longtou skill（输出前追加存档步骤）
7. 写 golden snapshot 测试

### Phase 3: 复盘报告

8. 实现 `review` 子命令，生成结构化复盘报告
9. 集成 portfolio_manager.py，实现平仓联动
10. 配置每周 cron job

---

## 免责声明

本系统仅用于个人交易纪律训练和策略复盘，不构成投资建议。
