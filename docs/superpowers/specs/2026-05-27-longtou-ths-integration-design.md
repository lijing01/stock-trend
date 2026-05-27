# 同花顺数据集成 — longtou 优化设计

## 概述

保持东方财富 (East Money) 为 backbone，新增同花顺 DDX/龙虎榜数据作为增强维度。两个数据源均为"加分项"非"依赖项"——获取失败时回退到当前逻辑。

## 问题诊断

现有 longtou 三阶段 pipeline 的弱环：

| Phase | 当前方法 | 问题 |
|---|---|---|
| 1. 热点板块扫描 | East Money 板块涨幅+资金+涨跌比 | 无新闻/舆情信号，可接受 |
| 2. 龙头/中军筛选 | 涨幅×50% + 成交额×30% | 无法区分资金真伪，纯价格驱动 |
| 3. 深度分析 | 5维度 scoring pipeline | sentiment 维度信号弱（仅北向/两融代理） |

**核心痛点**: Phase 2 龙头选中后，无法判断"这波拉升是机构建仓还是游资一日游"。同花顺 DDX 和龙虎榜正好补这个。

## 架构

### 数据流

```
Phase 1 (无变化)
  East Money 板块扫描 → 10个热点板块

Phase 2 (增强 leader 评分)
  原逻辑: 50%涨幅 + 30%成交额
  增强: 30%涨幅 + 20%成交额 + 30%DDX + 20%超级资金
          ↓
  新增 fetch_ddx.py: 获取 DDX/DDY/DDZ/超级资金占比

Phase 3 (龙虎榜风险修正)
  原 pipeline + scoring
          ↓
  新增 fetch_longhubang.py: 对候选股查龙虎榜
  结果注入 sentiment 维度 & risk 列表
```

### 降级策略

- DDX 获取超时 10s → 跳过，用原分
- 龙虎榜获取超时 15s → 跳过，不加额外风险项
- 两个模块独立失败，互不影响

## 新增文件

### 1. `fetch_ddx.py` — DDX & 超级资金

**数据源**: 同花顺 DDE 排行公开接口

```
GET http://data.10jqka.com.cn/financial/ddx/opendata/
```

**输出格式**:
```json
{
  "002415": {
    "ddx": 0.873,
    "ddx_days": 5,
    "ddy": 0.234,
    "ddz": 18.5,
    "super_order_ratio": 0.12,
    "fetch_time": "2026-05-27 15:30"
  }
}
```

### 2. `fetch_longhubang.py` — 龙虎榜

**数据源**: 同花顺龙虎榜公开数据

```
GET http://data.10jqka.com.cn/financial/longhubang/
```

**输出格式**:
```json
{
  "002415": {
    "is_on_board": true,
    "net_buy_total": 12500000,
    "buy_seats": [{"name": "机构专用", "amount": 50000000, "type": "institution"}],
    "sell_seats": [],
    "has_institution_buy": true,
    "has_institution_sell": false,
    "has_floating_capital": true,
    "floating_capital_net_buy": false,
    "retail_dominated": false,
    "risk_level": "low"
  }
}
```

## 修改文件

### 1. `fetch_sector_data.py` — `filter_leaders()`

```
旧: leader_score = change_score * 0.50 + amount_score * 0.30
新: leader_score = change_score * 0.30 + amount_score * 0.20
                  + ddx_score * 0.30 + super_order_score * 0.20
```

DDX 评分锚点:
- ddx ≥ 0.5 + 连续红柱 ≥ 3天 → ddx_score = 100 (资金持续布局)
- ddx ≥ 0.2 → ddx_score = 80
- ddx < 0 → ddx_score = max(0, 50 + ddx * 100) (负值压分)

超级资金评分锚点:
- super_order_ratio ≥ 15% → 100 (机构主导)
- super_order_ratio ≥ 8% → 80
- super_order_ratio < 5% → 50 (散户特征)

### 2. `compute_scores.py` — sentiment 维度

龙虎榜信号注入 sentiment score:

| 信号 | 调整 |
|---|---|
| 机构净买入 ≥ 2家 | sentiment +0.8 |
| 机构净卖出 ≥ 2家 | sentiment -1.0 |
| 纯游资主导,无机构 | sentiment -0.3 |
| 散户主导买入 | sentiment -1.0 |
| 游资净买入+机构净卖出 | sentiment -0.5 (分歧) |
| 上榜但机构交易额 < 20% | sentiment -0.3 |

### 3. `market_leader.py` — 编排

Phase 2 → Phase 3 之间插入:

```
1. Phase 2 完成: sectors_analyzed 含 leaders/cores
2. 收集所有 candidates 列表
3. 并行调用 fetch_ddx() (ThreadPool, max_workers=4, timeout=10s)
4. 将 DDX 数据 attach 到每个 candidate
5. Phase 2 filter_leaders() 读取 DDX 数据重算 leader_score
6. 裁剪最终 leaders 列表 (有 DDX 负值的降级)
7. 并行调用 fetch_longhubang() 查龙虎榜
8. 龙虎榜数据缓存到 pipeline_summary
9. 进入原 Phase 3 pipeline
```

### 4. 测试文件

`tests/test_longtou.py` 新增:
- DDX 解析测试 (mock 同花顺 HTML 响应)
- 龙虎榜解析测试
- leader_score DDX 修正测试 (给定输入验证排序)
- 降级测试 (DDX 超时 → 用原分)

## 不变部分

- Phase 1 板块扫描逻辑完全不变
- `run_pipeline.py` 不变
- 现有 scoring 权重体系不变
- generate_report 输出格式不变 (龙虎榜风险表现在 risk_tips 中)
- cache 机制不变

## 风险

| 风险 | 缓解 |
|---|---|
| 同花顺接口变更 | 降级设计, 失败不阻塞 |
| 反爬封 IP | 加请求间隔 1-2s, User-Agent 轮换 |
| DDX 数据延迟 (盘中 vs 盘后) | 增加 fetch_time 字段, 盘后评分权重更高 |
| 龙虎榜只覆盖涨停/跌停股 | 命中率约 20%, 超过一半 candidate 无数据, 正常降级 |

## 投入估算

| 脚本 | 行数 | 复杂度 |
|---|---|---|
| fetch_ddx.py | ~80 | 低 |
| fetch_longhubang.py | ~100 | 低 |
| fetch_sector_data.py 修改 | ~30 | 中 |
| compute_scores.py 修改 | ~40 | 中 |
| market_leader.py 修改 | ~50 | 中 |
| 测试 | ~150 | 低 |
| **合计** | **~450** | |
