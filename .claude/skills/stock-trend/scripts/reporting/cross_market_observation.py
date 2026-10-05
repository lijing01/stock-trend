#!/usr/bin/env python3
"""Shared HTML and Markdown rendering for cross-market observation rows."""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote


HTML_BLOCK_START = "<!-- CROSS_MARKET_OBSERVATION:START -->"
HTML_BLOCK_END = "<!-- CROSS_MARKET_OBSERVATION:END -->"
MD_BLOCK_START = HTML_BLOCK_START
MD_BLOCK_END = HTML_BLOCK_END
DISCLAIMER = "本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。"

RELATION_LABELS = {
    "direct_industry": "直接行业",
    "supply_chain": "供应链关联",
    "demand": "共同需求关联",
    "demand_link": "共同需求关联",
}
STATUS_LABELS = {
    "complete": "完整",
    "degraded": "降级",
    "unavailable": "不可用",
    "verified": "已验证",
    "unverified": "未验证",
}


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _plain(value: Any, default: str = "—") -> str:
    if value in (None, ""):
        return default
    return str(value)


def _h(value: Any, default: str = "—") -> str:
    return html.escape(_plain(value, default), quote=True)


def _md(value: Any, default: str = "—") -> str:
    text = html.escape(
        _plain(value, default).replace("\r", " ").replace("\n", " "),
        quote=False,
    )
    return text.replace("\\", "\\\\").replace("|", r"\|").replace(
        "[", r"\[").replace("]", r"\]")


def _pct(value: Any, *, ratio: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if ratio:
        number *= 100
    return f"{number:+.2f}%"


def _sector_text(row: Mapping) -> str:
    parts = []
    for value in _sequence(row.get("sector_evidence")):
        sector = _mapping(value)
        streak = sector.get("consecutive_up_days")
        streak_text = "—" if streak is None else (
            ("≥" if sector.get("consecutive_up_days_lower_bound") else "")
            + str(streak)
        )
        parts.append(
            f"{_plain(sector.get('sector_name'))}：5日 {_pct(sector.get('return_5d'), ratio=True)}，"
            f"20日 {_pct(sector.get('return_20d'), ratio=True)}，连续上涨 {streak_text} 日"
        )
    return "；".join(parts) or "同依据日板块证据未验证"


def _items_text(row: Mapping) -> str:
    parts = []
    for value in _sequence(row.get("items")):
        item = _mapping(value)
        structure = {"valid": "有效", "confirmed_holding": "确认后保持",
                     "structure_valid": "有效", "structure_restored": "恢复有效",
                     "structure_invalidated": "失效", "invalid": "失效",
                     "invalidated": "失效", "failed_breakout": "突破失效"}.get(
                         str(item.get("structure_state") or ""), "未验证")
        parts.append(
            f"{_plain(item.get('name') or item.get('code'))}（{_plain(item.get('code'))}，"
            f"结构 {structure}）"
        )
    return "；".join(parts) or "现有观察池无合格匹配标的"


def _conditions_text(row: Mapping) -> str:
    parts = []
    for value in _sequence(row.get("validation_conditions")):
        condition = _mapping(value)
        status = STATUS_LABELS.get(
            str(condition.get("status") or ""), _plain(condition.get("status")))
        parts.append(
            f"{_plain(condition.get('label'))}：{status}（{_plain(condition.get('reason'))}）"
        )
    return "；".join(parts) or "待验证条件缺失"


def _view_rows(state: Mapping) -> list[dict]:
    rows = []
    for value in _sequence(state.get("rows")):
        row = _mapping(value)
        rows.append({
            "symbol": _plain(row.get("symbol")),
            "name": _plain(row.get("name") or row.get("symbol")),
            "performance": (
                f"最近一日 {_pct(row.get('daily_pct'))}；"
                f"假期/区间累计 {_pct(row.get('interval_pct'))}"
            ),
            "us_direction": _plain(row.get("us_direction")),
            "a_share_direction": _plain(row.get("a_share_direction")),
            "relation": RELATION_LABELS.get(
                str(row.get("relation_type") or ""),
                _plain(row.get("relation_type")),
            ),
            "sectors": "、".join(
                _plain(name, "") for name in _sequence(row.get("sector_names"))
            ) or "—",
            "sector_evidence": _sector_text(row),
            "items": _items_text(row),
            "conditions": _conditions_text(row),
            "rationale": _plain(row.get("rationale")),
        })
    return rows


def _sources(state: Mapping) -> list[dict]:
    result = []
    for value in _sequence(state.get("sources")):
        source = _mapping(value)
        title = _plain(source.get("title"), "")
        url = _plain(source.get("url"), "")
        if title and url.startswith(("https://", "http://")):
            result.append({"title": title, "url": url})
    return result


def render_html(state: dict | None) -> str:
    """Render a self-contained, escaped HTML section from normalized rows."""
    state = _mapping(state)
    rows = _view_rows(state)
    body = "".join(
        "<tr>"
        f"<td>{_h(row['name'])}<br><small>{_h(row['symbol'])}</small></td>"
        f"<td>{_h(row['performance'])}<br><small>{_h(row['us_direction'])}</small></td>"
        f"<td>{_h(row['a_share_direction'])}<br><small>{_h(row['sectors'])}</small></td>"
        f"<td>{_h(row['relation'])}</td>"
        f"<td>{_h(row['sector_evidence'])}</td>"
        f"<td>{_h(row['items'])}</td>"
        f"<td>{_h(row['conditions'])}</td>"
        f"<td>{_h(row['rationale'])}</td>"
        "</tr>"
        for row in rows
    )
    if not body:
        body = (
            '<tr><td colspan="8">'
            + _h(state.get("reason") or "跨市场观察证据不可用")
            + "</td></tr>"
        )
    sources = _sources(state)
    source_html = " · ".join(
        f'<a href="{html.escape(source["url"], quote=True)}" rel="noopener noreferrer">'
        f'{_h(source["title"])}</a>' for source in sources
    ) or "映射来源不可用"
    status = STATUS_LABELS.get(
        str(state.get("status") or ""), _plain(state.get("status"), "不可用"))
    return f"""{HTML_BLOCK_START}
<section id="cross-market-observation">
<style>#cross-market-observation{{max-width:100%;min-width:0}}#cross-market-observation .cross-table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}#cross-market-observation table{{width:100%;border-collapse:collapse}}#cross-market-observation th,#cross-market-observation td{{padding:8px 10px;text-align:left;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px}}#cross-market-observation th{{background:#334155;color:#fff}}#cross-market-observation small,#cross-market-observation .cross-meta{{color:#64748b}}#cross-market-observation td{{overflow-wrap:anywhere;word-break:break-word}}</style>
<h3>美股与A股观察关联</h3>
<p class="cross-meta">状态：{_h(status)} · 映射版本：{_h(state.get('mapping_version'))} · 映射核验日：{_h(state.get('mapping_verified_at'))} · A股证据依据日：{_h(state.get('basis_date'))} · 报告截止：{_h(state.get('report_cutoff'))}</p>
<p>{_h(state.get('scope_note'))}</p>
<div class="cross-table-wrap"><table><thead><tr><th>美股标的</th><th>美股表现</th><th>A股观察方向</th><th>关系类型</th><th>板块现状</th><th>现有观察标的</th><th>待验证条件</th><th>映射说明</th></tr></thead><tbody>{body}</tbody></table></div>
<p class="cross-meta">来源：{source_html}</p>
<p class="cross-meta">{_h(DISCLAIMER)}</p>
</section>
{HTML_BLOCK_END}"""


def render_markdown(state: dict | None) -> str:
    """Render the same normalized rows as a marked Markdown section."""
    state = _mapping(state)
    rows = _view_rows(state)
    status = STATUS_LABELS.get(
        str(state.get("status") or ""), _plain(state.get("status"), "不可用"))
    lines = [
        MD_BLOCK_START,
        '<a id="cross-market-observation"></a>',
        "",
        "### 美股与A股观察关联",
        "",
        f"- 状态：{_md(status)}",
        f"- 映射版本：{_md(state.get('mapping_version'))}；核验日：{_md(state.get('mapping_verified_at'))}",
        f"- A股证据依据日：{_md(state.get('basis_date'))}；报告截止：{_md(state.get('report_cutoff'))}",
        f"- 范围：{_md(state.get('scope_note'))}",
        "",
        "| 美股标的 | 美股表现 | A股观察方向 | 关系类型 | 板块现状 | 现有观察标的 | 待验证条件 | 映射说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {_md(row['name'])}（{_md(row['symbol'])}） | {_md(row['performance'])}（{_md(row['us_direction'])}） | "
            f"{_md(row['a_share_direction'])}（{_md(row['sectors'])}） | {_md(row['relation'])} | "
            f"{_md(row['sector_evidence'])} | {_md(row['items'])} | {_md(row['conditions'])} | {_md(row['rationale'])} |"
        )
    if not rows:
        lines.append(
            f"| {_md(state.get('reason') or '跨市场观察证据不可用')} | — | — | — | — | — | — | — |"
        )
    sources = _sources(state)
    lines.extend(["", "来源："])
    if sources:
        for source in sources:
            safe_url = quote(source["url"], safe=":/?&=#%+,-._~")
            lines.append(f"- [{_md(source['title'])}]({safe_url})")
    else:
        lines.append("- 映射来源不可用")
    lines.extend(["", DISCLAIMER, "", MD_BLOCK_END])
    return "\n".join(lines)
