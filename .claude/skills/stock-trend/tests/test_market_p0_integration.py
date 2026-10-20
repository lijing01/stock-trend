"""Offline regressions for P0 source semantics and unchanged scoring."""

import copy
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analysis import market_regime as market
from analysis.observation_list_analysis import _sector_snapshot_complete
from reporting.market_detail_links import render_details


def collect_at(clock):
    end = datetime(2026, 10, 20)
    days = sorted((end - timedelta(days=i)).strftime("%Y%m%d")
                  for i in range(45) if (end - timedelta(days=i)).weekday() < 5)
    rows = [{"trade_date": day, "close": 100 + i, "amount": 1000e8}
            for i, day in enumerate(days)]
    rows[-1]["amount"] = 5511e8

    def index(code, **kwargs):
        kwargs["diagnostics"].update(provider="eastmoney", source="eastmoney",
                                     data_date="2026-10-20", record_count=len(rows))
        return copy.deepcopy(rows)

    evidence = {"provider": "eastmoney", "data_date": "2026-10-20",
                "date_origin": "provider", "requested_date": "2026-10-20",
                "source_timestamp": "2026-10-20T13:37:42+08:00", "completeness": "complete"}
    activity = {"up": 2833, "down": 2545, "main_force_yi": -85.8,
                "main_force_raw_yi": -85.78786915, "up_down_sample_count": 5378,
                "flat_count": None, "total_count": None, "evidence": evidence}
    zt = {"count": 51, "streak_count": 12, "max_streak": 4, "evidence": {
        "provider": "akshare", "data_date": None, "date_origin": "request",
        "requested_date": "2026-10-20", "completeness": "complete"}}
    history = {"2026-10-19": {"date": "2026-10-19", "regime_score": 36.1,
               "amount_yi": 11000, "components": {
                   key: 36.1 for key in market.REGIME_COMPONENT_ORDER}}}
    with patch.object(market, "fetch_index_kline", side_effect=index), \
            patch.object(market, "fetch_sector_rankings", return_value=[]), \
            patch.object(market, "fetch_market_activity", return_value=activity), \
            patch.object(market, "fetch_zt_stats", return_value=zt), \
            patch.object(market, "load_history", return_value=history):
        return market.collect_context(now=clock)


class MarketP0IntegrationTests(unittest.TestCase):
    def test_legacy_review_json_does_not_relabel_sample_count_or_rate_as_total_or_amount(self):
        rows = [{"provider": "eastmoney", "code": "BK1295", "amount": 21.67,
                 "up_count": 2, "down_count": 0, "total_count": 2}]
        original = copy.deepcopy(rows)
        output = market._sector_output_rows(rows)
        self.assertIsNone(output[0]["amount"])
        self.assertEqual(output[0]["turnover_rate"], 21.67)
        self.assertIsNone(output[0]["total_count"])
        self.assertEqual(output[0]["up_down_sample_count"], 2)
        self.assertEqual(rows, original)

    def test_projected_inputs_and_observations_are_distinct_without_score_change(self):
        ctx = collect_at(datetime(2026, 10, 20, 13, 37))
        self.assertEqual(ctx["model_inputs"]["kind"], "projection")
        self.assertEqual(ctx["detail_inputs"]["zt_emotion"]["count"], 51)
        self.assertEqual(ctx["model_inputs"]["zt_emotion"]["count"], 78)
        self.assertEqual(ctx["raw_components"]["breadth"]["up"], 2833)
        self.assertEqual(ctx["components"]["breadth"]["up"], 4331)
        self.assertEqual(ctx["detail_inputs"]["volume"]["amount_yi"], 11022)
        self.assertGreater(ctx["model_inputs"]["volume"]["amount_yi"], 16000)
        scores = ctx["intraday_evidence"]
        self.assertEqual(ctx["regime"]["score"], round(
            (1 - market._blend_weight(scores["fraction"])) * 36.1
            + market._blend_weight(scores["fraction"]) * scores["projected_score"], 1))
        self.assertEqual(scores["projected_score"], market.compute_regime(ctx["components"])["score"])
        self.assertEqual(ctx["market_observations"]["raw_inputs"]["capital"]["main_force_yi"], -85.78786915)
        self.assertIsNone(ctx["market_observations"]["raw_inputs"]["zt_emotion"]["source"]["provider_date"])

    def test_early_anchor_does_not_substitute_yesterdays_inputs_for_raw_counts(self):
        ctx = collect_at(datetime(2026, 10, 20, 9, 40))
        self.assertEqual(ctx["model_inputs"]["kind"], "anchor")
        self.assertEqual(ctx["model_inputs"]["source_date"], "2026-10-19")
        self.assertNotIn("zt_emotion", ctx["model_inputs"])
        self.assertEqual(ctx["market_observations"]["raw_inputs"]["zt_emotion"]["count"], 51)
        self.assertEqual(ctx["regime"]["score"], 36.1)

    def test_close_json_and_both_formats_use_the_same_frozen_values(self):
        ctx = collect_at(datetime(2026, 10, 20, 16, 0))
        self.assertEqual(ctx["model_inputs"]["kind"], "observed")
        self.assertFalse(ctx["intraday"])
        before = copy.deepcopy(ctx)
        output = market.build_agent_output(ctx)
        self.assertEqual(output["market_observations"], ctx["market_observations"])
        for format in ("html", "markdown"):
            content = market.render_market_overview(ctx, format)
            self.assertIn("2833", content)
            self.assertIn("2545", content)
            self.assertIn("51", content)
            self.assertIn("11022", content)
            self.assertIn("-85.78786915", content)
            self.assertIn("原始观测", content)
        self.assertEqual(ctx, before)

    def test_activity_missing_funding_is_unknown_and_sample_is_not_total(self):
        payload = {"data": {"total": 1, "diff": [{
            "f12": "BK0001", "f104": 10, "f105": 5, "f62": None}]}}
        with patch("fetchers.sector_data._fetch_json", return_value=payload):
            activity = market.fetch_market_activity()
        self.assertEqual(activity["up_down_sample_count"], 15)
        self.assertIsNone(activity["total_count"])
        self.assertIsNone(activity["flat_count"])
        self.assertIsNone(activity["main_force_yi"])
        self.assertIsNone(activity["evidence"]["data_date"])
        self.assertEqual(market.score_capital(activity)["data_status"], "missing")

    def test_duplicate_regions_are_not_double_counted(self):
        row = {"f12": "BK0001", "f104": 10, "f105": 5, "f62": 0}
        with patch("fetchers.sector_data._fetch_json", return_value={
                "data": {"total": 2, "diff": [row, dict(row)]}}):
            self.assertIsNone(market.fetch_market_activity())

    def test_up_down_sample_does_not_certify_constituent_completeness(self):
        stocks = [{"code": "600001"}, {"code": "600002"}]
        self.assertFalse(_sector_snapshot_complete(stocks, {"up_count": 2, "down_count": 0}))
        self.assertFalse(_sector_snapshot_complete(stocks, {}))
        self.assertTrue(_sector_snapshot_complete(stocks, {"total_count": 2}))
        self.assertFalse(_sector_snapshot_complete([stocks[0], stocks[0]], {"total_count": 2}))
        for row in stocks:
            row.update(membership_total_count=2, membership_coverage_complete=True)
        self.assertTrue(_sector_snapshot_complete(stocks, {}))

    def test_missing_zt_source_placeholder_zero_is_not_a_market_fact(self):
        ctx = {"intraday": True, "zt_evidence": {"completeness": "missing"},
               "detail_inputs": {"zt_emotion": {"count": 0, "streak_count": 0, "max_streak": 0}}}
        for format in ("html", "markdown"):
            text = render_details(ctx, format)
            self.assertIn("原始涨停池抓取不可用", text)
            self.assertNotIn("涨停家数：0", text)


if __name__ == "__main__":
    unittest.main()
