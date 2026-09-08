import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from core.candidate_research_snapshot import build_research_snapshot, save_research_snapshot


def candidate(code, score=80):
    return {"code": code, "ts_code": code + ".SH", "composite_score": score,
            "raw_composite_score": score, "quality_adjusted_score": score - 2,
            "buy_point_priority_bonus": 3, "data_quality": {"eligible": True,
            "dimensions": {"kline": {"data_date": "2026-09-07"}}},
            "source_evidence": {"kline": {"source": "eastmoney"}},
            "sector_name": "测试板块", "sector_actionable": True}


class T(unittest.TestCase):
    def build(self, rows, buckets, tracking={"status": "created"}):
        return build_research_snapshot(rows, buckets, "2026-09-07", {"mode": "actionable"},
            {"score": 85}, [], 50, official_tracking=tracking,
            parameter_summary={"top": 1})

    def test_records_all_scanned_rows_and_truncation(self):
        snap = self.build([candidate("A"), candidate("B")], {"actionable": [candidate("A")]})
        rows = {row["code"]: row for row in snap["content"]["records"]}
        self.assertEqual(rows["A"]["final_status"], "actionable")
        self.assertEqual(rows["B"]["selection_reason"], "top_n_truncated")
        self.assertEqual(rows["A"]["evidence"]["source_dates"]["kline"], "2026-09-07")

    def test_phase2_filtered_row_has_an_explicit_terminal_state(self):
        filtered = candidate("C", 0)
        filtered["research_terminal_status"] = "phase2_filtered"
        filtered["research_terminal_reason"] = "phase2_no_eligible_buy_point_or_data_error"
        snap = self.build([filtered], {})
        row = snap["content"]["records"][0]
        self.assertEqual(row["final_status"], "phase2_filtered")
        self.assertEqual(row["selection_reason"], "phase2_no_eligible_buy_point_or_data_error")

    def test_idempotent_and_same_day_conflict_is_not_training_sample(self):
        snap = self.build([candidate("A")], {"actionable": [candidate("A")]})
        changed = self.build([candidate("B")], {"actionable": [candidate("B")]})
        with tempfile.TemporaryDirectory() as root:
            first = save_research_snapshot(snap, root)
            again = save_research_snapshot(snap, root)
            conflict = save_research_snapshot(changed, root)
        self.assertEqual(first["status"], "created")
        self.assertTrue(first["training_eligible"])
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(conflict["status"], "conflict")
        self.assertFalse(conflict["training_eligible"])

    def test_created_and_unchanged_official_link_share_one_identity(self):
        rows = [candidate("A")]
        buckets = {"actionable": rows}
        created = self.build(rows, buckets, {"status": "created", "path": "/x", "content_sha256": "abc"})
        unchanged = self.build(rows, buckets, {"status": "unchanged", "path": "/x", "content_sha256": "abc"})
        self.assertEqual(created["run_id"], unchanged["run_id"])

    def test_source_and_capture_times_keep_distinct_statuses(self):
        captured = build_research_snapshot([candidate("A")], {}, "2026-09-07",
            {}, {}, [], 50, captured_at="2026-09-07T16:00:00+08:00")
        known = build_research_snapshot([candidate("A")], {}, "2026-09-07",
            {}, {}, [], 50, known_at="2026-09-07T15:30:00+08:00")
        both = build_research_snapshot([candidate("A")], {}, "2026-09-07",
            {}, {}, [], 50, known_at="2026-09-07T15:30:00+08:00",
            captured_at="2026-09-07T16:00:00+08:00")
        unknown = self.build([candidate("A")], {})
        self.assertEqual(captured["content"]["source_time_status"], "captured")
        self.assertEqual(known["content"]["source_time_status"], "known")
        self.assertEqual(both["content"]["source_time_status"], "known_and_captured")
        self.assertEqual(unknown["content"]["source_time_status"], "unknown")

    def test_provisional_never_enters_training_set(self):
        snap = build_research_snapshot([candidate("A")], {}, "2026-09-07",
            {"provisional": True}, {}, [], 50, official_tracking={"status": "skipped_provisional"})
        with tempfile.TemporaryDirectory() as root:
            result = save_research_snapshot(snap, root)
        self.assertEqual(result["status"], "created")
        self.assertFalse(result["training_eligible"])


if __name__ == "__main__":
    unittest.main()
