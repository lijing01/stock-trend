#!/usr/bin/env python3
"""Focused reporting tests for the parallel weak-to-strong v2 research view."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from reporting import weak_to_strong as report


def row(code, *, evidence="ready", pattern="base_consolidation", slopes=(.01, .01),
        extended=False, distance=.2, volume=1.1, stage="LOCAL_CONFIRMATION"):
    return {
        "code": code, "name": "股<" + code + ">", "as_of_date": "2026-10-22",
        "origin_pattern_type": pattern, "pattern_type": pattern, "stage": stage,
        "evidence_status": evidence, "signal_date": "2026-10-22",
        "risk_flags": ["extended"] if extended else [],
        "metrics": {"close": 12.03, "ma10": 11.3, "ma20": 10.8,
                    "ma10_slope_3": slopes[0], "ma20_slope_3": slopes[1],
                    "h5": 11.58, "h10": 11.8, "h20": 13.9,
                    "trigger_distance_atr": distance, "volume_prior5_ratio": volume,
                    "close_strength": .8},
        "structure": {"anchor_high": 13.9}, "checks": {"holds_trigger_level": True},
    }


class WeakToStrongV2ReportingTests(unittest.TestCase):
    def test_sort_uses_evidence_ma_risk_pattern_quality_and_code(self):
        rows = [
            row("8", evidence="data_insufficient"),
            row("7", slopes=(-.01, -.01)),
            row("6", extended=True),
            row("5", pattern="deep_repair"),
            row("4", pattern="short_pullback"),
            row("3", distance=.5),
            row("2", distance=.2, volume=.9),
            row("1", distance=.2, volume=1.2),
        ]
        self.assertEqual(["4", "1", "2", "3", "5", "6", "7", "8"],
                         [item["code"] for item in report.sort_v2_rows(rows)])
        self.assertEqual([], report.sort_v2_rows("bad"))

    def test_stage_groups_have_same_order_in_markdown_and_html(self):
        payload = {
            "schema_version": "daily-weak-to-strong/v2",
            "summary": {"stock_count": 4, "new_event_count": 3,
                        "validation_status": "continue_accumulating"},
            "range_breakout": [row("4", stage="RANGE_BREAKOUT")],
            "local_confirmation": [row("3")],
            "early_repair": [row("2", stage="EARLY_REPAIR")],
            "preparing": [row("1", stage="PREPARING")],
            "provisional": [], "diagnostics": [],
        }
        for rendered in (report.render_v2_markdown(payload), report.render_v2_html(payload)):
            self.assertLess(rendered.index("股&lt;4&gt;" if rendered.startswith("<") else "股<4>"),
                            rendered.index("股&lt;3&gt;" if rendered.startswith("<") else "股<3>"))
            self.assertLess(rendered.index("股&lt;3&gt;" if rendered.startswith("<") else "股<3>"),
                            rendered.index("股&lt;2&gt;" if rendered.startswith("<") else "股<2>"))
            self.assertLess(rendered.index("股&lt;2&gt;" if rendered.startswith("<") else "股<2>"),
                            rendered.index("股&lt;1&gt;" if rendered.startswith("<") else "股<1>"))
            self.assertIn("continue_accumulating", rendered)
            self.assertIn("父高 13.9", rendered)

    def test_html_escapes_all_values_and_markdown_escapes_pipes(self):
        item = row("1")
        item["name"] = "<script>|股"
        item["risk_flags"] = ["<risk>"]
        payload = {"summary": {}, "range_breakout": [], "local_confirmation": [item],
                   "early_repair": [], "preparing": [], "provisional": [],
                   "diagnostics": [{"code": "<x>", "reason_codes": ["bad<script>"]}]}
        html = report.render_v2_html(payload)
        markdown = report.render_v2_markdown(payload)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;|股", html)
        self.assertIn("&lt;risk&gt;", html)
        self.assertIn("<script>\\|股", markdown)
        self.assertIn("<details><summary>v2 诊断</summary>", markdown)

    def test_v1_rendering_remains_unchanged_by_v2_payload(self):
        payload = {"scope": {"scope_complete": True, "collector_status": "ready"},
                   "summary": {"evaluated": 0, "data_insufficient": 0},
                   "confirmed": [], "watching": [], "pullback": [], "provisional": [],
                   "diagnostics": [], "weak_to_strong_v2": {"local_confirmation": [row("1")]}}
        self.assertNotIn("v2", report.render_markdown(payload))
        self.assertNotIn("v2", report.render_html(payload))

    def test_selection_shares_thirty_slots_across_stages_and_deduplicates(self):
        payload = {
            "schema_version": "daily-weak-to-strong/v2",
            "summary": {"stock_count": 37, "new_event_count": 37,
                        "validation_status": "continue_accumulating"},
            "range_breakout": [
                row(f"{index:06d}", stage="RANGE_BREAKOUT", distance=index / 100)
                for index in range(1, 30)
            ],
            "local_confirmation": [
                row("000001", stage="LOCAL_CONFIRMATION", distance=.01),
                row("100001", stage="LOCAL_CONFIRMATION", distance=.01),
                row("100002", stage="LOCAL_CONFIRMATION", distance=.02),
            ],
            "early_repair": [row("200001", stage="EARLY_REPAIR")],
            "preparing": [row("300001", stage="PREPARING")],
            "provisional": [row("400001", stage="PREPARING")],
            "diagnostics": [],
        }
        payload["provisional"][0]["provisional"] = True
        payload["preparing"].append(
            row("300002", stage="PREPARING", evidence="data_insufficient"))

        selected, excluded = report.select_v2_rows(payload)

        self.assertEqual(30, sum(len(items) for items in selected.values()))
        self.assertEqual(29, len(selected["range_breakout"]))
        self.assertEqual(["100001"], [item["code"] for item in selected["local_confirmation"]])
        self.assertEqual([], selected["early_repair"])
        self.assertEqual([], selected["preparing"])
        selected_codes = [item["code"] for items in selected.values() for item in items]
        self.assertEqual(30, len(set(selected_codes)))
        self.assertGreaterEqual(sum(excluded.values()), 6)
        self.assertEqual(29, len(payload["range_breakout"]),
                         "selection must retain the complete research payload")

        for rendered in (report.render_v2_markdown(payload), report.render_v2_html(payload)):
            self.assertIn("股&lt;100001&gt;" if rendered.startswith("<") else "股<100001>", rendered)
            self.assertNotIn("股&lt;100002&gt;" if rendered.startswith("<") else "股<100002>", rendered)
            self.assertIn("展示 30", rendered)

    def test_selection_is_deterministic_when_input_order_changes(self):
        rows = [row(str(index), distance=.2, volume=1.0) for index in range(35, 0, -1)]
        payload = {"range_breakout": [], "local_confirmation": rows,
                   "early_repair": [], "preparing": [], "provisional": [],
                   "diagnostics": []}
        reversed_payload = dict(payload, local_confirmation=list(reversed(rows)))

        selected, _ = report.select_v2_rows(payload)
        reversed_selected, _ = report.select_v2_rows(reversed_payload)

        self.assertEqual([item["code"] for item in selected["local_confirmation"]],
                         [item["code"] for item in reversed_selected["local_confirmation"]])


if __name__ == "__main__":
    unittest.main()
