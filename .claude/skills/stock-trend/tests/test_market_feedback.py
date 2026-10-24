"""Tests for frozen strong-stock cohorts and next-session feedback."""

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analysis.market_feedback import (  # noqa: E402
    build_feedback, canonical_digest, compare_close_feedback,
)
from analysis.market_feedback_job import (  # noqa: E402
    COHORT_SCHEMA, collect_feedback, resolve_feedback_session,
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
            "fetched_at": fetched, "mode": "close", "close_confirmed": True,
            "pct_chg_origin": "provider", "price_basis": "native",
            "source_time_raw": day, "finality": "live_daily_close"}


class MarketFeedbackAnalysisTests(unittest.TestCase):
    def test_intraday_cache_and_derived_returns_cannot_become_close_feedback(self):
        for change in ({"close_confirmed": False}, {"pct_chg_origin": "derived"},
                       {"price_basis": "qfq"}, {"mode": None}, {"mode": "unknown"},
                       {"finality": None}, {"source_time_raw": None},
                       {"source_time_raw": "2026-09-29"}):
            row = quote("000001.SZ", 3)
            row.update(change)
            result = build_feedback(cohort(), [row], basis_date="2026-09-30",
                                    as_of="2026-09-30T16:00:00+08:00")
            self.assertEqual(result["summary"]["available"], 0)

    def test_rule_versions_are_not_comparable(self):
        result = build_feedback(cohort(), [quote("000001.SZ"), quote("000002.SZ")],
                                basis_date="2026-09-30", as_of="2026-09-30T16:00:00+08:00")
        old = copy.deepcopy(result)
        old["rule_version"] = "strong-stock-next-session-feedback/v1"
        self.assertIn("feedback_rule_mismatch", compare_close_feedback(result, old)["reasons"])

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
                cache_dir=self.root, refresh_mode="live_refresh")

    def test_stale_market_date_does_not_block_independent_today_feedback(self):
        self.ctx["data_date"] = "2026-09-29"
        state = self.run_collect()
        self.assertEqual(state["market_basis_date"], "2026-09-29")
        self.assertEqual(state["feedback_basis_date"], "2026-09-30")
        self.assertEqual(state["summary"]["available"], 2)
        self.assertIn("market_context_stale", state["advisories"])
        offline = collect_feedback(self.ctx, calendar=None, as_of=self.finish,
                                   cache_dir=self.root, no_refresh=True)
        self.assertEqual(offline["items"], state["items"])
        self.ctx["market_feedback_binding"]["market_basis_date"] = "2026-09-28"
        invalid = collect_feedback(self.ctx, calendar=None, as_of=self.finish,
                                   cache_dir=self.root, no_refresh=True)
        self.assertEqual(invalid["reason"], "market_feedback_binding_missing")

    def test_all_33_failures_keep_null_statistics_and_error_evidence(self):
        members = [{"code": f"{600000+i}.SH", "roles": ["limit_up"]} for i in range(33)]
        p = self.root / "strong_stock_cohorts" / "2026-09-29.json"
        p.write_text(json.dumps(cohort(members=members)), encoding="utf-8")
        data = self.fetched()
        data["quotes"] = [{"code": x["code"], "status": "missing",
                           "reason": "provider_error", "provider_attempts": [
                               {"provider": "eastmoney", "reason": "provider_error",
                                "error": "connection closed"}]} for x in members]
        data["errors"] = [{"stage": "quote", "code": x["code"],
                           "reason": "provider_error", "error": "connection closed"} for x in members]
        state = self.run_collect(data)
        self.assertEqual(len(state["items"]), 33)
        self.assertEqual(state["summary"]["missing"], 33)
        self.assertIsNone(state["summary"]["mean_pct_chg"])
        self.assertEqual(state["items"][0]["reason"], "provider_error")
        artifact = json.loads(Path(state["artifact_path"]).read_text())
        self.assertEqual(len(artifact["inputs"]["errors"]), 33)

    def test_outer_fetch_timeout_is_visible_on_every_unpriced_member(self):
        state = self.run_collect({"pool": {}, "quotes": [], "errors": ["fetch_timeout"]})
        self.assertEqual(state["summary"]["missing"], 2)
        self.assertTrue(all(x["reason"] == "timeout" for x in state["items"]))

    def test_native_backup_recovery_round_trips_a_stale_market_binding(self):
        from fetchers import market_feedback as fetcher
        fixtures = json.loads((Path(__file__).parent / "fixtures" /
                               "tencent_native_quote_20261023.json").read_text())
        yesterday = cohort(day="2026-10-22", members=[
            {"code": "600519.SH", "roles": ["limit_up"]},
            {"code": "000001.SZ", "roles": ["limit_up"]}])
        directory = self.root / "strong_stock_cohorts"
        (directory / "2026-10-22.json").write_text(json.dumps(yesterday))
        ctx = {"data_date": "2026-10-22"}
        start = datetime.fromisoformat("2026-10-23T16:34:00+08:00")
        finish = datetime.fromisoformat("2026-10-23T16:35:00+08:00")

        def response(url, **kwargs):
            if "getTopicZTPool" in url:
                return json.dumps({"data": {"qdate": "20261023", "tc": 1, "pool": [
                    {"c": "600519", "n": "fixture", "p": 1335000, "lbc": 1}]}})
            code = "600519.SH" if "sh600519" in url else "000001.SZ"
            return json.dumps(fixtures["responses"][code])

        with patch.object(fetcher, "fetch_url", side_effect=response), \
                patch.object(fetcher, "fetch_eastmoney", side_effect=RuntimeError("primary failed")), \
                patch.object(fetcher, "_now", return_value=finish), \
                patch("analysis.market_feedback_job._now", side_effect=[start, finish]):
            state = collect_feedback(ctx, calendar=calendar("2026-10-22", "2026-10-23"),
                                     as_of=start, cache_dir=self.root, refresh_mode="live_refresh")
        self.assertEqual(state["summary"]["coverage"], 1)
        self.assertEqual([x["pct_chg"] for x in state["items"]], [1.25, -.88])
        self.assertTrue(all(x["quote_source"] == "tencent_native_quote" for x in state["items"]))
        self.assertIn("primary_provider_failed_but_recovered", state["advisories"])
        replay = collect_feedback(ctx, calendar=None, as_of=finish, cache_dir=self.root, no_refresh=True)
        self.assertEqual(replay["items"], state["items"])

    def test_calendar_outage_never_passes_yesterday_members_to_fetcher(self):
        self.cal = None
        with patch("analysis.market_feedback_job._bounded_fetch", return_value=self.fetched()) as fetch, \
                patch("analysis.market_feedback_job._now", side_effect=[self.start, self.finish]):
            state = collect_feedback(self.ctx, calendar=None, as_of=self.start,
                                     cache_dir=self.root, refresh_mode="live_refresh")
        self.assertEqual(fetch.call_args.args[1], [])
        self.assertEqual(state["cohort_freeze"]["status"], "created")

    def test_explicit_same_day_historical_request_never_fetches(self):
        with patch("analysis.market_feedback_job._now", return_value=self.start), \
                patch("analysis.market_feedback_job._bounded_fetch") as fetch:
            state = collect_feedback(self.ctx, calendar=self.cal, as_of=self.start,
                                     cache_dir=self.root, refresh_mode="historical")
        fetch.assert_not_called()
        self.assertEqual(state["reason"], "historical_live_collection_forbidden")

    def test_report_wrapper_historical_mode_never_loads_live_calendar(self):
        from analysis import market_regime
        with patch.object(market_regime, "_review_calendar") as cal_fetch, \
                patch("analysis.market_feedback_job._bounded_fetch") as quotes_fetch, \
                patch("analysis.market_feedback_job._now", return_value=self.start):
            result = market_regime.collect_market_feedback(self.ctx, as_of=self.start,
                                                          refresh_mode="historical")
        cal_fetch.assert_not_called()
        quotes_fetch.assert_not_called()
        self.assertEqual(result["reason"], "historical_live_collection_forbidden")

    def test_report_wrapper_failure_retains_both_dates(self):
        from analysis import market_regime
        self.ctx["data_date"] = "2026-09-29"
        with patch.object(market_regime, "_review_calendar", side_effect=OSError("fixture")):
            result = market_regime.collect_market_feedback(self.ctx, as_of=self.start,
                                                          refresh_mode="live_refresh")
        self.assertEqual(result["basis_date"], "2026-09-30")
        self.assertEqual(result["feedback_basis_date"], "2026-09-30")
        self.assertEqual(result["market_basis_date"], "2026-09-29")

    def test_session_mode_is_fixed_at_report_cutoff(self):
        cutoff = datetime.fromisoformat("2026-09-30T15:09:00+08:00")
        result = resolve_feedback_session(self.cal, cutoff, self.finish, "live_refresh")
        self.assertEqual(result["mode"], "intraday")
        holiday = resolve_feedback_session(calendar("2026-09-29", "2026-10-01"),
                                           self.start, self.finish, "live_refresh")
        self.assertEqual(holiday["reason"], "non_trading_session")

    def test_legacy_binding_remains_readable_but_requires_original_market_date(self):
        state = {"schema_version": "market-feedback/v1", "basis_date": "2026-09-30",
                 "as_of": self.finish.isoformat(), "items": [], "summary": {}}
        artifact = {"schema_version": "market-feedback-artifact/v1", "basis_date": "2026-09-30",
                    "created_at": self.finish.isoformat(), "state": state, "inputs": {}}
        artifact["content_sha256"] = canonical_digest(artifact)
        p = self.root / "market_feedback_artifacts" / "2026-09-30" / f"{artifact['content_sha256']}.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(artifact))
        self.ctx["market_feedback_binding"] = {"basis_date": "2026-09-30", "artifact_path": str(p),
                                               "content_sha256": artifact["content_sha256"]}
        loaded = collect_feedback(self.ctx, calendar=None, as_of=self.finish,
                                  cache_dir=self.root, no_refresh=True)
        self.assertEqual(loaded["schema_version"], "market-feedback/v1")
        self.ctx["data_date"] = "2026-09-29"
        refused = collect_feedback(self.ctx, calendar=None, as_of=self.finish,
                                   cache_dir=self.root, no_refresh=True)
        self.assertEqual(refused["reason"], "market_feedback_binding_missing")

    def test_new_binding_rejects_version_clock_and_date_mismatch(self):
        state = self.run_collect()
        original = copy.deepcopy(self.ctx)
        for key, value in (("schema_version", "market-feedback-binding/v99"),
                           ("feedback_basis_date", "2026-09-29"),
                           ("evidence_as_of", "2026-09-30T15:21:00+08:00"),
                           ("report_as_of", "2026-09-30T15:21:00+08:00")):
            self.ctx = copy.deepcopy(original)
            self.ctx["market_feedback_binding"][key] = value
            result = collect_feedback(self.ctx, calendar=None, as_of=self.finish,
                                      cache_dir=self.root, no_refresh=True)
            self.assertEqual(result["reason"], "market_feedback_binding_missing", key)
        self.assertEqual(state["run_binding"]["schema_version"], "market-feedback-binding/v2")

    def test_new_close_does_not_overwrite_legacy_same_day_close(self):
        p = self.root / "market_feedback_close" / "2026-09-30.json"
        p.parent.mkdir()
        p.write_text("old immutable evidence")
        state = self.run_collect()
        self.assertEqual(p.read_text(), "old immutable evidence")
        paths = list(p.parent.glob("*/2026-09-30.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["schema_version"], "market-feedback-close/v2")
        self.assertEqual(state["schema_version"], "market-feedback/v2")

    def test_recovered_errors_are_advisory_not_missing(self):
        data = self.fetched()
        data["errors"] = [{"stage": "quote", "code": "000001.SZ", "error": "primary error"}]
        state = self.run_collect(data)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["summary"]["missing"], 0)
        self.assertIn("primary_provider_failed_but_recovered", state["diagnostics"]["advisories"])
        self.assertNotIn("fetch_quote_error", state["reasons"])

    def test_rerun_comparison_uses_current_collection_not_first_same_day_close(self):
        from analysis import market_feedback_job as job
        previous = {"schema_version": "market-feedback-close/v2",
                    "basis_date": "2026-09-29", "rule_version": "strong-stock-next-session-feedback/v2",
                    "frozen_at": "2026-09-29T15:20:00+08:00", "mode": "close",
                    "summary": {"coverage": 1, "median_pct_chg": 1, "positive_ratio": 1}}
        previous["content_sha256"] = canonical_digest(previous)
        path = job._close_path(self.root, "2026-09-29")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(previous))
        first = self.run_collect()
        today = job._close_path(self.root, "2026-09-30")
        original = today.read_bytes()
        changed = self.fetched()
        changed["quotes"] = [quote("000001.SZ", 5), quote("000002.SZ", 6)]
        second = self.run_collect(changed)
        self.assertEqual(first["comparison"]["median_pct_chg_change"], -5.5)
        self.assertEqual(second["comparison"]["median_pct_chg_change"], 4.5)
        self.assertEqual(today.read_bytes(), original)

    def test_cross_midnight_collection_cannot_publish_a_live_binding(self):
        self.start = datetime.fromisoformat("2026-09-30T23:59:50+08:00")
        self.finish = datetime.fromisoformat("2026-10-01T00:00:01+08:00")
        state = self.run_collect()
        self.assertEqual(state["reason"], "collection_crossed_session_day")
        self.assertNotIn("market_feedback_binding", self.ctx)

    def test_invalid_existing_close_is_not_replaced_or_used_for_comparison(self):
        from analysis import market_feedback_job as job
        p = job._close_path(self.root, "2026-09-30")
        p.parent.mkdir(parents=True)
        p.write_text("corrupt frozen baseline")
        state = self.run_collect()
        self.assertEqual(state["summary"]["available"], 2)
        self.assertEqual(state["comparison"]["status"], "unavailable")
        self.assertEqual(state["comparison"]["reason"], "current_close_unavailable")
        self.assertEqual(p.read_text(), "corrupt frozen baseline")

    def test_starting_intraday_never_creates_close_feedback_after_threshold(self):
        self.start = datetime.fromisoformat("2026-09-30T15:09:00+08:00")
        state = self.run_collect()
        self.assertEqual(state["mode"], "intraday")
        self.assertFalse((self.root / "market_feedback_close").exists())

    def test_outer_timeout_closes_shared_request_gate(self):
        from analysis import market_feedback_job as job
        observed = {}
        finished = threading.Event()

        def late_fetch(*args, **kwargs):
            observed.update(kwargs)
            kwargs["cancel_event"].wait(1)
            observed["cancelled"] = kwargs["cancel_event"].is_set()
            finished.set()
            return {"pool": {}, "quotes": [{"code": "late"}], "errors": []}

        with patch("fetchers.market_feedback.fetch_inputs", side_effect=late_fetch), \
                patch.object(job, "FETCH_TIMEOUT_SECONDS", .03):
            started = time.monotonic()
            result = job._bounded_fetch("2026-09-30", [], self.start)
            self.assertLess(time.monotonic() - started, .23)
        self.assertTrue(finished.wait(1))
        self.assertTrue(observed["cancelled"])
        self.assertIsInstance(observed["deadline"], float)
        self.assertEqual(result["quotes"], [])
        self.assertEqual(result["errors"], ["fetch_timeout"])

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
