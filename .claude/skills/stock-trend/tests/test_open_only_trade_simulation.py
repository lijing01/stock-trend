import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.open_only_trade_simulation import build_contract, simulate_trade


def sessions(count=70):
    result = []
    current = date(2026, 7, 6)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def plan(**overrides):
    value = {
        "schema_version": "lps-trade-plan/v1",
        "plan_status": "ready",
        "immutable_identity": "record-1@snapshot-sha-1",
        "code": "600000",
        "basis_date": "2026-07-06",
        "entry_method": "next_session_open_only_v1",
        "entry": {"low": 9.9, "high": 10.1},
        "stop_loss": {"price": 9.0},
        "targets": {"primary": 11.0},
        "position": {"risk_budget_pct": 0.5, "max_portfolio_pct": 60.0},
        "price_scale": "raw",
        "price_scale_id": "fixture-scale",
        "market_eligibility": {"eligible": True, "allocation_pct": 60.0},
        "validity": {"trading_sessions": 1, "entry_session": "2026-07-07"},
    }
    value.update(overrides)
    return value


def market(count=70, price=10.0):
    days = sessions(count)
    rows = [{
        "date": day, "open": price, "high": price + 0.2,
        "low": price - 0.2, "close": price, "volume": 1_000_000,
    } for day in days]
    benchmark = [{"date": day, "open": 100 + index, "close": 100 + index}
                 for index, day in enumerate(days)]
    return {
        "corporate_actions": {"source": "fixture", "no_actions": True,
                              "complete_from": days[0],
                              "complete_through": days[-1]},
        "calendar": {
            "sessions": days, "calendar_id": "cn-a-v1", "source": "frozen-test",
            "complete_through": days[-1],
        },
        "rows": rows,
        "metadata": {
            "code": "600000", "source": "frozen-test", "as_of": days[0],
            "exchange": "SSE", "board": "main", "security_type": "ordinary_a_share",
            "is_st": False, "ipo_special_rules": False, "lot_size": 100,
            "price_limit_pct": 10, "price_scale": "raw",
            "rules_valid_through": days[-1],
        },
        "benchmark": {
            "metadata": {"source": "frozen-test", "calendar_id": "cn-a-v1", "code": "000300.SH"},
            "rows": benchmark,
        },
    }


def set_bar(data, index, **changes):
    data["rows"][index].update(changes)


class OpenOnlyTradeSimulationTests(unittest.TestCase):
    def test_stop_limit_delay_exits_at_first_available_open_even_after_recovery(self):
        data = market()
        set_bar(data, 2, open=9, high=9, low=9, close=9, limit_down_price=9)
        set_bar(data, 3, open=9.5, high=9.8, low=9.4, close=9.7)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["exit_reason"], "stop_delayed_open")
        self.assertEqual(result["execution"]["exit_reference_price_raw"], 9.5)
        self.assertTrue(result["execution"]["delayed_exit"])

    def test_missing_corporate_action_and_expired_security_rules_block_simulation(self):
        data = market()
        data.pop("corporate_actions")
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "corporate_action_cashflow_mapping_unverified")
        data = market()
        data["metadata"]["rules_valid_through"] = data["calendar"]["sessions"][1]
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "security_rules_coverage_insufficient")

    def test_plan_entry_session_must_match_frozen_calendar(self):
        data = market()
        result = simulate_trade(plan(validity={"trading_sessions": 1, "entry_session": "2026-07-08"}),
                                data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "trade_plan_entry_session_mismatch")

    def test_contract_freezes_reference_and_stress_costs(self):
        reference = build_contract("reference")
        stress = build_contract("stress")
        self.assertEqual(reference["contract_version"], "open-only-v1")
        self.assertEqual(reference["costs"]["buy_commission_bps"], 3.0)
        self.assertEqual(reference["costs"]["minimum_commission_cny_per_side"], 5.0)
        self.assertEqual(reference["costs"]["sell_stamp_tax_bps"], 5.0)
        self.assertEqual(reference["costs"]["buy_slippage_bps"], 5.0)
        self.assertEqual(stress["costs"]["buy_slippage_bps"], 15.0)
        self.assertEqual(reference["rule_sources"]["sell_stamp_tax"]["verified_through"],
                         "2026-10-01")
        with self.assertRaisesRegex(ValueError, "unsupported_cost_scenario"):
            build_contract("custom")

    def test_open_only_does_not_backfill_intraday_zone_touch(self):
        data = market()
        set_bar(data, 1, open=10.5, high=10.6, low=10.0, close=10.1)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["opportunity_status"], "not_filled")
        self.assertEqual(result["execution"]["reason"], "entry_open_outside_zone")

    def test_entry_suspension_and_one_price_limit_up_do_not_fill(self):
        suspended = market()
        set_bar(suspended, 1, volume=0)
        first = simulate_trade(plan(), suspended, suspended["calendar"]["sessions"][20])
        self.assertEqual(first["execution"]["reason"], "entry_suspended")

        limit_up = market()
        set_bar(limit_up, 1, open=11, high=11, low=11, close=11,
                limit_up_price=11)
        second = simulate_trade(
            plan(entry={"low": 10.9, "high": 11.1}, targets={"primary": 12.0}), limit_up,
            limit_up["calendar"]["sessions"][20])
        self.assertEqual(second["execution"]["reason"], "entry_one_price_limit_up")

    def test_slippage_above_entry_high_prevents_fill(self):
        data = market()
        set_bar(data, 1, open=10.1, high=10.2, low=10.0, close=10.1)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "buy_slippage_above_entry_high")

    def test_t_plus_one_and_same_bar_ambiguity_use_stop_first(self):
        data = market()
        set_bar(data, 1, open=10, high=11.5, low=8.5, close=10)
        set_bar(data, 2, open=10, high=11.5, low=8.5, close=10)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["exit_date"], data["calendar"]["sessions"][2])
        self.assertEqual(result["execution"]["exit_reason"], "stop_touch")
        self.assertTrue(result["risk"]["ambiguous_exit"])
        self.assertTrue(result["risk"]["entry_day_invalidation_touched"])

    def test_entry_day_cannot_sell_and_next_day_target_can_exit(self):
        data = market()
        set_bar(data, 1, open=10, high=10.5, low=8.5, close=10)
        set_bar(data, 2, open=10, high=11.2, low=9.8, close=11)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["exit_reason"], "target_touch")
        self.assertEqual(result["execution"]["exit_reference_price_raw"], 11.0)

    def test_gap_stop_uses_actual_open(self):
        data = market()
        set_bar(data, 2, open=8.5, high=8.8, low=8.0, close=8.4)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["exit_reason"], "stop_gap")
        self.assertEqual(result["execution"]["exit_reference_price_raw"], 8.5)

    def test_expiry_close_and_limit_down_delay_are_distinct(self):
        normal = market()
        normal_result = simulate_trade(plan(), normal, normal["calendar"]["sessions"][25])
        self.assertEqual(normal_result["execution"]["exit_reason"], "expiry_close")
        self.assertEqual(normal_result["execution"]["exit_date"], normal["calendar"]["sessions"][20])

        delayed = market()
        expiry = 20
        set_bar(delayed, expiry, open=9, high=9, low=9, close=9,
                limit_down_price=9)
        set_bar(delayed, expiry + 1, open=8.8, high=9, low=8.7, close=8.9)
        delayed_result = simulate_trade(
            plan(stop_loss={"price": 8}), delayed, delayed["calendar"]["sessions"][25])
        self.assertEqual(delayed_result["execution"]["exit_reason"], "expiry_delayed_open")
        self.assertEqual(delayed_result["execution"]["exit_date"],
                         delayed["calendar"]["sessions"][expiry + 1])
        self.assertTrue(delayed_result["execution"]["delayed_exit"])

    def test_event_costs_are_cash_amounts_and_slippage_is_not_double_deducted(self):
        data = market()
        set_bar(data, 2, open=11.2, high=11.3, low=11.1, close=11.2)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        flows = result["cash_flows"]
        self.assertGreaterEqual(flows["buy_commission_cny"], 5)
        self.assertGreaterEqual(flows["sell_commission_cny"], 5)
        self.assertGreater(flows["sell_stamp_tax_cny"], 0)
        expected = flows["recovered_cash_cny"] / flows["invested_cash_cny"] - 1
        self.assertAlmostEqual(result["returns"]["net_return"], expected)
        self.assertAlmostEqual(
            flows["net_profit_cny"],
            flows["recovered_cash_cny"] - flows["invested_cash_cny"])

    def test_position_size_obeys_lot_risk_market_name_and_cash_constraints(self):
        data = market()
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][25])
        sizing = result["execution"]["sizing"]
        qty = sizing["quantity"]
        self.assertEqual(qty % 100, 0)
        self.assertLessEqual(qty * sizing["risk_per_share_at_sizing"], 5_000)
        self.assertLessEqual(qty * sizing["sizing_price_raw"], 200_000)
        self.assertLessEqual(result["cash_flows"]["invested_cash_cny"], 1_000_000)

    def test_missing_formal_market_allocation_keeps_opportunity_as_data_error(self):
        data = market()
        broken = plan(market_eligibility={"eligible": True})
        result = simulate_trade(broken, data, data["calendar"]["sessions"][20])
        self.assertEqual(result["opportunity_status"], "data_error")
        self.assertEqual(result["execution"]["reason"], "formal_market_allocation_missing")

    def test_adjustment_mapping_is_required_and_applied(self):
        data = market()
        adjusted_plan = plan(
            price_scale="qfq",
            entry={"low": 4.95, "high": 5.05},
            stop_loss={"price": 4.5}, targets={"primary": 5.5})
        missing = simulate_trade(adjusted_plan, data, data["calendar"]["sessions"][20])
        self.assertEqual(missing["execution"]["reason"], "adjustment_mapping_missing")
        data["adjustment"] = {
            "scale_id": "fixture-scale", "basis_date": plan()["basis_date"],
            "method": "multiplicative", "adjusted_equals_raw_times_factor": True,
            "source": "frozen-test",
            "factors": {day: 0.5 for day in data["calendar"]["sessions"]},
        }
        complete = simulate_trade(
            adjusted_plan, data, data["calendar"]["sessions"][25])
        self.assertEqual(complete["execution"]["entry_reference_price_raw"], 10.0)
        self.assertEqual(complete["diagnostics"]["price_scale"], "qfq")

    def test_calendar_and_security_metadata_fail_closed(self):
        no_identity = market()
        del no_identity["calendar"]["calendar_id"]
        result = simulate_trade(plan(), no_identity, no_identity["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "calendar_identity_missing")

        unsupported = market()
        unsupported["metadata"]["board"] = "chinext"
        result = simulate_trade(plan(), unsupported, unsupported["calendar"]["sessions"][20])
        self.assertEqual(result["execution"]["reason"], "unsupported_board")

    def test_actual_exit_and_fixed_window_benchmarks_use_distinct_endpoints(self):
        data = market()
        set_bar(data, 2, open=11.2, high=11.3, low=11.1, close=11.2)
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][25])
        self.assertEqual(result["benchmark"]["actual_exit"]["exit_date"],
                         data["calendar"]["sessions"][2])
        self.assertEqual(result["benchmark"]["fixed_window"]["exit_date"],
                         data["calendar"]["sessions"][20])
        self.assertNotEqual(result["benchmark"]["actual_exit"]["return"],
                            result["benchmark"]["fixed_window"]["return"])

    def test_unmatured_and_delayed_positions_retain_denominator_without_fake_net_return(self):
        data = market()
        early = simulate_trade(plan(), data, data["calendar"]["sessions"][10])
        self.assertEqual(early["opportunity_status"], "open_position")
        self.assertIsNone(early["returns"]["net_return"])
        self.assertTrue(early["diagnostics"]["opportunity_retained_in_denominator"])

        delayed = market()
        for index in range(20, 26):
            set_bar(delayed, index, volume=0)
        open_result = simulate_trade(plan(), delayed, delayed["calendar"]["sessions"][25])
        self.assertEqual(open_result["opportunity_status"], "open_delayed_exit")
        self.assertIsNone(open_result["returns"]["net_return"])

    def test_twenty_and_sixty_session_contracts_are_independent(self):
        data = market()
        result20 = simulate_trade(plan(), data, data["calendar"]["sessions"][65], window=20)
        result60 = simulate_trade(plan(), data, data["calendar"]["sessions"][65], window=60)
        self.assertEqual(result20["execution"]["exit_date"], data["calendar"]["sessions"][20])
        self.assertEqual(result60["execution"]["exit_date"], data["calendar"]["sessions"][60])
        self.assertNotEqual(result20["benchmark"]["fixed_window"]["exit_date"],
                            result60["benchmark"]["fixed_window"]["exit_date"])

    def test_tax_period_and_calendar_coverage_are_validated(self):
        data = market()
        future_plan = plan(basis_date="2026-10-02")
        future = simulate_trade(future_plan, data, "2026-10-05")
        self.assertEqual(future["execution"]["reason"], "cost_tax_table_unverified_for_basis_date")

        incomplete = market()
        cutoff = incomplete["calendar"]["sessions"][20]
        incomplete["calendar"]["complete_through"] = incomplete["calendar"]["sessions"][10]
        result = simulate_trade(plan(), incomplete, cutoff)
        self.assertEqual(result["execution"]["reason"], "calendar_coverage_incomplete")

    def test_input_objects_are_not_mutated(self):
        original_plan = plan()
        original_data = market()
        plan_copy = copy.deepcopy(original_plan)
        data_copy = copy.deepcopy(original_data)
        simulate_trade(original_plan, original_data, original_data["calendar"]["sessions"][25])
        self.assertEqual(original_plan, plan_copy)
        self.assertEqual(original_data, data_copy)

    def test_plan_schema_status_identity_and_one_session_validity_are_required(self):
        data = market()
        cutoff = data["calendar"]["sessions"][20]
        cases = (
            (plan(schema_version="candidate-trade-plan/v1"), "trade_plan_schema_invalid"),
            (plan(plan_status="waiting_trigger"), "trade_plan_not_ready"),
            (plan(immutable_identity=""), "trade_plan_identity_missing"),
            (plan(validity={"trading_sessions": 3}), "trade_plan_validity_invalid"),
        )
        for candidate, reason in cases:
            with self.subTest(reason=reason):
                result = simulate_trade(candidate, data, cutoff)
                self.assertEqual(result["execution"]["reason"], reason)

    def test_post_fill_data_gap_preserves_fill_and_incurred_cost(self):
        data = market()
        missing_day = data["calendar"]["sessions"][5]
        data["rows"] = [row for row in data["rows"] if row["date"] != missing_day]
        result = simulate_trade(plan(), data, data["calendar"]["sessions"][20])
        self.assertEqual(result["opportunity_status"], "data_error")
        self.assertEqual(result["execution"]["status"], "hypothetical_open_fill")
        self.assertEqual(result["execution"]["valuation_reason"], "holding_row_missing")
        self.assertGreater(result["cash_flows"]["invested_cash_cny"], 0)
        self.assertTrue(result["diagnostics"]["opportunity_retained_in_denominator"])


def run_open_only_trade_simulation_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OpenOnlyTradeSimulationTests)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    unittest.main()
