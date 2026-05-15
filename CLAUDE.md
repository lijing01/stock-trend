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
- 安装 git hooks（新人必做）: `bash .githooks/install-hooks.sh`

## Architecture

```
.claude/
├── skills/
│   └── stock-trend/
│       ├── SKILL.md                      # Skill 定义入口
│       ├── references/
│       │   ├── trend-dimensions.md        # 趋势维度详细说明
│       │   └── kline-patterns.md          # K线形态参考
│       ├── scripts/
│       │   ├── fetch_kline.py             # K线数据获取
│       │   ├── fetch_kline_eastmoney.py   # 东方财富数据源
│       │   └── analyze_technical.py       # 技术分析脚本
│       └── assets/
│           ├── report-template.md         # Markdown报告模板
│           └── report-template.html       # HTML报告模板
└── specs/
    └── stock-trend-skill.md              # 功能规格说明
reports/                                    # 生成的报告（已 gitignore）
```

## Token 优化

- 不要主动扫描、读取或列出 `reports/` 目录下的文件，除非用户明确触发与报告相关的操作（如 `/stock-trend`、查看报告、生成报告等语义）
- Explore agent 搜索范围应排除 `reports/` 目录