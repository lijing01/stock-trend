# 市场环境评分 + 今日复盘 + /stock-trend 大盘/板块对比 — 设计方案 (P0-1)

> 日期: 2026-08-01
> 来源: `投资体系/投资体系.txt` Trading System v1.0 第一层需求 + 短板#2(相对强弱)

## 背景

投资体系文档第一层要求:**每天先判市场环境**(大盘站上MA20?成交额放大?主线有赚钱效应?涨停家数增?),评分≥80 才开始找股票,否则降仓/空仓("熊市还在找牛股"是亏钱主因)。文档同时把**相对强弱分析**(个股 vs 大盘 vs 板块)列为第二大短板。

现状:skill 已有 `/market-theme`(板块)、`/longtou`(龙头)、`/stock-trend`(个股),但**无"市场环境 gate"层**;`/stock-trend` 报告不对比大盘/板块。

方案:**新建独立「今日复盘」脚本**(market_regime.py),整合全市场上下文 → 输出市场环境评分 + 每日复盘报告 + 持久化上下文;`/stock-trend` 跑个股时读该上下文,报告自动加「大盘/板块对比」段。

## 数据源(全部复用现有 fetcher,无新依赖)

| 数据 | 来源 | 说明 |
|---|---|---|
| 大盘指数+MA20 | `fetchers/kline_eastmoney.py` fetch `000001.SH`(上证)/`000300.SH`(沪深300)/`399001.SZ`(深成) | index secid 已支持(`core/eastmoney_utils.py:104`);MA20 用 `eastmoney_utils.ma()` |
| 两市成交额 | 上证指数 `000001.SH` amount + **深证综指 `399106.SZ`** amount | 深成指只含500成分股会低估深市,必须用深证综指(全深市);指数 K 线自带 amount 字段 |
| 涨跌家数 | `sector_data.get_sector_rankings()` **仅 `type=="industry"`** 板块 up_count/down_count 加总 | 概念板块重叠重复计数,只加总行业板块(近似划分全市场) |
| 板块最强/最弱 | 同上 industry sector rankings | change_pct 排序取前3/后3 |
| 涨停家数/连板 | `zt_replay.fetch_limitup_stocks(date)` + `aggregate_by_limit_streak()` | {1:25,2:8} → 涨停总数/连板数 |
| 北向资金 | `capital_flow.fetch_northbound_flow()` | ⚠️ 2024-08 披露机制调整后北向净买入多不可用;失败时**降级用板块 main_force_net 加总(主力净流入)** |
| HS300 涨跌 | `macro_snapshot.fetch_hs300()` | 已进 pipeline 的 hs300 snapshot |
| 个股→板块 | `sector_mapper.py --lookup <code>` / `stock_sector_map.json` 缓存 | /stock-trend 集成用 |

**降级策略**:单组件失败 → 记 `null` 并置中性 50,权重重分配给其余组件,不阻塞。

## 模块 1: `scripts/analysis/market_regime.py` — 今日复盘(独立命令 `/daily-review`)

镜像 `weekly_report.py` 模式:SCRIPT_DIR/PROJECT_ROOT 常量、`sys.path.insert`、argparse、MD 手拼 `lines[]`、`--json` stdout、报告写 `reports/lists/`。

### CLI

```
python3 analysis/market_regime.py [--no-refresh] [--json] [--html]
```

- 默认:拉数据 → 算分 → 持久化 → stdout 摘要 + 存 `reports/lists/daily-review-<YYYYmmdd-HHMMSS>.md`
- `--json`:stdout 结构化 JSON(给 Agent 消费)
- `--html`:额外生成 HTML
- `--no-refresh`:跳过实时拉取,用今日缓存 `market_regime.json` 重出报告(测试/复盘用)

### 市场环境评分(0-100,5 组件加权)

| 组件 | 权重 | 计算 |
|---|---|---|
| index_trend 大盘趋势 | 25 | 上证/沪深300/深成 各自:收盘>MA20 且 MA20↑=100;>MA20 但 MA20↓=60;<MA20 但 MA20↑=40;<MA20 且 MA20↓=0,取均 |
| volume 成交额 | 20 | 两市成交额(上证+深证综指) vs 近20日均额(历史不足用5日):`clamp(0,100, 50+(ratio-1)*100)` |
| breadth 赚钱效应 | 25 | 行业板块涨跌家数比 up_ratio*100 与 板块上涨占比 混合 |
| zt_emotion 涨停情绪 | 20 | 涨停家数 vs 近20日均涨停数 + 连板高度(历史不足降权) |
| capital 资金 | 10 | 北向净买入:`clamp(50 + net_yi*10)`;**北向不可用 → 降级用行业板块 main_force_net 加总(主力净流入亿元)** |

`score = Σ(component*weight)/100`,clamp 0-100 保留 1 位小数。

**状态 gate**(对齐文档):

- ≥80 **强势** — 可正常建仓/加仓
- 60-79 **中性** — 轻仓观察
- <60 **弱势** — 降仓/空仓,不找牛股

### 持久化

- `CACHE_DIR/market_regime.json` — **今日上下文**(regime_score/状态/指数状态/最强最弱板块/涨跌家数/成交额/涨停/北向)。供 `/stock-trend` 读取。
- `CACHE_DIR/market_regime_history.json` — `{date:{regime_score,components,zt_count,volume,breadth}}`,prune 30 天。支撑"涨停/成交额 vs 均值"与未来统计。

### 复盘报告 MD 结构(对齐文档复盘模板)

1. **市场环境**:regime 分数 + 状态标签 + 组件明细 + 涨跌家数/两市成交额/北向/涨停家数/连板
2. **板块**:最强前3 + 最弱前3
3. **持仓(轻量)**:读 `portfolio.yaml`,每只拉K线 → 现价 vs MA5/MA20、今日涨跌、相对止损/目标位;提示"深挖用 /stock-trend"
4. **明日计划**:if-then 模板,由 regime 状态 + 持仓信号生成(如"如果大盘≥80且个股放量突破→加仓;如果跌破MA20→止损")
5. 免责声明(必带)

### 实现要点

- **K线排序**:fetch_eastmoney 返回顺序不保证,MA 计算前 `sorted(records, key=trade_date)` 升序(旧→新)。
- **非交易日/盘中处理**:镜像 `market_theme.py:47` 零活动检测 — 非交易日数据为空/过期时,用 `market_regime_history.json` 最近交易日快照重出报告 + 标注数据日期;盘中跑则提示"盘中数据,收盘后重跑"。
- **相对强弱 pct_chg 来源**:个股今日 pct_chg 取 `kline.json` **最后一条记录**的 `pct_chg` 字段(非 technical summary);沪深300 change_pct 取 `macro_snapshot.json` 的 `hs300.change_pct`。

## 模块 2: `/stock-trend` 加「大盘/板块对比」段

目标:个股分析带市场上下文,补文档短板#2(相对强弱)。

改 `scripts/reporting/report.py`:

- 读 `CACHE_DIR/market_regime.json`(存在且当日)→ context 加 `大盘背景`(regime_score+状态)、`板块背景`(今日最强/最弱板块)。
- 读 `macro_snapshot.json` hs300 涨跌 → 个股今日 pct_chg(technical.json)vs 沪深300 涨跌 → `相对强弱` 判断(强/弱/持平)。
- 个股→板块:`sector_mapper.py --lookup <code>`(有缓存);成功则标注个股所属板块在今日排名的位置(最强 top3/最弱 bottom3/中游)。

改模板:

- `assets/report-template.md` + `assets/report-template.html`:加「📊 大盘/板块对比」段。
- 上下文缺失时该段显示"今日未生成复盘(market_regime.json 缺失),建议先跑 /daily-review"。

SKILL.md `/stock-trend` 流程加一句说明:报告含大盘/板块对比段,来源为今日复盘上下文。

## 模块 3: `tests/test_market_regime.py`

独立脚本(无 pytest),仿 `tests/test_market_theme.py` 的 `test()` reporter + in-memory fixture:

- 纯函数测试:regime 评分(fixture 指数/板块/涨停/北向)、组件归一化、gate 标签、复盘 MD 生成、if-then 计划生成。
- `HAS_AKSHARE` guard 的 live 加载器测试(仿 `test_weekly_report.py:153`)。
- 持仓轻量段:用 fixture portfolio.yaml 结构测"现价 vs MA/止损"输出。

**不做 golden**(market-wide 非 per-symbol;先例 weekly_report/market_theme 均不在 golden_config)。

## 模块 4: `SKILL.md`

- frontmatter allowed-tools 加:`Bash(python3 .claude/skills/stock-trend/scripts/analysis/market_regime.py *)`。
- 加 `/daily-review` 段落(仿 `/weekly` 段:CLI、步骤、输出)。
- `/stock-trend` Step 4 或报告说明处注明大盘/板块对比段来源。

## 验证

1. `python3 tests/test_market_regime.py -v` — 全过
2. `python3 tests/test_stock_trend.py` — 全过(若 report.py 模板改动导致断言失败,同步更新断言)
3. `python3 tests/test_golden.py --diff` — 无失败(report 输出不在 golden 范围,应无影响;若 diff 有数值变化→`--regenerate` 并在 commit 说明)
4. 手动:`python3 analysis/market_regime.py --json` 真拉一次,检查分数合理;`--no-refresh` 重出报告
5. `/stock-trend` 抽 1 只验证报告含「大盘/板块对比」段

## 后续迭代(不在本方案)

- 南向资金(v1 跳过,接口同北向,后续一行加)
- 全市场 LPS/Spring 选股漏斗(P0-2)
- `scores.py --mode wyckoff` 100分制(P0-3)
- 交易日志+统计(P1)
