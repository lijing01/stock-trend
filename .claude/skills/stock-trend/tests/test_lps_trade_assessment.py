"""Tests for the isolated LPS trade-plan adapter and report renderer."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.lps_trade_assessment import (
    PLAN_SCHEMA_VERSION, STATUS_INSUFFICIENT, STATUS_READY, STATUS_UNAVAILABLE,
    STATUS_WAIT, build_lps_trade_assessment, build_lps_trade_assessment_run,
    validate_lps_trade_plan,
)
from reporting.trade_assessment_report import (
    DISCLAIMER, render_html, render_json, render_markdown,
)


DAY = "2026-09-30"


def record(**overrides):
    timing = {
        "sub_phase": "lps", "signal_status": "confirmed", "signal_age_bars": 1,
        "current_state": "confirmed_holding", "current_close": 10.2,
        "trigger_close": 10.0, "current_atr": 1.0,
        "trigger_extension_atr": .2, "trigger_extension_pct": .02,
    }
    candidate = {
        "code": "600001", "name": "<测试&股份>", "formal_bucket": "actionable",
        "quality_adjusted_score": 88, "sector_actionable": True,
        "data_quality": {"dimensions": {"kline": {"data_date": DAY}}},
        "source_evidence": {"kline": {"source": "fixture", "data_date": DAY,
            "fetched_at": "20260930-152000", "cache_used": False,
            "status": "live_success"}},
        "wyckoff": {"entry_timing": timing,
                    "event_health": {"state": "confirmed_holding",
                                     "structural_floor": 9.0}},
    }
    result = {
        "record_id": "r1", "research_snapshot_sha256": "s1", "code": "600001",
        "basis_date": DAY, "decision_at": "2026-09-30T16:00:00+08:00",
        "final_status": "actionable", "candidate": candidate,
    }
    for key, value in overrides.items():
        if key == "timing":
            timing.update(value)
        elif key == "candidate":
            candidate.update(value)
        else:
            result[key] = value
    return result


def context(**overrides):
    result = {
        "known_at": "2026-09-30T15:30:00+08:00",
        "price_scale_evidence": {"source": "fixture", "scale_id": "raw-v1",
                                 "price_scale": "raw", "known_at": "2026-09-30T15:30:00+08:00"},
        "calendar_evidence": {"calendar_id": "fixture-cn", "source": "fixture",
                              "complete_through": "2026-10-12"},
        "market_sessions": [DAY, "2026-10-09", "2026-10-12"],
        "security_meta": {"code": "600001", "exchange": "SH", "board": "main_board",
            "security_type": "common_stock", "is_st": False,
            "ipo_special_rules": False, "lot_size": 100, "as_of": DAY, "price_limit_pct": 10,
            "source": "fixture"},
        "formal_market_allocation": {"allocation_pct": 30},
        "target_evidence": {"basis_date": DAY, "source": "frozen_resistance", "price_scale": "raw",
                            "resistances": [{"price": 13.0}]},
        "event_check": {"status": "pending", "reason": "公告尚待复核"},
        "cost_config": {"contract_id": "reference-cost-v1", "commission_bps": 3,
                        "minimum_commission_cny": 5, "sell_stamp_tax_bps": 5,
                        "commission_includes_exchange_and_transfer_fees": True,
                        "tax_source": "https://shanghai.chinatax.gov.cn/tax/zcfw/zcfgk/yhs/202308/t468451.html",
                        "tax_effective_from": "2023-08-28", "tax_verified_through": "2026-10-01",
                        "slippage_bps_each_side": 5},
        "price_scale": "raw",
        "counterargument": "若放量跌破结构位，LPS 假设失效。",
    }
    result.update(overrides)
    return result


def policy(**overrides):
    value = {"mode": "actionable", "max_portfolio_pct": 30,
             "max_recommendations": 3}
    value.update(overrides)
    return value


class LpsTradeAssessmentTests(unittest.TestCase):
    def test_availability_and_price_scale_cannot_be_asserted_without_evidence(self):
        for changes in ({"known_at": None}, {"known_at": "2026-09-30T17:00:00+08:00"},
                        {"calendar_evidence": {}}, {"price_scale_evidence": {}},
                        {"price_scale": "adjusted"}):
            with self.subTest(changes=changes):
                result = build_lps_trade_assessment(record(), policy(), DAY, context(**changes))
                self.assertEqual(result["status"], STATUS_INSUFFICIENT)
                self.assertIsNone(result["plan"])

    def test_context_allocation_cannot_raise_frozen_policy(self):
        result = build_lps_trade_assessment(record(), policy(max_portfolio_pct=5), DAY,
                                           context(formal_market_allocation={"allocation_pct": 80}))
        self.assertLessEqual(result["plan"]["position"]["max_portfolio_pct"], 5)

    def test_ready_plan_is_versioned_and_uses_real_next_session(self):
        item = build_lps_trade_assessment(record(), policy(), DAY, context())
        self.assertEqual(item["status"], STATUS_READY, item)
        plan = item["plan"]
        self.assertEqual(plan["schema_version"], PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["validity"], {"trading_sessions": 1,
                                            "entry_session": "2026-10-09"})
        self.assertEqual(plan["entry"], {"low": 10.0, "high": 10.5,
                                         "trigger_price": 10.0, "atr": 1.0})
        self.assertEqual(plan["stop_loss"]["price"], 9.0)
        self.assertEqual(plan["targets"]["primary"], 13.0)
        self.assertEqual(plan["immutable_identity"], "r1@s1")
        self.assertTrue(validate_lps_trade_plan(plan, DAY)["complete"])

    def test_missing_calendar_never_uses_weekday_fallback(self):
        item = build_lps_trade_assessment(
            record(), policy(), DAY, context(market_sessions=[]))
        self.assertEqual(item["status"], STATUS_INSUFFICIENT)
        self.assertIn("calendar_basis_missing", item["status_reasons"])
        self.assertIsNone(item["next_entry_session"])

    def test_calendar_must_be_sorted_and_unique(self):
        item = build_lps_trade_assessment(
            record(), policy(), DAY,
            context(market_sessions=[DAY, "2026-10-09", "2026-10-09"]))
        self.assertEqual(item["status"], STATUS_INSUFFICIENT)
        self.assertIn("calendar_not_sorted_unique", item["status_reasons"])

    def test_cached_midday_close_is_data_insufficient(self):
        row = record()
        row["candidate"]["source_evidence"]["kline"].update(
            fetched_at="20260930-133000", cache_used=True, status="cached_valid")
        item = build_lps_trade_assessment(row, policy(), DAY, context())
        self.assertEqual(item["status"], STATUS_INSUFFICIENT)
        self.assertIn("final_close_evidence_missing", item["status_reasons"])

    def test_waiting_bucket_keeps_complete_plan_but_not_ready_status(self):
        row = record(final_status="waiting_trigger")
        row["candidate"]["formal_bucket"] = "waiting_trigger"
        item = build_lps_trade_assessment(row, policy(), DAY, context())
        self.assertEqual(item["status"], STATUS_WAIT)
        self.assertIsNotNone(item["plan"])
        self.assertIn("formal_waiting_trigger", item["status_reasons"])

    def test_known_st_and_unsupported_board_are_not_plannable(self):
        ctx = context()
        ctx["security_meta"].update(is_st=True, board="star")
        item = build_lps_trade_assessment(record(), policy(), DAY, ctx)
        self.assertEqual(item["status"], STATUS_UNAVAILABLE)
        self.assertIn("st_ineligible", item["status_reasons"])
        self.assertIn("board_not_supported", item["status_reasons"])

    def test_unknown_security_metadata_is_data_insufficient(self):
        item = build_lps_trade_assessment(
            record(), policy(), DAY, context(security_meta={}))
        self.assertEqual(item["status"], STATUS_INSUFFICIENT)
        self.assertIn("security_metadata_missing", item["status_reasons"])

    def test_structural_stop_is_not_moved_to_manufacture_buffer(self):
        row = record()
        row["candidate"]["wyckoff"]["event_health"]["structural_floor"] = 9.8
        item = build_lps_trade_assessment(row, policy(), DAY, context())
        self.assertEqual(item["status"], STATUS_UNAVAILABLE)
        self.assertIn("stop_buffer_insufficient", item["status_reasons"])
        self.assertIsNone(item["plan"])

    def test_resistance_must_meet_rr_without_moving_stop(self):
        ctx = context(target_evidence={"basis_date": DAY, "source": "fixture", "price_scale": "raw",
                                              "resistances": [11.0]})
        item = build_lps_trade_assessment(record(), policy(), DAY, ctx)
        self.assertEqual(item["status"], STATUS_UNAVAILABLE)
        self.assertIn("resistance_rr_below_minimum", item["status_reasons"])

    def test_event_pending_is_separate_and_risk_blocks(self):
        pending = build_lps_trade_assessment(record(), policy(), DAY, context())
        self.assertEqual(pending["event_check"]["display_status"], "待核验")
        self.assertEqual(pending["status"], STATUS_READY)
        risk = build_lps_trade_assessment(
            record(), policy(), DAY,
            context(event_check={"status": "risk", "reason": "重大诉讼",
                                 "reviewed_at": "2026-09-30T15:30:00+08:00",
                                 "evidence": [{"url": "https://www.cninfo.com.cn/fixture",
                                               "published_at": "2026-09-30T14:00:00+08:00",
                                               "known_at": "2026-09-30T15:00:00+08:00"}]}))
        self.assertEqual(risk["status"], STATUS_UNAVAILABLE)
        self.assertIn("event_risk_blocked", risk["status_reasons"])

    def test_untrusted_event_claim_and_cost_id_alone_remain_unknown(self):
        item = build_lps_trade_assessment(record(), policy(), DAY,
            context(event_check={"status": "clear"}, cost_config={"contract_id": "asserted"}))
        self.assertEqual(item["event_check"]["display_status"], "待核验")
        self.assertFalse(item["plan"]["net_return_eligible"])

    def test_cost_unknown_never_claims_net_return_eligibility(self):
        item = build_lps_trade_assessment(
            record(), policy(), DAY, context(cost_config=None))
        self.assertEqual(item["status"], STATUS_READY)
        self.assertFalse(item["plan"]["net_return_eligible"])
        self.assertEqual(item["plan"]["cost_config"]["status"], "cost_unknown")

    def test_missing_immutable_identity_fails_closed(self):
        item = build_lps_trade_assessment(
            record(research_snapshot_sha256=None), policy(), DAY, context())
        self.assertEqual(item["status"], STATUS_INSUFFICIENT)
        self.assertIn("immutable_identity_missing", item["status_reasons"])

    def test_formal_market_hard_gate(self):
        item = build_lps_trade_assessment(
            record(), policy(mode="observation"), DAY, context())
        self.assertEqual(item["status"], STATUS_UNAVAILABLE)
        self.assertIn("market_policy_ineligible", item["status_reasons"])

    def test_reports_include_evidence_cost_counterargument_and_escape_html(self):
        item = build_lps_trade_assessment(record(), policy(), DAY, context())
        artifact = {"schema_version": "lps-trade-assessment-run/v1",
                    "basis_date": DAY, "status": "completed", "items": [item]}
        markdown = render_markdown(artifact)
        html = render_html(artifact)
        payload = json.loads(render_json(artifact))
        self.assertIn("LPS 独立交易评估", markdown)
        self.assertIn("reference-cost-v1", markdown)
        self.assertIn("反方论点", markdown)
        self.assertIn(DISCLAIMER, markdown)
        self.assertIn("&lt;测试&amp;股份&gt;", html)
        self.assertNotIn("<测试&股份>", html)
        self.assertIn(DISCLAIMER, html)
        self.assertEqual(payload["items"][0]["record_id"], "r1")

    def test_run_joins_context_and_simulation_only_by_compound_identity(self):
        row = record()
        row.pop("research_snapshot_sha256")
        good_simulation = {"immutable_identity": "r1@s1",
                           "opportunity_status": "filled"}
        artifact = build_lps_trade_assessment_run(
            [row], policy(), DAY, {"r1@s1": context(), "600001": {}}, "s1",
            {"r1@s1": good_simulation, "600001": {"opportunity_status": "wrong"}})
        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["items"][0]["status"], STATUS_READY)
        self.assertEqual(artifact["items"][0]["simulation"], good_simulation)
        missing = build_lps_trade_assessment_run(
            [row], policy(), DAY, {"600001": context()}, "s1")
        self.assertEqual(missing["items"][0]["status"], STATUS_INSUFFICIENT)
        self.assertIn("assessment_context_missing",
                      missing["items"][0]["status_reasons"])


def run_lps_trade_assessment_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LpsTradeAssessmentTests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    unittest.main()
