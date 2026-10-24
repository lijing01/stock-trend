"""Render yesterday's frozen cohort without filtering today's losers."""

import html
import math
import re

REASONS = {
    "market_feedback_binding_missing": "该次复盘未绑定强势反馈证据，离线重放不可补采。",
    "adjacent_cohort_missing": "缺少上一交易日冻结名单；首次启用不倒选历史样本。",
    "calendar_invalid": "权威交易日历不可用，无法核验相邻交易日。",
    "calendar_current_session_missing": "权威交易日历未覆盖依据日。",
    "previous_session_outside_calendar_coverage": "权威交易日历未覆盖上一交易日。",
    "historical_live_collection_forbidden": "依据日不是当前采集日，不能用后来行情补成当时反馈。",
    "adjacent_close_feedback_missing": "缺少相邻交易日的合格收盘反馈，暂不判断改善或走弱。",
    "intraday_not_comparable": "盘中反馈不与收盘反馈判断改善或走弱。",
    "close_feedback_required": "两次反馈尚未均完成收盘确认。",
    "complete_coverage_required": "两次反馈的有效行情覆盖尚未完整。",
    "comparable_metrics_missing": "缺少可比的中位数或正收益比例。",
    "rule_version_mismatch": "两次反馈规则版本不同，不能比较。",
    "feedback_rule_mismatch": "两次反馈规则版本不同，不能比较。",
    "fetch_pool_error": "当日涨停池采集失败，无法补成完整名单。",
    "fetch_quote_error": "部分昨日样本行情采集失败，仍保留为缺失。",
    "fetch_timeout": "反馈取数超时，缺失样本仍保留。",
    "current_cohort_not_freezable": "当日名单尚不满足收盘日期、完整覆盖或采集时间要求。",
    "existing_cohort_invalid": "已有冻结名单未通过完整性校验，保留原文件并暂停使用。",
    "market_context_stale": "市场评分证据滞后；反馈单独采集。",
    "non_trading_session": "反馈日不是权威日历确认的交易日。",
    "provider_error": "提供方请求失败",
    "timeout": "提供方请求超时",
    "missing_target_session": "提供方未返回目标交易日",
    "invalid_quote": "行情字段未通过资格校验",
    "unsupported_market": "行情源不支持该市场",
    "budget_exhausted": "取数预算已耗尽",
    "not_started": "预算截止前未发起请求",
    "quote_evidence_missing": "有样本未取得合格的当日行情证据。",
    "quote_evidence_ineligible": "行情日期、来源口径或收盘证据不足",
    "collection_crossed_session_day": "采集跨过自然日，未将结果写入当日反馈绑定。",
    "current_close_unavailable": "本次收盘反馈未能取得可验证的持久化基线，暂停跨日比较。",
    "invalid_native_quote": "原生行情结构未通过核验",
    "identity_mismatch": "行情证券身份与请求不符",
    "source_date_mismatch": "来源交易日与反馈日不符",
    "future_source_time": "来源时刻晚于采集时刻",
    "native_pct_inconsistent": "原生涨跌幅与报价参考值不一致",
    "close_not_confirmed": "来源尚未确认收盘行情",
    "unknown": "失败原因未分类",
    "primary_provider_failed_but_recovered": "主行情源失败后已由合格备用源恢复。",
}


def _reason(value):
    return REASONS.get(str(value), str(value))


def _number(value, suffix="", signed=False):
    if isinstance(value, bool):
        return "未提供"
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "未提供"
    if not math.isfinite(value):
        return "未提供"
    return (f"{value:+.2f}" if signed else f"{value:.2f}") + suffix


def _cell(value, fmt):
    value = str(value if value is not None else "未提供")
    if fmt == "html":
        return html.escape(value)
    value = value.replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()<>|#!])", r"\\\1", value)


def _attempt_summary(attempt):
    """Render a bounded, non-technical summary from frozen provider evidence."""
    if not isinstance(attempt, dict):
        return ""
    parts = []
    provider = str(attempt.get("provider") or attempt.get("source") or "").strip()
    if provider:
        parts.append(provider)
    status = str(attempt.get("status") or "").strip().lower()
    reason = attempt.get("reason") or attempt.get("reason_code")
    if status in ("success", "ok"):
        parts.append("成功")
    elif reason:
        parts.append(_reason(reason))
    elif status:
        parts.append(status)
    latest = attempt.get("latest_data_date") or attempt.get("latest_date")
    if latest:
        parts.append(f"最新交易日 {latest}")
    return "，".join(parts[:3])


def _row_diagnostic(row):
    attempts = row.get("provider_attempts")
    attempt_text = "；".join(
        text for text in (_attempt_summary(item) for item in attempts or []) if text
    )
    source = str(row.get("quote_source") or "").strip()
    source_time = row.get("source_time_raw")
    fetched_at = row.get("quote_fetched_at")
    parts = []
    if source:
        parts.append(f"最终来源 {source}")
    if source_time:
        parts.append(f"来源时刻 {source_time}")
    if fetched_at:
        parts.append(f"采集于 {fetched_at}")
    if row.get("status") == "missing":
        reason = row.get("reason") or row.get("reason_code")
        if reason:
            parts.append(_reason(reason))
        elif not attempt_text:
            parts.append("旧记录未保存详细原因")
    if attempt_text:
        parts.append("尝试：" + attempt_text)
    return "；".join(parts) or "未提供"


def _diagnostic_reason(value):
    if isinstance(value, dict):
        reason = value.get("reason") or value.get("reason_code")
        stage = value.get("stage")
        text = _reason(reason or "unknown")
        return f"{stage}：{text}" if stage else text
    return _reason(value)


def render_market_feedback(state, output_format):
    """Both formats read only the already frozen analysis result."""
    if output_format not in ("html", "markdown"):
        raise ValueError("unsupported output format")
    state = state or {}
    fmt = output_format
    summary = state.get("summary") or {}
    mode = state.get("mode")
    clock = ("盘中反馈，截至 " + str(state.get("as_of") or "未提供") if mode == "intraday"
             else "收盘反馈，采集截止 " + str(state.get("as_of") or "未提供") if mode == "close" else "反馈证据不足")
    feedback_date = state.get("feedback_basis_date") or state.get("basis_date")
    market_date = state.get("market_basis_date")
    texts = [
        f"名单日：{state.get('cohort_date') or state.get('snapshot_date') or '未提供'}；反馈日：{feedback_date or '未提供'}；{clock}。",
        "固定使用昨日冻结全体样本，今日掉队、下跌、停牌和缺价仍保留。日涨跌幅是行情反馈，不代表可实现交易收益。",
    ]
    if market_date or state.get("feedback_basis_date"):
        texts.append(
            f"市场评分依据日：{market_date or '未提供'}；反馈依据日：{feedback_date or '未提供'}。"
        )
    if market_date and feedback_date and market_date != feedback_date:
        texts.append(_reason("market_context_stale"))
    reason = state.get("reason")
    if reason:
        rendered = _reason(reason)
        if rendered not in texts:
            texts.append(rendered)
    reasons = state.get("reasons") or []
    if reasons:
        details = []
        for item in reasons:
            rendered = _reason(item)
            if item != reason and rendered not in texts and rendered not in details:
                details.append(rendered)
        if details:
            texts.append("数据说明：" + "；".join(details))
    if summary:
        total = summary.get("total", 0)
        available = summary.get("available", 0)
        coverage = summary.get("coverage")
        positive = summary.get("positive_ratio")
        negative = summary.get("negative_ratio")
        texts.extend([
            f"冻结 {total} 只，有效行情 {available} 只，覆盖率 {_number(coverage * 100 if isinstance(coverage, (int, float)) else None, '%')}；上涨 {summary.get('up', 0)}、下跌 {summary.get('down', 0)}、平盘 {summary.get('flat', 0)}、停牌 {summary.get('suspended', 0)}、缺失 {summary.get('missing', 0)}。",
            f"有效样本：涨跌幅中位数 {_number(summary.get('median_pct_chg'), '%', True)}，均值 {_number(summary.get('mean_pct_chg'), '%', True)}，正收益比例 {_number(positive * 100 if isinstance(positive, (int, float)) else None, '%')}，负收益比例 {_number(negative * 100 if isinstance(negative, (int, float)) else None, '%')}，最差涨跌幅 {_number(summary.get('worst_pct_chg'), '%', True)}。",
        ])
        if total and available == 0 and summary.get("missing") == total:
            texts.append("全部样本行情缺失；未用零涨跌幅填补，汇总收益指标保持未提供。")
    diagnostics = state.get("diagnostics") or {}
    errors = diagnostics.get("errors") or []
    if errors:
        texts.append("失败概览：" + "；".join(
            dict.fromkeys(_diagnostic_reason(item) for item in errors)
        ))
    advisories = diagnostics.get("advisories") or []
    if advisories:
        texts.append("辅助说明：" + "；".join(_reason(item) for item in advisories))
    comparison = state.get("comparison") or {}
    if comparison:
        if comparison.get("status") == "comparable":
            texts.append("相邻收盘反馈变化：中位数 " + _number(comparison.get("median_pct_chg_change"), "个百分点", True)
                         + "；正收益比例 " + _number(comparison.get("positive_ratio_change_pp"), "个百分点", True) + "。")
        else:
            texts.append("反馈变化：" + _reason(comparison.get("reason") or "相邻合格收盘反馈不足，暂不判断改善或走弱。"))
    freeze = state.get("cohort_freeze") or {}
    if freeze:
        if freeze.get("status") in ("created", "unchanged"):
            texts.append("当日名单：" + ("已冻结，供下一交易日追踪。" if freeze["status"] == "created" else "沿用首次冻结记录，供下一交易日追踪。"))
        else:
            texts.append("当日名单：" + _reason(freeze.get("reason") or freeze.get("status")))
    texts.append("核心口径：连板高度核心仅指昨日涨停池中最高连板数且至少二板的股票；成交、趋势核心待独立证据确认。")
    headers = ["股票", "昨日角色", "昨日连板", "今日涨跌幅", "今日反馈", "行情诊断"]
    rows = []
    names = {"limit_up": "涨停", "height_core": "连板高度核心", "core": "核心观察"}
    for role, group in (state.get("groups") or {}).items():
        if not group.get("total"):
            continue
        texts.append(f"{names.get(role, role)}组：冻结 {group['total']} 只，有效 {group.get('available', 0)} 只，"
                     f"上涨 {group.get('up', 0)}、下跌 {group.get('down', 0)}，"
                     f"涨跌幅中位数 {_number(group.get('median_pct_chg'), '%', True)}。")
    statuses = {"up": "上涨", "down": "下跌", "flat": "平盘", "missing": "行情缺失", "suspended": "停牌"}
    for row in state.get("items") or []:
        code = str(row.get("code") or "")
        label = _cell(f"{row.get('name') or ''} {code}", fmt)
        match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", code)
        if match:
            url = f"https://quote.eastmoney.com/{match[2].lower()}{match[1]}.html"
            label = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>' if fmt == "html" else f"[{label}]({url})"
        rows.append([label, _cell("、".join(names.get(role, role) for role in row.get("roles") or []) or "未提供", fmt),
                     _cell(row.get("streak"), fmt), _number(row.get("pct_chg"), "%", True),
                     _cell(statuses.get(row.get("status"), row.get("status") or "行情缺失"), fmt),
                     _cell(_row_diagnostic(row), fmt)])
    if fmt == "markdown":
        lines = ['<a id="market-feedback"></a>', "### 昨日强势股今日反馈", ""]
        lines.extend(_cell(text, fmt) + "\n" for text in texts)
        if rows:
            lines.extend(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"])
            lines.extend("| " + " | ".join(row) + " |" for row in rows)
        return "\n".join(lines)
    table = ""
    if rows:
        table = '<div class="summary-table-wrap"><table><thead><tr>' + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows) + "</tbody></table></div>"
    return '<section id="market-feedback"><h2>昨日强势股今日反馈</h2>' + "".join("<p>" + _cell(text, fmt) + "</p>" for text in texts) + table + "</section>"
