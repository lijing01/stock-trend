#!/usr/bin/env python3
"""Offline tests for synchronized observation report updates."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from reporting import observation_report_update as update  # noqa: E402


START = "<!-- OBSERVATION_LIST:START -->"
END = "<!-- OBSERVATION_LIST:END -->"


def render_html(state, *, pending=False):
    status = "pending" if pending else state["status"]
    return f"{START}<section>HTML:{status}:{len(state.get('items', []))}</section>{END}"


def render_markdown(state, *, pending=False):
    status = "pending" if pending else state["status"]
    return f"{START}\nMD:{status}:{len(state.get('items', []))}\n{END}"


class ObservationReportUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.html = self.root / "daily-review.html"
        self.markdown = self.root / "daily-review.md"
        self.html.write_text(f"html-before\n{render_html({}, pending=True)}\nhtml-after", encoding="utf-8")
        self.markdown.write_text(
            f"md-before\n{render_markdown({}, pending=True)}\nmd-after", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, state=None, *, pending=False):
        return update.update_observation_reports(
            self.html, self.markdown, state=state, pending=pending,
            render_html=render_html, render_markdown=render_markdown,
            block_start=START, block_end=END)

    def test_same_state_updates_html_and_markdown_idempotently(self):
        state = {"status": "ready", "items": [{"code": "001207"}]}
        first = self.call(state)
        second = self.call(state)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["files"]["html"]["status"], "updated")
        self.assertEqual(first["files"]["markdown"]["status"], "updated")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.html.read_text(encoding="utf-8").count(START), 1)
        self.assertEqual(self.markdown.read_text(encoding="utf-8").count(START), 1)
        self.assertIn("HTML:ready:1", self.html.read_text(encoding="utf-8"))
        self.assertIn("MD:ready:1", self.markdown.read_text(encoding="utf-8"))

    def test_partial_markdown_failure_is_explicit_and_rerun_recovers(self):
        state = {"status": "degraded", "items": [{"code": "001207"}]}
        real_write = update.atomic_write_text

        def fail_markdown(path, content):
            if Path(path) == self.markdown:
                raise OSError("fixture markdown failure")
            return real_write(path, content)

        with patch.object(update, "atomic_write_text", side_effect=fail_markdown):
            failed = self.call(state)

        self.assertEqual(failed["status"], "degraded")
        self.assertEqual(failed["files"]["html"]["status"], "updated")
        self.assertEqual(failed["files"]["markdown"]["status"], "failed")
        self.assertEqual(failed["partial_failures"], ["markdown"])
        self.assertIn("HTML:degraded:1", self.html.read_text(encoding="utf-8"))
        self.assertIn("MD:pending:0", self.markdown.read_text(encoding="utf-8"))

        recovered = self.call(state)
        self.assertEqual(recovered["status"], "completed")
        self.assertNotIn("partial_failures", recovered)
        self.assertIn("MD:degraded:1", self.markdown.read_text(encoding="utf-8"))

    def test_missing_marker_reports_each_failed_target_without_overwrite(self):
        self.markdown.write_text("unrelated markdown", encoding="utf-8")
        result = self.call({"status": "ready", "items": []})
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["files"]["markdown"]["reason"], "observation_block_missing")
        self.assertEqual(self.markdown.read_text(encoding="utf-8"), "unrelated markdown")


if __name__ == "__main__":
    unittest.main()
