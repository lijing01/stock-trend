"""Regression tests for the pipeline's optional JSON input reader."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pipeline import runner


class TestReadJson(unittest.TestCase):
    def test_reads_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            self.assertEqual(runner.read_json(path), {"ok": True})

    def test_missing_and_malformed_inputs_keep_optional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            self.assertIsNone(runner.read_json(path))
            path.write_text("{bad", encoding="utf-8")
            self.assertIsNone(runner.read_json(path))
            path.write_text("[" * (sys.getrecursionlimit() + 100) + "0" +
                            "]" * (sys.getrecursionlimit() + 100),
                            encoding="utf-8")
            self.assertIsNone(runner.read_json(path))

    def test_unexpected_programming_error_is_visible(self):
        with patch.object(runner.json, "load", side_effect=RuntimeError("bug")):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                runner.read_json(__file__)


if __name__ == "__main__":
    unittest.main()
