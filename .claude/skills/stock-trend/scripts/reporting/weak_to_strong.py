"""Report an independent daily weak-to-strong observation."""

import math
from html import escape

from reporting.sector_display import _sector_html


DISPLAY_LIMIT = 30


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
    provisional = (item.get("provisional") is True or
                   item.get("confirmation") == "provisional")
    if provisional and item.get("state") != "PROVISIONAL_BREAKOUT":
        signal += "（盘中临时，未收盘）"
    if item.get("state") == "PULLBACK" and item.get("two_day_decline") is True:
        signal += "；连续两日收盘下跌"
    checks = item.get("checks") or {}
    support = checks.get("ma10_support") if isinstance(checks, dict) else None
    support_evidence = support.get("evidence") if isinstance(support, dict) else None
    if isinstance(support_evidence, dict):
        margin = support_evidence.get("margin_ratio")
        margin_text = f"{margin * 100:+.2f}%" if isinstance(margin, (int, float)) and not isinstance(margin, bool) and math.isfinite(margin) else "—"
        signal += (f"；MA10支撑依据 {_value(support_evidence.get('date'))}"
                   f"，收盘 {_value(support_evidence.get('close'))}"
                   f"，MA10 {_value(support_evidence.get('ma10'))}"
                   f"，支撑阈值 {_value(support_evidence.get('support_price'))}"
                   f"，余量 {margin_text}")
    evidence = {"ready": "数据合格", "data_insufficient": "数据不足"}.get(
        item.get("evidence_status"), _value(item.get("evidence_status")))
    kline_evidence = item.get("kline_evidence") or {}
    source = kline_evidence.get("provider")
    fetched_at = kline_evidence.get("fetched_at")
    evidence_parts = [f"{_value(item.get('as_of_date'))} / {evidence}"]
    if item.get("evidence_status") == "data_insufficient":
        evidence_parts.append("原因 " + "、".join(str(x) for x in item.get("reason_codes") or ["未记录"]))
    if source:
        evidence_parts.append("来源 " + str(source))
    if fetched_at:
        evidence_parts.append("获取 " + str(fetched_at))
    return [f"{item.get('name', '')} ({item.get('code', '')})", sectors,
            signal,
            _value(item.get("adjustment_days")),
            f"{_value(item.get('H'))} / {_value(item.get('L'))}",
            _value(item.get("va_v0_ratio")), _value(item.get("breakout_volume_ratio")),
            _value(item.get("close_strength")),
            "；".join(evidence_parts), status]


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
    provisional = summary.get("intraday_provisional", 0)
    close_unconfirmed = summary.get("close_unconfirmed", 0)
    details = []
    if provisional:
        details.append(f"其中盘中临时 {provisional} 只")
    if close_unconfirmed:
        details.append(f"收盘未确认 {close_unconfirmed} 只")
    suffix = "；" + "，".join(details) if details else ""
    if not scope.get("scope_complete") or insufficient or scope.get("collector_status") != "ready":
        return f"覆盖不足：已评估 {evaluated} 只，数据不足 {insufficient} 只{suffix}；详见诊断。"
    return f"本次已评估 {evaluated} 只{suffix}。"


def _follow_up(payload):
    value = payload.get("follow_up") or {}
    if isinstance(value, list):
        return {"rows": value}
    return value if isinstance(value, dict) else {}


def _diagnostic_detail(item):
    reasons = item.get("reason_codes") or ["无"]
    detail = f"{item.get('code', '')}：{item.get('state', '未知')}；" + "、".join(
        FOLLOW_UP_REASONS.get(str(reason), str(reason)) for reason in reasons)
    evidence = item.get("kline_evidence") or {}
    provider = evidence.get("provider")
    fetched_at = evidence.get("fetched_at")
    if provider:
        detail += "；来源 " + str(provider)
    if fetched_at:
        detail += "；获取 " + str(fetched_at)
    return detail


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
        badge = _sector_html({"sector_name": str(name),
                              "sector_hot_score": member.get("hot_score"),
                              "sector_score": member.get("sector_score")},
                             unknown_heat_plain=True)
        rendered.append("<div class='weak-sector'>" + badge + "</div>")
    return "".join(rendered) or "未记录"


MERGED_HEADERS = HEADERS[:3] + ["跟踪状态 / 原信号日 / 判断依据"] + HEADERS[3:]
DISPLAY_HEADERS = ["名次 / 入选依据"] + MERGED_HEADERS


def _merged_rows(payload):
    """Join only records with an explicitly matching security and cycle."""
    main = _rows(payload)
    follows = _follow_up_rows(payload)
    used = set()
    rows = []
    for item in main:
        cycle = str(item.get("cycle_id") or "")
        match = next((i for i, follow in enumerate(follows)
                      if i not in used and cycle and _code(item) == _code(follow)
                      and cycle == str(follow.get("origin_cycle_id") or "")), None)
        follow = follows[match] if match is not None else None
        if match is not None:
            used.add(match)
        rows.append((item, follow))
    rows.extend((None, follow) for i, follow in enumerate(follows) if i not in used)

    def key(pair):
        item, follow = pair
        status = _follow_up_status(follow or {}) if follow else ""
        state = (item or {}).get("state", "")
        if status in ("INVALIDATED", "失效"):
            priority = 0
        elif status in ("PENDING_REVIEW", "待复评"):
            priority = 1
        elif ((item or {}).get("provisional") is True or
              (item or {}).get("confirmation") == "provisional" or
              state == "PROVISIONAL_BREAKOUT"):
            priority = 5
        elif state == "BREAKOUT_CONFIRMED" or status in ("ENHANCED", "STRENGTHENED", "增强"):
            priority = 2
        elif state == "SUPPLY_DRY" or status in ("CONTINUED", "延续"):
            priority = 3
        else:
            priority = 4
        group = {"BREAKOUT_CONFIRMED": "confirmed", "SUPPLY_DRY": "watching",
                 "PULLBACK": "pullback", "PROVISIONAL_BREAKOUT": "provisional"}.get(state)
        # Existing order is deterministic within each signal group.
        if item and group:
            detail = (0, _main_sort_key(group, item))
        else:
            detail = (1, _follow_up_sort_key(follow or {}))
        return (priority, detail, _code(item or follow or {}))
    return sorted(rows, key=key)


def _item_metrics(item):
    metrics = item.get("metrics") or {}
    return metrics if isinstance(metrics, dict) else {}


def _v1_trend_tier(item):
    """Prefer established upward MA direction when v1 happens to carry it."""
    metrics = _item_metrics(item)
    ma10 = _number(metrics.get("ma10_slope_3", metrics.get("ma10_slope_3d",
                                                           item.get("ma10_slope_3"))))
    ma20 = _number(metrics.get("ma20_slope_3", metrics.get("ma20_slope_3d",
                                                           item.get("ma20_slope_3"))))
    if ma10 is None or ma20 is None:
        direction = str(metrics.get("ma_direction") or item.get("ma_direction") or "").lower()
        return {"both_up": 0, "ma20_up": 1}.get(direction, 3 if not direction else 2)
    tolerance = _number(metrics.get("ma_flat_tolerance"))
    threshold = 0.002 if tolerance is None else tolerance
    return 0 if ma10 > threshold and ma20 > threshold else 1 if ma20 > threshold else 2


def _risk_tier(item):
    risks = item.get("risk_flags") or []
    if isinstance(risks, str):
        risks = [risks]
    material = {"extended", "weak_volume", "lost_level", "parent_overhead", "flat_bar"}
    return (any(str(risk) in material for risk in risks),
            sum(str(risk) in material for risk in risks))


def _v1_quality_key(item):
    state = str(item.get("state") or "")
    if state == "BREAKOUT_CONFIRMED":
        return (_descending(item.get("close_strength")),
                _descending(item.get("breakout_volume_ratio")),
                _ascending(item.get("breakout_ratio")))
    if state == "SUPPLY_DRY":
        return (_ascending(item.get("va_v0_ratio")),
                _ascending(item.get("drawdown_ratio", item.get("drawdown"))),
                _ascending(item.get("adjustment_days")))
    return (item.get("two_day_decline") is True,
            _ascending(item.get("drawdown_ratio", item.get("drawdown"))),
            _ascending(item.get("adjustment_days")))


def _v1_display_key(pair):
    item, follow = pair
    status = _follow_up_status(follow or {}) if follow else ""
    state = str(item.get("state") or "")
    if state == "BREAKOUT_CONFIRMED" or status in {"ENHANCED", "STRENGTHENED", "增强"}:
        stage = 0
    elif state == "SUPPLY_DRY" or status in {"CONTINUED", "延续"}:
        stage = 1
    else:
        stage = 2
    return (stage, _v1_trend_tier(item), _risk_tier(item),
            _v1_quality_key(item), _code(item), str(item.get("cycle_id") or ""))


def _selection_stats(scanned):
    return {"scanned_rows": scanned, "eligible_securities": 0, "displayed": 0,
            "not_displayed": scanned, "omitted_by_limit": 0,
            "excluded_data": 0, "excluded_provisional": 0,
            "excluded_status": 0, "excluded_duplicate": 0,
            "excluded_lost_level": 0, "excluded_stale_date": 0,
            "date_validation_unknown": 0}


def _has_current_date(payload, item, stats):
    expected = str(payload.get("as_of_date") or "")
    actual = str(item.get("as_of_date") or item.get("data_date") or "")
    if not expected:
        stats["date_validation_unknown"] = 1
        return True
    if actual != expected:
        stats["excluded_stale_date"] += 1
        return False
    return True


def select_v1_rows(payload, limit=DISPLAY_LIMIT):
    """Select the investment-review main table without mutating source evidence."""
    raw = _merged_rows(payload)
    stats = _selection_stats(len(raw))
    candidates = []
    for item, follow in raw:
        if item is None or item.get("evidence_status") != "ready":
            stats["excluded_data"] += 1
            continue
        if not _has_current_date(payload, item, stats):
            continue
        state = str(item.get("state") or "")
        if (item.get("provisional") is True or item.get("confirmation") == "provisional"
                or state == "PROVISIONAL_BREAKOUT"):
            stats["excluded_provisional"] += 1
            continue
        status = _follow_up_status(follow or {}) if follow else ""
        if status in {"INVALIDATED", "PENDING_REVIEW", "失效", "待复评"}:
            stats["excluded_status"] += 1
            continue
        risks = item.get("risk_flags") or []
        if isinstance(risks, str):
            risks = [risks]
        if "lost_level" in risks:
            stats["excluded_lost_level"] += 1
            continue
        if state not in {"BREAKOUT_CONFIRMED", "SUPPLY_DRY", "PULLBACK"}:
            stats["excluded_status"] += 1
            continue
        candidates.append((item, follow))

    selected = []
    seen = set()
    for pair in sorted(candidates, key=_v1_display_key):
        code = _code(pair[0])
        if not code or code in seen:
            stats["excluded_duplicate"] += 1
            continue
        seen.add(code)
        selected.append(pair)
    stats["eligible_securities"] = len(selected)
    cap = max(0, int(limit)) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else DISPLAY_LIMIT
    stats["omitted_by_limit"] = max(0, len(selected) - cap)
    selected = selected[:cap]
    stats["displayed"] = len(selected)
    stats["not_displayed"] = max(0, stats["scanned_rows"] - stats["displayed"])
    return selected, stats


def _merged_cells(item, follow):
    if item:
        cells = _cells(item)
    else:
        fc = _follow_up_cells(follow)
        cells = [fc[0], "", "—", "—", f"{_value(follow.get('H'))} / {_value(follow.get('L'))}", "—", "—", "—",
                 f"{fc[6]} / {fc[7]}", "未记录"]
    if follow:
        count = follow.get("tracking_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            cells[0] += "（跟踪 " + str(count) + " 次）"
        fc = _follow_up_cells(follow)
        detail = f"{fc[3]} → {fc[4]}；原信号日 {fc[2]}；{fc[5]}；数据 {fc[6]} / {fc[7]}"
        if not item:
            detail += "；周期 " + _value(follow.get("origin_cycle_id"))
    else:
        detail = "—"
    return cells[:3] + [detail] + cells[3:]


def _selection_summary(stats):
    text = (f"主表筛选：扫描 {stats['scanned_rows']} 行，有效证券 "
            f"{stats['eligible_securities']} 只，展示 {stats['displayed']} 只，"
            f"未展示 {stats['not_displayed']} 行。")
    if stats.get("date_validation_unknown"):
        text += " 报告依据日缺失，无法验证记录是否为当日数据。"
    return text


def _selection_exclusions(stats):
    labels = (("excluded_data", "数据不足或仅有历史跟踪"),
              ("excluded_provisional", "盘中临时"),
              ("excluded_status", "失效、待复评或状态不支持"),
              ("excluded_stale_date", "日期缺失或与报告依据日不一致"),
              ("excluded_lost_level", "跌回触发位"),
              ("excluded_duplicate", "同证券重复记录"),
              ("omitted_by_limit", "超过30只展示上限"))
    parts = [f"{label} {stats.get(key, 0)}" for key, label in labels if stats.get(key, 0)]
    return "；".join(parts)


def _v1_selection_reason(item):
    stage = {"BREAKOUT_CONFIRMED": "收盘确认", "SUPPLY_DRY": "缩量待突破",
             "PULLBACK": "结构有效回调"}.get(str(item.get("state") or ""), "有效形态")
    trend = {0: "MA10/MA20向上", 1: "MA20向上", 2: "均线未形成上行",
             3: "均线方向未记录"}[_v1_trend_tier(item)]
    return stage + "；" + trend


def build_v1_display_selection(payload, limit=DISPLAY_LIMIT):
    """Return the JSON companion for the v1 Markdown/HTML main table."""
    rows, stats = select_v1_rows(payload, limit=limit)
    return {
        "schema_version": "weak-to-strong-display-selection/v1",
        "limit": limit,
        "stats": stats,
        "items": [{"rank": rank, "code": _code(item),
                   "cycle_id": str(item.get("cycle_id") or ""),
                   "reason": _v1_selection_reason(item)}
                  for rank, (item, _follow) in enumerate(rows, 1)],
    }


def _report_preamble(payload, stats):
    parts = ["独立日线形态观察；收盘确认仅代表形态成立，正式推荐资格见状态与限制。",
             "排序：收盘确认或增强、缩量待突破或延续、结构有效回调；同阶段按均线方向、风险和形态质量排列。",
             _selection_summary(stats), _summary(payload)]
    summary = _follow_up_summary(payload)
    if summary:
        parts.append(summary)
    follow = _follow_up(payload)
    if follow and not _follow_up_rows(payload):
        message = follow.get("message") or follow.get("reason") or "没有需要持续跟踪的上一交易日信号。"
        parts.append(FOLLOW_UP_REASONS.get(message, message))
    return parts


def render_markdown(payload):
    if payload is None:
        return ""
    rows, stats = select_v1_rows(payload)
    lines = ["## 日线弱转强", ""]
    for part in _report_preamble(payload, stats):
        lines += [part, ""]
    if rows:
        lines += ["| " + " | ".join(DISPLAY_HEADERS) + " |", "| " + " | ".join(["---"] * len(DISPLAY_HEADERS)) + " |"]
        for rank, (item, follow) in enumerate(rows, 1):
            cells = [f"{rank} / {_v1_selection_reason(item)}"] + _merged_cells(item, follow)
            source = item or follow
            memberships = source.get("sector_memberships") or []
            cells[2] = "、".join(str(m.get("name") or m.get("sector_name") or m.get("code") or "") for m in memberships if isinstance(m, dict)) or "未记录"
            lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
    else:
        lines.append("无满足条件的形态；覆盖不足时不代表完整扫描无信号。")
    diagnostics = payload.get("diagnostics") or []
    exclusions = _selection_exclusions(stats)
    if diagnostics or exclusions:
        lines += ["", "弱转强诊断："]
        if exclusions:
            lines.append("- 主表未展示：" + exclusions)
        lines.extend("- " + _diagnostic_detail(item) for item in diagnostics)
    return "\n".join(lines)


def render_html(payload):
    if payload is None:
        return ""
    rows, stats = select_v1_rows(payload)
    out = ["<section class='candidate-section'><h2>日线弱转强</h2>"]
    out.extend("<p>" + escape(part) + "</p>" for part in _report_preamble(payload, stats))
    if rows:
        out.append("<div class='candidate-table-wrap'><table class='weak-to-strong-table'><thead><tr>" +
                   "".join("<th>" + escape(x) + "</th>" for x in DISPLAY_HEADERS) + "</tr></thead><tbody>")
        for rank, (item, follow) in enumerate(rows, 1):
            cells = [f"{rank} / {_v1_selection_reason(item)}"] + _merged_cells(item, follow)
            out.append("<tr>" + "".join("<td>" + (_memberships_html(item or follow) if i == 2 else escape(value)) + "</td>" for i, value in enumerate(cells)) + "</tr>")
        out.append("</tbody></table></div>")
    else:
        out.append("<p>无满足条件的形态；覆盖不足时不代表完整扫描无信号。</p>")
    diagnostics = payload.get("diagnostics") or []
    exclusions = _selection_exclusions(stats)
    if diagnostics or exclusions:
        out.append("<details><summary>弱转强诊断</summary><ul>")
        if exclusions:
            out.append("<li>主表未展示：" + escape(exclusions) + "</li>")
        out.extend("<li>" + escape(_diagnostic_detail(item)) + "</li>" for item in diagnostics)
        out.append("</ul></details>")
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
V2_DISPLAY_HEADERS = ["名次 / 入选依据"] + V2_HEADERS


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


def select_v2_rows(payload, limit=DISPLAY_LIMIT):
    """Select at most *limit* unique, current, close-qualified v2 securities."""
    groups = {key: [] for key, _label in V2_GROUPS if key != "provisional"}
    scanned = sum(len(payload.get(key) or []) for key, _label in V2_GROUPS
                  if isinstance(payload.get(key) or [], list))
    stats = _selection_stats(scanned)
    eligible = []
    for key, _label in V2_GROUPS:
        rows = payload.get(key) or []
        if not isinstance(rows, list):
            continue
        for item in sort_v2_rows(rows):
            if item.get("evidence_status") != "ready":
                stats["excluded_data"] += 1
                continue
            if not _has_current_date(payload, item, stats):
                continue
            if (key == "provisional" or item.get("provisional") is True
                    or item.get("confirmation") == "provisional"):
                stats["excluded_provisional"] += 1
                continue
            risks = item.get("risk_flags") or []
            if isinstance(risks, str):
                risks = [risks]
            if "lost_level" in risks:
                stats["excluded_lost_level"] += 1
                continue
            eligible.append((key, item))

    unique = []
    seen = set()
    for key, item in eligible:
        code = _code(item)
        if not code or code in seen:
            stats["excluded_duplicate"] += 1
            continue
        seen.add(code)
        unique.append((key, item))
    stats["eligible_securities"] = len(unique)
    cap = max(0, int(limit)) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else DISPLAY_LIMIT
    stats["omitted_by_limit"] = max(0, len(unique) - cap)
    for key, item in unique[:cap]:
        groups[key].append(item)
    stats["displayed"] = sum(len(rows) for rows in groups.values())
    stats["not_displayed"] = max(0, stats["scanned_rows"] - stats["displayed"])
    return groups, stats


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


def _v2_selection_reason(item):
    stage = V2_STAGE_LABELS.get(str(item.get("stage")), _value(item.get("stage")))
    trend = {0: "MA10/MA20向上", 1: "MA20向上", 2: "均线未形成上行",
             3: "均线方向未记录"}[_v2_trend_tier(item)]
    return stage + "；" + trend


def build_v2_display_selection(payload, limit=DISPLAY_LIMIT):
    """Return the JSON companion for the v2 Markdown/HTML main table."""
    groups, stats = select_v2_rows(payload, limit=limit)
    items = []
    rank = 0
    for key, _label in V2_GROUPS:
        for item in groups.get(key, []):
            rank += 1
            items.append({"rank": rank, "group": key, "code": _code(item),
                          "cycle_id": str(item.get("cycle_id") or ""),
                          "reason": _v2_selection_reason(item)})
    return {"schema_version": "weak-to-strong-display-selection/v1",
            "limit": limit, "stats": stats, "items": items}


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
    selected, stats = select_v2_rows(payload)
    lines = ["## 日线弱转强 v2（研究版）", "",
             "按转强阶段分组；结构识别不等同于正式推荐资格。", "",
             _selection_summary(stats), "", _v2_summary(payload)]
    any_rows = False
    rank = 0
    for key, label in V2_GROUPS:
        rows = selected.get(key, [])
        if not rows:
            continue
        any_rows = True
        lines += ["", f"### {label}", "", "| " + " | ".join(V2_DISPLAY_HEADERS) + " |",
                  "| " + " | ".join(["---"] * len(V2_DISPLAY_HEADERS)) + " |"]
        for item in rows:
            rank += 1
            cells = [f"{rank} / {_v2_selection_reason(item)}"] + _v2_cells(item)
            lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
    if not any_rows:
        lines += ["", "当前没有可展示的 v2 研究形态。"]
    diagnostics = payload.get("diagnostics") or []
    exclusions = _selection_exclusions(stats)
    if (isinstance(diagnostics, list) and diagnostics) or exclusions:
        lines += ["", "<details><summary>v2 诊断</summary>", ""]
        if exclusions:
            lines.append("- 主表未展示：" + _markdown_cell(exclusions))
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
    selected, stats = select_v2_rows(payload)
    out = ["<section class='candidate-section weak-to-strong-v2'>",
           "<h2>日线弱转强 v2（研究版）</h2>",
           "<p>按转强阶段分组；结构识别不等同于正式推荐资格。</p>",
           "<p>" + escape(_selection_summary(stats)) + "</p>",
           "<p>" + escape(_v2_summary(payload)) + "</p>"]
    any_rows = False
    rank = 0
    for key, label in V2_GROUPS:
        rows = selected.get(key, [])
        if not rows:
            continue
        any_rows = True
        out.append("<h3>" + escape(label) + "</h3>")
        out.append("<div class='candidate-table-wrap'><table class='weak-to-strong-table weak-to-strong-v2-table'>"
                   "<thead><tr>" + "".join("<th>" + escape(header) + "</th>" for header in V2_DISPLAY_HEADERS) +
                   "</tr></thead><tbody>")
        for item in rows:
            rank += 1
            cells = [f"{rank} / {_v2_selection_reason(item)}"] + _v2_cells(item)
            out.append("<tr>" + "".join("<td>" + escape(str(cell)) + "</td>"
                                         for cell in cells) + "</tr>")
        out.append("</tbody></table></div>")
    if not any_rows:
        out.append("<p>当前没有可展示的 v2 研究形态。</p>")
    diagnostics = payload.get("diagnostics") or []
    exclusions = _selection_exclusions(stats)
    if (isinstance(diagnostics, list) and diagnostics) or exclusions:
        out.append("<details><summary>v2 诊断</summary><ul>")
        if exclusions:
            out.append("<li>主表未展示：" + escape(exclusions) + "</li>")
        for item in diagnostics:
            if isinstance(item, dict):
                detail = _code(item) + "：" + "、".join(str(reason) for reason in
                                                        (item.get("reason_codes") or ["无"]))
                out.append("<li>" + escape(detail) + "</li>")
        out.append("</ul></details>")
    return "".join(out) + "</section>"
