#!/usr/bin/env python3
"""Tests for /candidates recommendation policy."""
import json
import io
import sys
import tempfile
import types
import unittest
import copy
import numpy as np
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scans.daily_candidates import (
    _candidate_diagnostic_text,
    _append_candidate_table,
    _complete_performance,
    _emit_performance_summary,
    _generate_html,
    _is_final_valid_candidate,
    _freeze_output_envelope,
    _trade_plan_text,
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
from scans import stock_scanner as sc


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
            "minor_phase": {
                "code": "D",
                "name": "阶段D：需求确认",
                "description": "需求占优，回踩缩量后等待向上确认",
            },
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
        "trade_plan": {
            "schema_version": "candidate-trade-plan/v1",
            "basis_date": "2026-08-06",
            "action": "buy",
            "entry": {"low": 10.0, "high": 10.5},
            "confirmation": "收盘站上10.5且量能确认",
            "invalidation": "收盘跌破9.5",
            "stop_loss": {"price": 9.5},
            "targets": {"conservative": 11.0, "primary": 12.0,
                        "aggressive": 14.0},
            "risk_reward": {"supplied": 1.5, "recomputed": 1.5},
            "target_source": "resistance",
            "position": {"max_portfolio_pct": 10.0},
            "horizon": {"min_trading_days": 20,
                        "max_trading_days": 120},
            "validity": {"trading_sessions": 3},
            "counterargument": "跌破结构支撑则逻辑失效",
            "event_check": {"status": "not_implemented"},
        },
        "trade_plan_status": "complete",
        "trade_plan_reasons": [],
    }


def _complete_rankings_and_history():
    row = {
        "code": "BK1", "name": "测试板块", "change_pct": 2.0,
        "main_force_net": 1e8, "up_count": 9, "down_count": 1,
    }
    rankings = {
        "meta": {"complete": True, "data_date": "2026-08-06"},
        "sectors": [row],
    }
    history = {
        date: [{"code": "BK1", "hot_score": 70, "net_flow": 1e8}]
        for date in ("2026-08-04", "2026-08-05", "2026-08-06")
    }
    return rankings, history


class TestRecommendationPolicy(unittest.TestCase):
    def test_trade_plan_text_discloses_source_and_unavailable_rr(self):
        resistance_text = _trade_plan_text(candidate("resistance"))
        self.assertIn("目标来源 阻力位", resistance_text)
        self.assertIn("R:R 1.50", resistance_text)

        mismatch_item = candidate("mismatch")
        mismatch_item["trade_plan"]["risk_reward"]["recomputed"] = 99.0
        mismatch_text = _trade_plan_text(mismatch_item)
        self.assertIn("R:R 1.50", mismatch_text)
        self.assertNotIn("R:R 99.00", mismatch_text)

        atr_item = candidate("atr")
        atr_item["trade_plan"].update({
            "target_source": "atr_projection",
            "risk_reward": {"supplied": 1.2, "recomputed": 1.23},
        })
        atr_text = _trade_plan_text(atr_item)
        self.assertIn("目标来源 ATR投射（仅观察）", atr_text)

        unavailable_item = candidate("unavailable")
        unavailable_item["trade_plan"].update({
            "target_source": "unavailable",
            "target_reason": "没有高于计划入场价的有效目标梯度",
            "targets": {"conservative": None, "primary": None,
                        "aggressive": None},
            "risk_reward": {"supplied": None, "recomputed": None},
        })
        unavailable_text = _trade_plan_text(unavailable_item)
        self.assertIn("R:R —", unavailable_text)
        self.assertIn("目标来源 目标不可用", unavailable_text)
        self.assertNotIn("R:R 2.0", unavailable_text)

        legacy_item = candidate("legacy")
        legacy_item["trade_plan"].update({
            "target_source": "synthetic_fallback",
            "risk_reward": {"supplied": 2.0, "recomputed": 2.0},
            "targets": {"conservative": 11.0, "primary": 12.0,
                        "aggressive": 14.0},
        })
        legacy_text = _trade_plan_text(legacy_item)
        self.assertIn("目标来源 目标不可用", legacy_text)
        self.assertIn("目标—/—/—", legacy_text)
        self.assertIn("R:R —", legacy_text)
        self.assertNotIn("R:R 2.00", legacy_text)

        invalid_ladder_item = candidate("invalid-ladder")
        invalid_ladder_item["trade_plan"].update({
            "targets": {"conservative": 10.1, "primary": 10.2,
                        "aggressive": 10.3},
            "risk_reward": {"supplied": 2.0, "recomputed": 2.0},
        })
        invalid_ladder_text = _trade_plan_text(invalid_ladder_item)
        self.assertIn("目标—/—/—", invalid_ladder_text)
        self.assertIn("R:R —", invalid_ladder_text)

    def test_report_contains_target_source_audit_in_markdown_and_html(self):
        resistance = candidate("resistance")
        atr = candidate("atr")
        atr["trade_plan"]["target_source"] = "atr_projection"
        unavailable = candidate("unavailable")
        unavailable["trade_plan"].update({
            "target_source": "unavailable",
            "targets": {"conservative": None, "primary": None,
                        "aggressive": None},
            "risk_reward": {"supplied": None, "recomputed": None},
        })
        items = [resistance, atr, unavailable]
        buckets = {
            "actionable": [resistance], "waiting_trigger": [],
            "next_day_confirmation": [], "observation": [atr, unavailable],
        }
        policy = {"mode": "actionable", "max_recommendations": 5,
                  "max_portfolio_pct": 60, "reasons": []}
        report = generate_report(items, [], 0.1, policy, buckets)
        html = _generate_html(items, [], 0.1, "20260820-160000",
                              policy, buckets)
        audit = "目标来源审计：阻力位 1｜ATR投射（仅观察） 1｜目标不可用 1"
        self.assertIn(audit, report)
        self.assertIn(audit, html)

    def test_markdown_trade_plan_pipes_are_escaped(self):
        lines = []
        _append_candidate_table(lines, "测试", [candidate("600000")], "无")
        row = lines[-1]
        self.assertIn(r"入场10.0~10.5 \| 止损9.5", row)

    def test_final_valid_count_uses_same_predicate_as_scan_early_stop(self):
        valid = candidate("valid", adjusted_score=70)
        low_score = candidate("low", adjusted_score=49)
        stale = candidate("stale", eligible=False, adjusted_score=90)
        pulse = candidate(
            "pulse", adjusted_score=90, sector_actionable=False)
        items = [valid, low_score, stale, pulse]
        buckets = {
            "actionable": [valid], "waiting_trigger": [],
            "observation": [low_score, stale, pulse],
        }

        performance = _complete_performance(
            {}, None, items, buckets, min_score=50, total_seconds=1.25)

        self.assertEqual(
            performance["final_valid_count"],
            sum(_is_final_valid_candidate(item, 50) for item in items),
        )
        self.assertEqual(performance["final_valid_count"], 1)

    def test_complete_performance_preserves_degraded_scan_evidence(self):
        performance = {
            "degradation_reasons": ["resonance_error:RuntimeError"],
            "failed_batches": [{"sectors": ["BK1"], "reason": "OSError"}],
        }
        completed = _complete_performance(
            performance, None, [],
            {"actionable": [], "waiting_trigger": [], "observation": []},
            min_score=50, total_seconds=1.0)
        self.assertEqual(completed["scan_status"], "degraded")
        self.assertEqual(completed["degradation_reasons"],
                         ["resonance_error:RuntimeError"])
        self.assertEqual(completed["failed_batches"][0]["sectors"], ["BK1"])

    def test_advisory_reason_does_not_degrade_scan_status(self):
        performance = {
            "advisory_reasons": ["resonance_stale:date_mismatch"],
        }
        completed = _complete_performance(
            performance, None, [],
            {"actionable": [], "waiting_trigger": [], "observation": []},
            min_score=50, total_seconds=1.0)
        self.assertEqual(completed["scan_status"], "complete")
        self.assertEqual(completed["advisory_reasons"],
                         ["resonance_stale:date_mismatch"])

    def test_complete_performance_marks_all_failed_batches_as_error(self):
        performance = {
            "batch_count": 2,
            "failed_batches": [
                {"sectors": ["BK1"], "reason": "OSError"},
                {"sectors": ["BK2"], "reason": "TimeoutError"},
            ],
            "degradation_reasons": [
                "batch_error:OSError", "batch_error:TimeoutError",
            ],
        }
        completed = _complete_performance(
            performance, None, [],
            {"actionable": [], "waiting_trigger": [], "observation": []},
            min_score=50, total_seconds=1.0)
        self.assertEqual(completed["scan_status"], "error")

    def test_performance_audit_renders_in_markdown_html_and_stderr(self):
        policy = {
            "mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "reasons": [],
        }
        items = [candidate("1")]
        buckets = {
            "actionable": items, "waiting_trigger": [],
            "observation": [],
        }
        source_row = {
            "logical_live_requests": 1, "provider_attempts": 2,
            "cache_hits": 0, "failures": 0, "circuit_breaks": 0,
            "failure_reasons": {}, "state": "healthy",
        }
        performance = {
            "sector_ranking_seconds": 0.1,
            "sector_membership_seconds": 0.2,
            "kline_seconds": 0.3, "wyckoff_seconds": 0.01,
            "capital_seconds": 0.2, "fundamental_seconds": 0.2,
            "report_seconds": 0.0, "total_seconds": 1.1,
            "batch_count": 1, "raw_candidate_count": 2,
            "unique_candidate_count": 1, "wyckoff_pass_count": 1,
            "final_candidate_count": 1, "final_valid_count": 1,
            "actionable_count": 1,
            "advisory_reasons": ["resonance_stale:date_mismatch"],
            "sector_scan_coverage": 0.5,
            "sector_expansion_truncated": True,
            "sector_expansion_total_count": 7,
            "sources": {
                name: dict(source_row) for name in (
                    "sector_ranking", "sector_membership", "kline",
                    "capital", "fundamental")
            },
        }

        report = generate_report(
            items, [{"code": "BK1"}], 1.1, policy, buckets,
            performance=performance)
        html = _generate_html(
            items, [{"code": "BK1"}], 1.1, "20260806-160000",
            policy, buckets, performance=performance)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _emit_performance_summary(performance)

        self.assertIn("## 性能与数据源审计", report)
        self.assertIn("板块成分", report)
        self.assertIn("sector_membership", report)
        self.assertIn("性能与数据源审计", html)
        self.assertIn("sector_membership", html)
        self.assertIn("板块覆盖率", report)
        self.assertIn("板块覆盖率", html)
        self.assertIn("--max-sector-expansion 7", report)
        self.assertIn("--max-sector-expansion 7", html)
        self.assertIn("resonance_stale:date_mismatch", report)
        self.assertIn("resonance_stale:date_mismatch", html)
        self.assertIn("[performance]", stderr.getvalue())
        self.assertIn("final_valid=1", stderr.getvalue())
        for field in (
                "sector_ranking", "sector_membership", "kline", "wyckoff",
                "capital", "fundamental", "report", "total"):
            self.assertIn(f"{field}=", stderr.getvalue())

    def test_stderr_sorts_failure_reasons(self):
        performance = {
            **{field: 0.0 for field in dc._PERFORMANCE_PHASE_FIELDS},
            "sources": {"kline": {
                "logical_live_requests": 2, "provider_attempts": 2,
                "cache_hits": 0, "failures": 2, "circuit_breaks": 1,
                "failure_reasons": {"timeout": 1, "dns": 1},
                "state": "unavailable",
            }},
        }
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _emit_performance_summary(performance)

        self.assertIn('reasons={"dns":1,"timeout":1}', stderr.getvalue())

    def test_renderers_do_not_mutate_frozen_performance_snapshot(self):
        policy = {
            "mode": "actionable", "max_recommendations": 5,
            "max_portfolio_pct": 60, "reasons": [],
        }
        items = [candidate("1")]
        buckets = {
            "actionable": items, "waiting_trigger": [], "observation": [],
        }
        performance = _complete_performance(
            {}, None, items, buckets, 50, 1.0)
        performance["report_seconds"] = 0.125
        before = copy.deepcopy(performance)

        generate_report(
            items, [{"code": "BK1"}], 1.0, policy, buckets,
            performance=performance)
        _generate_html(
            items, [{"code": "BK1"}], 1.0, "20260806-160000",
            policy, buckets, performance=performance)

        self.assertEqual(performance, before)

    def test_output_envelope_times_all_formats_once_and_freezes_one_snapshot(self):
        performance = {"report_seconds": 0.0, "total_seconds": 1.0}
        with patch.object(dc.time, "monotonic", side_effect=[10.0, 10.25]):
            outputs, snapshot = _freeze_output_envelope(
                performance,
                [("markdown", lambda: "md"), ("html", lambda: "html")],
                run_started_at=9.0,
            )

        self.assertEqual(outputs, {"markdown": "md", "html": "html"})
        self.assertEqual(snapshot["report_seconds"], 0.25)
        self.assertEqual(snapshot["total_seconds"], 1.25)
        self.assertEqual(performance["report_seconds"], 0.0)

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

    def test_rankings_cache_rejects_unverified_date(self):
        from fetchers import sector_data

        rankings = {
            "meta": {"complete": True},
            "sectors": [{"up_count": 1, "down_count": 0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "CACHE_DIR", Path(tmpdir)), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"):
            with self.assertRaises(ValueError):
                sector_data.save_rankings_cache(
                    rankings, data_date="2026-08-08")

    def test_sector_snapshot_requires_verified_matching_date(self):
        from fetchers import sector_data

        rankings = {
            "meta": {"complete": True, "data_date": "2026-08-06"},
            "sectors": [{"code": "BK1", "name": "测试", "up_count": 4,
                         "down_count": 1, "change_pct": 1.0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "CACHE_DIR", Path(tmpdir)), \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"):
            sector_data.append_daily_snapshot(rankings, override_date="2026-08-06")
            self.assertTrue(sector_data.SNAPSHOT_FILE.exists())
            with self.assertRaises(ValueError):
                sector_data.append_daily_snapshot(
                    rankings, override_date="2026-08-07")
            with self.assertRaises(ValueError):
                sector_data.append_daily_snapshot(
                    rankings, override_date="2026-08-08")

    def test_snapshot_history_filters_non_iso_and_weekend_keys(self):
        from fetchers import sector_data

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"):
            sector_data.SNAPSHOT_FILE.write_text(json.dumps({
                "20260731": [],
                "2026-08-01": [],
                "2026-08-06": [],
            }), encoding="utf-8")
            history = sector_data.load_snapshot_history(days=10)
        self.assertEqual(list(history), ["2026-08-06"])

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
        self.assertEqual(sector["capital_evidence"], "positive_verified")

    def test_enriched_sector_context_preserves_hot_rank_position(self):
        ranked = [
            {"code": "BK0732", "name": "高热度", "absolute_hot_score": 80},
            {"code": "BK0001", "name": "低热度", "absolute_hot_score": 50},
        ]

        enriched = enrich_sector_context(ranked, {})

        self.assertEqual(
            [sector["ranking_position"] for sector in enriched], [1, 2])

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

    def test_negative_sector_flows_are_not_positive_verified(self):
        ranked = [{"code": "BK1", "name": "资金流出",
                   "absolute_hot_score": 70, "hot_score": 80}]
        history = {
            "2026-08-04": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -2e8}],
            "2026-08-05": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -1e8}],
            "2026-08-06": [{"code": "BK1", "hot_score": 70,
                            "net_flow": -3e8}],
        }
        sector = enrich_sector_context(ranked, history)[0]
        self.assertEqual(sector["capital_evidence"], "partial")

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

    def test_pick_hot_sectors_returns_all_absolute_heat_qualified_sectors(self):
        rows = [
            {"code": f"BK{i:02d}", "name": f"板块{i:02d}",
             "change_pct": 2.0, "main_force_net": 1e8,
             "up_count": 9, "down_count": 1}
            for i in range(21)
        ]
        rankings = {"meta": {"complete": True}, "sectors": rows}
        history = {
            date: [{"code": row["code"], "hot_score": 70,
                    "net_flow": 1e8} for row in rows]
            for date in ("2026-08-04", "2026-08-05", "2026-08-06")
        }
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings), \
             patch("fetchers.sector_data.save_rankings_cache"), \
             patch("fetchers.sector_data.append_daily_snapshot"), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(min_stocks=1)
        self.assertEqual(len(picked), 21)
        self.assertEqual(picked[-1]["code"], "BK20")

    def test_pick_hot_sectors_survives_rankings_cache_write_failure(self):
        rankings, history = _complete_rankings_and_history()
        metrics = {}
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings), \
             patch("fetchers.sector_data.save_rankings_cache",
                   side_effect=OSError("disk full")), \
             patch("fetchers.sector_data.append_daily_snapshot"), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06", metrics=metrics)
        self.assertTrue(picked)
        self.assertIn(
            "ranking_cache_write_error:OSError",
            metrics["degradation_reasons"],
        )

    def test_pick_hot_sectors_survives_snapshot_write_failure(self):
        rankings, history = _complete_rankings_and_history()
        metrics = {}
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings), \
             patch("fetchers.sector_data.save_rankings_cache"), \
             patch("fetchers.sector_data.append_daily_snapshot",
                   side_effect=OSError("disk full")), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06", metrics=metrics)
        self.assertTrue(picked)
        self.assertIn(
            "sector_snapshot_write_error:OSError",
            metrics["degradation_reasons"],
        )

    def test_pick_hot_sectors_marks_resonance_failure_without_blocking(self):
        rankings, history = _complete_rankings_and_history()
        metrics = {}
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings), \
             patch("fetchers.sector_data.save_rankings_cache"), \
             patch("fetchers.sector_data.append_daily_snapshot"), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history), \
             patch("bridge.sector_feeder.load_qualified_sectors",
                   side_effect=RuntimeError("feed unavailable")):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06", metrics=metrics)
        self.assertEqual(picked[0]["resonance_quality"], "error")
        self.assertEqual(picked[0]["resonance_reason"], "RuntimeError")
        self.assertTrue(picked[0]["sector_actionable"])
        self.assertIn("resonance_error:RuntimeError",
                      metrics["degradation_reasons"])

    def test_pick_hot_sectors_marks_stale_resonance_provenance(self):
        rankings, history = _complete_rankings_and_history()
        metrics = {}
        stale_resonance = types.SimpleNamespace(
            date="2026-08-05", sectors=[])
        with patch("fetchers.sector_data.get_sector_rankings",
                   return_value=rankings), \
             patch("fetchers.sector_data.save_rankings_cache"), \
             patch("fetchers.sector_data.append_daily_snapshot"), \
             patch("fetchers.sector_data.load_snapshot_history",
                   return_value=history), \
             patch("bridge.sector_feeder.load_qualified_sectors",
                   return_value=stale_resonance):
            picked = dc.pick_hot_sectors(
                min_stocks=1, as_of_date="2026-08-06", metrics=metrics)
        self.assertEqual(picked[0]["resonance_quality"], "stale")
        self.assertEqual(picked[0]["resonance_reason"], "date_mismatch")
        self.assertNotIn("resonance_stale:date_mismatch",
                         metrics.get("degradation_reasons", []))
        self.assertIn("resonance_stale:date_mismatch",
                      metrics["advisory_reasons"])

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

    def test_scan_reuses_context_and_only_analyzes_new_stock_codes(self):
        gather_calls = []
        phase2_codes = []

        def fake_gather(batch, top_n_per_sector, sector_context=None,
                        source_health=None, metrics=None):
            gather_calls.append((list(batch), sector_context))
            code = "600001" if batch[0] == "BK1" else "600002"
            candidates = [{"code": "600001", "sector_code": batch[0]}]
            if code != "600001":
                candidates.append({"code": code, "sector_code": batch[0]})
            return {"candidates": candidates}

        def fake_phase2(candidates, enable_wyckoff, as_of_date,
                        source_health=None, metrics=None):
            phase2_codes.append([item["code"] for item in candidates])
            return [{
                "code": item["code"], "sector_code": item["sector_code"],
                "composite_score": 80, "quality_adjusted_score": 80,
                "data_quality": {"eligible": True},
            } for item in candidates]

        context = {
            "BK1": {"name": "板块一", "sector_score": 80,
                    "sector_actionable": True},
            "BK2": {"name": "板块二", "sector_score": 70,
                    "sector_actionable": True},
        }
        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            result = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=5,
                sector_context=context)

        self.assertEqual(phase2_codes, [["600001"], ["600002"]])
        self.assertTrue(all(call[1] is context for call in gather_calls))
        self.assertEqual({item["code"] for item in result},
                         {"600001", "600002"})

    def test_source_health_scan_prefetches_membership_in_bounded_windows(self):
        gather_calls = []
        phase2_calls = []
        contexts = {
            f"BK{i:02d}": {
                "name": f"板块{i}", "ranking_position": i,
                "sector_actionable": True, "sector_score": 80,
            }
            for i in range(1, 8)
        }

        def fake_gather(batch, **_kwargs):
            gather_calls.append(tuple(batch))
            return {"candidates": [{
                "code": f"600{int(code[2:]):03d}", "sector_code": code,
            } for code in batch]}

        def fake_phase2(candidates, **_kwargs):
            phase2_calls.append([item["code"] for item in candidates])
            return [{
                **item, "composite_score": 80,
                "quality_adjusted_score": 80,
                "data_quality": {"eligible": True},
            } for item in candidates]

        metrics = {}
        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            result = dc.scan_sectors(
                list(contexts), min_candidates=99,
                sector_context=contexts, source_health=sc.RunSourceHealth(),
                metrics=metrics, initial_sector_window=3,
                sector_expansion_step=2, max_sector_expansion=5)

        self.assertEqual(gather_calls, [
            ("BK01", "BK02", "BK03"), ("BK04", "BK05"),
        ])
        self.assertEqual(len(phase2_calls), 2)
        self.assertEqual(metrics["sector_expanded_codes"], [
            "BK01", "BK02", "BK03", "BK04", "BK05",
        ])
        self.assertEqual(metrics["sector_expansion_limit"], 5)
        self.assertIn("sector_expansion_capped:5",
                      metrics["degradation_reasons"])
        self.assertEqual(metrics["sector_scan_coverage"], round(5 / len(contexts), 4))
        self.assertTrue(metrics["sector_expansion_truncated"])
        self.assertEqual(metrics["sector_expansion_total_count"], len(contexts))
        self.assertEqual(len(result), 5)

        full_metrics = {}
        with patch.object(dc, "gather_candidates", side_effect=fake_gather), \
             patch.object(dc, "run_phase2", side_effect=fake_phase2):
            full_result = dc.scan_sectors(
                list(contexts), min_candidates=99,
                sector_context=contexts, source_health=sc.RunSourceHealth(),
                metrics=full_metrics, initial_sector_window=3,
                sector_expansion_step=2, max_sector_expansion=len(contexts))
        self.assertEqual(full_metrics["sector_scan_coverage"], 1.0)
        self.assertFalse(full_metrics["sector_expansion_truncated"])
        self.assertEqual(len(full_result), len(contexts))

    def test_multi_batch_scan_fetches_ranking_snapshot_exactly_once(self):
        """One scan run owns one immutable full-market ranking snapshot."""
        from fetchers import sector_data

        rankings = {"sectors": [
            {"code": "BK1", "name": "板块一"},
            {"code": "BK2", "name": "板块二"},
        ]}

        def stocks(code, top_n=25):
            suffix = "1" if code == "BK1" else "2"
            return [{
                "code": f"60000{suffix}", "name": f"测试{suffix}",
                "market_cap": 1e10, "change_pct": 1.0,
                "amount": 1e8, "pe": 20,
            }]

        def score(candidates, **_kwargs):
            return [{
                **item, "composite_score": 80,
                "quality_adjusted_score": 80,
                "data_quality": {"eligible": True},
            } for item in candidates]

        with patch.object(sector_data, "get_sector_rankings",
                          return_value=rankings) as ranking_fetch, \
             patch.object(sector_data, "rank_hot_sectors",
                          return_value=rankings["sectors"]), \
             patch.object(sector_data, "get_sector_stocks",
                          side_effect=stocks), \
             patch.object(dc, "run_phase2", side_effect=score):
            dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=99)

        self.assertEqual(ranking_fetch.call_count, 1)

    def test_empty_ranking_snapshot_is_not_refetched_per_batch(self):
        """An unavailable shared snapshot remains explicit for the full scan."""
        from fetchers import sector_data

        def stocks(code, top_n=25):
            suffix = "1" if code == "BK1" else "2"
            return [{
                "code": f"60000{suffix}", "name": f"测试{suffix}",
                "market_cap": 1e10, "change_pct": 1.0,
                "amount": 1e8, "pe": 20,
            }]

        with patch.object(
                sector_data, "get_sector_rankings",
                return_value={
                    "meta": {"source": "error", "errors": ["dns"]},
                    "sectors": [],
                }) as ranking_fetch, \
             patch.object(sector_data, "rank_hot_sectors", return_value=[]), \
             patch.object(sector_data, "get_sector_stocks",
                          side_effect=stocks), \
             patch.object(dc, "run_phase2", return_value=[]):
            dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=99)

        self.assertEqual(ranking_fetch.call_count, 1)

    def test_cross_batch_duplicate_rebinds_to_later_stronger_sector(self):
        contexts = {
            "BK1": {
                "name": "弱板块", "sector_actionable": True,
                "sector_score": 60, "persistence_score": 55,
                "relative_strength": 0.5, "ranking_position": 1,
                "ranking_source": "realtime",
                "ranking_data_date": "2026-08-13",
                "ranking_quality": "good",
            },
            "BK2": {
                "name": "强板块", "sector_actionable": True,
                "sector_score": 90, "persistence_score": 80,
                "relative_strength": 1.2, "ranking_position": 2,
                "ranking_source": "realtime",
                "ranking_data_date": "2026-08-13",
                "ranking_quality": "good",
            },
        }

        def gather(batch, **_kwargs):
            code = batch[0]
            return {"candidates": [{
                "code": "600001", "ts_code": "600001.SH",
                "sector_code": code, "sector_name": contexts[code]["name"],
                "membership_source": "realtime",
                "membership_data_date": "2026-08-13",
                "membership_quality": "good",
            }]}

        def score(candidates, **_kwargs):
            return [{
                **item, "composite_score": 80,
                "raw_composite_score": 80,
                "quality_adjusted_score": 80,
                "base_data_quality": {
                    "eligible": True, "reasons": [],
                    "freshness_factor": 1.0,
                },
                "data_quality": {
                    "eligible": True, "reasons": [],
                    "freshness_factor": 1.0,
                },
            } for item in candidates]

        with patch.object(dc, "gather_candidates", side_effect=gather), \
             patch.object(dc, "run_phase2", side_effect=score):
            result = dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=99,
                sector_context=contexts)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sector_code"], "BK2")
        memberships = {m["code"]: m for m in result[0]["sector_memberships"]}
        self.assertEqual(set(memberships), {"BK1", "BK2"})
        self.assertEqual(memberships["BK2"]["ranking_position"], 2)
        self.assertEqual(result[0]["sector_score"], 90)
        self.assertEqual(result[0]["sector_relative_strength"], 1.2)

    def test_cross_batch_duplicate_fetches_each_stock_source_once(self):
        contexts = {
            "BK1": {"name": "板块一", "ranking_position": 1,
                    "sector_actionable": True, "sector_score": 70},
            "BK2": {"name": "板块二", "ranking_position": 2,
                    "sector_actionable": True, "sector_score": 90},
        }

        def gather(batch, **_kwargs):
            return {"candidates": [{
                "code": "600001", "ts_code": "600001.SH",
                "sector_code": batch[0], "sector_name": batch[0],
            }]}

        kline_calls = []
        capital_calls = []
        fundamental_calls = []

        def kline(ts_code, **_kwargs):
            kline_calls.append(ts_code)
            return {
                "meta": {"ts_code": ts_code},
                "data": [{"trade_date": f"202601{i:02d}", "close": 10,
                          "open": 10, "high": 10, "low": 10, "vol": 1,
                          "pre_close": 10} for i in range(1, 29)],
            }

        with patch.object(dc, "gather_candidates", side_effect=gather), \
             patch.object(sc, "_fetch_kline", side_effect=kline), \
             patch.object(sc, "_fetch_capital_flow",
                          side_effect=lambda ts, **_kwargs:
                          capital_calls.append(ts)), \
             patch.object(sc, "_fetch_fundamental",
                          side_effect=lambda ts, **_kwargs:
                          fundamental_calls.append(ts)):
            dc.scan_sectors(
                ["BK1", "BK2"], batch_size=1, min_candidates=99,
                sector_context=contexts)

        self.assertEqual(kline_calls, ["600001.SH"])
        self.assertLessEqual(len(capital_calls), 1)
        self.assertLessEqual(len(fundamental_calls), 1)

    def test_shuffled_sector_input_uses_same_ranked_early_stop_frontier(self):
        contexts = {
            f"BK{i}": {
                "name": f"板块{i}", "ranking_position": i,
                "sector_actionable": True, "sector_score": 100 - i,
            }
            for i in range(1, 5)
        }

        def execute(order):
            batches = []

            def gather(batch, **_kwargs):
                batches.append(tuple(batch))
                return {"candidates": [{
                    "code": f"6000{code[-1]}", "sector_code": code,
                } for code in batch]}

            def score(candidates, **_kwargs):
                return [{
                    **item, "composite_score": 80,
                    "quality_adjusted_score": 80,
                    "data_quality": {"eligible": True},
                } for item in candidates]

            with patch.object(dc, "gather_candidates", side_effect=gather), \
                 patch.object(dc, "run_phase2", side_effect=score):
                result = dc.scan_sectors(
                    order, batch_size=2, min_candidates=1,
                    sector_context=contexts)
            return batches, {item["code"] for item in result}

        ordered = execute(["BK1", "BK2", "BK3", "BK4"])
        shuffled = execute(["BK4", "BK2", "BK1", "BK3"])

        self.assertEqual(ordered, shuffled)
        self.assertEqual(ordered[0], [("BK1", "BK2")])

    def test_primary_sector_rebinding_reapplies_membership_to_base_quality(self):
        base_quality = {
            "eligible": True, "reasons": [], "freshness_factor": 1.0,
        }
        stale = {
            "code": "BK1", "name": "旧成分板块",
            "sector_actionable": False, "sector_score": 95,
            "ranking_position": 1, "membership_source": "cache",
            "membership_data_date": "2026-08-12",
            "membership_quality": "degraded",
        }
        fresh = {
            "code": "BK2", "name": "实时成分板块",
            "sector_actionable": True, "sector_score": 80,
            "ranking_position": 2, "membership_source": "realtime",
            "membership_data_date": "2026-08-13",
            "membership_quality": "good",
        }
        item = {
            "code": "600001", "sector_code": "BK1",
            "sector_memberships": [stale, fresh],
            "base_data_quality": base_quality,
            "data_quality": {
                "eligible": False,
                "reasons": ["sector_membership_stale"],
                "freshness_factor": 0.8,
            },
            "raw_composite_score": 80,
            "quality_adjusted_score": 64,
        }

        rebind = getattr(dc, "_rebind_primary_sector", None)
        self.assertIsNotNone(
            rebind, "primary-sector rebinding contract is not implemented")
        rebound = rebind(item, peer_cohorts={})

        self.assertEqual(rebound["sector_code"], "BK2")
        self.assertTrue(rebound["data_quality"]["eligible"])
        self.assertNotIn(
            "sector_membership_stale", rebound["data_quality"]["reasons"])
        self.assertEqual(rebound["data_quality"]["freshness_factor"], 1.0)
        self.assertEqual(rebound["quality_adjusted_score"], 80)

    def test_fresh_to_stale_rebinding_does_not_reuse_old_membership_quality(self):
        item = {
            "code": "600001", "sector_code": "BK1",
            "sector_memberships": [{
                "code": "BK2", "name": "过期板块",
                "sector_actionable": False, "sector_score": 90,
                "ranking_position": 1, "membership_source": "cache",
                "membership_data_date": "2026-08-12",
                "membership_quality": "degraded",
            }],
            "base_data_quality": {
                "eligible": True, "reasons": [],
                "freshness_factor": 1.0,
            },
            "data_quality": {
                "eligible": True, "reasons": [],
                "freshness_factor": 1.0,
            },
            "raw_composite_score": 80,
            "quality_adjusted_score": 80,
        }

        rebind = getattr(dc, "_rebind_primary_sector", None)
        self.assertIsNotNone(
            rebind, "primary-sector rebinding contract is not implemented")
        rebound = rebind(item, peer_cohorts={})

        self.assertEqual(rebound["sector_code"], "BK2")
        self.assertFalse(rebound["data_quality"]["eligible"])
        self.assertIn(
            "sector_membership_stale", rebound["data_quality"]["reasons"])
        self.assertLess(rebound["data_quality"]["freshness_factor"], 1.0)
        self.assertLess(
            rebound["quality_adjusted_score"],
            rebound["raw_composite_score"],
        )

    def test_expected_trading_date_uses_calendar_result_across_sessions(self):
        cases = [
            (datetime(2026, 8, 13, 10, 0), "2026-08-12", "2026-08-13"),
            (datetime(2026, 8, 13, 16, 0), "2026-08-12", "2026-08-13"),
            (datetime(2026, 8, 15, 10, 0), "2026-08-14", "2026-08-14"),
        ]
        for now, last_trading_date, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(
                    resolve_recommendation_date(
                        now=now, last_trading_date=last_trading_date),
                    expected,
                )

    def test_weekday_holiday_uses_injected_trading_calendar_result(self):
        self.assertEqual(
            resolve_recommendation_date(
                now=datetime(2026, 10, 1, 10, 0),
                last_trading_date="2026-09-30",
                is_trading_day=False,
            ),
            "2026-09-30",
        )

    def test_calendar_status_marks_weekday_holiday_closed_for_main_path(self):
        from fetchers import sector_data

        now = datetime(2026, 10, 1, 10, 0)
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"), \
             patch.object(
                 sector_data, "_load_authoritative_trading_dates",
                 return_value={"2026-09-30"}):
            sector_data.SNAPSHOT_FILE.write_text(json.dumps({
                "2026-09-30": [],
            }), encoding="utf-8")
            last_date, source = sector_data.get_last_trading_day(now=now)

        status = dc._is_current_trading_day(last_date, source, now=now)
        self.assertFalse(status)
        self.assertEqual(
            resolve_recommendation_date(
                now=now,
                last_trading_date=last_date,
                is_trading_day=status,
            ),
            "2026-09-30",
        )

    def test_stale_snapshot_does_not_rewind_main_path_expected_date(self):
        from fetchers import sector_data

        now = datetime(2026, 8, 13, 10, 0)
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(sector_data, "SNAPSHOT_FILE",
                          Path(tmpdir) / "snapshots.json"), \
             patch.object(sector_data, "CACHE_FILE",
                          Path(tmpdir) / "rankings.json"), \
             patch.object(
                 sector_data, "_load_authoritative_trading_dates",
                 return_value=set()):
            sector_data.SNAPSHOT_FILE.write_text(json.dumps({
                "2026-07-01": [],
            }), encoding="utf-8")
            last_date, source = sector_data.get_last_trading_day(now=now)

        status = dc._is_current_trading_day(last_date, source, now=now)
        self.assertIsNone(status)
        self.assertEqual(
            resolve_recommendation_date(
                now=now,
                last_trading_date=last_date,
                is_trading_day=status,
            ),
            "2026-08-13",
        )

    def test_malformed_same_day_calendar_cache_is_refetched_and_normalized(self):
        from fetchers import sector_data

        class FakeSeries:
            def tolist(self):
                return ["2026-09-30", "not-a-date", "2026-02-30"]

        fake_akshare = types.SimpleNamespace(
            tool_trade_date_hist_sina=lambda: {"trade_date": FakeSeries()})
        now = datetime(2026, 10, 1, 10, 0)

        for malformed_dates in ("2026-09-30", ["not-a-date"]):
            with self.subTest(malformed_dates=malformed_dates), \
                 tempfile.TemporaryDirectory() as tmpdir, \
                 patch.object(sector_data, "SNAPSHOT_FILE",
                              Path(tmpdir) / "snapshots.json"), \
                 patch.object(sector_data, "CACHE_FILE",
                              Path(tmpdir) / "rankings.json"), \
                 patch.object(sector_data, "TRADING_CALENDAR_FILE",
                              Path(tmpdir) / "calendar.json"), \
                 patch.dict(sys.modules, {"akshare": fake_akshare}):
                sector_data.TRADING_CALENDAR_FILE.write_text(json.dumps({
                    "checked_date": "2026-10-01",
                    "trading_dates": malformed_dates,
                }), encoding="utf-8")

                last_date, source = sector_data.get_last_trading_day(now=now)
                saved = json.loads(
                    sector_data.TRADING_CALENDAR_FILE.read_text(
                        encoding="utf-8"))

            self.assertEqual(last_date, "2026-09-30")
            self.assertEqual(source, "calendar_closed")
            self.assertEqual(saved["trading_dates"], ["2026-09-30"])

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

    def test_intraday_is_observation_only_and_provisional(self):
        regime = {"score": 90, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertEqual(policy["max_recommendations"], 0)
        self.assertEqual(policy["max_portfolio_pct"], 0)
        self.assertEqual(policy["provisional_target_mode"], "actionable")
        self.assertTrue(policy.get("provisional"))
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_intraday_neutral_tier_is_observation_only(self):
        regime = {"score": 70, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertEqual(policy["max_recommendations"], 0)
        self.assertEqual(policy["provisional_target_mode"], "waiting_trigger")
        self.assertTrue(policy.get("provisional"))
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_intraday_weak_tier_still_observation_with_both_reasons(self):
        regime = {"score": 50, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("regime_weak", policy["reasons"])
        self.assertIn("intraday_provisional", policy["reasons"])

    def test_intraday_stale_regime_still_observation(self):
        regime = {"score": 90, "data_date": "2026-08-05"}
        policy = build_recommendation_policy(
            regime, "2026-08-06", market_open=True)
        self.assertEqual(policy["mode"], "observation")
        self.assertIn("regime_stale", policy["reasons"])
        self.assertFalse(policy.get("provisional"))

    def test_intraday_observation_pool_marked_provisional(self):
        policy = build_recommendation_policy(
            {"score": 50, "data_date": "2026-08-06"},
            "2026-08-06", market_open=True)
        buckets = classify_candidates([candidate("1")], policy)
        self.assertIn(
            "intraday_provisional",
            buckets["observation"][0]["observation_reasons"])

    def test_neutral_regime_limits_waiting_list_to_two(self):
        regime = {"score": 70, "data_date": "2026-08-06"}
        policy = build_recommendation_policy(regime, "2026-08-06")
        buckets = classify_candidates(
            [candidate("1"), candidate("2"), candidate("3")], policy)
        self.assertEqual(policy["mode"], "waiting_trigger")
        self.assertEqual(len(buckets["waiting_trigger"]), 2)
        self.assertEqual(
            [row["code"] for row in buckets["next_day_confirmation"]],
            ["3"],
        )
        self.assertEqual(buckets["observation"], [])

    def test_waiting_trigger_excludes_non_structural_target_sources(self):
        policy = build_recommendation_policy(
            {"score": 70, "data_date": "2026-08-06"}, "2026-08-06")
        resistance = candidate("resistance")
        atr = candidate("atr")
        atr["trade_plan"]["target_source"] = "atr_projection"
        unavailable = candidate("unavailable")
        unavailable["trade_plan"]["target_source"] = "unavailable"
        buckets = classify_candidates(
            [resistance, atr, unavailable], policy)
        self.assertEqual(
            [row["code"] for row in buckets["waiting_trigger"]],
            ["resistance"],
        )
        self.assertEqual(
            {row["code"] for row in buckets["observation"]},
            {"atr", "unavailable"},
        )

    def test_divergence_requires_verified_sector_capital(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 20},
            "2026-08-06")
        historical = candidate("historical")
        historical["sector_capital_evidence"] = "verified"
        positive = candidate("positive")
        positive["sector_capital_evidence"] = "positive_verified"
        buckets = classify_candidates([historical, positive], policy)
        self.assertEqual([item["code"] for item in buckets["waiting_trigger"]],
                         ["positive"])
        self.assertEqual(
            [item["code"] for item in buckets["next_day_confirmation"]],
            ["historical"],
        )

    def test_positive_capital_proof_is_not_marked_as_divergence_in_observation(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 20},
            "2026-08-06")
        item = candidate("positive", eligible=False)
        item["sector_capital_evidence"] = "positive_verified"
        buckets = classify_candidates([item], policy)
        self.assertEqual([row["code"] for row in buckets["data_rejected"]],
                         ["positive"])
        self.assertNotIn(
            "breadth_capital_divergence",
            buckets["data_rejected"][0]["observation_reasons"],
        )

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
        self.assertEqual(buckets["observation"], [])

    def test_confirmation_candidate_is_not_duplicated_in_observation(self):
        policy = build_recommendation_policy(
            {"score": 65, "data_date": "2026-08-06", "capital_score": 50},
            "2026-08-06")
        watch = candidate("watch", sector_actionable=False)
        buckets = classify_candidates([watch], policy)
        confirmation_codes = {
            row["code"] for row in buckets["next_day_confirmation"]
        }
        observation_codes = {
            row["code"] for row in buckets["observation"]
        }
        self.assertTrue(confirmation_codes)
        self.assertFalse(confirmation_codes & observation_codes)

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
        self.assertEqual([item["code"] for item in buckets["data_rejected"]], ["2"])
        self.assertEqual(buckets["observation"], [])

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
        self.assertIn("小级别维科夫阶段", report)
        self.assertIn("阶段D：需求确认", report)
        self.assertIn("需求占优，回踩缩量后等待向上确认", report)
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
        self.assertIn("小级别维科夫阶段", html)
        self.assertIn("阶段D：需求确认", html)
        self.assertIn("需求占优，回踩缩量后等待向上确认", html)
        self.assertIn("股市有风险，投资需谨慎", html)

    def test_wyckoff_buy_level_uses_canonical_short_term_fields(self):
        cases = (
            ("spring", False, 1, "试错仓"),
            ("lps", False, 2, "核心仓"),
            ("jac", True, 3, "趋势仓"),
        )
        for (sub_phase, post_lps_reconfirmation,
             expected_number, expected_role) in cases:
            with self.subTest(sub_phase=sub_phase):
                level = dc._wyckoff_buy_level({
                    "sub_phase": "中文展示字段不应覆盖规范字段",
                    "short_term": {
                        "sub_phase": sub_phase,
                        "signal_status": "confirmed",
                        "post_lps_reconfirmation": post_lps_reconfirmation,
                    },
                })
                self.assertEqual(level["number"], expected_number)
                self.assertEqual(level["role"], expected_role)

    def test_wyckoff_buy_level_rejects_unconfirmed_or_unknown_setup(self):
        self.assertIsNone(dc._wyckoff_buy_level({
            "short_term": {
                "sub_phase": "backup",
                "signal_status": "candidate",
            },
        }))
        self.assertIsNone(dc._wyckoff_buy_level({
            "short_term": {
                "sub_phase": "continuation",
                "signal_status": "confirmed",
            },
        }))
        for sub_phase in ("secondary_test", "st", "backup"):
            with self.subTest(sub_phase=sub_phase):
                self.assertIsNone(dc._wyckoff_buy_level({
                    "short_term": {
                        "sub_phase": sub_phase,
                        "signal_status": "confirmed",
                    },
                }))
        self.assertIsNone(dc._wyckoff_buy_level({
            "short_term": {
                "sub_phase": "jac",
                "signal_status": "confirmed",
                "post_lps_reconfirmation": False,
            },
        }))
        self.assertIsNone(dc._wyckoff_buy_level({"sub_phase": "LPS"}))

    def test_html_highlights_confirmed_lps_as_level_two_row(self):
        item = candidate("lps")
        item["wyckoff"]["minor_phase"] = {
            "code": "D", "name": "阶段D：LPS已确认",
            "description": "回踩后已重新转强",
        }
        item["wyckoff"]["sub_phase"] = "lps"
        item["wyckoff"]["short_term"] = {
            "sub_phase": "lps", "signal_status": "confirmed",
        }
        html = dc._html_candidate_rows(
            [item], buy_level_display="actionable")
        self.assertIn("<tr class='wyckoff-buy-level-2'>", html)
        self.assertIn("二级 · SOS 后 LPS · 核心仓", html)
        self.assertIn("阶段D：LPS已确认", html)

        plain_html = dc._html_candidate_rows([item])
        self.assertNotIn("wyckoff-buy-level-2", plain_html)

    def test_html_marks_confirmed_lps_as_non_executable_observation_level(self):
        item = candidate("observation-lps")
        item["wyckoff"]["short_term"] = {
            "sub_phase": "lps",
            "signal_status": "confirmed",
        }

        html = dc._html_candidate_rows(
            [item], buy_level_display="observation")

        self.assertIn(
            "<tr class='wyckoff-observation-buy-level-2'>", html)
        self.assertIn("潜在二级 · SOS 后 LPS · 观察｜不可执行", html)
        self.assertNotIn("核心仓", html)
        self.assertNotIn("<tr class='wyckoff-buy-level-2'>", html)

    def test_observation_level_three_requires_post_lps_reconfirmation(self):
        first_jac = candidate("first-jac")
        first_jac["wyckoff"]["short_term"] = {
            "sub_phase": "jac",
            "signal_status": "confirmed",
            "post_lps_reconfirmation": False,
        }
        reconfirmed_jac = copy.deepcopy(first_jac)
        reconfirmed_jac["code"] = "reconfirmed-jac"
        reconfirmed_jac["wyckoff"]["short_term"][
            "post_lps_reconfirmation"] = True

        html = dc._html_candidate_rows(
            [first_jac, reconfirmed_jac],
            buy_level_display="observation",
        )

        self.assertEqual(
            html.count("wyckoff-observation-buy-level-3"), 1)
        self.assertIn(
            "潜在三级 · JAC/BU 后再确认 · 观察｜不可执行", html)

    def test_html_does_not_highlight_bu_candidate_as_lps(self):
        item = candidate("bu")
        item["wyckoff"]["minor_phase"] = {
            "code": "D", "name": "阶段D：BU回踩待确认",
            "description": "缩量守位，等待再次转强",
        }
        item["wyckoff"]["sub_phase"] = "backup"
        item["wyckoff"]["short_term"] = {
            "sub_phase": "backup", "signal_status": "candidate",
        }
        html = dc._html_candidate_rows(
            [item], buy_level_display="actionable")
        self.assertNotIn("wyckoff-buy-level-", html)
        self.assertIn("阶段D：BU回踩待确认", html)

    def test_generated_html_highlights_buy_levels_only_in_actionable_table(self):
        actionable = candidate("actionable")
        actionable["wyckoff"]["short_term"] = {
            "sub_phase": "lps", "signal_status": "confirmed",
        }
        observation = copy.deepcopy(actionable)
        observation["code"] = "observation"
        observation["name"] = "测试observation"
        buckets = {
            "actionable": [actionable],
            "waiting_trigger": [],
            "next_day_confirmation": [],
            "observation": [observation],
            "data_rejected": [],
        }
        html = _generate_html(
            [actionable, observation],
            [("BK1", "测试板块", 80)],
            1.0,
            "20260902-160000",
            {
                "mode": "actionable", "max_recommendations": 5,
                "max_portfolio_pct": 60, "reasons": [],
            },
            buckets,
        )

        self.assertEqual(
            html.count("<tr class='wyckoff-buy-level-2'>"), 1)
        self.assertIn("一级 · Spring/Test · 试错仓", html)
        self.assertIn("二级 · SOS 后 LPS · 核心仓", html)
        self.assertIn("三级 · JAC/BU 后再确认 · 趋势仓", html)

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

    def test_diagnostic_shows_provider_reason_for_fundamental_error(self):
        item = candidate("1", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["fundamental_error"],
            "dimensions": {
                "fundamental": {
                    "source": "error",
                    "stale_reason": "fundamental_error",
                },
            },
        }
        item["source_evidence"] = {
            "fundamental": {
                "reason": "timeout",
                "provider_attempts": 2,
                "cache_used": True,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("抓取原因码timeout", detail)
        self.assertIn("已回退缓存", detail)

    def test_missing_capital_cache_is_not_rendered_as_cache_fallback(self):
        item = candidate("cache-miss", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["cache_miss"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "cache_miss",
                    "stale_reason": "cache_miss",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "status": "cache_miss", "cache_used": False,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("未命中有效缓存", detail)
        self.assertNotIn("已回退缓存", detail)

    def test_deadline_skipped_capital_request_is_not_provider_error(self):
        item = candidate("deadline", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["not_started_deadline"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "not_started_deadline",
                    "stale_reason": "not_started_deadline",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "status": "not_started_deadline", "cache_used": False,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("达到截止时间", detail)
        self.assertNotIn("资金面数据返回错误", detail)

    def test_genuine_capital_fetch_failure_stays_provider_error(self):
        item = candidate("capital-error", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["capital_error"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "timeout",
                    "stale_reason": "capital_error",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "status": "timeout", "reason": "timeout",
                "cache_used": False,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("资金面数据返回错误", detail)
        self.assertIn("抓取原因码timeout", detail)

    def test_called_capital_failure_is_distinct_from_scheduler_omission(self):
        item = candidate("capital-empty", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["capital_error"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "eastmoney_empty",
                    "stale_reason": "capital_error",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "attempted": True,
                "status": "eastmoney_empty",
                "reason": "eastmoney_empty",
                "cache_used": False,
                "failure_chain": [
                    {"source": "eastmoney", "reason": "empty"},
                ],
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("资金面数据返回错误", detail)
        self.assertIn("接口已调用", detail)
        self.assertIn("抓取原因码eastmoney_empty", detail)
        self.assertIn("失败链路eastmoney:empty", detail)
        self.assertNotIn("调度原因码", detail)

    def test_not_selected_capital_is_rendered_as_unrequested_scheduler_state(self):
        item = candidate("not-selected", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["not_selected_for_enrichment"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "not_selected_for_enrichment",
                    "stale_reason": "not_selected_for_enrichment",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "attempted": False,
                "status": "not_selected_for_enrichment",
                "reason": "not_selected_for_enrichment",
                "cache_used": False,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("未进入资金增强优先队列（预算内未选中）", detail)
        self.assertIn("未调用", detail)
        self.assertIn("调度原因码not_selected_for_enrichment", detail)
        self.assertNotIn("抓取原因码not_selected_for_enrichment", detail)

    def test_source_unavailable_capital_is_rendered_as_unrequested_scheduler_state(self):
        item = candidate("source-unavailable", eligible=False)
        item["data_quality"] = {
            "eligible": False,
            "coverage": 0.55,
            "reasons": ["source_unavailable"],
            "dimensions": {
                "capital": {
                    "available": False,
                    "source_status": "source_unavailable",
                    "stale_reason": "source_unavailable",
                },
            },
        }
        item["source_evidence"] = {
            "capital": {
                "attempted": False,
                "status": "source_unavailable",
                "reason": "source_unavailable",
                "cache_used": False,
            },
        }

        detail = _candidate_diagnostic_text(item)

        self.assertIn("资金增强源不可用，本轮未调用", detail)
        self.assertIn("未调用", detail)
        self.assertIn("调度原因码source_unavailable", detail)
        self.assertNotIn("抓取原因码source_unavailable", detail)

    def test_capital_failure_audit_excludes_unrequested_scheduler_states(self):
        provider_failure = candidate("provider-failure", eligible=False)
        provider_failure["source_evidence"] = {
            "capital": {
                "attempted": True,
                "status": "eastmoney_empty",
                "reason": "eastmoney_empty",
            },
        }
        scheduler_omission = candidate("scheduler-omission", eligible=False)
        scheduler_omission["source_evidence"] = {
            "capital": {
                "attempted": False,
                "status": "not_selected_for_enrichment",
                "reason": "not_selected_for_enrichment",
            },
        }
        buckets = {
            "actionable": [], "waiting_trigger": [],
            "observation": [provider_failure, scheduler_omission],
        }

        performance = _complete_performance(
            {}, None, [provider_failure, scheduler_omission], buckets,
            min_score=50, total_seconds=1.0)

        self.assertEqual(
            performance["capital_failure_reasons"],
            {"eastmoney_empty": 1},
        )

    def test_capital_failure_audit_prefers_full_source_health_counts(self):
        health = dc.RunSourceHealth()
        token = health.try_acquire_live_permit("capital")
        health.mark_started(token)
        health.complete_failure(token, dc.live_attempt(
            attempted=True, provider_attempts=1,
            reason="eastmoney_empty", status="eastmoney_empty"))

        performance = _complete_performance(
            {}, health, [], {"actionable": [], "waiting_trigger": [],
                             "observation": []},
            min_score=50, total_seconds=1.0)

        self.assertEqual(
            performance["capital_failure_reasons"],
            {"eastmoney_empty": 1},
        )

    def test_capital_audit_fields_reconcile_cache_live_and_skipped(self):
        cache_hit = candidate("cache")
        cache_hit["source_evidence"] = {
            "capital": {"status": "cache_valid", "attempted": False},
        }
        live_success = candidate("live")
        live_success["source_evidence"] = {
            "capital": {"status": "live_success", "attempted": True},
        }
        skipped = candidate("skipped", eligible=False)
        skipped["source_evidence"] = {
            "capital": {
                "status": "not_selected_for_enrichment", "attempted": False,
            },
        }
        items = [cache_hit, live_success, skipped]
        buckets = {
            "actionable": [cache_hit, live_success],
            "waiting_trigger": [], "observation": [skipped],
        }

        performance = _complete_performance(
            {}, None, items, buckets, min_score=50, total_seconds=1.0)

        self.assertEqual(performance["capital_cache_valid_count"], 1)
        self.assertEqual(performance["capital_valid_count"], 2)
        self.assertEqual(performance["capital_skipped_by_budget"], 1)
        self.assertEqual(performance["capital_enrichment_population"], 3)
        self.assertEqual(
            performance["capital_cache_valid_count"]
            + performance["capital_valid_count"]
            - performance["capital_cache_valid_count"]
            + performance["capital_skipped_by_budget"],
            performance["capital_enrichment_population"],
        )

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
        self.assertEqual(output["candidates"][0]["wyckoff"]["minor_phase"]["code"], "D")

    def test_main_json_exposes_all_failed_scan_batches(self):
        def fake_scan(*_args, metrics=None, **_kwargs):
            metrics.update({
                "batch_count": 1,
                "failed_batches": [{
                    "sectors": ["BK1"], "reason": "OSError",
                }],
                "degradation_reasons": ["batch_error:OSError"],
            })
            return []

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(dc, "load_regime_context", return_value={}), \
             patch("fetchers.sector_data.get_last_trading_day",
                   return_value=("2026-08-06", "snapshot")), \
             patch.object(dc, "resolve_recommendation_date",
                          return_value="2026-08-06"), \
             patch.object(dc, "pick_hot_sectors", return_value=[{
                 "code": "BK1", "name": "测试板块", "sector_score": 60,
             }]), \
             patch.object(dc, "scan_sectors", side_effect=fake_scan), \
             patch.object(dc, "REPORTS_DIR", Path(temp_dir)), \
             patch.object(sys, "argv", ["daily_candidates.py", "--json",
                                          "--no-html"]):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                dc.main()

        output = json.loads(stdout.getvalue())
        performance = output["meta"]["performance"]
        self.assertEqual(performance["scan_status"], "error")
        self.assertEqual(performance["failed_batches"][0]["sectors"], ["BK1"])
        self.assertIn("batch_error:OSError",
                      performance["degradation_reasons"])

    def test_main_persists_candidate_snapshot_with_compact_trigger_date(self):
        item = candidate("000001")
        item["wyckoff"]["trigger_date"] = "20260806"
        item["signals"]["provider_flag"] = np.bool_(True)

        def fake_scan(*_args, metrics=None, **_kwargs):
            metrics.update({"batch_count": 1})
            return [item]

        with tempfile.TemporaryDirectory() as history_dir, \
             tempfile.TemporaryDirectory() as report_dir, \
             patch.object(dc, "load_regime_context", return_value={
                 "score": 80, "data_date": "2026-08-06",
             }), \
             patch("fetchers.sector_data.get_last_trading_day",
                   return_value=("2026-08-06", "snapshot")), \
             patch.object(dc, "resolve_recommendation_date",
                          return_value="2026-08-06"), \
             patch.object(dc, "is_recommendation_session", return_value=False), \
             patch.object(dc, "pick_hot_sectors", return_value=[{
                 "code": "BK1", "name": "测试板块", "sector_score": 60,
             }]), \
             patch.object(dc, "scan_sectors", side_effect=fake_scan), \
             patch.object(
                 dc, "save_snapshot_if_official",
                 side_effect=lambda source: (
                     __import__("core.recommendation_snapshot",
                                fromlist=["save_snapshot_if_official"])
                     .save_snapshot_if_official(source, Path(history_dir))
                 ),
             ), \
             patch.object(dc, "REPORTS_DIR", Path(report_dir)), \
             patch.object(sys, "argv", ["daily_candidates.py", "--json",
                                          "--no-html"]):
            first_stdout = io.StringIO()
            with redirect_stdout(first_stdout):
                dc.main()
            second_stdout = io.StringIO()
            with redirect_stdout(second_stdout):
                dc.main()

        first = json.loads(first_stdout.getvalue())
        second = json.loads(second_stdout.getvalue())
        self.assertEqual(first["meta"]["tracking"]["status"], "created")
        self.assertEqual(second["meta"]["tracking"]["status"], "unchanged")
        self.assertEqual(
            first["meta"]["tracking"]["path"],
            second["meta"]["tracking"]["path"],
        )
        self.assertTrue(first["meta"]["tracking"]["path"].endswith(
            "2026-08-06.json"))


def run_daily_candidates_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRecommendationPolicy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_daily_candidates_tests()
    raise SystemExit(1 if failed else 0)
