# ETF Scanner 设计规格

> 每日扫描 A 股 ETF 精选池，输出当日趋势排名和投资逻辑
> 基于现有 stock-trend skill 基础设施构建

## 1. 概述

### 1.1 目标

在现有 stock-trend skill（单只标的深度分析）基础上，构建批量 ETF 扫描能力：
- 每日扫描 30-50 只精选 A 股 ETF
- 快速粗筛 + 深度分析分层模式
- 输出横向排名和投资逻辑
- 通过 `/etf-scan` slash command 触发

### 1.2 设计原则

- **零修改现有脚本**：18 个现有 `.py` 文件全部不动
- **分层扫描**：全池快速粗筛 → 少数深度分析，平衡速度与质量
- **永不单点失败**：单只 ETF 数据异常不阻断全流程
- **配置驱动**：ETF 池、权重、阈值均可配置，不改代码

## 2. 架构

### 2.1 新增文件

| 文件 | 说明 |
|------|------|
| `scripts/etf_scanner.py` | 核心编排器，~500 行 |
| `scripts/watchlist.yaml` | ETF 精选池配置 |
| `tests/test_etf_scanner.py` | 测试用例 |
| `tests/golden/etf_scanner/` | 快照数据（可选） |

### 2.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `SKILL.md` | 注册 `/etf-scan` 命令 |

### 2.3 架构图

```
SKILL.md
  └─ /etf-scan [--top N] [--focus <板块>] [--output compact|full]
       │
       ▼
  etf_scanner.py
       │
       ├─ Phase 1: 全池快速扫描 ──────────────────────────┐
       │   watchlist.yaml → 解析名单                          │
       │   → 并行 fetch_kline_eastmoney.py (60日K线)         │
       │   → 并行 fetch_capital_flow.py (主力净流入)          │
       │   → 并行 fetch_etf_data.py (规模/份额/IOPV)         │
       │   → quick_score() → 速评分 0-100                    │
       │   → 排序 + 选 top N (默认 10)                       │
       │                                                     │
       ├─ Phase 2: 深度分析 ────────────────────────────┐   │
       │   top N 并行执行:                                   │   │
       │      run_pipeline.py --code {code}                  │   │
       │      compute_scores.py --code {code}                │   │
       │   → 收集深度评分 + 各维度细项                       │   │
       │                                                     │   │
       └─ Phase 3: 汇总输出 ────────────────────────────┐   │
           合并排名 → JSON → stdout                           │
```

### 2.4 数据流

```
Phase 1 (快速扫描):
  etf_scanner.py
    ├─ 读 watchlist.yaml ─────────→ list of {code, name, category}
    │
    ├─ 对每只 ETF:
    │   ├─ fetch_kline_eastmoney.py (60日)
    │   ├─ fetch_capital_flow.py (仅主力净流入, 5日)
    │   └─ fetch_etf_data.py (规模/份额/IOPV)
    │
    ├─ quick_score() ────────────→ 综合 5 个维度速评
    │   ├─ 价格动量 (MA斜率 + RSI + MACD方向)   权重 30%
    │   ├─ 量能活跃度 (成交额/市值比 + 量比)     权重 20%
    │   ├─ 资金流向 (主力净流入率)              权重 20%
    │   ├─ 份额趋势 (份额变化率)                权重 15%
    │   └─ IOPV折溢价 (折价偏好)                权重 15%
    │   (宏观因子作为修正项, ±10%)
    │
    └─ 排序 + 选 top N (默认 10)

Phase 2 (深度分析):
  top N 并行 `subprocess.run`:
    run_pipeline.py --code {code}
    compute_scores.py --code {code}
    → 深度分析结果: {pipeline_output.json, scores.json}

Phase 3 (聚合输出):
  JSON: {
    scan_time, etf_count, valid_count, market_state,
    phase1_ranking: [{code, name, quick_score, category, ...}],
    phase2_ranking: [{code, name, deep_score, signals, verdict, ...}],
    combined_ranking: [{code, name, combined_score, rank, detail, ...}],
    top_picks: [{code, name, logic: str}],
    excluded: [{code, name, reason}],
    sector_summary: {strong: [], weak: []}
  }
```

## 3. watchlist.yaml 配置

```yaml
categories:
  - name: 宽基指数
    etfs:
      - code: 510050    # 上证50
      - code: 510300    # 沪深300
      - code: 510500    # 中证500
      - code: 512100    # 中证1000
      - code: 588000    # 科创50

  - name: 行业/主题
    etfs:
      - code: 512880    # 证券ETF
      - code: 512760    # 芯片ETF
      - code: 515050    # 5G ETF
      - code: 515030   # 新能源汽车ETF
      - code: 512010   # 医药ETF
      - code: 515680   # 消费ETF
      - code: 512660   # 军工ETF
      - code: 513100   # 纳指ETF
      - code: 513180   # 恒生科技ETF
      - code: 513050   # 中概互联ETF
      - code: 159915   # 创业板ETF
      - code: 159949   # 创业板50
      - code: 159845   # 中证1000ETF
      - code: 516160   # 新能源ETF
      - code: 512480   # 半导体ETF
      - code: 515700   # 光伏ETF
      - code: 516970   # 基建ETF
      - code: 518880   # 黄金ETF
      - code: 513090   # 港股通ETF

settings:
  top_n: 10             # Phase 2 深度分析数量
  quick_kline_days: 60  # 粗筛 K 线天数
  min_volume: 10000000  # 最低成交额过滤 (元)
  min_scale: 200000000  # 最低规模过滤 (元)
  quick_score_weights:
    momentum: 30
    volume: 20
    capital_flow: 20
    shares_trend: 15
    iopv: 15
```

## 4. 速评分算法 (quick_score)

### 4.1 动量维度 (30%)

| 指标 | 数据源 | 计算方法 | 评分区间 | 权重 |
|------|--------|---------|---------|------|
| MA 趋势 | kline close | MA5/MA20/MA60 排列方向 | -20 ~ 40 | 40% |
| RSI 位置 | kline close | RSI(14) 值映射 | 0 ~ 30 | 30% |
| MACD 方向 | kline close | DIF 与 DEA 关系 | -10 ~ 30 | 30% |

MA 趋势评分：
- 多头排列 (MA5 > MA20 > MA60): 30-40
- MA5 > MA20 > MA60 但未完全多头: 15-29
- 交叉纠缠: 0-14
- 空头排列: (-20)-(-1)

RSI 评分：
- 40-60 (正常区间): 20-30
- 30-40 或 60-70 (边缘): 10-19
- 20-30 或 70-80 (极端): 5-9 (超卖/超买均减分)
- <20 或 >80: 0 (极端行情不追)

MACD 评分：
- DIF > DEA且DIF向上: 20-30
- DIF > DEA但DIF趋平: 10-19
- DIF < DEA但FIF向上交叉: 0-9
- DIF < DEA且向下: (-10)-(-1)

### 4.2 量能活跃度 (20%)

- 量比 = 近5日均量 / 近60日均量
  - 量比 > 1.5: 40-50 (放量活跃)
  - 量比 1.2-1.5: 30-39
  - 量比 0.8-1.2: 15-29 (正常)
  - 量比 < 0.8: 0-14 (缩量)
- 成交额分位数（相对全市场ETF）:
  - top 20%: 40-50
  - 20%-50%: 20-39
  - bottom 50%: 0-19

### 4.3 资金流向 (20%)

- 近5日主力净流入率 = 主力净流入额 / 成交额
  - > 2%: 70-100
  - 0.5% ~ 2%: 40-69
  - -0.5% ~ 0.5%: 10-39 (中性)
  - -2% ~ -0.5%: (-30)-9
  - < -2%: (-100)-(-31)

### 4.4 份额趋势 (15%)

- 近1月份额变化率
  - > 5%: 70-100 (资金持续申购)
  - 1% ~ 5%: 40-69
  - -1% ~ 1%: 10-39 (稳定)
  - -5% ~ -1%: (-30)-9
  - < -5%: (-100)-(-31)

### 4.5 IOPV 折溢价 (15%)

- 折价 0.1%-0.5%: 70-100 (理想)
- 折价 0%-0.1%: 40-69
- 折价 >0.5%: 20-39 (过大折价可能有流动性问题)
- 溢价 0%-0.3%: 10-29
- 溢价 >0.3%: 0-9 (追高信号)

### 4.6 宏观修正 (±10%)

宏观因子：基于 `fetch_macro_snapshot.py` 输出
- 沪深300 处于20日均线上方且放量: +5%
- 沪深300 处于20日均线下方且缩量: -5%
- 其他: 0

### 4.7 缺失数据处理

- 单一维度数据缺失: 该维度权重均分给其他维度
- 超过 3 个维度缺失: 标记为 `数据不足`, 速评分 = None (不进入排名)
- 全部维度缺失: 标记为 `数据异常`, 在报告中单独列出

## 5. 输出格式

### 5.1 JSON 输出 (etf_scanner.py → stdout)

```json
{
  "meta": {
    "scan_time": "2026-05-16T15:30:00+08:00",
    "market_state": "closed",
    "total_etfs": 48,
    "valid_etfs": 46,
    "phase1_duration": 65,
    "phase2_duration": 120
  },
  "phase1_ranking": [
    {"code": "513180", "name": "恒生科技ETF", "category": "行业/主题",
     "quick_score": 82, "rank": 1,
     "dimensions": {"momentum": 88, "volume": 75, "capital_flow": 85,
                    "shares_trend": 90, "iopv": 70}}
  ],
  "phase2_ranking": [
    {"code": "513180", "name": "恒生科技ETF",
     "deep_score": 87, "verdict": "up", "confidence": "high"}
  ],
  "combined_ranking": [
    {"code": "513180", "name": "恒生科技ETF", "category": "行业/主题",
     "quick_score": 82, "deep_score": 87,
     "combined_score": 85, "combined_formula": "0.3*quick + 0.7*deep",
     "rank": 1, "stars": 3,
     "verdict": "up", "confidence": "high",
     "signal": "↑↑", "recommendation": "★★★"}
  ],
  "top_picks": [
    {"code": "513180", "name": "恒生科技ETF",
     "logic": "MA5/MA20/MA60多头排列，主力净流入8.2亿，份额月增12%，趋势强劲"}
  ],
  "excluded": [
    {"code": "515050", "name": "5G ETF",
     "reason": "动量↓ 资金↓ 份额持续缩水"}
  ],
  "sector_summary": {
    "strong": [{"name": "恒生科技", "change": "+3"}],
    "weak": [{"name": "军工", "change": "-4"}]
  }
}
```

### 5.2 综合得分

- 同时有 Phase 1 和 Phase 2 数据: `combined_score = 0.3 × quick_score + 0.7 × deep_score`
- 仅有 Phase 1 数据（深度分析失败）: `combined_score = quick_score`
- 以此分数决定最终排名和推荐星级

### 5.3 对话呈现

Claude Code 收到 JSON 后按模板呈现：
- 头部：扫描概要（时间/数量/有效数）
- 综合排名表（10 行）
- Top 3-5 详细投资逻辑
- 低分排除摘要
- 板块强弱总结

### 5.4 --output compact 模式

简版只输出：
- Top 5 排名（代码 + 名称 + 得分 + 信号 + 推荐）
- Top 1-2 简略逻辑（一句话）
- 排除摘要（一句话）
- "详情运行 /etf-scan --output full"

## 6. SKILL.md 集成

```markdown
### /etf-scan [--top N] [--focus <板块>] [--output compact|full]

扫描精选 A 股 ETF 池，输出当日趋势排名。

参数：
- `--top N`    深度分析数量，默认 10
- `--focus <板块>` 只扫描指定板块（如 宽基、证券、科技）
- `--output compact|full`  输出简版/完整版

执行步骤：
1. Shell: python3 .claude/skills/stock-trend/scripts/etf_scanner.py ...
2. 读取 JSON 输出
3. 在对话中按模板呈现
```

## 7. 错误处理

| 场景 | 处理 |
|------|------|
| 单只 ETF 数据获取失败 | 跳过该只，报告中标记 `数据异常` |
| Phase 2 某只深度分析失败 | 用速评分替代，标记 `深度分析跳过` |
| 全部 ETF 无数据 | 返回错误提示 + 建议运行 `diagnose.py` |
| watchlist.yaml 格式错误 | 报错：配置文件格式不正确 |
| --focus 板块名不存在 | 列出可用板块列表 |
| 空缓存 + 盘中运行 | 正常拉取，告知数据为实时数据(5min TTL) |
| 网络故障 | 报告失败数量，建议检查网络后重试 |

永不因单只 ETF 失败导致全流程中断。

## 8. 测试策略

| 类型 | 内容 | 通过条件 |
|------|------|---------|
| 单元测试 | `quick_score()` 各维度计算、缺失值权重分配、边界值(RSI=0/100) | 全部 pass |
| 单元测试 | watchlist.yaml 格式验证（必需字段、数据类型） | 全部 pass |
| 集成测试 | 对精选池前 3 只执行全流程，验证 JSON 字段完整性 | JSON schema 验证通过 |
| 集成测试 | 模拟单只 ETF 数据异常，验证跳过机制 | 流程不中断，报告标记异常 |
| Golden | quick_score 快照对比（首次生成 baseline） | diff 无意外变化 |

## 9. YAGNI 声明

以下功能明确不在此规格中：

- 自动定时运行（随需触发即可）
- 推送通知/邮件报告
- Web 界面
- 历史趋势跟踪（每日扫描结果对比）
- 模拟盘/回测
- 全市场 1000+ ETF 扫描（维持精选池 30-50）

## 10. 实施顺序

1. 创建 `watchlist.yaml`（精选池配置）
2. 实现 `etf_scanner.py` 框架 + Phase 1（读配置 + quick_score + 排序）
3. 实现 Phase 2（top N 深度分析编排）
4. 实现 Phase 3（JSON 输出聚合）
5. 实现 CLI 参数（`--top`/`--focus`/`--output`）
6. 实现错误处理和缺失数据降级
7. 编写单元测试 + 集成测试 + golden 测试
8. 修改 `SKILL.md` 注册命令
9. 端到端验证
