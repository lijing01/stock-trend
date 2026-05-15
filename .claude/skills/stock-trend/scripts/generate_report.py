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
                        items.append(render_template(body, item))
                    else:
                        items.append(render_template(body, {key + "_item": item}))
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

    # Parse scores JSON
    scores = {}
    if args.scores:
        try:
            scores = json.loads(args.scores)
        except json.JSONDecodeError:
            pass

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
    stop_loss = summary.get("stop_loss", "—")
    target_conservative = summary.get("target_conservative")
    target_moderate = summary.get("target_moderate") or summary.get("target", "—")
    target_aggressive = summary.get("target_aggressive")
    target = summary.get("target", "—")
    rr_ratio = summary.get("risk_reward_ratio", "—")
    favorable_rr = summary.get("favorable_rr")
    position_sizing = summary.get("position_sizing", "—")
    max_drawdown = summary.get("max_drawdown_pct", "—")

    # Format target with three tiers if available
    target_display = str(target_moderate)
    if target_conservative and target_aggressive:
        target_display = f"{target_conservative}/{target_moderate}/{target_aggressive}"

    # Support/resistance levels
    support_levels = summary.get("support_levels", [])
    resistance_levels = summary.get("resistance_levels", [])
    support_str = " / ".join(str(s) for s in support_levels[:3]) if support_levels else "—"
    resistance_str = " / ".join(str(r) for r in resistance_levels[:3]) if resistance_levels else "—"

    # R:R warning
    rr_warning = summary.get("risk_reward_warning", "")

    # K-line data info
    data_source = kline_meta.get("data_source", "")
    data_points = kline_meta.get("record_count") or kline_meta.get("data_points") or meta.get("data_points", 0)
    kline_start = kline_meta.get("start_date", "")
    kline_end = kline_meta.get("end_date", "")
    kline_range = f"{kline_start}~{kline_end}" if kline_start and kline_end else ""

    # Risk/reward display
    rr_display = str(rr_ratio)
    if favorable_rr is True:
        rr_display += " ✓"
    elif favorable_rr is False:
        rr_display += " ✗"

    # Monitor signals
    monitor_signals = []
    if direction in ("bullish", "看多") and support_levels:
        monitor_signals = [
            ("跌破止损位", "立即止损离场"),
            (f"跌破支撑位{support_levels[0]}", "减半仓位，重新评估"),
            (f"突破目标价{target_moderate}", "可分批止盈"),
        ]
    elif direction in ("bearish", "看空") and resistance_levels:
        monitor_signals = [
            ("突破止损位", "立即离场"),
            (f"突破压力位{resistance_levels[0]}", "重新评估方向"),
            ("继续下跌创新低", "持有观望"),
        ]
    elif direction in ("neutral", "震荡"):
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
        iopv_str = f"IOPV折溢价率: {iopv_premium:+.2f}%" if iopv_premium is not None else "IOPV数据暂无"
        holdings = etf_data.get("top_holdings", [])
        holdings_str = ""
        if holdings:
            holdings_str = "前十大持仓: " + "、".join(f"{h['name']}({h.get('weight', '?')}%)" for h in holdings[:5])
        special_section = {
            "type": "etf",
            "title": "ETF 特殊分析",
            "content": f"{iopv_str}\n{holdings_str}" if holdings_str else iopv_str,
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
        "止损位": stop_loss,
        "目标位": target_display,
        "风险收益比": rr_display,
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
    }

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
    # Output paths
    parser.add_argument("--output-md", help="Output Markdown file path")
    parser.add_argument("--output-html", help="Output HTML file path")

    args = parser.parse_args()

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
        except (OSError, json.JSONDecodeError):
            pass

    # Load scores file to auto-fill scoring parameters
    if args.scores_file:
        try:
            with open(args.scores_file, "r", encoding="utf-8") as f:
                scores_data = json.load(f)
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
            os.makedirs(os.path.dirname(args.output_html) if os.path.dirname(args.output_html) else ".", exist_ok=True)
            with open(args.output_html, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"HTML report written to {args.output_html}", file=sys.stderr)
        else:
            print(f"Warning: template not found at {html_template_path}", file=sys.stderr)


if __name__ == "__main__":
    main()