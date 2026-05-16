📊 ETF 扫描报告    {{scan_time}}

▸ 扫描范围: {{total_etfs}} 只 ETF
▸ 有效数据: {{valid_etfs}} 只
▸ 耗时: {{duration_seconds}}s

┌─ 综合排名 ─────────────────────────────────────────┐
│ 排名  代码    名称            速评分  深度分  信号  推荐 │
│ ─────────────────────────────────────────────────── │
{{#ranking_rows}}
│ {{rank}}   {{code}}  {{name}}      {{quick_score}}     {{deep_score}}     {{signal}}   {{stars}} │
{{/ranking_rows}}
└───────────────────────────────────────────────────────┘

🏆 Top 投资逻辑:
{{#top_picks}}
#{{pick_rank}} {{name}} ({{code}}): {{logic}}
{{/top_picks}}

{{#has_excluded}}
❌ 低分排除: {{excluded_summary}}
{{/has_excluded}}

{{#has_sector_summary}}
📈 板块: {{sector_strong_summary}} | 📉 {{sector_weak_summary}}
{{/has_sector_summary}}

---

*免责声明：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。*