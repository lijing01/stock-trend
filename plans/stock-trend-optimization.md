# Stock-Trend Skill 优化计划

## Context

分析上次对恒生科技ETF大成(159740)的实际运行，发现以下问题：
1. **止损/目标价/风险收益比计算有Bug** — 当前价0.639，最近压力位0.640仅0.15%，R:R=0.05无意义
2. **ETF数据需手动curl+解析** — 净值、收益率、持仓等全靠手工
3. **资金流向数据需手动获取** — 无自动化脚本
4. **搜索查询效果差** — bing_search对中文财经查询返回大量无关结果
5. **报告完全手动生成** — 模板存在但无填充脚本
6. **Tushare ETF权限错误未自动降级** — 需手动检查后才fallback
7. **诊断结果未被使用** — 运行了但输出被忽略

## 修改文件清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `scripts/analyze_technical.py` | 修改 | 修复止损/目标价Bug，增加三级目标，动态聚类阈值 |
| `scripts/fetch_etf_data.py` | 新建 | ETF专属数据自动获取 |
| `scripts/fetch_capital_flow.py` | 新建 | 资金流向数据自动获取 |
| `scripts/generate_report.py` | 新建 | 报告自动生成脚本 |
| `scripts/fetch_kline.py` | 修改 | Tushare权限错误自动检测 |
| `SKILL.md` | 修改 | 更新工作流步骤，增加搜索模板，诊断结果处理 |
| `scripts/test_stock_trend.py` | 修改 | 增加新脚本测试 |

## 实施阶段

### Phase 1: Bug修复 (analyze_technical.py + fetch_kline.py)

**1a. 修复 `calc_risk_reward()` (analyze_technical.py:990-1042)**

当前Bug:
- 止损只用 `support - 1*ATR`，缺少 `max(support - 1*ATR, current - 2*ATR)` 的 `max` 逻辑
- 目标只取最近压力位，对紧密盘整市场产生无意义R:R

修复方案:
- 止损: `stop_loss = max(nearest_support - 1*ATR, current_price - 2*ATR)`
- 目标: 三级目标体系
  - `target_conservative`: 最近压力位（当前行为，保留）
  - `target_moderate`: 第一个R:R >= 1.5的压力位，若无则 `current + 2*ATR`
  - `target_aggressive`: target_moderate之后的下一个压力位，若无则 `current + 3*ATR`
- 主目标 = `target_moderate`
- 增加 `favorable_rr` 布尔值和 `warning` 字段

**1b. 修复 `calc_support_resistance()` 聚类阈值 (analyze_technical.py:889)**

当前Bug: 0.5%的硬编码聚类阈值对低价ETF过于激进（0.639的0.5%仅0.0032）

修复: 动态阈值 `max(0.005, 1.5 * atr_pct / 100)`，需要将 `atr_pct` 传入函数

**1c. Tushare权限错误检测 (fetch_kline.py:133-135)**

当前: 返回通用 `RuntimeError`，agent需手动检查错误消息

修复:
- `fetch_via_http()` 中检测权限相关错误字符串，抛出 `PermissionError`
- `fetch_kline()` 中单独捕获 `PermissionError`，直接降级不重试
- 输出JSON中增加 `error_type: "permission"` 字段

### Phase 2: 新建数据脚本

**2a. fetch_etf_data.py**

CLI: `python3 fetch_etf_data.py <fund_code> [-o output.json]`

数据源: 东方财富 `pingzhongdata/{code}.js` + `js/{code}.js`

获取数据:
- 基金名称、类型、跟踪指数
- 最新净值(NAV)、IOPV折溢价率
- 收益率(近1月/3月/6月/1年)
- 前十大持仓（名称+代码+权重）
- 股票仓位比例
- 基金规模(份额+资产)
- 近期申赎流向

解析: JavaScript变量用正则 `re.findall(r'var (\w+)\s*=\s*(.+?);', content)` 提取

**2b. fetch_capital_flow.py**

CLI: `python3 fetch_capital_flow.py <ts_code> [--asset E|FD] [-o output.json]`

股票路径: 东方财富 `push2.eastmoney.com/api/qt/stock/fflow/kline/get`
ETF路径: 复用 pingzhongdata 的申赎数据

secid解析: 复用 `fetch_kline_eastmoney.py` 的逻辑

### Phase 3: 报告生成脚本

**3a. generate_report.py**

CLI:
```bash
python3 generate_report.py \
  --technical /tmp/technical.json \
  --kline /tmp/kline.json \
  --etf-data /tmp/etf_data.json \
  --capital-flow /tmp/capital_flow.json \
  --scores '{"technical":-1,"capital_flow":-0.5,...}' \
  --direction '震荡' --score -0.08 --confidence '低' \
  --risks '["布林带极度收口","RSI顶背离"]' \
  --special '{"type":"etf","content":"IOPV折溢价..."}' \
  --output-md reports/159740.SZ/20260514-2200.md \
  --output-html reports/159740.SZ/20260514-2200.html
```

模板引擎: 简单字符串替换（`{{variable}}` + `{{#section}}...{{/section}}` 条件块），不引入Jinja2

关键设计: 脚本只负责格式化，评分和方向由agent决定

### Phase 4: SKILL.md更新

- Step 1: 增加诊断结果解析指引（跳过失败数据源）
- Step 3: 增加ETF数据和资金流向脚本命令；Tushare权限错误处理
- Step 4: 增加搜索查询模板；优先WebSearch而非bing_search
- Step 10: 增加generate_report.py命令

### Phase 5: 测试更新

在 `test_stock_trend.py` 中增加:
- `TA-10`: 验证三级目标体系输出（target_conservative/moderate/aggressive）
- `TA-11`: 验证动态聚类阈值
- `TA-12`: 验证止损max逻辑
- `TF-ETF-01`: fetch_etf_data.py 基本功能测试
- `TF-CF-01`: fetch_capital_flow.py 基本功能测试
- `TF-RPT-01`: generate_report.py 模板渲染测试

## 验证方式

1. 用159740重新运行 `analyze_technical.py`，验证三级目标和R:R修复
2. 用159740运行 `fetch_etf_data.py`，验证ETF数据获取
3. 用159740运行 `fetch_capital_flow.py`，验证资金流向
4. 用Tushare无权限场景运行 `fetch_kline.py 159740.SZ --asset FD`，验证权限错误自动降级
5. 运行完整 `/stock-trend 159740` 流程，对比优化前后报告