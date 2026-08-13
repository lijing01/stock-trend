#!/usr/bin/env python3
"""Deterministic contracts for the daily recommendation performance repair."""

import importlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


SOURCES = (
    "sector_ranking", "sector_membership", "kline", "capital",
    "fundamental",
)
PERFORMANCE_PHASE_FIELDS = (
    "sector_ranking_seconds", "sector_membership_seconds", "kline_seconds",
    "wyckoff_seconds", "capital_seconds", "fundamental_seconds",
    "report_seconds", "total_seconds",
)
PERFORMANCE_FUNNEL_FIELDS = (
    "batch_count", "raw_candidate_count", "unique_candidate_count",
    "wyckoff_pass_count", "final_candidate_count", "final_valid_count",
    "actionable_count",
)
SOURCE_FIELDS = (
    "logical_live_requests", "provider_attempts", "cache_hits", "failures",
    "circuit_breaks", "failure_reasons", "state",
)


def _source_health_contract(testcase):
    try:
        return importlib.import_module("core.source_health")
    except ModuleNotFoundError as exc:
        testcase.fail(
            "missing production core.source_health contract: " + str(exc))


def _attempt(provider_attempts=1, reason=""):
    return {
        "attempted": True,
        "reason": reason,
        "cache_used": False,
        "stale": False,
        "subprocess_started": False,
        "provider_attempts": provider_attempts,
    }


def _performance_fixture():
    """Twenty sectors, 250 raw rows, exactly 30% duplicate rows."""
    unique = [f"{600000 + i:06d}" for i in range(175)]
    rows = []
    for index in range(250):
        code = unique[index] if index < 175 else unique[index - 175]
        rows.append({
            "sector_code": f"BK{index % 20:04d}",
            "code": code,
            "profile": ("normal" if index % 3 == 0 else
                        "slow" if index % 3 == 1 else "unavailable"),
        })
    return rows


class TestRunSourceHealthContract(unittest.TestCase):
    def test_permit_lifecycle_counts_only_started_logical_requests(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        token = health.try_acquire_live_permit("kline")

        before = health.snapshot()["kline"]
        health.mark_started(token)
        started = health.snapshot()["kline"]
        health.complete_success(token, _attempt(provider_attempts=2))
        completed = health.snapshot()["kline"]

        self.assertEqual(before["logical_live_requests"], 0)
        self.assertEqual(started["logical_live_requests"], 1)
        self.assertEqual(started["in_flight"], 1)
        self.assertEqual(completed["provider_attempts"], 2)
        self.assertEqual(completed["in_flight"], 0)

    def test_unstarted_release_does_not_change_request_or_failure_counts(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        token = health.try_acquire_live_permit("capital")
        health.release_unstarted(token, "submit_failed")
        state = health.snapshot()["capital"]

        self.assertEqual(state["logical_live_requests"], 0)
        self.assertEqual(state["provider_attempts"], 0)
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["in_flight"], 0)

    def test_failure_threshold_and_in_flight_window_bound_requests_to_three(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        started = []
        while True:
            token = health.try_acquire_live_permit("fundamental")
            if token is None:
                break
            health.mark_started(token)
            started.append(token)
            if len(started) > 1:
                health.complete_failure(
                    started[-2], _attempt(reason="timeout"))
            if len(started) >= 3:
                health.complete_failure(
                    started[-1], _attempt(reason="timeout"))
                break

        state = health.snapshot()["fundamental"]
        self.assertEqual(state["logical_live_requests"], 3)
        self.assertEqual(state["state"], "unavailable")
        self.assertIsNone(health.try_acquire_live_permit("fundamental"))

    def test_failure_classifier_distinguishes_required_reason_codes(self):
        contract = _source_health_contract(self)
        classify = contract.classify_failure
        examples = {
            "dns": OSError("Name or service not known"),
            "timeout": TimeoutError("timed out"),
            "http": RuntimeError("HTTP 503"),
            "empty": ValueError("empty response"),
            "parse": ValueError("JSON decode error"),
            "subprocess": RuntimeError("subprocess exited 1"),
            "unknown": RuntimeError("unexpected provider failure"),
        }

        self.assertEqual(
            {name: classify(error) for name, error in examples.items()},
            {name: name for name in examples},
        )

    def test_sources_have_independent_circuits_and_cache_never_heals(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        for _ in range(2):
            token = health.try_acquire_live_permit("kline")
            health.mark_started(token)
            health.complete_failure(token, _attempt(reason="timeout"))

        capital = health.try_acquire_live_permit("capital")
        health.mark_started(capital)
        health.complete_success(capital, _attempt())
        health.record_cache_hit("kline", stale=True)
        snapshot = health.snapshot()

        self.assertEqual(snapshot["kline"]["state"], "unavailable")
        self.assertEqual(snapshot["kline"]["cache_hits"], 1)
        self.assertEqual(snapshot["capital"]["state"], "healthy")

    def test_release_and_completion_are_idempotent_and_record_reason(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        unstarted = health.try_acquire_live_permit("sector_membership")
        health.release_unstarted(unstarted, "cancelled")
        health.release_unstarted(unstarted, "cancelled")
        started = health.try_acquire_live_permit("sector_membership")
        health.mark_started(started)
        health.complete_failure(started, _attempt(reason="dns"))
        health.complete_failure(started, _attempt(reason="dns"))
        state = health.snapshot()["sector_membership"]

        self.assertEqual(state["in_flight"], 0)
        self.assertEqual(state["logical_live_requests"], 1)
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["failure_reasons"], {"dns": 1})

    def test_queued_deadline_cancellation_releases_unstarted_permit(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=2, max_in_flight=2)
        release = threading.Event()

        def slow_live(item):
            release.wait(0.2)
            return contract.source_result(item, _attempt())

        timer = threading.Timer(0.12, release.set)
        timer.start()
        started = time.monotonic()
        results = contract.bounded_source_map(
            "kline", [1, 2], health, slow_live, lambda item: -item,
            time.monotonic() + 0.03, max_workers=1)
        elapsed = time.monotonic() - started
        timer.cancel()
        release.set()
        state = health.snapshot()["kline"]

        self.assertLess(elapsed, 0.1)
        self.assertEqual(state["logical_live_requests"], 1)
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["in_flight"], 0)
        self.assertEqual(dict(results), {1: -1, 2: -2})

    def test_events_explain_unavailable_cache_only_and_stale_data(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=1, max_in_flight=2)
        token = health.try_acquire_live_permit("sector_ranking")
        health.mark_started(token)
        health.complete_failure(token, _attempt(reason="dns"))
        self.assertIsNone(
            health.try_acquire_live_permit("sector_ranking"))
        contract.bounded_source_map(
            "sector_ranking", ["ranking"], health,
            lambda item: contract.source_result(item, _attempt()),
            lambda item: {"cached": item}, time.monotonic() - 1,
            max_workers=1)

        events = health.events()
        reasons = [event.get("reason") for event in events]
        self.assertIn("source_unavailable", reasons)
        self.assertIn("cache_only", reasons)
        self.assertIn("data_stale", reasons)

    def test_cache_miss_is_not_counted_and_circuit_opens_once(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth(
            failure_threshold=1, max_in_flight=2)
        token = health.try_acquire_live_permit("capital")
        health.mark_started(token)
        health.complete_failure(token, _attempt(reason="timeout"))
        health.record_cache_result("capital", None)
        health.record_cache_result("capital", None)
        state = health.snapshot()["capital"]
        events = health.events()

        self.assertEqual(state["cache_hits"], 0)
        self.assertEqual(
            sum(event["event"] == "cache_miss" for event in events), 2)
        self.assertEqual(
            sum(event["event"] == "circuit_opened" for event in events), 1)

    def test_membership_empty_cache_wrapper_is_a_cache_miss(self):
        contract = _source_health_contract(self)
        health = contract.RunSourceHealth()
        payload = {"code": "BK1", "stocks": []}
        results = contract.bounded_source_map(
            "sector_membership", ["BK1"], health,
            lambda item: self.fail("deadline must force cache-only"),
            lambda item: payload, time.monotonic() - 1, max_workers=1,
            cache_usable=lambda result: bool(result.get("stocks")))

        self.assertEqual(results, [("BK1", payload)])
        self.assertEqual(
            health.snapshot()["sector_membership"]["cache_hits"], 0)
        self.assertIn("cache_miss", [
            event["event"] for event in health.events()])


class TestProductionPerformanceContract(unittest.TestCase):
    def test_production_deadline_and_budget_constants_bound_critical_path(self):
        contract = _source_health_contract(self)
        self.assertEqual(contract.SCAN_DEADLINE_SECONDS, 45)
        self.assertGreater(contract.FINALIZATION_RESERVE_SECONDS, 0)
        self.assertEqual(set(contract.LIVE_ATTEMPT_TIMEOUT_SECONDS), set(SOURCES))
        self.assertEqual(set(contract.MAX_PROVIDER_ATTEMPTS), set(SOURCES))

        ranking_path = (
            contract.LIVE_ATTEMPT_TIMEOUT_SECONDS["sector_ranking"]
            * contract.MAX_PROVIDER_ATTEMPTS["sector_ranking"])
        membership_path = (
            contract.LIVE_ATTEMPT_TIMEOUT_SECONDS["sector_membership"]
            * contract.MAX_PROVIDER_ATTEMPTS["sector_membership"])
        stock_path = sum(max(
            contract.LIVE_ATTEMPT_TIMEOUT_SECONDS[source]
            * contract.MAX_PROVIDER_ATTEMPTS[source]
            for source in parallel_sources
        ) for parallel_sources in (("kline",), ("capital", "fundamental")))
        live_critical_path = ranking_path + membership_path + stock_path
        self.assertLessEqual(
            live_critical_path + contract.FINALIZATION_RESERVE_SECONDS,
            contract.SCAN_DEADLINE_SECONDS,
        )

    def test_large_fixture_has_required_scale_and_duplicate_ratio(self):
        rows = _performance_fixture()
        unique_codes = {row["code"] for row in rows}

        self.assertEqual(len({row["sector_code"] for row in rows}), 20)
        self.assertEqual(len(rows), 250)
        self.assertEqual(len(unique_codes), 175)
        self.assertEqual((len(rows) - len(unique_codes)) / len(rows), 0.30)
        self.assertEqual(
            {row["profile"] for row in rows},
            {"normal", "slow", "unavailable"},
        )

    def test_json_performance_contract_is_additive_and_complete(self):
        from scans.daily_candidates import build_json_output

        legacy_candidate = {
            "code": "600001", "quality_adjusted_score": 80,
        }
        policy = {"mode": "actionable", "reasons": []}
        buckets = {
            "actionable": [legacy_candidate], "waiting_trigger": [],
            "observation": [],
        }
        source_metrics = {
            source: {
                "logical_live_requests": 1, "provider_attempts": 1,
                "cache_hits": 0, "failures": 0, "circuit_breaks": 0,
                "failure_reasons": {}, "state": "healthy",
            }
            for source in SOURCES
        }
        performance = {
            **{field: 0.0 for field in PERFORMANCE_PHASE_FIELDS},
            **{field: 0 for field in PERFORMANCE_FUNNEL_FIELDS},
            "sources": source_metrics,
        }
        output = build_json_output(
            [legacy_candidate], [{"code": "BK1"}], 1.0, policy, buckets,
            performance=performance)

        self.assertEqual(output["candidates"], [legacy_candidate])
        self.assertEqual(output["recommendations"], [legacy_candidate])
        self.assertEqual(output["policy"], policy)
        actual = output["meta"]["performance"]
        self.assertTrue(set(PERFORMANCE_PHASE_FIELDS).issubset(actual))
        self.assertTrue(set(PERFORMANCE_FUNNEL_FIELDS).issubset(actual))
        self.assertEqual(set(actual["sources"]), set(SOURCES))
        for source in SOURCES:
            self.assertTrue(
                set(SOURCE_FIELDS).issubset(actual["sources"][source]))

    def test_completed_metrics_are_typed_and_nonnegative(self):
        from core.source_health import RunSourceHealth
        from scans.daily_candidates import _complete_performance

        completed = _complete_performance(
            {}, RunSourceHealth(), [], {
                "actionable": [], "waiting_trigger": [], "observation": [],
            }, min_score=50, total_seconds=0.25)

        for field in PERFORMANCE_PHASE_FIELDS:
            self.assertIsInstance(completed[field], float)
            self.assertGreaterEqual(completed[field], 0.0)
        for field in PERFORMANCE_FUNNEL_FIELDS:
            self.assertIsInstance(completed[field], int)
            self.assertGreaterEqual(completed[field], 0)
        for source in SOURCES:
            self.assertEqual(
                set(completed["sources"][source]),
                set(SOURCE_FIELDS) | {"requests"})
            self.assertEqual(
                completed["sources"][source]["requests"],
                completed["sources"][source]["logical_live_requests"],
            )

    def test_json_mode_emits_performance_without_writing_reports(self):
        from scans import daily_candidates as dc

        args = Namespace(
            top=30, min_candidates=20, min_score=50,
            sectors="BK1", json=True, html=True)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch("argparse.ArgumentParser.parse_args", return_value=args), \
             patch.object(dc, "REPORTS_DIR", Path(tmpdir)), \
             patch.object(dc, "load_regime_context", return_value=None), \
             patch("fetchers.sector_data.get_last_trading_day",
                   return_value=("2026-08-12", {})), \
             patch.object(dc, "scan_sectors", return_value=[]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            dc.main()
            report_files = list(Path(tmpdir).iterdir())

        payload = json.loads(stdout.getvalue())
        self.assertIn("performance", payload["meta"])
        self.assertEqual(report_files, [])
        self.assertIn("[performance]", stderr.getvalue())


def run_performance_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    return result.testsRun - failed, failed


if __name__ == "__main__":
    _, failed = run_performance_tests()
    raise SystemExit(1 if failed else 0)
