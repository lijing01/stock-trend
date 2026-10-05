#!/usr/bin/env python3
"""Offline tests for the U.S.-to-A-share observation-only cross reference."""

import math
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis.cross_market_observation import (  # noqa: E402
    build_cross_market_observation,
)
from reporting.cross_market_observation import (  # noqa: E402
    HTML_BLOCK_END,
    HTML_BLOCK_START,
    MD_BLOCK_END,
    MD_BLOCK_START,
    render_html,
    render_markdown,
)


def mapping_document(*entries):
    return {
        "schema_version": "us-a-share-observation-mapping/v1",
        "version": "2026-10-05.1",
        "verified_at": "2026-10-05",
        "mappings": list(entries),
    }


def mapping(symbol="XLK", relation_type="direct_industry"):
    return {
        "symbol": symbol,
        "direction": "科技行业观察",
        "relation_type": relation_type,
        "sector_names": ["半导体"],
        "sources": [{"title": "官方行业资料", "url": "https://example.com/source?a=1&b=2"}],
        "rationale": "行业层面的观察映射，不构成个股业务关系证明。",
    }


def summary(*, value=1.5, requested="2026-10-03T16:00:00+08:00"):
    return {
        "schema_version": "us-market-summary/v2",
        "basis_date": "2026-09-30",
        "a_share_anchor_date": "2026-09-30",
        "requested_as_of": requested,
        "as_of": requested,
        "latest_completed_session": "2026-10-02",
        "status": "complete",
        "data_quality": "complete",
        "groups": {
            "indices": [],
            "sectors": [{
                "symbol": "XLK", "name": "科技ETF", "daily_pct": value,
                "interval_pct": 2.5, "status": "complete",
                "latest_completed_session": "2026-10-02",
                "actual_end_session": "2026-10-02",
            }],
            "stocks": [],
        },
    }


def sector_state():
    return {
        "schema_version": "sector-persistence-analysis/v1",
        "status": "complete",
        "basis_date": "2026-09-30",
        "items": [{
            "name": "半导体", "type": "industry", "status": "complete",
            "basis_date": "2026-09-30", "return_5d": 0.04,
            "return_20d": 0.12, "consecutive_up_days": 3,
            "consecutive_up_days_lower_bound": False, "reasons": [],
        }],
    }


def observed_items(*, generated="2026-09-30T15:30:00+08:00"):
    base = {
        "schema": "yaml-observation-analysis/v2",
        "status": "ready",
        "data_date": "2026-09-30",
        "evidence_cutoff_date": "2026-09-30",
        "generated_at": generated,
    }
    exact = {
        "code": "688981", "market": "SH", "name": "中芯国际", "status": "ready",
        "data_date": "2026-09-30",
        "data_quality": {"eligible": True},
        "sector_memberships": [{
            "name": "半导体", "sector_type": "industry",
            "membership_data_date": "2026-09-30",
            "membership_quality": "historical_verified",
        }],
        "wyckoff": {"event_health": {"state": "structure_valid"}},
    }
    fuzzy = {
        "code": "600000", "market": "SH", "name": "半导体设备名称相似", "status": "ready",
        "data_date": "2026-09-30", "data_quality": {"eligible": True},
        "sector_memberships": [{
            "name": "半导体设备", "sector_type": "industry",
            "membership_data_date": "2026-09-30",
            "membership_quality": "historical_verified",
        }],
    }
    concept = {
        "code": "000001", "market": "SZ", "name": "概念成员", "status": "ready",
        "data_date": "2026-09-30", "data_quality": {"eligible": True},
        "sector_memberships": [{
            "name": "半导体", "sector_type": "concept",
            "membership_data_date": "2026-09-30",
            "membership_quality": "historical_verified",
        }],
    }
    return {**base, "items": [exact, fuzzy, concept]}


class CrossMarketAnalysisTests(unittest.TestCase):
    def test_builds_versioned_rows_from_exact_frozen_industry_evidence(self):
        result = build_cross_market_observation(
            summary(), sector_state(), observed_items(),
            mappings=mapping_document(mapping()),
        )

        self.assertEqual(result["schema_version"], "cross-market-observation/v1")
        self.assertEqual(result["mapping_version"], "2026-10-05.1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual((row["daily_pct"], row["interval_pct"]), (1.5, 2.5))
        self.assertEqual(row["us_direction"], "上涨")
        self.assertEqual(row["a_share_direction"], "科技行业观察")
        self.assertEqual(row["relation_type"], "direct_industry")
        self.assertEqual([item["code"] for item in row["items"]], ["688981"])
        self.assertEqual(row["sector_evidence"][0]["return_5d"], 0.04)
        self.assertEqual(row["sector_evidence"][0]["status"], "verified")
        conditions = {item["key"]: item for item in row["validation_conditions"]}
        self.assertEqual(conditions["sector_current_state"]["status"], "verified")
        self.assertEqual(conditions["observation_structure"]["status"], "verified")
        self.assertEqual(conditions["activity_recovery"]["status"], "unverified")
        self.assertIn("缺少历史对比证据", conditions["activity_recovery"]["reason"])
        self.assertEqual(result["sources"][0]["title"], "官方行业资料")

    def test_does_not_fuzzy_match_or_treat_mapping_as_stock_business_proof(self):
        result = build_cross_market_observation(
            summary(), sector_state(), observed_items(),
            mappings=mapping_document(mapping()),
        )
        row = result["rows"][0]
        self.assertEqual([item["name"] for item in row["items"]], ["中芯国际"])
        self.assertIn("不构成个股业务关系证明", row["scope_note"])
        self.assertNotIn("供应商", str(row["items"]))

    def test_explicit_codes_only_select_existing_qualified_frozen_items(self):
        entry = mapping("NVDA", "demand")
        entry["sector_names"] = []
        entry["observation_codes"] = ["301489", "999999"]
        observations = observed_items()
        observations["items"].append({
            "code": "301489", "market": "SZ", "name": "思泉新材", "status": "ready",
            "data_date": "2026-09-30", "data_quality": {"eligible": True},
            "sector_memberships": [],
            "wyckoff": {"event_health": {"state": "confirmed_holding"}},
        })
        market = summary()
        market["groups"]["stocks"] = [{
            **market["groups"]["sectors"][0], "symbol": "NVDA", "name": "英伟达",
        }]
        result = build_cross_market_observation(
            market, sector_state(), observations,
            mappings=mapping_document(entry),
        )

        row = result["rows"][0]
        self.assertEqual([item["code"] for item in row["items"]], ["301489"])
        self.assertEqual(row["items"][0]["selection_basis"], "explicit_code")
        self.assertNotIn("999999", str(row["items"]))

    def test_unknown_relation_missing_symbol_and_non_finite_values_degrade(self):
        document = mapping_document(
            mapping("XLK", "invented_relation"), mapping("MISSING"),
        )
        result = build_cross_market_observation(
            summary(value=math.nan), sector_state(), observed_items(), mappings=document,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual([row["symbol"] for row in result["rows"]], ["MISSING"])
        self.assertEqual(result["rows"][0]["status"], "unavailable")
        self.assertIn("unknown_relation_type:XLK", result["reasons"])
        self.assertIn("us_symbol_missing:MISSING", result["reasons"])
        self.assertIn("us_performance_unavailable:MISSING", result["reasons"])

    def test_future_observation_and_mismatched_sector_dates_are_not_used(self):
        sectors = sector_state()
        sectors["basis_date"] = "2026-09-29"
        observations = observed_items(generated="2026-10-04T09:00:00+08:00")
        result = build_cross_market_observation(
            summary(), sectors, observations, mappings=mapping_document(mapping()),
        )

        self.assertEqual(result["status"], "degraded")
        row = result["rows"][0]
        self.assertEqual(row["items"], [])
        self.assertEqual(row["sector_evidence"], [])
        self.assertIn("observation_after_report_cutoff", result["reasons"])
        self.assertIn("sector_basis_mismatch", result["reasons"])
        self.assertTrue(all(
            condition["status"] == "unverified"
            for condition in row["validation_conditions"]
        ))

    def test_non_a_share_identity_and_generic_good_structure_are_not_qualified(self):
        observations = observed_items()
        observations["items"][0]["market"] = "HK"
        result = build_cross_market_observation(summary(), sector_state(), observations,
            mappings=mapping_document(mapping()))
        self.assertEqual(result["rows"][0]["items"], [])
        observations["items"][0]["market"] = "SH"
        observations["items"][0]["wyckoff"]["event_health"]["state"] = "good"
        result = build_cross_market_observation(summary(), sector_state(), observations,
            mappings=mapping_document(mapping()))
        self.assertFalse(result["rows"][0]["items"][0]["structure_verified"])

    def test_anchor_is_not_report_basis_and_empty_interval_stays_empty(self):
        market = summary()
        market["a_share_anchor_date"] = "2026-09-29"
        market["groups"]["sectors"][0]["interval_pct"] = None
        result = build_cross_market_observation(market, sector_state(), observed_items(),
            mappings=mapping_document(mapping()))
        self.assertEqual(result["basis_date"], "2026-09-30")
        self.assertEqual(result["rows"][0]["daily_pct"], 1.5)
        self.assertIsNone(result["rows"][0]["interval_pct"])
        self.assertTrue(result["rows"][0]["items"])

    def test_mismatched_us_session_and_invalid_sector_quality_are_not_evidence(self):
        market = summary()
        market["groups"]["sectors"][0]["latest_completed_session"] = "2026-10-01"
        sectors = sector_state()
        sectors["items"][0]["reasons"] = ["mixed_source"]
        result = build_cross_market_observation(market, sectors, observed_items(),
            mappings=mapping_document(mapping()))
        self.assertIsNone(result["rows"][0]["daily_pct"])
        self.assertIsNone(result["rows"][0]["interval_pct"])
        self.assertEqual(result["rows"][0]["sector_evidence"], [])

    def test_missing_default_mapping_is_a_safe_degradation(self):
        from unittest.mock import patch

        with patch("analysis.cross_market_observation.load_mapping",
                   side_effect=OSError("missing")):
            result = build_cross_market_observation(summary())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["rows"], [])
        self.assertIn("mapping_unavailable", result["reasons"])


class CrossMarketReportingTests(unittest.TestCase):
    def test_html_and_markdown_share_rows_markers_sources_and_disclaimer(self):
        hostile = mapping()
        hostile["direction"] = "科技<script>alert(1)</script>"
        hostile["rationale"] = "行业|观察 <b>raw</b>"
        state = build_cross_market_observation(
            summary(), sector_state(), observed_items(),
            mappings=mapping_document(hostile),
        )

        html = render_html(state)
        markdown = render_markdown(state)
        self.assertIn(HTML_BLOCK_START, html)
        self.assertIn(HTML_BLOCK_END, html)
        self.assertIn(MD_BLOCK_START, markdown)
        self.assertIn(MD_BLOCK_END, markdown)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn(r"行业\|观察 &lt;b&gt;raw&lt;/b&gt;", markdown)
        for value in ("XLK", "中芯国际", "2026-10-05.1", "官方行业资料"):
            self.assertIn(value, html)
            self.assertIn(value, markdown)
        disclaimer = "本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。"
        self.assertIn(disclaimer, html)
        self.assertIn(disclaimer, markdown)


if __name__ == "__main__":
    unittest.main()
