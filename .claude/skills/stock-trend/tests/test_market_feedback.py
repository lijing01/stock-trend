"""Tests for frozen strong-stock cohorts and next-session feedback."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analysis.market_feedback import (  # noqa: E402
    build_feedback, canonical_digest, compare_close_feedback,
)
from analysis.market_feedback_job import (  # noqa: E402
    COHORT_SCHEMA, collect_feedback,
)


def calendar(*days):
    return {
        "schema": "review-time/v1", "trading_dates": list(days),
        "coverage": {"start": days[0], "end": days[-1]}, "source": "test",
    }


def cohort(day="2026-09-29", members=None, frozen_at=None):
    value = {
        "schema_version": COHORT_SCHEMA, "basis_date": day,
        "frozen_at": frozen_at or f"{day}T15:20:00+08:00",
        "source": "test", "source_fetched_at": f"{day}T15:19:00+08:00",
        "selection_rules": {},
        "members": members or [
            {"code": "000001.SZ", "name": "甲", "streak": 2,
             "roles": ["limit_up", "height_core"], "selection_reason": "最高2板"},
            {"code": "000002.SZ", "name": "乙", "streak": 1,
             "roles": ["limit_up"], "selection_reason": ""},
        ],
    }
    value["content_sha256"] = canonical_digest(value)
    return value


def quote(code, pct=-1, *, status="normal", fetched="2026-09-30T15:20:00+08:00",
          day="2026-09-30", close=10):
    return {"code": code, "data_date": day, "pct_chg": pct, "close": close,
            "volume": 100, "status": status, "source": "test",
            "fetched_at": fetched}


class MarketFeedbackAnalysisTests(unittest.TestCase):
    def test_left_join_keeps_losers_suspended_and_missing(self):
        result = build_feedback(cohort(), [
            quote("000001.SZ", -8.5),
            quote("000002.SZ", status="suspended", pct=None, close=None),
        ], basis_date="2026-09-30", as_of="2026-09-30T16:00:00+08:00")
        self.assertEqual([row["feedback"] for row in result["members"]],
                         ["down", "suspended"])
        self.assertEqual(result["summary"]["down"], 1)
        self.assertEqual(result["summary"]["suspended"], 1)
        self.assertEqual(result["summary"]["coverage"], 0.5)
        self.assertEqual(result["summary"]["observed_coverage"], 1)
        self.assertEqual(result["summary"]["worst_pct_chg"], -8.5)

    def test_future_wrong_day_early_close_and_invalid_price_are_missing(self):
        members = [
            {"code": f"00000{i}.SZ", "name": str(i), "roles": ["limit_up"]}
            for i in range(1, 5)
        ]
        result = build_feedback(cohort(members=members), [
            quote("000001.SZ", day="2026-10-01"),
            quote("000002.SZ", fetched="2026-09-30T16:01:00+08:00"),
            quote("000003.SZ", fetched="2026-09-30T14:59:00+08:00"),
            quote("000004.SZ", close=0),
        ], basis_date="2026-09-30", as_of="2026-09-30T16:00:00+08:00")
        self.assertEqual(result["summary"]["missing"], 4)
        self.assertEqual(result["summary"]["coverage"], 0)

    def test_intraday_accepts_current_quote_but_never_compares_change(self):
        result = build_feedback(cohort(), [
            quote("000001.SZ", 1, fetched="2026-09-30T10:00:00+08:00")
        ], basis_date="2026-09-30", as_of="2026-09-30T10:01:00+08:00")
        self.assertEqual(result["mode"], "intraday")
        self.assertEqual(result["summary"]["up"], 1)
        compared = compare_close_feedback(result, result)
        self.assertEqual(compared["status"], "unavailable")
        self.assertIn("close_feedback_required", compared["reasons"])

    def test_comparison_requires_all_members_priced(self):
        complete = build_feedback(cohort(members=[cohort()["members"][0]]), [
            quote("000001.SZ", 2)
        ], basis_date="2026-09-30", as_of="2026-09-30T16:00:00+08:00")
        incomplete = copy.deepcopy(complete)
        incomplete["summary"]["coverage"] = 0
        self.assertEqual(compare_close_feedback(complete, incomplete)["status"],
                         "unavailable")
        comparison = compare_close_feedback(complete, complete)
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["positive_ratio_change_pp"], 0)


class MarketFeedbackJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        path = self.root / "strong_stock_cohorts" / "2026-09-29.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(cohort(), ensure_ascii=False), encoding="utf-8")
        self.ctx = {"data_date": "2026-09-30"}
        self.cal = calendar("2026-09-29", "2026-09-30")
        self.start = datetime.fromisoformat("2026-09-30T15:15:00+08:00")
        self.finish = datetime.fromisoformat("2026-09-30T15:20:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def fetched(self):
        return {
            "pool": {"data_date": "2026-09-30", "date_origin": "provider",
                     "complete": True, "source": "test", "mode": "close",
                     "fetched_at": self.finish.isoformat(), "members": [
                         {"code": "000003.SZ", "name": "丙", "close": 8, "streak": 3},
                         {"code": "000004.SZ", "name": "丁", "close": 9, "streak": 3},
                         {"code": "000005.SZ", "name": "戊", "close": 7, "streak": 1},
                     ]},
            "quotes": [quote("000001.SZ", -3), quote("000002.SZ", -6)],
            "errors": [],
        }

    def run_collect(self, fetched=None):
        with patch("analysis.market_feedback_job._bounded_fetch",
                   return_value=fetched or self.fetched()), patch(
                       "analysis.market_feedback_job._now",
                       side_effect=[self.start, self.finish]):
            return collect_feedback(
                self.ctx, calendar=self.cal, as_of=self.start,
                cache_dir=self.root)

    def test_collect_tracks_all_prior_losers_and_freezes_all_current_limit_ups(self):
        state = self.run_collect()
        self.assertEqual([item["status"] for item in state["items"]],
                         ["down", "down"])
        self.assertEqual(state["summary"]["negative_ratio"], 1)
        frozen = json.loads((self.root / "strong_stock_cohorts" /
                             "2026-09-30.json").read_text(encoding="utf-8"))
        self.assertEqual(len(frozen["members"]), 3)
        height = [row["code"] for row in frozen["members"]
                  if "height_core" in row["roles"]]
        self.assertEqual(height, ["000003.SZ", "000004.SZ"])
        self.assertIn("非正式市场核心", frozen["members"][0]["selection_reason"])
        self.assertTrue(Path(state["artifact_path"]).is_file())
        self.assertEqual(self.ctx["market_feedback_binding"], state["run_binding"])

    def test_first_eligible_cohort_is_immutable(self):
        self.run_collect()
        path = self.root / "strong_stock_cohorts" / "2026-09-30.json"
        first = path.read_bytes()
        changed = self.fetched()
        changed["pool"]["members"] = [{"code": "999999.SH", "streak": 9}]
        self.run_collect(changed)
        self.assertEqual(path.read_bytes(), first)

    def test_calendar_outage_preserves_today_but_cannot_select_yesterday(self):
        self.cal = None
        state = self.run_collect()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["items"], [])
        self.assertIn("calendar_invalid", state["reasons"])
        self.assertEqual(state["cohort_freeze"]["status"], "created")
        self.assertEqual(state["cohort_freeze"]["calendar_status"], "calendar_invalid")
        self.assertTrue((self.root / "strong_stock_cohorts" / "2026-09-30.json").exists())

    def test_corrupt_existing_cohort_is_not_overwritten_or_claimed_usable(self):
        path = self.root / "strong_stock_cohorts" / "2026-09-30.json"
        path.write_text("[]", encoding="utf-8")
        state = self.run_collect()
        self.assertEqual(state["cohort_freeze"]["status"], "existing_cohort_invalid")
        self.assertIsNone(state["cohort_freeze"]["path"])
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_missing_adjacent_cohort_still_freezes_today_but_hides_empty_ratios(self):
        (self.root / "strong_stock_cohorts" / "2026-09-29.json").unlink()
        state = self.run_collect()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["summary"], {})
        self.assertIn("adjacent_cohort_missing", state["reasons"])
        self.assertTrue((self.root / "strong_stock_cohorts" /
                         "2026-09-30.json").is_file())

    def test_offline_load_uses_only_exact_binding_and_detects_tamper(self):
        state = self.run_collect()
        later = datetime.fromisoformat("2026-09-30T16:00:00+08:00")
        offline = collect_feedback(
            self.ctx, calendar=None, as_of=later, cache_dir=self.root,
            no_refresh=True)
        self.assertEqual(offline["items"], state["items"])
        path = Path(state["artifact_path"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["state"]["items"] = []
        path.write_text(json.dumps(raw), encoding="utf-8")
        missing = collect_feedback(
            self.ctx, calendar=None, as_of=later, cache_dir=self.root,
            no_refresh=True)
        self.assertEqual(missing["reason"], "market_feedback_binding_missing")

    def test_historical_live_collection_is_forbidden_without_fetch(self):
        with patch("analysis.market_feedback_job._now",
                   return_value=datetime.fromisoformat("2026-10-01T09:00:00+08:00")), patch(
                       "analysis.market_feedback_job._bounded_fetch") as fetch:
            state = collect_feedback(
                self.ctx, calendar=self.cal, as_of=self.start,
                cache_dir=self.root)
        fetch.assert_not_called()
        self.assertEqual(state["reason"], "historical_live_collection_forbidden")

    def test_intraday_pool_cannot_freeze_even_if_fetch_finishes_after_close(self):
        fetched = self.fetched()
        fetched["pool"]["mode"] = "intraday"
        state = self.run_collect(fetched)
        self.assertEqual(state["cohort_freeze"]["status"],
                         "current_cohort_not_freezable")
        self.assertFalse((self.root / "strong_stock_cohorts" /
                          "2026-09-30.json").exists())

    def test_wrong_day_or_late_frozen_prior_cohort_is_rejected(self):
        bad = cohort(frozen_at="2026-09-30T09:00:00+08:00")
        path = self.root / "strong_stock_cohorts" / "2026-09-29.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        state = self.run_collect()
        self.assertIn("adjacent_cohort_missing", state["reasons"])


if __name__ == "__main__":
    unittest.main()
