"""Markdown/HTML rendering for the frozen market explanation contract."""

from html import escape


def _cell(value):
    return str(value if value not in (None, "") else "—").replace("|", r"\|").replace("\n", " ")


def _evidence_summary(component):
    evidence = component.get("evidence") or {}
    return "; ".join(
        f"{key}={_cell(evidence.get(key))}"
        for key in ("completeness", "freshness", "source_kind", "usage")
    )


def _index_summary(explanation):
    index_component = next(
        (item for item in explanation.get("components", [])
         if item.get("id") == "index_trend"), None)
    evidence = (index_component or {}).get("evidence") or {}
    codes = evidence.get("index_codes") or []
    if not codes:
        return "指数名单：未知"
    return "指数名单：" + "、".join(str(code) for code in codes)


def _render_markdown(explanation):
    lines = [
        "### 市场环境解释",
        "",
        f"- 正式评分：**{_cell(explanation.get('score'))}** / 100",
        f"- 口径：{_cell(explanation.get('mode'))}；基准日 {_cell(explanation.get('basis_date'))}；"
        f"归一化分母 {_cell(explanation.get('normalization_denominator'))}",
        f"- 计算校验：{_cell(explanation.get('reconciliation'))}；"
        f"原始加权合计 {_cell(explanation.get('raw_weighted_total'))}",
        f"- {_index_summary(explanation)}",
        "",
        "| 组件 | 得分 | 权重 | 贡献 | 证据资格 | 说明 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for component in explanation.get("components", []):
        lines.append(
            f"| {_cell(component.get('name') or component.get('id'))} "
            f"({_cell(component.get('id'))}) | {_cell(component.get('score'))} | "
            f"{_cell(component.get('weight'))} | {_cell(component.get('contribution'))} | "
            f"{_evidence_summary(component)} | {_cell(component.get('detail'))} |"
        )
    reasons = explanation.get("blocking_reasons") or []
    notes = explanation.get("quality_notes") or []
    lines.extend([
        "",
        "- 限制原因：" + ("、".join(_cell(item) for item in reasons) or "无"),
        "- 数据质量说明：" + ("、".join(_cell(item) for item in notes) or "无"),
    ])
    intraday = explanation.get("intraday")
    if intraday:
        lines.extend([
            "- 盘中混合："
            f"({_cell(intraday.get('formula'))}) = {_cell(intraday.get('formula_value'))}；"
            f"锚分 {_cell(intraday.get('anchor_score'))}，"
            f"盘中外推 {_cell(intraday.get('projected_score'))}，"
            f"权重 {_cell(intraday.get('blend_weight'))}",
        ])
    return "\n".join(lines)


def _render_html(explanation):
    def h(value):
        return escape(str(value if value not in (None, "") else "—"))

    rows = []
    for component in explanation.get("components", []):
        evidence = component.get("evidence") or {}
        rows.append(
            "<tr>"
            f"<td>{h(component.get('name') or component.get('id'))}<br>"
            f"<small>{h(component.get('id'))}</small></td>"
            f"<td>{h(component.get('score'))}</td>"
            f"<td>{h(component.get('weight'))}</td>"
            f"<td>{h(component.get('contribution'))}</td>"
            f"<td>{h(_evidence_summary(component))}</td>"
            f"<td>{h(component.get('detail'))}</td>"
            "</tr>"
        )
    reasons = "、".join(h(item) for item in explanation.get("blocking_reasons", [])) or "无"
    notes = "、".join(h(item) for item in explanation.get("quality_notes", [])) or "无"
    intraday = explanation.get("intraday")
    intraday_html = ""
    if intraday:
        intraday_html = (
            "<p>盘中混合："
            f"<code>{h(intraday.get('formula'))}</code> = {h(intraday.get('formula_value'))}；"
            f"锚分 {h(intraday.get('anchor_score'))}，外推分 {h(intraday.get('projected_score'))}，"
            f"权重 {h(intraday.get('blend_weight'))}</p>"
        )
    index_summary = h(_index_summary(explanation))
    return (
        "<section class='market-explanation'>"
        "<h2>市场环境解释</h2>"
        f"<p>正式评分：<strong>{h(explanation.get('score'))}</strong> / 100；"
        f"口径：{h(explanation.get('mode'))}；基准日 {h(explanation.get('basis_date'))}；"
        f"归一化分母 {h(explanation.get('normalization_denominator'))}；"
        f"计算校验 {h(explanation.get('reconciliation'))}</p>"
        f"<p>{index_summary}</p>"
        "<table><thead><tr><th>组件</th><th>得分</th><th>权重</th><th>贡献</th>"
        "<th>证据资格</th><th>说明</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        f"<p>限制原因：{reasons}</p>"
        f"<p>数据质量说明：{notes}</p>"
        + intraday_html
        + "</section>"
    )


def render_market_explanation(explanation, format="markdown"):
    """Render one frozen explanation as markdown or HTML only."""
    if not isinstance(explanation, dict):
        raise TypeError("explanation must be a dict")
    if format == "markdown":
        return _render_markdown(explanation)
    if format == "html":
        return _render_html(explanation)
    raise ValueError("format must be markdown or html")
