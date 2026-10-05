"""Render one observation-comparison state for HTML and Markdown reports."""

import html

EVENT_LABELS = {
    "structure_invalidated": "结构失效",
    "structure_restored": "结构恢复",
    "data_recovered": "数据恢复",
    "confirmed_evidence": "确认事件",
}

REASON_LABELS = {
    "legacy_collection_only": "旧版记录仅支持集合变化",
    "model_version_mismatch": "评分模型版本不同",
    "code_reentered": "代码移除后重新加入",
    "row_config_changed": "观察行配置已变化",
    "quality_method_mismatch": "数据质量方法不同",
    "quality_changed": "数据质量不同",
    "score_evidence_incomplete": "分数证据不完整",
}


def _identity(item):
    return f"{item.get('market') or '未知'}:{item.get('code') or '未知'}"


def _signed(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "不可比"
    return f"{value:+g}"


def _collection_lines(state):
    changes = state.get("collection_changes") or {}
    wording = changes.get("wording")
    prefix = "YAML" if wording == "yaml_config" else "记录集合"
    lines = []
    if changes.get("added"):
        lines.append((f"{prefix} 新增", "、".join(
            _identity(item) for item in changes["added"])))
    if changes.get("removed"):
        lines.append((f"{prefix} 移除", "、".join(
            _identity(item) for item in changes["removed"])))
    return lines


def _event_text(event):
    label = EVENT_LABELS.get(event.get("type"), event.get("type") or "变化")
    if event.get("type") == "confirmed_evidence":
        label += f" {event.get('event_type') or '未命名'}"
        if event.get("confirmation_date"):
            label += f"（{event['confirmation_date']}）"
    return f"{_identity(event)}：{label}"


def render_html(state):
    state = state if isinstance(state, dict) else {}
    status = str(state.get("status") or "degraded")
    chunks = [
        f'<div class="observation-comparison" '
        f'data-observation-comparison-status="{html.escape(status, quote=True)}">',
        "<h3>观察列表变化</h3>",
    ]
    reason = str(state.get("reason") or "")
    if reason:
        chunks.append(f'<p class="dt">{html.escape(reason)}</p>')
    collection = _collection_lines(state)
    events = state.get("events") or []
    comparable = [row for row in (state.get("rows") or [])
                  if row.get("score_status") == "comparable"]
    if collection or events:
        chunks.append("<ul>")
        chunks.extend(
            f"<li>{html.escape(label)}：{html.escape(value)}</li>"
            for label, value in collection)
        chunks.extend(f"<li>{html.escape(_event_text(event))}</li>"
                      for event in events)
        chunks.append("</ul>")
    if comparable:
        chunks.append(
            '<div class="observation-table-wrap"><table><thead><tr>'
            "<th>标的</th><th>六维原始分变化</th><th>质量调整分变化</th>"
            "</tr></thead><tbody>")
        for row in comparable:
            chunks.append(
                f"<tr><td>{html.escape(_identity(row))}</td>"
                f"<td>{html.escape(_signed(row.get('raw_score_delta')))}</td>"
                f"<td>{html.escape(_signed(row.get('quality_adjusted_score_delta')))}</td></tr>")
        chunks.append("</tbody></table></div>")
    if not collection and not events and not comparable and not reason:
        chunks.append('<p class="dt">相邻交易日没有可展示的观察变化。</p>')
    guarded = [row for row in (state.get("rows") or [])
               if row.get("reason_code")]
    if guarded:
        summary = "；".join(
            f"{_identity(row)} {REASON_LABELS.get(row['reason_code'], row['reason_code'])}"
            for row in guarded)
        chunks.append(f'<p class="dt">不可比：{html.escape(summary)}</p>')
    chunks.append("</div>")
    return "\n".join(chunks)


def _md(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(state):
    state = state if isinstance(state, dict) else {}
    lines = ["### 观察列表变化", ""]
    reason = str(state.get("reason") or "")
    if reason:
        lines.extend([reason, ""])
    for label, value in _collection_lines(state):
        lines.append(f"- {label}：{value}")
    for event in (state.get("events") or []):
        lines.append(f"- {_event_text(event)}")
    comparable = [row for row in (state.get("rows") or [])
                  if row.get("score_status") == "comparable"]
    if comparable:
        lines.extend([
            "", "| 标的 | 六维原始分变化 | 质量调整分变化 |",
            "|---|---:|---:|",
        ])
        lines.extend(
            f"| {_md(_identity(row))} | {_signed(row.get('raw_score_delta'))} | "
            f"{_signed(row.get('quality_adjusted_score_delta'))} |"
            for row in comparable)
    if len(lines) == 2 and not reason:
        lines.append("相邻交易日没有可展示的观察变化。")
    guarded = [row for row in (state.get("rows") or [])
               if row.get("reason_code")]
    if guarded:
        summary = "；".join(
            f"{_identity(row)} {REASON_LABELS.get(row['reason_code'], row['reason_code'])}"
            for row in guarded)
        lines.extend(["", f"不可比：{summary}"])
    return "\n".join(lines).rstrip() + "\n"


render_observation_comparison_html = render_html
render_observation_comparison_markdown = render_markdown
