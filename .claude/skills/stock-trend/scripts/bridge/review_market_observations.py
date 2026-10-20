#!/usr/bin/env python3
"""Backfill frozen market observations into one explicit daily-review pair."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


_COMPONENTS = (
    ("index_trend", "大盘趋势"),
    ("volume", "成交额"),
    ("breadth", "赚钱效应"),
    ("zt_emotion", "涨停情绪"),
    ("capital", "资金"),
)
_HTML_OVERVIEW_START = '<h2>① 市场环境</h2>'
_HTML_OVERVIEW_END = '<h2 id="market-sectors">'
_MD_OVERVIEW_START = "### ① 市场环境"
_MD_OVERVIEW_END = '<a id="market-sectors"></a>'
_DETAIL_START = "<!-- MARKET_DETAIL_LINKS:START -->"
_DETAIL_END = "<!-- MARKET_DETAIL_LINKS:END -->"


def _require_single_matches(content: str, pattern: str, error: str, *, flags=0):
    matches = list(re.finditer(pattern, content, flags))
    if len(matches) != 1:
        raise ValueError(error)
    return matches[0]


def _load_context(path: Path) -> dict:
    context = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(context, dict):
        raise ValueError("context_invalid")
    value = context.get("data_date")
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("context_data_date_invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError("context_data_date_invalid")
    generated_at = context.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("context_generated_at_invalid")
    components = context.get("components")
    if not isinstance(components, dict):
        raise ValueError("context_components_invalid")
    for key, _ in _COMPONENTS:
        component = components.get(key)
        if not isinstance(component, dict):
            raise ValueError(f"context_component_invalid:{key}")
        try:
            score = float(component.get("score"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"context_component_score_invalid:{key}") from exc
        if not math.isfinite(score):
            raise ValueError(f"context_component_score_invalid:{key}")
        detail = component.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError(f"context_component_detail_invalid:{key}")
    return context


def _normal_text(value) -> str:
    value = html.unescape(str(value or ""))
    value = value.replace("**", "").replace(r"\|", "|")
    value = re.sub(r"\\([\\`*_\[\]()<>#])", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", "", value).strip()


def _score_equal(left, right) -> bool:
    try:
        return Decimal(_normal_text(left)) == Decimal(str(right))
    except InvalidOperation:
        return False


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    cells, current, escaped = [], [], False
    for char in stripped[1:-1]:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    cells.append("".join(current).strip())
    return cells


def _validate_component_rows(rows: list[list[str]], context: dict, error: str) -> None:
    if len(rows) != len(_COMPONENTS):
        raise ValueError(error)
    for row, (key, name) in zip(rows, _COMPONENTS):
        if len(row) not in {3, 4} or _normal_text(row[0]) != name:
            raise ValueError(error)
        component = context["components"][key]
        if not _score_equal(row[1], component["score"]):
            raise ValueError(f"{error}:score:{key}")
        if _normal_text(row[2]) != _normal_text(component["detail"]):
            raise ValueError(f"{error}:detail:{key}")


def _validate_old_html_table(region: str, context: dict) -> None:
    from bridge.review_detail_links import _DocumentParser, _descendants, _direct_cells

    parser = _DocumentParser(region)
    parser.feed(region)
    candidates = []
    for table in parser.find("table"):
        rows = _descendants(table, "tr")
        if not rows:
            continue
        header = [_normal_text(cell.text) for cell in _direct_cells(rows[0])]
        if header in (["组件", "得分", "说明"], ["组件", "得分", "说明", "详情"]):
            candidates.append(rows)
    if len(candidates) != 1:
        raise ValueError("html_market_component_table_invalid")
    rows = [[cell.text for cell in _direct_cells(row)] for row in candidates[0][1:]]
    _validate_component_rows(rows, context, "html_market_component_mismatch")


def _validate_old_markdown_table(region: str, context: dict) -> None:
    lines = region.splitlines()
    headers = [
        index for index, line in enumerate(lines)
        if [_normal_text(cell) for cell in _split_markdown_row(line)]
        in (["组件", "得分", "说明"], ["组件", "得分", "说明", "详情"])
    ]
    if len(headers) != 1 or headers[0] + 6 >= len(lines):
        raise ValueError("markdown_market_component_table_invalid")
    start = headers[0]
    separator = _split_markdown_row(lines[start + 1])
    if not separator or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        raise ValueError("markdown_market_component_table_invalid")
    rows = [_split_markdown_row(lines[index]) for index in range(start + 2, start + 7)]
    _validate_component_rows(rows, context, "markdown_market_component_mismatch")


def _overview_bounds(content: str, start_marker: str, end_marker: str, error: str):
    start = _require_single_matches(content, re.escape(start_marker), f"{error}_start_invalid")
    end = _require_single_matches(content, re.escape(end_marker), f"{error}_end_invalid")
    if start.start() >= end.start():
        raise ValueError(f"{error}_order_invalid")
    return start.start(), end.start()


def _rendered_overview(context: dict, output_format: str) -> str:
    from analysis.market_regime import render_market_overview

    rendered = render_market_overview(context, output_format)
    if not isinstance(rendered, str) or not rendered.strip():
        raise ValueError(f"rendered_{output_format}_overview_invalid")
    suffix = "\n" if output_format == "html" else "\n\n"
    return rendered.strip() + suffix


def _validate_identity(content: str, context: dict, output_format: str) -> None:
    date = re.escape(context["data_date"])
    generated = re.escape(context["generated_at"])
    if output_format == "html":
        _require_single_matches(
            content, rf"<h1>📅 今日复盘 {date}</h1>", "html_basis_date_mismatch"
        )
        _require_single_matches(
            content, rf'<p class="dt">{generated}</p>', "html_generated_at_mismatch"
        )
    else:
        _require_single_matches(
            content, rf"^## 📅 今日复盘 \({date}\)$", "markdown_basis_date_mismatch",
            flags=re.MULTILINE,
        )
        _require_single_matches(
            content, rf"^▸ 生成时间: {generated}$", "markdown_generated_at_mismatch",
            flags=re.MULTILINE,
        )


def _detail_bounds(content: str, output_format: str):
    start = _require_single_matches(
        content, re.escape(_DETAIL_START), f"{output_format}_market_detail_start_invalid"
    )
    end = _require_single_matches(
        content, re.escape(_DETAIL_END), f"{output_format}_market_detail_end_invalid"
    )
    end_pos = end.end()
    if start.start() >= end_pos:
        raise ValueError(f"{output_format}_market_detail_order_invalid")
    return start.start(), end_pos


def _validate_old_detail_block(block: str, context: dict, output_format: str) -> None:
    if output_format == "html":
        from bridge.review_detail_links import _DocumentParser

        parser = _DocumentParser(block)
        parser.feed(block)
        fragments = {}
        expected = {key for key, _ in _COMPONENTS}
        for element in parser.elements:
            identifier = element.attrs.get("id")
            if isinstance(identifier, str) and identifier.startswith("market-detail-"):
                key = identifier.removeprefix("market-detail-")
                if key not in expected:
                    continue
                if key in fragments:
                    raise ValueError("html_market_detail_component_duplicate")
                fragments[key] = element.text
    else:
        anchors = list(re.finditer(r'<a id="market-detail-([a-z_]+)"></a>', block))
        fragments = {}
        for index, match in enumerate(anchors):
            key = match.group(1)
            if key in fragments:
                raise ValueError("markdown_market_detail_component_duplicate")
            end = anchors[index + 1].start() if index + 1 < len(anchors) else len(block)
            fragments[key] = block[match.end():end]
    if tuple(fragments) != tuple(key for key, _ in _COMPONENTS):
        raise ValueError(f"{output_format}_market_detail_components_invalid")
    for key, _ in _COMPONENTS:
        normalized = _normal_text(fragments[key])
        component = context["components"][key]
        score = _normal_text(component["score"])
        detail = _normal_text(component["detail"])
        if score not in normalized:
            raise ValueError(f"{output_format}_market_detail_score_mismatch:{key}")
        if detail not in normalized:
            raise ValueError(f"{output_format}_market_detail_text_mismatch:{key}")


def _updated_report(content: str, context: dict, output_format: str) -> str:
    from reporting.market_detail_links import render_details

    _validate_identity(content, context, output_format)
    if output_format == "html":
        overview_start, overview_end = _overview_bounds(
            content, _HTML_OVERVIEW_START, _HTML_OVERVIEW_END, "html_market_overview"
        )
    else:
        overview_start, overview_end = _overview_bounds(
            content, _MD_OVERVIEW_START, _MD_OVERVIEW_END, "markdown_market_overview"
        )
    desired_overview = _rendered_overview(context, output_format)
    old_overview = content[overview_start:overview_end]
    if old_overview != desired_overview:
        if output_format == "html":
            _validate_old_html_table(old_overview, context)
        else:
            _validate_old_markdown_table(old_overview, context)

    detail_start, detail_end = _detail_bounds(content, output_format)
    desired_details = render_details(context, output_format).strip()
    old_details = content[detail_start:detail_end]
    if old_details.strip() != desired_details:
        _validate_old_detail_block(old_details, context, output_format)

    edits = [
        (overview_start, overview_end, desired_overview),
        (detail_start, detail_end, desired_details),
    ]
    result = content
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".pre-p0-observations")


def _backup_once(path: Path, original: str) -> None:
    from core.report_file import atomic_write_text

    backup = _backup_path(path)
    if not backup.exists():
        atomic_write_text(backup, original)


def update_reports(html_path, context_path) -> dict:
    """Patch an explicit HTML/Markdown pair using only the supplied context."""
    from bridge.review_detail_links import ReportUpdateError
    from core.report_file import atomic_write_text, report_lock

    html_path = Path(html_path)
    if html_path.suffix.lower() != ".html":
        raise ValueError("html_report_path_required")
    markdown_path = html_path.with_suffix(".md")
    if not markdown_path.exists():
        raise ValueError("markdown_companion_missing")
    context_path = Path(context_path)
    context = _load_context(context_path)
    written: list[Path] = []
    with report_lock(html_path):
        with report_lock(markdown_path):
            html_original = html_path.read_text(encoding="utf-8")
            markdown_original = markdown_path.read_text(encoding="utf-8")
            html_updated = _updated_report(html_original, context, "html")
            markdown_updated = _updated_report(markdown_original, context, "markdown")
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

    parser = argparse.ArgumentParser(description="补充每日复盘市场原始观察与模型口径")
    parser.add_argument("--html-path", required=True, type=Path)
    parser.add_argument("--context-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = update_reports(args.html_path, args.context_path)
    except ReportUpdateError as exc:
        details = exc.as_dict()
        details["recovery"] = (
            "修复写入错误后使用相同报告和冻结上下文重跑；"
            "已更新的市场观察区块保持幂等。"
        )
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
