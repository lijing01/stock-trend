#!/usr/bin/env python3
"""Regression test for the daily-candidates module's source syntax."""
import unittest
from pathlib import Path


class TestDailyCandidatesSyntax(unittest.TestCase):
    def test_daily_candidates_source_compiles(self):
        source_path = (
            Path(__file__).resolve().parent.parent
            / "scripts" / "scans" / "daily_candidates.py"
        )
        source = source_path.read_text(encoding="utf-8")
        compile(source, str(source_path), "exec")


if __name__ == "__main__":
    unittest.main()
