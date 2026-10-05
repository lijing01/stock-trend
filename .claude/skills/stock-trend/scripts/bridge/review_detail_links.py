#!/usr/bin/env python3
"""Add frozen market-component detail links to an existing daily review."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


HTML_BLOCK_START = "<!-- MARKET_DETAIL_LINKS:START -->"
HTML_BLOCK_END = "<!-- MARKET_DETAIL_LINKS:END -->"
MD_BLOCK_START = HTML_BLOCK_START
MD_BLOCK_END = HTML_BLOCK_END
SUMMARY_ANCHOR = "market-component-summary"
_EXPECTED_COMPONENTS = (
    ("index_trend", "大盘趋势"),
    ("volume", "成交额"),
    ("breadth", "赚钱效应"),
    ("zt_emotion", "涨停情绪"),
    ("capital", "资金"),
)


class ReportUpdateError(RuntimeError):
    """Report update failed, optionally after one visible file was written."""

    def __init__(self, message: str, *, written_paths: Iterable[Path] = ()):
        self.written_paths = tuple(str(Path(path).resolve()) for path in written_paths)
        self.status = "partial" if self.written_paths else "failed"
        super().__init__(message)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "error": str(self),
            "written_paths": list(self.written_paths),
            "recovery": "修复写入错误后使用相同参数重跑；区块标记保证重跑幂等。",
        }


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str | None]
    start: int
    start_end: int
    parent: "_Element | None" = None
    end_start: int | None = None
    end: int | None = None
    text_parts: list[str] = field(default_factory=list)
    children: list["_Element"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


class _DocumentParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0]
        for match in re.finditer(r"\n", source):
            self.line_offsets.append(match.end())
        self.stack: list[_Element] = []
        self.elements: list[_Element] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self._offset()
        raw = self.get_starttag_text() or ""
        parent = self.stack[-1] if self.stack else None
        element = _Element(
            tag=tag.lower(),
            attrs={str(key).lower(): value for key, value in attrs},
            start=start,
            start_end=start + len(raw),
            parent=parent,
        )
        if parent:
            parent.children.append(element)
        self.elements.append(element)
        self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        element = self.stack.pop()
        element.end_start = element.start_end
        element.end = element.start_end

    def handle_endtag(self, tag):
        tag = tag.lower()
        match_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1)
             if self.stack[index].tag == tag),
            None,
        )
        if match_index is None:
            return
        start = self._offset()
        end = self.source.find(">", start)
        end = len(self.source) if end < 0 else end + 1
        for element in self.stack[match_index:]:
            if element.end is None:
                element.end_start = start
                element.end = end
        del self.stack[match_index:]

    def handle_data(self, data):
        for element in self.stack:
            element.text_parts.append(data)

    def find(self, tag: str) -> list[_Element]:
        return [element for element in self.elements if element.tag == tag]


def _norm(value) -> str:
    value = html.unescape(str(value or ""))
    value = value.replace("**", "").replace(r"\|", "|")
    value = re.sub(r"\\([\\`*_\[\]()<>#])", r"\1", value)
    return re.sub(r"\s+", "", value).strip()


def _descendants(element: _Element, tag: str) -> list[_Element]:
    result = []
    pending = list(element.children)
    while pending:
        child = pending.pop(0)
        if child.tag == tag:
            result.append(child)
        pending[0:0] = child.children
    return result


def _direct_cells(row: _Element) -> list[_Element]:
    return [child for child in row.children if child.tag in {"td", "th"}]


def _require_single(items, error: str):
    if len(items) != 1:
        raise ValueError(error)
    return items[0]


def _validate_basis_date(value: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("basis_date_invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError("basis_date_invalid")
    return value


def _parse_html_report(content: str, basis_date: str) -> dict:
    parser = _DocumentParser(content)
    parser.feed(content)

    dated_headers = []
    for heading in parser.find("h1"):
        match = re.fullmatch(r"\s*(?:📅\s*)?今日复盘\s+(\d{4}-\d{2}-\d{2})\s*", heading.text)
        if match:
            dated_headers.append((heading, match.group(1)))
    header, report_date = _require_single(dated_headers, "daily_review_html_header_invalid")
    if report_date != basis_date:
        raise ValueError(f"basis_date_mismatch: report={report_date}, requested={basis_date}")

    market_headings = [item for item in parser.find("h2") if _norm(item.text) == "①市场环境"]
    market_heading = _require_single(market_headings, "market_environment_heading_invalid")
    board_heading = _require_single(
        [item for item in parser.find("h2") if _norm(item.text) == "②板块"],
        "sector_heading_invalid",
    )
    if market_heading.end is None or market_heading.end >= board_heading.start:
        raise ValueError("daily_review_html_section_order_invalid")

    tables = [
        table for table in parser.find("table")
        if market_heading.end <= table.start < board_heading.start
        and not any("market-explanation" in
                    (parent.attrs.get("class") or "").split()
                    for parent in _parents(table))
    ]
    summary_table = _require_single(tables, "market_component_table_invalid")
    if summary_table.end is None:
        raise ValueError("market_component_table_unclosed")

    rows = _descendants(summary_table, "tr")
    if len(rows) != 6:
        raise ValueError("market_component_row_count_invalid")
    header_cells = _direct_cells(rows[0])
    if [_norm(cell.text) for cell in header_cells] not in (
        ["组件", "得分", "说明"],
        ["组件", "得分", "说明", "详情"],
    ):
        raise ValueError("market_component_header_invalid")

    components = {}
    row_by_key = {}
    expected_by_name = {name: key for key, name in _EXPECTED_COMPONENTS}
    for row in rows[1:]:
        cells = _direct_cells(row)
        if len(cells) not in {3, 4}:
            raise ValueError("market_component_column_count_invalid")
        name = _norm(cells[0].text)
        key = expected_by_name.get(name)
        if not key or key in components:
            raise ValueError("market_component_names_invalid")
        components[key] = {
            "name": name,
            "score": cells[1].text.strip(),
            "detail": cells[2].text.strip(),
        }
        row_by_key[key] = (row, cells)
    if tuple(components) != tuple(key for key, _ in _EXPECTED_COMPONENTS):
        raise ValueError("market_component_order_invalid")

    explanation_sections = [
        element for element in parser.elements
        if element.tag in {"section", "details"}
        and "market-explanation" in (element.attrs.get("class") or "").split()
        and summary_table.end <= element.start
    ]
    explanation = _require_single(explanation_sections, "market_explanation_section_invalid")
    basis_matches = re.findall(r"基准日\s*(\d{4}-\d{2}-\d{2})", explanation.text)
    if basis_matches != [basis_date]:
        raise ValueError("market_explanation_basis_date_invalid")
    explanation_rows = _descendants(explanation, "tr")[1:]
    explanation_map = {}
    for row in explanation_rows:
        cells = _direct_cells(row)
        if len(cells) < 6:
            continue
        name = _norm(cells[0].text).replace("index_trend", "").replace("volume", "")
        name = name.replace("breadth", "").replace("zt_emotion", "").replace("capital", "")
        matched = next(((key, label) for key, label in _EXPECTED_COMPONENTS if name == label), None)
        if not matched or matched[0] in explanation_map:
            raise ValueError("market_explanation_components_invalid")
        explanation_map[matched[0]] = (_norm(cells[1].text), _norm(cells[5].text))
    if tuple(explanation_map) != tuple(key for key, _ in _EXPECTED_COMPONENTS):
        raise ValueError("market_explanation_components_invalid")
    for key, component in components.items():
        if explanation_map[key] != (_norm(component["score"]), _norm(component["detail"])):
            raise ValueError(f"market_explanation_component_mismatch:{key}")

    generated_candidates = [
        item for item in parser.find("p")
        if header.end is not None and header.end <= item.start < market_heading.start
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", item.text.strip())
    ]
    generated_at = _require_single(generated_candidates, "generated_at_invalid").text.strip()
    return {
        "parser": parser,
        "table": summary_table,
        "header_row": (rows[0], header_cells),
        "row_by_key": row_by_key,
        "board_heading": board_heading,
        "explanation": explanation,
        "ctx": {
            "data_date": basis_date,
            "generated_at": generated_at,
            "components": components,
        },
    }


def _parents(element: _Element):
    parent = element.parent
    while parent:
        yield parent
        parent = parent.parent


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


def _parse_markdown_report(content: str, basis_date: str, html_components: dict) -> dict:
    header_dates = re.findall(
        r"(?m)^##\s+📅\s+今日复盘\s*\((\d{4}-\d{2}-\d{2})\)\s*$", content
    )
    if header_dates != [basis_date]:
        raise ValueError("daily_review_markdown_basis_date_invalid")
    explanation_dates = re.findall(r"(?m)^- 口径：.*?；基准日\s+(\d{4}-\d{2}-\d{2})；", content)
    if explanation_dates != [basis_date]:
        raise ValueError("market_explanation_markdown_basis_date_invalid")

    section_matches = list(re.finditer(r"(?m)^###\s+①\s*市场环境\s*$", content))
    board_matches = list(re.finditer(r"(?m)^###\s+②\s*板块\s*$", content))
    explanation_matches = list(re.finditer(r"(?m)^###\s+市场环境解释\s*$", content))
    if len(section_matches) != 1 or len(board_matches) != 1 or len(explanation_matches) != 1:
        raise ValueError("daily_review_markdown_headings_invalid")
    section_match = section_matches[0]
    board_match = board_matches[0]
    explanation_match = explanation_matches[0]
    if section_match.end() >= board_match.start() or explanation_match.start() <= section_match.end():
        raise ValueError("daily_review_markdown_sections_invalid")
    region = content[section_match.end():board_match.start()]
    lines = region.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines)
         if [_norm(cell) for cell in _split_markdown_row(line)] in
         (["组件", "得分", "说明"], ["组件", "得分", "说明", "详情"])),
        None,
    )
    if header_index is None or header_index + 6 >= len(lines):
        raise ValueError("market_component_markdown_table_invalid")
    separator = _split_markdown_row(lines[header_index + 1])
    if not separator or not all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in separator):
        raise ValueError("market_component_markdown_separator_invalid")

    expected_by_name = {name: key for key, name in _EXPECTED_COMPONENTS}
    components = {}
    row_indices = {}
    for index in range(header_index + 2, header_index + 7):
        cells = _split_markdown_row(lines[index])
        if len(cells) not in {3, 4}:
            raise ValueError("market_component_markdown_columns_invalid")
        key = expected_by_name.get(_norm(cells[0]))
        if not key or key in components:
            raise ValueError("market_component_markdown_names_invalid")
        components[key] = (_norm(cells[1]), _norm(cells[2]))
        row_indices[key] = index
    if tuple(components) != tuple(key for key, _ in _EXPECTED_COMPONENTS):
        raise ValueError("market_component_markdown_order_invalid")
    next_index = header_index + 7
    while next_index < len(lines) and not lines[next_index].strip():
        next_index += 1
    if next_index < len(lines) and _split_markdown_row(lines[next_index]):
        raise ValueError("market_component_markdown_extra_row")
    for key, component in html_components.items():
        if components[key] != (_norm(component["score"]), _norm(component["detail"])):
            raise ValueError(f"html_markdown_component_mismatch:{key}")

    prefix_len = section_match.end() + sum(len(line) for line in lines[:header_index])
    table_end = section_match.end() + sum(len(line) for line in lines[:header_index + 7])
    return {
        "table_start": prefix_len,
        "table_end": table_end,
        "lines": lines[header_index:header_index + 7],
        "board_start": board_match.start(),
        "board_end": board_match.end(),
        "explanation_start": explanation_match.start(),
        "explanation_end": explanation_match.end(),
    }


def _replace_marker_block(
    content: str,
    block: str,
    *,
    insert_at: int,
    allowed_ranges: tuple[tuple[int, int], ...],
) -> tuple[int, int, str]:
    start_count = content.count(HTML_BLOCK_START)
    end_count = content.count(HTML_BLOCK_END)
    if start_count or end_count:
        if start_count != 1 or end_count != 1:
            raise ValueError("market_detail_links_marker_invalid")
        start = content.index(HTML_BLOCK_START)
        end = content.index(HTML_BLOCK_END, start) + len(HTML_BLOCK_END)
        if not any(lower <= start < end <= upper for lower, upper in allowed_ranges):
            raise ValueError("market_detail_links_marker_location_invalid")
        existing = content[start:end]
        for key, _ in _EXPECTED_COMPONENTS:
            if len(re.findall(
                rf"\bid\s*=\s*['\"]market-detail-{re.escape(key)}['\"]", existing
            )) != 1:
                raise ValueError("market_detail_links_existing_block_invalid")
        # Existing generated reports may contain richer frozen raw evidence. Preserve it;
        # only upgrade the generated presentation wrapper.
        from reporting.market_detail_links import upgrade_html_block
        return start, end, upgrade_html_block(existing)
    return insert_at, insert_at, block.strip() + "\n\n"


def _apply_edits(content: str, edits: list[tuple[int, int, str]]) -> str:
    result = content
    last_start = len(content) + 1
    for start, end, replacement in sorted(edits, reverse=True):
        if not (0 <= start <= end <= len(content)) or end > last_start:
            raise ValueError("overlapping_report_edits")
        result = result[:start] + replacement + result[end:]
        last_start = start
    return result


def _fold_explanation(content: str, explanation: _Element) -> tuple[int, int, str]:
    if explanation.end_start is None or explanation.end is None:
        raise ValueError("market_explanation_section_invalid")
    opening = content[explanation.start:explanation.start_end]
    if explanation.tag == "section":
        opening = re.sub(r"^<section\b", "<details", opening, count=1, flags=re.IGNORECASE)
    inner = content[explanation.start_end:explanation.end_start]
    if "market-explanation-table-wrap" not in inner:
        tables = _descendants(explanation, "table")
        table = _require_single(tables, "market_explanation_table_invalid")
        if table.end is None:
            raise ValueError("market_explanation_table_invalid")
        relative_start = table.start - explanation.start_end
        relative_end = table.end - explanation.start_end
        wrapper_start = (
            '<div class="market-explanation-table-wrap" '
            'style="overflow-x:auto;max-width:100%">'
            '<style>.market-explanation-table-wrap>table{min-width:760px}</style>'
        )
        inner = (
            inner[:relative_start]
            + wrapper_start
            + inner[relative_start:relative_end]
            + "</div>"
            + inner[relative_end:]
        )
    summary = "<summary>评分解释</summary>" if explanation.tag == "section" else ""
    replacement = opening + summary + inner + "</details>"
    return explanation.start, explanation.end, replacement


def _updated_html(content: str, parsed: dict) -> str:
    from reporting.market_detail_links import render_details, summary_link

    table = parsed["table"]
    relative_edits = []
    opening = content[table.start:table.start_end]
    anchor_elements = [
        element for element in parsed["parser"].elements
        if element.attrs.get("id") == SUMMARY_ANCHOR
    ]
    existing_id = table.attrs.get("id")
    if existing_id and existing_id != SUMMARY_ANCHOR:
        raise ValueError("market_component_table_id_conflict")
    if existing_id == SUMMARY_ANCHOR:
        if anchor_elements != [table]:
            raise ValueError("market_component_summary_anchor_duplicate")
        updated_opening = opening
    else:
        if anchor_elements:
            raise ValueError("market_component_summary_anchor_conflict")
        updated_opening = opening[:-1] + f' id="{SUMMARY_ANCHOR}">'
    if updated_opening != opening:
        relative_edits.append((0, table.start_end - table.start, updated_opening))

    header_row, header_cells = parsed["header_row"]
    if len(header_cells) == 3:
        relative_edits.append((header_row.end_start - table.start, header_row.end_start - table.start,
                               "<th>详情</th>"))
    for key, _ in _EXPECTED_COMPONENTS:
        row, cells = parsed["row_by_key"][key]
        rendered = f"<td>{summary_link(key, format='html')}</td>"
        if len(cells) == 3:
            relative_edits.append((row.end_start - table.start, row.end_start - table.start, rendered))
        elif not (
            f'href="#market-detail-{key}"' in content[cells[3].start:cells[3].end]
            and _norm(cells[3].text) == "查看详情"
        ):
            relative_edits.append((cells[3].start - table.start, cells[3].end - table.start, rendered))

    table_source = content[table.start:table.end]
    updated_table = _apply_edits(table_source, relative_edits)
    has_scroll_wrapper = any(
        "summary-table-wrap" in (parent.attrs.get("class") or "").split()
        for parent in _parents(table)
    )
    if not has_scroll_wrapper:
        updated_table = (
            '<div class="summary-table-wrap" style="overflow-x:auto;max-width:100%">'
            '<style>.summary-table-wrap>#market-component-summary{min-width:540px}</style>'
            + updated_table
            + "</div>"
        )
    block = render_details(parsed["ctx"], format="html")
    board = parsed["board_heading"]
    explanation = parsed["explanation"]
    insert_at = board.start if explanation.start < board.start else explanation.start
    allowed_ranges = (
        (table.end, board.start),
        (board.end, explanation.start),
    )
    detail_edit = _replace_marker_block(
        content,
        block,
        insert_at=insert_at,
        allowed_ranges=allowed_ranges,
    )
    explanation_edit = _fold_explanation(content, parsed["explanation"])
    return _apply_edits(
        content,
        [(table.start, table.end, updated_table), explanation_edit, detail_edit],
    )


def _updated_markdown(content: str, parsed: dict, ctx: dict) -> str:
    from reporting.market_detail_links import render_details, summary_link

    lines = list(parsed["lines"])
    headers = _split_markdown_row(lines[0])
    ending = "\n" if lines[0].endswith("\n") else ""
    if len(headers) == 3:
        lines[0] = "| " + " | ".join(headers + ["详情"]) + " |" + ending
        lines[1] = "|---|---:|---|---|" + ("\n" if lines[1].endswith("\n") else "")
    for offset, (key, _) in enumerate(_EXPECTED_COMPONENTS, start=2):
        cells = _split_markdown_row(lines[offset])
        expected_link = summary_link(key, format="markdown")
        if len(cells) == 3 or _norm(cells[3]) != _norm(expected_link):
            ending = "\n" if lines[offset].endswith("\n") else ""
            lines[offset] = "| " + " | ".join(cells[:3] + [expected_link]) + " |" + ending
    table = "".join(lines)
    before_table = content[:parsed["table_start"]]
    anchor = f'<a id="{SUMMARY_ANCHOR}"></a>\n'
    anchor_count = len(re.findall(
        rf"<a\s+id=['\"]{SUMMARY_ANCHOR}['\"]\s*></a>", content
    ))
    if anchor_count > 1 or (anchor_count == 1 and anchor not in before_table[-120:]):
        raise ValueError("market_component_summary_markdown_anchor_invalid")
    if anchor_count == 0:
        table = anchor + "\n" + table
    block = render_details(ctx, format="markdown")
    explanation_heading = re.search(r"(?m)^###\s+市场环境解释\s*$", content)
    if not explanation_heading or explanation_heading.start() != parsed["explanation_start"]:
        raise ValueError("market_explanation_markdown_heading_invalid")
    insert_at = (
        parsed["board_start"]
        if explanation_heading.start() < parsed["board_start"]
        else explanation_heading.start()
    )
    detail_edit = _replace_marker_block(
        content,
        block,
        insert_at=insert_at,
        allowed_ranges=(
            (parsed["table_end"], parsed["board_start"]),
            (parsed["board_end"], explanation_heading.start()),
        ),
    )
    return _apply_edits(
        content,
        [(parsed["table_start"], parsed["table_end"], table), detail_edit],
    )


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".pre-detail-links")


def _backup_once(path: Path, original: str) -> None:
    from core.report_file import atomic_write_text

    backup = _backup_path(path)
    if not backup.exists():
        atomic_write_text(backup, original)


def update_reports(html_path, basis_date: str) -> dict:
    """Patch one daily-review HTML file and its same-name Markdown companion."""
    from core.report_file import atomic_write_text, report_lock

    basis_date = _validate_basis_date(basis_date)
    html_path = Path(html_path)
    md_path = html_path.with_suffix(".md")
    written = []
    with report_lock(html_path):
        html_original = html_path.read_text(encoding="utf-8")
        html_parsed = _parse_html_report(html_original, basis_date)
        html_updated = _updated_html(html_original, html_parsed)
        md_original = md_updated = None
        if md_path.exists():
            with report_lock(md_path):
                md_original = md_path.read_text(encoding="utf-8")
                md_parsed = _parse_markdown_report(
                    md_original, basis_date, html_parsed["ctx"]["components"]
                )
                md_updated = _updated_markdown(md_original, md_parsed, html_parsed["ctx"])
                try:
                    if html_updated != html_original:
                        _backup_once(html_path, html_original)
                    if md_updated != md_original:
                        _backup_once(md_path, md_original)
                    if html_updated != html_original:
                        atomic_write_text(html_path, html_updated)
                        written.append(html_path)
                    if md_updated != md_original:
                        atomic_write_text(md_path, md_updated)
                        written.append(md_path)
                except Exception as exc:
                    raise ReportUpdateError(
                        f"report_write_failed: {exc}", written_paths=written
                    ) from exc
        else:
            try:
                if html_updated != html_original:
                    _backup_once(html_path, html_original)
                    atomic_write_text(html_path, html_updated)
                    written.append(html_path)
            except Exception as exc:
                raise ReportUpdateError(
                    f"report_write_failed: {exc}", written_paths=written
                ) from exc

    return {
        "status": "updated" if written else "unchanged",
        "html_path": str(html_path.resolve()),
        "markdown_path": str(md_path.resolve()) if md_path.exists() else None,
        "basis_date": basis_date,
        "written_paths": [str(path.resolve()) for path in written],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="补充每日复盘五项评分详情链接")
    parser.add_argument("--html-path", required=True, type=Path)
    parser.add_argument("--basis-date", required=True)
    args = parser.parse_args(argv)
    try:
        _validate_basis_date(args.basis_date)
    except ValueError:
        parser.error("--basis-date 必须为 YYYY-MM-DD")
    try:
        result = update_reports(args.html_path, args.basis_date)
    except ReportUpdateError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
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


__all__ = ["ReportUpdateError", "update_reports"]
