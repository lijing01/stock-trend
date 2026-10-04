"""Markdown/HTML rendering for the frozen market explanation contract."""

from html import escape


_QUALITY_NOTE_LABELS = {
    "source_timestamp_unknown": (
        "来源事件时刻未记录；交易日资格与事件时效分别核验"
    ),
    "legacy_context_evidence_unknown": "旧缓存缺少来源证据",
    "legacy_evidence_unknown": "旧缓存来源证据未知",
    "intraday_anchor_missing": "盘中锚点缺失，无法复算混合分",
}

_QUALIFICATION_LABELS = {
    "qualified": "交易日已核验",
    "mismatched": "来源日期不一致",
    "unknown": "来源日期未知",
    "partial": "组件证据不完整",
    "missing": "组件证据缺失",
}


def _quality_note_label(value):
    return _QUALITY_NOTE_LABELS.get(str(value), str(value))


def _cell(value):
    return str(value if value not in (None, "") else "—").replace("|", r"\|").replace("\n", " ")


def _evidence_summary(component):
    evidence = component.get("evidence") or {}
    return "; ".join(
        f"{key}={_cell(evidence.get(key))}"
        for key in ("completeness", "freshness", "source_kind", "usage", "date_origin", "alignment")
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


def _qualification_summary(explanation):
    qualification = explanation.get("conclusion_qualification") or {}
    status = qualification.get("status") or "unknown"
    label = _QUALIFICATION_LABELS.get(status, str(status))
    if qualification.get("eligible") and qualification.get("event_time_status") == "unknown":
        label = "交易日已核验，事件时刻未知"
    elif qualification.get("date_alignment") == "unknown" and status != "unknown":
        label += "；来源日期未知"
    elif qualification.get("date_alignment") == "mismatched" and status != "mismatched":
        label += "；来源日期不一致"
    return qualification, label


def _render_markdown(explanation):
    qualification, qualification_label = _qualification_summary(explanation)
    qualified = qualification.get("eligible") is True
    score_label = "模型计算分" if qualified else "参考评分（模型计算分）"
    lines = [
        "### 市场环境解释",
        "",
        f"- {score_label}：**{_cell(explanation.get('score'))}** / 100",
        f"- 结论资格：{_cell(qualification_label)}；"
        f"日期对齐 {_cell(qualification.get('date_alignment'))}；"
        f"事件时效 {_cell(qualification.get('freshness'))}",
        f"- 口径：{_cell(explanation.get('mode'))}；基准日 {_cell(explanation.get('basis_date'))}；"
        f"归一化分母 {_cell(explanation.get('normalization_denominator'))}",
        "- 结论资格原因：" + ("、".join(_cell(item) for item in qualification.get("reasons") or []) or "无"),
        f"- 计算校验：{_cell(explanation.get('reconciliation'))}；"
        f"原始加权合计 {_cell(explanation.get('raw_weighted_total'))}",
        f"- {_index_summary(explanation)}",
    ]
    if not qualified:
        lines.extend(["- 证据不足，以下为模型提示。"])
    lines.extend([
        "",
        "| 组件 | 得分 | 权重 | 贡献 | 证据资格 | 说明 |",
        "|---|---:|---:|---:|---|---|",
    ])
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
        "- 数据质量说明：" + (
            "、".join(_cell(_quality_note_label(item)) for item in notes) or "无"
        ),
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

    qualification, qualification_label = _qualification_summary(explanation)
    qualified = qualification.get("eligible") is True
    score_label = "模型计算分" if qualified else "参考评分（模型计算分）"
    model_note = "" if qualified else "<p><strong>证据不足，以下为模型提示。</strong></p>"
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
    notes = "、".join(
        h(_quality_note_label(item))
        for item in explanation.get("quality_notes", [])
    ) or "无"
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
        f"<p>{h(score_label)}：<strong>{h(explanation.get('score'))}</strong> / 100；"
        f"口径：{h(explanation.get('mode'))}；基准日 {h(explanation.get('basis_date'))}；"
        f"归一化分母 {h(explanation.get('normalization_denominator'))}；"
        f"计算校验 {h(explanation.get('reconciliation'))}</p>"
        f"<p>结论资格：{h(qualification_label)}；"
        f"日期对齐 {h(qualification.get('date_alignment'))}；"
        f"事件时效 {h(qualification.get('freshness'))}</p>"
        "<p>结论资格原因：" + ("、".join(h(item) for item in qualification.get("reasons") or []) or "无") + "</p>"
        f"{model_note}"
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
