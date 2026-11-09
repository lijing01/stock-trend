"""Render the daily weak-to-strong V4 market result."""

from html import escape


DISPLAY_LIMIT = 30
V4_TYPES = (("T1", "T1 新确认"), ("T2", "T2 新确认"))
V4_HEADERS = [
    "类型内名次", "名称/代码", "V4 / V3 分", "预期净收益", "估计胜率",
    "信号日 / 特征日", "模型版本 / 哈希", "状态",
]


def _value(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _number(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _selection_summary(stats):
    return (
        f"展示 {stats['displayed']} 只；未展示 {stats['not_displayed']} 只"
        f"（展示上限省略 {stats['omitted_by_limit']} 只）。"
    )


def _v4_candidates(payload):
    value = payload.get("research_candidates") or {}
    return value if isinstance(value, dict) else {}


def _v4_percent(value):
    number = _number(value)
    return "—" if number is None else f"{number:.2%}"


def _v4_identity(item):
    symbol = item.get("symbol") or item.get("code") or item.get("ts_code") or ""
    name = item.get("name") or symbol
    return f"{name} ({symbol})" if name != symbol else str(symbol)


def _v4_reason_text(item):
    reasons = item.get("classification_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    prediction = item.get("prediction") or {}
    if not isinstance(prediction, dict):
        prediction = {}
    reason = item.get("reason") or prediction.get("fallback_reason")
    values = [str(value) for value in reasons if value not in (None, "")]
    if reason not in (None, ""):
        values.append(str(reason))
    return "、".join(dict.fromkeys(values)) or "未记录"


def _v4_intraday_text(item):
    v3 = item.get("v3_result")
    if not isinstance(v3, dict) or v3.get("provisional") is not True:
        return ""
    if v3.get("current_bar_evaluated") is not True:
        return "盘中当日K线未完成评估"
    if v3.get("evidence_status") != "ready":
        return "已评估盘中当日K线，但结构证据不足"
    stage = v3.get("stage_label") or {
        "PREPARING": "准备中", "EARLY_REPAIR": "早期修复",
        "LOCAL_CONFIRMATION": "局部确认", "RANGE_BREAKOUT": "区间突破",
    }.get(v3.get("stage"), "无活跃结构")
    lifecycle = v3.get("lifecycle_status")
    status = {"active": "活跃", "invalidated": "已失效", "expired": "已过期",
              "completed": "已完成"}.get(lifecycle)
    suffix = f"，状态：{status}" if status else ""
    return f"盘中当日K线已评估，结构：{stage}{suffix}（临时，收盘待确认）"


def _v4_cells(item, signal_type, fallback_rank):
    prediction = item.get("prediction") or {}
    if not isinstance(prediction, dict):
        prediction = {}
    feature = item.get("feature_snapshot") or {}
    if not isinstance(feature, dict):
        feature = {}
    rank = item.get("v4_type_rank") or fallback_rank
    model_version = prediction.get("model_version") or item.get("model_version")
    model_hash = prediction.get("model_hash") or item.get("model_hash")
    signal_date = prediction.get("signal_date") or item.get("as_of_date")
    feature_date = prediction.get("feature_asof") or feature.get("feature_asof")
    score_status = prediction.get("score_status") or item.get("status")
    return [
        f"{signal_type}-{rank}", _v4_identity(item),
        f"{_value(prediction.get('v4_score'))} / {_value(prediction.get('v3_score'))}",
        _v4_percent(prediction.get("expected_net_return")),
        _v4_percent(prediction.get("estimated_win_rate")),
        f"{_value(signal_date)} / {_value(feature_date)}",
        f"{_value(model_version)} / {_value(model_hash)}", _value(score_status),
    ]


def select_v4_rows(payload, limit=DISPLAY_LIMIT):
    """Select V4 candidates while preserving source type-local ranking."""
    groups = {signal_type: [] for signal_type, _label in V4_TYPES}
    candidates = _v4_candidates(payload)
    scanned = 0
    remaining = (
        max(0, int(limit))
        if isinstance(limit, (int, float)) and not isinstance(limit, bool)
        else DISPLAY_LIMIT
    )
    for signal_type, _label in V4_TYPES:
        rows = candidates.get(signal_type) or []
        if not isinstance(rows, list):
            continue
        valid = [row for row in rows if isinstance(row, dict)]
        scanned += len(valid)
        groups[signal_type] = valid[:remaining]
        remaining -= len(groups[signal_type])
    displayed = sum(len(rows) for rows in groups.values())
    stats = {
        "scanned_rows": scanned, "eligible_securities": scanned,
        "displayed": displayed, "not_displayed": max(0, scanned - displayed),
        "omitted_by_limit": max(0, scanned - displayed),
    }
    return groups, stats


def build_v4_display_selection(payload, limit=DISPLAY_LIMIT):
    """Return the JSON companion for the V4 Markdown/HTML candidate tables."""
    groups, stats = select_v4_rows(payload, limit=limit)
    items = []
    for signal_type, _label in V4_TYPES:
        for fallback_rank, item in enumerate(groups[signal_type], 1):
            prediction = item.get("prediction") or {}
            if not isinstance(prediction, dict):
                prediction = {}
            items.append({
                "signal_type": signal_type,
                "type_rank": item.get("v4_type_rank") or fallback_rank,
                "code": str(item.get("symbol") or item.get("code") or ""),
                "event_id": str(prediction.get("event_id") or item.get("event_id") or ""),
                "reason": "expected_net_return_then_win_rate",
            })
    return {
        "schema_version": "weak-to-strong-display-selection/v4",
        "limit": limit, "stats": stats, "items": items,
    }


def _v4_summary(payload, stats):
    summary = payload.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    structural = summary.get(
        "structural_observations", len(payload.get("structural_observations") or []))
    inactive = summary.get(
        "no_active_signal", len(payload.get("no_active_signal") or []))
    insufficient = summary.get(
        "data_insufficient", len(payload.get("data_insufficient") or []))
    return (
        f"依据日 {_value(payload.get('as_of_date') or payload.get('requested_as_of_date'))}；"
        f"模型状态 {_value(payload.get('model_status'))}；V4 候选 {stats['scanned_rows']} 只"
        f"（T1 {len((_v4_candidates(payload).get('T1') or []))}，"
        f"T2 {len((_v4_candidates(payload).get('T2') or []))}）；"
        f"结构观察 {structural} 只，无活跃信号 {inactive} 只，数据不足 {insufficient} 只。"
    )


def _v4_diagnostic_rows(payload):
    groups = (
        ("structural_observations", "结构观察"),
        ("no_active_signal", "无活跃信号"),
        ("data_insufficient", "数据不足"),
    )
    rows = []
    for key, label in groups:
        values = payload.get(key) or []
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                detail = _v4_intraday_text(item)
                reason = _v4_reason_text(item)
                rows.append((label, _v4_identity(item),
                             f"{reason}；{detail}" if detail else reason))
    return rows


def _v4_context(payload):
    notes = []
    if payload.get("model_status") == "model_missing":
        notes.append("V4 模型未配置，无法计算预期收益、胜率或 V4 排名；结构观察仍可参考。")
    items = (item for key in ("structural_observations", "no_active_signal", "data_insufficient")
             for item in (payload.get(key) or []) if isinstance(item, dict))
    if any(isinstance(item.get("v3_result"), dict)
           and item["v3_result"].get("provisional") is True for item in items):
        notes.append("盘中使用当日临时日线评估结构，价格及成交量可能变化，尚非收盘确认信号；盘后需重新拉取日线并复评。")
    return notes


def render_v4_markdown(payload):
    """Render the V4 weak-to-strong report as the primary algorithm view."""
    if payload is None:
        return ""
    selected, stats = select_v4_rows(payload)
    lines = [
        "## 日线弱转强 V4", "",
        "T1 与 T2 分组独立排序；排序依据为预期净收益，其次为估计胜率。", "",
        _v4_summary(payload, stats), "", _selection_summary(stats),
    ]
    for note in _v4_context(payload):
        lines += ["", note]
    any_rows = False
    for signal_type, label in V4_TYPES:
        rows = selected[signal_type]
        if not rows:
            continue
        any_rows = True
        lines += [
            "", f"### {label}", "", "| " + " | ".join(V4_HEADERS) + " |",
            "| " + " | ".join(["---"] * len(V4_HEADERS)) + " |",
        ]
        for fallback_rank, item in enumerate(rows, 1):
            lines.append("| " + " | ".join(
                _markdown_cell(cell)
                for cell in _v4_cells(item, signal_type, fallback_rank)
            ) + " |")
    if not any_rows:
        lines += ["", "当日没有可评分的 V4 T1/T2 新确认信号。"]
    diagnostics = _v4_diagnostic_rows(payload)
    if diagnostics:
        lines += ["", "<details><summary>V4 状态与诊断</summary>", ""]
        lines.extend(
            f"- {_markdown_cell(label)}｜{_markdown_cell(identity)}：{_markdown_cell(reason)}"
            for label, identity, reason in diagnostics
        )
        lines += ["", "</details>"]
    return "\n".join(lines)


def render_v4_html(payload):
    """Render the V4 weak-to-strong report with all dynamic values escaped."""
    if payload is None:
        return ""
    selected, stats = select_v4_rows(payload)
    out = [
        "<section class='candidate-section weak-to-strong-v4'>",
        "<h2>日线弱转强 V4</h2>",
        "<p>T1 与 T2 分组独立排序；排序依据为预期净收益，其次为估计胜率。</p>",
        "<p>" + escape(_v4_summary(payload, stats)) + "</p>",
        "<p>" + escape(_selection_summary(stats)) + "</p>",
    ]
    out.extend("<p>" + escape(note) + "</p>" for note in _v4_context(payload))
    any_rows = False
    for signal_type, label in V4_TYPES:
        rows = selected[signal_type]
        if not rows:
            continue
        any_rows = True
        out.append("<h3>" + escape(label) + "</h3>")
        out.append(
            "<div class='candidate-table-wrap'><table class='weak-to-strong-table weak-to-strong-v4-table'>"
            "<thead><tr>" + "".join(
                "<th>" + escape(header) + "</th>" for header in V4_HEADERS
            ) + "</tr></thead><tbody>"
        )
        for fallback_rank, item in enumerate(rows, 1):
            cells = _v4_cells(item, signal_type, fallback_rank)
            out.append("<tr>" + "".join(
                "<td>" + escape(str(cell)) + "</td>" for cell in cells
            ) + "</tr>")
        out.append("</tbody></table></div>")
    if not any_rows:
        out.append("<p>当日没有可评分的 V4 T1/T2 新确认信号。</p>")
    diagnostics = _v4_diagnostic_rows(payload)
    if diagnostics:
        out.append("<details><summary>V4 状态与诊断</summary><ul>")
        out.extend(
            "<li>" + escape(f"{label}｜{identity}：{reason}") + "</li>"
            for label, identity, reason in diagnostics
        )
        out.append("</ul></details>")
    return "".join(out) + "</section>"
