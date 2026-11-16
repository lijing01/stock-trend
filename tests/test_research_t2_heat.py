#!/usr/bin/env python3
"""Focused tests for the offline T2 heat research tool."""

import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "research_t2_heat.py"
SPEC = importlib.util.spec_from_file_location("research_t2_heat", MODULE_PATH)
research = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(research)


def row(symbol, day, expected, heat, net):
    return {
        "symbol": symbol, "signal_date": day, "event_id": f"event-{symbol}",
        "net_return": net, "research_expected_net_return": expected,
        "research_estimated_win_rate": .5,
        "features": {"features": {"close_to_ma20": heat}},
    }


class T2HeatResearchTests(unittest.TestCase):
    def test_filter_reads_nested_signal_day_feature(self):
        self.assertTrue(research.passes_heat_filter(row("A", "2026-01-02", .1, .05, .01)))
        self.assertFalse(research.passes_heat_filter(row("B", "2026-01-02", .1, .0501, .01)))

    def test_filter_then_rank_can_replace_hot_baseline_rows(self):
        rows = [
            row("HOT", "2026-01-02", .09, .08, -.03),
            row("COOL1", "2026-01-02", .08, .04, .02),
            row("COOL2", "2026-01-02", .07, .03, .01),
        ]
        baseline = research.select_daily(rows, 2, heat_filter=False)
        heat = research.select_daily(rows, 2, heat_filter=True)
        self.assertEqual(["HOT", "COOL1"], [item["symbol"] for item in baseline])
        self.assertEqual(["COOL1", "COOL2"], [item["symbol"] for item in heat])
        diagnostics = research.selection_diagnostics(rows, baseline, heat, 2)
        self.assertEqual(1, diagnostics["baseline_selected_removed_by_filter"])
        self.assertEqual(1, diagnostics["heat_replacements_vs_baseline"])

    def test_bootstrap_retains_complete_calendar_and_is_deterministic(self):
        baseline = [row("A", "2026-01-02", .1, .08, -.03)]
        heat = [row("B", "2026-01-02", .1, .02, .02)]
        calendar = ["2026-01-02", "2026-01-05", "2026-01-06"]
        first = research.moving_block_bootstrap(
            baseline, heat, calendar, draws=50, seed=7, block_length=2)
        second = research.moving_block_bootstrap(
            baseline, heat, calendar, draws=50, seed=7, block_length=2)
        self.assertEqual(first, second)
        self.assertTrue(first["zero_signal_sessions_retained"])
        self.assertEqual("complete_trading_session_calendar", first["cluster"])
        self.assertEqual(1.0, first["win_rate"]["difference"])
        self.assertAlmostEqual(.05, first["mean_net_return"]["difference"])

    def test_blocks_do_not_wrap_from_calendar_end_to_start(self):
        blocks = research.contiguous_blocks(
            ["2026-01-01", "2026-01-02", "2026-01-03"], 2)
        self.assertEqual([
            ["2026-01-01", "2026-01-02"],
            ["2026-01-02", "2026-01-03"],
        ], blocks)

    def test_immature_selection_censors_both_arms_for_the_day(self):
        baseline = [row("OPEN", "2026-01-02", .2, .08, None)]
        heat = [row("CLOSED", "2026-01-02", .1, .02, .01)]
        clean_baseline, clean_heat, metadata = research.paired_mature_days(
            baseline, heat)
        self.assertEqual([], clean_baseline)
        self.assertEqual([], clean_heat)
        self.assertEqual(["2026-01-02"], metadata["censored_days"])

    def test_source_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.jsonl"
            source.write_text("", encoding="utf-8")
            bundle = {"training_source_identity": {"sources": [{
                "path": str(source), "sha256": hashlib.sha256(b"changed").hexdigest(),
            }]}}
            with self.assertRaisesRegex(ValueError, "source sha256 mismatch"):
                research._load_records(bundle)

    def test_month_training_excludes_future_and_unclosed_labels(self):
        calendar = [f"2025-12-{day:02d}" for day in range(1, 32)] + [
            f"2026-01-{day:02d}" for day in range(1, 10)]
        calendar = sorted(calendar)

        def training_row(symbol, signal_date, exit_date):
            return {
                "symbol": symbol, "signal_date": signal_date, "exit_date": exit_date,
                "event_id": f"event-{symbol}", "probe_pattern": "T2",
                "features": {"feature_asof": signal_date}, "net_return": .01,
            }

        rows = [
            training_row("READY", "2025-12-20", "2025-12-25"),
            training_row("PURGED", "2025-12-28", "2025-12-29"),
            training_row("UNCLOSED", "2025-12-20", "2026-01-02"),
            training_row("FUTURE", "2026-01-02", "2026-01-03"),
        ]
        eligible, boundaries = research.training_records_for_month(
            rows, calendar, "2026-01")
        self.assertEqual(["READY"], [item["symbol"] for item in eligible])
        self.assertEqual("2025-12-26", boundaries["purge_cutoff"])
        self.assertEqual("2025-12-31", boundaries["training_cutoff"])

    def test_month_with_fewer_than_120_t2_signal_days_is_not_scored(self):
        calendar = [f"2025-12-{day:02d}" for day in range(1, 32)] + [
            f"2026-01-{day:02d}" for day in range(1, 10)]
        scored, metadata = research._score_month([], [], sorted(calendar), "2026-01")
        self.assertEqual([], scored)
        self.assertEqual("training_window_insufficient", metadata["status"])

    def test_official_scorer_fallback_is_excluded_with_reason(self):
        candidate = row("MISSING", "2026-01-02", .1, .02, .01)
        candidate["probe_pattern"] = "T2"
        with patch.object(research, "score_v4_shadow_record", return_value={
            "score_status": "fallback_v3",
            "fallback_reason": "required_feature_missing:ma10_to_ma20",
        }):
            scored, reasons = research.score_validation_records(
                {}, [candidate], "2026-01")
        self.assertEqual([], scored)
        self.assertEqual(1, reasons["required_feature_missing:ma10_to_ma20"])


if __name__ == "__main__":
    unittest.main()
