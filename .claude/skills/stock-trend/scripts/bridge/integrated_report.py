#!/usr/bin/env python3
"""Integrated report — merges ths-theme + longtou outputs into one report.

Functions:
    build_sector_overview: build overview section data
    generate_integrated_md: generate combined Markdown report
    generate_integrated_html: generate combined HTML report
"""

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Optional

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "reports" / "lists"


def build_sector_overview(
    total_hot: int,
    leaders: int,
    dual: int,
    lhb: int,
    top: list[str],
) -> dict:
    """Build overview section data."""
    return {
        "total_hot_sectors": total_hot,
        "total_leaders": leaders,
        "dual_confirmed": dual,
        "lhb_strong": lhb,
        "top_sectors": top,
    }


def generate_integrated_md(
    date: str,
    ths_report: str,
    leader_report: str,
    overview: dict,
) -> str:
    """Generate integrated Markdown report."""
    lines = []
    lines.append(f"# 市场热力 · 龙头整合报告 — {date}")
    lines.append("")

    # Section 1: Overview
    lines.append("## 一、市场总览")
    lines.append("")
    lines.append(f"- 双强热力板块: {overview.get('total_hot_sectors', 0)} 个")
    if overview.get("top_sectors"):
        lines.append(f"- 最强板块: {'、'.join(overview['top_sectors'][:3])}")
    lines.append(f"- 龙头标的: {overview.get('total_leaders', 0)} 只")
    lines.append(f"- 机构净买板块(LHB≥60): {overview.get('lhb_strong', 0)} 个")
    # Qualitative market sentiment
    heat = overview.get("total_hot_sectors", 0)
    if heat >= 5:
        lines.append("- 市场情绪: **积极** 🟢 — 多板块共振")
    elif heat >= 2:
        lines.append("- 市场情绪: **中性** 🔵 — 局部热点")
    else:
        lines.append("- 市场情绪: **谨慎** ⚪ — 板块效应弱")
    lines.append("")

    # Section 2: Sector leaders
    lines.append("## 二、热力板块 · 龙头扫描")
    lines.append("")
    if leader_report.strip():
        lines.append(leader_report.strip())
    else:
        lines.append("*无满足双强条件的板块，跳过龙头扫描*")
        lines.append("")

    # Section 3: Reference
    lines.append("---")
    lines.append("")
    if ths_report.strip():
        lines.append("### 参考：板块热力详情")
        lines.append("")
        lines.append(ths_report.strip())
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> *本报告由 ths-theme + longtou 整合生成 | 仅供学习参考，不构成投资建议*")
    lines.append("")

    return "\n".join(lines)


def generate_integrated_html(
    date: str,
    ths_report: str,
    leader_report: str,
    overview: dict,
) -> str:
    """Generate integrated HTML report."""

    # Color tag CSS for signal labels
    tags_css = """
    <style>
    .signal-strong { color: #16a34a; background: #f0fdf4; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
    .signal-active { color: #1d4ed8; background: #eff6ff; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
    .signal-caution { color: #d97706; background: #fffbeb; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
    .signal-watch   { color: #6b7280; background: #f9fafb; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
    .tag-strong { display:inline-block; background:#16a34a; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:700; }
    .tag-active { display:inline-block; background:#1d4ed8; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:600; }
    .tag-caution { display:inline-block; background:#d97706; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; font-weight:600; }
    .tag-watch { display:inline-block; background:#6b7280; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; }
    </style>
    """

    heat = overview.get("total_hot_sectors", 0)
    if heat >= 5:
        sentiment = '<span class="tag-strong">积极</span> — 多板块共振'
    elif heat >= 2:
        sentiment = '<span class="tag-active">中性</span> — 局部热点'
    else:
        sentiment = '<span class="tag-strong" style="background:#9ca3af">谨慎</span> — 板块效应弱'

    html_parts = []
    html_parts.append('<!DOCTYPE html>')
    html_parts.append('<html lang="zh-CN">')
    html_parts.append('<head>')
    html_parts.append('<meta charset="UTF-8">')
    html_parts.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html_parts.append(f'<title>市场热力 · 龙头整合报告 — {escape(date)}</title>')
    html_parts.append(tags_css)
    html_parts.append("""
    <style>
    body { font-family: 'PingFang SC','Microsoft YaHei',sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #fafafa; color: #1d1d1f; }
    h1 { font-size: 24px; margin-bottom: 4px; }
    h2 { font-size: 18px; margin-top: 24px; border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; }
    h3 { font-size: 16px; margin-top: 20px; }
    table { width: 100%; border-collapse: collapse; margin: 12px 0; }
    th,td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #e5e7eb; font-size: 14px; }
    th { background: #1d4ed8; color: #fff; }
    tr:hover td { background: #f0f0f0; }
    .overview { display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }
    .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px 20px; flex: 1; min-width: 120px; }
    .card .num { font-size: 28px; font-weight: 700; }
    .card .lbl { font-size: 12px; color: #86868b; }
    .sector-block { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin: 16px 0; }
    .footer { font-size: 12px; color: #a1a1a6; text-align: center; margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb; }
    </style>
    """)
    html_parts.append('</head>')
    html_parts.append('<body>')

    html_parts.append('<h1>\U0001f4ca 市场热力 · 龙头整合报告</h1>')
    html_parts.append(f'<p style="color:#86868b;margin:0 0 16px">{escape(date)}</p>')

    # Overview cards
    html_parts.append('<div class="overview">')
    html_parts.append(f'  <div class="card"><div class="num">{overview.get("total_hot_sectors",0)}</div><div class="lbl">双强热力板块</div></div>')
    html_parts.append(f'  <div class="card"><div class="num">{overview.get("total_leaders",0)}</div><div class="lbl">龙头标的</div></div>')
    html_parts.append(f'  <div class="card"><div class="num">{overview.get("lhb_strong",0)}</div><div class="lbl">机构净买板块</div></div>')
    html_parts.append(f'  <div class="card"><div class="lbl" style="margin-top:8px">市场情绪: {sentiment}</div></div>')
    html_parts.append('</div>')

    # Sector leaders section
    html_parts.append('<div class="sector-block">')
    html_parts.append('<h2>\U0001f525 热力板块 · 龙头扫描</h2>')

    if leader_report.strip():
        lines = leader_report.split("\n")
        in_table = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("###"):
                if in_table:
                    html_parts.append("</tbody></table>")
                    in_table = False
                html_parts.append(f"<h3>{escape(stripped.lstrip('#').strip())}</h3>")
            elif stripped.startswith("> "):
                html_parts.append(f'<p style="font-size:13px;color:#86868b">{escape(stripped[2:])}</p>')
            elif stripped.startswith("**龙头**"):
                if in_table:
                    html_parts.append("</tbody></table>")
                html_parts.append("<table><thead><tr><th>个股</th><th>评分</th><th>方向</th></tr></thead><tbody>")
                in_table = True
            elif stripped.startswith("**中军**"):
                if in_table:
                    html_parts.append("</tbody></table>")
                html_parts.append("<table><thead><tr><th>个股</th><th>评分</th><th>方向</th></tr></thead><tbody>")
            elif stripped.startswith("- "):
                name_end = stripped.find("(", 2)
                if name_end > 2:
                    name = stripped[2:name_end]
                    rest = stripped[name_end:]
                    html_parts.append(f"<tr><td>{escape(name)}</td><td>{escape(rest[:60])}</td><td></td></tr>")
                else:
                    html_parts.append(f"<tr><td colspan='3'>{escape(stripped)}</td></tr>")
            else:
                if in_table and stripped == "":
                    html_parts.append("</tbody></table>")
                    in_table = False
                elif stripped:
                    html_parts.append(f'<p style="margin:4px 0;font-size:13px">{escape(stripped)}</p>')
        if in_table:
            html_parts.append("</tbody></table>")
    else:
        html_parts.append('<p style="color:#86868b">无满足双强条件的板块，跳过龙头扫描</p>')

    html_parts.append("</div>")

    # Reference section
    if ths_report.strip():
        html_parts.append('<div class="sector-block"><h2>\U0001f4cb 参考：板块热力详情</h2>')
        html_parts.append(f"<pre style='font-size:13px;color:#333;white-space:pre-wrap'>{escape(ths_report[:2000])}</pre>")
        html_parts.append("</div>")

    html_parts.append("""
    <div class="footer">
    本报告由 ths-theme + longtou 整合生成 | 仅供学习参考，不构成投资建议
    </div>
    </body>
    </html>""")

    return "\n".join(html_parts)
