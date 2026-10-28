"""Report an independent daily weak-to-strong observation."""

import math
from html import escape
from reporting.sector_display import _sector_html


def _value(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _cells(item):
    memberships = item.get("sector_memberships") or []
    sectors = "、".join(str(x.get("name") or x.get("sector_name") or x.get("code") or "")
                       for x in memberships if isinstance(x, dict)) or "未记录"
    labels = {"BREAKOUT_CONFIRMED": "当日收盘确认", "SUPPLY_DRY": "缩量待突破",
              "PULLBACK": "结构有效回调（观察，尚未缩量）",
              "PROVISIONAL_BREAKOUT": "盘中候选，未收盘确认"}
    context = item.get("context") or {}
    restrictions = context.get("formal_blockers") or []
    status = context.get("formal_bucket_label") or "未进入正式候选"
    if restrictions:
        status += "；" + "、".join(str(x) for x in restrictions)
    signal = labels.get(item.get("state"), _value(item.get("state")))
    if item.get("state") == "PULLBACK" and item.get("two_day_decline") is True:
        signal += "；连续两日收盘下跌"
    return [f"{item.get('name', '')} ({item.get('code', '')})", sectors,
            signal,
            _value(item.get("adjustment_days")),
            f"{_value(item.get('H'))} / {_value(item.get('L'))}",
            _value(item.get("va_v0_ratio")), _value(item.get("breakout_volume_ratio")),
            _value(item.get("close_strength")),
            f"{_value(item.get('as_of_date'))} / {_value(item.get('evidence_status'))}", status]


HEADERS = ["名称/代码", "板块", "信号状态", "调整天数", "压力 H / 支撑 L",
           "调整量比 Va/V0", "突破量比 Vt/Va", "收盘强度", "依据日/数据资格", "市场限制及正式状态"]

FOLLOW_UP_HEADERS = ["名称/代码", "板块", "原信号日", "上一状态", "今日状态",
                     "判断依据", "数据日期", "数据资格"]

FOLLOW_UP_LABELS = {
    "enhanced": "增强", "ENHANCED": "增强", "strengthened": "增强",
    "STRENGTHENED": "增强", "增强": "增强",
    "continued": "延续", "CONTINUED": "延续", "延续": "延续",
    "invalidated": "失效", "INVALIDATED": "失效", "失效": "失效",
    "pending_review": "待复评", "PENDING_REVIEW": "待复评", "待复评": "待复评",
    "BREAKOUT_CONFIRMED": "收盘确认", "SUPPLY_DRY": "缩量待突破",
}
FOLLOW_UP_REASONS = {
    "current_evidence_incomplete": "当日数据不足", "close_unconfirmed": "收盘未确认",
    "current_close_missing": "当日收盘价缺失", "data_date_mismatch": "数据日期不匹配",
    "adjustment_basis_changed": "复权基准变化", "adjustment_basis_missing": "复权基准缺失",
    "origin_state_unsupported": "原始信号状态不支持", "origin_cycle_missing": "原信号周期缺失",
    "cycle_not_reliably_linked": "无法关联原信号周期", "threshold_missing": "原压力位缺失",
    "previous_close_missing": "上一有效收盘价缺失", "new_cycle_replaced_original": "新形态覆盖原周期",
    "supply_dry_breakout_confirmed": "缩量形态收盘突破", "supply_dry_continues": "缩量形态延续",
    "same_cycle_invalidated": "原周期形态失效", "same_cycle_state_unresolved": "原周期状态待确认",
    "close_below_breakout_threshold": "收盘跌破原压力位", "close_above_previous_valid_close": "收盘高于上一有效收盘",
    "close_holds_breakout_threshold": "收盘仍守住原压力位",
    "snapshot_missing": "上一交易日没有正式快照", "previous_trading_date_missing": "缺少上一交易日历",
    "as_of_date_not_in_trading_calendar": "评估日不在权威交易日历中",
}


def _number(value):
    """Return a finite numeric value, or None for missing/invalid input."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ascending(value):
    number = _number(value)
    return (number is None, number if number is not None else 0.0)


def _descending(value):
    number = _number(value)
    return (number is None, -number if number is not None else 0.0)


def _code(item):
    return str(item.get("code") or item.get("ts_code") or "")


def _main_sort_key(group, item):
    if group == "confirmed":
        return (_descending(item.get("breakout_volume_ratio")),
                _descending(item.get("close_strength")),
                _ascending(item.get("breakout_ratio")), _code(item))
    if group == "watching":
        return (_ascending(item.get("va_v0_ratio")),
                _ascending(item.get("drawdown_ratio", item.get("drawdown"))),
                _ascending(item.get("adjustment_days")), _code(item))
    if group == "pullback":
        return (item.get("two_day_decline") is not True,
                _ascending(item.get("drawdown_ratio", item.get("drawdown"))),
                _code(item))
    return (_descending(item.get("breakout_volume_ratio")),
            _descending(item.get("close_strength")), _code(item))


def _sorted_group(payload, group):
    rows = payload.get(group) or []
    if not isinstance(rows, list):
        return []
    return sorted((item for item in rows if isinstance(item, dict)),
                  key=lambda item: _main_sort_key(group, item))


def _follow_up_status(row):
    return str(row.get("status") or row.get("follow_up_status")
               or row.get("tracking_status") or "pending_review").upper()


def _follow_up_sort_key(row):
    priorities = {"INVALIDATED": 0, "失效": 0,
                  "PENDING_REVIEW": 1, "待复评": 1,
                  "ENHANCED": 2, "STRENGTHENED": 2, "增强": 2,
                  "CONTINUED": 3, "延续": 3}
    date = str(row.get("signal_date") or row.get("origin_signal_date") or "")
    digits = "".join(character for character in date if character.isdigit())
    date_valid = len(digits) == 8
    date_value = -int(digits) if date_valid else 0
    return (priorities.get(_follow_up_status(row), 4), not date_valid,
            date_value, _code(row))


def sort_payload(payload):
    """Sort report arrays in place and return *payload* for convenient chaining."""
    if not isinstance(payload, dict):
        return payload
    for group in ("confirmed", "watching", "pullback", "provisional"):
        if group in payload:
            payload[group] = _sorted_group(payload, group)
    follow_up = payload.get("follow_up")
    if isinstance(follow_up, dict):
        for key in ("rows", "items"):
            rows = follow_up.get(key)
            if isinstance(rows, list):
                follow_up[key] = sorted(
                    (row for row in rows if isinstance(row, dict)),
                    key=_follow_up_sort_key)
    elif isinstance(follow_up, list):
        payload["follow_up"] = sorted(
            (row for row in follow_up if isinstance(row, dict)),
            key=_follow_up_sort_key)
    return payload


def _rows(payload):
    return [item for group in ("confirmed", "watching", "pullback", "provisional")
            for item in _sorted_group(payload, group)]


def _summary(payload):
    scope = payload.get("scope") or {}
    summary = payload.get("summary") or {}
    evaluated = summary.get("evaluated", 0)
    insufficient = summary.get("data_insufficient", 0)
    if not scope.get("scope_complete") or insufficient or scope.get("collector_status") != "ready":
        return f"覆盖不足：已评估 {evaluated} 只，数据不足 {insufficient} 只；详见诊断。"
    return f"本次已评估 {evaluated} 只。"


def _follow_up(payload):
    value = payload.get("follow_up") or {}
    if isinstance(value, list):
        return {"rows": value}
    return value if isinstance(value, dict) else {}


def _follow_up_rows(payload):
    follow_up = _follow_up(payload)
    rows = follow_up.get("rows", follow_up.get("items", []))
    if not isinstance(rows, list):
        return []
    return sorted((row for row in rows if isinstance(row, dict)),
                  key=_follow_up_sort_key)


def _follow_up_reason(row):
    detail = row.get("reason") or row.get("reason_text")
    reasons = row.get("reason_codes") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    parts = [str(detail)] if detail else [FOLLOW_UP_REASONS.get(str(reason), str(reason)) for reason in reasons]
    for label, key in (("收盘", "current_close"), ("前收", "previous_close"),
                       ("压力 H", "threshold_H")):
        value = row.get(key)
        if value is not None and value != "":
            parts.append(f"{label} {_value(value)}")
    return "；".join(parts) or "—"


def _follow_up_cells(row):
    status = (row.get("status") or row.get("follow_up_status")
              or row.get("tracking_status") or "pending_review")
    previous = row.get("previous_status") or row.get("previous_state") or row.get("prior_state")
    return [
        f"{row.get('name', '')} ({row.get('code') or row.get('ts_code') or ''})",
        "",  # HTML uses the structured sector renderer for this cell.
        _value(row.get("signal_date") or row.get("origin_signal_date")),
        FOLLOW_UP_LABELS.get(previous, _value(previous)),
        FOLLOW_UP_LABELS.get(status, _value(status)),
        _follow_up_reason(row),
        _value(row.get("data_date") if "data_date" in row else row.get("as_of_date")),
        {"ready": "数据合格", "data_insufficient": "数据不足"}.get(
            row.get("evidence_status"), _value(row.get("evidence_status"))),
    ]


def _follow_up_summary(payload):
    follow_up = _follow_up(payload)
    baseline = follow_up.get("baseline") or {}
    if not isinstance(baseline, dict):
        baseline = {}
    source_date = (baseline.get("as_of_date") or baseline.get("source_date")
                   or follow_up.get("source_date") or follow_up.get("baseline_date"))
    selection = (baseline.get("selection_method") or baseline.get("selection")
                 or follow_up.get("selection_method") or follow_up.get("selection"))
    parts = []
    if source_date:
        parts.append("基线 " + str(source_date))
    if selection:
        parts.append("选择方式 " + {
            "run_index_generated_at_sequence": "按运行时间选择最新快照",
            "run_index_sequence": "按运行顺序选择最新快照",
            "legacy_mtime_fallback": "旧快照按文件修改时间选择",
        }.get(str(selection), str(selection)))
    if follow_up.get("coverage_warning"):
        parts.append(str(follow_up["coverage_warning"]))
    return "；".join(parts)


def _markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _memberships_html(item):
    rendered = []
    for member in item.get("sector_memberships") or []:
        if not isinstance(member, dict):
            continue
        name = member.get("name") or member.get("sector_name") or member.get("code")
        if not name:
            continue
        evidence = dict(member, sector_name=str(name),
                        sector_hot_score=member.get("hot_score"))
        rendered.append("<div class='weak-sector'>" +
                        _sector_html(evidence, unknown_heat_plain=True) + "</div>")
    return "".join(rendered) or "未记录"


def render_markdown(payload):
    if payload is None:
        return ""
    rows = _rows(payload)
    lines = ["## 日线弱转强", "", "独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制。",
             "", "排序：收盘确认、缩量待突破、结构有效回调、盘中候选；各阶段按形态质量排列。",
             "", _summary(payload)]
    if rows:
        lines += ["", "| " + " | ".join(HEADERS) + " |", "| " + " | ".join(["---"] * len(HEADERS)) + " |"]
        for item in rows:
            lines.append("| " + " | ".join(x.replace("|", "\\|").replace("\n", " ") for x in _cells(item)) + " |")
    else:
        lines += ["", "无满足条件的形态；覆盖不足时不代表完整扫描无信号。"]
    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        lines += ["", "弱转强诊断："]
        for item in diagnostics:
            lines.append(f"- {item.get('code', '')}：{item.get('state', '未知')}；" + "、".join(item.get("reason_codes") or ["无"]))
    follow_up = _follow_up(payload)
    follow_up_rows = _follow_up_rows(payload)
    if follow_up:
        lines += ["", "### 上一交易日信号跟踪"]
        summary = _follow_up_summary(payload)
        if summary:
            lines += ["", summary]
        if follow_up_rows:
            lines += ["", "| " + " | ".join(FOLLOW_UP_HEADERS) + " |",
                      "| " + " | ".join(["---"] * len(FOLLOW_UP_HEADERS)) + " |"]
            for item in follow_up_rows:
                cells = _follow_up_cells(item)
                memberships = item.get("sector_memberships") or []
                cells[1] = "、".join(
                    str(member.get("name") or member.get("sector_name") or member.get("code") or "")
                    for member in memberships if isinstance(member, dict)) or "未记录"
                lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
        else:
            message = follow_up.get("message") or follow_up.get("reason") or "没有需要持续跟踪的上一交易日信号。"
            message = FOLLOW_UP_REASONS.get(message, message)
            lines += ["", str(message)]
    return "\n".join(lines)


def render_html(payload):
    if payload is None:
        return ""
    rows = _rows(payload)
    out = ["<section class='candidate-section'><h2>日线弱转强</h2>",
           "<p>独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制。</p>",
           "<p>排序：收盘确认、缩量待突破、结构有效回调、盘中候选；各阶段按形态质量排列。</p>",
           "<p>" + escape(_summary(payload)) + "</p>"]
    if rows:
        out.append("<div class='candidate-table-wrap'><table class='weak-to-strong-table'><thead><tr>" +
                   "".join("<th>" + escape(x) + "</th>" for x in HEADERS) + "</tr></thead><tbody>")
        for item in rows:
            cells = _cells(item)
            out.append("<tr>" + "".join(
                "<td>" + (_memberships_html(item) if index == 1 else escape(value)) + "</td>"
                for index, value in enumerate(cells)) + "</tr>")
        out.append("</tbody></table></div>")
    else:
        out.append("<p>无满足条件的形态；覆盖不足时不代表完整扫描无信号。</p>")
    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        out.append("<details><summary>弱转强诊断</summary><ul>")
        for item in diagnostics:
            detail = f"{item.get('code', '')}：{item.get('state', '未知')}；" + "、".join(item.get("reason_codes") or ["无"])
            out.append("<li>" + escape(detail) + "</li>")
        out.append("</ul></details>")
    follow_up = _follow_up(payload)
    follow_up_rows = _follow_up_rows(payload)
    if follow_up:
        out.append("<h3>上一交易日信号跟踪</h3>")
        summary = _follow_up_summary(payload)
        if summary:
            out.append("<p>" + escape(summary) + "</p>")
        if follow_up_rows:
            out.append("<div class='candidate-table-wrap'><table class='weak-to-strong-table weak-follow-up-table'><thead><tr>" +
                       "".join("<th>" + escape(header) + "</th>" for header in FOLLOW_UP_HEADERS) +
                       "</tr></thead><tbody>")
            for item in follow_up_rows:
                cells = _follow_up_cells(item)
                out.append("<tr>" + "".join(
                    "<td>" + (_memberships_html(item) if index == 1 else escape(cell)) + "</td>"
                    for index, cell in enumerate(cells)) + "</tr>")
            out.append("</tbody></table></div>")
        else:
            message = follow_up.get("message") or follow_up.get("reason") or "没有需要持续跟踪的上一交易日信号。"
            message = FOLLOW_UP_REASONS.get(message, message)
            out.append("<p>" + escape(str(message)) + "</p>")
    return "".join(out) + "</section>"


V2_GROUPS = (
    ("range_breakout", "区间突破"),
    ("local_confirmation", "局部转强"),
    ("early_repair", "早期修复"),
    ("preparing", "准备观察"),
    ("provisional", "盘中候选"),
)

V2_STAGE_LABELS = {
    "RANGE_BREAKOUT": "区间突破", "LOCAL_CONFIRMATION": "局部转强",
    "EARLY_REPAIR": "早期修复", "PREPARING": "准备观察", "NONE": "无阶段",
}
V2_PATTERN_LABELS = {
    "short_pullback": "短线强势回调", "base_consolidation": "平台/趋势整理",
    "deep_repair": "深回撤修复",
}
V2_RISK_LABELS = {
    "extended": "偏离较大", "weak_volume": "量能偏弱", "lost_level": "跌回触发位",
    "parent_overhead": "父结构压力", "flat_bar": "一字线",
}

V2_HEADERS = ["名称/代码", "阶段/背景", "均线位置与方向", "局部/区间/父结构压力",
              "量能/收盘强度", "偏离风险", "依据日/数据资格"]


def _v2_trend_tier(item):
    """Return the plan-defined MA direction tier (lower sorts first)."""
    metrics = item.get("metrics") or {}
    if not isinstance(metrics, dict):
        return 3
    ma10 = _number(metrics.get("ma10_slope_3", metrics.get("ma10_slope_3d")))
    ma20 = _number(metrics.get("ma20_slope_3", metrics.get("ma20_slope_3d")))
    if ma10 is None or ma20 is None:
        direction = str(metrics.get("ma_direction") or "").lower()
        return {"both_up": 0, "ma20_up": 1}.get(direction, 3 if not direction else 2)
    tolerance = _number(metrics.get("ma_flat_tolerance"))
    threshold = 0.002 if tolerance is None else tolerance
    ma10_up, ma20_up = ma10 > threshold, ma20 > threshold
    return 0 if ma10_up and ma20_up else 1 if ma20_up else 2


def _v2_quality_key(item):
    pattern = str(item.get("pattern_type") or "")
    metrics = item.get("metrics") or {}
    checks = item.get("checks") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    if not isinstance(checks, dict):
        checks = {}
    if pattern == "short_pullback":
        if item.get("stage") in {"LOCAL_CONFIRMATION", "RANGE_BREAKOUT"}:
            return (_descending(metrics.get("breakout_volume_ratio")),
                    _descending(metrics.get("close_strength")),
                    _ascending(metrics.get("breakout_ratio")))
        return (_ascending(metrics.get("va_v0_ratio")),
                _ascending(metrics.get("drawdown_ratio")),
                _ascending(metrics.get("adjustment_days")))
    distance = metrics.get("trigger_distance_atr", metrics.get("distance_to_trigger_atr"))
    volume = metrics.get("volume_prior5_ratio", metrics.get("volume_ratio_5"))
    if pattern == "deep_repair":
        holds = checks.get("holds_trigger_level")
        holds = holds.get("passed") if isinstance(holds, dict) else holds
        return (holds is not True, _ascending(distance))
    return (_ascending(distance), _descending(volume))


def _v2_sort_key(item):
    evidence = str(item.get("evidence_status") or "")
    risks = item.get("risk_flags") or []
    if isinstance(risks, str):
        risks = [risks]
    pattern_rank = {"short_pullback": 0, "base_consolidation": 1,
                    "deep_repair": 2}.get(str(item.get("pattern_type") or ""), 3)
    return (evidence != "ready", _v2_trend_tier(item), "extended" in risks,
            pattern_rank, _v2_quality_key(item), _code(item))


def sort_v2_rows(rows):
    """Return valid v2 rows in deterministic plan-defined display order."""
    if not isinstance(rows, list):
        return []
    return sorted((row for row in rows if isinstance(row, dict)), key=_v2_sort_key)


def _v2_cells(item):
    metrics = item.get("metrics") or {}
    structure = item.get("structure") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    if not isinstance(structure, dict):
        structure = {}
    stage = V2_STAGE_LABELS.get(str(item.get("stage")), _value(item.get("stage")))
    pattern = V2_PATTERN_LABELS.get(str(item.get("pattern_type")),
                                    _value(item.get("pattern_type")))
    origin = item.get("origin_pattern_type")
    if origin and origin != item.get("pattern_type"):
        pattern += "（起始：" + V2_PATTERN_LABELS.get(str(origin), str(origin)) + "）"
    close = metrics.get("close")
    ma10, ma20 = metrics.get("ma10"), metrics.get("ma20")
    slope10 = metrics.get("ma10_slope_3", metrics.get("ma10_slope_3d"))
    slope20 = metrics.get("ma20_slope_3", metrics.get("ma20_slope_3d"))
    def direction(slope):
        value = _number(slope)
        if value is None:
            return "方向未知"
        tolerance = _number(metrics.get("ma_flat_tolerance"))
        tolerance = 0.002 if tolerance is None else tolerance
        label = "向上" if value > tolerance else "向下" if value < -tolerance else "走平"
        return f"{label} {value:+.2%}"
    ma_text = (f"收 {_value(close)}；MA10 {_value(ma10)} ({direction(slope10)})；"
               f"MA20 {_value(ma20)} ({direction(slope20)})")
    h5 = metrics.get("h5", structure.get("h5"))
    h10 = metrics.get("h10", structure.get("h10"))
    h20 = metrics.get("h20", structure.get("h20"))
    parent = structure.get("anchor_high", structure.get("parent_high"))
    pressure = f"H5 {_value(h5)} / H10 {_value(h10)} / H20 {_value(h20)} / 父高 {_value(parent)}"
    volume = metrics.get("volume_prior5_ratio", metrics.get("volume_ratio_5"))
    strength = metrics.get("close_strength")
    quality = f"量/前5日 {_value(volume)}；收盘强度 {_value(strength)}"
    risks = item.get("risk_flags") or []
    if isinstance(risks, str):
        risks = [risks]
    risk_text = "、".join(V2_RISK_LABELS.get(str(risk), str(risk)) for risk in risks) or "无"
    evidence = {"ready": "数据合格", "data_insufficient": "数据不足"}.get(
        item.get("evidence_status"), _value(item.get("evidence_status")))
    basis_date = item.get("signal_date") or item.get("stage_first_at") or item.get("as_of_date")
    return [f"{item.get('name', '')} ({_code(item)})", f"{stage} / {pattern}", ma_text,
            pressure, quality, risk_text, f"{_value(basis_date)} / {evidence}"]


def _v2_summary(payload):
    summary = payload.get("summary") or {}
    status = summary.get("validation_status") or payload.get("validation_status")
    if not status:
        status = "continue_accumulating"
    stock_count = summary.get("stock_count", 0)
    event_count = summary.get("new_event_count", 0)
    return f"研究验证状态：{status}；股票 {stock_count} 只，当日新事件 {event_count} 个。"


def render_v2_markdown(payload):
    """Render the parallel research v2 view without replacing the v1 report."""
    if payload is None:
        return ""
    lines = ["## 日线弱转强 v2（研究版）", "",
             "按转强阶段分组；结构识别不等同于正式推荐资格。", "", _v2_summary(payload)]
    any_rows = False
    for key, label in V2_GROUPS:
        rows = sort_v2_rows(payload.get(key) or [])
        if not rows:
            continue
        any_rows = True
        lines += ["", f"### {label}", "", "| " + " | ".join(V2_HEADERS) + " |",
                  "| " + " | ".join(["---"] * len(V2_HEADERS)) + " |"]
        for item in rows:
            lines.append("| " + " | ".join(_markdown_cell(cell) for cell in _v2_cells(item)) + " |")
    if not any_rows:
        lines += ["", "当前没有可展示的 v2 研究形态。"]
    diagnostics = payload.get("diagnostics") or []
    if isinstance(diagnostics, list) and diagnostics:
        lines += ["", "<details><summary>v2 诊断</summary>", ""]
        for item in diagnostics:
            if isinstance(item, dict):
                reasons = item.get("reason_codes") or ["无"]
                lines.append(f"- {_markdown_cell(_code(item))}：" +
                             "、".join(_markdown_cell(reason) for reason in reasons))
        lines += ["", "</details>"]
    return "\n".join(lines)


def render_v2_html(payload):
    """Render the parallel research v2 view with all values HTML-escaped."""
    if payload is None:
        return ""
    out = ["<section class='candidate-section weak-to-strong-v2'>",
           "<h2>日线弱转强 v2（研究版）</h2>",
           "<p>按转强阶段分组；结构识别不等同于正式推荐资格。</p>",
           "<p>" + escape(_v2_summary(payload)) + "</p>"]
    any_rows = False
    for key, label in V2_GROUPS:
        rows = sort_v2_rows(payload.get(key) or [])
        if not rows:
            continue
        any_rows = True
        out.append("<h3>" + escape(label) + "</h3>")
        out.append("<div class='candidate-table-wrap'><table class='weak-to-strong-table weak-to-strong-v2-table'>"
                   "<thead><tr>" + "".join("<th>" + escape(header) + "</th>" for header in V2_HEADERS) +
                   "</tr></thead><tbody>")
        for item in rows:
            out.append("<tr>" + "".join("<td>" + escape(str(cell)) + "</td>"
                                         for cell in _v2_cells(item)) + "</tr>")
        out.append("</tbody></table></div>")
    if not any_rows:
        out.append("<p>当前没有可展示的 v2 研究形态。</p>")
    diagnostics = payload.get("diagnostics") or []
    if isinstance(diagnostics, list) and diagnostics:
        out.append("<details><summary>v2 诊断</summary><ul>")
        for item in diagnostics:
            if isinstance(item, dict):
                detail = _code(item) + "：" + "、".join(str(reason) for reason in
                                                        (item.get("reason_codes") or ["无"]))
                out.append("<li>" + escape(detail) + "</li>")
        out.append("</ul></details>")
    return "".join(out) + "</section>"
