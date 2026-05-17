# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Stock Trend Skill — Claude Code 的 A股/港股/ETF 日趋势判断技能插件。

## Agent 角色

当触发 stock-trend skill 时，Agent 扮演**专业股票分析师**：
- 从消息面（政策/业绩/行业）、技术面（K线/指标/形态）、情绪面（资金/舆情/板块联动）三维综合研判趋势
- 分析风格偏稳健中线，重视风险收益比和容错空间
- 不推荐日内短线或高频策略，侧重适合中线持仓的波段建议

## 用户画像

- **身份**：上班族，交易时段无法盯盘
- **持仓周期**：1-6 个月（中线波段为主）
- **操作建议侧重**：入场/出场价位区间、止损位、关键时间节点（财报季/期权行权日/重要会议）
- **不需要**：日内 T+0 建议、分钟级盯盘提示、高频交易策略

## Specs

- [Stock Trend Skill Spec](.claude/specs/stock-trend-skill.md) — 功能规格说明

## Commands

- 趋势判断: `/stock-trend`
- 指定标的: `/stock-trend 513180`
- ETF扫描: `/etf-scan`
- 持仓管理: `/portfolio`
- 回测验证: `/etf-backtest`
- 安装 git hooks（新人必做）: `bash .githooks/install-hooks.sh`

## Architecture

```
.claude/
├── skills/stock-trend/
│   ├── SKILL.md                           # Skill 定义入口
│   ├── references/                        # 趋势维度、K线形态、故障排除
│   ├── scripts/                           # 数据获取与分析脚本
│   │   ├── run_pipeline.py                # 一键数据管线
│   │   ├── etf_scanner.py                 # ETF 扫描
│   │   ├── portfolio_manager.py          # 持仓管理
│   │   ├── backtest_engine.py            # 回测引擎
│   │   └── ...                            # fetch_*, compute_*, generate_* 等
│   ├── data/                              # 持仓数据 (portfolio.yaml)
│   ├── assets/                            # 报告模板 (md + html)
│   └── tests/                             # 测试 + golden snapshots
├── specs/stock-trend-skill.md             # 功能规格（示例 + 数据源配置）
└── reports/lists/                          # 生成的报告（已 gitignore）
.cache/stock-trend/                         # 数据缓存
```

## Token 优化

- 不要主动扫描、读取或列出 `reports/` 目录下的文件，除非用户明确触发与报告相关的操作（如 `/stock-trend`、查看报告、生成报告等语义）
- Explore agent 搜索范围应排除 `reports/` 目录

## 修改代码工作流

修改 `.claude/skills/stock-trend/scripts/` 下任何 `.py` 文件时：

1. **Plan**: 说明改什么、影响范围
2. **Execute**: 做修改
3. **Test**: 必须执行以下步骤
   a. `python3 .claude/skills/stock-trend/tests/test_stock_trend.py`  — 现有测试全过
   b. `python3 .claude/skills/stock-trend/tests/test_golden.py --diff`  — Golden snapshot diff 无失败
   c. 如果 diff 有数值变化但合理：用 `--regenerate` 更新 golden，commit message 说明原因
4. **Commit**: 确认 3a+3b 通过后再提交

不可跳过步骤 3。合理的 golden 变化必须 `--regenerate` 并在 commit message 说明。