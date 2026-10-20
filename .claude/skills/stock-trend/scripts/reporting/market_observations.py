"""Render the frozen market observation/model-input audit contract."""

from __future__ import annotations

from html import escape


START = "<!-- MARKET_OBSERVATIONS:START -->"
END = "<!-- MARKET_OBSERVATIONS:END -->"

_ROWS = (
    ("volume", "两市成交额", (("amount_yi", "亿"),), (("amount_yi", "亿"),)),
    ("breadth", "涨跌家数", (
        ("up", "家上涨"), ("down", "家下跌"),
        ("flat_count", "家平盘"), ("total_count", "家市场总数"),
        ("up_down_sample_count", "家涨跌样本"),
        ("suspended_count", "家停牌"), ("uncovered_count", "家未覆盖"),
    ), (("up", "家上涨"), ("down", "家下跌"))),
    ("zt_emotion", "涨停情绪", (
        ("count", "家涨停"), ("streak_count", "家连板"),
        ("max_streak", "板最高"),
    ), (("count", "家涨停"), ("streak_count", "家连板"), ("max_streak", "板最高"))),
    ("capital", "全市场主力净流入", (("main_force_yi", "亿"),), (("main_force_yi", "亿"),)),
)

_MODEL_KIND_LABELS = {
    "projection": "盘中外推输入",
    "anchor": "昨收锚定输入",
    "observed": "原始观测输入",
    "legacy_intraday": "旧版盘中模型输入",
    "legacy_close": "旧版收盘模型输入",
    "unknown": "模型口径未知",
}

_STATUS_LABELS = {
    "complete": "完整",
    "partial": "部分",
    "missing": "缺失",
    "unknown": "未知",
    "good": "完整",
}

_ALIGNMENT_LABELS = {
    "matched": "与报告依据日一致",
    "mismatched": "与报告依据日不一致",
    "unknown": "与报告依据日未核验",
}

_MISSING_FIELD_LABELS = {
    "amount_yi": "成交额",
    "up": "上涨家数",
    "down": "下跌家数",
    "flat_count": "平盘数",
    "total_count": "市场总数",
    "up_down_sample_count": "涨跌样本数",
    "suspended_count": "停牌数",
    "uncovered_count": "未覆盖数",
    "count": "涨停数",
    "streak_count": "连板数",
    "max_streak": "最高板",
    "main_force_yi": "主力净流入",
}


def _value(value, suffix=""):
    return "未保存" if value is None else f"{value}{suffix}"


def _group_value(group, fields):
    if not isinstance(group, dict):
        group = {}
    values = []
    for field, suffix in fields:
        value = group.get(field)
        values.append(
            f"{_MISSING_FIELD_LABELS.get(field, field)}未保存"
            if value is None else _value(value, suffix)
        )
    return "、".join(values) if values else "未保存"


def _date_text(source):
    if not isinstance(source, dict):
        source = {}
    provider_date = source.get("provider_date") or "未知"
    requested = source.get("requested_date")
    event_time = source.get("event_time") or "未知"
    alignment = _ALIGNMENT_LABELS.get(
        str(source.get("alignment") or "unknown"), "与报告依据日未核验")
    text = f"来源交易日 {provider_date}（{alignment}）；来源事件时刻 {event_time}"
    if requested:
        text += f"；请求日 {requested}"
    return text


def _score_lines(observations):
    scores = observations.get("score_context") or {}
    return (
        ("昨收锚分", scores.get("anchor_score")),
        ("盘中外推分", scores.get("projected_score")),
        ("混合权重", scores.get("blend_weight")),
        ("报告分", scores.get("reported_score")),
        ("锚点日期", scores.get("anchor_date")),
        ("会话进度", scores.get("session_fraction")),
    )


def _model_kind_label(kind):
    return _MODEL_KIND_LABELS.get(str(kind), "模型口径未知")


def _status_label(status):
    return _STATUS_LABELS.get(str(status or "unknown"), "未知")


def _basis_text(observations):
    return (
        f"报告依据日 {_value(observations.get('basis_date'))}；"
        f"生成时间 {_value(observations.get('generated_at'))}；"
        f"观测截止 {_value(observations.get('as_of'))}"
    )


def _md(value):
    return escape(str(value), quote=False).replace("|", r"\|").replace("\n", " ")


def _render_markdown(observations):
    model = observations.get("model_inputs") or {}
    kind = str(model.get("kind") or "unknown")
    kind_label = _model_kind_label(kind)
    lines = [
        START,
        '<a id="market-observations"></a>',
        "",
        "### 原始观测与模型口径",
        "",
        f"- 模型输入类型：{_md(kind_label)}",
        f"- {_md(_basis_text(observations))}",
        "- " + "；".join(f"{name} {_value(value)}" for name, value in _score_lines(observations)),
        "",
        "| 指标 | 原始观测 | 模型输入 | 状态与日期 |",
        "|---|---:|---:|---|",
    ]
    raw_inputs = observations.get("raw_inputs") or {}
    for key, name, raw_fields, model_fields in _ROWS:
        raw = raw_inputs.get(key) or {}
        model_group = model.get(key) or {}
        source = raw.get("source") or {}
        lines.append(
            f"| {_md(name)} | {_md(_group_value(raw, raw_fields))} | "
            f"{_md(_group_value(model_group, model_fields))} | "
            f"{_md(_status_label(source.get('status')))}；{_md(_date_text(source))} |"
        )
    breadth = raw_inputs.get("breadth") or {}
    if breadth.get("flat_count") is None or breadth.get("total_count") is None:
        missing = []
        if breadth.get("flat_count") is None:
            missing.append("平盘数")
        if breadth.get("total_count") is None:
            missing.append("市场总数")
        lines.append(
            "\n- 上涨/下跌为已保存涨跌样本；"
            + "、".join(missing) + "未保存，因此该样本不代表市场总数。")
    lines.extend([
        "- 主力净流入采用东方财富算法统计口径，成交额与换手率分别计量。",
        "- 原始观测为本次抓取记录；来源事件时刻缺失时保持未知，未核验为实时或收盘最终值。",
        END,
    ])
    return "\n".join(lines)


def _render_html(observations):
    def h(value):
        return escape(str(value), quote=True)

    model = observations.get("model_inputs") or {}
    kind = str(model.get("kind") or "unknown")
    kind_label = _model_kind_label(kind)
    raw_inputs = observations.get("raw_inputs") or {}
    rows = []
    for key, name, raw_fields, model_fields in _ROWS:
        raw = raw_inputs.get(key) or {}
        model_group = model.get(key) or {}
        source = raw.get("source") or {}
        rows.append(
            "<tr>"
            f"<th scope=\"row\">{h(name)}</th>"
            f"<td>{h(_group_value(raw, raw_fields))}</td>"
            f"<td>{h(_group_value(model_group, model_fields))}</td>"
            f"<td>{h(_status_label(source.get('status')))}；{h(_date_text(source))}</td>"
            "</tr>"
        )
    score_text = "；".join(
        f"{name} {h(_value(value))}" for name, value in _score_lines(observations)
    )
    sample_note = ""
    breadth = raw_inputs.get("breadth") or {}
    if breadth.get("flat_count") is None or breadth.get("total_count") is None:
        missing = []
        if breadth.get("flat_count") is None:
            missing.append("平盘数")
        if breadth.get("total_count") is None:
            missing.append("市场总数")
        sample_note = (
            "<p>上涨/下跌为已保存涨跌样本；" + h("、".join(missing))
            + "未保存，因此该样本不代表市场总数。</p>")
    return (
        START
        + '<section class="market-observations" id="market-observations">'
        + "<style>"
        + ".market-observations .market-observations-table-wrap{overflow-x:auto;max-width:100%;}"
        + ".market-observations .market-observations-table-wrap>table{min-width:760px;}"
        + "</style>"
        + "<h2>原始观测与模型口径</h2>"
        + f"<p>模型输入类型：{h(kind_label)}</p>"
        + f"<p>{h(_basis_text(observations))}</p>"
        + f"<p>{score_text}</p>"
        + '<div class="market-observations-table-wrap"><table><thead><tr>'
        + "<th>指标</th><th>原始观测</th><th>模型输入</th><th>状态与日期</th>"
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + sample_note
        + "<p>主力净流入采用东方财富算法统计口径，成交额与换手率分别计量。</p>"
        + "<p>原始观测为本次抓取记录；来源事件时刻缺失时保持未知，未核验为实时或收盘最终值。</p>"
        + "</section>"
        + END
    )


def render_market_observations(observations, output_format="markdown", **options):
    """Render a detached observation contract as Markdown or HTML."""
    if not isinstance(observations, dict):
        raise TypeError("observations must be a dict")
    if "format" in options:
        output_format = options.pop("format")
    if options:
        raise TypeError(f"unexpected options: {', '.join(sorted(options))}")
    if output_format == "markdown":
        return _render_markdown(observations)
    if output_format == "html":
        return _render_html(observations)
    raise ValueError("output_format must be markdown or html")
