#!/usr/bin/env python3
"""Add frozen East Money sector links to an existing daily review pair."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _require_single(matches, error: str):
    matches = list(matches)
    if len(matches) != 1:
        raise ValueError(error)
    return matches[0]


def _load_context(path: Path) -> dict:
    context = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(context, dict):
        raise ValueError("context_invalid")
    data_date = context.get("data_date")
    try:
        parsed_date = dt.date.fromisoformat(data_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("context_data_date_invalid") from exc
    if parsed_date.isoformat() != data_date:
        raise ValueError("context_data_date_invalid")
    if not isinstance(context.get("generated_at"), str) or not context["generated_at"].strip():
        raise ValueError("context_generated_at_invalid")
    for key, limit in (("top_sectors", 10), ("bottom_sectors", 3)):
        sectors = context.get(key)
        if not isinstance(sectors, list) or len(sectors) < limit:
            raise ValueError(f"context_{key}_invalid")
        for sector in sectors[:limit]:
            if not isinstance(sector, dict) or not isinstance(sector.get("name"), str):
                raise ValueError(f"context_{key}_invalid")
            try:
                change_pct = float(sector.get("change_pct"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"context_{key}_invalid") from exc
            if not math.isfinite(change_pct):
                raise ValueError(f"context_{key}_invalid")
    return context


def _old_html_block(context: dict) -> str:
    def ranking(title: str, sectors: list[dict]) -> str:
        items = "".join(
            f"<li><strong>{sector['name']}</strong> {float(sector['change_pct']):+.2f}%</li>"
            for sector in sectors
        )
        return f"<p><strong>{title}:</strong></p><ul>{items}</ul>"

    return "\n".join((
        ranking("当日排名最强前10", context["top_sectors"][:10]),
        ranking("当日排名最弱前3", context["bottom_sectors"][:3]),
    ))


def _old_markdown_block(context: dict) -> str:
    lines = ["**当日排名最强前10**:"]
    lines.extend(
        f"- {sector['name']} {float(sector['change_pct']):+.2f}%"
        for sector in context["top_sectors"][:10]
    )
    lines.extend(("", "**当日排名最弱前3**:"))
    lines.extend(
        f"- {sector['name']} {float(sector['change_pct']):+.2f}%"
        for sector in context["bottom_sectors"][:3]
    )
    return "\n".join(lines)


def _replace_exact_area(content: str, start: int, end: int, old: str, new: str,
                        error: str) -> str:
    area = content[start:end]
    core = area.strip()
    if core == new:
        return content
    if core != old:
        raise ValueError(error)
    leading = len(area) - len(area.lstrip())
    trailing = len(area) - len(area.rstrip())
    suffix_start = len(area) - trailing if trailing else len(area)
    replacement = area[:leading] + new + area[suffix_start:]
    return content[:start] + replacement + content[end:]


def _validate_html_identity(content: str, context: dict) -> tuple[int, int]:
    data_date = re.escape(context["data_date"])
    generated_at = re.escape(context["generated_at"])
    _require_single(
        re.finditer(rf"<h1>📅 今日复盘 {data_date}</h1>", content),
        "html_basis_date_mismatch",
    )
    _require_single(
        re.finditer(rf'<p class="dt">{generated_at}</p>', content),
        "html_generated_at_mismatch",
    )
    heading = _require_single(
        re.finditer(r'<h2 id="market-sectors">② 板块</h2>', content),
        "html_sector_heading_invalid",
    )
    persistence = _require_single(
        re.finditer(r'<section id="sector-persistence"(?:\s|>)', content),
        "html_sector_persistence_invalid",
    )
    if heading.end() >= persistence.start():
        raise ValueError("html_sector_section_order_invalid")
    return heading.end(), persistence.start()


def _validate_markdown_identity(content: str, context: dict) -> tuple[int, int]:
    data_date = re.escape(context["data_date"])
    generated_at = re.escape(context["generated_at"])
    _require_single(
        re.finditer(rf"(?m)^## 📅 今日复盘 \({data_date}\)$", content),
        "markdown_basis_date_mismatch",
    )
    _require_single(
        re.finditer(rf"(?m)^▸ 生成时间: {generated_at}$", content),
        "markdown_generated_at_mismatch",
    )
    heading = _require_single(
        re.finditer(r"(?m)^### ② 板块$", content),
        "markdown_sector_heading_invalid",
    )
    ranking = _require_single(
        re.finditer(r"(?m)^\*\*当日排名最强前10\*\*:$", content),
        "markdown_sector_ranking_invalid",
    )
    persistence = _require_single(
        re.finditer(r"(?m)^### 板块持续性$", content),
        "markdown_sector_persistence_invalid",
    )
    if not heading.end() < ranking.start() < persistence.start():
        raise ValueError("markdown_sector_section_order_invalid")
    return ranking.start(), persistence.start()


def _updated_reports(html_content: str, markdown_content: str,
                     context: dict) -> tuple[str, str]:
    from analysis.market_regime import render_sector_rankings

    html_start, html_end = _validate_html_identity(html_content, context)
    markdown_start, markdown_end = _validate_markdown_identity(markdown_content, context)
    html_updated = _replace_exact_area(
        html_content,
        html_start,
        html_end,
        _old_html_block(context),
        render_sector_rankings(context, "html"),
        "html_sector_rankings_mismatch",
    )
    markdown_updated = _replace_exact_area(
        markdown_content,
        markdown_start,
        markdown_end,
        _old_markdown_block(context),
        render_sector_rankings(context, "markdown"),
        "markdown_sector_rankings_mismatch",
    )
    return html_updated, markdown_updated


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".pre-sector-links")


def _backup_once(path: Path, original: str) -> None:
    from core.report_file import atomic_write_text

    backup = _backup_path(path)
    if not backup.exists():
        atomic_write_text(backup, original)


def update_reports(html_path, context_path) -> dict:
    """Patch an explicit HTML/Markdown report pair from one frozen context."""
    from bridge.review_detail_links import ReportUpdateError
    from core.report_file import atomic_write_text, report_lock

    html_path = Path(html_path)
    if html_path.suffix.lower() != ".html":
        raise ValueError("html_report_path_required")
    context_path = Path(context_path)
    markdown_path = html_path.with_suffix(".md")
    if not markdown_path.exists():
        raise ValueError("markdown_companion_missing")
    context = _load_context(context_path)
    written: list[Path] = []
    with report_lock(html_path):
        with report_lock(markdown_path):
            html_original = html_path.read_text(encoding="utf-8")
            markdown_original = markdown_path.read_text(encoding="utf-8")
            html_updated, markdown_updated = _updated_reports(
                html_original, markdown_original, context
            )
            try:
                if html_updated != html_original:
                    _backup_once(html_path, html_original)
                if markdown_updated != markdown_original:
                    _backup_once(markdown_path, markdown_original)
                if html_updated != html_original:
                    atomic_write_text(html_path, html_updated)
                    written.append(html_path)
                if markdown_updated != markdown_original:
                    atomic_write_text(markdown_path, markdown_updated)
                    written.append(markdown_path)
            except Exception as exc:
                raise ReportUpdateError(
                    f"report_write_failed: {exc}", written_paths=written
                ) from exc
    return {
        "status": "updated" if written else "unchanged",
        "html_path": str(html_path.resolve()),
        "markdown_path": str(markdown_path.resolve()),
        "context_path": str(context_path.resolve()),
        "basis_date": context["data_date"],
        "generated_at": context["generated_at"],
        "written_paths": [str(path.resolve()) for path in written],
    }


def main(argv=None) -> int:
    from bridge.review_detail_links import ReportUpdateError

    parser = argparse.ArgumentParser(description="补充每日复盘板块东方财富链接")
    parser.add_argument("--html-path", required=True, type=Path)
    parser.add_argument("--context-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = update_reports(args.html_path, args.context_path)
    except ReportUpdateError as exc:
        details = exc.as_dict()
        details["recovery"] = "修复写入错误后使用相同报告和冻结上下文重跑；已更新的板块区块保持幂等。"
        print(json.dumps(details, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
            "written_paths": [],
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["update_reports"]
