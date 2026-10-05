"""Synchronize one frozen observation state into HTML and Markdown reports."""

from pathlib import Path

from core.report_file import atomic_write_text, report_lock


def _replace_block(original, replacement, block_start, block_end):
    start = original.find(block_start)
    end = original.find(block_end, start + len(block_start)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ValueError("observation_block_missing")
    end += len(block_end)
    return original[:start] + replacement + original[end:]


def _update_file(path, replacement, block_start, block_end):
    original = path.read_text(encoding="utf-8")
    updated = _replace_block(original, replacement, block_start, block_end)
    if updated != original:
        atomic_write_text(path, updated)
    return {"status": "updated", "path": str(path.resolve()),
            "changed": updated != original}


def _failure(path, exc):
    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    return {"status": "failed", "path": str(path.resolve()), "reason": reason}


def update_observation_reports(
        html_path, markdown_path, *, state=None, pending=False,
        render_html, render_markdown, block_start, block_end):
    """Atomically update each report under a shared, consistently ordered lock.

    The two visible files cannot be replaced as one filesystem transaction.  A
    per-file result therefore records partial failure, and the operation is
    deliberately idempotent so a rerun repairs whichever target did not land.
    """
    html_path = Path(html_path)
    markdown_path = Path(markdown_path)
    if html_path.resolve() == markdown_path.resolve():
        raise ValueError("observation_report_paths_must_differ")

    frozen_state = state or {"status": "ready", "items": []}
    html_block = render_html(frozen_state, pending=pending)
    markdown_block = render_markdown(frozen_state, pending=pending)
    files = {}

    # The HTML report is the shared coordination path used by the other
    # background enrichers.  All observation updates acquire it first, then
    # the Markdown lock, so concurrent read/modify/write operations serialize.
    with report_lock(html_path):
        try:
            files["html"] = _update_file(
                html_path, html_block, block_start, block_end)
        except (OSError, UnicodeError, ValueError) as exc:
            files["html"] = _failure(html_path, exc)

        with report_lock(markdown_path):
            try:
                files["markdown"] = _update_file(
                    markdown_path, markdown_block, block_start, block_end)
            except (OSError, UnicodeError, ValueError) as exc:
                files["markdown"] = _failure(markdown_path, exc)

    failures = [name for name, detail in files.items() if detail["status"] == "failed"]
    result = {
        "status": "degraded" if failures else ("pending" if pending else "completed"),
        "observation_status": "pending" if pending else frozen_state.get("status", "unavailable"),
        "count": 0 if pending else len(frozen_state.get("items") or []),
        "html_path": str(html_path.resolve()),
        "markdown_path": str(markdown_path.resolve()),
        "files": files,
    }
    if failures:
        result["partial_failures"] = failures
    return result


__all__ = ["update_observation_reports"]
