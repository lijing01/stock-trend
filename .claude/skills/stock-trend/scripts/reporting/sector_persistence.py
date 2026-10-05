#!/usr/bin/env python3
"""Shared HTML/Markdown rendering for sector persistence evidence."""

from __future__ import annotations

import html


def _pct(value):
    return "—" if value is None else f"{value * 100:+.2f}%"


def _status(value):
    return {"complete": "完整", "partial": "部分", "missing": "缺失"}.get(
        value, "缺失")


REASON_LABELS = {
    "observation_memberships_unavailable": "观察标的关联板块证据尚未齐备",
    "authority_calendar_unverified": "权威交易日未核验",
    "window_5d_insufficient": "5日窗口不足",
    "window_20d_insufficient": "20日窗口不足",
    "window_5d_not_contiguous": "5日交易日序列断档",
    "window_20d_not_contiguous": "20日交易日序列断档",
    "window_5d_invalid_close": "5日收盘价无效",
    "window_20d_invalid_close": "20日收盘价无效",
    "duplicate_trade_date": "交易日重复",
    "mixed_sector_id": "板块身份不一致",
    "mixed_classification_version": "板块分类版本不一致",
    "mixed_source": "行情来源不一致",
    "mixed_price_type": "价格类型不一致",
    "artifact_missing": "本次运行绑定缓存缺失",
    "artifact_invalid": "本次运行绑定缓存无效",
    "artifact_binding_mismatch": "本次运行绑定缓存不匹配",
    "frozen_run_binding_missing": "本次运行冻结绑定缺失",
    "fetch_timeout": "板块行情抓取超时",
    "fetch_empty": "板块行情未返回",
    "completed_basis_or_calendar_unverified": "收盘依据日或权威交易日未完成核验",
}


def _reason_text(reasons):
    labels = []
    for reason in reasons or []:
        text = str(reason)
        if text.startswith("observation_sector_identity_unverified:"):
            count = text.rsplit(":", 1)[-1]
            labels.append(f"{count} 个观察关联板块身份未核验")
        elif text.startswith("fetch_error:"):
            labels.append("板块行情抓取失败")
        else:
            labels.append(REASON_LABELS.get(text, "板块证据缺失"))
    return "；".join(labels)


def _streak(item):
    value = item.get("consecutive_up_days")
    if value is None:
        return "—"
    prefix = "≥" if item.get("consecutive_up_days_lower_bound") else ""
    return f"{prefix}{value}"


def _evidence(item):
    reasons = _reason_text(item.get("reasons"))
    return reasons or _status(item.get("status"))


def _md_cell(value):
    return str(value or "—").replace("\\", "\\\\").replace(
        "|", "\\|").replace("\r", " ").replace("\n", " ")


def render_html(state: dict | None) -> str:
    state = state or {}
    rows = []
    for item in state.get("items") or []:
        ratio = item.get("constituent_up_ratio")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('name') or item.get('code') or '—'))}</td>"
            f"<td>{_pct(item.get('return_5d'))}</td>"
            f"<td>{_pct(item.get('return_20d'))}</td>"
            f"<td>{_streak(item)}</td>"
            f"<td>{_pct(ratio)}</td>"
            f"<td>{html.escape(_evidence(item))}</td>"
            "</tr>"
        )
    note = "口径：当日展示板块（当日最强10、最弱3及观察标的关联板块）的收盘证据；不代表全市场中期排名。"
    state_reason = _reason_text(state.get("reasons"))
    return (
        '<section id="sector-persistence"><h3>板块持续性</h3>'
        f'<p class="dt">{note}</p>'
        + (f'<p class="dt">证据说明：{html.escape(state_reason)}</p>'
           if state_reason else '')
        +
        '<div class="summary-table-wrap"><table><thead><tr>'
        '<th>板块</th><th>5日收益</th><th>20日收益</th>'
        '<th>连续上涨交易日数</th><th>板块内上涨比例</th><th>证据</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="6">'
           + html.escape(_reason_text(state.get("reasons")) or "证据不足")
           + '</td></tr>')
        + "</tbody></table></div></section>"
    )


def render_markdown(state: dict | None) -> str:
    state = state or {}
    items = state.get("items") or []
    lines = [
        "### 板块持续性",
        "",
        "口径：当日展示板块（当日最强10、最弱3及观察标的关联板块）的收盘证据；不代表全市场中期排名。",
        "",
        "| 板块 | 5日收益 | 20日收益 | 连续上涨交易日数 | 板块内上涨比例 | 证据 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    state_reason = _reason_text(state.get("reasons"))
    if state_reason:
        lines[4:4] = [f"证据说明：{_md_cell(state_reason)}", ""]
    for item in items:
        lines.append(
            f"| {_md_cell(item.get('name') or item.get('code'))} | "
            f"{_pct(item.get('return_5d'))} | {_pct(item.get('return_20d'))} | "
            f"{_streak(item)} | "
            f"{_pct(item.get('constituent_up_ratio'))} | "
            f"{_md_cell(_evidence(item))} |"
        )
    if not items:
        reason = _reason_text(state.get("reasons")) or "证据不足"
        lines.append(f"| {_md_cell(reason)} | — | — | — | — | 缺失 |")
    return "\n".join(lines)
