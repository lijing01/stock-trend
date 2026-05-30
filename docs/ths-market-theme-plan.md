# 基于同花顺的市场主题引擎设计方案

> 日期：2026-05-30
> 状态：设计稿

---

## 1. 背景与设计原则

现有 `/market-theme` 基于**东方财富板块排行 API**（`push2.eastmoney.com`），通过涨跌幅、主力净流入、涨跌比等计算板块热度及持续性。

同花顺（10jqka）有东方财富不具备的独特数据维度，**不应简单替代东方财富，而是做差异化补充**。

| 东方财富擅长 | 同花顺擅长 |
|------------|-----------|
| 板块排行 JSON API（结构化、稳定） | DDX/DDY/DDZ 大单资金指标 |
| 全市场覆盖面广 | 涨停概念归因（标注因何涨停） |
| 涨跌幅/涨跌家数/主力净流入 | 龙虎榜席位级机构行为 |
| BK 分类体系稳定 | 概念分类更新更快（契合短线热点） |

**核心策略**：同花顺引擎聚焦东方财富做不了的领域，两个引擎独立运行，结果交叉验证。

---

## 2. 同花顺可用数据源

### 2.1 DDX 排行页面（已实现 ✅）

| 项 | 说明 |
|----|------|
| URL | `https://data.10jqka.com.cn/financial/ddx/opendata/` |
| 脚本 | `fetchers/ddx.py` |
| 功能 | `fetch_ddx_data(codes)` 按 code 查询 + `fetch_ddx_ranking(top_n=100)` 无目标扫描 |
| 用途 | 获取 top N 个股的 DDX/DDY/DDZ + 连续红柱天数 + 超级大单占比 |

### 2.2 龙虎榜（已有，待增强）

| 项 | 说明 |
|----|------|
| URL | `https://data.10jqka.com.cn/financial/longhubang/` |
| 脚本 | `fetchers/longhubang.py` |
| 当前功能 | 按 code 列表查询龙虎榜机构买入/卖出 |
| 待改造 | 无目标扫描 + 按板块聚合机构净额 |

### 2.3 涨停复盘（已实现 ✅）

| 项 | 说明 |
|----|------|
| URL | `https://data.10jqka.com.cn/financial/zt/` |
| 脚本 | `fetchers/zt_replay.py` |
| 功能 | `fetch_limitup_stocks(date=None)` → 涨停股票列表 |
| 独有价值 | **涨停概念归因**（每只涨停股标注所属概念标签） |

字段：code, name, concepts, first_limit_time, limit_streak, seal_amount, limit_type(firm/blown/retest), timing_bucket, board

### 2.4 股票→板块映射（已实现 ✅）

| 项 | 说明 |
|----|------|
| 脚本 | `fetchers/sector_mapper.py` |
| 原理 | 遍历东方财富所有 BK 板块（行业 + 概念）→ 获取各板块成分股 → 反向建索引 |
| 缓存 | `.cache/stock-trend/stock_sector_map.json`（7 天 TTL） |
| 主函数 | `get_mapping()` 加载或构建；`aggregate_ddx_by_sector(ddx_list, mapping)` 按板块聚合 DDX |

### 2.5 概念板块排行（规划中）

| 项 | 说明 |
|----|------|
| URL | `https://q.10jqka.com.cn/gn/` |
| 状态 | ❌ 未实现 |
| 价值 | 同花顺独立概念分类（和东方财富 BK 不完全重叠），用于双源交叉验证 |

---

## 3. 架构设计

```
┌─────────────────────────────────────────────────────┐
│   Layer 1: 数据获取                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│  │ 涨停复盘  │ │ DDX扫描  │ │ 龙虎榜   │ │概念板块  ││
│  │ (爬虫)   │ │ (已有改造)│ │ (已有改造)│ │ (爬虫)   ││
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘│
├───────┼────────────┼────────────┼────────────┼───────┤
│   Layer 2: 板块聚合 (股票→板块映射)                  │
│  ┌──────────────────────────────────────────────────┐│
│  │ 东方财富 BK 成分股映射表                          ││
│  │ 概念板块涨停家数 | DDX流入家数 | 大单占比 | 机构净额││
│  └──────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────┤
│   Layer 3: 评分引擎                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│  │资金热度分 │ │情绪热度分 │ │机构深度分 │ │持续性分  ││
│  │(35%)     │ │(35%)     │ │(20%)     │ │(10%)     ││
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘│
│               ↓ 综合主题分                              │
├──────────────────────────────────────────────────────┤
│   Layer 4: 输出                                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │ MD报告   │ │ HTML报告 │ │ JSON     │              │
│  │          │ │          │ │ (给Agent) │              │
│  └──────────┘ └──────────┘ └──────────┘              │
└─────────────────────────────────────────────────────┘
```

### 3.1 Layer 1：数据获取

#### 涨停复盘（P0 已实现 ✅）

```python
# scripts/fetchers/zt_replay.py
fetch_limitup_stocks(date_str: str = None) -> list[dict]
# 返回：
# [
#   {
#     "code": "002xxx", "name": "...",
#     "first_limit_time": "09:35",
#     "limit_type": "firm",          # firm / blown / retest
#     "limit_streak": 3,             # 连板数
#     "concepts": ["DeepSeek", "AI芯片", "国产替代"],
#     "seal_amount": 1.2e8,          # 封单金额（元）
#     "timing_bucket": "morning_early",  # 时间分桶
#     "board": "sh",
#   }
# ]
aggregate_by_concept(stocks) -> list[dict]     # 按概念标签聚合涨停统计
aggregate_by_limit_streak(stocks) -> dict       # 连板分布 {streak: count}
```

#### DDX 全市场扫描（P1 已实现 ✅）

```python
# scripts/fetchers/ddx.py
fetch_ddx_ranking(top_n: int = 100) -> list[dict]
# 返回：
# [
#   {
#     "code": "002xxx",
#     "ddx": 0.85,                    # DDX 值
#     "ddx_days": 5,                  # 连续红柱天数
#     "super_order_ratio": 0.12,      # 超级大单占比
#     "ddy": 0.32, "ddz": 1.23,
#   }
# ]
compute_ddx_score(ddx_data: dict) -> float          # 0-100 单股DDX评分
compute_super_order_score(ddx_data: dict) -> float   # 0-100 超级大单评分
```

#### 板块映射（P1 已实现 ✅）

```python
# scripts/fetchers/sector_mapper.py
get_mapping(rebuild=False) -> dict                   # 加载/构建映射表
get_stock_sectors(code) -> list[dict]                # 单个股票→板块查询
aggregate_ddx_by_sector(ddx_list, mapping) -> list[dict]  # DDX 按板块聚合
# 聚合结果字段：
# sector_code, sector_name, sector_type,
# total_ddx_stocks, ddx_inflow_count, ddx_inflow_ratio,
# continuous_count, continuous_ratio,
# high_super_count, avg_ddx, avg_super_order_ratio, ddx_score(0-100)
```

### 3.2 Layer 2：板块聚合（DDX + 涨停双源交叉）

不再走统一的 `aggregate_by_sector`，而是**两条独立路径 + 交叉验证**：

```
涨停聚合路径:     涨停股 → zt_replay.aggregate_by_concept() → 概念热度分
DDX 聚合路径:     DDX排行 → sector_mapper.aggregate_ddx_by_sector() → DDX资金分
交叉验证:         ths_theme.cross_reference_with_ddx() → 概念名模糊匹配
```

交叉验证策略：
1. 涨停概念名 exact match 同花顺概念 vs BK 板块名称
2. 失败 → BK 板块名包含同花顺概念名（`len>=3`）
3. 再失败 → 同花顺概念名包含 BK 板块名（`len>=4`）
4. 匹配成功：在原概念评分中注入 `ddx_score, ddx_inflow_count, continuous_count`
5. 匹配失败：标记 `ddx_cross = False`

### 3.3 Layer 3：评分引擎

#### 涨停热力分（单独使用）

```
hot_score = stock_count×30% + cont_score×25% + morning_score×20% + seal_score×15% - blown_penalty×10%
```

| 子维度 | 权重 | 计算 |
|--------|------|------|
| stock_count | 30% | 涨停家数归一化 0-100 |
| cont_score | 25% | `continuous_ratio×0.6 + (max_streak-1)×10×0.4` |
| morning_score | 20% | `morning_ratio × 100` |
| seal_score | 15% | 封单金额归一化 0-100 |
| blown_penalty | -10% | `blown_ratio × 100` |

#### DDX 资金分（单独使用）

```
ddx_score = inflow_ratio×50 + continuous_ratio×30 + super_ratio×20
```

| 子维度 | 权重 | 计算 |
|--------|------|------|
| inflow_ratio | 50% | DDX>0 家数 / 板块总 DDX 覆盖家数 |
| continuous_ratio | 30% | DDX_days≥3 家数 / 板块总家数 |
| super_ratio | 20% | 超级大单占比>5% 家数 / 板块总家数 |

#### 综合评分（`--ddx` 开启）

```
combined_score = hot_score × 0.70 + ddx_score × 0.30
```

仅在交叉验证匹配成功的概念上生效。无 DDX 匹配的概念保持原分。

#### 暗线识别（资金潜伏）

```python
# ths_theme.py 的 DDX 报告章节自动识别
# 条件：ddx_score >= 60 且 概念名未出现在涨停热力排行中
```

**主题分类阈值**：

| 类别 | 综合分 | 含义 |
|------|-------|------|
| 主线确认 | ≥70 | 资金 + 情绪 + 机构共振 |
| 候选主线 | 50-69 | 两维以上较强 |
| 暗线潜伏 | 40-49 | DDX 流入但涨停不多，还没爆发 |
| 脉冲热点 | <40 | 单日涨停但资金不持续 |
| 退潮 | <30 | DDX 流出 + 涨停家数下降 |

#### 暗线识别（同花顺引擎核心卖点）

```python
def identify_dark_horses(sectors: list[dict]) -> list[dict]:
    """识别资金潜伏但尚未爆发的板块。

    条件：
    1. DDX 流入家数 > 板块总家数 30%
    2. 涨停家数 = 0 或极少
    3. 超级大单占比 > 均值
    """
```

---

## 4. 反爬策略

同花顺 `data.10jqka.com.cn` 和 `q.10jqka.com.cn` 均有反爬（已验证返回 403）。参考已有 `ddx.py` 的策略：

```python
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.10jqka.com.cn/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
```

关键措施：
1. 完整浏览器 UA + Referer
2. 请求间隔 1-2s（随机抖动）
3. 失败重试 2-3 次（指数退避）
4. 每次只爬 1-2 页，避免高并发

---

## 5. 文件结构（当前状态）

```
scripts/
├── core/
│   └── ths_utils.py                 # 同花顺反爬工具(header/retry/parse)
├── fetchers/
│   ├── ddx.py                       # DDX排行: fetch_ddx_data + fetch_ddx_ranking
│   ├── longhubang.py                # 龙虎榜(按code查询，待增强无目标扫描)
│   ├── zt_replay.py                 # 涨停复盘爬虫 + 按概念聚合
│   └── sector_mapper.py             # 股票→板块映射构建 + DDX板块聚合
└── analysis/
    └── ths_theme.py                 # 主题引擎: 涨停评分 + DDX交叉验证 + MD/HTML报告

tests/
├── test_zt_replay.py               # 13 tests (zt_replay + ths_utils)
├── test_ths_theme.py                # 14 tests (评分引擎 + 报告)
└── test_ddx_integration.py          # 15 tests (DDX扫描 + 聚合 + 交叉验证)
```

---

## 6. 实现路线图（当前状态 ✅）

| 阶段 | 内容 | 状态 | 文件 |
|------|------|------|------|
| **P0** | 涨停复盘爬虫 + 概念聚合 | ✅ 已完成 | `zt_replay.py`, `test_zt_replay.py` |
| **P1** | DDX无目标扫描 + 板块映射 + 资金聚合 | ✅ 已完成 | `ddx.py`, `sector_mapper.py`, `test_ddx_integration.py` |
| **P2** | 评分引擎 + MD/HTML + SKILL挂钩 | ✅ 已完成 | `ths_theme.py`, `test_ths_theme.py`, `SKILL.md` |
| **P3** | 双源交叉验证（同花顺概念 ↔ BK 板块） | ✅ 已包含在 P1/P2 | `cross_reference_with_ddx()` |
| **P4** | 龙虎榜机构倾向板块聚合 | 🚧 待开发 | `longhubang.py` 改造 |
| **P5** | 概念板块排行（q.10jqka.com.cn 爬取） | ❌ 待开发 | 新文件 |

---

## 7. 与现有 market-theme 的关系

```
/market-theme (东方财富)          /ths-theme (同花顺)
─────────────────────            ─────────────────────
宽基覆盖所有 BK 板块              聚焦涨停+资金驱动的主题
数据稳定可靠                      数据更激进，含暗线预测
适合中线持仓参考                  适合短线/波段信号补充

         ↓ 交叉验证 ↓
    两个引擎同时命中 → 高置信度主题
    东方财富命中 + 同花顺暗线 → 关注但观望
    同花顺暗线 + 东方财富无信号 → 小额试仓观察
```

---

## 8. 风险与限制

1. **反爬升级风险**：同花顺可能加强反爬，需持续维护
2. **数据稳定性**：HTML 结构变动会导致解析失败
3. **板块映射精度**：混合映射方案可能漏掉部分小市值股票
4. **非交易日**：涨停/DDX 数据在非交易日不可用

> 建议优先实现 P0-P2，达到可用状态后投入实战验证，再决定是否继续 P3-P4。

---

## 9. 命令参考：`/ths-theme --ddx`

### 执行流程

```
1. fetch_limitup_stocks()           ← 爬同花顺涨停复盘
       ↓
2. compute_concept_scores()         ← 涨停维度评分
       ↓
3. [--ddx] fetch_ddx_ranking()      ← 爬同花顺DDX排行 top 100
       ↓
   [--ddx] get_mapping()            ← 加载/构建板块映射（东方财富BK）
       ↓
   [--ddx] aggregate_ddx_by_sector()← 按板块聚合DDX
       ↓
   [--ddx] cross_reference_with_ddx()← 概念名匹配 + 注入DDX数据
       ↓
   [--ddx] compute_combined_score()  ← 涨停×0.7 + DDX×0.3
       ↓
4. generate_report() + HTML         ← MD + HTML 双格式报告
```

### 报告结构（带DDX）

```
概念热度排行          → 综合评分排序
核心热点(≥70)        → 资金+情绪共振
活跃方向(50-69)      → 跟进观察
初现方向(30-49)      → 刚冒头
📊 DDX资金交叉验证    → 同花顺概念 × BK板块 交叉匹配结果
⚡ 资金潜伏           → DDX流入较强但无涨停的板块（暗线）
炸板统计              → 负面信号
```

### JSON 输出结构（`--json`）

```json
{
  "meta": {"date_str", "scan_time", "elapsed_seconds", "total_stocks", "total_concepts", "has_ddx"},
  "summary": {"total", "firm", "blown", "continuous", "high_streak", "early", "max_streak"},
  "scores": [{"concept", "hot_score", "stock_count", "ddx_score", "ddx_inflow_ratio", ...}],
  "strong": [...],
  "active": [...],
  "ddx_sectors": [...]  // (仅 --ddx 时) DDX板块聚合 top 10
}
```
