#!/usr/bin/env python3
"""Fixture tests for daily-review sector persistence evidence."""
import json
import math
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.sector_persistence import (  # noqa: E402
    analyze_sector_series,
    select_displayed_sectors,
)
from analysis.sector_snapshot_job import freeze_sector_persistence  # noqa: E402
from reporting.sector_persistence import render_html, render_markdown  # noqa: E402


DATES = [f"2026-09-{day:02d}" for day in range(1, 22)]


def sector(code="BK0001", name="测试行业", kind="industry"):
    return {
        "code": code,
        "name": name,
        "type": kind,
        "provider": "eastmoney",
        "sector_id": f"eastmoney:{kind}:{code}",
        "change_pct": 1.2,
        "up_count": 8,
        "down_count": 2,
        "flat_count": 1,
        "total_count": 11,
    }


def records(dates=DATES, *, start=100.0, step=1.0):
    return [
        {
            "trade_date": date.replace("-", ""),
            "close": start + index * step,
            "sector_id": "eastmoney:industry:BK0001",
            "classification_version": "eastmoney-industry-concept/v1",
            "source": "eastmoney",
            "price_type": "forward_adjusted_close",
        }
        for index, date in enumerate(dates)
    ]


class TestSectorPersistenceCalculation(unittest.TestCase):
    def test_exact_returns_and_consecutive_up_days(self):
        result = analyze_sector_series(
            sector(), records(), trading_dates=DATES,
            basis_date=DATES[-1],
        )

        self.assertEqual(result["status"], "complete")
        self.assertAlmostEqual(result["return_5d"], 120 / 115 - 1)
        self.assertAlmostEqual(result["return_20d"], 120 / 100 - 1)
        self.assertEqual(result["consecutive_up_days"], 20)
        self.assertAlmostEqual(result["constituent_up_ratio"], 8 / 11)
        self.assertEqual(result["ranking_scope"], "today_displayed")

    def test_six_dates_support_5d_but_not_20d(self):
        result = analyze_sector_series(
            sector(), records(DATES[-6:], start=115),
            trading_dates=DATES, basis_date=DATES[-1],
        )

        self.assertIsNotNone(result["return_5d"])
        self.assertIsNone(result["return_20d"])
        self.assertIn("window_20d_insufficient", result["reasons"])

    def test_authority_gap_rejects_window_without_summing_pct(self):
        broken = records()
        del broken[-3]
        result = analyze_sector_series(
            sector(), broken, trading_dates=DATES,
            basis_date=DATES[-1],
        )

        self.assertIsNone(result["return_5d"])
        self.assertIsNone(result["return_20d"])
        self.assertIn("window_5d_not_contiguous", result["reasons"])

    def test_mixed_identity_classification_source_or_price_type_is_rejected(self):
        fields = {
            "sector_id": "eastmoney:industry:BK9999",
            "classification_version": "other/v2",
            "source": "other",
            "price_type": "unadjusted_close",
        }
        for field, value in fields.items():
            with self.subTest(field=field):
                mixed = records()
                mixed[-2] = dict(mixed[-2], **{field: value})
                result = analyze_sector_series(
                    sector(), mixed, trading_dates=DATES,
                    basis_date=DATES[-1],
                )
                self.assertIsNone(result["return_5d"])
                self.assertIn(f"mixed_{field}", result["reasons"])

    def test_code_rename_keeps_stable_id_and_current_display_identity(self):
        current = sector(code="BK7777", name="新名称")
        current["sector_id"] = "eastmoney:industry:stable-sector"
        renamed_records = records()
        for row in renamed_records:
            row["sector_id"] = current["sector_id"]
        result = analyze_sector_series(
            current, renamed_records, trading_dates=DATES,
            basis_date=DATES[-1],
        )
        self.assertEqual(result["name"], "新名称")
        self.assertEqual(result["code"], "BK7777")
        self.assertEqual(result["sector_id"], current["sector_id"])

    def test_up_ratio_requires_complete_constituent_counts(self):
        incomplete = sector()
        incomplete.pop("flat_count")
        result = analyze_sector_series(
            incomplete, records(), trading_dates=DATES,
            basis_date=DATES[-1],
        )
        self.assertIsNone(result["constituent_up_ratio"])

    def test_non_finite_close_and_counts_are_not_accepted(self):
        invalid = records()
        invalid[-1]["close"] = math.nan
        invalid_sector = sector()
        invalid_sector["up_count"] = math.inf
        result = analyze_sector_series(
            invalid_sector, invalid, trading_dates=DATES,
            basis_date=DATES[-1],
        )
        self.assertIsNone(result["return_5d"])
        self.assertIsNone(result["constituent_up_ratio"])

    def test_all_rising_fetch_window_is_reported_as_lower_bound(self):
        earlier = "2026-08-31"
        result = analyze_sector_series(
            sector(), records(), trading_dates=[earlier, *DATES],
            basis_date=DATES[-1],
        )
        self.assertEqual(result["consecutive_up_days"], 20)
        self.assertTrue(result["consecutive_up_days_lower_bound"])

    def test_display_selection_is_bounded_and_deduplicated(self):
        strongest = [sector(f"BK{i:04d}", f"强{i}") for i in range(12)]
        weakest = [sector("BK0009", "重复")] + [
            sector(f"BK{i:04d}", f"弱{i}") for i in range(20, 24)
        ]
        observed = [sector("BK0021", "重复观察"), sector("BK0099", "观察")]

        selected = select_displayed_sectors(strongest, weakest, observed)

        self.assertEqual(len(selected), 13)
        self.assertEqual(selected[0]["roles"], ["strongest"])
        by_code = {item["code"]: item for item in selected}
        self.assertEqual(by_code["BK0009"]["roles"], ["strongest", "weakest"])
        self.assertEqual(by_code["BK0021"]["roles"], ["weakest", "observation"])


class TestSectorPersistenceFreeze(unittest.TestCase):
    def test_refresh_freezes_bound_artifact_and_offline_reuses_exact_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            fetcher = Mock(return_value={"BK0001": records()})
            first = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=True, fetcher=fetcher,
            )
            second = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=False,
                fetcher=Mock(side_effect=AssertionError("offline fetched")),
            )

            self.assertEqual(first, second)
            fetcher.assert_called_once_with(
                ["BK0001"], min_records=80, max_workers=4,
                total_timeout=12.0)
            frozen = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(frozen["schema_version"], "sector-persistence/v1")
            self.assertEqual(frozen["basis_date"], DATES[-1])
            self.assertEqual(frozen["sectors"][0]["record_version"],
                             "eastmoney-sector-kline/v1")

    def test_refresh_reuses_exact_valid_bound_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            fetcher = Mock(return_value={"BK0001": records()})
            first = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=True, fetcher=fetcher,
            )
            second = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=True,
                fetcher=Mock(side_effect=AssertionError("refetched")),
            )
        self.assertEqual(first, second)
        fetcher.assert_called_once()

    def test_run_cutoff_does_not_backdate_actual_fetch_completion(self):
        run_cutoff = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
        fetched_at = "2026-09-21T07:00:02+00:00"
        actual_cutoff = datetime(2026, 9, 21, 7, 0, 3,
                                 tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            first = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact, refresh=True,
                run_as_of=run_cutoff, as_of=actual_cutoff,
                fetched_at=fetched_at,
                fetcher=Mock(return_value={"BK0001": records()}),
            )
            second = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact, refresh=False,
                run_as_of=run_cutoff, as_of=actual_cutoff,
            )
            frozen = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(frozen["run_as_of"], run_cutoff.isoformat())
        self.assertEqual(frozen["fetched_at"], fetched_at)

    def test_freeze_whitelists_finite_fields_and_binds_calendar_evidence(self):
        calendar = {
            "schema": "review-time/v1", "trading_dates": DATES,
            "source": "authority-fixture",
            "fetched_at": "2026-09-21T07:00:00+00:00",
        }
        source_sector = sector()
        source_sector.update({"main_force_net": math.nan, "unexpected": "drop"})
        source_records = records()
        source_records[-1]["unexpected"] = "drop"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            freeze_sector_persistence(
                [source_sector], DATES[-1], DATES, artifact, refresh=True,
                calendar_evidence=calendar,
                fetcher=Mock(return_value={"BK0001": source_records}),
            )
            frozen = json.loads(artifact.read_text(encoding="utf-8"))
            loaded = freeze_sector_persistence(
                [source_sector], DATES[-1], DATES, artifact, refresh=False,
                calendar_evidence=calendar,
            )
        self.assertEqual(loaded["status"], "complete")
        self.assertEqual(frozen["calendar_source"], "authority-fixture")
        self.assertTrue(frozen["calendar_evidence_digest"])
        self.assertNotIn("unexpected", frozen["sectors"][0])
        self.assertNotIn("main_force_net", frozen["sectors"][0])
        self.assertNotIn("unexpected", frozen["sectors"][0]["records"][-1])

    def test_offline_missing_or_mismatched_artifact_never_fetches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            fetcher = Mock(side_effect=AssertionError("offline fetched"))
            missing = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=False, fetcher=fetcher,
            )
            artifact.write_text(json.dumps({
                "schema_version": "sector-persistence/v1",
                "basis_date": DATES[-2], "sectors": [],
            }), encoding="utf-8")
            mismatch = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=False, fetcher=fetcher,
            )

            self.assertEqual(missing["status"], "missing")
            self.assertEqual(mismatch["status"], "mismatched")
            fetcher.assert_not_called()

    def test_offline_rejects_tampered_bound_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=True, fetcher=Mock(return_value={"BK0001": records()}),
            )
            raw = json.loads(artifact.read_text(encoding="utf-8"))
            raw["sectors"][0]["records"][-1]["close"] = 9999
            artifact.write_text(json.dumps(raw), encoding="utf-8")

            result = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact,
                refresh=False,
                fetcher=Mock(side_effect=AssertionError("offline fetched")),
            )

        self.assertEqual(result["status"], "mismatched")

    def test_freeze_filters_records_after_basis_date(self):
        future = records() + [{
            **records()[-1], "trade_date": "20260922", "close": 9999,
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact, refresh=True,
                fetcher=Mock(return_value={"BK0001": future}),
            )
            frozen = json.loads(artifact.read_text(encoding="utf-8"))

        self.assertEqual(len(frozen["sectors"][0]["records"]), 21)
        self.assertNotIn("20260922", {
            row["trade_date"] for row in frozen["sectors"][0]["records"]
        })

    def test_offline_rejects_artifact_frozen_after_run_cutoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "bound.json"
            freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact, refresh=True,
                fetched_at="2026-09-21T08:00:00+00:00",
                fetcher=Mock(return_value={"BK0001": records()}),
            )
            result = freeze_sector_persistence(
                [sector()], DATES[-1], DATES, artifact, refresh=False,
                as_of=datetime(2026, 9, 21, 7, 59, tzinfo=timezone.utc),
                fetcher=Mock(side_effect=AssertionError("offline fetched")),
            )

        self.assertEqual(result["status"], "mismatched")

    def test_fetch_is_bounded_by_total_timeout_and_deduplicated(self):
        def slow_fetch(codes, **kwargs):
            time.sleep(0.25)
            return {code: records() for code in codes}

        with tempfile.TemporaryDirectory() as tmpdir:
            started = time.monotonic()
            result = freeze_sector_persistence(
                [sector(), sector()], DATES[-1], DATES,
                Path(tmpdir) / "bound.json", refresh=True,
                fetcher=slow_fetch, total_timeout=0.03,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(result["status"], "degraded")
        self.assertIn("fetch_timeout", result["reasons"])

    def test_renderers_share_state_and_do_not_claim_market_wide_ranking(self):
        state = {
            "status": "complete", "basis_date": DATES[-1],
            "items": [analyze_sector_series(
                sector(), records(), trading_dates=DATES,
                basis_date=DATES[-1])],
        }
        html = render_html(state)
        markdown = render_markdown(state)
        for text in (html, markdown):
            self.assertIn("5日收益", text)
            self.assertIn("20日收益", text)
            self.assertIn("连续上涨", text)
            self.assertIn("当日展示板块", text)
            self.assertNotIn("全市场5/20日排名", text)

    def test_renderers_localize_reasons_escape_names_and_show_streak_bound(self):
        item = analyze_sector_series(
            {**sector(name="行业|甲\n乙"), "roles": ["strongest"]},
            records(DATES[-6:], start=115),
            trading_dates=["2026-08-31", *DATES], basis_date=DATES[-1],
        )
        item["consecutive_up_days"] = 5
        item["consecutive_up_days_lower_bound"] = True
        state = {"status": "degraded", "items": [item]}
        html = render_html(state)
        markdown = render_markdown(state)
        self.assertIn("20日窗口不足", html)
        self.assertIn("≥5", html)
        self.assertIn("行业\\|甲 乙", markdown)
        self.assertIn("20日窗口不足", markdown)
        self.assertIn("≥5", markdown)


if __name__ == "__main__":
    unittest.main()
