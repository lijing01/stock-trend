"""Point-in-time and fail-closed tests for the recommendation news overlay."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.candidate_news import apply_news_overlay, evaluate_candidate_news
from analysis.recommendation_diagnostics import diagnostic_dimensions
from scans import daily_candidates as daily
from fetchers import candidate_news as fetcher


def _candidate(code, priority, eligible=True):
    return {
        "code": code,
        "execution_priority_score": priority,
        "quality_adjusted_score": priority,
        "score_eligible": True,
        "sector_actionable": True,
        "data_quality": {"eligible": eligible},
        "wyckoff": {"short_term": {"signal_status": "confirmed"}},
    }


class CandidateNewsTests(unittest.TestCase):
    cutoff = "2026-09-09T15:00:00+08:00"

    def test_future_and_unknown_time_are_excluded(self):
        result = evaluate_candidate_news([
            {"title": "公司拟回购股份", "published_at": "2026-09-09 16:00:00",
             "source": "巨潮资讯"},
            {"title": "公司中标重大项目", "source": "公司公告"},
        ], cutoff=self.cutoff)
        self.assertEqual(result["article_count"], 0)
        self.assertEqual(result["excluded"]["future"], 1)
        self.assertEqual(result["excluded"]["unknown_time"], 1)

    def test_official_critical_disclosure_creates_shadow_veto(self):
        result = evaluate_candidate_news([{
            "title": "关于公司被立案调查暨风险提示的公告",
            "published_at": "2026-09-09 08:00:00",
            "source": "巨潮资讯", "url": "https://www.cninfo.com.cn/example",
        }], cutoff=self.cutoff)
        self.assertEqual(result["risk_level"], "critical")
        self.assertTrue(result["shadow_veto"])
        self.assertEqual(result["score"], -3.0)

    def test_uncertain_media_positive_is_small_and_source_weighted(self):
        result = evaluate_candidate_news([{
            "title": "公司拟中标项目，尚存在不确定性",
            "published_at": "2026-09-09 09:00:00", "source": "财经媒体",
        }], cutoff=self.cutoff)
        self.assertGreater(result["score"], 0)
        self.assertLessEqual(result["score"], 0.15)

    def test_risk_negation_does_not_create_false_veto(self):
        result = evaluate_candidate_news([{
            "title": "公司申请撤销退市风险警示并解除质押",
            "published_at": "2026-09-09 09:00:00", "source": "巨潮资讯",
        }], cutoff=self.cutoff)
        self.assertFalse(result["shadow_veto"])
        self.assertEqual(result["risk_level"], "none")

    def test_overlay_is_shadow_only_and_preserves_hard_gates(self):
        original = [_candidate("A", 80), _candidate("B", 79),
                    _candidate("C", 99, eligible=False)]
        frozen = copy.deepcopy(original)
        evidence = {
            "A": [{"title": "公司被立案调查", "published_at": "2026-09-09 09:00:00",
                   "source": "巨潮资讯"}],
            "B": [{"title": "公司中标重大合同", "published_at": "2026-09-09 09:00:00",
                   "source": "巨潮资讯"}],
            "C": [{"title": "公司业绩预增", "published_at": "2026-09-09 09:00:00",
                   "source": "巨潮资讯"}],
        }
        annotated, shadow = apply_news_overlay(
            original, evidence, recommendation_date="2026-09-09",
            cutoff=self.cutoff, policy={"max_recommendations": 2})
        self.assertEqual(original, frozen)
        self.assertFalse(any(row["news_analysis"]["formal_policy_affected"] for row in annotated))
        self.assertEqual(shadow["shadow_selected"], ["B"])
        self.assertEqual(shadow["baseline_order"], ["A", "B", "C"])
        selected = {row["code"]: row["news_analysis"]["shadow_selected"]
                    for row in annotated}
        self.assertEqual(selected, {"A": False, "B": True, "C": False})

    def test_diagnostics_expose_news_dimensions(self):
        dimensions = diagnostic_dimensions({
            "candidate": {"news_analysis": {
                "score": -1.2, "risk_level": "high", "shadow_selected": False}},
            "scores": {}, "sector_persistence": {}, "market_regime": {},
        })
        self.assertEqual(dimensions["news_score_band"], "negative")
        self.assertEqual(dimensions["news_risk_level"], "high")
        self.assertEqual(dimensions["news_shadow_selection"], "not_selected")

    def test_official_snapshot_excludes_shadow_news(self):
        item = _candidate("A", 80)
        item["news_analysis"] = {"score": -3, "shadow_veto": True}
        buckets = {"actionable": [item], "waiting_trigger": [],
                   "next_day_confirmation": [], "observation": [],
                   "data_rejected": []}
        captured = {}

        def save(source):
            captured.update(source)
            return SimpleNamespace(status="created", path=None,
                                   content_sha256="digest",
                                   normalization_warnings=[])

        with patch.object(daily, "save_snapshot_if_official", side_effect=save):
            result = daily._save_recommendation_snapshot(
                [item], [], {"mode": "actionable"}, buckets,
                "2026-09-09", market_regime={})
        self.assertEqual(result["status"], "created")
        self.assertEqual(captured["model_version"], "daily-candidates/v4")
        self.assertNotIn("news_analysis", captured["candidates"][0])
        self.assertNotIn("news_analysis", captured["buckets"]["actionable"][0])

    def test_cninfo_direct_adapter_normalizes_official_row(self):
        class Response:
            def raise_for_status(self):
                return None
            def json(self):
                return {"announcements": [{
                    "announcementTitle": "关于回购股份的公告",
                    "announcementTime": 1788912000000,
                    "announcementId": "id-1",
                }]}

        with patch.object(fetcher, "_cninfo_stock_ids",
                          return_value={"600519": "gssh0600519"}), \
             patch("requests.post", return_value=Response()):
            rows = fetcher._fetch_cninfo(
                "600519", __import__("datetime").date(2026, 9, 1),
                __import__("datetime").date(2026, 9, 9))
        self.assertEqual(rows[0]["source"], "巨潮资讯/公司公告")
        self.assertIn("announcementId=id-1", rows[0]["url"])
        self.assertIn("T", rows[0]["published_at"])

    def test_both_provider_failures_are_unknown_not_no_news(self):
        with patch.object(fetcher, "fetch_one",
                          return_value=("600519", [], ["cninfo:Timeout", "news_em:Timeout"])):
            evidence, meta = fetcher.fetch_candidate_news(
                ["600519"], end_date="2026-09-09", workers=1)
        self.assertNotIn("600519", evidence)
        self.assertEqual(meta["status"], "partial")

    def test_reports_show_auditable_news_evidence(self):
        item = _candidate("A", 80)
        item.update({"name": "测试A", "sector_name": "测试", "composite_score": 80,
                     "wyckoff": {"confidence": 0.6, "short_term": {"signal_status": "confirmed"}}})
        item["news_analysis"] = {
            "status": "ready", "lookback_days": 14, "article_count": 1,
            "score": 0.5, "risk_level": "none", "articles": [{
                "title": "公司中标重大合同", "published_at": "2026-09-09T09:00:00+08:00",
                "label": "positive", "risk_level": "none", "source": "巨潮资讯",
                "url": "https://www.cninfo.com.cn/example",
            }],
        }
        policy = {"mode": "actionable", "max_recommendations": 1, "reasons": []}
        buckets = {"actionable": [item], "waiting_trigger": [],
                   "next_day_confirmation": [], "observation": [], "data_rejected": []}
        shadow = {"status": "ready", "lookback_days": 14, "cutoff": self.cutoff,
                  "baseline_order": ["A"], "shadow_order": ["A"]}
        markdown = daily.generate_report(
            [item], [], 0.1, policy, buckets, news_shadow=shadow)
        html = daily._generate_html(
            [item], [], 0.1, "20260909", policy, buckets, news_shadow=shadow)
        self.assertLess(
            markdown.index("## 今日可执行"),
            markdown.index("## 新闻后置判断（影子观察）"),
        )
        self.assertLess(
            html.index(">今日可执行"),
            html.index(">新闻后置判断（影子观察）"),
        )
        for rendered in (markdown, html):
            self.assertIn("公司中标重大合同", rendered)
            self.assertIn("巨潮资讯", rendered)


def run_candidate_news_tests():
    result = unittest.TextTestRunner(verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CandidateNewsTests))
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    unittest.main()
