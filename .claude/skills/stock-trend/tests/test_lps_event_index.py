#!/usr/bin/env python3
"""Formal SOS/LPS index contracts, separate from Phase D shadow output."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.lps_event_index import (
    build_daily_index, load_prior_lps, save_daily_index,
)
from scans.stock_scanner import gather_candidates
from scans import daily_candidates as dc


def formal_candidate():
    sos = {
        "type": "sos", "status": "confirmed", "event_index": 10,
        "detected_index": 11, "event_date": "2026-09-16",
        "detected_date": "2026-09-17", "range_id": "r1",
        "structure_level": "single",
    }
    lps = {
        "type": "lps", "status": "confirmed", "event_index": 13,
        "detected_index": 14, "event_date": "2026-09-18",
        "detected_date": "2026-09-21", "range_id": "r1",
        "structure_level": "single", "parent_event": "sos",
        "parent_event_index": 10,
    }
    identity = {
        "event": "lps", "event_date": lps["event_date"],
        "confirmation_date": lps["detected_date"], "range_id": "r1",
        "status": "confirmed", "event_index": 13, "detected_index": 14,
    }
    return {
        "code": "600001", "ts_code": "600001.SH", "name": "测试股",
        "sector_memberships": [{"code": "BK001", "name": "测试行业"}],
        "wyckoff": {
            "confirmed_event": lps,
            "event_health": {
                "event_type": "lps", "state": "confirmed_holding",
                "event_identity": identity,
            },
            "_formal_event_history": [sos, lps],
            "_phase_d_lps_context": {"shadow_only": True},
        },
    }


class TestFormalLpsIndex(unittest.TestCase):
    def test_requires_confirmed_parent_and_health(self):
        candidate = formal_candidate()
        result = build_daily_index([candidate], "2026-09-21")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["events"]["sos"]["type"], "sos")
        self.assertNotIn("shadow_only", str(result["records"]))
        candidate["wyckoff"]["event_health"]["state"] = "failed_breakout"
        self.assertEqual(build_daily_index([candidate], "2026-09-21")["records"], [])
        candidate = formal_candidate()
        candidate["wyckoff"]["_formal_event_history"] = [
            candidate["wyckoff"]["_formal_event_history"][1]]
        self.assertEqual(build_daily_index([candidate], "2026-09-21")["records"], [])

    def test_five_sessions_deduplicate_and_exclude_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            complete = build_daily_index([formal_candidate()], "2026-09-21")
            save_daily_index(complete, root)
            repeat = build_daily_index([formal_candidate()], "2026-09-22")
            save_daily_index(repeat, root)
            incomplete = build_daily_index(
                [formal_candidate()], "2026-09-23", scope_complete=False)
            save_daily_index(incomplete, root)
            records, meta = load_prior_lps(
                "2026-09-24",
                ["2026-09-17", "2026-09-18", "2026-09-21",
                 "2026-09-22", "2026-09-23", "2026-09-24"], root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["observation_count"], 2)
            self.assertEqual(meta["status"], "partial")
            self.assertIn("2026-09-23", meta["incomplete_dates"])

    def test_membership_expansion_is_sector_specific(self):
        calls = {}

        def stocks(sector_code, top_n=25, **_kwargs):
            calls[sector_code] = top_n
            codes = (["600001", "600002"] if sector_code == "BK1"
                     else ["600002", "600003"])
            return [{
                "code": code, "name": f"测试{code}",
                "market_cap": 1e10, "change_pct": 1, "amount": 1e8,
            } for code in codes[:top_n]]

        context = {
            code: {"name": code, "hot_score": 70}
            for code in ("BK1", "BK2")
        }
        with patch("fetchers.sector_data.get_sector_stocks", side_effect=stocks):
            result = gather_candidates(
                ["BK1", "BK2"], top_n_per_sector=1,
                sector_context=context,
                include_codes={"BK1": {"600002"}})
        self.assertEqual(calls, {"BK1": 500, "BK2": 1})
        by_code = {row["code"]: row for row in result["candidates"]}
        self.assertEqual(set(by_code), {"600001", "600002"})
        self.assertFalse(by_code["600002"]["historical_lps_included"])
        self.assertEqual(
            {row["code"] for row in by_code["600002"]["sector_memberships"]},
            {"BK1", "BK2"})

        with patch("fetchers.sector_data.get_sector_stocks", side_effect=stocks):
            result = gather_candidates(
                ["BK1"], top_n_per_sector=1,
                sector_context=context,
                include_codes={"BK1": {"600002"}})
        by_code = {row["code"]: row for row in result["candidates"]}
        self.assertTrue(by_code["600002"]["historical_lps_included"])

    def test_added_prior_candidate_requires_current_healthy_lps(self):
        previous = formal_candidate()
        raw = {
            "code": "600001", "ts_code": "600001.SH", "name": "测试股",
            "sector_code": "BK1", "sector_name": "测试行业",
            "historical_lps_included": True,
            "sector_memberships": [{
                "code": "BK1", "name": "测试行业", "hot_score": 70,
                "sector_actionable": True,
            }],
        }

        def run_phase2(candidates, **_kwargs):
            self.assertEqual(candidates[0]["code"], "600001")
            return [{
                **candidates[0], "composite_score": 80,
                "quality_adjusted_score": 80,
                "data_quality": {"eligible": True},
                "wyckoff": {
                    "signal": {"event": "lps", "status": "confirmed",
                               "age_bars": 2},
                    "event_health": {"state": self.current_state},
                    "short_term": {"sub_phase": "lps",
                                   "signal_status": "confirmed",
                                   "signal_age_bars": 2},
                },
            }]

        context = {"BK1": {"name": "测试行业", "hot_score": 70,
                           "sector_actionable": True}}
        for state, expected in (("confirmed_holding", 1),
                                ("failed_breakout", 0)):
            self.current_state = state
            with self.subTest(state=state), patch.object(
                    dc, "gather_candidates",
                    return_value={"candidates": [dict(raw)]}), patch.object(
                    dc, "run_phase2", side_effect=run_phase2):
                metrics = {}
                result = dc.scan_sectors(
                    ["BK1"], sector_context=context, metrics=metrics,
                    prior_lps_records=[previous], min_candidates=99)
                self.assertEqual(len(result), expected)
                self.assertEqual(metrics["prior_lps_revalidated_count"], expected)

    def test_later_regular_sector_restores_candidate(self):
        previous = formal_candidate()
        history_extra = {
            "code": "600001", "ts_code": "600001.SH", "name": "测试股",
            "sector_code": "BK1", "sector_name": "行业一",
            "historical_lps_included": True,
        }
        regular = {
            **history_extra, "sector_code": "BK2", "sector_name": "行业二",
            "historical_lps_included": False,
        }
        calls = []

        def gather(sectors, **_kwargs):
            return {"candidates": [dict(
                history_extra if sectors == ["BK1"] else regular)]}

        def score(candidates, **_kwargs):
            calls.append(candidates[0]["sector_code"])
            return [{
                **candidates[0], "composite_score": 80,
                "quality_adjusted_score": 80,
                "data_quality": {"eligible": True},
                "wyckoff": {
                    "signal": {"event": "spring", "status": "confirmed",
                               "age_bars": 1},
                    "event_health": {"state": "confirmed_holding"},
                },
            }]

        context = {
            code: {"name": code, "hot_score": 70,
                   "sector_actionable": True}
            for code in ("BK1", "BK2")
        }
        with patch.object(dc, "gather_candidates", side_effect=gather), \
                patch.object(dc, "run_phase2", side_effect=score):
            result = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1,
                sector_context=context, prior_lps_records=[previous],
                min_candidates=99)
        self.assertEqual(calls, ["BK1", "BK1"])
        self.assertEqual(len(result), 1)

    def test_incomplete_rerun_preserves_complete_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_daily_index(build_daily_index(
                [formal_candidate()], "2026-09-21"), root)
            save_daily_index(build_daily_index(
                [], "2026-09-21", scope_complete=False), root)
            records, _ = load_prior_lps(
                "2026-09-22", ["2026-09-21"], root)
            self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
