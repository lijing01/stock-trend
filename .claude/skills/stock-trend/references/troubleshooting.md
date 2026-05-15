# 故障排除与高级用法

## 1. 管线失败时的手动降级

如果 `run_pipeline.py` 整体失败，可按以下步骤手动执行：

```bash
# K线数据获取（Tushare）
python3 .claude/skills/stock-trend/scripts/fetch_kline.py <ts_code> --asset <E|FD> --freq <D|W> --adj <qfq|none> -o /tmp/kline.json
# Tushare失败时降级东方财富
python3 .claude/skills/stock-trend/scripts/fetch_kline_eastmoney.py <ts_code> --asset <E|FD> --freq <D|W> -o /tmp/kline.json
# 技术分析
python3 .claude/skills/stock-trend/scripts/analyze_technical.py /tmp/kline.json -o /tmp/technical.json
# ETF数据（仅ETF标的）
python3 .claude/skills/stock-trend/scripts/fetch_etf_data.py <fund_code> -o /tmp/etf_data.json
# 资金流向
python3 .claude/skills/stock-trend/scripts/fetch_capital_flow.py <ts_code> --asset <E|FD> -o /tmp/capital_flow.json
```

## 2. 数据源降级链详情

- A股/ETF：Tushare → 东方财富(增强头+节点轮换) → BaoStock → 无数据模式
- 港股(.HK)：Tushare → 腾讯财经港股API → 无数据模式

判断 Tushare 是否失败：检查 JSON 的 `meta.data_source`，为 `error` 则降级。`meta.error_type: "permission"` 表示权限不足，直接降级不需要重试。所有数据源均失败时，技术面按 0 分处理并标注"无数据源"。数据不足 60 条时标注。

## 3. 多周期共振模式 (`--multi-timeframe`)

除获取日线数据外，额外获取周线数据（`--freq W`），输出到 `/tmp/kline_weekly.json` 和 `/tmp/technical_weekly.json`。在 Step 5 中对比日线与周线趋势方向，计算周期共振得分。

## 4. 手动传参生成报告（方式二）

```bash
python3 .claude/skills/stock-trend/scripts/generate_report.py \
  --technical /tmp/technical.json \
  --kline /tmp/kline.json \
  --etf-data /tmp/etf_data.json \
  --capital-flow /tmp/capital_flow.json \
  --scores '{"technical":1,"capital_flow":0.5,"fundamental":-1,"sentiment":0,"macro":0}' \
  --direction '看多' --score 1.2 --confidence '中' \
  --risks '["布林带极度收口","RSI顶背离"]' \
  --special '{"type":"etf","title":"ETF 特殊分析","content":"IOPV折溢价率: +0.15%"}' \
  --ts-code 159740.SZ --stock-name '恒生科技ETF大成' \
  --output-md reports/159740.SZ/20260514-2200.md \
  --output-html reports/159740.SZ/20260514-2200.html
```

## 5. Tushare Token 配置

配置优先级：命令行 `--token` > 环境变量 `TUSHARE_TOKEN` > `.claude/tushare-config.json`。未配置时自动降级东方财富。