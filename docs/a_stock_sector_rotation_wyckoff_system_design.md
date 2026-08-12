# A股热点轮动 + Wyckoff 交易决策系统设计

## 1. 设计目标

系统不做简单的"热点榜复制器"，而是构建一套：

> **市场环境 → 资金轮动 → 主线识别 → 强股识别 → Wyckoff 结构 →
> 低风险买点 → 风险/仓位**

核心回答三个问题：

1.  **钱在哪里？**
2.  **钱正在去哪里？**
3.  **主线中的强势个股什么时候具备较好的风险收益比？**

------------------------------------------------------------------------

## 2. 总体架构

``` text
                         ┌─────────────────────┐
                         │   同花顺 iFinD API   │
                         │ 行情/板块/个股/问财  │
                         └──────────┬──────────┘
                                    │
                              Data Collector
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        指数/市场数据          行业/概念板块           个股行情
              │                     │                     │
              └─────────────────────┼─────────────────────┘
                                    ▼
                         ┌─────────────────────┐
                         │ Feature Engine      │
                         │ 特征计算 + 标准化    │
                         └──────────┬──────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 ▼                  ▼                  ▼
          Market Engine       Sector Engine       Stock Engine
          市场环境判断          热点轮动            个股强弱
                 │                  │                  │
                 └──────────────────┼──────────────────┘
                                    ▼
                           Wyckoff Engine
                     Spring / Test / SOS / LPS
                                    │
                                    ▼
                           Signal Fusion
                             信号融合层
                                    │
                 ┌──────────────────┼─────────────────┐
                 ▼                  ▼                 ▼
             观察池              候选池             买点池
                                    │
                                    ▼
                          Dashboard / Agent
```

建议拆成六个核心模块：

-   Data Collector
-   Feature Engine
-   Market Engine
-   Sector Rotation Engine
-   Stock Relative Strength Engine
-   Wyckoff / Signal Fusion Engine

LLM Agent
位于确定性计算之后，主要负责解释和综合判断，不直接对原始行情进行黑盒式买卖判断。

------------------------------------------------------------------------

## 3. Data Collector

数据层原则：

> **保存事实，不做判断。**

### 3.1 MarketSnapshot

建议每 5 分钟保存一次市场快照。

``` kotlin
data class MarketSnapshot(
    val timestamp: Long,

    val shIndexChange: Double,
    val szIndexChange: Double,
    val cybChange: Double,

    val totalAmount: Double,

    val upCount: Int,
    val downCount: Int,

    val limitUpCount: Int,
    val limitDownCount: Int,

    val highLimitUpCount: Int
)
```

后续可扩展：

-   涨停数
-   跌停数
-   炸板率
-   连板高度
-   红盘率
-   全市场成交额
-   成交额相对昨日增量
-   大盘不同宽基指数强弱

用于判断：

-   赚钱效应
-   情绪周期
-   市场风险偏好
-   当前是否适合进攻

------------------------------------------------------------------------

## 4. Sector Rotation Engine

Sector Rotation Engine 是系统核心。

不要只计算：

> 板块涨幅排名

而要计算：

> **板块当前资金状态及其变化速度。**

### 4.1 SectorSnapshot

``` kotlin
data class SectorSnapshot(
    val code: String,
    val name: String,

    val changePct: Double,
    val amount: Double,
    val amountRatio: Double,

    val upRatio: Double,
    val limitUpCount: Int,

    val leaderChange: Double,

    val rs5m: Double,
    val rs15m: Double,
    val rs30m: Double,

    val rank: Int,
    val rankDelta: Int,

    val heatScore: Double
)
```

------------------------------------------------------------------------

## 5. HeatScore 热点评分模型

第一版优先使用可解释的规则模型，而不是机器学习。

基础公式：

``` text
HeatScore =
    20% × PriceStrength
  + 20% × Momentum
  + 15% × Breadth
  + 15% × Volume
  + 15% × Leader
  + 15% × RelativeStrength
```

### 5.1 因子说明

  因子                 权重 含义
  ------------------ ------ ----------------------------
  PriceStrength         20% 板块涨幅及横截面位置
  Momentum              20% 5/15/30 分钟短周期动量
  Breadth               15% 上涨家数占比、板块赚钱效应
  Volume                15% 成交额及成交额增量
  Leader                15% 龙头、涨停、大涨个股强度
  RelativeStrength      15% 相对大盘/宽基指数强度

各指标先进行横截面标准化，再计算综合得分。

例如：

``` text
AI软件

涨幅             82
短周期动量        91
上涨广度          88
成交额增量        93
龙头强度          86
RS                90

HeatScore = 88.5
```

### 5.2 比绝对分数更重要的是变化率

``` text
10:00  71
10:10  75
10:20  81
10:30  86
10:40  89
```

关注：

``` text
d(HeatScore) / dt
```

用于判断热点正在：

-   加速
-   稳定
-   钝化
-   分歧
-   退潮

------------------------------------------------------------------------

## 6. Sector State Machine

热点轮动不能只做排名，应建立状态机。

``` text
COLD
 ↓
WARMING
 ↓
INFLOW
 ↓
STRONG
 ↓
ACCELERATING
 ↓
OVERHEATED
 ↓
DIVERGENCE
 ↓
OUTFLOW
 ↓
COOLING
```

示例：

``` text
机器人

09:45 WARMING
10:05 INFLOW
10:25 STRONG
10:50 ACCELERATING
13:20 OVERHEATED
14:00 DIVERGENCE
```

### 6.1 交易意义

重点关注：

``` text
WARMING → INFLOW → STRONG
```

尤其是：

``` text
WARMING → INFLOW
```

这是潜在主线开始形成的阶段。

反之：

``` text
STRONG → OVERHEATED
```

即使排名第一，也可能已经不是理想的风险收益位置。

------------------------------------------------------------------------

## 7. Rank Velocity

热点轮动的关键指标之一：

``` text
RankVelocity = Rank(t-n) - Rank(t)
```

例如：

  板块         10:00   10:30   RankVelocity
  ---------- ------- ------- --------------
  机器人          15       4            +11
  有色             9       2             +7
  AI软件           1       1              0
  消费电子         3      10             -7

解释：

``` text
机器人       +11   强烈加强
有色          +7   明显加强
AI软件         0   高位稳定
消费电子      -7   明显转弱
```

因此：

> 排名第 4、但正在快速上升的机器人板块，可能比已经排名第 1
> 且动量钝化的板块更值得研究。

系统的核心不是找"最热"，而是识别：

> **资金从旧热点向新热点迁移的过程。**

------------------------------------------------------------------------

## 8. Rotation Matrix

建立资金轮动矩阵：

  板块         强度   动量 状态
  ---------- ------ ------ ----------
  机器人       86 ↑    +18 加速
  有色         82 ↑    +12 流入
  AI软件       91 →     +1 高位一致
  半导体       75 ↓     -8 分歧
  消费电子     61 ↓    -16 退潮
  银行         48 ↑     +7 回暖

进一步映射到四象限：

``` text
                    动量 ↑

             ② 潜在主线       ① 主线
               机器人          有色
               军工            AI

强度低  ←────────────────────→ 强度高

               银行            半导体
               地产            消费电子

             ③ 冷门           ④ 退潮

                    动量 ↓
```

重点扫描：

``` text
② 潜在主线 → ① 主线
```

而不是机械追逐已经位于第一象限顶部的板块。

------------------------------------------------------------------------

## 9. Stock Relative Strength Engine

确定板块之后，再筛选板块内个股。

逻辑：

``` text
市场
 ↓
板块
 ↓
个股
```

基础相对强弱：

``` text
StockRS = Return(stock) - Return(sector)
```

例如：

``` text
有色板块       +2.1%

A              +5.8%   RS +3.7
B              +3.4%   RS +1.3
C              +1.7%   RS -0.4
D              -0.5%   RS -2.6
```

优先进入观察池：

``` text
A / B
```

后续进一步增加：

-   5m RS
-   15m RS
-   30m RS
-   日线 RS
-   成交额排名
-   成交额增量
-   量比
-   板块上涨贡献度
-   龙头/中军属性
-   回撤抗跌性

------------------------------------------------------------------------

## 10. Wyckoff Engine

### 10.1 输入

``` text
OHLCV
成交量
MA20
MA60
ATR
Trading Range
关键高低点
Sector RS
Stock RS
```

长期可以识别：

``` text
PS
SC
AR
ST

Spring
Test

SOS
LPS

UT
UTAD
SOW
LPSY
```

第一版不建议直接挑战完整的 Wyckoff A-E 自动识别。

优先实现三个高价值模式：

1.  Spring-Test
2.  SOS
3.  LPS

------------------------------------------------------------------------

## 11. Spring-Test

结构：

``` text
Trading Range
      ↓
向下跌破支撑
      ↓
快速重新收回
      ↓
Spring
      ↓
反弹
      ↓
Test
      ↓
缩量 + 不再创新低
```

重点特征：

-   Spring 是否快速收回区间
-   下破幅度是否合理
-   Volume 是否异常
-   Test 是否缩量
-   Test 是否守住 Spring Low
-   Test 是否出现 Supply Dry-up
-   板块同期是否开始增强

最终输出：

``` text
SPRING_CANDIDATE
SPRING_CONFIRMED
TEST_CANDIDATE
TEST_CONFIRMED
```

------------------------------------------------------------------------

## 12. SOS

基础结构：

``` text
突破 Trading Range
+
Spread 扩大
+
Volume 增加
+
Close 靠近 High
```

可增加：

``` text
Sector Heat ↑
Sector RankVelocity > 0
Stock RS > 0
```

避免把纯个股脉冲错误识别为高质量 SOS。

------------------------------------------------------------------------

## 13. LPS

核心结构：

``` text
SOS
 ↓
回踩突破区域
 ↓
成交量收缩
 ↓
Spread 收窄
 ↓
不破关键支撑
 ↓
重新出现 Demand
```

LPS 评分重点：

``` text
突破质量
回踩深度
回踩成交量
回踩时间
支撑有效性
Demand 恢复
Sector RS
Stock RS
```

系统最终输出：

``` text
LPS_CANDIDATE
LPS_HIGH_QUALITY
LPS_FAILED
```

------------------------------------------------------------------------

## 14. Signal Fusion

最终交易判断不能来自单一信号。

不要：

``` text
发现 LPS
→ 买入
```

而应该：

``` text
Market
   ↓
Sector
   ↓
Stock
   ↓
Wyckoff
   ↓
Risk / Reward
```

概念模型：

``` text
TradeScore =
    MarketScore
  × SectorScore
  × StockScore
  × WyckoffScore
```

实际工程实现中建议采用标准化加权模型，避免直接相乘导致分布失真。

例如：

  因子             得分
  -------------- ------
  市场环境           78
  板块强度           88
  板块动量           92
  个股 RS            87
  Wyckoff 结构       91
  量价确认           84

综合：

``` text
TradeScore = 87
```

建议分级：

``` text
< 60        忽略
60 - 70     观察
70 - 80     候选
80 - 90     高质量机会
> 90        极强信号，但必须检查过热程度
```

------------------------------------------------------------------------

## 15. Position Penalty

必须增加位置惩罚，否则系统很容易退化为追涨模型。

最终：

``` text
FinalScore = TradeScore - PositionPenalty
```

PositionPenalty 可以考虑：

-   距离 MA20 的乖离率
-   距离突破位的乖离率
-   3 日累计涨幅
-   5 日累计涨幅
-   ATR 扩张程度
-   板块连续高潮天数
-   个股连续加速程度
-   风险收益比

例如：

``` text
距离MA20          +18%
距离突破位         +15%
3日累计涨幅         +25%
板块连续高潮         3天

TradeScore        = 92
PositionPenalty   = 20

FinalScore        = 72
```

系统输出：

> 强势，但位置过高，当前风险收益比下降，不追。

------------------------------------------------------------------------

## 16. Dashboard

建议盘中 Dashboard 一屏解决核心决策。

``` text
══════════════ 市场状态 ══════════════

市场：      强势震荡
情绪：      回暖 ↑
赚钱效应：  72
成交额：    1.21万亿 ↑8.7%

══════════════ 热点轮动 ══════════════

板块       热度    Δ排名    状态

机器人      91      +8      加速
有色        87      +5      流入
AI软件      92       0      高位
半导体      79      -4      分歧
消费电子    63      -9      退潮

══════════════ 潜在机会 ══════════════

机器人
    A股    RS 91    LPS候选
    B股    RS 87    SOS
    C股    RS 82    回踩中

有色
    紫金    RS 88    LPS
    XXX     RS 84    SOS

══════════════ 风险 ═════════════════

AI软件
    热度很高
    但连续高潮
    PositionPenalty = 22

→ 不追高
```

------------------------------------------------------------------------

## 17. Agent 层设计

LLM 不负责底层数值计算。

错误架构：

``` text
原始行情
  ↓
LLM
  ↓
买 / 卖
```

推荐架构：

``` text
原始数据
 ↓
确定性算法
 ↓
Feature Engine
 ↓
Rule Engine
 ↓
Signal
 ↓
LLM Agent
 ↓
解释 + 综合判断
```

例如确定性系统输出：

``` json
{
  "sector": "有色",
  "heat": 87,
  "rankVelocity": 6,
  "state": "INFLOW",
  "stockRS": 89,
  "wyckoff": "LPS_CANDIDATE",
  "volume": "CONTRACTING",
  "positionPenalty": 4
}
```

Agent 将其解释为：

> 有色板块热点排名快速提升，资金轮动明显；目标个股相对板块保持强势，目前处于
> SOS 后第一次缩量回踩区域，符合 LPS 候选特征。位置惩罚较低，可继续等待
> Demand 恢复确认。

优势：

-   可解释
-   可回测
-   可复现
-   可调参
-   不依赖 LLM 猜测原始 K 线含义

------------------------------------------------------------------------

## 18. MVP 开发路线

### V0.1：热点轮动

``` text
iFinD
 ↓
行业 / 概念板块
 ↓
5分钟 Snapshot
 ↓
HeatScore
 ↓
RankVelocity
 ↓
Rotation State
```

解决：

> 钱在哪里？钱正在去哪里？

### V0.2：板块 → 个股

增加：

``` text
Stock RS
成交额
成交额增量
涨幅
量比
板块贡献度
```

解决：

> 主线里面谁更强？

### V0.3：Wyckoff

第一版只做：

``` text
Spring-Test
SOS
LPS
```

解决：

> 强股什么时候具备较低风险的介入结构？

### V0.4：交易决策

增加：

``` text
Market Regime
PositionPenalty
Risk / Reward
止损
仓位
```

最终形成：

``` text
市场环境
   ↓
资金轮动
   ↓
主线识别
   ↓
强股识别
   ↓
Wyckoff结构
   ↓
LPS / Spring-Test
   ↓
盈亏比
   ↓
仓位
```

------------------------------------------------------------------------

## 19. 第一性原则

整个系统建议坚持以下原则：

1.  **先市场，后板块，再个股。**
2.  **关注热点变化，而不仅是热点绝对排名。**
3.  **寻找资金从弱到强的迁移，而不是机械追逐最热方向。**
4.  **板块强不等于个股可以买，必须检查个股 RS。**
5.  **个股强不等于位置好，必须检查 Wyckoff 结构。**
6.  **LPS/Spring 不等于必然成功，必须结合市场和板块环境。**
7.  **高强度不等于高风险收益比，因此必须加入 PositionPenalty。**
8.  **底层指标使用确定性算法，LLM 主要负责解释、归纳和辅助决策。**
9.  **所有信号必须可记录、可回放、可回测。**
10. **最终优化目标不是预测涨跌，而是寻找高胜率、高赔率、风险可控的交易位置。**

------------------------------------------------------------------------

## 20. 推荐最终工程链路

``` text
同花顺 iFinD
      ↓
Raw Market Data
      ↓
Snapshot Storage
      ↓
Feature Engine
      ↓
Market Regime Engine
      ↓
Sector Rotation Engine
      ↓
Sector State Machine
      ↓
Stock RS Engine
      ↓
Wyckoff Engine
      ↓
Signal Fusion
      ↓
Position Penalty
      ↓
Risk / Reward
      ↓
Candidate Pool
      ↓
LLM Agent
      ↓
Dashboard / Alert / 复盘
```

该架构最终将
**板块强弱、资金轮动、量价关系、Wyckoff、Spring-Test、SOS、LPS、风险收益比和仓位管理**
整合为一套机器可执行、可回测、可解释的交易决策框架。
