#!/usr/bin/env python3
"""Tests for /candidates recommendation policy."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans.daily_candidates import (
    _candidate_diagnostic_text,
    _generate_html,
    build_json_output,
    build_recommendation_policy,
    candidate_rank_score,
    classify_candidates,
    enrich_sector_context,
    generate_report,
    is_recommendation_session,
    merge_sector_resonance,
    resolve_recommendation_date,
)
from scans import daily_candidates as dc


def candidate(code, eligible=True, adjusted_score=80.0,
              sector_actionable=True, score_eligible=True):
    return {
        "code": code,
        "name": f"测试{code}",
        "sector_name": "测试板块",
        "composite_score": 80.0,
        "quality_adjusted_score": adjusted_score,
        "sector_actionable": sector_actionable,
        "sector_type": "mainline" if sector_actionable else "single_day_pulse",
        "score_eligible": score_eligible,
        "wyckoff": {
            "sub_phase": "LPS", "confidence": 0.6,
            "long_term": {
                "eligible": True, "phase_name": "吸筹阶段",
                "confidence": 0.7, "bars_available": 251,
                "minimum_bars": 250, "reason_code": "", "reason": "",
            },
            "alignment": {"label": "中线吸筹，短线买点确认", "recommendation_gate": "actionable"},
        },
        "signals": {},
        "data_quality": {
            "eligible": eligible,
            "coverage": 0.8 if eligible else 0.55,
            "reasons": [] if eligible else ["coverage_below_70pct"],
        },
    }


class TestRecommendationPolicy(unittest.TestCase):
    def test_rankings_cache_persists_verified_data_date(self):
        from fetchers import sector_data

        rankings = {
            "meta": {"complete": True},
            "sectors": [{"up_count": 1, "down_count": 0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "CACHE_DIR", Path(tmpdir)), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"):
            sector_data.save_rankings_cache(
                rankings, data_date="2026-08-06")
            payload = json.loads(
                sector_data.CACHE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(payload["data_date"], "2026-08-06")

    def test_partial_rankings_do_not_overwrite_complete_cache(self):
        from fetchers import sector_data

        complete = {
            "meta": {"complete": True},
            "sectors": [{"code": "BK1", "up_count": 1, "down_count": 0}],
        }
        partial = {
            "meta": {"complete": False},
            "sectors": [{"code": "BK2", "up_count": 1, "down_count": 0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "CACHE_DIR", Path(tmpdir)), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"):
            sector_data.save_rankings_cache(
                complete, data_date="2026-08-06")
            sector_data.save_rankings_cache(
                partial, data_date="2026-08-07")
            payload = json.loads(
                sector_data.CACHE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(payload["data_date"], "2026-08-06")
        self.assertEqual(payload["rankings"]["sectors"][0]["code"], "BK1")

    def test_market_theme_saves_rankings_with_data_date(self):
        from analysis import market_theme

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 6, 16, 0, 0)

        rankings = {
            "meta": {
                "total_sectors": 1,
                "complete": True,
                "data_date": "2026-08-05",
            },
            "sectors": [{"code": "BK1", "up_count": 1, "down_count": 0}],
        }
        hot = [{"code": "BK1", "name": "测试板块"}]
        with patch.object(market_theme, "datetime", FrozenDateTime), \
             patch.object(market_theme, "get_sector_rankings",
                          return_value=rankings), \
             patch.object(market_theme, "rank_hot_sectors",
                          return_value=hot), \
             patch.object(market_theme, "save_rankings_cache") as save, \
             patch.object(market_theme, "append_daily_snapshot"):
            market_theme.get_top_sectors(top_n=1)

        self.assertTrue(save.call_args_list)
        for call in save.call_args_list:
            self.assertEqual(call.kwargs["data_date"], "2026-08-05")

    def test_market_theme_does_not_cache_unverified_ranking_date(self):
        from analysis import market_theme

        rankings = {
            "meta": {"total_sectors": 1, "complete": True},
            "sectors": [{"code": "BK1", "up_count": 1, "down_count": 0}],
        }
        with patch.object(market_theme, "get_sector_rankings",
                          return_value=rankings), \
             patch.object(market_theme, "rank_hot_sectors",
                          return_value=[]), \
             patch.object(market_theme, "save_rankings_cache") as save:
            market_theme.get_top_sectors(top_n=1)

        save.assert_not_called()

    def test_empty_ranking_source_is_incomplete(self):
        from fetchers import sector_data

        empty = {"rc": 0, "data": {"diff": []}}
        concept = {
            "rc": 0,
            "data": {
                "diff": [
                    {
                        "f12": f"BK{i}",
                        "f14": f"概念{i}",
                        "f3": 1,
                        "f62": 0,
                        "f104": 1,
                        "f105": 0,
                    }
                    for i in range(5)
                ]
            },
        }
        with patch.object(sector_data, "_fetch_json",
                          side_effect=[empty, concept]), \
             patch.object(sector_data.time, "sleep"), \
             patch("fetchers.sector_akshare.get_sector_rankings_akshare",
                   return_value=None):
            rankings = sector_data.get_sector_rankings()

        self.assertFalse(rankings["meta"]["complete"])
        self.assertEqual(rankings["meta"]["sources"]["industry"], "empty")

    def test_sparse_nonempty_ranking_sources_are_incomplete(self):
        from fetchers import sector_data

        sparse = {
            "rc": 0,
            "data": {"diff": [{
                "f12": "BK1", "f14": "稀疏板块", "f3": 1,
                "f62": 0, "f104": 1, "f105": 0,
            }]},
        }
        with patch.object(sector_data, "_fetch_json",
                          side_effect=[sparse, sparse]), \
             patch.object(sector_data.time, "sleep"), \
             patch("fetchers.sector_akshare.get_sector_rankings_akshare",
                   return_value=None):
            rankings = sector_data.get_sector_rankings()

        self.assertFalse(rankings["meta"]["complete"])
        self.assertEqual(rankings["meta"]["sources"]["industry"], "sparse")
        self.assertEqual(rankings["meta"]["sources"]["concept"], "sparse")

    def test_partial_akshare_fallback_is_not_promoted_to_complete(self):
        from fetchers import sector_data

        empty = {"rc": 0, "data": {"diff": []}}
        akshare = {
            "meta": {
                "total_sectors": 5,
                "complete": False,
                "sources": {"industry": "ok", "concept": "error"},
            },
            "sectors": [
                {"code": f"BK{i}", "up_count": 1, "down_count": 0}
                for i in range(5)
            ],
        }
        with patch.object(sector_data, "_fetch_json",
                          side_effect=[empty, empty]), \
             patch.object(sector_data.time, "sleep"), \
             patch("fetchers.sector_akshare.get_sector_rankings_akshare",
                   return_value=akshare):
            rankings = sector_data.get_sector_rankings()

        self.assertFalse(rankings["meta"]["complete"])
        self.assertNotEqual(rankings["meta"].get("source"), "akshare")

    def test_akshare_single_subsource_failure_is_incomplete(self):
        import pandas as pd
        from fetchers import sector_akshare

        industries = pd.DataFrame([
            {
                "序号": i,
                "板块": f"行业{i}",
                "涨跌幅": 1,
                "总成交额": 1,
                "净流入": 1,
                "上涨家数": 1,
                "下跌家数": 0,
            }
            for i in range(5)
        ])
        with patch.object(sector_akshare, "HAS_AKSHARE", True), \
             patch.object(sector_akshare.ak,
                          "stock_board_industry_summary_ths",
                          return_value=industries), \
             patch.object(sector_akshare.ak,
                          "stock_board_concept_name_ths",
                          side_effect=RuntimeError("dns")):
            rankings = sector_akshare.get_sector_rankings_akshare()

        self.assertFalse(rankings["meta"]["complete"])
        self.assertEqual(rankings["meta"]["sources"]["industry"], "ok")
        self.assertEqual(rankings["meta"]["sources"]["concept"], "error")

    def test_weekend_uses_latest_trading_date(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 8, 10, 0),
            regime_date="2026-08-07",
            last_trading_date="2026-08-07",
        )
        self.assertEqual(result, "2026-08-07")

    def test_calendar_fallback_handles_first_day_of_month(self):
        from fetchers import sector_data

        class FrozenDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 8, 1, 10, 0)

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"), \
             patch.object(sector_data, "datetime", FrozenDateTime):
            trade_date, source = sector_data.get_last_trading_day()
        self.assertEqual(trade_date, "2026-07-31")
        self.assertEqual(source, "calendar")

    def test_last_trading_day_uses_cached_data_date_not_write_time(self):
        from fetchers import sector_data

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 6, 16, 0, 0)

        payload = {
            "cached_at": "2026-08-06T16:00:00",
            "data_date": "2026-08-05",
            "rankings": {"meta": {"complete": True}, "sectors": []},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "rankings.json"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sector_data, "SNAPSHOT_FILE",
                              Path(tmpdir) / "snapshots.json"), \
                 patch.object(sector_data, "CACHE_FILE", cache_file), \
                 patch.object(sector_data, "datetime", FrozenDateTime):
                trade_date, source = sector_data.get_last_trading_day()

        self.assertEqual(trade_date, "2026-08-05")
        self.assertEqual(source, "cache")

    def test_market_theme_cache_fallback_uses_explicit_data_date(self):
        from analysis import market_theme

        realtime = {
            "meta": {"total_sectors": 1, "complete": False},
            "sectors": [{
                "code": "BK0", "name": "休市数据",
                "up_count": 0, "down_count": 0, "change_pct": 0,
            }],
        }
        cached_hot = [{"code": "BK1", "name": "缓存板块"}]
        cache_payload = {
            "cached_at": "2026-08-06T16:00:00",
            "data_date": "2026-08-05",
            "rankings": {"sectors": [{"up_count": 1, "down_count": 0}]},
            "hot_sectors": cached_hot,
        }
        with patch.object(market_theme, "get_sector_rankings",
                          return_value=realtime), \
             patch.object(market_theme, "rank_hot_sectors", return_value=[]), \
             patch.object(market_theme, "load_rankings_cache_full",
                          return_value=cache_payload):
            hot, data_date, source = market_theme.get_top_sectors(top_n=1)

        self.assertEqual(hot, cached_hot)
        self.assertEqual(data_date, "2026-08-05")
        self.assertEqual(source, "cache")

    def test_premarket_uses_previous_close_date(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-08-05",
            last_trading_date="2026-08-05",
        )
        self.assertEqual(result, "2026-08-05")

    def test_premarket_rejects_stale_snapshot_and_regime_dates(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-07-01",
            last_trading_date="2026-07-01",
        )
        self.assertEqual(result, "2026-08-05")

    def test_premarket_rejects_date_from_previous_week(self):
        result = resolve_recommendation_date(
            now=datetime(2026, 8, 6, 8, 30),
            regime_date="2026-07-30",
            last_trading_date="2026-07-30",
        )
        self.assertEqual(result, "2026-08-05")

    def test_lunch_break_is_still_intraday_provisional(self):
        self.assertTrue(is_recommendation_session(
            datetime(2026, 8, 6, 12, 0)))

    def test_sector_with_three_day_persistence_is_actionable(self):
        ranked = [{
            "code": "BK1", "name": "持续主线",
            "absolute_hot_score": 70, "hot_score": 90,
            "change_pct": 2.0,
        }]
        history = {
            "2026-08-04": [{"code": "BK1", "hot_score": 65,
                            "net_flow": 1e8}],
            "2026-08-05": [{"code": "BK1", "hot_score": 70,
                            "net_flow": 2e8}],
            "2026-08-06": [{"code": "BK1", "hot_score": 75,
                            "net_flow": 3e8}],
        }
        sector = enrich_sector_context(
            ranked, history, hs300_change=0.5)[0]
        self.assertEqual(sector["sector_type"], "mainline")
        self.assertTrue(sector["sector_actionable"])
        self.assertEqual(sector["persistence_days"], 3)
        self.assertGreater(sector["relative_strength"], 0)
        self.assertGreater(sector["capital_persistence"], 50)

    def test_sector_without_history_is_single_day_observation(self):
        ranked = [{
            "code": "BK1", "name": "单日脉冲",
            "absolute_hot_score": 70, "hot_score": 100,
            "change_pct": 3.0,
        }]
        sector = enrich_sector_context(ranked, {}, hs300_change=0.0)[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])
        self.assertEqual(sector["persistence_status"], "history_insufficient")
        self.assertEqual(sector["capital_evidence"], "unknown")

    def test_sector_missing_latest_days_is_not_a_mainline(self):
        ranked = [{
            "code": "BK1", "name": "过期热点",
            "absolute_hot_score": 80, "hot_score": 90,
            "change_pct": 2.0,
        }]
        history = {
            "2026-08-02": [{"code": "BK1", "hot_score": 90}],
            "2026-08-03": [{"code": "BK1", "hot_score": 90}],
            "2026-08-04": [{"code": "BK1", "hot_score": 90}],
            "2026-08-05": [{"code": "BK2", "hot_score": 80}],
            "2026-08-06": [{"code": "BK2", "hot_score": 80}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])

    def test_short_history_does_not_claim_three_day_persistence(self):
        ranked = [{
            "code": "BK1", "name": "新热点",
            "absolute_hot_score": 70, "hot_score": 80,
        }]
        history = {
            "2026-08-05": [{"code": "BK1", "hot_score": 70}],
            "2026-08-06": [{"code": "BK1", "hot_score": 80}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertIsNone(sector["persistence_3d"])

    def test_missing_sector_day_counts_as_zero_in_persistence_window(self):
        ranked = [{
            "code": "BK1", "name": "间断热点",
            "absolute_hot_score": 70, "hot_score": 90,
        }]
        history = {
            "2026-08-04": [{"code": "BK1", "hot_score": 90}],
            "2026-08-05": [{"code": "BK2", "hot_score": 80}],
            "2026-08-06": [{"code": "BK1", "hot_score": 90}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertEqual(sector["persistence_3d"], 60.0)
        self.assertFalse(sector["sector_actionable"])

    def test_stale_sector_history_is_observation_only(self):
        ranked = [{
            "code": "BK1", "name": "过期连续热点",
            "absolute_hot_score": 70, "hot_score": 90,
        }]
        history = {
            "2026-07-01": [{"code": "BK1", "hot_score": 90}],
            "2026-07-02": [{"code": "BK1", "hot_score": 90}],
            "2026-07-03": [{"code": "BK1", "hot_score": 90}],
        }
        sector = enrich_sector_context(
            ranked, history, as_of_date="2026-08-06")[0]
        self.assertEqual(sector["sector_type"], "single_day_pulse")
        self.assertFalse(sector["sector_actionable"])

    def test_capital_persistence_measures_positive_days_not_amount(self):
        ranked = [{
            "code": "BK1", "name": "资金不连续",
            "absolute_hot_score": 70, "hot_score": 80,
        }]
        history = {
            "2026-08-02": [{"code": "BK1", "hot_score": 70,
                            "net_flow": 100e8}],
            "2026-08-03": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-04": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-05": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-06": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertLess(sector["capital_persistence"], 50)
        self.assertEqual(sector["capital_positive_days"], 1)
        self.assertEqual(sector["capital_streak"], 0)

    def test_sector_resonance_merges_zt_and_lhb_scores_by_name(self):
        ranked = [{"code": "BK1", "name": "半导体"}]
        merged = merge_sector_resonance(ranked, [{
            "name": "半导体", "zt_score": 75, "lhb_score": 65,
        }])
        self.assertEqual(merged[0]["zt_score"], 75)
        self.assertEqual(merged[0]["lhb_score"], 65)

    def test_rank_score_prefers_quality_adjusted_score(self):
        self.assertEqual(
            candidate_rank_score(candidate("1", adjusted_score=63.5)), 63.5)

    def test_pick_hot_sectors_uses_absolute_threshold(self):
        rankings = {"meta": {"complete": True}, "sectors": [
            {"code": "BK1", "name": "弱中最强", "change_pct": -1.0,
             "main_force_net": -1e8, "up_count": 2, "down_count": 8},
            {"code": "BK2", "name": "更弱", "change_pct": -3.0,
             "main_force_net": -2e8, "up_count": 2, "down_count": 8},
        ]}
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings):
            picked = dc.pick_hot_sectors(top_n=20, min_hot=45, min_stocks=1)
        self.assertEqual(picked, [])

    def test_pick_hot_sectors_uses_cache_when_live_sources_fail(self):
        row = {
            "code": "BK1", "name": "缓存板块", "change_pct": 2.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }
        cached = {
            "cached_at": "2026-08-06T15:10:00",
            "data_date": "2026-08-06",
            "rankings": {
                "meta": {"total_sectors": 1},
                "sectors": [row],
            },
        }
        history = {
            date: [{"code": "BK1", "hot_score": 70,
                    "net_flow": 1e8}]
            for date in ("2026-08-04", "2026-08-05", "2026-08-06")
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value={
                       "meta": {"total_sectors": 0}, "sectors": []}), \
             patch("fetchers.sector_data.load_rankings_cache_full",
                   return_value=cached), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06")

        self.assertEqual(picked[0]["ranking_source"], "cache")
        self.assertEqual(picked[0]["ranking_data_date"], "2026-08-06")
        self.assertEqual(picked[0]["ranking_quality"], "degraded")

    def test_stale_rankings_cache_is_observation_only(self):
        row = {
            "code": "BK1", "name": "过期缓存板块", "change_pct": 2.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }
        cached = {
            "cached_at": "2026-08-05T15:10:00",
            "data_date": "2026-08-05",
            "rankings": {
                "meta": {"total_sectors": 1},
                "sectors": [row],
            },
        }
        history = {
            date: [{"code": "BK1", "hot_score": 70,
                    "net_flow": 1e8}]
            for date in ("2026-08-04", "2026-08-05", "2026-08-06")
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value={
                       "meta": {"total_sectors": 0}, "sectors": []}), \
             patch("fetchers.sector_data.load_rankings_cache_full",
                   return_value=cached), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06")

        self.assertFalse(picked[0]["sector_actionable"])
        self.assertEqual(picked[0]["sector_type"], "stale_cache")

    def test_legacy_rankings_cache_without_data_date_is_observation_only(self):
        row = {
            "code": "BK1", "name": "旧格式缓存", "change_pct": 2.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }
        cached = {
            "cached_at": "2026-08-06T15:10:00",
            "rankings": {"meta": {"total_sectors": 1}, "sectors": [row]},
        }
        history = {
            date: [{"code": "BK1", "hot_score": 70,
                    "net_flow": 1e8}]
            for date in ("2026-08-04", "2026-08-05", "2026-08-06")
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value={
                       "meta": {"total_sectors": 0}, "sectors": []}), \
             patch("fetchers.sector_data.load_rankings_cache_full",
                   return_value=cached), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06")

        self.assertEqual(picked[0]["ranking_data_date"], "")
        self.assertFalse(picked[0]["sector_actionable"])
        self.assertEqual(picked[0]["sector_type"], "stale_cache")

    def test_partial_live_rankings_prefer_complete_cache(self):
        row = {
            "code": "BK1", "name": "完整缓存", "change_pct": 2.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }
        partial = {
            "meta": {"total_sectors": 1, "complete": False},
            "sectors": [{**row, "name": "部分实时"}],
        }
        cached = {
            "cached_at": "2026-08-06T15:10:00",
            "data_date": "2026-08-06",
            "rankings": {"meta": {"total_sectors": 1}, "sectors": [row]},
        }
        history = {
            date: [{"code": "BK1", "hot_score": 70,
                    "net_flow": 1e8}]
            for date in ("2026-08-04", "2026-08-05", "2026-08-06")
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=partial), \
             patch("fetchers.sector_data.load_rankings_cache_full",
                   return_value=cached), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06")

        self.assertEqual(picked[0]["name"], "完整缓存")
        self.assertEqual(picked[0]["ranking_source"], "cache")

    def test_known_partial_rankings_cache_is_rejected(self):
        row = {
            "code": "BK1", "name": "部分缓存", "change_pct": 2.0,
            "main_force_net": 1e8, "up_count": 9, "down_count": 1,
        }
        cached = {
            "cached_at": "2026-08-06T15:10:00",
            "data_date": "2026-08-06",
            "rankings": {
                "meta": {"total_sectors": 1, "complete": False},
                "sectors": [row],
            },
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value={
                       "meta": {"total_sectors": 0, "complete": False},
                       "sectors": [],
                   }), \
             patch("fetchers.sector_data.load_rankings_cache_full",
                   return_value=cached), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value={}):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06")

        self.assertEqual(picked, [])

    def test_scan_expands_until_eligible_count_reaches_target(self):
        calls = []

        def fake_gather(batch, top_n_per_sector):
            calls.append(list(batch))
            return {"candidates": [{"code": batch[0]}]}

        results = {
            "BK1": [{"code": "1", "composite_score": 80,
                      "sector_code": "BK1",
                      "quality_adjusted_score": 40,
                      "data_quality": {"eligible": True}}],
            "BK2": [{"code": "2", "composite_score": 80,
                      "sector_code": "BK2",
                      "quality_adjusted_score": 80,
                      "data_quality": {"eligible": True}}],
        }

        def fake_phase2(candidates, enable_wyckoff, as_of_date):
            return results[candidates[0]["code"]]

        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            scored = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=1,
                min_score=50, as_of_date="2026-08-06",
                sector_context={
                    "BK1": {
                        "ranking_source": "cache",
                        "ranking_data_date": "2026-08-05",
                        "ranking_quality": "degraded",
                    },
                })
        self.assertEqual(len(calls), 2)
        self.assertEqual({item["code"] for item in scored}, {"1", "2"})
        cached_item = next(item for item in scored if item["code"] == "1")
        self.assertEqual(cached_item["ranking_source"], "cache")
        self.assertEqual(cached_item["ranking_data_date"], "2026-08-05")

    def test_missing_regime_allows_observation_only(self):
        policy = build_recommendation_policy(None, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertEqual(policy["max_recommendations"], 0)

    def test_stale_regime_allows_observation_only(self):
        regime = {"score": 90, "data_date": "2026-08-05"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("regime_stale", policy["reasons"])

    def test_weak_regime_allows_observation_only(self):
        regime = {"score": 59, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        self.assertEqual(policy["mode"], "observation")

    def test_intraday_output_is_provisional_observation(self):
        regime = {"score": 90, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_neutral_regime_limits_waiting_list_to_two(self):
        regime = {"score": 70, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2"), candidate("3")], policy)
        self.assertEqual(policy["mode"], "waiting_trigger")
        self.assertEqual(len(buckets["waiting_trigger"]), 2)
        self.assertEqual(len(buckets["observation"]), 1)

    def test_divergence_requires_verified_sector_capital(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 20},
            "2026-08-06")
        unverified = candidate("unknown")
        verified = candidate("verified")
        verified["sector_capital_evidence"] = "verified"
        buckets = classify_candidates([unverified, verified], policy)
        self.assertEqual([item["code"] for item in buckets["waiting_trigger"]],
                         ["verified"])
        self.assertIn("breadth_capital_divergence",
                      buckets["observation"][0]["observation_reasons"])

    def test_zero_capital_score_still_enables_divergence_gate(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 0},
            "2026-08-06")
        self.assertTrue(policy["requires_sector_capital_proof"])

    def test_neutral_market_builds_non_recommendation_confirmation_list(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 50},
            "2026-08-06")
        item = candidate("watch", sector_actionable=False)
        buckets = classify_candidates([item], policy)
        self.assertEqual([row["code"] for row in buckets["next_day_confirmation"]],
                         ["watch"])
        self.assertIn("次日板块跑赢沪深300",
                      buckets["next_day_confirmation"][0]["confirmation_conditions"])

    def test_confirmation_list_does_not_duplicate_waiting_candidate(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 50},
            "2026-08-06")
        item = candidate("waiting")
        buckets = classify_candidates([item], policy)
        self.assertEqual([row["code"] for row in buckets["waiting_trigger"]],
                         ["waiting"])
        self.assertEqual(buckets["next_day_confirmation"], [])

    def test_strong_regime_never_promotes_ineligible_candidate(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2", eligible=False)], policy)
        self.assertEqual([item["code"] for item in buckets["actionable"]], ["1"])
        self.assertEqual([item["code"] for item in buckets["observation"]], ["2"])

    def test_strong_regime_never_promotes_single_day_pulse(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1", sector_actionable=False)], policy)
        self.assertEqual(buckets["actionable"], [])
        self.assertEqual(buckets["observation"][0]["code"], "1")
        self.assertIn(
            "single_day_pulse",
            buckets["observation"][0]["observation_reasons"],
        )

    def test_low_quality_score_stays_in_observation_pool(self):
        regime = {"score": 85, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates([
            candidate("1", adjusted_score=40, score_eligible=False),
        ], policy)
        self.assertEqual(buckets["actionable"], [])
        self.assertEqual(buckets["observation"][0]["code"], "1")
        self.assertIn(
            "quality_adjusted_below_min_score",
            buckets["observation"][0]["observation_reasons"],
        )

    def test_top_limit_prioritizes_promotable_candidate(self):
        pulse = candidate("pulse", adjusted_score=100,
                          sector_actionable=False)
        valid = candidate("valid", adjusted_score=80)
        selected = dc.select_candidate_pool(
            [pulse, valid], top=1, min_score=50)
        self.assertEqual([item["code"] for item in selected], ["valid"])

    def test_report_renders_all_buckets_and_full_disclaimer(self):
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        cached_observation = candidate("2", eligible=False)
        cached_observation.update({
            "ranking_source": "realtime",
            "ranking_data_date": "2026-08-06",
            "ranking_quality": "good",
            "membership_source": "cache",
            "membership_data_date": "2026-08-05",
            "membership_quality": "degraded",
        })
        buckets = {
            "actionable": [candidate("1")],
            "waiting_trigger": [],
            "observation": [cached_observation],
        }
        report = generate_report(
            buckets["actionable"] + buckets["observation"],
            [("BK1", "测试板块", 80)],
            1.0,
            policy,
            buckets,
        )
        self.assertIn("## 今日可执行", report)
        self.assertIn("## 等待触发", report)
        self.assertIn("## 观察池", report)
        self.assertIn("质量分", report)
        self.assertIn("短线置信度", report)
        self.assertIn("中线置信度", report)
        self.assertIn("K线根数/要求", report)
        self.assertIn("251/250", report)
        self.assertIn("数据维度覆盖率", report)
        self.assertIn("数据问题/异常及原因", report)
        self.assertIn("数据覆盖率55%，低于70%门槛", report)
        self.assertIn("排行 来源 realtime", report)
        self.assertIn("排行 来源 realtime｜日期 2026-08-06｜质量 good", report)
        self.assertIn("成分 来源 cache｜日期 2026-08-05｜质量 degraded", report)
        self.assertIn("中线吸筹，短线买点确认", report)
        self.assertIn("股市有风险，投资需谨慎", report)

    def test_countertrend_wyckoff_candidate_is_observation_only(self):
        item = candidate("countertrend")
        item["wyckoff"]["alignment"] = {
            "label": "中线偏空，短线买点属逆势反弹",
            "recommendation_gate": "observation",
        }
        buckets = classify_candidates([item], {
            "mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "reasons": [],
        })

        self.assertEqual(buckets["actionable"], [])
        self.assertIn("wyckoff_countertrend", buckets["observation"][0]["observation_reasons"])

    def test_html_renders_all_buckets_and_full_disclaimer(self):
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        cached_observation = candidate("2", eligible=False)
        cached_observation.update({
            "ranking_source": "realtime",
            "ranking_data_date": "2026-08-06",
            "ranking_quality": "good",
            "membership_source": "cache",
            "membership_data_date": "2026-08-05",
            "membership_quality": "degraded",
        })
        buckets = {
            "actionable": [candidate("1")],
            "waiting_trigger": [],
            "observation": [cached_observation],
        }
        html = _generate_html(
            buckets["actionable"] + buckets["observation"],
            [("BK1", "测试板块", 80)],
            1.0,
            "20260806-160000",
            policy,
            buckets,
        )
        self.assertIn("今日可执行", html)
        self.assertIn("等待触发", html)
        self.assertIn("观察池", html)
        self.assertIn("排行 来源 realtime｜日期 2026-08-06｜质量 good", html)
        self.assertIn("成分 来源 cache｜日期 2026-08-05｜质量 degraded", html)
        self.assertIn("短线置信度", html)
        self.assertIn("中线置信度", html)
        self.assertIn("251/250", html)
        self.assertIn("股市有风险，投资需谨慎", html)

    def test_report_explains_unavailable_long_term_structure(self):
        item = candidate("1")
        item["wyckoff"]["long_term"] = {
            "eligible": True,
            "phase_name": "无法判定",
            "confidence": 0.0,
            "bars_available": 251,
            "minimum_bars": 250,
            "reason_code": "context_range_missing",
            "reason": "250日内未识别出符合要求的长期箱体",
        }
        buckets = {
            "actionable": [], "waiting_trigger": [], "observation": [item],
        }
        report = generate_report(
            [item], [{"code": "BK1"}], 1.0,
            {"mode": "observation", "max_recommendations": 0,
             "max_portfolio_pct": 0, "reasons": []},
            buckets,
        )

        self.assertIn(
            "无法判定（250日内未识别出符合要求的长期箱体）", report)
        self.assertIn("| 60% | - | 251/250 |", report)

    def test_final_column_explains_data_problem_and_cause(self):
        item = candidate("1", eligible=False)
        item["data_quality"].update({
            "as_of_date": "2026-08-06",
            "reasons": ["kline_stale", "coverage_below_70pct"],
            "dimensions": {
                "kline": {
                    "data_date": "2026-08-05",
                    "source": "cache",
                    "stale_reason": "kline_stale",
                },
            },
        })

        detail = _candidate_diagnostic_text(item)

        self.assertIn("数据问题/异常：K线数据过期", detail)
        self.assertIn("数据日期2026-08-05", detail)
        self.assertIn("要求覆盖至2026-08-06", detail)
        self.assertIn("来源cache", detail)
        self.assertIn("数据覆盖率55%，低于70%门槛", detail)

    def test_final_column_marks_no_data_problem(self):
        detail = _candidate_diagnostic_text(candidate("1"))
        self.assertIn("数据问题/异常：无", detail)

    def test_json_output_keeps_candidates_and_adds_action_buckets(self):
        items = [candidate("1")]
        policy = {
            "mode": "actionable",
            "max_recommendations": 5,
            "max_portfolio_pct": 60,
            "reasons": [],
        }
        buckets = {
            "actionable": items,
            "waiting_trigger": [],
            "observation": [],
        }
        sectors = [{"code": "BK1", "name": "测试板块",
                    "ranking_source": "cache"}]
        output = build_json_output(
            items, sectors, 1.0, policy, buckets)
        self.assertEqual(output["candidates"], items)
        self.assertEqual(output["recommendations"], items)
        self.assertEqual(output["waiting_trigger"], [])
        self.assertEqual(output["observation"], [])
        self.assertEqual(output["policy"], policy)
        self.assertEqual(output["sectors"], sectors)
        self.assertEqual(output["candidates"][0]["quality_adjusted_score"], 80.0)


def run_daily_candidates_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationPolicy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_daily_candidates_tests()
    raise SystemExit(1 if failed else 0)
