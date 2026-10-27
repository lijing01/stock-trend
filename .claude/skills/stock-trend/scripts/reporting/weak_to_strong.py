"""Report an independent daily weak-to-strong observation."""

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
              "PROVISIONAL_BREAKOUT": "盘中候选，未收盘确认"}
    context = item.get("context") or {}
    restrictions = context.get("formal_blockers") or []
    status = context.get("formal_bucket_label") or "未进入正式候选"
    if restrictions:
        status += "；" + "、".join(str(x) for x in restrictions)
    return [f"{item.get('name', '')} ({item.get('code', '')})", sectors,
            labels.get(item.get("state"), _value(item.get("state"))),
            _value(item.get("adjustment_days")),
            f"{_value(item.get('H'))} / {_value(item.get('L'))}",
            _value(item.get("va_v0_ratio")), _value(item.get("breakout_volume_ratio")),
            _value(item.get("close_strength")),
            f"{_value(item.get('as_of_date'))} / {_value(item.get('evidence_status'))}", status]


HEADERS = ["名称/代码", "板块", "信号状态", "调整天数", "压力 H / 支撑 L",
           "调整量比 Va/V0", "突破量比 Vt/Va", "收盘强度", "依据日/数据资格", "市场限制及正式状态"]


def _rows(payload):
    return [item for key in ("confirmed", "watching", "provisional")
            for item in sorted(payload.get(key) or [], key=lambda x: str(x.get("code", "")))]


def _summary(payload):
    scope = payload.get("scope") or {}
    summary = payload.get("summary") or {}
    evaluated = summary.get("evaluated", 0)
    insufficient = summary.get("data_insufficient", 0)
    if not scope.get("scope_complete") or insufficient or scope.get("collector_status") != "ready":
        return f"覆盖不足：已评估 {evaluated} 只，数据不足 {insufficient} 只；详见诊断。"
    return f"本次已评估 {evaluated} 只。"


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
    lines = ["## 日线弱转强", "", "独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制。", "", _summary(payload)]
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
    return "\n".join(lines)


def render_html(payload):
    if payload is None:
        return ""
    rows = _rows(payload)
    out = ["<section class='candidate-section'><h2>日线弱转强</h2>",
           "<p>独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制。</p>",
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
    return "".join(out) + "</section>"
