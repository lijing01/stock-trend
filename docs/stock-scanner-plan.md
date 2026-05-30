# 三合一优化计划：A股筛选器 + 机构连续净买入 + 龙头增强

## Context

当前工程覆盖了ETF扫描（全市场排名）、市场主线和龙头分析（板块→个股），但缺少：

1. **A股个股全市场筛选器** — 没有对标`etf_scanner.py`的个股快速筛选工具。市场主线和龙头分析找到板块后，需要一个能从热点板块成分股中多维度打分筛选A股的工具
2. **机构资金连续性检测** — `capital_flow.py`有北向/主力数据，`scores.py`只有市场整体北向连续买（>=3天+1分），缺少个股层面的主力/北向连续净买入检测
3. **龙头筛选缺少硬过滤** — `market_leader.py`的`filter_leaders`和`filter_core_stocks`没有ST排雷、流通市值过滤、放量突破标记、补涨识别

**目标**：补齐这三个缺口，形成 `market_theme → market_leader → stock_scanner` 完整决策链路。

---

## 计划 1：`stock_scanner.py` — A股热点板块个股筛选器

### 1.1 文件位置

**新增**：`.claude/skills/stock-trend/scripts/scans/stock_scanner.py`  
**不改动现有文件**

### 1.2 输入来源

两种调用方式：

```bash
# 方式1：指定板块代码（来自 market_theme 输出中的 strong/moderate 板块）
python3 stock_scanner.py --sectors BK0897,BK0482,BK0923 --top 10

# 方式2：从 market_leader JSON 输出文件读取已分析板块
python3 stock_scanner.py --from-leader /path/to/leader_output.json --top 10
```

### 1.3 三阶段架构

#### 阶段1：个股汇聚 + 硬过滤

```
输入: 板块代码列表
  │
  ├─ 对每个板块调用 sector_data.get_sector_stocks(code, top_n=30)
  ├─ 去重(按code)
  ├─ A股过滤：code以6/0/3开头（排除5/15开头的ETF，排除HK）
  ├─ ST排雷：name不含 "ST|*ST|退"
  ├─ 市值过滤：50亿 <= 流通市值 <= 500亿（从fundamental或sector stocks的market_cap字段）
  ├─ 流动性过滤：近5日均成交 > 1亿（从K线amount字段）
  └─ 输出: 候选池 list[dict]，每只含 code, name, sector_code, sector_name, sector_hot_score
```

**并行策略**：板块成分股获取并行(ThreadPoolExecutor, max_workers=4)，每板块50只上限。

#### 阶段2：快速多维打分 (不跑完整pipeline，对标 etf_scanner 阶段1)

对候选池每只股票：

**维度1: 动量 (30%)** — `score_momentum()`
- 从K线计算（并行获取60日K线，复用`kline_eastmoney.py`）
- MA排列：MA5>MA20>MA60 → +25, MA5>MA20 → +10, 空头 → -25
- RSI(14)：40-70区间最优，<30或>80惩罚
- MACD方向：金叉+红柱放大 → +15
- 20日涨幅：分段映射到0-100
- 输出：0-100

**维度2: 量价 (20%)** — `score_volume_price()`
- 量比(vol_ma5/vol_ma20)：>1.5 → +20, <0.5 → -10
- 量价配合：放量上涨(+15), 缩量下跌(+5), 放量下跌(-15)
- 近5日量价背离检测
- 输出：0-100

**维度3: 资金 (20%)** — `score_capital()`
- 主力净流入方向（近5日net_inflow总和）：>0 → +15, 显著正 → +25
- 北向个股持仓变动方向：增持 → +10
- 资金流向持续性（近5日中主力净流入>0的天数 >= 3 → +10）
- 输出：0-100

**维度4: 基本面 (15%)** — `score_fundamental_quick()`
- PE分位：3年PE百分位 < 30% → +20, > 80% → -15
- ROE：>15% → +15, >10% → +10
- 净利润增速：>20% → +15
- 营收增速：>15% → +10
- 输出：0-100

**维度5: 板块强度 (15%)** — `score_sector_strength()`
- 所属板块hot_score归一化 → 0-100
- 板块内相对涨幅排名 → 补涨加分（涨幅在中位以下但基本面好 → +10）
- 输出：0-100

**综合分**：`0.30*momentum + 0.20*volume + 0.20*capital + 0.15*fundamental + 0.15*sector`

**数据获取**：
- K线：并行调用`kline_eastmoney.py`（60日日线），ThreadPoolExecutor(4)
- 基本面：读缓存优先（`STOCK_TREND_CACHE_DIR/{code}/fundamental.json`），缺失时调用`fundamental.py`
- 资金流向：并行调用`capital_flow.py`（5日）

#### 阶段3：排名 + 输出

- 按综合分降序排列
- 星级：>=80 三星, >=65 二星, >=50 一星
- 风险标记：高分但量价背离、基本面差但动量强（短期炒作嫌疑）
- 输出JSON → stdout（`<!--JSON_OUTPUT-->`包裹，对标 market_leader 输出格式）

### 1.4 输出结构

```python
{
    "meta": {
        "scan_time": "20260530-103000",
        "source": "market_theme",     # or "market_leader"
        "input_sectors": ["BK0897", "BK0482"],
        "candidate_count": 120,       # after filters
        "scored_count": 95,           # with valid scores
        "elapsed_seconds": 45.2,
    },
    "rankings": [
        {
            "code": "600519",
            "ts_code": "600519.SH",
            "name": "贵州茅台",
            "sector_code": "BK0477",
            "sector_name": "白酒",
            "sector_hot_score": 85.0,
            "composite_score": 82.5,
            "stars": 3,
            "dimensions": {
                "momentum": 75.0,
                "volume_price": 80.0,
                "capital": 70.0,
                "fundamental": 85.0,
                "sector_strength": 90.0,
            },
            "signals": {
                "ma_alignment": "多头排列",
                "volume_breakout": True,     # 放量突破
                "capital_streak": 3,          # 主力连续净买天数
                "northbound_adding": True,    # 北向增持
                "pe_percentile_3y": 25.0,    # PE低估区域
                "roe": 18.5,
            },
            "warnings": [],
            "sector_relative_rank": 3,       # 板块内排名
            "sector_total": 45,
        },
        # ... more stocks
    ],
    "sector_summary": {
        "BK0477": {"name": "白酒", "hot_score": 85.0, "stock_count": 8, "avg_score": 72.5},
        # ...
    },
    "excluded": [
        {"code": "000xxx", "name": "STxxx", "reason": "ST股票"},
        {"code": "300xxx", "name": "xxx", "reason": "市值不足50亿"},
    ],
}
```

### 1.5 复用函数清单

| 复用 | 来源 | 用途 |
|------|------|------|
| `get_sector_stocks()` | `fetchers/sector_data.py` | 获取板块成分股 |
| `run_script()` | `core/cache_utils.py` | 子进程调用其他脚本 |
| `kline_eastmoney.py` (CLI) | `fetchers/` | 获取60日K线 |
| `capital_flow.py` (CLI) | `fetchers/` | 获取资金流向 |
| `fundamental.py` (CLI) | `fetchers/` | 获取基本面 |
| `ma()`, `rsi()`, `macd_direction()` | `core/eastmoney_utils.py` | 指标计算 |
| `read_json()` pattern | `pipeline/runner.py` | 读JSON文件 |
| `resolve_asset()` | `core/resolve_code.py` | A股/ETF判断 |

---

## 计划 2：机构连续净买入检测

### 2.1 改动文件

- **修改**：`scripts/fetchers/capital_flow.py` — 加个股连续净买天数计算
- **修改**：`scripts/analysis/scores.py` — 资金维度和情绪维度加连续净买信号

### 2.2 `capital_flow.py` 改动

在 `data_extended` 中新增 `individual_streak` 字段：

```python
# 在 main() 中，fetch_stock_capital_flow 后计算连续净买入
if asset == "E" and data:
    # 计算个股主力资金连续净买入天数
    main_streak = 0           # 主力净流入>0的连续天数（从最近向前）
    total_streak = 0          # 总净流入>0的连续天数
    for record in data:       # data已按日期降序（最新在前）
        if (record.get("main_net_inflow") or 0) > 0:
            main_streak += 1
        else:
            break
    for record in data:
        if (record.get("total_net_inflow") or 0) > 0:
            total_streak += 1
        else:
            break

    result["data_extended"]["individual_streak"] = {
        "main_streak": main_streak,       # 主力连续净流入天数
        "total_streak": total_streak,     # 总资金连续净流入天数
    }
```

同时修复 `fetch_individual_northbound()` 中缺失的日期字段：
```python
# 目前只返回最新一行，加日期字段便于后续使用
return {
    "date": str(df.iloc[-1].get("日期", "")),
    "hold_shares": ...,
    "hold_value_billion": ...,
    "change_shares": ...,
}
```

### 2.3 `scores.py` 改动

#### 2.3.1 资金维度增强（~line 875-900）

在自动化资金流向评分中新增：

```python
# 信号3：个股主力连续净买入
istreak = ext.get("individual_streak", {})
if istreak:
    ms = istreak.get("main_streak", 0)
    if ms >= 5:
        cap_score += 2       # 强连续流入
    elif ms >= 3:
        cap_score += 1       # 中等连续流入
    elif ms == 0:            # 最新日净流出
        # 检查是否连续净流出
        ...

# 信号4：北向连续增持（需capital_flow扩展返回北向历史）
nb_market = ext.get("northbound_market", [])
if isinstance(nb_market, list) and len(nb_market) >= 3:
    nb_streak = 0
    for day in reversed(nb_market):
        if (day.get("net_buy_billion") or 0) > 0:
            nb_streak += 1
        else:
            break
    # >=5天连续北向净买入 → +2，>=3天 → +1（现有逻辑已有，调整分值）
    if nb_streak >= 5:
        cap_score += 2
    elif nb_streak >= 3:
        cap_score += 1
```

#### 2.3.2 情绪维度增强（~line 902-957）

在自动化情绪评分中，新增个股资金连续性信号：

```python
# 信号：个股主力连续净流入对情绪的影响
istreak = ext.get("individual_streak", {})
if istreak:
    ms = istreak.get("main_streak", 0)
    if ms >= 5:
        sent_score += 1.0    # 强信心信号
    elif ms >= 3:
        sent_score += 0.5    # 温和信心信号
```

### 2.4 不影响现有行为

- 新增字段 `individual_streak` 为可选，下游不读取时无影响
- 评分改动为增量（只加不减），不改变现有阈值
- 北向市场连续买入逻辑不变，只是增加分值上限

---

## 计划 3：`market_leader.py` 增强

### 3.1 改动文件

- **修改**：`scripts/fetchers/sector_data.py` — `filter_leaders()` 和 `filter_core_stocks()` 增强
- **修改**：`scripts/scans/market_leader.py` — `analyze_sector()` 加过滤 + 补涨识别

### 3.2 `sector_data.py` — `filter_leaders()` 增强

**当前问题**：只用涨跌幅(50%)+成交额(30%)，无ST过滤、无市值过滤、无放量检测。

**改动**：

```python
def filter_leaders(stocks, top_n=3, min_market_cap=5e9, max_market_cap=5e11):
    """
    新增参数：
        min_market_cap: 最低市值(元)，默认50亿
        max_market_cap: 最高市值(元)，默认5000亿
    
    新增过滤：
        1. ST排雷：name含"ST"或"*ST"或"退" → 跳过
        2. 市值过滤：market_cap < min_market_cap or market_cap > max_market_cap → 跳过
    
    评分公式修复：
        change_score = min(100, max(0, 50 + change * 5))     # 权重50%
        amount_score = min(100, amount / 1e7)                 # 权重30%
        breakout_bonus = 0                                    # 权重20%（之前缺失）
        
        # 放量突破检测：当日涨>3% 且 成交额>板块成分股中位数的1.5倍
        median_amount = median(s['amount'] for s in stocks)
        if change > 3.0 and amount > median_amount * 1.5:
            breakout_bonus = min(100, max(0, 50 + (amount / median_amount - 1) * 50))
        
        leader_score = change_score*0.50 + amount_score*0.30 + breakout_bonus*0.20
    """
```

**关键改动点**（代码级别）：

1. `filter_leaders()` 开头加ST排雷循环
2. 加市值边界检查
3. 修复评分公式（补全缺失的20%权重）
4. 新增 `breakout_bonus` 放量突破加分

### 3.3 `sector_data.py` — `filter_core_stocks()` 增强

```python
def filter_core_stocks(stocks, top_n=3, min_market_cap=5e9, max_market_cap=5e11):
    """
    新增过滤（同filter_leaders）：
        1. ST排雷
        2. 市值过滤（中军更看重市值，范围可宽：50-5000亿）
    
    评分增强：
        # 新增：板块内相对涨幅排名（补涨识别）
        changes = sorted([s.get('change_pct', 0) for s in stocks])
        pct_rank = percentile_rank(change, changes)  # 0-100
        
        # 补涨加分：涨幅在板块后50%但基本面好
        laggard_bonus = 0
        if pct_rank < 50 and pe and 0 < pe < 30:
            laggard_bonus = min(20, (50 - pct_rank) * 0.4)
        
        core_score = cap_score*0.35 + pe_score*0.35 + stability_score*0.15 + laggard_bonus*0.15
    """
```

**补涨识别逻辑**：
- 相对涨幅百分位 < 50%（板块内偏后）
- PE在0-30之间（估值合理）
- 按百分位越低加分越多（最大+20）
- 权重从 stability 分出一半(5%) + 新增5% = 15%

### 3.4 `market_leader.py` — `analyze_sector()` 增强

**在 `analyze_sector()` 中新增**：

```python
def analyze_sector(sector, leader_n=3, core_n=3):
    stocks = get_sector_stocks(sector["code"], top_n=50)
    
    # --- 新增：硬过滤 ---
    filtered_stocks = []
    excluded = []
    for s in stocks:
        name = s.get("name", "")
        mcap = s.get("market_cap", 0)
        
        # ST排雷
        if any(kw in name for kw in ["ST", "*ST", "退"]):
            excluded.append({"code": s["code"], "name": name, "reason": "ST/退市风险"})
            continue
        
        # 市值过滤（默认50-5000亿，可通过参数调整）
        if mcap < 5e9:
            excluded.append({"code": s["code"], "name": name, "reason": "市值过小"})
            continue
        
        filtered_stocks.append(s)
    
    # 用过滤后的池子做精选
    leaders = filter_leaders(filtered_stocks, top_n=leader_n)
    cores = filter_core_stocks(filtered_stocks, top_n=core_n)
    
    # 去重（已有逻辑保留）
    ...
    
    return {
        ...
        "stocks_before_filter": len(stocks),
        "stocks_after_filter": len(filtered_stocks),
        "excluded": excluded[:10],  # 最多展示10个排除项
        ...
    }
```

### 3.5 输出中增加标记

在 `market_leader.py` 的最终输出中，对每只 leader/core 增加信号标记：

```python
{
    "code": "600xxx",
    "name": "...",
    ...
    "flags": {
        "is_st": False,
        "volume_breakout": True,           # 放量突破标记
        "consecutive_main_inflow": 4,      # 连续主力净流入天数
        "is_laggard": False,               # 是否补涨标的
        "pe_percentile_3y": 25.0,         # PE历史分位
    }
}
```

### 3.6 影响范围

| 文件 | 改动类型 | 风险 |
|------|---------|------|
| `sector_data.py` | `filter_leaders()` 加参数和过滤逻辑 | 低 — 向后兼容(默认参数保持行为) |
| `sector_data.py` | `filter_core_stocks()` 加参数和补涨逻辑 | 低 — 同上 |
| `market_leader.py` | `analyze_sector()` 加硬过滤 | 低 — 新增逻辑，不改现有路径 |
| `market_leader.py` | 输出结构加 `flags` 字段 | 低 — 新增字段 |

---

## 验证计划

### 通用验证步骤

```bash
# 1. 现有测试全部通过
cd /Users/trace/work/agent/stock-trend
python3 .claude/skills/stock-trend/tests/test_stock_trend.py
python3 .claude/skills/stock-trend/tests/test_golden.py --diff

# 2. 新增脚本单元测试
python3 .claude/skills/stock-trend/tests/test_stock_trend.py  # 已包含 SU-* 测试

# 3. 端到端测试
# 计划1：跑一次完整 stock_scanner
python3 .claude/skills/stock-trend/scripts/scans/stock_scanner.py \
  --sectors BK0477 --top 10 2>&1 | head -50

# 计划2：验证 capital_flow 新字段
python3 .claude/skills/stock-trend/scripts/fetchers/capital_flow.py \
  600519.SH --asset E -o /tmp/test_cap.json
python3 -c "import json; d=json.load(open('/tmp/test_cap.json')); print(d.get('data_extended',{}).get('individual_streak'))"

# 计划3：验证龙头扫描增强
python3 .claude/skills/stock-trend/scripts/scans/market_leader.py \
  --top 5 --compact 2>&1 | head -100
```

### 测试用例设计

**计划1（stock_scanner）**：
- `stock_scanner.py --sectors BK0477 --top 5` 能正常输出JSON
- 输出中无ST股票
- 输出中无ETF代码（5开头）
- 输出中无港股代码（0开头非A股）
- 综合分在0-100范围

**计划2（连续净买入）**：
- `capital_flow.py` 输出包含 `individual_streak` 字段
- `individual_streak.main_streak` 为整数 0-5
- `scores.py` 在资金维度包含新增信号标记
- 现有黄金快照 diff 无意外变化（若有合理变化用 `--regenerate`）

**计划3（龙头增强）**：
- `market_leader.py` 输出中无ST股票
- `filter_leaders` 返回结果数可能小于请求数（被过滤）
- `excluded` 列表记录被过滤的股票及原因
- `flags.volume_breakout` 标记存在
- 现有黄金快照 diff 无意外变化

### Golden Snapshot 处理

若改动导致评分/排名的合理数值变化：
```bash
python3 .claude/skills/stock-trend/tests/test_golden.py --regenerate
git add tests/golden/
git commit -m "test: regenerate golden snapshots after stock scanner and leader enhancements"
```
