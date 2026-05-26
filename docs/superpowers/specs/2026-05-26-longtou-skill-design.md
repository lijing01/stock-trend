# /longtou 市场龙头中军分析技能设计

## 概述

在 stock-trend 技能中新增 `/longtou` 命令，扫描 A股全市场热点板块，识别板块内龙头股和中军股，复用现有 pipeline 做深度分析，输出投资建议。

## 触发方式

```
/longtou [--top N] [--sector <板块名>] [--compact]
```

- `--top N`：热点板块数量，默认 10
- `--sector <板块名>`：只分析指定板块，跳过板块扫描阶段
- `--compact`：精简输出

## 三阶段架构

```
Phase 1: 板块热点扫描
  └─ 东方财富板块概念API → 板块综合评分(涨幅40%+主力资金30%+涨跌比30%)
  └─ 输出：热点板块排名 Top N
         ↓
Phase 2: 板块内龙头+中军筛选
  ├─ 龙头筛选：阶段涨幅(50%) + 成交额(30%) + 涨跌停(20%)
  ├─ 中军筛选：市值(40%) + 基本面(40%) + 走势稳定性(20%)
  └─ 每板块 3龙头 + 3中军（可重叠）
         ↓
Phase 3: 深度分析 + 报告
  ├─ 调 run_pipeline.py → pipeline 全维度数据
  ├─ 调 compute_scores.py → 综合评分
  └─ 生成 Markdown 结构化报告
```

## 数据源

### 东方财富板块 API

| 接口 | 用途 | 说明 |
|------|------|------|
| `push2.eastmoney.com/api/qt/clist/get` | 板块行情排行 | 板块代码、名称、涨幅、涨跌家数、成交额 |
| `push2.eastmoney.com/api/qt/sector/get` | 板块成分股 | 板块成分股行情（涨幅、成交额、市值、PE） |
| `datacenter.eastmoney.com/api/data/v1/get` | 板块资金流 | 板块主力资金净流入 |

### 现有脚本复用

| 脚本 | 阶段 | 用途 |
|------|------|------|
| `resolve_code.py` | Phase 2-3 | 代码解析 |
| `run_pipeline.py` | Phase 3 | 数据管线 |
| `compute_scores.py` | Phase 3 | 综合评分 |

## 脚本新增

### 1. `fetch_sector_data.py`

板块数据获取模块。

**功能**：
- `get_sector_list()` → 获取全市场板块/概念列表（代码、名称、类型）
- `get_sector_rankings()` → 获取板块排行（涨幅、资金、涨跌比）
- `get_sector_stocks(sector_code)` → 获取板块成分股行情数据

**输出格式**：

```json
{
  "meta": {"fetch_time": "...", "total_sectors": 200},
  "sectors": [
    {
      "code": "BKxxx",
      "name": "半导体",
      "change_pct": 8.3,
      "main_force_net": 1250000000,
      "up_count": 80,
      "down_count": 20,
      "amount": 85000000000
    }
  ]
}
```

### 2. `market_leader.py`

主线脚本，编排三个阶段。

**命令行接口**：

```bash
python3 market_leader.py [--top N] [--sector <板块名>] [--compact] [--output-html]
```

**内部流程**：

```
1. Phase 1: fetch_sector_data → 板块评分 → 热点排名
2. Phase 2: 每个热点板块 → 获取成分股 → 筛选龙头+中军
3. Phase 3: 对候选股调 pipeline → 汇总结果 → 生成报告
```

**输出 JSON**：

```json
{
  "meta": {"scan_time": "...", "total_sectors": 200},
  "hot_sectors": [
    {
      "rank": 1,
      "name": "半导体",
      "score": 85,
      "change_pct": 8.3,
      "leaders": [
        {"code": "002371", "name": "北方华创", "change": 15.0, "role": "龙头",
         "pipeline_scores": {...}, "analysis": "...", "suggestion": "..."}
      ],
      "core_stocks": [
        {"code": "688981", "name": "中芯国际", "market_cap": 5800, "role": "中军",
         "pipeline_scores": {...}, "analysis": "...", "suggestion": "..."}
      ],
      "verdict": {"logic": "...", "sustainability": "...", "risk": "..."}
    }
  ],
  "summary": {
    "best_picks": [...],
    "risk_tips": [...]
  }
}
```

## SKILL.md 集成

在 SKILL.md 新增 `/longtou` 命令节（参考 `/etf-scan` 模式），包含：
1. 命令参数说明
2. 三阶段执行步骤
3. 输出格式模板
4. 免责声明

## 性能考量

- Phase 1 板块扫描：1 次 HTTP 请求，耗时 < 5s
- Phase 2 成分股获取：N 个板块 × 1 次请求，耗时 < 10s
- Phase 3 深度分析：每只候选股调 pipeline（复用缓存），耗时取决于 pipeline
- `--sector` 指定单板块时跳过 Phase 1，显著提速

## 风险与限制

- 东方财富接口可能变动，需异常处理
- 板块分类体系（行业/概念/地域）需明确只覆盖行业+概念
- 深度分析阶段候选股过多时可能耗时较长，建议限制 Top N
- 股票基本面数据（PE/ROE）需复用现有 fundamental 接口或 AKShare
