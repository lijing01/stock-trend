"""Frozen A-share anchor evidence and the 15:10 completion boundary."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bridge import us_review


class ReviewTimeTests(unittest.TestCase):
    def test_ordinary_weekend_and_calendar_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            capture = datetime.fromisoformat("2026-09-25T16:00:00+08:00")
            with patch("bridge.us_review._observed_now", return_value=capture), patch(
                    "fetchers.sector_data._load_authoritative_trading_dates",
                    return_value={"2026-09-24", "2026-09-25", "2026-09-28", "2026-09-30"}):
                us_review.resolve_anchor(capture, root)
            for day in ("2026-09-26", "2026-09-27"):
                result = us_review.resolve_anchor(datetime.fromisoformat(day + "T09:00:00+08:00"),
                                                 root, no_refresh=True)
                self.assertEqual(result["a_share_anchor_date"], "2026-09-25")
            outside = us_review.resolve_anchor(datetime.fromisoformat("2026-10-04T09:00:00+08:00"),
                                              root, no_refresh=True)
            self.assertIsNone(outside["a_share_anchor_date"])

    def test_completion_boundary_and_offline_calendar(self):
        with tempfile.TemporaryDirectory() as root:
            capture = datetime(2026, 9, 29, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
            dates = {"2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09"}
            with patch("bridge.us_review._observed_now", return_value=capture), patch(
                    "fetchers.sector_data._load_authoritative_trading_dates", return_value=dates):
                us_review.resolve_anchor(capture, root, no_refresh=False)
            for clock in ("07:00", "09:00", "10:00", "12:00", "15:05", "15:10", "16:00"):
                cutoff = datetime.fromisoformat(f"2026-09-30T{clock}:00+08:00")
                with patch("fetchers.sector_data._load_authoritative_trading_dates", side_effect=AssertionError("network")):
                    result = us_review.resolve_anchor(cutoff, root, no_refresh=True)
                self.assertEqual(result["a_share_anchor_date"], "2026-09-30" if clock >= "15:10" else "2026-09-29")
            holiday = us_review.resolve_anchor(datetime.fromisoformat("2026-10-04T09:00:00+08:00"), root, no_refresh=True)
            self.assertEqual(holiday["a_share_anchor_date"], "2026-09-30")
            for invalid_date in ("2026-10-03", "2026-10-01", "2026-10-09"):
                self.assertIsNone(us_review.resolve_anchor(
                    datetime.fromisoformat("2026-10-04T09:00:00+08:00"), root,
                    no_refresh=True, explicit_date=invalid_date)["a_share_anchor_date"])
            explicit = us_review.resolve_anchor(datetime.fromisoformat("2026-10-04T09:00:00+08:00"),
                root, no_refresh=True, explicit_date="2026-09-29")
            self.assertEqual(explicit["a_share_anchor_date"], "2026-09-29")
            self.assertFalse(explicit["latest_confirmed"])
            future = us_review.resolve_anchor(capture.replace(hour=15), root, no_refresh=True)
            self.assertIsNone(future["a_share_anchor_date"])

    def test_no_calendar_does_not_guess(self):
        with tempfile.TemporaryDirectory() as root:
            result = us_review.resolve_anchor(datetime.fromisoformat("2026-10-04T09:00:00+08:00"), root, no_refresh=True)
            self.assertIsNone(result["a_share_anchor_date"])

    def test_verified_frozen_close_fallback_is_not_latest_claim(self):
        cutoff = datetime.fromisoformat("2026-10-04T09:00:00+08:00")
        row = {"data_date": "2026-09-30", "frozen_at": "2026-09-30T16:00:00+08:00",
               "date_origin": "provider", "completed_close": True, "source": "daily_kline",
               "code": "000001.SH", "close": 3000}
        with tempfile.TemporaryDirectory() as root:
            result = us_review.resolve_anchor(cutoff, root, no_refresh=True, frozen_closes=[row])
            self.assertEqual(result["a_share_anchor_date"], "2026-09-30")
            self.assertFalse(result["latest_confirmed"])
            self.assertEqual(result["status"], "degraded")
            for field, value in (("completed_close", False), ("date_origin", "request"),
                                 ("close", None), ("close", float("nan")),
                                 ("frozen_at", "2026-10-05T16:00:00+08:00")):
                invalid = dict(row, **{field: value})
                self.assertIsNone(us_review.resolve_anchor(cutoff, root, no_refresh=True,
                                                          frozen_closes=[invalid])["a_share_anchor_date"])


if __name__ == "__main__":
    unittest.main()
