"""Render the daily weak-to-strong V4 market result."""

from html import escape


DISPLAY_LIMIT = 30
EARLY_DISPLAY_LIMIT = 20
V4_TYPES = (
    ("T2", "T2 新确认：放量下跌后的缩量回踩试盘"),
    ("T1", "T1 新确认：支撑位或 MA20 附近的下影线试盘"),
)
TYPE_DISPLAY_PRIORITY = tuple(signal_type for signal_type, _label in V4_TYPES)
EARLY_TIE_PRIORITY = {signal_type: rank
                      for rank, signal_type in enumerate(TYPE_DISPLAY_PRIORITY)}
V4_HEADERS = [
    "类型内名次", "名称/代码", "V4 / V3 分", "预期净收益", "估计胜率",
    "信号日 / 特征日", "模型版本 / 哈希", "状态",
]
EARLY_HEADERS = [
    "观察分 / 状态", "分项", "名称/代码", "试盘类型 / 状态", "试盘日 / 年龄", "结构阶段",
    "结构失效位", "待确认条件", "数据状态",
]

EARLY_COMPONENT_LABELS = (
    ("trend_structure", "趋势结构"),
    ("risk_space", "风险空间"),
    ("confirmation_distance", "确认距离"),
    ("probe_quality", "试探质量"),
    ("freshness", "新鲜度"),
    ("market_sector", "市场板块"),
)
EARLY_COMMON_COMPONENTS = tuple(
    key for key, _label in EARLY_COMPONENT_LABELS if key != "market_sector"
)
EARLY_RANKABLE_STATUSES = {"ready", "trial_unvalidated"}


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


def _early_observations(payload):
    value = payload.get("early_observations") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _early_score_fields(item):
    observation = _mapping(item.get("early_observation")) or item
    score_value = observation.get("observation_score")
    if score_value in (None, ""):
        score_value = item.get("observation_score")
    score = _number(score_value)
    components = _mapping(observation.get("observation_score_components"))
    if not components:
        components = _mapping(item.get("observation_score_components"))
    status = observation.get("observation_score_status") or item.get(
        "observation_score_status")
    if status in (None, ""):
        status = "ready" if score is not None else "unscored"
    return score, components, str(status)


def _early_score_coverage(item):
    observation = _mapping(item.get("early_observation")) or item
    maximum = _number(observation.get("observation_score_max"))
    if maximum is None:
        maximum = _number(item.get("observation_score_max"))
    cohort = observation.get("ranking_cohort") or item.get("ranking_cohort")
    return maximum, str(cohort) if cohort not in (None, "") else None


def _early_type(item):
    observation = _mapping(item.get("early_observation")) or item
    probe = _mapping(observation.get("probe")) or _mapping(observation.get("selected_probe"))
    pattern = str(observation.get("signal_type") or observation.get("probe_type") or
                  probe.get("pattern") or "").upper()
    if pattern.startswith("T1"):
        return "T1"
    if pattern.startswith("T2"):
        return "T2"
    return "UNKNOWN"


def select_early_observations(payload, limit=EARLY_DISPLAY_LIMIT):
    """Merge T1/T2 on the common 90-point core and return at most ``limit`` rows."""
    observations = _early_observations(payload)
    cap = (
        max(0, int(limit))
        if isinstance(limit, (int, float)) and not isinstance(limit, bool)
        else EARLY_DISPLAY_LIMIT
    )
    scored = []
    unscored = []
    for source_index, item in enumerate(observations):
        original_score, components, status = _early_score_fields(item)
        core_values = {key: _number(components.get(key)) for key in EARLY_COMMON_COMPONENTS}
        signal_type = _early_type(item)
        if (original_score is None or status not in EARLY_RANKABLE_STATUSES
                or signal_type not in {"T1", "T2"} or any(
                value is None for value in core_values.values())):
            unscored.append(item)
            continue
        merged_item = dict(item)
        source_observation = _mapping(item.get("early_observation")) or item
        observation = dict(source_observation)
        observation.update({
            "observation_score": round(sum(core_values.values()), 2),
            "observation_score_max": 90,
            "observation_score_components": core_values,
            "ranking_cohort": "T1-T2:90-common-core",
        })
        if source_observation is item:
            merged_item.update(observation)
        else:
            merged_item["early_observation"] = observation
        scored.append((merged_item, observation["observation_score"], core_values,
                       status, source_index))

    def scored_key(row):
        item, score, components, _status, source_index = row
        observation = _mapping(item.get("early_observation")) or item
        age = _number(observation.get("candidate_age_sessions"))
        symbol = str(item.get("symbol") or item.get("code") or item.get("ts_code") or "")
        signal_type = _early_type(item)
        return (-score, -len(components), EARLY_TIE_PRIORITY.get(signal_type, 99),
                age if age is not None else float("inf"), symbol, source_index)

    scored.sort(key=scored_key)
    return {
        "items": [row[0] for row in scored[:cap]],
        "max_score": 90,
        "selected_total": len(scored),
        "omitted_by_limit": max(0, len(scored) - cap),
        "scored_total": len(scored),
        "unscored_total": len(unscored),
    }


def _v4_percent(value):
    number = _number(value)
    return "—" if number is None else f"{number:.2%}"


def _v4_identity(item):
    symbol = item.get("symbol") or item.get("code") or item.get("ts_code") or ""
    name = item.get("name") or symbol or "未知名称"
    return f"{name} ({symbol or '代码缺失'})"


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


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _early_cells(item):
    observation = _mapping(item.get("early_observation")) or item
    v3 = _mapping(item.get("v3_result"))
    probe_block = _mapping(v3.get("probe"))
    probe = (_mapping(observation.get("probe")) or
             _mapping(observation.get("selected_probe")) or
             _mapping(probe_block.get("selected")))
    pattern = str(observation.get("signal_type") or observation.get("probe_type") or
                  probe.get("pattern") or "")
    probe_type = "T1" if pattern.upper().startswith("T1") else (
        "T2" if pattern.upper().startswith("T2") else _value(pattern))
    status = observation.get("probe_status") or probe.get("status") or "candidate"
    probe_status_text = {"candidate": "待确认", "confirmed": "试盘已确认"}.get(
        str(status).lower(), _value(status))
    candidate_date = (observation.get("candidate_date") or
                      observation.get("probe_date") or probe.get("candidate_date"))
    age = observation.get("candidate_age_sessions")
    if age is None:
        age = probe.get("candidate_age_sessions")
    raw_stage = observation.get("structure_stage") or observation.get("stage") or v3.get("stage")
    stage = observation.get("stage_label") or v3.get("stage_label") or {
        "PREPARING": "准备中", "EARLY_REPAIR": "早期修复",
        "LOCAL_CONFIRMATION": "局部确认", "RANGE_BREAKOUT": "区间突破",
    }.get(raw_stage, raw_stage)
    failure = (observation.get("structure_failure_line") or
               observation.get("failure_line") or observation.get("structure_low"))
    candidate_high = observation.get("candidate_high") or probe.get("high")
    confirmation_state = observation.get("confirmation_state")
    probe_confirmed = str(status).lower() == "confirmed"
    if probe_confirmed:
        confirm = "历史试盘确认条件已满足；当日无新结构确认事件，继续观察活跃结构"
    else:
        high = _number(candidate_high)
        threshold = _value(high * 1.002) if high is not None else "试盘高点×1.002"
        confirm = observation.get("confirmation_condition") or (
            f"等待收盘 > 试盘高点×1.002（{threshold}），且成交量/前5日均量 ≥ 1.15")
    provisional = bool(observation.get("provisional") is True or
                       v3.get("provisional") is True)
    if probe_confirmed:
        data_status = (
            "盘中临时；历史试盘已确认，当日无新确认事件（收盘待复评）"
            if provisional else "收盘数据；历史试盘已确认，当日无新确认事件"
        )
    else:
        data_status = (
            "盘中临时，收盘待复评" if provisional
            else "收盘数据；试盘未确认、仅观察"
        )
    score, components, score_status = _early_score_fields(item)
    score_max, _cohort = _early_score_coverage(item)
    score_status_text = {
        "ready": "试运行",
        "trial_unvalidated": "试运行（未验证）",
        "incomplete": "信息不完整",
        "data_insufficient": "数据不足",
        "unscored": "未评分",
    }.get(score_status, score_status)
    component_values = []
    used_keys = set()
    for key, label in EARLY_COMPONENT_LABELS:
        if key in components:
            component_values.append(f"{label} {_value(components[key])}")
            used_keys.add(key)
    component_values.extend(
        f"{key} {_value(value)}" for key, value in components.items()
        if key not in used_keys
    )
    component_text = "；".join(component_values) or "—"
    return [
        f"{_value(score)} / {_value(score_max)}（{_value(score_status_text)}）", component_text,
        _v4_identity(item), f"{probe_type} / {probe_status_text}",
        f"{_value(candidate_date)} / {_value(age)} 个交易日",
        _value(stage), _value(failure), _value(confirm), data_status,
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
        "limit": limit,
        "type_priority": list(TYPE_DISPLAY_PRIORITY),
        "priority_policy": "display_capacity_only_user_preference",
        "early_observation_order": (
            "common_core_score_desc_then_t2_tie_priority_then_age_then_symbol"
        ),
        "stats": stats, "items": items,
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
    early = summary.get("early_observations", len(_early_observations(payload)))
    return (
        f"依据日 {_value(payload.get('as_of_date') or payload.get('requested_as_of_date'))}；"
        f"模型状态 {_value(payload.get('model_status'))}；V4 候选 {stats['scanned_rows']} 只"
        f"（T1 {len((_v4_candidates(payload).get('T1') or []))}，"
        f"T2 {len((_v4_candidates(payload).get('T2') or []))}）；"
        f"提前观察 {early} 只，结构观察 {structural} 只，"
        f"无活跃信号 {inactive} 只，数据不足 {insufficient} 只。"
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
    items = (item for key in ("early_observations", "structural_observations",
                              "no_active_signal", "data_insufficient")
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
        "T1 与 T2 分组独立排序；组内依据为预期净收益，其次为估计胜率。展示顺序和总名额优先 T2，这是用户设定的观察偏好，不代表 T1/T2 跨类型收益或胜率可直接比较。", "",
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
    early = select_early_observations(payload)
    if early["items"]:
        lines += [
            "", "### T1/T2 提前观察评分（未确认、仅观察）", "",
            "观察分处于试运行阶段，尚未回测；合并 T1/T2 排序，但跨类型分数可比性尚未验证。该分数只安排观察优先级，不表示上涨概率，不参与正式推荐。", "",
            f"#### T1/T2 合并提前观察 Top 20（满分 {early['max_score']:g}）", "",
            "合并排序统一使用五项共同分量（趋势结构、风险空间、确认距离、试探质量、新鲜度），满分 90；市场板块分不计入。五项任一缺失均不参与排名；观察分相同时优先 T2，这是用户设定的观察偏好，不代表跨类型胜率更高。", "",
            "| " + " | ".join(EARLY_HEADERS) + " |",
            "| " + " | ".join(["---"] * len(EARLY_HEADERS)) + " |",
        ]
        lines.extend(
            "| " + " | ".join(_markdown_cell(cell) for cell in _early_cells(item)) + " |"
            for item in early["items"]
        )
        if early["omitted_by_limit"]:
            lines += ["", f"另有 {early['omitted_by_limit']} 只同覆盖范围观察未进入 Top 20。"]
        if early["unscored_total"]:
            lines += ["", f"另有 {early['unscored_total']} 只因原始评分无效、评分状态不可用、五项共同分量不完整或信号类型无效未参与排名。"]
    elif early["unscored_total"]:
        lines += [
            "", "### T1/T2 提前观察评分（未确认、仅观察）", "",
            f"本次没有可排名候选；{early['unscored_total']} 只因原始评分无效、评分状态不可用、五项共同分量不完整或信号类型无效而未参与排名。",
        ]
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
        "<p>T1 与 T2 分组独立排序；组内依据为预期净收益，其次为估计胜率。展示顺序和总名额优先 T2，这是用户设定的观察偏好，不代表 T1/T2 跨类型收益或胜率可直接比较。</p>",
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
    early = select_early_observations(payload)
    if early["items"]:
        out.extend([
            "<h3>T1/T2 提前观察评分（未确认、仅观察）</h3>",
            "<p>观察分处于试运行阶段，尚未回测；合并 T1/T2 排序，但跨类型分数可比性尚未验证。该分数只安排观察优先级，不表示上涨概率，不参与正式推荐。</p>",
            "<h4>" + escape(
                f"T1/T2 合并提前观察 Top 20（满分 {early['max_score']:g}）"
            ) + "</h4>",
            "<p>合并排序统一使用五项共同分量（趋势结构、风险空间、确认距离、试探质量、新鲜度），满分 90；市场板块分不计入。五项任一缺失均不参与排名；观察分相同时优先 T2，这是用户设定的观察偏好，不代表跨类型胜率更高。</p>",
            "<div class='candidate-table-wrap'><table class='weak-to-strong-table weak-to-strong-v4-early-table'>",
            "<thead><tr>" + "".join(
                "<th>" + escape(header) + "</th>" for header in EARLY_HEADERS
            ) + "</tr></thead><tbody>",
        ])
        out.extend(
            "<tr>" + "".join(
                "<td>" + escape(str(cell)) + "</td>" for cell in _early_cells(item)
            ) + "</tr>" for item in early["items"]
        )
        out.append("</tbody></table></div>")
        if early["omitted_by_limit"]:
            out.append("<p>" + escape(
                f"另有 {early['omitted_by_limit']} 只同覆盖范围观察未进入 Top 20。"
            ) + "</p>")
        if early["unscored_total"]:
            out.append("<p>" + escape(
                f"另有 {early['unscored_total']} 只因原始评分无效、评分状态不可用、五项共同分量不完整或信号类型无效未参与排名。"
            ) + "</p>")
    elif early["unscored_total"]:
        out.extend([
            "<h3>T1/T2 提前观察评分（未确认、仅观察）</h3>",
            "<p>" + escape(
                f"本次没有可排名候选；{early['unscored_total']} 只因原始评分无效、评分状态不可用、五项共同分量不完整或信号类型无效而未参与排名。"
            ) + "</p>",
        ])
    diagnostics = _v4_diagnostic_rows(payload)
    if diagnostics:
        out.append("<details><summary>V4 状态与诊断</summary><ul>")
        out.extend(
            "<li>" + escape(f"{label}｜{identity}：{reason}") + "</li>"
            for label, identity, reason in diagnostics
        )
        out.append("</ul></details>")
    return "".join(out) + "</section>"
