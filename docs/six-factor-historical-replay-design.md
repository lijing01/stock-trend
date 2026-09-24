# 六维权重历史回归测试补齐方案

**状态**：设计方案（未实现）  
**日期**：2026-09-21  
**目标**：对以下固定权重进行可复现、无未来函数的 A 股历史回归测试：

| 维度 | 权重 |
|---|---:|
| 动量 | 25% |
| 量价 | 15% |
| 资金 | 15% |
| 基本面 | 10% |
| 板块强度 | 10% |
| 维科夫结构 | 25% |

## 1. 当前基础与边界

现有六维合成公式已在
`.claude/skills/stock-trend/scripts/scans/stock_scanner.py:1949` 实现，并在扫描结果中保存
`raw_dimensions`、`raw_composite_score` 和 `quality_adjusted_score`。

现存能力不能直接替代历史六维回测：

- `analysis/factor_ablation.py` 只对已经保存的正式研究快照做逐因子移除；
- `analysis/recommendation_attribution.py` 能做交易归因，但输入是已有推荐快照。

此前的维科夫事件回放和 ETF 排名回测入口已移除，不作为本方案的实现基础。

因此需要补一条独立链路：

```text
历史数据仓
  → as_of 股票池
  → point-in-time 六维输入
  → 纯评分器
  → 门控与排名
  → 信号/组合构建
  → 前向收益与交易归因
  → 样本外统计报告
```

## 2. 历史数据契约

每个数据集必须同时记录“数据对应日期”和“最早可见日期”：

```json
{
  "security": "600519.SH",
  "data_date": "2024-06-28",
  "available_at": "2024-06-28T15:30:00+08:00",
  "known_at": "2024-06-28T16:00:00+08:00",
  "source": "provider_name",
  "source_version": "v1",
  "payload": {}
}
```

回放日期为 `as_of` 时，任何记录必须满足：

```text
available_at <= as_of 决策时刻
```

财报、估值和盈利增速使用公告/披露时间，而不是报告期末时间；修订数据必须保留版本，禁止用今天的最新值回填过去。

建议使用不入 Git 的本地历史仓：

```text
data/history/v1/
  manifest.json
  calendar/
  universe/
  prices/
  capital_flow/
  fundamentals/
  sectors/
  memberships/
  corporate_actions/
```

第一版可使用 SQLite 或分区 JSONL，避免为存储格式新增依赖。每次导入生成 manifest，记录覆盖区间、数据行数、缺口、来源和内容 hash。

## 3. 必须补齐的数据

### 3.1 历史股票池

按交易日保存当时可投资的证券集合和状态：

- 上市/退市日期；
- ST、*ST、暂停上市状态；
- 停牌状态和成交量；
- 代码变更；
- 当日涨跌停规则及价格；
- 历史板块成分，而不是当前成分倒灌。

这一步用于避免幸存者偏差，也决定某个候选在当日是否有资格进入评分和交易。

### 3.2 六维输入

- **行情**：日 OHLCV、成交额、复权因子、分红送配/拆并股；
- **资金**：主力净流入及其历史明细，禁止以当前数据或 K 线估算值冒充历史真实资金流；
- **基本面**：PE/PB、ROE、利润/营收增速、报告期、公告日、修订版本；
- **板块**：每日完整板块排行、热度、相对强弱、持续性和成分快照；
- **维科夫**：只用 `as_of` 前的 K 线重新计算，不读取未来确认事件；
- **基准**：沪深 300 及需要的行业基准行情。

## 4. 抽取纯 `as_of` 评分器

将扫描器中的网络访问、缓存读取与纯计算拆开，新增类似接口：

```python
score_as_of(
    as_of,
    universe,
    kline_by_code,
    capital_by_code,
    fundamentals_by_code,
    sector_snapshot,
    memberships,
    policy,
) -> ScoreSnapshot
```

该函数必须：

1. 只接受已经截断到 `as_of` 的输入；
2. 复用生产评分函数，不复制另一套阈值；
3. 输出六个原始维度分、权重贡献、综合分、质量调整分、门控原因和排名；
4. 记录策略版本、输入 manifest hash 和评分器版本；
5. 遇到缺失数据时沿用生产数据质量规则，不能静默补成可用样本。

研究快照结构可参考
`.claude/skills/stock-trend/scripts/core/candidate_research_snapshot.py`，但历史回放必须额外保存完整股票池和所有未入选股票的评分证据。

## 5. 新增历史重放入口

建议新增：

```text
.claude/skills/stock-trend/scripts/backtesting/six_factor_replay.py
```

接口示例：

```bash
python3 .claude/skills/stock-trend/scripts/backtesting/six_factor_replay.py \
  --start 2022-01-01 \
  --end 2025-12-31 \
  --rebalance weekly \
  --top-n 10 \
  --holding-days 20 \
  --data-root data/history/v1 \
  --policy six-factor/v1 \
  --json
```

每个调仓日执行：

1. 从历史股票池构建当日可投资宇宙；
2. 截断行情、资金、基本面、板块和维科夫输入；
3. 重算六维分数；
4. 应用质量因子、维科夫门控、板块门槛和买点奖励；
5. 排序并记录 Top N、分数分解和未入选原因；
6. 按明确定义的入场规则生成信号；
7. 计算 5/10/20/60 个交易日的前向结果。

第一阶段先做“因子预测力回测”：次日开盘或 VWAP 入场、固定持有期、等权组合。第二阶段再加入止损、目标、T+1、涨跌停、停牌、滑点、佣金和印花税，避免把选股能力和执行规则混为一个结果。

## 6. 统计输出

每个维度和综合分都应输出：

- 截面 Spearman IC、IC 均值/标准差/IR；
- 分位数组合收益；
- Top-vs-Bottom 收益差；
- 命中率、平均收益、收益分布；
- 沪深 300 超额收益；
- 换手率、最大回撤、成本前后收益；
- 各日期、各证券的数据覆盖率和排除原因。

时间切分采用滚动或固定的 train/validation/test，最终样本外区间不得参与参数选择。持有期重叠时使用 purge 和交易日区块 bootstrap，不能把重叠交易简单视为独立样本。

## 7. 回归测试与验收门槛

至少增加以下测试：

- 冻结输入下，历史评分器与生产评分器逐维分、综合分、质量调整分一致；
- 向 `as_of` 之后插入更优行情、基本面或资金数据，历史结果不变化；
- 财报公告前不得进入基本面评分；
- 退市、ST、停牌和涨跌停按当时状态处理；
- 公司行为前后收益口径连续且可解释；
- 缺失因子不会静默提升资格；
- 相同 manifest、策略版本和日期范围重复运行，输出 hash 一致；
- 历史股票池、因子输入、排名和交易结果都可通过 record/hash 追溯。

建议第一验收样本使用一个完整年度、每周调仓、Top 10；确认链路和防未来函数测试通过后，再扩展到 3–5 年和全市场。

## 8. 实施顺序

1. 定义 schema、manifest、可见性规则和缺失状态；
2. 先积累/导入一段完整历史数据；
3. 抽取纯评分器并做生产/回放一致性测试；
4. 实现 `six_factor_replay.py` 和无成本预测力报告；
5. 接入交易执行与成本模型；
6. 加入样本外、purge、bootstrap 和回归金样例；
7. 扩大历史区间，最后才评估是否调整六维权重。

在历史数据契约、股票池和 `as_of` 重算器完成前，任何权重结论都只能视为快照消融结果，不能视为历史回归结论。

> 本文是工程研究设计，不构成投资建议。
