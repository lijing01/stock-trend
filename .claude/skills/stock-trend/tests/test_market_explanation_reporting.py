#!/usr/bin/env python3
"""Report integration tests for the market explanation contract."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.market_explanation import build_market_explanation
from analysis import market_regime as mr
from reporting.market_explanation import render_market_explanation
from scans import daily_candidates as dc


def explanation_context():
    return {
        "generated_at": "2026-09-07 15:04:32",
        "data_date": "2026-09-07",
        "regime": {
            "score": 53.4, "label": "弱势", "data_quality": "partial",
            "missing_components": [], "partial_components": ["capital"],
        },
        "components": {
            "index_trend": {"score": 13.3, "detail": "三指数MA20状态平均",
                             "data_status": "good"},
            "volume": {"score": 37.7, "detail": "成交额较20日均额 -8%",
                        "data_status": "good"},
            "breadth": {"score": 62.5, "detail": "涨跌家数与行业上涨占比",
                         "data_status": "good"},
            "zt_emotion": {"score": 100.0, "detail": "涨停与连板情绪",
                            "data_status": "good"},
            "capital": {"score": 69.4,
                         "detail": "全市场主力净流入 +323.8亿(北向不可用降级)",
                         "data_status": "partial"},
        },
        "indices": {
            "000001.SH": {"ok": True, "close": 1},
            "000300.SH": {"ok": True, "close": 1},
            "399001.SZ": {"ok": True, "close": 1},
        },
        "index_data_quality": {},
    }


class TestMarketExplanationReporting(unittest.TestCase):
    def test_markdown_and_html_render_all_explanation_evidence(self):
        explanation = build_market_explanation(
            explanation_context(), "2026-09-07")
        markdown = render_market_explanation(explanation, "markdown")
        html = render_market_explanation(explanation, "html")

        for text in (markdown, html):
            self.assertIn("53.4", text)
            self.assertIn("index_trend", text)
            self.assertIn("capital", text)
            self.assertIn("regime_data_partial", text)
            self.assertIn("regime_weak", text)
            self.assertIn("贡献", text)
        self.assertIn("000300.SH", markdown)

    def test_html_escapes_untrusted_detail(self):
        ctx = explanation_context()
        ctx["components"]["volume"]["detail"] = "<script>alert(1)</script>"
        explanation = build_market_explanation(ctx, "2026-09-07")
        html = render_market_explanation(explanation, "html")

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_candidate_report_uses_supplied_frozen_explanation(self):
        explanation = build_market_explanation(
            explanation_context(), "2026-09-07")
        regime = {
            "score": 53.4, "label": "弱势", "data_date": "2026-09-07",
            "data_quality": "partial", "partial_components": ["capital"],
            "missing_components": [], "market_explanation": explanation,
        }
        policy = {"mode": "observation", "max_recommendations": 0,
                  "reasons": ["regime_data_partial"]}
        buckets = {"actionable": [], "waiting_trigger": [],
                   "next_day_confirmation": [], "observation": [],
                   "data_rejected": []}

        markdown = dc.generate_report([], [], 0.1, policy, buckets,
                                      market_regime=regime)
        html = dc._generate_html([], [], 0.1, "20260907-150000", policy,
                                 buckets, market_regime=regime)
        payload = dc.build_json_output([], [], 0.1, policy, buckets,
                                       market_regime=regime)

        self.assertIn("市场环境解释", markdown)
        self.assertIn("市场环境解释", html)
        self.assertEqual(payload["market_regime"]["market_explanation"],
                         explanation)

    def test_old_cache_gets_conservative_explanation_without_network(self):
        ctx = explanation_context()
        ctx.pop("indices")
        ctx.pop("index_data_quality")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_regime.json"
            path.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
            with patch.object(dc, "CACHE_DIR", Path(tmp)):
                loaded = dc.load_regime_context()

        self.assertIsNotNone(loaded)
        self.assertIn("market_explanation", loaded)
        self.assertIn("legacy_context_evidence_unknown",
                      loaded["market_explanation"]["quality_notes"])

    def test_official_snapshot_uses_legacy_market_regime_projection(self):
        explanation = build_market_explanation(
            explanation_context(), "2026-09-07")
        regime = {
            "score": 53.4, "label": "弱势", "data_date": "2026-09-07",
            "advice": "观察", "data_quality": "partial",
            "missing_components": [], "partial_components": ["capital"],
            "score_lower": 53.4, "score_upper": 53.4,
            "hs300_change": -0.2, "capital_score": 69.4,
            "market_explanation": explanation,
        }
        captured = {}

        def fake_save(source):
            captured.update(source)
            return types.SimpleNamespace(status="created", path=None,
                                         content_sha256="digest")

        policy = {"mode": "observation", "max_recommendations": 0,
                  "reasons": ["regime_weak"]}
        buckets = {"actionable": [], "waiting_trigger": [],
                   "next_day_confirmation": [], "observation": [],
                   "data_rejected": []}
        with patch.object(dc, "save_snapshot_if_official", side_effect=fake_save):
            result = dc._save_recommendation_snapshot(
                [], [], policy, buckets, "2026-09-07", market_regime=regime)

        self.assertEqual(result["status"], "created")
        self.assertNotIn("market_explanation", captured["market_regime"])
        self.assertEqual(captured["market_regime"]["score"], 53.4)

    def test_daily_review_consumes_same_explanation_object(self):
        ctx = explanation_context()
        explanation = build_market_explanation(ctx, "2026-09-07")
        ctx["market_explanation"] = explanation
        ctx.update({"stale_note": "", "intraday_note": "", "amount_yi": 19460,
                    "zt": {"count": 93, "streak_count": 13},
                    "top_sectors": [], "bottom_sectors": [],
                    "holdings": [], "plan": []})

        markdown = mr.generate_report(ctx)
        html = mr._generate_html(ctx, "20260907-150432")
        self.assertIn("市场环境解释", markdown)
        self.assertIn("53.4", html)
        self.assertIn("regime_data_partial", markdown)


if __name__ == "__main__":
    unittest.main()
