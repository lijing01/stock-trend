"""Shared sector evidence presentation for candidate reports."""

from html import escape


def _sector_persistence_text(item):
    """Explain history coverage separately from hot-sector appearances."""
    values = (
        item.get("history_window_days"),
        item.get("history_coverage_days"),
        item.get("hot_appearance_days"),
        item.get("hot_streak"),
    )
    if all(value is None for value in values):
        return ""
    coverage = item.get("history_coverage_days")
    window = item.get("history_window_days")
    appearances = item.get("hot_appearance_days")
    if appearances is None:
        appearances = item.get("persistence_days", 0)
    streak = item.get("hot_streak", 0)
    if coverage is None:
        coverage = 0
    if window is None:
        window = coverage
    status = (
        item.get("sector_persistence_status")
        or item.get("persistence_status")
        or item.get("sector_type", "")
    )
    history_unknown = status == "history_unknown"
    status_label = {
        "history_insufficient": "历史不足",
        "history_unknown": "本板块历史未匹配，持续性未知",
        "single_day_pulse": "单日脉冲",
        "verified": "已验证",
        "mainline": "主线",
        "emerging": "新兴",
    }.get(status, status or "未标记")
    appearance_text = "—" if history_unknown else f"{appearances}/{window}"
    streak_text = "—" if history_unknown else f"{streak}"
    return (
        f"持续性：快照覆盖 {coverage}/{window}｜"
        f"热点出现 {appearance_text}｜连续 {streak_text} 日｜{status_label}"
    )


def _sector_text(item):
    text = item.get("sector_name", "")
    persistence = _sector_persistence_text(item)
    provenance = []
    for label, prefix in (("排行", "ranking"), ("成分", "membership")):
        source = item.get(f"{prefix}_source", "")
        if not source:
            continue
        data_date = item.get(f"{prefix}_data_date", "") or "未知"
        quality = item.get(f"{prefix}_quality", "") or "未知"
        provenance.append(
            f"{label} 来源 {source}｜日期 {data_date}｜质量 {quality}"
        )
    details = ([persistence] if persistence else []) + provenance
    if details:
        return f"{text}（{'；'.join(details)}）"
    return text


def _sector_html(item, unknown_heat_plain=False):
    """Render the sector name as a high-contrast heat-level badge."""
    text = _sector_text(item)
    if not text:
        return ""

    sector_name = str(item.get("sector_name", "")).strip()
    safe_text = escape(text)
    safe_name = escape(sector_name)
    if not safe_name or safe_name not in safe_text:
        return safe_text

    if unknown_heat_plain and item.get("sector_hot_score") is None and item.get("sector_score") is None:
        return safe_text

    try:
        heat_score = float(item.get("sector_hot_score") or
                           item.get("sector_score") or 0)
    except (TypeError, ValueError):
        heat_score = 0
    if heat_score >= 80:
        heat_class = "sector-hot"
    elif heat_score >= 60:
        heat_class = "sector-warm"
    elif heat_score >= 40:
        heat_class = "sector-neutral"
    else:
        heat_class = "sector-cool"

    badge = f"<span class='sector-tag {heat_class}'>{safe_name}</span>"
    return safe_text.replace(safe_name, badge, 1)
