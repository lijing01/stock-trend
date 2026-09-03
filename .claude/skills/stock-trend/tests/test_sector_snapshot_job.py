#!/usr/bin/env python3
"""Tests for the standalone post-close sector snapshot job."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis import sector_snapshot_job as job


def complete_rankings(data_date="2026-09-03"):
    return {
        "meta": {
            "complete": True,
            "source": "eastmoney",
            "data_date": data_date,
            "total_sectors": 12,
            "sources": {"industry": "ok", "concept": "ok"},
        },
        "sectors": [{
            "code": f"BK{i:04d}", "name": f"板块{i}",
            "change_pct": 1.0, "main_force_net": 1e8,
            "up_count": 9, "down_count": 1,
        } for i in range(12)],
    }


class TestSectorSnapshotJob(unittest.TestCase):
    def test_capture_saves_complete_post_close_snapshot(self):
        payload = complete_rankings()
        commit_result = {
            "status": "saved", "data_date": "2026-09-03",
            "ranked_count": 12,
        }
        with patch.object(
                job, "get_last_trading_day",
                return_value=("2026-09-03", "calendar_open")), \
             patch.object(
                 job, "get_sector_rankings",
                 return_value={"payload": payload, "live_attempt": {}}) as fetch, \
             patch.object(job, "save_rankings_cache") as save_cache, \
             patch.object(job, "append_daily_snapshot") as append_snapshot, \
             patch.object(
                 job, "commit_candidate_sector_snapshot",
                 return_value=commit_result) as commit:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 20),
                expected_date="2026-09-03",
            )

        self.assertEqual(result["status"], "saved")
        self.assertTrue(result["written"])
        fetch.assert_called_once_with(with_evidence=True)
        save_cache.assert_called_once_with(
            payload, data_date="2026-09-03")
        append_snapshot.assert_called_once_with(
            payload, override_date="2026-09-03")
        commit.assert_called_once_with(
            payload, data_date="2026-09-03")

    def test_capture_refuses_intraday_write(self):
        with patch.object(job, "get_sector_rankings") as fetch, \
             patch.object(job, "get_last_trading_day") as last_day:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 3, 14, 59),
                expected_date="2026-09-03",
            )

        self.assertEqual(result["status"], "not_closed")
        self.assertFalse(result["written"])
        fetch.assert_not_called()
        last_day.assert_not_called()

    def test_capture_does_not_save_partial_rankings(self):
        payload = complete_rankings()
        payload["meta"].update({
            "complete": False,
            "source": "akshare",
            "sources": {"industry": "ok", "concept": "empty"},
        })
        with patch.object(
                job, "get_last_trading_day",
                return_value=("2026-09-03", "calendar_open")), \
             patch.object(
                 job, "get_sector_rankings",
                 return_value={"payload": payload, "live_attempt": {}}), \
             patch.object(job, "save_rankings_cache") as save_cache, \
             patch.object(job, "append_daily_snapshot") as append_snapshot, \
             patch.object(job, "commit_candidate_sector_snapshot") as commit:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 20),
                expected_date="2026-09-03",
            )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["written"])
        save_cache.assert_not_called()
        append_snapshot.assert_not_called()
        commit.assert_not_called()

    def test_capture_refuses_weekend_without_fetching(self):
        with patch.object(job, "get_sector_rankings") as fetch, \
             patch.object(job, "get_last_trading_day") as last_day:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 5, 15, 20),
                expected_date="2026-09-05",
            )

        self.assertEqual(result["status"], "market_closed")
        self.assertFalse(result["written"])
        fetch.assert_not_called()
        last_day.assert_not_called()

    def test_capture_rejects_provider_date_mismatch_without_writing(self):
        payload = complete_rankings(data_date="2026-09-02")
        with patch.object(
                job, "get_last_trading_day",
                return_value=("2026-09-03", "calendar_open")), \
             patch.object(
                 job, "get_sector_rankings",
                 return_value={"payload": payload, "live_attempt": {}}), \
             patch.object(job, "save_rankings_cache") as save_cache, \
             patch.object(job, "append_daily_snapshot") as append_snapshot, \
             patch.object(job, "commit_candidate_sector_snapshot") as commit:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 20),
                expected_date="2026-09-03",
            )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["written"])
        save_cache.assert_not_called()
        append_snapshot.assert_not_called()
        commit.assert_not_called()

    def test_capture_dry_run_validates_without_writing(self):
        payload = complete_rankings()
        with patch.object(
                job, "get_last_trading_day",
                return_value=("2026-09-03", "calendar_open")), \
             patch.object(
                 job, "get_sector_rankings",
                 return_value={"payload": payload, "live_attempt": {}}), \
             patch.object(job, "save_rankings_cache") as save_cache, \
             patch.object(job, "append_daily_snapshot") as append_snapshot, \
             patch.object(job, "commit_candidate_sector_snapshot") as commit:
            result = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 20),
                expected_date="2026-09-03",
                dry_run=True,
            )

        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["written"])
        self.assertEqual(result["universe_count"], 12)
        save_cache.assert_not_called()
        append_snapshot.assert_not_called()
        commit.assert_not_called()

    def test_capture_is_idempotent_for_same_trading_date(self):
        from fetchers import sector_data

        payload = complete_rankings()
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(job, "get_last_trading_day",
                          return_value=("2026-09-03", "calendar_open")), \
             patch.object(job, "get_sector_rankings",
                          return_value={"payload": payload,
                                        "live_attempt": {}}), \
             patch.object(job, "save_rankings_cache"), \
             patch.object(job, "append_daily_snapshot"), \
             patch.object(sector_data, "CANDIDATE_SNAPSHOT_FILE",
                          Path(tmpdir) / "candidate-history.json"):
            first = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 20),
                expected_date="2026-09-03",
            )
            second = job.capture_snapshot(
                now=datetime(2026, 9, 3, 15, 25),
                expected_date="2026-09-03",
            )
            history = job.load_candidate_sector_history(days=10)

        self.assertEqual(first["status"], "saved")
        self.assertEqual(second["status"], "saved")
        self.assertEqual(list(history), ["2026-09-03"])

    def test_cli_error_is_json_without_traceback(self):
        output = io.StringIO()
        with patch.object(job, "capture_snapshot",
                          side_effect=RuntimeError("provider exploded")), \
             redirect_stdout(output):
            exit_code = job.main(["--json"])

        self.assertEqual(exit_code, 1)
        rendered = output.getvalue()
        self.assertNotIn("Traceback", rendered)
        payload = json.loads(rendered)
        self.assertEqual(payload["status"], "error")

    def test_status_reports_coverage_and_next_requirement(self):
        with patch.object(
                job, "load_candidate_sector_history",
                return_value={
                    "2026-09-02": {
                        "complete": True, "quality": "good",
                        "sectors": [{}],
                    },
                }) as load_history:
            result = job.snapshot_status(
                as_of_date="2026-09-03", days=10)

        self.assertEqual(result, {
            "as_of_date": "2026-09-03",
            "coverage_days": 1,
            "minimum_days": 2,
            "days_needed": 1,
            "classification_ready": False,
        })
        load_history.assert_called_once_with(days=10)


if __name__ == "__main__":
    unittest.main()
