# 昨日强势股反馈缺失：规划上下文

- 用户请求：制定合理修复方案；只规划，不实施。
- 目标：同日可靠取到昨日冻结全名单的当日反馈；无法取得时可定位原因，保持时间、价格与样本口径。
- 已知：2026-10-23 15:45 运行有昨日33只名单、今日82只完整涨停池，反馈0/33；artifact只有 fetch_quote_error，未存底层错误。不能据此认定33只均网络失败，可能包含无当日记录。
- 已知：16:02重跑指数最新日期10/22，market_regime.collect_context以指数日期最大值生成 data_date，反馈据此触发 historical_live_collection_forbidden。
- 代码：fetchers/market_feedback.py仅直接调用东财日K，精确匹配日期；analysis/market_feedback_job.py冻结时遗漏 fetched.errors；其_load_bound目前要求反馈依据日等于ctx.data_date。
- 腾讯现有 fetch_tencent_a_stock 读取qfq K线并由相邻复权收盘推算pct_chg，不能直接当供应方原生日涨跌幅。BaoStock函数有会话登录且无此模块需要的严格请求截止接口，不列入首轮并发回退。
- 约束：不改五项评分/推荐门控；不放宽精确日期、冻结名单、历史只读、盘中与收盘隔离；无新依赖；4并发、30秒总预算；无GUI/历史回填。
- 未知：15:45底层逐股错误无法由现有artifact恢复；备用原生行情源日期/停牌/价格尺度/覆盖能力需用官方或供应方资料和脱敏原始响应验证。
- 工作区已有无关未跟踪文件 .omx/plans/2026-10-22-lps-early-warning-tiered-entry.md，保留。
