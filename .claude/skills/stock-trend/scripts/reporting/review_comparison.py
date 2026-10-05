"""Render the frozen previous-session daily-review comparison."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from html import escape


HTML_BLOCK_START = "<!-- REVIEW_COMPARISON:START -->"
HTML_BLOCK_END = "<!-- REVIEW_COMPARISON:END -->"
MD_BLOCK_START = "<!-- REVIEW_COMPARISON:START -->"
MD_BLOCK_END = "<!-- REVIEW_COMPARISON:END -->"

_COMPONENTS = (
    ("index_trend", "大盘趋势"),
    ("volume", "成交额"),
    ("breadth", "赚钱效应"),
    ("zt_emotion", "涨停情绪"),
    ("capital", "资金"),
)
_STATUS_LABELS = {
    "comparable": "可比",
    "reference_only": "仅供参考",
    "unavailable": "不可比较",
}
_REASON_LABELS = {
    "adjacent_snapshot_missing": "缺少上一交易日的合格快照",
    "previous_session_snapshot_missing": "缺少上一交易日的合格快照",
    "previous_session_outside_calendar_coverage": "交易日历未覆盖上一交易日",
    "calendar_current_session_missing": "交易日历缺少当前依据日",
    "calendar_invalid": "交易日历不可用",
    "intraday_vs_close_not_comparable": "盘中数据仅供参考，不能与收盘数据比较",
    "model_version_mismatch": "两日评分模型版本不同",
    "model_version_missing": "评分模型版本信息不完整",
    "methodology_version_mismatch": "两日统计口径版本不同",
    "methodology_version_missing": "统计口径版本信息不完整",
    "normalization_denominator_mismatch": "两日评分分母不同",
    "normalization_denominator_missing": "评分分母信息不完整",
    "indicator_coverage_mismatch": "两日指标覆盖范围不同",
    "component_coverage_mismatch": "两日组件覆盖范围不同",
    "component_weight_mismatch": "两日组件权重不同",
    "component_weight_missing": "组件权重信息不完整",
    "component_score_missing": "组件评分缺失",
    "score_missing": "总分缺失",
    "model_score_missing": "模型计算分缺失",
    "current_close_not_eligible": "当前收盘记录不具备比较资格",
    "previous_close_not_eligible": "上一收盘记录不具备比较资格",
    "current_close_unqualified": "当前收盘证据不完整",
    "previous_close_unqualified": "上一收盘证据不完整",
    "close_qualification_mismatch": "两日收盘证据资格不同",
    "current_not_completed_close": "当前尚未完成收盘确认",
    "legacy_close_not_completed": "历史记录缺少收盘完成确认",
    "freeze_unknown": "冻结时间无法核验",
    "amount_freeze_unknown": "成交额冻结时间无法核验",
    "amount_frozen_after_cutoff": "成交额证据晚于比较截止时间",
    "amount_missing": "两市成交额缺失",
    "amount_evidence_incomplete": "成交额证据不完整",
    "amount_evidence_not_scorable": "成交额证据不具备评分资格",
    "amount_date_unqualified": "成交额日期未通过核验",
    "amount_range_unqualified": "成交额市场覆盖不完整",
    "amount_coverage_mismatch": "两日成交额覆盖范围不同",
    "snapshot_reconciliation_unqualified": "历史快照未通过分数核对",
    "weighted_contribution_mismatch": "组件贡献与总分变化未对齐",
}


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _sequence(value) -> list:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value] if value not in (None, "") else []


def _plain(value, default="—") -> str:
    if value in (None, ""):
        return default
    if isinstance(value, Mapping):
        preferred = value.get("message") or value.get("reason") or value.get("code")
        if preferred not in (None, ""):
            return _plain(preferred, default)
        return "；".join(
            f"{_plain(key, '')}: {_plain(item, '')}" for key, item in value.items()
        ) or default
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "、".join(_plain(item, "") for item in value) or default
    return str(value)


def _h(value, default="—") -> str:
    return escape(_plain(value, default), quote=True)


def _md(value, default="—") -> str:
    text = _plain(value, default).replace("\r", " ").replace("\n", " ")
    text = text.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]()#+.!|<>])", r"\\\1", text)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fixed(value, *, signed=False, unit="") -> str:
    number = _number(value)
    if number is None:
        return "—"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:,.2f}{unit}"


def _status(value) -> str:
    raw = _plain(value, "unavailable")
    return f"{_STATUS_LABELS.get(raw, raw)}（{raw}）"


def _status_label(value) -> str:
    raw = _plain(value, "unavailable")
    return _STATUS_LABELS.get(raw, "状态未知")


def _reason_label(value) -> str:
    raw = _plain(value, "")
    if not raw:
        return "无"
    if raw in _REASON_LABELS:
        return _REASON_LABELS[raw]
    if re.search(r"[A-Za-z_<>{}\[\]]", raw):
        return "证据不足，详见完整比较"
    return raw


def _reason_summary(values) -> str:
    labels = list(dict.fromkeys(_reason_label(value) for value in _sequence(values)))
    return "；".join(labels) if labels else "无"


def _rounding_note(comparison):
    reconciliation = _mapping(comparison.get("contribution_reconciliation"))
    residual = _number(reconciliation.get("rounding_residual"))
    model_change = _number(reconciliation.get("model_score_change"))
    if (reconciliation.get("status") != "matched" or residual is None
            or model_change is None or abs(residual) <= 1e-9):
        return ""
    return (f"未舍入模型贡献合计变化 {model_change:+.3f} 分；"
            f"显示总分差的舍入残差为 {residual:+.3f} 分，不归入任何组件贡献。")


def _field(comparison: Mapping, key: str) -> Mapping:
    direct = comparison.get(key)
    if isinstance(direct, Mapping):
        return direct
    return _mapping(_mapping(comparison.get("fields")).get(key))


def _field_reason(field: Mapping) -> str:
    reasons = _sequence(field.get("reasons"))
    if not reasons:
        reasons = _sequence(field.get("reason"))
    return _plain(reasons, "无")


def _components(comparison: Mapping) -> Mapping:
    direct = comparison.get("components")
    if isinstance(direct, Mapping):
        return direct
    return _mapping(_mapping(comparison.get("fields")).get("components"))


def _top_reasons(comparison: Mapping) -> list[Mapping]:
    rows = [row for row in _sequence(comparison.get("main_reasons")) if isinstance(row, Mapping)]
    return sorted(
        rows,
        key=lambda row: (
            -abs(_number(row.get("change")) or 0.0),
            _plain(row.get("component_id"), ""),
        ),
    )


def _component_name(row: Mapping) -> str:
    component_id = _plain(row.get("component_id"), "")
    return _plain(row.get("name"), dict(_COMPONENTS).get(component_id, "其他组件"))


def _date_lines(comparison: Mapping) -> tuple[str, str]:
    expected = comparison.get("prior_session_date")
    actual = comparison.get("actual_baseline_date")
    gap = _number(comparison.get("session_gap"))
    if actual not in (None, "") and actual != expected:
        gap_text = "—" if gap is None else f"{int(gap)} 个交易日"
        md = (
            f"- 应比较的上一交易日：{_md(expected)}；实际参考日：{_md(actual)}；"
            f"间隔：{_md(gap_text)}"
        )
        html = (
            f"<p class=\"review-comparison-meta\">应比较的上一交易日：{_h(expected)} · "
            f"实际参考日：{_h(actual)} · 间隔：{_h(gap_text)}</p>"
        )
        return md, html
    md = f"- 上一交易日：{_md(expected)}"
    html = f"<p class=\"review-comparison-meta\">上一交易日：{_h(expected)}</p>"
    return md, html


def _markdown_close(comparison: Mapping) -> list[str]:
    score = _field(comparison, "score")
    amount = _field(comparison, "amount") or _field(comparison, "turnover_amount")
    lines = [
        "#### 总分变化",
        "",
        "| 当前 | 前值 | 变化 | 状态 | 原因 |",
        "|---:|---:|---:|---|---|",
        f"| {_fixed(score.get('current'))} | {_fixed(score.get('previous'))} | "
        f"{_fixed(score.get('change'), signed=True, unit=' 分')} | "
        f"{_md(_status(score.get('status')))} | {_md(_field_reason(score))} |",
        "",
        "#### 五项变化",
        "",
        "| 组件 | 当前 | 前值 | 变化 | 状态 | 原因 |",
        "|---|---:|---:|---:|---|---|",
    ]
    components = _components(comparison)
    for component_id, name in _COMPONENTS:
        field = _mapping(components.get(component_id))
        lines.append(
            f"| {name} | {_fixed(field.get('current'))} | {_fixed(field.get('previous'))} | "
            f"{_fixed(field.get('change'), signed=True, unit=' 分')} | "
            f"{_md(_status(field.get('status')))} | {_md(_field_reason(field))} |"
        )
    lines.extend([
        "",
        "#### 两市成交额（独立比较）",
        "",
        "| 当前 | 前值 | 金额变化 | 百分比变化 | 状态 | 原因 |",
        "|---:|---:|---:|---:|---|---|",
        f"| {_fixed(amount.get('current'), unit=' 亿')} | {_fixed(amount.get('previous'), unit=' 亿')} | "
        f"{_fixed(amount.get('change'), signed=True, unit=' 亿')} | "
        f"{_fixed(amount.get('percent_change'), signed=True, unit='%')} | "
        f"{_md(_status(amount.get('status')))} | {_md(_field_reason(amount))} |",
    ])
    reasons = _top_reasons(comparison)
    if reasons:
        lines.extend([
            "", "#### 主要变化原因", "",
            "以下仅为评分模型内的加权贡献变化归因，不代表市场涨跌因果。", "",
            "| 组件 | 当前贡献 | 前值贡献 | 贡献变化 |", "|---|---:|---:|---:|",
        ])
        for row in reasons:
            lines.append(
                f"| {_md(row.get('name') or row.get('component_id'))} | "
                f"{_fixed(row.get('current_contribution'))} | {_fixed(row.get('previous_contribution'))} | "
                f"{_fixed(row.get('change'), signed=True, unit=' 分')} |"
            )
    note = _rounding_note(comparison)
    if note:
        lines.extend(["", note])
    return lines


def _markdown_intraday(comparison: Mapping) -> list[str]:
    reference = _mapping(comparison.get("previous_close_reference"))
    components = _mapping(reference.get("components"))
    lines = [
        "> 盘中混合分不与正式收盘分作方向性比较；以下只展示上一收盘参考。",
        "",
        f"- 上一收盘参考日：{_md(reference.get('date') or comparison.get('actual_baseline_date'))}",
        f"- 上一收盘参考总分：{_fixed(reference.get('score'))}",
        "",
        "| 上一收盘组件 | 参考分 |",
        "|---|---:|",
    ]
    for component_id, name in _COMPONENTS:
        value = components.get(component_id)
        if isinstance(value, Mapping):
            value = value.get("score", value.get("value"))
        lines.append(f"| {name} | {_fixed(value)} |")
    return lines


def render_markdown(comparison) -> str:
    """Render a comparison artifact without recomputing any comparison value."""
    comparison = _mapping(comparison)
    date_md, _ = _date_lines(comparison)
    top_reasons = comparison.get("reasons")
    if top_reasons in (None, ""):
        top_reasons = comparison.get("reason")
    lines = [
        MD_BLOCK_START,
        "### 与上一交易日比较",
        "",
        f"- 当前依据日：{_md(comparison.get('basis_date'))}",
        date_md,
        f"- 比较状态：{_md(_status(comparison.get('status')))}",
        f"- 原因：{_md(_plain(_sequence(top_reasons), '无'))}",
        "",
    ]
    if _plain(comparison.get("mode"), "close") == "intraday":
        lines.extend(_markdown_intraday(comparison))
    else:
        lines.extend(_markdown_close(comparison))
    lines.extend(["", MD_BLOCK_END])
    return "\n".join(lines)


def _html_table(headers, rows) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f'<div class="review-comparison-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _html_close(comparison: Mapping) -> str:
    score = _field(comparison, "score")
    amount = _field(comparison, "amount") or _field(comparison, "turnover_amount")
    score_table = _html_table(
        ("当前", "前值", "变化", "状态", "原因"),
        [[
            _h(_fixed(score.get("current"))), _h(_fixed(score.get("previous"))),
            _h(_fixed(score.get("change"), signed=True, unit=" 分")),
            _h(_status(score.get("status"))), _h(_field_reason(score)),
        ]],
    )
    components = _components(comparison)
    component_rows = []
    for component_id, name in _COMPONENTS:
        field = _mapping(components.get(component_id))
        component_rows.append([
            _h(name), _h(_fixed(field.get("current"))), _h(_fixed(field.get("previous"))),
            _h(_fixed(field.get("change"), signed=True, unit=" 分")),
            _h(_status(field.get("status"))), _h(_field_reason(field)),
        ])
    component_table = _html_table(
        ("组件", "当前", "前值", "变化", "状态", "原因"), component_rows,
    )
    amount_table = _html_table(
        ("当前", "前值", "金额变化", "百分比变化", "状态", "原因"),
        [[
            _h(_fixed(amount.get("current"), unit=" 亿")),
            _h(_fixed(amount.get("previous"), unit=" 亿")),
            _h(_fixed(amount.get("change"), signed=True, unit=" 亿")),
            _h(_fixed(amount.get("percent_change"), signed=True, unit="%")),
            _h(_status(amount.get("status"))), _h(_field_reason(amount)),
        ]],
    )
    reasons = _top_reasons(comparison)
    reasons_html = ""
    if reasons:
        rows = [[
            _h(_component_name(row)),
            _h(_fixed(row.get("current_contribution"))),
            _h(_fixed(row.get("previous_contribution"))),
            _h(_fixed(row.get("change"), signed=True, unit=" 分")),
        ] for row in reasons]
        reasons_html = (
            "<h4>主要变化原因</h4>"
            "<p>以下仅为评分模型内的加权贡献变化归因，不代表市场涨跌因果。</p>"
            + _html_table(("组件", "当前贡献", "前值贡献", "贡献变化"), rows)
        )
    return (
        f"<h4>总分变化</h4>{score_table}"
        f"<h4>五项变化</h4>{component_table}"
        f"<h4>两市成交额（独立比较）</h4>{amount_table}{reasons_html}"
        + (f"<p>{_h(_rounding_note(comparison))}</p>" if _rounding_note(comparison) else "")
    )


def _html_intraday(comparison: Mapping) -> str:
    reference = _mapping(comparison.get("previous_close_reference"))
    components = _mapping(reference.get("components"))
    rows = []
    for component_id, name in _COMPONENTS:
        value = components.get(component_id)
        if isinstance(value, Mapping):
            value = value.get("score", value.get("value"))
        rows.append([_h(name), _h(_fixed(value))])
    return (
        '<p class="review-comparison-note">盘中混合分不与正式收盘分作方向性比较；以下只展示上一收盘参考。</p>'
        f'<p>上一收盘参考日：{_h(reference.get("date") or comparison.get("actual_baseline_date"))} · '
        f'上一收盘参考总分：{_h(_fixed(reference.get("score")))}</p>'
        + _html_table(("上一收盘组件", "参考分"), rows)
    )


def _html_primary_changes(comparison: Mapping) -> str:
    if _plain(comparison.get("mode"), "close") == "intraday":
        reference = _mapping(comparison.get("previous_close_reference"))
        return (
            '<div class="review-comparison-highlights">'
            '<h4>主要变化</h4>'
            '<p>盘中不判断较上一收盘改善或恶化；'
            f'上一收盘参考总分为 {_h(_fixed(reference.get("score")))}。</p>'
            '</div>'
        )
    items = []
    score = _field(comparison, "score")
    if score.get("status") == "comparable" and _number(score.get("change")) is not None:
        items.append(
            '<li><strong>总分较上一交易日</strong> '
            f'{_h(_fixed(score.get("change"), signed=True, unit=" 分"))}</li>'
        )
    amount = _field(comparison, "amount") or _field(comparison, "turnover_amount")
    if (amount.get("status") == "comparable"
            and _number(amount.get("change")) is not None
            and _number(amount.get("percent_change")) is not None):
        items.append(
            '<li><strong>两市成交额</strong> '
            f'{_h(_fixed(amount.get("change"), signed=True, unit=" 亿"))}'
            f'（{_h(_fixed(amount.get("percent_change"), signed=True, unit="%"))}）</li>'
        )
    reasons = _top_reasons(comparison)[:3]
    items.extend(
        f'<li><strong>{_h(_component_name(row))}</strong>'
        f'贡献 {_h(_fixed(row.get("change"), signed=True, unit=" 分"))}</li>'
        for row in reasons
    )
    if not items:
        return (
            '<div class="review-comparison-highlights">'
            '<h4>主要变化</h4><p>暂无可归因的模型贡献变化。</p></div>'
        )
    return (
        '<div class="review-comparison-highlights"><h4>主要变化</h4>'
        f'<ul>{"".join(items)}</ul></div>'
    )


def render_html(comparison) -> str:
    """Render a comparison artifact as a self-contained HTML section."""
    comparison = _mapping(comparison)
    _, date_html = _date_lines(comparison)
    reasons = comparison.get("reasons")
    if reasons in (None, ""):
        reasons = comparison.get("reason")
    content = (
        _html_intraday(comparison)
        if _plain(comparison.get("mode"), "close") == "intraday"
        else _html_close(comparison)
    )
    highlights = _html_primary_changes(comparison)
    return f'''{HTML_BLOCK_START}
<section id="review-comparison" data-status="{_h(comparison.get('status'), 'unavailable')}">
<style>
#review-comparison{{margin:20px 0;color:#1d1d1f}}
#review-comparison *{{box-sizing:border-box}}
#review-comparison .review-comparison-meta{{color:#6b7280;font-size:13px}}
#review-comparison .review-comparison-note{{padding:10px 12px;background:#f8fafc;border-left:3px solid #64748b;border-radius:4px}}
#review-comparison .review-comparison-highlights{{margin:12px 0;padding:12px 14px;background:#f8fafc;border-radius:8px}}
#review-comparison .review-comparison-highlights h4{{margin:0 0 6px}}
#review-comparison .review-comparison-highlights ul{{margin:0;padding-left:20px}}
#review-comparison details{{margin-top:12px;border:1px solid #e5e7eb;border-radius:8px;padding:0 12px 12px}}
#review-comparison summary{{cursor:pointer;padding:12px 0;font-weight:600;overflow-wrap:anywhere}}
#review-comparison .review-comparison-evidence{{overflow-wrap:anywhere}}
#review-comparison .review-comparison-table{{max-width:100%;overflow-x:auto;margin:8px 0 14px}}
#review-comparison table{{width:100%;min-width:620px;border-collapse:collapse}}
#review-comparison th,#review-comparison td{{padding:7px 9px;text-align:left;border-bottom:1px solid #e5e7eb;white-space:nowrap}}
#review-comparison td:last-child{{white-space:normal;min-width:180px;overflow-wrap:anywhere}}
#review-comparison th{{background:#f3f4f6}}
@media(max-width:600px){{
  #review-comparison{{margin:16px 0}}
  #review-comparison .review-comparison-highlights{{padding:10px 12px}}
  #review-comparison details{{padding:0 10px 10px}}
}}
</style>
<h3>与上一交易日比较</h3>
<p class="review-comparison-meta">当前依据日：{_h(comparison.get('basis_date'))}</p>
{date_html}
<p class="review-comparison-meta">比较状态：{_h(_status_label(comparison.get('status')))} · 说明：{_h(_reason_summary(reasons))}</p>
{highlights}
<details class="review-comparison-details">
<summary>展开完整比较与原始证据</summary>
<div class="review-comparison-evidence">
<p class="review-comparison-meta">原始状态：{_h(_status(comparison.get('status')))} · 原始原因：{_h(_plain(_sequence(reasons), '无'))}</p>
{content}
</div>
</details>
</section>
{HTML_BLOCK_END}'''


def render_review_comparison(comparison, format="html") -> str:
    """Render using the integration entry point shared by HTML and Markdown."""
    if format == "html":
        return render_html(comparison)
    if format == "markdown":
        return render_markdown(comparison)
    raise ValueError("format must be html or markdown")


__all__ = ["render_html", "render_markdown", "render_review_comparison"]
