"""Render independent LPS trade-assessment artifacts as JSON, Markdown or HTML."""

import json
from html import escape


DISCLAIMER = "本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。"


def _text(value, default="—"):
    return default if value in (None, "") else str(value)


def _pct(value):
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def render_json(artifact):
    return json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def render_markdown(artifact):
    lines = [
        "# LPS 独立交易评估", "",
        f"依据日：{_text(artifact.get('basis_date'))}  ",
        f"评价截至日：{_text(artifact.get('evaluation_as_of'))}  ",
        f"运行状态：{_text(artifact.get('status'))}  ",
        "成交口径：下一有效交易日开盘一次性机会；未落入入场区不成交。", "",
    ]
    for item in artifact.get("items") or []:
        plan = item.get("plan") or {}
        evidence = item.get("lps_evidence") or {}
        entry = plan.get("entry") or {}
        stop = plan.get("stop_loss") or {}
        targets = plan.get("targets") or {}
        rr = plan.get("risk_reward") or {}
        position = plan.get("position") or {}
        costs = plan.get("cost_config") or {}
        simulation = item.get("simulation") or {}
        lines.extend([
            f"## {_text(item.get('name'), '')}({_text(item.get('code'))})", "",
            f"- 状态：**{_text(item.get('status'))}**；原正式分桶：{_text(item.get('formal_bucket'))}",
            f"- 证据：记录 `{_text(item.get('record_id'))}`；快照 `{_text(item.get('research_snapshot_sha256'))}`；最终收盘 {_text((evidence.get('close_evidence') or {}).get('status'))}",
            f"- LPS：触发价 {_text(evidence.get('trigger_close'))}；距离 {_text(evidence.get('distance_atr'))} ATR / {_pct(evidence.get('distance_pct'))}",
            f"- 计划：入场 {_text(entry.get('low'))}–{_text(entry.get('high'))}；有效交易日 {_text((plan.get('validity') or {}).get('entry_session') or item.get('next_entry_session'))}",
            f"- 风险：结构失效/止损 {_text(stop.get('price'))}（{_text(stop.get('trigger_mode'))}）；目标 {_text(targets.get('primary'))}（{_text(targets.get('source'))}）",
            f"- R:R：入场下沿 {_text(rr.get('rr_at_entry_low'))}；上沿 {_text(rr.get('rr_at_entry_high'))}；最低 {_text(rr.get('minimum'))}",
            f"- 风险预算：{_text(position.get('risk_budget_pct'))}%；正式仓位上限 {_text(position.get('max_portfolio_pct'))}%",
            f"- 事件复核：{_text((item.get('event_check') or {}).get('display_status'))}；成本：{_text(costs.get('contract_id') or costs.get('status'), '未知')}",
            f"- 未成交/失效：{_text(plan.get('invalidation'))}",
            f"- 反方论点：{_text(plan.get('counterargument'))}",
            f"- 模拟结果：{_text(simulation.get('opportunity_status') or '尚未评价')}；净收益资格 {_text(plan.get('net_return_eligible'))}",
        ])
        reasons = item.get("status_reasons") or []
        if reasons:
            lines.append(f"- 限制原因：{', '.join(map(str, reasons))}")
        simulations = item.get("simulations") or []
        if simulations:
            lines.extend(["", "| 窗口 / 成本 | 机会状态 | 成交 / 退出 | 研究净收益 / 超额 | 保守 MAE | 原因 |",
                          "|---|---|---|---|---|---|"])
            for row in simulations:
                execution = row.get("execution") or {}
                returns = row.get("returns") or {}
                alpha = ((row.get("benchmark") or {}).get("actual_exit") or {}).get("net_excess_return")
                lines.append(f"| {row.get('window')} / {row.get('cost_scenario')} | {row.get('opportunity_status')} | {_text(execution.get('entry_date'))} / {_text(execution.get('exit_date'))} | {_pct(returns.get('net_return'))} / {_pct(alpha)} | {_pct((row.get('risk') or {}).get('conservative_mae'))} | {_text(execution.get('reason') or execution.get('valuation_reason') or execution.get('exit_reason'))} |")
        lines.append("")
    summary = artifact.get("summary") or {}
    if summary:
        lines.extend(["## 机会统计", "", f"冻结 LPS 机会：{summary.get('opportunities', 0)}。重复股票为独立单笔机会，不构成组合回测。", ""])
        for row in summary.get("contracts") or []:
            lines.append(f"- {row.get('window')} 日 / {row.get('cost_scenario')}：{row.get('opportunities')} 个机会，{row.get('filled_opportunities')} 个假设成交，{row.get('completed_trades')} 个完成交易；净收益有效样本 {row.get('net_return_samples')}，均值 {_pct(row.get('net_return_mean'))}；状态 {row.get('status_counts')}。")
        lines.append("")
    lines.extend([
        "## 统一假设与限制", "",
        "- 可制定交易计划仅表示证据和计划字段完整，不保证成交或盈利。",
        "- 日 K 模拟不能证明真实成交；跳空、停牌和一字跌停可能造成超出计划的损失。",
        "- 风险预算与成本均为研究先验，不代表个人账户配置或实际费率。", "",
        "- 研究账户 100 万元，仅计算单笔机会；0.5% 风险预算，100 股整数手，单票名义上限 20%，另受正式市场额度约束。",
        "- 参考成本：每边佣金 3 bps、最低 5 元（含经手/过户），卖出印花税 5 bps，买卖滑点各 5 bps；压力滑点各 15 bps。费用来源及适用日期冻结于 JSON 合同。",
        "- 未卖出仅报告未实现值；假设清仓费用单列。保守 MAE 包含退出日整根 K 线，不代表精确成交风险。", "",
        DISCLAIMER, "",
    ])
    return "\n".join(lines)


def render_html(artifact):
    def h(value, default="—"):
        return escape(_text(value, default), quote=True)

    cards = []
    for item in artifact.get("items") or []:
        plan = item.get("plan") or {}
        evidence = item.get("lps_evidence") or {}
        entry = plan.get("entry") or {}
        stop = plan.get("stop_loss") or {}
        targets = plan.get("targets") or {}
        rr = plan.get("risk_reward") or {}
        costs = plan.get("cost_config") or {}
        reasons = "、".join(h(value) for value in item.get("status_reasons") or []) or "无"
        cards.append(
            "<section class='card'>"
            f"<h2>{h(item.get('name'), '')}({h(item.get('code'))})</h2>"
            f"<p><strong>{h(item.get('status'))}</strong> · 原正式分桶 {h(item.get('formal_bucket'))}</p>"
            "<dl>"
            f"<dt>证据</dt><dd>记录 {h(item.get('record_id'))}；快照 {h(item.get('research_snapshot_sha256'))}；最终收盘 {h((evidence.get('close_evidence') or {}).get('status'))}</dd>"
            f"<dt>LPS</dt><dd>触发价 {h(evidence.get('trigger_close'))}；距离 {h(evidence.get('distance_atr'))} ATR / {h(_pct(evidence.get('distance_pct')))}</dd>"
            f"<dt>计划</dt><dd>入场 {h(entry.get('low'))}–{h(entry.get('high'))}；有效日 {h((plan.get('validity') or {}).get('entry_session') or item.get('next_entry_session'))}</dd>"
            f"<dt>风险</dt><dd>止损 {h(stop.get('price'))}（{h(stop.get('trigger_mode'))}）；目标 {h(targets.get('primary'))}（{h(targets.get('source'))}）</dd>"
            f"<dt>R:R</dt><dd>下沿 {h(rr.get('rr_at_entry_low'))}；上沿 {h(rr.get('rr_at_entry_high'))}；最低 {h(rr.get('minimum'))}</dd>"
            f"<dt>事件/成本</dt><dd>{h((item.get('event_check') or {}).get('display_status'))}；{h(costs.get('contract_id') or costs.get('status'), '未知')}</dd>"
            f"<dt>未成交/失效</dt><dd>{h(plan.get('invalidation'))}</dd>"
            f"<dt>反方论点</dt><dd>{h(plan.get('counterargument'))}</dd>"
            f"<dt>限制原因</dt><dd>{reasons}</dd>"
            "</dl></section>"
        )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>LPS 独立交易评估</title><style>body{font-family:system-ui,sans-serif;"
        "max-width:980px;margin:32px auto;padding:0 18px;color:#17202a}"
        ".card{border:1px solid #d5d8dc;border-radius:10px;padding:16px;margin:16px 0}"
        "dt{font-weight:700;margin-top:8px}dd{margin-left:0}</style></head><body>"
        f"<h1>LPS 独立交易评估</h1><p>依据日 {h(artifact.get('basis_date'))}；"
        f"评价截至日 {h(artifact.get('evaluation_as_of'))}；"
        f"运行状态 {h(artifact.get('status'))}</p>"
        "<p>下一有效交易日开盘一次性机会；未落入入场区不成交。</p>"
        + "".join(cards) + "<details open><summary>完整评估与模拟明细</summary><pre style='white-space:pre-wrap'>" + h(render_markdown(artifact)) + "</pre></details>" +
        "<section><h2>统一假设与限制</h2><ul>"
        "<li>可制定交易计划仅表示证据和计划字段完整，不保证成交或盈利。</li>"
        "<li>日 K 模拟不能证明真实成交；跳空、停牌和一字跌停可能扩大损失。</li>"
        "<li>风险预算与成本均为研究先验，不代表个人账户配置或实际费率。</li>"
        f"</ul></section><p>{h(DISCLAIMER)}</p></body></html>"
    )
