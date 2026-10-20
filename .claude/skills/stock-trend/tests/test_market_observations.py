#!/usr/bin/env python3
"""Tests for the pure market observation/model-input audit contract."""

import copy
import math
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis.market_observations import (
    build_market_observations,
    component_observation_detail,
)
from reporting.market_observations import render_market_observations


def projected_context():
    return {
        "data_date": "2026-10-20",
        "generated_at": "2026-10-20 13:37:44",
        "as_of": "2026-10-20T13:37:44+08:00",
        "intraday": True,
        "regime": {"score": 61.3},
        "intraday_evidence": {
            "anchor_score": 58.1,
            "projected_score": 65.7,
            "blend_weight": 0.42,
            "anchor_date": "2026-10-19",
            "session_elapsed_fraction": 0.65,
        },
        "detail_inputs": {
            "volume": {"amount_yi": 11022},
            "breadth": {
                "up": 2833, "down": 2545, "flat_count": 12,
                "total_count": 5390, "up_down_sample_count": 5378,
                "suspended_count": 5, "uncovered_count": 7,
            },
            "zt_emotion": {"count": 51, "streak_count": 8, "max_streak": 4},
            "capital": {"main_force_yi": -85.787},
        },
        "model_inputs": {
            "kind": "projection",
            "volume": {"amount_yi": 16849},
            "breadth": {"up": 4331, "down": 3890},
            "zt_emotion": {"count": 78, "streak_count": 8, "max_streak": 4},
            "capital": {"main_force_yi": -131},
        },
        "raw_components": {"volume": {"score": 48.2}},
        "components": {
            "index_trend": {"detail": "三指数状态"},
            "volume": {"detail": "两市 16849亿"},
            "breadth": {"detail": "涨跌 4331/3890"},
            "zt_emotion": {"detail": "涨停 78家"},
            "capital": {"detail": "全市场主力净流入 -131.0亿"},
        },
        "amount_evidence": {
            "completeness": "complete", "provider": "eastmoney",
            "data_date": "2026-10-20", "date_origin": "provider",
            "requested_date": "2026-10-20", "source_timestamp": None,
        },
        "activity_evidence": {
            "completeness": "complete", "provider": "eastmoney",
            "data_date": "2026-10-20", "date_origin": "provider",
            "source_timestamp": None,
        },
        "zt_evidence": {
            "completeness": "complete", "provider": "akshare",
            "data_date": "2026-10-20", "date_origin": "provider",
            "source_timestamp": None,
        },
    }


class MarketObservationTests(unittest.TestCase):
    def test_projection_keeps_raw_and_model_values_separate(self):
        ctx = projected_context()
        before = copy.deepcopy(ctx)

        result = build_market_observations(ctx)

        self.assertEqual(result["schema_version"], "market-observations/v1")
        self.assertEqual(result["raw_inputs"]["zt_emotion"]["count"], 51)
        self.assertEqual(result["model_inputs"]["zt_emotion"]["count"], 78)
        self.assertEqual(result["raw_inputs"]["breadth"]["up"], 2833)
        self.assertEqual(result["raw_inputs"]["breadth"]["flat_count"], 12)
        self.assertEqual(result["raw_inputs"]["breadth"]["total_count"], 5390)
        self.assertEqual(result["model_inputs"]["breadth"]["up"], 4331)
        self.assertEqual(result["raw_inputs"]["volume"]["amount_yi"], 11022)
        self.assertEqual(result["model_inputs"]["volume"]["amount_yi"], 16849)
        self.assertEqual(result["raw_inputs"]["capital"]["main_force_yi"], -85.787)
        self.assertEqual(result["model_inputs"]["capital"]["main_force_yi"], -131)
        self.assertEqual(result["raw_components"], {"volume": {"score": 48.2}})
        self.assertEqual(ctx, before)

    def test_score_context_preserves_anchor_projection_mix_and_reported_score(self):
        scores = build_market_observations(projected_context())["score_context"]
        self.assertEqual(scores, {
            "anchor_score": 58.1,
            "projected_score": 65.7,
            "blend_weight": 0.42,
            "reported_score": 61.3,
            "anchor_date": "2026-10-19",
            "session_fraction": 0.65,
        })

    def test_early_anchor_keeps_raw_facts_and_unknown_model_groups(self):
        ctx = projected_context()
        ctx["model_inputs"] = {
            "kind": "anchor",
            "volume": {"amount_yi": None},
            "breadth": {"up": None, "down": None},
            "zt_emotion": {"count": None, "streak_count": None, "max_streak": None},
            "capital": {"main_force_yi": None},
        }

        result = build_market_observations(ctx)

        self.assertEqual(result["raw_inputs"]["zt_emotion"]["count"], 51)
        self.assertIsNone(result["model_inputs"]["zt_emotion"]["count"])
        self.assertIn("昨收锚定说明", component_observation_detail(ctx, "volume"))

    def test_legacy_context_does_not_parse_capital_detail(self):
        ctx = projected_context()
        del ctx["model_inputs"]
        ctx["amount_yi"] = 16849
        ctx["zt"] = {"count": 78, "streak_count": 8, "max_streak": 4}
        ctx["components"]["breadth"].update(up=4331, down=3890)

        model = build_market_observations(ctx)["model_inputs"]

        self.assertEqual(model["kind"], "legacy_intraday")
        self.assertEqual(model["volume"]["amount_yi"], 16849)
        self.assertEqual(model["breadth"]["up"], 4331)
        self.assertEqual(model["zt_emotion"]["count"], 78)
        self.assertIsNone(model["capital"]["main_force_yi"])

    def test_missing_zero_bool_and_nan_are_not_conflated(self):
        ctx = projected_context()
        ctx["detail_inputs"] = {
            "volume": {"amount_yi": 0},
            "breadth": {"up": True, "down": 0, "flat_count": float("nan")},
            "zt_emotion": {"count": False, "streak_count": 0},
            "capital": {"main_force_yi": math.inf},
        }

        raw = build_market_observations(ctx)["raw_inputs"]

        self.assertEqual(raw["volume"]["amount_yi"], 0)
        self.assertIsNone(raw["breadth"]["up"])
        self.assertEqual(raw["breadth"]["down"], 0)
        self.assertIsNone(raw["breadth"]["flat_count"])
        self.assertIsNone(raw["zt_emotion"]["count"])
        self.assertEqual(raw["zt_emotion"]["streak_count"], 0)
        self.assertIsNone(raw["capital"]["main_force_yi"])

    def test_missing_source_status_does_not_publish_placeholder_zero(self):
        ctx = projected_context()
        ctx["zt_evidence"]["completeness"] = "missing"
        ctx["detail_inputs"]["zt_emotion"] = {
            "count": 0, "streak_count": 0, "max_streak": 0,
        }

        raw = build_market_observations(ctx)["raw_inputs"]["zt_emotion"]

        self.assertIsNone(raw["count"])
        self.assertIsNone(raw["streak_count"])
        self.assertIsNone(raw["max_streak"])

    def test_request_date_never_becomes_provider_date(self):
        ctx = projected_context()
        ctx["amount_evidence"] = {
            "requested_date": "2026-10-20", "data_date": "2026-10-20",
            "date_origin": "request", "source_timestamp": None,
        }

        source = build_market_observations(ctx)["raw_inputs"]["volume"]["source"]

        self.assertIsNone(source["provider_date"])
        self.assertEqual(source["requested_date"], "2026-10-20")
        self.assertIsNone(source["event_time"])
        self.assertEqual(source["alignment"], "unknown")

    def test_legacy_breadth_aliases_publish_standard_count_keys(self):
        ctx = projected_context()
        ctx["detail_inputs"]["breadth"] = {
            "up": 2, "down": 3, "flat": 4, "total": 9,
        }

        breadth = build_market_observations(ctx)["raw_inputs"]["breadth"]

        self.assertEqual(breadth["flat_count"], 4)
        self.assertEqual(breadth["total_count"], 9)
        self.assertNotIn("flat", breadth)
        self.assertNotIn("total", breadth)

    def test_source_date_mismatch_is_explicit(self):
        ctx = projected_context()
        ctx["activity_evidence"]["data_date"] = "2026-10-19"

        observations = build_market_observations(ctx)
        source = observations["raw_inputs"]["breadth"]["source"]

        self.assertEqual(source["alignment"], "mismatched")
        for output_format in ("markdown", "html"):
            rendered = render_market_observations(observations, output_format)
            self.assertIn("来源交易日 2026-10-19", rendered)
            self.assertIn("与报告依据日不一致", rendered)

    def test_details_use_raw_values_and_keep_index_detail(self):
        ctx = projected_context()

        self.assertEqual(component_observation_detail(ctx, "index_trend"), "三指数状态")
        self.assertIn("11022亿", component_observation_detail(ctx, "volume"))
        self.assertIn("2833家", component_observation_detail(ctx, "breadth"))
        self.assertIn("51家", component_observation_detail(ctx, "zt_emotion"))
        self.assertIn("-85.787亿", component_observation_detail(ctx, "capital"))
        self.assertIn("模型外推说明", component_observation_detail(ctx, "capital"))

    def test_both_formats_show_same_values_dates_notes_and_escape_content(self):
        observations = build_market_observations(projected_context())
        observations["raw_inputs"]["volume"]["source"]["event_time"] = '<unsafe & "time">'

        markdown = render_market_observations(observations, "markdown")
        html = render_market_observations(observations, "html")

        for text in (markdown, html):
            for value in ("11022", "16849", "2833", "4331", "51", "78", "-85.787", "-131"):
                self.assertIn(value, text)
            self.assertIn("58.1", text)
            self.assertIn("65.7", text)
            self.assertIn("0.42", text)
            self.assertIn("61.3", text)
            self.assertIn("来源事件时刻 未知", text)
            self.assertIn("12家平盘", text)
            self.assertIn("5390家市场总数", text)
            self.assertIn("未核验为实时或收盘最终值", text)
            self.assertIn("MARKET_OBSERVATIONS:START", text)
            self.assertIn("MARKET_OBSERVATIONS:END", text)
            self.assertNotIn('<unsafe & "time">', text)
            self.assertIn("盘中外推输入", text)
            self.assertNotIn("`projection`", text)
        self.assertIn('id="market-observations"', html)
        self.assertIn('class="market-observations-table-wrap"', html)
        self.assertIn('<a id="market-observations"></a>', markdown)
        self.assertEqual(
            render_market_observations(observations, format="html"), html)

    def test_unknown_flat_and_total_are_explicit_sample_limits(self):
        ctx = projected_context()
        ctx["detail_inputs"]["breadth"] = {"up": 2833, "down": 2545}
        observations = build_market_observations(ctx)

        for output_format in ("markdown", "html"):
            rendered = render_market_observations(observations, output_format)
            self.assertIn("平盘数未保存", rendered)
            self.assertIn("市场总数未保存", rendered)
            self.assertIn("平盘数、市场总数未保存", rendered)
            self.assertIn("该样本不代表市场总数", rendered)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            build_market_observations([])
        with self.assertRaises(TypeError):
            render_market_observations([], "html")
        with self.assertRaises(ValueError):
            render_market_observations({}, "json")


if __name__ == "__main__":
    unittest.main()
