# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Stock Trend Skill — Claude Code 的 A股/港股/ETF 日趋势判断技能插件。

## Specs

- [Stock Trend Skill Spec](.claude/specs/stock-trend-skill.md) — 功能规格说明

## Commands

- 趋势判断: `/stock-trend`
- 指定标的: `/stock-trend 513180`

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
│       │   ├── analyze_technical.py       # 技术分析脚本
│       │   └── generate_chart_html.py     # K线图表生成
│       └── assets/
│           ├── report-template.md         # Markdown报告模板
│           └── report-template.html       # HTML报告模板
└── specs/
    └── stock-trend-skill.md              # 功能规格说明
reports/                                    # 生成的报告（已 gitignore）
```