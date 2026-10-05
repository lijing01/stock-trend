"""Offline rendering and idempotent report updates for U.S. market summaries."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from urllib.parse import quote


HTML_BLOCK_START = "<!-- US_MARKET_SUMMARY:START -->"
HTML_BLOCK_END = "<!-- US_MARKET_SUMMARY:END -->"
MD_BLOCK_START = "<!-- US_MARKET_SUMMARY:START -->"
MD_BLOCK_END = "<!-- US_MARKET_SUMMARY:END -->"
OBSERVATION_BLOCK_START = "<!-- OBSERVATION_LIST:START -->"

_INDEX_ORDER = ("SPY", "QQQ", "DIA", "IWM")
_INDEX_DEFAULT_NAMES = {
    "SPY": "标普500 ETF代理",
    "QQQ": "纳斯达克100 ETF代理",
    "DIA": "道指 ETF代理",
    "IWM": "罗素2000 ETF代理",
}
_STATUS_LABELS = {
    "complete": "完整",
    "ok": "完整",
    "partial": "部分可用",
    "cached": "缓存",
    "empty": "暂无新增收盘",
    "unavailable": "不可用",
    "missing": "数据缺失",
    "error": "异常",
}
_ANCHOR_STATUS_LABELS = {
    "qualified": "已核验",
    "verified": "已核验",
    "degraded": "降级核验",
    "legacy": "历史口径",
    "legacy_basis": "历史口径",
    "explicit": "显式锚点",
    "unavailable": "不可用",
    "unknown": "未核验",
}
_NYSE_CALENDAR_URL = "https://www.nyse.com/trade/hours-calendars"
_SECTOR_SOURCE_URL = (
    "https://www.ssga.com/us/en/intermediary/capabilities/equities/"
    "sector-investing/select-sector-etfs"
)


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(summary: Mapping, group: str) -> list[Mapping]:
    values = _mapping(_mapping(summary.get("groups")).get(group))
    if values:
        # Be permissive if a future artifact wraps rows with metadata.
        values = values.get("rows", [])
    else:
        values = _mapping(summary.get("groups")).get(group, [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _pct(value) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:+.2f}%"


def _pp(value) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:+.2f} 个百分点"


def _price(value) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _plain(value, default: str = "—") -> str:
    if value in (None, ""):
        return default
    if isinstance(value, (list, tuple, set)):
        return "、".join(_plain(item, "") for item in value) or default
    if isinstance(value, Mapping):
        preferred = (
            value.get("message") or value.get("reason") or value.get("error")
            or value.get("name") or value.get("provider")
        )
        if preferred not in (None, ""):
            return _plain(preferred, default)
        flattened = [f"{_plain(key, '')}: {_plain(item, '')}" for key, item in value.items()]
        return "；".join(part for part in flattened if part.strip(": ")) or default
    return str(value)


def _h(value, default: str = "—") -> str:
    return escape(_plain(value, default), quote=True)


def _md(value, default: str = "—") -> str:
    text = escape(_plain(value, default).replace("\r", " ").replace("\n", " "), quote=False)
    return text.replace("\\", "\\\\").replace("|", r"\|").replace("[", r"\[").replace("]", r"\]")


def _status(summary: Mapping) -> tuple[str, str]:
    status = _plain(summary.get("status"), "").lower()
    raw = "empty" if status == "empty" else _plain(
        summary.get("data_quality") or summary.get("status"), "unavailable"
    )
    return raw, _STATUS_LABELS.get(raw.lower(), raw)


def _anchor_status(summary: Mapping) -> tuple[str, str]:
    evidence = _mapping(summary.get("anchor_evidence"))
    raw = _plain(
        summary.get("anchor_qualification") or evidence.get("status"),
        "unknown",
    ).lower()
    return raw, _ANCHOR_STATUS_LABELS.get(raw, raw)


def _legacy_artifact(summary: Mapping) -> bool:
    return _plain(summary.get("schema_version"), "") in {
        "", "us-market-summary/v1",
    }


def _anchor_date(summary: Mapping) -> object:
    value = summary.get("a_share_anchor_date")
    if value not in (None, ""):
        return value
    return summary.get("basis_date") if _legacy_artifact(summary) else None


def _latest_sessions(summary: Mapping) -> tuple[object, object]:
    latest = summary.get("latest_completed_session")
    if latest in (None, "") and _legacy_artifact(summary):
        latest = summary.get("actual_end_session")
    previous = (
        summary.get("latest_previous_session") or summary.get("previous_session")
    )
    return latest, previous


def _anchor_reason(summary: Mapping) -> object:
    evidence = _mapping(summary.get("anchor_evidence"))
    return summary.get("anchor_reason") or evidence.get("reason") or evidence.get("source")


def _anchor_digest(summary: Mapping) -> object:
    return _mapping(summary.get("anchor_evidence")).get("evidence_sha256")


def _sorted_performance(rows: Sequence[Mapping]) -> list[Mapping]:
    return sorted(
        rows,
        key=lambda row: (
            _number(row.get("interval_pct")) is None,
            -(_number(row.get("interval_pct")) or 0.0),
            _plain(row.get("symbol"), ""),
        ),
    )


def _index_rows(summary: Mapping) -> list[Mapping]:
    by_symbol = {_plain(row.get("symbol"), "").upper(): row for row in _rows(summary, "indices")}
    return [
        by_symbol.get(symbol, {"symbol": symbol, "name": _INDEX_DEFAULT_NAMES[symbol], "status": "missing"})
        for symbol in _INDEX_ORDER
    ]


def _row_name(row: Mapping) -> str:
    return _plain(row.get("name") or row.get("symbol"))


def _row_reason(row: Mapping) -> str:
    return _plain(
        row.get("reason") or row.get("reasons") or row.get("error") or row.get("errors"),
        "—",
    )


def _symbol_url(symbol) -> str:
    return "https://finance.yahoo.com/quote/" + quote(_plain(symbol, ""), safe=".-^")


def _narrative(summary: Mapping) -> list[str]:
    if _plain(summary.get("status"), "").lower() == "empty":
        return ["区间无新增交易日；仍展示最近已完成美股交易日的单日涨跌。"]
    indices = {str(row.get("symbol", "")).upper(): row for row in _rows(summary, "indices")}
    market_bits = []
    for symbol in ("SPY", "QQQ"):
        row = indices.get(symbol)
        if row and _number(row.get("interval_pct")) is not None:
            market_bits.append(f"{symbol} 区间{_pct(row.get('interval_pct'))}")

    sentences = []
    if market_bits:
        sentences.append("大盘ETF代理：" + "，".join(market_bits) + "。")
    sectors = [row for row in _sorted_performance(_rows(summary, "sectors")) if _number(row.get("interval_pct")) is not None]
    if sectors:
        sentences.append(
            f"标普500行业ETF代理中，{_row_name(sectors[0])}领先（{_pct(sectors[0].get('interval_pct'))}），"
            f"{_row_name(sectors[-1])}相对靠后（{_pct(sectors[-1].get('interval_pct'))}）。"
        )
    stocks = [row for row in _sorted_performance(_rows(summary, "stocks")) if _number(row.get("interval_pct")) is not None]
    if stocks:
        sentences.append(
            f"固定代表观察池内，{_row_name(stocks[0])}领先（{_pct(stocks[0].get('interval_pct'))}），"
            f"{_row_name(stocks[-1])}靠后（{_pct(stocks[-1].get('interval_pct'))}）。"
        )
    if not sentences:
        sentences.append("当前没有足够的共同端点数据，区间表现暂不可计算。")
    return sentences


def _html_daily_details(summary: Mapping, groups: Sequence[tuple[str, Sequence[Mapping]]]) -> str:
    detail_rows = []
    empty_interval = _plain(summary.get("status"), "").lower() == "empty"
    for group_name, rows in groups:
        for row in rows:
            symbol = _h(row.get("symbol"))
            if empty_interval:
                detail_rows.append(
                    f"<tr><td>{_h(group_name)}</td><td>{symbol}</td><td>{_h(row.get('latest_previous_session') or row.get('previous_session'))}（前一交易日）</td>"
                    f"<td>{_h(_price(row.get('previous_price')))}</td><td>—</td></tr>"
                )
                detail_rows.append(
                    f"<tr><td>{_h(group_name)}</td><td>{symbol}</td><td>{_h(row.get('latest_completed_session') or row.get('actual_end_session'))}（最近完成）</td>"
                    f"<td>{_h(_price(row.get('latest_price')))}</td><td>{_h(_pct(row.get('daily_pct')))}</td></tr>"
                )
                continue
            detail_rows.append(
                f"<tr><td>{_h(group_name)}</td><td>{symbol}</td><td>{_h(summary.get('baseline_session'))}（基准）</td>"
                f"<td>{_h(_price(row.get('baseline_price')))}</td><td>—</td></tr>"
            )
            daily = row.get("daily")
            if not isinstance(daily, Sequence) or isinstance(daily, (str, bytes)) or not daily:
                detail_rows.append(
                    f"<tr><td>{_h(group_name)}</td><td>{symbol}</td><td>{_h(row.get('actual_end_session'))}（终点）</td>"
                    f"<td>{_h(_price(row.get('end_price')))}</td><td>{_h(_pct(row.get('daily_pct')))}</td></tr>"
                )
                continue
            for item in daily:
                item = _mapping(item)
                detail_rows.append(
                    f"<tr><td>{_h(group_name)}</td><td>{symbol}</td><td>{_h(item.get('date'))}</td>"
                    f"<td>{_h(_price(item.get('price')))}</td><td>{_h(_pct(item.get('change_pct')))}</td></tr>"
                )
    return (
        '<details class="us-details"><summary>每日明细与价格端点</summary>'
        '<div class="us-table-wrap"><table><thead><tr><th>分组</th><th>代码</th><th>交易日</th>'
        '<th>复权价格</th><th>当日涨跌</th></tr></thead><tbody>'
        + ("".join(detail_rows) or '<tr><td colspan="5">数据缺失</td></tr>')
        + "</tbody></table></div></details>"
    )


def _html_anchor_details(summary: Mapping) -> str:
    raw_status, _ = _anchor_status(summary)
    return (
        '<details class="us-details us-technical-details">'
        '<summary>查看锚点资格与原始证据</summary>'
        '<dl class="us-technical-list">'
        f'<div><dt>原始资格代码</dt><dd class="us-technical-value">{_h(raw_status)}</dd></div>'
        f'<div><dt>原始原因</dt><dd class="us-technical-value">{_h(_anchor_reason(summary))}</dd></div>'
        f'<div><dt>锚点证据摘要</dt><dd class="us-technical-value">{_h(_anchor_digest(summary))}</dd></div>'
        '</dl></details>'
    )


def render_html(summary) -> str:
    """Render a self-contained, scoped HTML block without network access."""
    summary = _mapping(summary)
    status_raw, status_label = _status(summary)
    _, anchor_status_label = _anchor_status(summary)
    latest_completed, latest_previous = _latest_sessions(summary)
    source_status = _plain(summary.get("source_status"), "unknown")
    anchor_digest = _anchor_digest(summary)
    empty_interval = _plain(summary.get("status"), "").lower() == "empty"
    quality_raw = _plain(summary.get("data_quality"), "unavailable").lower()
    quality_label = _STATUS_LABELS.get(quality_raw, quality_raw)
    badge_label = f"{status_label} · {quality_label}" if empty_interval else status_label
    index_rows = _index_rows(summary)
    sector_rows = _sorted_performance(_rows(summary, "sectors"))
    stock_rows = _sorted_performance(_rows(summary, "stocks"))

    cards = []
    for row in index_rows:
        if empty_interval:
            endpoint_text = (
                f"最近一日价格 {_h(_price(row.get('previous_price')))} → "
                f"{_h(_price(row.get('latest_price')))}"
            )
        else:
            endpoint_text = (
                f"区间端点 {_h(_price(row.get('baseline_price')))} → "
                f"{_h(_price(row.get('end_price')))}"
            )
        cards.append(
            '<article class="us-card">'
            f'<div><strong>{_h(row.get("name") or _INDEX_DEFAULT_NAMES.get(_plain(row.get("symbol")), "ETF代理"))}</strong> '
            f'<a href="{escape(_symbol_url(row.get("symbol")), quote=True)}" rel="noopener noreferrer">{_h(row.get("symbol"))}</a></div>'
            f'<div class="us-return">{_h(_pct(row.get("interval_pct")))}</div>'
            f'<div class="us-muted">最近一日 {_h(_pct(row.get("daily_pct")))} · 截止 {_h(row.get("actual_end_session"))}</div>'
            f'<div class="us-muted">{endpoint_text} · {_h(row.get("status"), "数据缺失")} · {_h(_row_reason(row))}</div>'
            "</article>"
        )

    sectors_html = []
    for row in sector_rows:
        sectors_html.append(
            f'<tr><td>{_h(row.get("name"))}</td><td><a href="{escape(_symbol_url(row.get("symbol")), quote=True)}" '
            f'rel="noopener noreferrer">{_h(row.get("symbol"))}</a></td><td>{_h(_pct(row.get("interval_pct")))}</td>'
            f'<td>{_h(_pct(row.get("daily_pct")))}</td><td>{_h(_pp(row.get("relative_spy_pp")))}</td>'
            f'<td>{_h(row.get("actual_end_session"))}</td><td>{_h(row.get("status"), "数据缺失")}</td>'
            f'<td class="us-wrap">{_h(_row_reason(row))}</td></tr>'
        )

    stocks_html = []
    for row in stock_rows:
        sort_value = _number(row.get("interval_pct"))
        sort_attr = "-999999" if sort_value is None else f"{sort_value:.12g}"
        stocks_html.append(
            f'<tr data-sector="{_h(row.get("sector"), "未分类")}" data-return="{sort_attr}">'
            f'<td>{_h(row.get("name"))}</td><td><a href="{escape(_symbol_url(row.get("symbol")), quote=True)}" '
            f'rel="noopener noreferrer">{_h(row.get("symbol"))}</a></td><td>{_h(row.get("sector"), "未分类")}</td>'
            f'<td>{_h(_pct(row.get("interval_pct")))}</td><td>{_h(_pct(row.get("daily_pct")))}</td>'
            f'<td>{_h(row.get("actual_end_session"))}</td><td>{_h(row.get("status"), "数据缺失")}</td>'
            f'<td class="us-wrap">{_h(_row_reason(row))}</td></tr>'
        )

    errors = summary.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        errors = [errors] if errors else []
    errors_html = ""
    if errors:
        errors_html = '<div class="us-errors"><strong>数据限制：</strong><ul>' + "".join(
            f"<li>{_h(error)}</li>" for error in errors
        ) + "</ul></div>"

    sessions = summary.get("sessions")
    session_count = len(sessions) if isinstance(sessions, Sequence) and not isinstance(sessions, (str, bytes)) else 0
    narratives = "".join(f"<p>{escape(line, quote=True)}</p>" for line in _narrative(summary))
    details = _html_daily_details(summary, (
        ("大盘ETF", index_rows), ("行业ETF", sector_rows), ("代表个股", stock_rows),
    ))
    anchor_details = _html_anchor_details(summary)
    return f"""{HTML_BLOCK_START}
<section id="us-market-summary" data-status="{escape(status_raw, quote=True)}" data-anchor-evidence="{_h(anchor_digest, '')}">
<style>
#us-market-summary{{max-width:100%;min-width:0;margin:22px 0 10px;color:#1d1d1f;overflow-wrap:anywhere}}
#us-market-summary *{{box-sizing:border-box}}
#us-market-summary .us-head{{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}}
#us-market-summary .us-head h2{{flex:1 1 260px}}
#us-market-summary .us-badge{{padding:3px 9px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px}}
#us-market-summary .us-meta,#us-market-summary .us-muted{{color:#6b7280;font-size:13px;line-height:1.55}}
#us-market-summary .us-note{{margin:10px 0;padding:10px 12px;background:#f8fafc;border-left:3px solid #64748b;border-radius:4px}}
#us-market-summary .us-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:12px 0}}
#us-market-summary .us-card{{min-width:0;border:1px solid #e5e7eb;border-radius:8px;padding:11px;background:#fff}}
#us-market-summary .us-return{{font-size:22px;font-weight:750;margin:5px 0}}
#us-market-summary a{{color:#1d4ed8;text-decoration:none}}
#us-market-summary .us-table-wrap{{overflow-x:auto;max-width:100%;margin:10px 0;-webkit-overflow-scrolling:touch}}
#us-market-summary table{{min-width:760px;width:100%;border-collapse:collapse;margin:0}}
#us-market-summary th,#us-market-summary td{{padding:8px 10px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;white-space:nowrap}}
#us-market-summary td.us-wrap{{min-width:180px;white-space:normal;overflow-wrap:anywhere;word-break:break-word}}
#us-market-summary th{{background:#1d4ed8;color:#fff}}
#us-market-summary .us-toolbar{{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin:8px 0;font-size:13px}}
#us-market-summary select{{max-width:100%;padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;background:#fff}}
#us-market-summary .us-details{{margin:12px 0}}
#us-market-summary .us-details summary{{cursor:pointer;font-weight:650}}
#us-market-summary .us-technical-list{{margin:8px 0;padding:10px 12px;background:#f8fafc;border-radius:6px;font-size:13px}}
#us-market-summary .us-technical-list div{{display:grid;grid-template-columns:minmax(110px,auto) minmax(0,1fr);gap:8px;margin:5px 0}}
#us-market-summary .us-technical-list dt{{font-weight:650}}
#us-market-summary .us-technical-list dd{{min-width:0;margin:0}}
#us-market-summary .us-technical-value{{overflow-wrap:anywhere;word-break:break-word}}
#us-market-summary .us-errors{{margin:10px 0;padding:10px 12px;background:#fff7ed;color:#9a3412;border-radius:6px;font-size:13px}}
#us-market-summary .us-errors ul{{margin:4px 0 0;padding-left:18px}}
@media(max-width:600px){{#us-market-summary .us-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}#us-market-summary .us-card{{padding:9px}}}}
@media(max-width:390px){{#us-market-summary .us-grid{{grid-template-columns:1fr}}#us-market-summary .us-toolbar{{justify-content:flex-start;flex-wrap:wrap}}}}
</style>
<div class="us-head"><h2>③ 美股区间概要</h2><span class="us-badge">{_h(badge_label)} · {_h(source_status)}</span></div>
<p class="us-meta">A股报告依据日：{_h(summary.get('basis_date'))} · 美股锚点交易日：{_h(_anchor_date(summary))} · 锚点时刻：{_h(summary.get('anchor_at'))}</p>
<p class="us-meta">A股锚点资格：{_h(anchor_status_label)} · 请求截止：{_h(summary.get('requested_as_of') or summary.get('as_of'))} · 数据截止：{_h(summary.get('as_of'))}</p>
<p class="us-meta">最近已完成美股交易日：{_h(latest_completed)} · 前一交易日：{_h(latest_previous)} · 美国区间基准日：{_h(summary.get('baseline_session'))} · 预期终点：{_h(summary.get('expected_end_session'))} · 区间新增交易日：{session_count} 个 · 行情抓取于：{_h(summary.get('fetched_at'))}</p>
<div class="us-note">{narratives}</div>
<h3>大盘ETF代理</h3><div class="us-grid">{''.join(cards)}</div>
<h3>标普500主要行业ETF代理</h3><div class="us-table-wrap"><table><thead><tr><th>行业</th><th>ETF</th><th>区间复权涨跌</th><th>最近一日</th><th>相对SPY</th><th>截止日</th><th>状态</th><th>说明</th></tr></thead><tbody>{''.join(sectors_html) or '<tr><td colspan="8">数据缺失</td></tr>'}</tbody></table></div>
<div class="us-head"><h3>固定代表个股观察池</h3><label class="us-toolbar">排序 <select data-us-sort><option value="return">区间涨跌</option><option value="sector">行业</option></select></label></div>
<div class="us-table-wrap"><table><thead><tr><th>名称</th><th>代码</th><th>行业</th><th>区间复权涨跌</th><th>最近一日</th><th>截止日</th><th>状态</th><th>说明</th></tr></thead><tbody data-us-stocks>{''.join(stocks_html) or '<tr><td colspan="8">数据缺失</td></tr>'}</tbody></table></div>
{anchor_details}{details}{errors_html}
<p class="us-meta">口径：仅统计截止时刻前已完成的美股常规交易时段，涨跌使用一致的复权收盘价。行业表现为标普500行业ETF代理；个股为固定代表观察池，不代表全市场排行。</p>
<p class="us-meta">来源：<a href="https://finance.yahoo.com/" rel="noopener noreferrer">Yahoo Finance 行情</a> · <a href="{_NYSE_CALENDAR_URL}" rel="noopener noreferrer">NYSE交易日历</a> · <a href="{_SECTOR_SOURCE_URL}" rel="noopener noreferrer">State Street行业ETF资料</a> · 提供方 {_h(summary.get('provider'))}</p>
<p class="us-meta">本区块仅供学习参考，不构成任何投资建议。</p>
<script>(function(){{var s=document.querySelector('#us-market-summary [data-us-sort]'),b=document.querySelector('#us-market-summary [data-us-stocks]');if(!s||!b)return;s.addEventListener('change',function(){{var r=Array.prototype.slice.call(b.querySelectorAll('tr[data-return]'));r.sort(function(a,c){{if(s.value==='sector')return a.dataset.sector.localeCompare(c.dataset.sector,'zh-CN')||Number(c.dataset.return)-Number(a.dataset.return);return Number(c.dataset.return)-Number(a.dataset.return)}});r.forEach(function(x){{b.appendChild(x)}})}})}})();</script>
</section>
{HTML_BLOCK_END}"""


def render_markdown(summary) -> str:
    """Render the same summary as a marked Markdown section."""
    summary = _mapping(summary)
    _, status_label = _status(summary)
    _, anchor_status_label = _anchor_status(summary)
    latest_completed, latest_previous = _latest_sessions(summary)
    index_rows = _index_rows(summary)
    sector_rows = _sorted_performance(_rows(summary, "sectors"))
    stock_rows = _sorted_performance(_rows(summary, "stocks"))
    sessions = summary.get("sessions")
    session_count = len(sessions) if isinstance(sessions, Sequence) and not isinstance(sessions, (str, bytes)) else 0
    lines = [
        MD_BLOCK_START,
        '<a id="us-market-summary"></a>',
        "",
        "### ③ 美股区间概要",
        "",
        f"- 数据状态：{_md(status_label)}；来源状态：{_md(summary.get('source_status'))}",
        f"- A股报告依据日：{_md(summary.get('basis_date'))}；美股锚点交易日：{_md(_anchor_date(summary))}；锚点时刻：{_md(summary.get('anchor_at'))}",
        f"- A股锚点资格：{_md(anchor_status_label)}（原始代码：{_md(_anchor_status(summary)[0])}）；原因：{_md(_anchor_reason(summary))}",
        f"- 锚点证据摘要：{_md(_anchor_digest(summary))}",
        f"- 请求截止：{_md(summary.get('requested_as_of') or summary.get('as_of'))}；数据截止：{_md(summary.get('as_of'))}",
        f"- 最近已完成美股交易日：{_md(latest_completed)}；前一交易日：{_md(latest_previous)}",
        f"- 美国区间基准日：{_md(summary.get('baseline_session'))}；预期终点：{_md(summary.get('expected_end_session'))}；区间新增交易日：{session_count} 个",
        f"- 行情提供方：{_md(summary.get('provider'))}；抓取时间：{_md(summary.get('fetched_at'))}",
        "",
    ]
    lines.extend(f"> {_md(sentence)}" for sentence in _narrative(summary))
    lines.extend(["", "#### 大盘ETF代理", "", "| 名称 | ETF | 区间复权涨跌 | 最近一日 | 截止日 | 状态 | 说明 |", "|---|---|---:|---:|---|---|---|"])
    for row in index_rows:
        symbol = _md(row.get("symbol"))
        lines.append(
            f"| {_md(row.get('name') or _INDEX_DEFAULT_NAMES.get(_plain(row.get('symbol')), 'ETF代理'))} | "
            f"[{symbol}]({_symbol_url(row.get('symbol'))}) | {_pct(row.get('interval_pct'))} | {_pct(row.get('daily_pct'))} | "
            f"{_md(row.get('actual_end_session'))} | {_md(row.get('status'), '数据缺失')} | {_md(_row_reason(row))} |"
        )
    lines.extend(["", "#### 标普500主要行业ETF代理", "", "| 行业 | ETF | 区间复权涨跌 | 最近一日 | 相对SPY | 截止日 | 状态 | 说明 |", "|---|---|---:|---:|---:|---|---|---|"])
    for row in sector_rows:
        symbol = _md(row.get("symbol"))
        lines.append(
            f"| {_md(row.get('name'))} | [{symbol}]({_symbol_url(row.get('symbol'))}) | {_pct(row.get('interval_pct'))} | "
            f"{_pct(row.get('daily_pct'))} | {_pp(row.get('relative_spy_pp'))} | {_md(row.get('actual_end_session'))} | {_md(row.get('status'), '数据缺失')} | {_md(_row_reason(row))} |"
        )
    if not sector_rows:
        lines.append("| 数据缺失 | — | — | — | — | — | unavailable | — |")
    lines.extend(["", "#### 固定代表个股观察池（按区间涨跌排序）", "", "| 名称 | 代码 | 行业 | 区间复权涨跌 | 最近一日 | 截止日 | 状态 | 说明 |", "|---|---|---|---:|---:|---|---|---|"])
    for row in stock_rows:
        symbol = _md(row.get("symbol"))
        lines.append(
            f"| {_md(row.get('name'))} | [{symbol}]({_symbol_url(row.get('symbol'))}) | {_md(row.get('sector'), '未分类')} | "
            f"{_pct(row.get('interval_pct'))} | {_pct(row.get('daily_pct'))} | {_md(row.get('actual_end_session'))} | {_md(row.get('status'), '数据缺失')} | {_md(_row_reason(row))} |"
        )
    if not stock_rows:
        lines.append("| 数据缺失 | — | — | — | — | — | unavailable | — |")

    errors = summary.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        errors = [errors] if errors else []
    if errors:
        lines.extend(["", "数据限制："] + [f"- {_md(error)}" for error in errors])
    lines.extend([
        "",
        "口径：仅统计截止时刻前已完成的美股常规交易时段，使用一致的复权收盘价。行业表现为标普500行业ETF代理；个股为固定代表观察池，不代表全市场排行。",
        "",
        f"来源：[Yahoo Finance 行情](https://finance.yahoo.com/) · [NYSE交易日历]({_NYSE_CALENDAR_URL}) · [State Street行业ETF资料]({_SECTOR_SOURCE_URL})",
        "",
        "本区块仅供学习参考，不构成任何投资建议。",
        MD_BLOCK_END,
    ])
    return "\n".join(lines)


def _replace_or_insert_html(original: str, block: str) -> str:
    start_count = original.count(HTML_BLOCK_START)
    end_count = original.count(HTML_BLOCK_END)
    if start_count or end_count:
        if start_count != 1 or end_count != 1:
            raise ValueError("us_market_summary_block_invalid")
        start = original.index(HTML_BLOCK_START)
        end = original.index(HTML_BLOCK_END, start) + len(HTML_BLOCK_END)
        return original[:start] + block + original[end:]

    if original.count(OBSERVATION_BLOCK_START) == 1:
        position = original.index(OBSERVATION_BLOCK_START)
    else:
        footer_matches = list(re.finditer(r"<footer\b", original, flags=re.IGNORECASE))
        if len(footer_matches) != 1:
            raise ValueError("us_market_summary_insertion_point_not_unique")
        position = footer_matches[0].start()
    return original[:position] + block + "\n\n" + original[position:]


def _replace_or_insert_markdown(original: str, block: str) -> str:
    start_count = original.count(MD_BLOCK_START)
    end_count = original.count(MD_BLOCK_END)
    if start_count or end_count:
        if start_count != 1 or end_count != 1:
            raise ValueError("us_market_summary_markdown_block_invalid")
        start = original.index(MD_BLOCK_START)
        end = original.index(MD_BLOCK_END, start) + len(MD_BLOCK_END)
        return original[:start] + block + original[end:]

    separators = list(re.finditer(r"(?m)^---\s*$", original))
    if not separators:
        raise ValueError("us_market_summary_markdown_insertion_point_missing")
    position = separators[-1].start()
    if "投资建议" not in original[position:]:
        raise ValueError("us_market_summary_markdown_disclaimer_missing")
    return original[:position].rstrip() + "\n\n" + block + "\n\n" + original[position:]


def _html_basis_date(content: str) -> str:
    match = re.search(
        r"<h1\b[^>]*>\s*(?:📅\s*)?今日复盘\s+(\d{4}-\d{2}-\d{2})\s*</h1>",
        content,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("daily_review_html_basis_date_missing")
    return match.group(1)


def _markdown_basis_date(content: str) -> str:
    match = re.search(r"(?m)^##\s+📅\s+今日复盘\s*\((\d{4}-\d{2}-\d{2})\)\s*$", content)
    if not match:
        raise ValueError("daily_review_markdown_basis_date_missing")
    return match.group(1)


def _validate_basis(report_basis: str, summary: Mapping) -> None:
    summary_basis = _plain(summary.get("basis_date"), "")
    if not summary_basis or report_basis != summary_basis:
        raise ValueError(f"basis_date_mismatch: report={report_basis}, summary={summary_basis or 'missing'}")


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".pre-us-summary")


def _write_updated(path: Path, original: str, updated: str) -> None:
    from core.report_file import atomic_write_text

    backup = _backup_path(path)
    if not backup.exists():
        atomic_write_text(backup, original)
    atomic_write_text(path, updated)


def update_reports(html_path, summary) -> dict:
    """Update an existing HTML daily review and its same-name Markdown report."""
    from core.report_file import report_lock

    summary = _mapping(summary)
    html_path = Path(html_path)
    md_path = html_path.with_suffix(".md")
    md_written = False
    with report_lock(html_path):
        # Re-read under the shared lock so an observation-list update cannot be lost.
        html_original = html_path.read_text(encoding="utf-8")
        _validate_basis(_html_basis_date(html_original), summary)
        html_updated = _replace_or_insert_html(html_original, render_html(summary))
        if md_path.exists():
            with report_lock(md_path):
                md_original = md_path.read_text(encoding="utf-8")
                _validate_basis(_markdown_basis_date(md_original), summary)
                md_updated = _replace_or_insert_markdown(md_original, render_markdown(summary))
                # Both reports have been validated before either visible file changes.
                _write_updated(html_path, html_original, html_updated)
                _write_updated(md_path, md_original, md_updated)
                md_written = True
        else:
            _write_updated(html_path, html_original, html_updated)

    return {
        "status": "updated",
        "html_path": str(html_path.resolve()),
        "markdown_path": str(md_path.resolve()) if md_written else None,
        "basis_date": summary.get("basis_date"),
        "data_quality": summary.get("data_quality") or summary.get("status") or "unavailable",
    }


__all__ = ["render_html", "render_markdown", "update_reports"]
