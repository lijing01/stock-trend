#!/usr/bin/env python3
"""Report generator for stock-trend skill.

Renders Markdown and HTML reports from analysis data using simple template engine.
The script only handles formatting; scoring and direction are determined by the agent.

Template syntax:
  - {{variable}}: simple variable substitution
  - {{#section}}...{{/section}}: conditional block (renders if section key is truthy)
  - {{^section}}...{{/section}}: inverse conditional (renders if section key is falsy)

Usage:
    python3 generate_report.py \\
        --technical /tmp/technical.json \\
        --kline /tmp/kline.json \\
        --scores '{"technical":-1,"capital_flow":-0.5,...}' \\
        --direction '震荡' --score -0.08 --confidence '低' \\
        --risks '["布林带极度收口","RSI顶背离"]' \\
        --output-md reports/159740.SZ/20260514-2200.md \\
        --output-html reports/159740.SZ/20260514-2200.html
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ASSETS_DIR = SCRIPT_DIR.parent / "assets"


def render_template(template_str, context):
    """Simple template engine: {{variable}}, {{#section}}...{{/section}}, {{^section}}...{{/section}}."""
    result = template_str

    # Process conditional blocks first: {{#key}}...{{/key}}
    import re
    for m in re.finditer(r'\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}', result, re.DOTALL):
        key = m.group(1)
        body = m.group(2)
        value = context.get(key)
        if value:
            # If value is a list, repeat the body for each item
            if isinstance(value, list):
                items = []
                for item in value:
                    if isinstance(item, dict):
                        items.append(render_template(body, item).strip())
                    else:
                        items.append(render_template(body, {key + "_item": item}).strip())
                replacement = "\n".join(items)
            else:
                replacement = render_template(body, context)
        else:
            replacement = ""
        result = result.replace(m.group(0), replacement, 1)

    # Process inverse conditional blocks: {{^key}}...{{/key}}
    for m in re.finditer(r'\{\{\^(\w+)\}\}(.*?)\{\{/\1\}\}', result, re.DOTALL):
        key = m.group(1)
        body = m.group(2)
        value = context.get(key)
        if not value:
            replacement = render_template(body, context)
        else:
            replacement = ""
        result = result.replace(m.group(0), replacement, 1)

    # Process simple variable substitution: {{key}}
    for m in re.finditer(r'\{\{(\w+)\}\}', result):
        key = m.group(1)
        value = context.get(key, "")
        result = result.replace(m.group(0), str(value) if value is not None else "", 1)

    return result


def score_css(score_val):
    """Map score to CSS class."""
    if score_val is None:
        return "sz"
    if score_val > 0:
        return "sp" if score_val >= 1.5 else "sz"
    elif score_val < 0:
        return "sn" if score_val <= -1.5 else "sz"
    return "sz"


def direction_css(direction):
    """Map direction to CSS class."""
    if "多" in direction or "bull" in direction.lower():
        return "bull"
    elif "空" in direction or "bear" in direction.lower():
        return "bear"
    return "neut"


def direction_symbol(direction):
    """Map direction to trend symbol."""
    if "多" in direction or "bull" in direction.lower():
        return "▲"
    elif "空" in direction or "bear" in direction.lower():
        return "▼"
    return "◆"


def _safe_float(value):
    if value in (None, "", "—"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_numeric(values):
    for value in values or []:
        num = _safe_float(value)
        if num is not None:
            return num
    return None


def _nearest_support(values, close):
    numeric_values = [num for num in (_safe_float(value) for value in values or []) if num is not None]
    if not numeric_values:
        return None

    close_num = _safe_float(close)
    if close_num is None:
        return numeric_values[0]

    below_or_at_close = [value for value in numeric_values if value <= close_num]
    if below_or_at_close:
        return max(below_or_at_close)
    return min(numeric_values, key=lambda value: abs(value - close_num))


def _fmt_price(value):
    num = _safe_float(value)
    return "—" if num is None else f"{num:.2f}"


def _build_time_window(events):
    if not events:
        return "未来 5-10 个交易日有效；若始终未触发则重新评估。"
    first = events[0]
    label = first.get("date") or first.get("name") or "关键事件"
    return f"未来 5-10 个交易日有效；若 {label} 前仍未触发则该计划失效，需重新评估。"


def _load_scores_file(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: failed to load scores file {path}: {exc}", file=sys.stderr)
        return {}


def _hydrate_args_from_scores_file(args):
    scores_data = getattr(args, "_scores_data", None)
    if scores_data is None:
        scores_data = _load_scores_file(getattr(args, "scores_file", None))
        setattr(args, "_scores_data", scores_data)

    report_params = scores_data.get("report_params", {}) if scores_data else {}
    if scores_data and not getattr(args, "analysis", None):
        analysis = scores_data.get("analysis")
        if analysis:
            args.analysis = json.dumps(analysis, ensure_ascii=False)
    if not getattr(args, "entry_verdict", None):
        args.entry_verdict = report_params.get("entry_verdict", "wait")
    if not getattr(args, "entry_signals", None):
        args.entry_signals = json.dumps(report_params.get("entry_signals", []), ensure_ascii=False)
    return scores_data


def build_action_plan(direction, confidence, latest_close, report_params, analysis_data):
    """Derive actionable report context from scoring/report parameters."""
    report_params = report_params or {}
    analysis_data = analysis_data or {}

    support_levels = report_params.get("support_levels", [])
    resistance_levels = report_params.get("resistance_levels", [])
    close = _safe_float(latest_close)
    support = _nearest_support(support_levels, close)
    resistance = _first_numeric(resistance_levels)
    stop_loss = _safe_float(report_params.get("stop_loss"))
    tp1 = _safe_float(report_params.get("target_conservative"))
    tp2 = _safe_float(report_params.get("target_moderate") or report_params.get("target"))
    rr_ratio = _safe_float(report_params.get("risk_reward_ratio") or report_params.get("rr_moderate"))

    direction_text = direction or ""
    is_bearish = "看空" in direction_text or "bear" in direction_text.lower()
    low_confidence = confidence in ("低", "low")
    near_tp1 = close is not None and tp1 is not None and close >= tp1 * 0.98
    near_support = close is not None and support is not None and abs(close - support) / support <= 0.015
    incomplete_decision_levels = any(
        value is None for value in (support, resistance, stop_loss, tp1, tp2, rr_ratio)
    )

    if is_bearish or low_confidence or incomplete_decision_levels or rr_ratio < 1.5:
        action_label = "只观察"
    elif near_tp1:
        action_label = "分批止盈"
    elif near_support:
        action_label = "可低吸"
    else:
        action_label = "等回踩"

    support_text = _fmt_price(support)
    resistance_text = _fmt_price(resistance)
    stop_text = _fmt_price(stop_loss)
    tp1_text = _fmt_price(tp1)
    tp2_text = _fmt_price(tp2)

    if action_label == "分批止盈":
        summary = f"当前价接近第一目标 {tp1_text}，优先分批止盈并抬高保护位。"
        detail = "不再追高加仓，保留底仓观察能否放量突破下一目标。"
    elif action_label == "只观察":
        summary = "当前风险收益比或关键价位条件不足，先观察不主动开仓。"
        detail = "等待支撑、止损、目标与量能条件重新匹配后再制定入场计划。"
    elif action_label == "可低吸":
        summary = f"当前价接近支撑 {support_text}，可按计划分批低吸。"
        detail = f"以 {stop_text} 作为硬止损，单次仓位不宜过重。"
    else:
        summary = f"趋势偏多但距离支撑 {support_text} 仍有空间，等待回踩确认。"
        detail = "避免在压力位下方追高，优先等待缩量回踩或放量突破后的二次确认。"

    scenario_b_action = "继续观察，等待关键价位补全。"
    if support is not None and stop_loss is not None:
        scenario_b_action = f"回踩 {support_text} 附近且不破 {stop_text} 时分批试仓；跌破止损则放弃。"

    events = analysis_data.get("events", [])
    if action_label == "只观察":
        if incomplete_decision_levels:
            scenario_a_condition = "压力位或目标价缺失，暂不设置突破买入条件。"
            scenario_a_action = "只观察量价变化，等待压力位、目标价与风险收益比补全。"
            scenario_b_condition = "关键决策价位不完整，暂不设置低吸条件。"
            scenario_b_action = "不主动试仓，等待支撑、止损、目标与风险收益比重新匹配。"
            exit_condition_1 = "关键止损或目标价位不完整"
            exit_action_1 = "不新增风险敞口，等待关键价位补全后再评估。"
            exit_condition_2 = "目标价位补全前出现冲高"
            exit_action_2 = "不追高，等待完整计划后再评估。"
        else:
            scenario_a_condition = f"放量突破或站稳压力位 {resistance_text}"
            scenario_a_action = "只观察突破后的量价持续性，等待置信度或风险收益比改善后再评估。"
            scenario_b_condition = f"回踩支撑位 {support_text} 附近并出现止跌信号"
            scenario_b_action = "继续观察支撑有效性，等待更明确的确认信号后再制定入场计划。"
            exit_condition_1 = f"有效跌破止损位 {stop_text}"
            exit_action_1 = "不新增风险敞口，等待止跌确认后重新评估。"
            exit_condition_2 = f"冲高接近第一目标 {tp1_text}"
            exit_action_2 = "只观察目标位附近承接与量能，不按目标价执行交易。"
        return {
            "操作计划": True,
            "今日动作标签": action_label,
            "今日动作摘要": summary,
            "今日动作说明": detail,
            "场景A标题": "场景 A：继续上冲",
            "场景A条件": scenario_a_condition,
            "场景A动作": scenario_a_action,
            "场景B标题": "场景 B：回调到位",
            "场景B条件": scenario_b_condition,
            "场景B动作": scenario_b_action,
            "退出条件1": exit_condition_1,
            "退出动作1": exit_action_1,
            "退出条件2": exit_condition_2,
            "退出动作2": exit_action_2,
            "退出条件3": "事件前仍未触发入场条件或量能持续背离",
            "退出动作3": "继续观察事件落地后的趋势与风险收益比变化。",
            "执行时间窗": _build_time_window(events),
        }

    return {
        "操作计划": True,
        "今日动作标签": action_label,
        "今日动作摘要": summary,
        "今日动作说明": detail,
        "场景A标题": "场景 A：继续上冲",
        "场景A条件": f"放量突破或站稳压力位 {resistance_text}",
        "场景A动作": f"不追高满仓；若接近 {tp1_text} 先分批止盈，突破后再看 {tp2_text}。",
        "场景B标题": "场景 B：回调到位",
        "场景B条件": f"回踩支撑位 {support_text} 附近并出现止跌信号",
        "场景B动作": scenario_b_action,
        "退出条件1": f"有效跌破止损位 {stop_text}",
        "退出动作1": "立即止损离场，避免亏损扩大。",
        "退出条件2": f"冲高接近第一目标 {tp1_text}",
        "退出动作2": "分批止盈，剩余仓位跟踪趋势。",
        "退出条件3": "事件前仍未触发入场条件或量能持续背离",
        "退出动作3": "取消原计划，重新评估趋势与风险收益比。",
        "执行时间窗": _build_time_window(events),
    }


def build_context(args):
    """Build template context from CLI arguments and data files."""
    # Load technical analysis data
    technical = {}
    if args.technical:
        try:
            with open(args.technical, "r", encoding="utf-8") as f:
                technical = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Load kline data for metadata
    kline = {}
    if args.kline:
        try:
            with open(args.kline, "r", encoding="utf-8") as f:
                kline = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Load ETF data
    etf_data = {}
    if args.etf_data:
        try:
            with open(args.etf_data, "r", encoding="utf-8") as f:
                etf_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Load capital flow data
    capital_flow = {}
    if args.capital_flow:
        try:
            with open(args.capital_flow, "r", encoding="utf-8") as f:
                capital_flow = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Load chip distribution data
    chip_dist = {}
    if args.chip_distribution:
        try:
            with open(args.chip_distribution, "r", encoding="utf-8") as f:
                chip_dist = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Parse scores JSON
    scores = {}
    if args.scores:
        try:
            scores = json.loads(args.scores)
        except json.JSONDecodeError:
            pass

    scores_data = _hydrate_args_from_scores_file(args)
    report_params = scores_data.get("report_params", {}) if scores_data else {}

    # Parse risks JSON
    risks = []
    if args.risks:
        try:
            risks = json.loads(args.risks)
        except json.JSONDecodeError:
            risks = [args.risks]

    # Parse special JSON
    special = {}
    if args.special:
        try:
            special = json.loads(args.special)
        except json.JSONDecodeError:
            pass

    # Extract data
    summary = technical.get("summary", {})
    meta = technical.get("meta", {})
    kline_meta = kline.get("meta", {})
    patterns = technical.get("patterns", [])
    merged_report_params = {**summary, **report_params}

    # Latest close price
    latest_close = technical.get("latest", {}).get("close", None)

    # Parse entry signals JSON
    entry_signals_list = []
    if args.entry_signals:
        if isinstance(args.entry_signals, list):
            entry_signals_list = args.entry_signals
        else:
            try:
                entry_signals_list = json.loads(args.entry_signals)
            except (json.JSONDecodeError, TypeError):
                entry_signals_list = [args.entry_signals] if isinstance(args.entry_signals, str) else []

    # Build context
    ts_code = args.ts_code or meta.get("ts_code", kline_meta.get("ts_code", "unknown"))
    stock_name = args.stock_name or etf_data.get("fund_name", ts_code)
    analysis_date = args.date or datetime.now().strftime("%Y-%m-%d")

    # Technical scores
    tech_score = scores.get("technical", summary.get("total_score", 0))
    capital_score = scores.get("capital_flow", 0)
    fund_score = scores.get("fundamental", 0)
    sent_score = scores.get("sentiment", 0)
    macro_score = scores.get("macro", 0)

    # Direction and confidence
    direction = args.direction or summary.get("direction", "neutral")
    composite_score = args.score if args.score is not None else summary.get("total_score", 0)
    confidence = args.confidence or summary.get("confidence", "低")

    # Key signals from technical
    tech_signals = summary.get("key_signals", [])
    tech_summary = "；".join(tech_signals[:3]) if tech_signals else "—"

    # Pattern summary
    pattern_summary = "；".join(f"{p['name']}({p['direction']})" for p in patterns[:3]) if patterns else ""

    # Risk/reward
    stop_loss = report_params.get("stop_loss", summary.get("stop_loss", "—"))
    target_conservative = report_params.get("target_conservative", summary.get("target_conservative"))
    target_moderate = report_params.get("target_moderate", summary.get("target_moderate") or summary.get("target", "—"))
    target_aggressive = report_params.get("target_aggressive", summary.get("target_aggressive"))
    target = report_params.get("target", summary.get("target", "—"))
    rr_ratio = report_params.get("risk_reward_ratio", summary.get("risk_reward_ratio", "—"))
    favorable_rr = report_params.get("favorable_rr", summary.get("favorable_rr"))
    position_sizing = report_params.get("position_sizing", summary.get("position_sizing", "—"))
    max_drawdown = report_params.get("max_drawdown_pct", summary.get("max_drawdown_pct", "—"))

    # Format target with three tiers if available
    target_display = str(target_moderate)
    if target_conservative and target_aggressive:
        target_display = f"{target_conservative}/{target_moderate}/{target_aggressive}"

    # Support/resistance levels
    support_levels = report_params.get("support_levels", summary.get("support_levels", []))
    resistance_levels = report_params.get("resistance_levels", summary.get("resistance_levels", []))
    support_str = " / ".join(str(s) for s in support_levels[:3]) if support_levels else "—"
    resistance_str = " / ".join(str(r) for r in resistance_levels[:3]) if resistance_levels else "—"

    # R:R warning
    rr_warning = summary.get("risk_reward_warning", "")

    # K-line data info
    data_source = kline_meta.get("data_source", "")
    data_points = kline_meta.get("record_count") or kline_meta.get("data_points") or meta.get("data_points", 0)
    kline_start = kline_meta.get("start_date", "")
    kline_end = kline_meta.get("end_date", "")
    # If meta doesn't have date range, compute from data array
    if not kline_start or not kline_end:
        kline_data_array = kline.get("data", [])
        if kline_data_array:
            dates = []
            for r in kline_data_array:
                d = r.get("date") or r.get("trade_date") or r.get("datetime", "")
                if d:
                    dates.append(str(d))
            if dates:
                kline_start = min(dates)
                kline_end = max(dates)
    kline_range = f"{kline_start}~{kline_end}" if kline_start and kline_end else f"{data_points}条"

    # Risk/reward display
    rr_display = str(rr_ratio)
    if favorable_rr is True:
        rr_display += " ✓"
    elif favorable_rr is False:
        rr_display += " ✗"

    # Three-tier R:R
    rr_conservative = report_params.get("rr_conservative", summary.get("rr_conservative"))
    rr_moderate = report_params.get("rr_moderate", summary.get("rr_moderate")) or rr_ratio
    rr_aggressive = report_params.get("rr_aggressive", summary.get("rr_aggressive"))

    def fmt_rr(val):
        if val is None:
            return "—"
        return str(val)

    # Monitor signals (use partial matching to handle "震荡偏多", "震荡偏空" etc.)
    monitor_signals = []
    dir_lower = direction.lower()
    is_bullish = any(kw in dir_lower for kw in ("看多", "bullish")) and not any(kw in dir_lower for kw in ("看空", "bearish"))
    is_bearish = any(kw in dir_lower for kw in ("看空", "bearish"))
    is_neutral = any(kw in dir_lower for kw in ("震荡", "neutral", "sideways")) or (not is_bullish and not is_bearish)

    if is_bullish and support_levels:
        monitor_signals = [
            ("跌破止损位", "立即止损离场"),
            (f"跌破支撑位{support_levels[0]}", "减半仓位，重新评估"),
            (f"突破目标价{target_moderate}", "可分批止盈"),
        ]
    elif is_bearish and resistance_levels:
        monitor_signals = [
            ("突破止损位", "立即离场"),
            (f"突破压力位{resistance_levels[0]}", "重新评估方向"),
            ("继续下跌创新低", "持有观望"),
        ]
    elif is_neutral:
        monitor_signals = [
            (f"突破压力位{resistance_levels[0] if resistance_levels else '—'}", "突破方向追入，需量价确认"),
            (f"跌破支撑位{support_levels[0] if support_levels else '—'}", "减仓观望"),
            ("持续窄幅震荡", "耐心等待方向选择"),
        ]

    # Special section for ETF/ST/HK
    special_section = None
    if special:
        special_section = special
    elif etf_data:
        nav_info = etf_data.get("nav", {})
        iopv_premium = nav_info.get("iopv_premium_pct")
        content_parts = []

        # Fund name
        fund_name = etf_data.get("fund_name", "")
        if fund_name:
            content_parts.append(f"基金名称: {fund_name}")

        # IOPV premium/discount
        if iopv_premium is not None:
            direction = "溢价" if iopv_premium > 0 else "折价"
            content_parts.append(f"IOPV折溢价率: {iopv_premium:+.2f}%（{direction}）")

        # Latest NAV
        nav_val = nav_info.get("nav")
        if nav_val:
            content_parts.append(f"最新净值: {nav_val}")

        # Fund size
        fund_size = etf_data.get("fund_size", {})
        shares = fund_size.get("shares_billion")
        net_asset = fund_size.get("net_asset_billion")
        if shares is not None or net_asset is not None:
            size_parts = []
            if shares is not None:
                size_parts.append(f"{shares:.2f}亿份")
            if net_asset is not None:
                size_parts.append(f"{net_asset:.2f}亿元")
            if size_parts:
                content_parts.append(f"基金规模: {'/'.join(size_parts)}")

        # Returns track record
        returns = etf_data.get("returns", {})
        if returns:
            ret_parts = []
            period_labels = {"1m": "近1月", "3m": "近3月", "6m": "近6月", "1y": "近1年"}
            for key, label in period_labels.items():
                val = returns.get(key)
                if val is not None:
                    ret_parts.append(f"{label}: {val:+.2f}%")
            if ret_parts:
                content_parts.append("收益率: " + " | ".join(ret_parts))

        # Top holdings
        holdings = etf_data.get("top_holdings", [])
        if holdings:
            holding_strs = [f"{h['name']}({h.get('weight', '?')}%)" for h in holdings[:5]]
            content_parts.append("前十大持仓: " + "、".join(holding_strs))

        # Subscription/redemption flows
        recent_flows = etf_data.get("recent_flows", [])
        if recent_flows:
            first_shares = recent_flows[0].get("shares_billion", 0)
            last_shares = recent_flows[-1].get("shares_billion", 0)
            if first_shares and last_shares:
                change = last_shares - first_shares
                content_parts.append(f"近{len(recent_flows)}日申赎: 份额变动{change:+.2f}亿份")

        special_section = {
            "type": "etf",
            "title": "ETF 特殊分析",
            "content": "\n\n".join(content_parts) if content_parts else "ETF数据暂无",
        }

    context = {
        "股票名称": stock_name,
        "代码": ts_code,
        "日期": analysis_date,
        "周期": args.horizon or "日线",
        "侧重": args.focus or "",
        "趋势方向": direction,
        "趋势符号": direction_symbol(direction),
        "趋势CSS": direction_css(direction),
        "综合评分": composite_score,
        "置信度": confidence,
        # Dimension scores
        "技术面得分": tech_score,
        "资金面得分": capital_score,
        "基本面得分": fund_score,
        "情绪面得分": sent_score,
        "宏观面得分": macro_score,
        # CSS classes
        "技术面CSS": score_css(tech_score if isinstance(tech_score, (int, float)) else 0),
        "资金面CSS": score_css(capital_score if isinstance(capital_score, (int, float)) else 0),
        "基本面CSS": score_css(fund_score if isinstance(fund_score, (int, float)) else 0),
        "情绪面CSS": score_css(sent_score if isinstance(sent_score, (int, float)) else 0),
        "宏观面CSS": score_css(macro_score if isinstance(macro_score, (int, float)) else 0),
        # Summaries
        "技术面摘要": tech_summary,
        "资金面摘要": args.capital_summary or "—",
        "基本面摘要": args.fundamental_summary or "—",
        "情绪面摘要": args.sentiment_summary or "—",
        "宏观面摘要": args.macro_summary or "—",
        # Risk/reward
        "支撑位": support_str,
        "压力位": resistance_str,
        "当前价": latest_close if latest_close is not None else "—",
        "止损位": stop_loss,
        "目标位": target_display,
        "风险收益比": rr_display,
        "保守目标价": target_conservative or "—",
        "主目标价": target_moderate or "—",
        "激进目标价": target_aggressive or "—",
        "保守RR": fmt_rr(rr_conservative),
        "主目标RR": fmt_rr(rr_moderate),
        "激进RR": fmt_rr(rr_aggressive),
        "仓位建议": position_sizing,
        "最大回撤": f"{max_drawdown}%" if max_drawdown != "—" and max_drawdown is not None else "—",
        # Risks
        "风险列表": [{"风险项": r} for r in risks] if risks else None,
        # Monitor signals
        "监控信号": len(monitor_signals) > 0,
        "监控条件1": monitor_signals[0][0] if len(monitor_signals) > 0 else "",
        "监控动作1": monitor_signals[0][1] if len(monitor_signals) > 0 else "",
        "监控条件2": monitor_signals[1][0] if len(monitor_signals) > 1 else "",
        "监控动作2": monitor_signals[1][1] if len(monitor_signals) > 1 else "",
        "监控条件3": monitor_signals[2][0] if len(monitor_signals) > 2 else "",
        "监控动作3": monitor_signals[2][1] if len(monitor_signals) > 2 else "",
        # K-line patterns
        "kline_patterns": len(patterns) > 0,
        "kline_pattern_list": [{"形态": p["name"], "方向": p.get("direction", ""), "位置": p.get("position", ""), "得分": p.get("score", 0)} for p in patterns],
        "kline_data_source": data_source if data_source and data_source != "error" else "",
        "kline_data_range": kline_range,
        "kline_data_count": data_points,
        "kline_summary": pattern_summary,
        # Special section
        "特殊标记": special_section is not None,
        "特殊标记标题": special_section.get("title", "") if special_section else "",
        "特殊标记内容": special_section.get("content", "") if special_section else "",
        # Data quality warning
        "数据质量警告": summary.get("risk_reward_warning") or ("⚠️ 数据不足，分析可靠性有限" if meta.get("data_points", 999) < 60 else ""),
        # Chart and data source annotations
        "has_chart": args.chart is not None and os.path.exists(args.chart),
        "tech_data_source": data_source if data_source else "Tushare/东方财富",
        "capital_data_source": capital_flow.get("meta", {}).get("data_source", "东方财富"),
        # Entry timing
        "入场时机": args.entry_verdict != "wait",
        "entry_verdict": args.entry_verdict,
        "entry_verdict_text": {"ready": "可入场", "watch": "等待确认", "wait": "暂观望", "avoid": "不建议入场"}.get(args.entry_verdict, ""),
        "entry_signals": entry_signals_list,
        "entry_signals_text": " + ".join(entry_signals_list) if entry_signals_list else "",
        "entry_signal_count": len(entry_signals_list),
        # Chip distribution
        "chip_distribution": chip_dist and "error" not in chip_dist,
        "chip_avg_cost": chip_dist.get("avg_cost", "—") if chip_dist else "",
        "chip_current_price": chip_dist.get("current_price", "—") if chip_dist else "",
        "chip_profit_ratio": f"{chip_dist['profit_ratio']:.1%}" if chip_dist and chip_dist.get("profit_ratio") is not None else "",
        "chip_concentration": f"{chip_dist['concentration']:.1%}" if chip_dist and chip_dist.get("concentration") is not None else "",
        "chip_high_volume_nodes": [
            {"node_price": n["price"], "node_vol_ratio": f"{n['vol_ratio']:.1%}"}
            for n in chip_dist.get("high_volume_nodes", [])
        ] if chip_dist else [],
    }

    # Load fundamental data flag
    has_fund = False
    if args.fundamental_data:
        try:
            with open(args.fundamental_data, "r", encoding="utf-8") as f:
                fd = json.load(f)
            has_fund = fd.get("summary", {}).get("data_quality") in ("good", "partial")
        except Exception:
            pass
    context["has_fundamental_data"] = has_fund

    # Load macro data flag
    has_macro = False
    if args.macro_data:
        try:
            with open(args.macro_data, "r", encoding="utf-8") as f:
                md = json.load(f)
            has_macro = md.get("summary", {}).get("data_quality") in ("good", "partial")
        except Exception:
            pass
    context["has_macro_data"] = has_macro

    # Load futures data
    futures_data = {}
    if args.futures_data:
        try:
            with open(args.futures_data, "r", encoding="utf-8") as f:
                futures_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    if futures_data:
        basis_pct = futures_data.get("basis_pct")
        direction = "升水" if basis_pct is not None and basis_pct > 0 else "贴水"
        context["futures_basis"] = f"{basis_pct:+.2f}%({direction})" if basis_pct is not None else ""
        _signals = futures_data.get("signals") or {}
        context["futures_signal"] = _signals.get("composite_signal", "")
        context["has_futures_data"] = True
    else:
        context["futures_basis"] = ""
        context["futures_signal"] = ""
        context["has_futures_data"] = False

    # Comprehensive analysis (综合研判 section) from args.analysis JSON
    analysis_data = None
    if args.analysis:
        try:
            analysis_data = json.loads(args.analysis)
        except (json.JSONDecodeError, TypeError):
            pass

    if analysis_data:
        events = analysis_data.get("events", [])
        advice = analysis_data.get("advice", [])
        context["综合研判"] = True
        context["核心矛盾"] = analysis_data.get("core_conflict", "")
        context["关键事件"] = len(events) > 0
        context["事件列表"] = [
            {
                "日期": e.get("date") or e.get("name") or "",
                "事件": e.get("event") or e.get("detail") or "",
                "影响": e.get("impact", ""),
            }
            for e in events
        ]
        context["操作建议列表"] = [{"内容": a} for a in advice if a]
    else:
        context["综合研判"] = False
        context["核心矛盾"] = ""
        context["关键事件"] = False
        context["事件列表"] = []
        context["操作建议列表"] = []

    context.update(build_action_plan(direction, confidence, latest_close, merged_report_params, analysis_data))

    # Validation warnings from scores file
    validation_warnings = scores_data.get("validation_warnings", []) if scores_data else []
    context["校验警告"] = len(validation_warnings) > 0
    context["校验警告列表"] = [{"警告项": w} for w in validation_warnings] if validation_warnings else []

    return context


def main():
    parser = argparse.ArgumentParser(description="Generate stock-trend report")
    parser.add_argument("--technical", help="Path to technical analysis JSON")
    parser.add_argument("--kline", help="Path to kline data JSON")
    parser.add_argument("--etf-data", help="Path to ETF data JSON")
    parser.add_argument("--capital-flow", help="Path to capital flow JSON")
    parser.add_argument("--scores", help="JSON string with dimension scores")
    parser.add_argument("--scores-file", help="Path to compute_scores.py output JSON (overrides --scores/--direction/--score/--confidence/--risks/--special)")
    parser.add_argument("--pipeline", help="Path to run_pipeline.py output JSON (auto-fills --kline/--technical/--etf-data/--capital-flow)")
    parser.add_argument("--direction", help="Trend direction: 看多/看空/震荡")
    parser.add_argument("--score", type=float, help="Composite score")
    parser.add_argument("--confidence", help="Confidence level: 高/中/低")
    parser.add_argument("--risks", help="JSON array of risk strings")
    parser.add_argument("--special", help="JSON object with type/title/content for special section")
    parser.add_argument("--ts-code", help="Stock code (overrides data file)")
    parser.add_argument("--stock-name", help="Stock name (overrides data file)")
    parser.add_argument("--date", help="Analysis date (default: today)")
    parser.add_argument("--horizon", default="日线", help="Analysis horizon")
    parser.add_argument("--focus", help="Focus dimension")
    # Summary texts for non-technical dimensions
    parser.add_argument("--capital-summary", help="Capital flow dimension summary")
    parser.add_argument("--fundamental-summary", help="Fundamental dimension summary")
    parser.add_argument("--sentiment-summary", help="Sentiment dimension summary")
    parser.add_argument("--macro-summary", help="Macro dimension summary")
    # Comprehensive analysis for 综合研判 section
    parser.add_argument("--entry-verdict", help="Entry timing: ready/watch/wait/avoid")
    parser.add_argument("--entry-signals", help="JSON array of entry confirmation signal strings")
    parser.add_argument("--analysis", help="JSON object with core_conflict, events, advice for 综合研判 section")
    # Chart and new data files
    parser.add_argument("--chart", help="Path to chart HTML fragment to embed")
    parser.add_argument("--fundamental-data", help="Path to fundamental data JSON")
    parser.add_argument("--macro-data", help="Path to macro snapshot JSON")
    parser.add_argument("--futures-data", help="Path to futures data JSON (ETF only)")
    parser.add_argument("--chip-distribution", help="Path to chip distribution JSON")
    # Output paths
    parser.add_argument("--output-md", help="Output Markdown file path")
    parser.add_argument("--output-html", help="Output HTML file path")
    parser.add_argument("--code", help="Stock/ETF code to locate data directory")
    parser.add_argument("--data-dir", help="Data directory path (default: .cache/stock-trend/{code}/)")

    args = parser.parse_args()

    # Resolve data directory from --code
    if args.code:
        from cache_utils import CACHE_DIR
        data_dir = Path(args.data_dir) if args.data_dir else Path(CACHE_DIR) / args.code

        # Auto-fill paths from data directory
        if not args.pipeline:
            pipeline_path = data_dir / "pipeline_output.json"
            if pipeline_path.exists():
                args.pipeline = str(pipeline_path)

        if not args.scores_file:
            scores_path = data_dir / "scores.json"
            if scores_path.exists():
                args.scores_file = str(scores_path)

        if not args.chart:
            chart_path = data_dir / "chart_fragment.html"
            if chart_path.exists():
                args.chart = str(chart_path)

        if not args.futures_data:
            futures_path = data_dir / "futures_data.json"
            if futures_path.exists():
                args.futures_data = str(futures_path)

    if not args.output_md and not args.output_html:
        print("Error: at least one of --output-md or --output-html is required", file=sys.stderr)
        sys.exit(1)

    # Load pipeline output to auto-fill data file paths
    if args.pipeline:
        try:
            with open(args.pipeline, "r", encoding="utf-8") as f:
                pipeline = json.load(f)
            output_files = pipeline.get("output_files", {})
            if not args.technical and output_files.get("technical"):
                args.technical = output_files["technical"]
            if not args.kline and output_files.get("kline"):
                args.kline = output_files["kline"]
            if not args.etf_data and output_files.get("etf_data"):
                args.etf_data = output_files["etf_data"]
            if not args.capital_flow and output_files.get("capital_flow"):
                args.capital_flow = output_files["capital_flow"]
            if not args.fundamental_data and output_files.get("fundamental"):
                args.fundamental_data = output_files["fundamental"]
            if not args.chip_distribution and output_files.get("chip_distribution"):
                args.chip_distribution = output_files["chip_distribution"]
            if not args.macro_data and output_files.get("macro_snapshot"):
                args.macro_data = output_files["macro_snapshot"]
        except (OSError, json.JSONDecodeError):
            pass

    # Load scores file to auto-fill scoring parameters
    if args.scores_file:
        try:
            with open(args.scores_file, "r", encoding="utf-8") as f:
                scores_data = json.load(f)
            setattr(args, "_scores_data", scores_data)
            # Override individual parameters from scores file
            if not args.scores:
                args.scores = json.dumps(scores_data.get("scores", {}))
            if not args.direction:
                args.direction = scores_data.get("direction", "")
            if args.score is None:
                args.score = scores_data.get("composite_score")
            if not args.confidence:
                args.confidence = scores_data.get("confidence", "")
            if not args.risks:
                risks = scores_data.get("risks", [])
                if risks:
                    args.risks = json.dumps(risks, ensure_ascii=False)
            if not args.special:
                special = scores_data.get("special")
                if special:
                    args.special = json.dumps(special, ensure_ascii=False)
            # Read dimension summaries from scores.json
            dim_summaries = scores_data.get("dimension_summaries", {})
            if not args.capital_summary and dim_summaries.get("capital"):
                args.capital_summary = dim_summaries["capital"]
            if not args.fundamental_summary and dim_summaries.get("fundamental"):
                args.fundamental_summary = dim_summaries["fundamental"]
            if not args.sentiment_summary and dim_summaries.get("sentiment"):
                args.sentiment_summary = dim_summaries["sentiment"]
            if not args.macro_summary and dim_summaries.get("macro"):
                args.macro_summary = dim_summaries["macro"]
            # Read comprehensive analysis for 综合研判 section
            if not args.analysis:
                analysis = scores_data.get("analysis")
                if analysis:
                    args.analysis = json.dumps(analysis, ensure_ascii=False)
            # Read entry signals from report_params
            rp = scores_data.get("report_params", {})
            if not args.entry_verdict:
                args.entry_verdict = rp.get("entry_verdict", "wait")
            if not args.entry_signals:
                args.entry_signals = json.dumps(rp.get("entry_signals", []), ensure_ascii=False)
        except (OSError, json.JSONDecodeError):
            pass

    context = build_context(args)

    # Generate Markdown report
    if args.output_md:
        md_template_path = ASSETS_DIR / "report-template.md"
        if md_template_path.exists():
            template = md_template_path.read_text(encoding="utf-8")
            report = render_template(template, context)
            os.makedirs(os.path.dirname(args.output_md) if os.path.dirname(args.output_md) else ".", exist_ok=True)
            with open(args.output_md, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"Markdown report written to {args.output_md}", file=sys.stderr)
        else:
            print(f"Warning: template not found at {md_template_path}", file=sys.stderr)

    # Generate HTML report
    if args.output_html:
        html_template_path = ASSETS_DIR / "report-template.html"
        if html_template_path.exists():
            template = html_template_path.read_text(encoding="utf-8")
            report = render_template(template, context)
            # Embed chart fragment (post-render to avoid template syntax conflicts)
            if context.get("has_chart") and args.chart:
                try:
                    with open(args.chart, "r", encoding="utf-8") as f:
                        chart_html = f.read()
                    report = report.replace("__CHART_HTML__", chart_html)
                except Exception:
                    report = report.replace("__CHART_HTML__", "<!-- Chart unavailable -->")
            os.makedirs(os.path.dirname(args.output_html) if os.path.dirname(args.output_html) else ".", exist_ok=True)
            with open(args.output_html, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"HTML report written to {args.output_html}", file=sys.stderr)
        else:
            print(f"Warning: template not found at {html_template_path}", file=sys.stderr)


if __name__ == "__main__":
    main()