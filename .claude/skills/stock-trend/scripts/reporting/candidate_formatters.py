"""Side-effect-free formatters for daily candidate reports."""

from html import escape


SIGNAL_LABELS = {
    "volume_breakout": "放量突破",
    "northbound_adding": "北向增持",
}


def _signal_text(signals, *, labels=None):
    """Render signals dict to Chinese-readable string (bools → labels)."""
    labels = SIGNAL_LABELS if labels is None else labels
    parts = []
    for key, value in signals.items():
        if key in labels:
            parts.append(labels[key])
        elif isinstance(value, bool):
            parts.append(key)
        else:
            parts.append(str(value))
    return "、".join(parts) or "-"


def _phase_d_lps_display_value(value, unknown="未知"):
    return unknown if value is None or value == "" else str(value)


def _phase_d_lps_evidence_text(row, *, display_value=None):
    """Share identical zone and volume evidence text between MD and HTML."""
    display_value = _phase_d_lps_display_value if display_value is None else display_value
    zone = row["candidate_zone"]
    zone_text = (
        f"{row['pullback_state']}；{zone['label']} "
        f"{display_value(zone.get('lower_bound'))}–"
        f"{display_value(zone.get('upper_bound'), '上沿未知')}"
        f"（{zone.get('basis_date') or '日期未知'}；{zone['status']}）"
    )
    supply = row["supply_evidence"]
    supply_text = (
        f"BU日 {display_value(supply.get('bu_event_date'))}"
        f" 低/高/收/量 {display_value(supply.get('bu_low'))}/"
        f"{display_value(supply.get('bu_high'))}/"
        f"{display_value(supply.get('bu_close'))}/"
        f"{display_value(supply.get('bu_volume'))}；"
        f"SOS量/ATR {display_value(supply.get('sos_volume'))}/"
        f"{display_value(supply.get('sos_atr'))}；"
        f"原箱顶/LPS回踩幅度 "
        f"{display_value(supply.get('tr_resistance'))}/"
        f"{display_value(supply.get('lps_pullback_spread'))}；"
        f"5/10/TR均量 {display_value(supply.get('lps_volume_avg5'))}/"
        f"{display_value(supply.get('lps_volume_avg10'))}/"
        f"{display_value(supply.get('lps_volume_tr_median'))}；"
        f"量比 SOS/5/10/TR "
        f"{display_value(supply.get('volume_vs_sos_ratio'))}/"
        f"{display_value(supply.get('volume_vs_avg5_ratio'))}/"
        f"{display_value(supply.get('volume_vs_avg10_ratio'))}/"
        f"{display_value(supply.get('volume_vs_tr_median_ratio'))}"
    )
    return zone_text, supply_text


def _phase_d_lps_shadow_markdown(shadow, *, evidence_text=None):
    evidence_text = _phase_d_lps_evidence_text if evidence_text is None else evidence_text
    if not shadow:
        return []
    pool = shadow.get("scan_pool") or {}
    pool_hash = str(pool.get("codes_sha256") or "")[:12] or "未知"
    lines = [
        "", "## Phase D/LPS 观察（影子观察，不参与推荐）", "",
        f"> 评价日 {shadow.get('as_of') or '未知'}；Phase2扫描池 {pool.get('code_count', 0)} 只"
        f"（{pool.get('scope_status', 'unknown')}，代码哈希 {pool_hash}）；"
        f"有效 Phase D {shadow['phase_d_count']} 只；SOS→LPS {shadow['sos_lps_count']} 只；"
        f"失效 {shadow.get('invalidated_count', 0)} 只；范围外 {shadow.get('out_of_scope_count', 0)} 只；"
        f"证据不足 {shadow.get('insufficient_evidence_count', 0)} 只；展示 {shadow['shown_count']} 只；"
        f"增强证据未完整 {shadow['evidence_incomplete_count']} 只。",
    ]
    if not shadow["items"]:
        return lines + ["> 本轮没有同一箱体内已确认 SOS 的观察对象。"]
    lines.extend([
        "",
        "| 名称(代码) | 状态 | 状态说明 | SOS（事件/确认） | LPS（事件/确认） | 回踩状态/候选价区 | 原箱体（支撑/上沿） | BU量价与量能比率 | 健康状态/原因码 | 结构失效位 | 正式门控原因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in shadow["items"]:
        lps = row["lps"] or {}
        zone_text, supply_text = evidence_text(row)
        lines.append(
            f"| {row['name']}({row['code']}) | {row['state']}"
            f"{'；增强未完成: ' + ','.join(row['incomplete_sources']) if row['evidence_incomplete'] else ''} | "
            f"{row['reason']} | "
            f"{row['sos']['event_date'] or '未知'} / {row['sos']['confirmation_date'] or '未知'} | "
            f"{lps.get('event_date') or '等待回踩'} / {lps.get('confirmation_date') or '未知'} | "
            f"{zone_text} | "
            f"{row['range']['support'] if row['range']['support'] is not None else '未知'} / "
            f"{row['range']['resistance'] if row['range']['resistance'] is not None else '未知'} | "
            f"{supply_text} | {row['event_health']['state']} / {row['event_health']['reason_code'] or '无'} | "
            f"{row['event_health']['structural_floor'] if row['event_health']['structural_floor'] is not None else '未知'} | "
            f"{'; '.join(row['formal_blockers']) or '无（仍为影子观察）'} |"
        )
    return lines


def _phase_d_lps_shadow_html(shadow, *, evidence_text=None):
    evidence_text = _phase_d_lps_evidence_text if evidence_text is None else evidence_text
    if not shadow:
        return ""
    pool = shadow.get("scan_pool") or {}
    pool_hash = str(pool.get("codes_sha256") or "")[:12] or "未知"
    rows = []
    for row in shadow["items"]:
        lps = row["lps"] or {}
        zone_text, supply_text = evidence_text(row)
        rows.append(
            "<tr>"
            f"<td>{escape(str(row['name']))}({escape(str(row['code']))})</td>"
            f"<td>{escape(str(row['state']))}{'；增强未完成: ' + ','.join(row['incomplete_sources']) if row['evidence_incomplete'] else ''}</td>"
            f"<td>{escape(str(row['reason']))}</td>"
            f"<td>{escape(str(row['sos']['event_date'] or '未知'))} / {escape(str(row['sos']['confirmation_date'] or '未知'))}</td>"
            f"<td>{escape(str(lps.get('event_date') or '等待回踩'))} / {escape(str(lps.get('confirmation_date') or '未知'))}</td>"
            f"<td>{escape(zone_text)}</td>"
            f"<td>{escape(str(row['range']['support'] if row['range']['support'] is not None else '未知'))} / "
            f"{escape(str(row['range']['resistance'] if row['range']['resistance'] is not None else '未知'))}</td>"
            f"<td>{escape(supply_text)}</td>"
            f"<td>{escape(str(row['event_health']['state']))} / {escape(str(row['event_health']['reason_code'] or '无'))}</td>"
            f"<td>{escape(str(row['event_health']['structural_floor'] if row['event_health']['structural_floor'] is not None else '未知'))}</td>"
            f"<td>{escape('；'.join(row['formal_blockers']) or '无（仍为影子观察）')}</td>"
            "</tr>"
        )
    content = ("<p class='dt'>本区块独立于正式分桶：评价日 "
               f"{escape(str(shadow.get('as_of') or '未知'))}；Phase2扫描池 "
               f"{pool.get('code_count', 0)}（{escape(str(pool.get('scope_status', 'unknown')))}，"
               f"代码哈希 {escape(pool_hash)}）；Phase D {shadow['phase_d_count']}，"
               f"SOS→LPS {shadow['sos_lps_count']}，展示 {shadow['shown_count']}，"
               f"失效 {shadow.get('invalidated_count', 0)}，范围外 {shadow.get('out_of_scope_count', 0)}，"
               f"证据不足 {shadow.get('insufficient_evidence_count', 0)}，"
               f"增强证据未完整 {shadow['evidence_incomplete_count']}。</p>")
    if rows:
        content += ("<div class='phase-d-lps-table-wrap'><table class='phase-d-lps-table'>"
                    "<thead><tr><th>标的</th><th>状态</th><th>状态说明</th><th>SOS</th><th>LPS</th>"
                    "<th>回踩状态/候选价区</th><th>原箱体</th><th>BU量价与量能比率</th>"
                    "<th>健康状态/原因码</th><th>结构失效位</th><th>正式门控原因</th>"
                    "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    else:
        content += "<p class='dt'>本轮没有同一箱体内已确认 SOS 的观察对象。</p>"
    return ("<details class='secondary-panel'><summary>Phase D/LPS 观察 · 影子观察，不参与推荐"
            "</summary><div class='details-content'>" + content + "</div></details>")
