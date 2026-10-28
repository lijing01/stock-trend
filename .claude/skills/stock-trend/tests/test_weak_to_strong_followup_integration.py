"""Exercise signal carry-over outside the current scan universe."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from core.source_health import RunSourceHealth
from core.weak_to_strong_snapshot import save_snapshot
from scans import daily_candidates as dc


class FollowUpIntegrationTests(unittest.TestCase):
    def test_missing_prior_tracking_snapshot_keeps_count_unknown(self):
        item = {"code": "600001", "ts_code": "600001.SH", "state": "SUPPLY_DRY",
                "cycle_id": "cycle", "signal_date": "2026-10-27", "H": 10,
                "kline_evidence": {"basis": "qfq"}}
        dates = ["2026-10-27", "2026-10-28"]
        with tempfile.TemporaryDirectory() as root, patch.object(dc, "CACHE_DIR", Path(root)):
            save_snapshot({"as_of_date": dates[0], "watching": [item],
                           "follow_up": {"status": "missing", "rows": []}},
                          {}, {}, {}, Path(root) / "weak_to_strong")
            with patch.object(dc, "bounded_source_map", return_value=[("600001.SH", None)]):
                output = dc.build_weak_follow_up(dates[1], dates, {}, {}, RunSourceHealth())
            self.assertIn("跟踪链存在缺口", output["coverage_warning"])
            self.assertIsNone(output["rows"][0]["tracking_count"])

    def test_outside_scan_is_fetched_and_carried_to_next_day(self):
        code, raw_cycle = "600001.SH", "cycle"
        cycle = hashlib.sha256((code + ":" + raw_cycle).encode()).hexdigest()
        item = {"code": "600001", "ts_code": code, "name": "测试", "state": "BREAKOUT_CONFIRMED",
                "cycle_id": cycle, "signal_date": "2026-10-26", "H": 10.0, "Ct": 11.0,
                "kline_evidence": {"basis": "qfq"}}
        dates = ["2026-10-26", "2026-10-27", "2026-10-28"]
        today_data = {"data": [{"trade_date": "20261027", "close": 12.0}]}
        result = {"as_of_date": dates[1], "state": "WAIT", "cycle_id": "",
                  "evidence_status": "ready", "kline_evidence": {"basis": "qfq",
                    "close_confirmation": {"confirmed": True, "source": "fixture"}}}
        with tempfile.TemporaryDirectory() as root, patch.object(dc, "CACHE_DIR", Path(root)):
            snapshot_root = Path(root) / "weak_to_strong"
            save_snapshot({"as_of_date": dates[0], "confirmed": [item]}, {}, {}, {}, snapshot_root)
            with patch.object(dc, "bounded_source_map", return_value=[(code, today_data)]) as fetch, \
                 patch.object(dc, "analyze_daily_weak_to_strong", return_value=result):
                output = dc.build_weak_follow_up(dates[1], dates, {}, {}, RunSourceHealth())
            self.assertEqual(fetch.call_args.args[1], [code])
            self.assertEqual(output["rows"][0]["follow_up_status"], "ENHANCED")
            self.assertEqual(output["rows"][0]["latest_valid_close"], 12.0)
            self.assertEqual(output["rows"][0]["tracking_count"], 1)
            save_snapshot({"as_of_date": dates[1], "follow_up": output}, {}, {}, {}, snapshot_root)
            tomorrow = {"kline_payload": {"data": [{"trade_date": "20261028", "close": 9.0}]},
                        "result": {**result, "as_of_date": dates[2]}}
            with patch.object(dc, "bounded_source_map") as fetch:
                next_output = dc.build_weak_follow_up(dates[2], dates, {}, {code: tomorrow}, RunSourceHealth())
            fetch.assert_not_called()
            self.assertEqual(next_output["rows"][0]["follow_up_status"], "INVALIDATED")
            self.assertEqual(next_output["rows"][0]["previous_close"], 12.0)
            self.assertEqual(next_output["rows"][0]["tracking_count"], 2)
            save_snapshot({"as_of_date": dates[2], "follow_up": next_output}, {}, {}, {}, snapshot_root)
            with patch.object(dc, "bounded_source_map") as fetch:
                ended = dc.build_weak_follow_up("2026-10-29", dates + ["2026-10-29"], {}, {}, RunSourceHealth())
            fetch.assert_not_called()
            self.assertEqual(ended["rows"], [])

    def test_missing_data_is_retained_for_the_next_session(self):
        item = {"code": "600001", "ts_code": "600001.SH", "state": "BREAKOUT_CONFIRMED",
                "cycle_id": "original", "signal_date": "2026-10-26", "H": 10.0, "Ct": 11.0,
                "kline_evidence": {"basis": "qfq"}}
        dates = ["2026-10-26", "2026-10-27", "2026-10-28"]
        with tempfile.TemporaryDirectory() as root, patch.object(dc, "CACHE_DIR", Path(root)):
            snapshot_root = Path(root) / "weak_to_strong"
            save_snapshot({"as_of_date": dates[0], "confirmed": [item]}, {}, {}, {}, snapshot_root)
            with patch.object(dc, "bounded_source_map", return_value=[("600001.SH", None)]):
                output = dc.build_weak_follow_up(dates[1], dates, {}, {}, RunSourceHealth())
            self.assertEqual(output["rows"][0]["follow_up_status"], "PENDING_REVIEW")
            save_snapshot({"as_of_date": dates[1], "follow_up": output}, {}, {}, {}, snapshot_root)
            with patch.object(dc, "bounded_source_map", return_value=[("600001.SH", None)]) as fetch:
                next_output = dc.build_weak_follow_up(dates[2], dates, {}, {}, RunSourceHealth())
            self.assertEqual(fetch.call_args.args[1], ["600001.SH"])
            self.assertEqual(next_output["rows"][0]["previous_close"], 11.0)


if __name__ == "__main__":
    unittest.main()
