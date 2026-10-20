#!/usr/bin/env python3
"""Offline regression tests for sector field, coverage, and date semantics."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetchers import sector_data


def _epoch(day: str, hour: int = 10) -> int:
    return int(datetime.fromisoformat(
        f"{day}T{hour:02d}:00:00"
    ).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _ranking_response(*, day: str | None = "2026-10-20",
                      amount=123_456_789, turnover=6.25, prefix="BK") -> dict:
    rows = []
    for index in range(5):
        row = {
            "f12": f"{prefix}{index}",
            "f14": f"板块{index}",
            "f3": 2.5,
            "f6": amount,
            "f8": turnover,
            "f62": 10_000,
            "f104": 7,
            "f105": 3,
        }
        if day:
            row["f124"] = _epoch(day)
        rows.append(row)
    return {"rc": 0, "data": {"total": 5, "diff": rows}}


class SectorFieldSemanticsTests(unittest.TestCase):
    def test_legacy_alternative_source_does_not_certify_total_or_guess_rate(self):
        rankings = {"meta": {"provider": "ths", "source": "akshare"}, "sectors": [{
            "amount": 100_000_000, "up_count": 2, "down_count": 1, "total_count": 3}]}
        protected, changed = sector_data._protect_legacy_ranking_amounts(rankings)
        self.assertTrue(changed)
        row = protected["sectors"][0]
        self.assertIsNone(row["amount"])
        self.assertNotIn("turnover_rate", row)
        self.assertIsNone(row["total_count"])
        self.assertEqual(row["up_down_sample_count"], 3)
        self.assertEqual(protected["meta"]["provider"], "ths")

    def test_missing_constituent_timestamp_cannot_verify_the_whole_response(self):
        rows = [
            {"f12": "600000", "f14": "甲", "f6": 1e8, "f8": 1.2,
             "f124": _epoch("2026-10-20")},
            {"f12": "600001", "f14": "乙", "f6": 2e8, "f8": 2.4},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(sector_data, "SECTOR_STOCKS_CACHE_DIR", Path(tmpdir)), \
                patch.object(sector_data, "_fetch_json", return_value={"rc": 0, "data": {"total": 2, "diff": rows}}):
            stocks = sector_data.get_sector_stocks("BK1")
        self.assertTrue(stocks[0]["membership_coverage_complete"])
        self.assertEqual(stocks[0]["membership_data_date"], "")
        self.assertEqual(stocks[0]["membership_quality"], "partial")

    def test_unverified_snapshot_commit_never_reports_saved(self):
        rankings = {"meta": {"complete": True, "data_date": "2026-10-20", "date_origin": "request"},
                    "sectors": [{"code": "BK1", "up_count": 10, "down_count": 1}]}
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
                sector_data, "CANDIDATE_SNAPSHOT_FILE", Path(tmpdir) / "history.json"):
            result = sector_data.commit_candidate_sector_snapshot(rankings, "2026-10-20")
            self.assertFalse(sector_data.CANDIDATE_SNAPSHOT_FILE.exists())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason"], "ranking_source_date_unverified")

    def _rankings(self, first: dict, second: dict) -> dict:
        with patch.object(sector_data, "_fetch_json",
                          side_effect=[first, second]), \
             patch.object(sector_data.time, "sleep"):
            return sector_data.get_sector_rankings(
                allow_cross_source_fallback=False)

    def test_rankings_keep_f6_amount_and_f8_turnover_separate(self):
        rankings = self._rankings(
            _ranking_response(amount=900_000_000, turnover=3.75),
            _ranking_response(amount=900_000_000, turnover=3.75,
                              prefix="CK"),
        )

        sector = rankings["sectors"][0]
        self.assertEqual(sector["amount"], 900_000_000)
        self.assertEqual(sector["turnover_rate"], 3.75)
        self.assertEqual(
            rankings["meta"]["field_semantics"]["amount"], "f6:yuan")

    def test_missing_f6_does_not_copy_f8_into_amount(self):
        for missing in (None, "--", float("nan"), float("inf"), -1, True):
            with self.subTest(missing=missing):
                response = _ranking_response(amount=missing, turnover=8.4)
                rankings = self._rankings(response, response)
                sector = rankings["sectors"][0]
                self.assertIsNone(sector["amount"])
                self.assertEqual(sector["turnover_rate"], 8.4)

    def test_constituents_keep_f6_amount_and_f8_turnover_separate(self):
        response = {
            "rc": 0,
            "data": {"total": 1, "diff": [{
                "f12": "600000", "f14": "浦发银行", "f3": 1.2,
                "f6": 345_000_000, "f8": 0.87, "f20": 10_000_000_000,
                "f37": 7.2, "f124": _epoch("2026-10-20"),
            }]},
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SECTOR_STOCKS_CACHE_DIR",
                          Path(tmpdir)), \
             patch.object(sector_data, "_fetch_json", return_value=response):
            stocks = sector_data.get_sector_stocks("BK1")

        self.assertEqual(stocks[0]["amount"], 345_000_000)
        self.assertEqual(stocks[0]["turnover_rate"], 0.87)
        self.assertEqual(stocks[0]["membership_data_date"], "2026-10-20")
        self.assertEqual(stocks[0]["membership_total_count"], 1)
        self.assertTrue(stocks[0]["membership_coverage_complete"])

    def test_constituent_truncation_is_not_complete_membership(self):
        response = {
            "rc": 0,
            "data": {"total": 2, "diff": [{
                "f12": "600000", "f14": "浦发银行", "f6": 10,
                "f8": 1.0, "f124": _epoch("2026-10-20"),
            }]},
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SECTOR_STOCKS_CACHE_DIR",
                          Path(tmpdir)), \
             patch.object(sector_data, "_fetch_json", return_value=response):
            stocks = sector_data.get_sector_stocks("BK1", top_n=1)

        self.assertEqual(stocks[0]["membership_total_count"], 2)
        self.assertEqual(stocks[0]["membership_returned_count"], 1)
        self.assertFalse(stocks[0]["membership_coverage_complete"])

    def test_up_down_counts_are_sample_not_complete_constituent_total(self):
        response = _ranking_response()
        rankings = self._rankings(response, response)

        sector = rankings["sectors"][0]
        self.assertEqual(sector["up_down_sample_count"], 10)
        self.assertIsNone(sector["total_count"])
        self.assertIsNone(sector["flat_count"])
        self.assertIsNone(sector["suspended_count"])
        self.assertIsNone(sector["uncovered_count"])
        self.assertFalse(sector["constituent_coverage_complete"])

    def test_provider_date_requires_all_rows_to_share_one_date(self):
        verified = self._rankings(
            _ranking_response(day="2026-10-19"),
            _ranking_response(day="2026-10-19", prefix="CK"),
        )
        self.assertEqual(verified["meta"]["data_date"], "2026-10-19")
        self.assertEqual(verified["meta"]["date_origin"], "provider")
        self.assertEqual(
            verified["meta"]["provider_timestamp_status"], "verified")

        missing = self._rankings(
            _ranking_response(day=None),
            _ranking_response(day=None, prefix="CK"),
        )
        self.assertIsNone(missing["meta"]["data_date"])
        self.assertEqual(
            missing["meta"]["provider_timestamp_status"], "missing")

        mixed = self._rankings(
            _ranking_response(day="2026-10-19"),
            _ranking_response(day="2026-10-20", prefix="CK"),
        )
        self.assertIsNone(mixed["meta"]["data_date"])
        self.assertEqual(mixed["meta"]["provider_timestamp_status"], "mixed")

    def test_request_date_cannot_replace_missing_provider_date(self):
        rankings = {
            "meta": {
                "requested_date": "2026-10-20",
                "data_date": None,
                "date_origin": "unknown",
            }
        }
        self.assertIsNone(sector_data._resolve_verified_data_date(
            rankings, "2026-10-20"))

    def test_explicit_request_only_date_is_not_persisted(self):
        rankings = {
            "meta": {
                "complete": True,
                "provider": "eastmoney",
                "requested_date": "2026-10-20",
                "data_date": None,
                "date_origin": "unknown",
                "field_semantics_version":
                    sector_data.SECTOR_FIELD_SEMANTICS_VERSION,
            },
            "sectors": [
                {"code": f"BK{i}", "up_count": 1, "down_count": 0}
                for i in range(5)
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "rankings.json"
            with patch.object(sector_data, "CACHE_FILE", cache_file):
                sector_data.save_rankings_cache(
                    rankings, data_date="2026-10-20")
                self.assertFalse(cache_file.exists())

    def test_nonnegative_count_rejects_nonintegral_and_nonfinite_values(self):
        self.assertEqual(sector_data._nonnegative_int_or_none("2.0"), 2)
        for value in (1.5, "1.5", float("inf"), float("nan"), -1, True):
            with self.subTest(value=value):
                self.assertIsNone(
                    sector_data._nonnegative_int_or_none(value))

    def test_legacy_ranking_cache_protects_ambiguous_amount(self):
        now = datetime.now()
        data_date = now.strftime("%Y-%m-%d")
        payload = {
            "cached_at": now.isoformat(),
            "data_date": data_date,
            "rankings": {
                "meta": {"complete": True, "provider": "eastmoney"},
                "sectors": [
                    {"code": f"BK{i}", "amount": 5.5,
                     "up_count": 3, "down_count": 2, "total_count": 5}
                    for i in range(5)
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "rankings.json"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sector_data, "CACHE_FILE", cache_file):
                loaded = sector_data.load_rankings_cache_full()

        sector = loaded["rankings"]["sectors"][0]
        self.assertIsNone(sector["amount"])
        self.assertEqual(sector["turnover_rate"], 5.5)
        self.assertEqual(sector["amount_quality"], "legacy_ambiguous_f8")
        self.assertEqual(sector["up_down_sample_count"], 5)
        self.assertIsNone(sector["total_count"])
        self.assertIsNone(sector["flat_count"])
        self.assertFalse(sector["constituent_coverage_complete"])
        self.assertEqual(
            loaded["cache_provenance"]["semantics_status"],
            "legacy_protected")
        self.assertIsNone(loaded["data_date"])
        self.assertEqual(loaded["legacy_claimed_data_date"], data_date)
        self.assertIsNone(loaded["rankings"]["meta"]["data_date"])

    def test_legacy_ranking_wrapper_date_is_not_last_trading_day_proof(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 20, 16, 0, tzinfo=tz)

        payload = {
            "cached_at": "2026-10-20T16:00:00",
            "data_date": "2026-10-20",
            "rankings": {
                "meta": {"complete": True, "provider": "eastmoney"},
                "sectors": [{"code": "BK1", "amount": 2.0}],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "rankings.json"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sector_data, "CACHE_FILE", cache_file), \
                 patch.object(sector_data, "SNAPSHOT_FILE",
                              Path(tmpdir) / "missing-history.json"), \
                 patch.object(sector_data, "datetime", FrozenDateTime):
                data_date, source = sector_data.get_last_trading_day()

        self.assertIsNone(data_date)
        self.assertEqual(source, "")

    def test_versioned_cache_preserves_verified_amount_and_provenance(self):
        data_date = datetime.now().strftime("%Y-%m-%d")
        rankings = {
            "meta": {
                "complete": True,
                "provider": "eastmoney",
                "field_semantics_version":
                    sector_data.SECTOR_FIELD_SEMANTICS_VERSION,
                "data_date": data_date,
                "date_origin": "provider",
            },
            "sectors": [
                {"code": f"BK{i}", "amount": 200_000_000,
                 "turnover_rate": 2.0, "up_count": 1, "down_count": 0}
                for i in range(5)
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "rankings.json"
            with patch.object(sector_data, "CACHE_FILE", cache_file):
                sector_data.save_rankings_cache(rankings)
                loaded = sector_data.load_rankings_cache_full()

        self.assertEqual(
            loaded["rankings"]["sectors"][0]["amount"], 200_000_000)
        self.assertEqual(
            loaded["cache_provenance"]["field_semantics_version"],
            sector_data.SECTOR_FIELD_SEMANTICS_VERSION)
        self.assertEqual(
            loaded["cache_provenance"]["semantics_status"], "verified")
        self.assertEqual(
            loaded["rankings"]["meta"]["cache_provenance"]["data_date"],
            data_date)

    def test_legacy_constituent_cache_protects_ambiguous_amount(self):
        payload = {
            "schema_version": 2,
            "cached_at": datetime.now().isoformat(),
            "fetched_at": datetime.now().isoformat(),
            "data_date": datetime.now().strftime("%Y-%m-%d"),
            "provider": "eastmoney",
            "stocks": [{"code": "600000", "amount": 7.7}],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            path = cache_dir / "BK1.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sector_data, "SECTOR_STOCKS_CACHE_DIR", cache_dir):
                loaded = sector_data.load_sector_stocks_cache("BK1")
                exact = sector_data.load_sector_stocks_cache(
                    "BK1", as_of_date=payload["data_date"])

        stock = loaded["stocks"][0]
        self.assertIsNone(stock["amount"])
        self.assertEqual(stock["turnover_rate"], 7.7)
        self.assertEqual(stock["amount_quality"], "legacy_ambiguous_f8")
        self.assertIsNone(loaded["data_date"])
        self.assertEqual(
            loaded["legacy_claimed_data_date"], payload["data_date"])
        self.assertIsNone(exact)


if __name__ == "__main__":
    unittest.main()
