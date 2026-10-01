# 独立交易评估输入契约

入口：`analysis/trade_assessment.py --as-of YYYY-MM-DD --context-file FILE --json --save`。
不指定文件也可生成报告：缺证据的机会会保留为数据不足。输入为离线证据，不能把本示例的虚构字段当作真实行情。

JSON 顶层：`{"schema_version":"trade-assessment-context/v1","opportunities":{"记录ID@研究快照完整SHA256":{"decision_context":{},"market_data":{}}}}`。
以记录与快照共同身份关联，不接受仅代码关联。读取截至日前最新冻结正式快照；依据日与截至日分别展示。

## 决策证据 decision_context

| 字段 | 要求 |
|---|---|
| `known_at` | 带时区的证据可用时间，不晚于冻结记录的 `decision_at` |
| `market_sessions` | 有序去重的真实交易日 ISO 列表，含依据日与下一有效交易日 |
| `calendar_evidence` | `calendar_id/source/complete_through`，覆盖下一有效交易日 |
| `security_meta` | `code/exchange/board/security_type/is_st/ipo_special_rules/lot_size/as_of/source/price_limit_pct`；主板普通 A 股，非 ST/初期特殊规则，100 股手数、10% 规则 |
| `target_evidence` | `basis_date/source/resistances:[{price:数值}]/price_scale`；当时冻结阻力，与信号同尺度 |
| `price_scale` | `raw` 或 `adjusted` |
| `price_scale_evidence` | `source/scale_id/price_scale/known_at`，可追溯信号价格尺度与可用时间 |
| `event_check` | 未核验可用 `status:pending`；`clear/risk` 需要 `reviewed_at` 及 `evidence:[{url,published_at,known_at}]`，仅受信官方公告域，全部早于决策 |
| `cost_config` | 可选账户/参考配置；`contract_id/commission_bps/minimum_commission_cny/sell_stamp_tax_bps/slippage_bps_each_side/commission_includes_exchange_and_transfer_fees/tax_source/tax_effective_from/tax_verified_through`；缺失即 `cost_unknown`，独立参考情景不冒充账户实际费用 |
| `counterargument` | 可选人工反方论点；缺失时使用明确的形态风险说明 |

市场额度取冻结正式政策；`formal_market_allocation.allocation_pct` 只能进一步缩小额度。
当时收盘、LPS 状态、ATR、触发价及结构失效证据从研究记录复算，不从该文件覆盖。

## 后续行情 market_data

| 字段 | 要求 |
|---|---|
| `calendar` | `calendar_id/source/sessions/complete_through`，真实交易日覆盖评价截至日 |
| `metadata` | 同上述证券字段，另有 `price_scale:raw`、`rules_valid_through`，当时规则覆盖截至日；未知变化不得沿用旧规则 |
| `rows` | `[ {date,open,high,low,close,volume,limit_up_price?,limit_down_price?} ]`；原始价格，同日重复拒绝；停牌保留明确零量记录，不能把缺行当停牌 |
| `corporate_actions` | `source/complete_from/complete_through/no_actions:true`；首版仅接受持有区间无公司行动的已验证证据。公司行动现金流/股数映射未实现，其他情况数据不足 |
| `adjustment` | 调整价计划必需：`source/method:multiplicative/adjusted_equals_raw_times_factor:true/scale_id/basis_date/factors:{日期:正数}`；尺度身份和依据日须匹配计划 |
| `benchmark` | `metadata:{source,calendar_id,code:000300.SH}`，`rows:[{date,open,close}]`；按入场日开盘至实际退出日收盘的日线代理计算，另列固定窗口基准，缺基准不填零 |

参考交易规则始于 2026-07-06；税表核验截至 2026-10-01。更早规则期间或后续未知税表不产生完整净收益序列，需要独立新版来源核验与合同。
日 K 只支持假设成交。现金流保存实际发生的模拟买入/卖出费用；成交后出现数据缺口仍保留已买入事实。未完成交易不算实现净收益。

输出包括机会覆盖、成交未知数、未成交原因和各统计子分母。数据不足时成交率为空，不把缺数据解释为零成交。
输入身份包含证据、原始快照身份、实现源码身份及交易合同；结果不可覆盖，重复执行幂等。JSON 可审计，MD/HTML 为独立阅读报告。

本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。
