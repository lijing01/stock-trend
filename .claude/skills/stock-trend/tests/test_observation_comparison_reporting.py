"""HTML and Markdown rendering for observation-list changes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reporting import observation_comparison as reporting


class ObservationComparisonReportingTests(unittest.TestCase):
    def test_ready_state_renders_same_events_and_collection_changes(self):
        state = {
            "status": "ready", "current_date": "2026-09-30",
            "previous_date": "2026-09-29", "comparison_scope": "full",
            "collection_changes": {
                "added": [{"market": "SH", "code": "600519"}],
                "removed": [{"market": "SZ", "code": "000001"}],
                "wording": "yaml_config",
            },
            "events": [
                {"type": "structure_invalidated", "priority": 10,
                 "market": "SZ", "code": "300750"},
                {"type": "confirmed_evidence", "priority": 40,
                 "market": "SH", "code": "600036", "event_type": "lps",
                 "confirmation_date": "2026-09-30"},
            ],
            "rows": [{
                "market": "SH", "code": "600036",
                "score_status": "comparable", "raw_score_delta": 2.5,
                "quality_adjusted_score_delta": 2.0,
            }],
        }
        html = reporting.render_html(state)
        markdown = reporting.render_markdown(state)
        for text in (html, markdown):
            self.assertIn("600519", text)
            self.assertIn("300750", text)
            self.assertIn("结构失效", text)
            self.assertIn("确认事件 lps", text)
            self.assertIn("+2.5", text)
        self.assertIn("data-observation-comparison-status=\"ready\"", html)
        self.assertIn("### 观察列表变化", markdown)

    def test_legacy_and_degraded_wording_do_not_claim_yaml_change(self):
        state = {
            "status": "partial", "reason": "旧版 artifact 仅支持记录集合新增/移除",
            "comparison_scope": "collection_only",
            "collection_changes": {
                "added": [{"market": "SH", "code": "600519"}],
                "removed": [], "wording": "artifact_record_set",
            }, "events": [], "rows": [],
        }
        for text in (reporting.render_html(state), reporting.render_markdown(state)):
            self.assertIn("记录集合新增", text)
            self.assertNotIn("YAML 新增", text)

    def test_unavailable_state_is_compact_and_escaped(self):
        state = {"status": "degraded", "reason": "<missing & unsafe>",
                 "events": [], "rows": [], "collection_changes": {}}
        html = reporting.render_html(state)
        self.assertIn("&lt;missing &amp; unsafe&gt;", html)
        self.assertNotIn("<missing", html)
        self.assertIn("<missing & unsafe>", reporting.render_markdown(state))


if __name__ == "__main__":
    unittest.main()
