"""Run-scoped source health, evidence, deadlines, and bounded scheduling."""

from __future__ import annotations

import copy
import socket
import threading
import time
import math
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable


SOURCES = (
    "sector_ranking", "sector_membership", "kline", "capital",
    "fundamental",
)
# The scan has a hard end-to-end cap.  Live provider work stops early enough
# to retain the final report reserve.
SCAN_DEADLINE_SECONDS = 330
FINALIZATION_RESERVE_SECONDS = 10
KLINE_PHASE_SECONDS = 110
CAPITAL_PREFETCH_LIMIT = 36
CAPITAL_PREFETCH_BATCH_SIZE = 12
CAPITAL_TOPUP_LIMIT = 12
LIVE_ATTEMPT_TIMEOUT_SECONDS = {
    "sector_ranking": 3,
    "sector_membership": 3,
    # kline fetchers walk EM host rotation → Tencent → BaoStock; a slow EM
    # host can take several seconds before the fallback completes. 25s lets
    # the fallback chain finish without abandoning the slot to a stale cache.
    "kline": 25,
    # capital/fundamental fetchers each walk several AKShare/Tushare
    # endpoints (~8-10s standalone); under 4-worker concurrency the old 15s
    # subprocess timeout tripped and orphaned those dimensions to stale
    # cache. 25s covers the full fallback chain.
    "capital": 25,
    "fundamental": 25,
}
CAPITAL_TOPUP_SAFETY_SECONDS = 2
MAX_PROVIDER_ATTEMPTS = {
    "sector_ranking": 4,
    "sector_membership": 2,
    "kline": 1,
    "capital": 1,
    "fundamental": 1,
}
# Per-source live concurrency. kline is the long tail (subprocess fetchers
# with a multi-host fallback chain), so it gets more slots than the quick
# ranking/membership dimensions.
MAX_IN_FLIGHT = {
    "sector_ranking": 2,
    "sector_membership": 2,
    "kline": 4,
    "capital": 4,
    "fundamental": 4,
}


def capital_topup_reserve_seconds(
        topup_limit: int = CAPITAL_TOPUP_LIMIT,
        max_in_flight: dict | None = None,
        timeouts: dict | None = None,
        safety_seconds: float = CAPITAL_TOPUP_SAFETY_SECONDS) -> float:
    """Return the protected window required for independent top-up waves."""
    workers = max_in_flight or MAX_IN_FLIGHT
    attempt_timeouts = timeouts or LIVE_ATTEMPT_TIMEOUT_SECONDS
    try:
        limit = max(0, int(topup_limit))
        parallelism = min(
            max(1, int(workers.get("capital", 1) or 1)),
            max(1, int(workers.get("fundamental", 1) or 1)),
        )
        timeout = max(
            float(attempt_timeouts.get("capital", 0) or 0),
            float(attempt_timeouts.get("fundamental", 0) or 0),
        )
        margin = max(0.0, float(safety_seconds))
    except (TypeError, ValueError):
        return 0.0
    return math.ceil(limit / parallelism) * timeout + margin if limit else margin


CAPITAL_TOPUP_RESERVE_SECONDS = capital_topup_reserve_seconds()
# A source only hard-stops after this many *consecutive* live failures.
# Below that it stays "degraded" and keeps retrying so a transient blip
# (e.g. 1-2 kline timeouts) never orphans the rest of the run to stale cache.
HARD_FAILURE_THRESHOLD = 8
DATE_LAG_FAILURE_REASONS = frozenset({"stale_data"})


def classify_failure(error: BaseException | str | None) -> str:
    """Map provider errors to stable, low-cardinality reason codes."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    message = str(error or "").lower()
    if any(text in message for text in (
            "operation not permitted", "permission denied")):
        return "permission_denied"
    if any(text in message for text in (
            "proxyerror", "unable to connect to proxy", "proxy connection")):
        return "proxy_error"
    if any(text in message for text in (
            "remotedisconnected", "remote end closed connection",
            "connection aborted", "connection reset", "connection refused")):
        return "connection_error"
    if any(text in message for text in (
            "name or service not known", "temporary failure in name",
            "nodename nor servname", "getaddrinfo", "dns")):
        return "dns"
    if any(text in message for text in ("timed out", "timeout")):
        return "timeout"
    if any(text in message for text in (
            "http ", "http error", "status code", "status=")):
        return "http"
    if any(text in message for text in (
            "empty", "no data", "无响应", "空列表", "未返回有效")):
        return "empty"
    if any(text in message for text in (
            "json", "decode", "parse", "解析")):
        return "parse"
    if any(text in message for text in (
            "subprocess", "exited", "exit code", "子进程")):
        return "subprocess"
    return "unknown"


def live_attempt(*, attempted: bool, provider_attempts: int = 0,
                 reason: str = "", cache_used: bool = False,
                 stale: bool = False, subprocess_started: bool = False,
                 status: str = "", failure_chain: list | None = None,
                 error_type: str = "", stale_sources: list | None = None,
                 failure_detail: str = "", expected_date: str = "",
                 latest_date: str = "", scope: str = "",
                 evidence_source: str = "", affected_scope: str = "",
                 date_lag_evidence: list | None = None) -> dict:
    """Build the common evidence record used by every source adapter."""
    evidence = {
        "attempted": bool(attempted),
        "reason": reason,
        "cache_used": bool(cache_used),
        "stale": bool(stale),
        "subprocess_started": bool(subprocess_started),
        "provider_attempts": max(0, int(provider_attempts or 0)),
        "status": str(status or ""),
    }
    # Keep the successful/legacy evidence shape stable.  Diagnostics are
    # attached only when a failed payload actually provides them.
    if failure_chain:
        evidence["failure_chain"] = list(failure_chain)
    if error_type:
        evidence["error_type"] = str(error_type)
    if stale_sources:
        evidence["stale_sources"] = list(stale_sources)
    if failure_detail:
        evidence["failure_detail"] = str(failure_detail)
    for key, value in (
            ("expected_date", expected_date), ("latest_date", latest_date),
            ("scope", scope), ("evidence_source", evidence_source),
            ("affected_scope", affected_scope)):
        if value:
            evidence[key] = str(value)
    if date_lag_evidence:
        evidence["date_lag_evidence"] = copy.deepcopy(date_lag_evidence)
    return evidence


def source_result(payload: Any, attempt: dict | None = None) -> dict:
    """Return an internal result wrapper without changing public payload APIs."""
    return {
        "payload": payload,
        "live_attempt": attempt or live_attempt(attempted=False),
    }


def _has_source_date_lag_proof(evidence: dict) -> bool:
    """Accept run-wide date blocking only with explicit dated source proof."""
    expected = str(evidence.get("expected_date") or "").replace("-", "")
    latest = str(evidence.get("latest_date") or "").replace("-", "")
    samples = evidence.get("date_lag_evidence")

    def sample_proves_lag(sample):
        if not isinstance(sample, dict) or not sample.get("source") \
                or sample.get("reason") != "stale_data":
            return False
        sample_expected = str(sample.get("expected_date") or "").replace(
            "-", "")
        sample_latest = str(sample.get("latest_date") or "").replace(
            "-", "")
        return bool(
            sample_expected == expected
            and len(sample_latest) == 8 and sample_latest.isdigit()
            and sample_latest < expected)

    sample_proof = (
        isinstance(samples, list) and bool(samples)
        and all(sample_proves_lag(sample) for sample in samples)
    )
    return bool(
        evidence.get("scope") == "source"
        and len(expected) == 8 and expected.isdigit()
        and len(latest) == 8 and latest.isdigit()
        and latest < expected
        and evidence.get("evidence_source")
        and evidence.get("affected_scope")
        and sample_proof
    )


def _date_key(value: Any) -> str:
    text = str(value or "").replace("-", "")
    return text if len(text) == 8 and text.isdigit() else ""


@dataclass
class _Permit:
    source: str
    sequence: int
    started: bool = False
    completed: bool = False


def _new_source_state() -> dict:
    return {
        "logical_live_requests": 0,
        "requests": 0,
        "provider_attempts": 0,
        "cache_hits": 0,
        "failures": 0,
        "circuit_breaks": 0,
        "failure_reasons": {},
        "state": "healthy",
        "in_flight": 0,
        "consecutive_live_failures": 0,
        "expected_date": "",
        "latest_date": "",
        "date_lag_scope": "",
        "date_lag_evidence_source": "",
        "date_lag_evidence": [],
        "date_coverage_successes": {},
    }


class RunSourceHealth:
    """Thread-safe per-run circuit state for independent data sources."""

    def __init__(self, failure_threshold: int = 2,
                 max_in_flight: int = 2,
                 hard_failure_threshold: int = HARD_FAILURE_THRESHOLD,
                 per_source_max: dict | None = None):
        self.failure_threshold = failure_threshold
        self.max_in_flight = max_in_flight
        self.hard_failure_threshold = hard_failure_threshold
        self._per_source_max = dict(MAX_IN_FLIGHT)
        if per_source_max:
            self._per_source_max.update(per_source_max)
        self._lock = threading.RLock()
        self._states = {source: _new_source_state() for source in SOURCES}
        self._events: list[dict] = []
        # Enrichment is invoked once per sector window in the compatibility
        # scanner. Keep admission budgets on the shared run object so those
        # windows cannot each allocate a fresh prefetch/top-up queue.
        self._enrichment_admitted = {}
        self._sequence = 0
        self.started_at = time.monotonic()
        self.live_deadline = (
            self.started_at + SCAN_DEADLINE_SECONDS
            - FINALIZATION_RESERVE_SECONDS)

    @property
    def kline_deadline(self) -> float:
        """Absolute deadline for the K-line/Wyckoff phase.

        The property is derived from the run start and the live deadline so
        callers cannot accidentally move the phase boundary independently of
        the shared 230-second live window.
        """
        return min(
            self.live_deadline,
            self.started_at + KLINE_PHASE_SECONDS,
        )

    @property
    def capital_initial_deadline(self) -> float:
        """Latest safe admission time for the initial capital frontier."""
        return self.live_deadline - CAPITAL_TOPUP_RESERVE_SECONDS

    def admit_enrichment_slots(self, source: str, stage: str,
                               requested: int, limit: int) -> int:
        """Reserve a run-global number of logical enrichment slots.

        The return value is the number admitted for this window. This is a
        scheduler decision, not a provider request, so it deliberately does
        not affect source health counters or circuit state.
        """
        try:
            requested = max(0, int(requested))
        except (TypeError, ValueError):
            requested = 0
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 0
        key = (str(source), str(stage))
        with self._lock:
            admitted = self._enrichment_admitted.get(key, 0)
            available = max(0, limit - admitted)
            granted = min(requested, available)
            self._enrichment_admitted[key] = admitted + granted
            if requested > granted:
                self._events.append({
                    "event": "enrichment_budget_limited",
                    "source": str(source),
                    "stage": str(stage),
                    "requested": requested,
                    "admitted": granted,
                    "limit": limit,
                })
            return granted

    def enrichment_admitted(self, source: str, stage: str) -> int:
        """Return the run-global logical slots admitted for a stage."""
        with self._lock:
            return self._enrichment_admitted.get(
                (str(source), str(stage)), 0)

    def _state(self, source: str) -> dict:
        return self._states.setdefault(source, _new_source_state())

    def _inflight_cap(self, source: str) -> int:
        base = self._per_source_max.get(source, self.max_in_flight)
        # Throttle concurrency while degraded, but never fully stop: a
        # transient blip must keep retrying so a live fetch can still succeed
        # and reset the failure streak.
        if self._state(source)["state"] == "degraded":
            return max(1, base // 2)
        return base

    def try_acquire_live_permit(self, source: str) -> _Permit | None:
        """Reserve admission capacity; counters change only after start."""
        with self._lock:
            state = self._state(source)
            block_reason = self._live_block_reason_locked(source)
            if block_reason:
                self._events.append({
                    "event": "live_skipped", "source": source,
                    "reason": block_reason,
                })
                return None
            if state["in_flight"] >= self._inflight_cap(source):
                return None
            self._sequence += 1
            state["in_flight"] += 1
            return _Permit(source, self._sequence)

    def mark_started(self, token: _Permit | None) -> bool:
        with self._lock:
            if token is None or token.completed or token.started:
                return False
            token.started = True
            state = self._state(token.source)
            state["logical_live_requests"] += 1
            state["requests"] = state["logical_live_requests"]
            self._events.append({
                "event": "started", "source": token.source,
                "token": token.sequence,
            })
            return True

    def release_unstarted(self, token: _Permit | None,
                          reason: str = "cancelled") -> bool:
        with self._lock:
            if token is None or token.completed or token.started:
                return False
            token.completed = True
            state = self._state(token.source)
            state["in_flight"] = max(0, state["in_flight"] - 1)
            self._events.append({
                "event": "released", "source": token.source,
                "token": token.sequence, "reason": reason,
            })
            return True

    def _complete(self, token: _Permit | None, attempt: dict | None,
                  succeeded: bool) -> bool:
        with self._lock:
            if (token is None or token.completed or not token.started):
                return False
            token.completed = True
            state = self._state(token.source)
            state["in_flight"] = max(0, state["in_flight"] - 1)
            evidence = attempt or live_attempt(attempted=True)
            state["provider_attempts"] += max(
                0, int(evidence.get("provider_attempts", 0) or 0))
            if succeeded:
                state["consecutive_live_failures"] = 0
                state["state"] = "healthy"
                expected = _date_key(evidence.get("expected_date"))
                latest = _date_key(evidence.get("latest_date"))
                if expected and latest and latest >= expected:
                    successes = state["date_coverage_successes"]
                    successes[expected] = successes.get(expected, 0) + 1
                state["expected_date"] = ""
                state["latest_date"] = ""
                state["date_lag_scope"] = ""
                state["date_lag_evidence_source"] = ""
                state["date_lag_evidence"] = []
                event = "success"
            else:
                state["failures"] += 1
                reason = evidence.get("reason") or "unknown"
                reasons = state["failure_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1
                if reason in DATE_LAG_FAILURE_REASONS:
                    # A stale result is scoped to the fetched item unless the
                    # adapter explicitly proves that the source is stale as a
                    # whole. Neither kind contributes to outage/circuit counts.
                    state["consecutive_live_failures"] = 0
                    expected_key = _date_key(evidence.get("expected_date"))
                    source_lag_proven = _has_source_date_lag_proof(evidence)
                    mixed_target_date_success = bool(
                        expected_key
                        and state["date_coverage_successes"].get(
                            expected_key, 0))
                    if source_lag_proven and not mixed_target_date_success:
                        state.update({
                            "state": "date_lagging",
                            "expected_date": str(evidence["expected_date"]),
                            "latest_date": str(evidence["latest_date"]),
                            "date_lag_scope": str(evidence["affected_scope"]),
                            "date_lag_evidence_source": str(
                                evidence["evidence_source"]),
                            "date_lag_evidence": copy.deepcopy(
                                evidence["date_lag_evidence"]),
                        })
                        self._events.append({
                            "event": "source_date_lagging",
                            "source": token.source,
                            "reason": "source_date_lagging",
                            "expected_date": state["expected_date"],
                            "latest_date": state["latest_date"],
                            "scope": state["date_lag_scope"],
                            "evidence_source": state[
                                "date_lag_evidence_source"],
                            "date_lag_evidence": copy.deepcopy(
                                state["date_lag_evidence"]),
                        })
                    else:
                        if (state["state"] != "date_lagging"
                                or mixed_target_date_success):
                            state["state"] = "degraded"
                        self._events.append({
                            "event": (
                                "source_date_lagging_rejected_mixed_success"
                                if source_lag_proven and mixed_target_date_success
                                else "item_date_lagging"),
                            "source": token.source,
                            "reason": reason,
                            "token": token.sequence,
                            "expected_date": evidence.get("expected_date", ""),
                            "latest_date": evidence.get("latest_date", ""),
                            "scope": evidence.get("scope") or "item",
                            "target_date_successes": (
                                state["date_coverage_successes"].get(
                                    expected_key, 0)
                                if expected_key else 0),
                        })
                elif (token.source == "sector_membership" and reason in {
                        "sector_mapping_missing", "sector_mapping_ambiguous"}):
                    # A successful directory lookup without an exact match is
                    # an item-level taxonomy issue, not a provider outage.
                    state["consecutive_live_failures"] = 0
                    state["state"] = "healthy"
                else:
                    source_date_blocked = state["state"] == "date_lagging"
                    state["consecutive_live_failures"] += 1
                    crossed_threshold = (
                        state["consecutive_live_failures"]
                        == self.failure_threshold)
                    if (not source_date_blocked
                            and state["consecutive_live_failures"]
                            >= self.hard_failure_threshold):
                        if state["state"] != "unavailable":
                            state["circuit_breaks"] += 1
                            self._events.append({
                                "event": "circuit_opened",
                                "source": token.source,
                                "reason": "source_unavailable",
                            })
                        state["state"] = "unavailable"
                    elif not source_date_blocked:
                        # Degrade (throttle concurrency) but keep retrying: a
                        # transient blip must not hard-stop the rest of the run.
                        state["state"] = "degraded"
                    if crossed_threshold and not source_date_blocked:
                        self._events.append({
                            "event": "source_degraded",
                            "source": token.source,
                            "reason": reason,
                        })
                event = "failure"
            self._events.append({
                "event": event, "source": token.source,
                "token": token.sequence,
                "live_attempt": dict(evidence),
            })
            return True

    def complete_success(self, token: _Permit | None,
                         attempt: dict | None = None) -> bool:
        return self._complete(token, attempt, succeeded=True)

    def complete_failure(self, token: _Permit | None,
                         attempt: dict | None = None) -> bool:
        return self._complete(token, attempt, succeeded=False)

    def record_cache_hit(self, source: str, stale: bool = False,
                         reason: str = "cache_only") -> None:
        """Record fallback use without changing the live circuit state."""
        with self._lock:
            self._state(source)["cache_hits"] += 1
            self._events.append({
                "event": "cache_hit", "source": source, "stale": stale,
                "reason": reason,
            })
            if stale:
                self._events.append({
                    "event": "data_stale", "source": source,
                    "reason": "data_stale",
                })

    def record_cache_result(self, source: str, payload: Any,
                            stale: bool = False,
                            reason: str = "cache_only") -> None:
        if payload:
            self.record_cache_hit(source, stale=stale, reason=reason)
            return
        with self._lock:
            self._events.append({
                "event": "cache_miss", "source": source,
                "reason": "cache_only",
            })

    def unavailable(self, source: str) -> bool:
        with self._lock:
            return self._state(source)["state"] == "unavailable"

    def live_block_reason(self, source: str) -> str:
        """Return the scheduler reason for suppressing live work, if any."""
        with self._lock:
            return self._live_block_reason_locked(source)

    def _live_block_reason_locked(self, source: str) -> str:
        return {
            "unavailable": "source_unavailable",
            "date_lagging": "source_date_lagging",
        }.get(self._state(source)["state"], "")

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._states)

    def events(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._events)


def bounded_source_map(
        source: str, items: Iterable[Any], health: RunSourceHealth,
        live_fetch: Callable[[Any], dict], cache_fetch: Callable[[Any], Any],
        live_deadline: float, max_workers: int = 4,
        cache_usable: Callable[[Any], bool] | None = None,
        include_evidence: bool = False,
        cache_fetch_with_reason: Callable[[Any, str], Any] | None = None,
        deadline_reason: str = "deadline",
        ) -> list[tuple[Any, Any]]:
    """Run admitted live work incrementally, then finish cache-only.

    When ``include_evidence`` is true, the second tuple value is the internal
    ``source_result`` wrapper.  The default remains the historical payload-only
    contract so existing callers do not receive internal attempt metadata.

    The executor is deliberately shut down without waiting after the deadline;
    completed late work cannot mutate health because its permit is finalized by
    the scheduler before returning.
    """
    pending_items = iter(items)
    results: list[tuple[Any, Any]] = []
    futures = {}
    exhausted = False
    pool = ThreadPoolExecutor(max_workers=max_workers)

    def cached(item: Any, evidence_reason: str = "cache_only") -> Any:
        if cache_fetch_with_reason is not None:
            payload = cache_fetch_with_reason(item, evidence_reason)
        else:
            payload = cache_fetch(item)
        usable = cache_usable(payload) if cache_usable else bool(payload)
        health.record_cache_result(
            source, payload if usable else None, stale=usable,
            # Keep the health event vocabulary stable; the wrapper carries
            # the more precise scheduler reason for per-item diagnostics.
            reason="cache_only")
        if not include_evidence:
            return payload
        blocked_evidence = {}
        if evidence_reason == "source_date_lagging":
            blocked_state = health.snapshot().get(source, {})
            blocked_evidence = {
                "expected_date": blocked_state.get("expected_date", ""),
                "latest_date": blocked_state.get("latest_date", ""),
                "scope": "source",
                "evidence_source": blocked_state.get(
                    "date_lag_evidence_source", ""),
                "affected_scope": blocked_state.get("date_lag_scope", ""),
                "date_lag_evidence": blocked_state.get(
                    "date_lag_evidence", []),
            }
        return source_result(payload, live_attempt(
            attempted=False, cache_used=usable, stale=usable,
            reason=evidence_reason if evidence_reason else (
                "cache_only" if usable else ""),
            status=evidence_reason if evidence_reason else "",
            **blocked_evidence))

    try:
        while not exhausted or futures:
            while not exhausted and time.monotonic() < live_deadline:
                token = health.try_acquire_live_permit(source)
                if token is None:
                    break
                try:
                    item = next(pending_items)
                except StopIteration:
                    health.release_unstarted(token, "exhausted")
                    exhausted = True
                    break
                try:
                    future = pool.submit(live_fetch, item)
                except Exception:
                    health.release_unstarted(token, "submit_failed")
                    results.append((item, cached(item)))
                    continue
                futures[future] = (item, token)

            if not futures:
                break
            remaining = live_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(futures, timeout=remaining,
                           return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                item, token = futures.pop(future)
                try:
                    wrapped = future.result()
                    payload = wrapped["payload"]
                    attempt = wrapped["live_attempt"]
                    if not attempt.get("attempted"):
                        health.release_unstarted(token, "cache_hit")
                        if attempt.get("cache_used"):
                            health.record_cache_hit(
                                source, stale=attempt.get("stale", False))
                    elif attempt.get("reason"):
                        health.mark_started(token)
                        health.complete_failure(token, attempt)
                        if attempt.get("cache_used"):
                            health.record_cache_result(
                                source, payload,
                                stale=attempt.get("stale", False),
                                reason="cache_only")
                    else:
                        health.mark_started(token)
                        health.complete_success(token, attempt)
                    results.append((
                        item, wrapped if include_evidence else payload))
                except Exception as exc:
                    failure = live_attempt(
                        attempted=True, provider_attempts=1,
                        reason=classify_failure(exc))
                    health.mark_started(token)
                    health.complete_failure(token, failure)
                    fallback = cached(item)
                    if include_evidence:
                        fallback = source_result(
                            fallback["payload"], {
                                **failure,
                                "cache_used": fallback["live_attempt"].get(
                                    "cache_used", False),
                                "stale": fallback["live_attempt"].get(
                                    "stale", False),
                            })
                    results.append((item, fallback))

        for future, (item, token) in list(futures.items()):
            if future.cancel():
                health.release_unstarted(token, "cancelled")
                attempt = live_attempt(
                    attempted=False, reason="cancelled")
            else:
                health.mark_started(token)
                attempt = live_attempt(
                    attempted=True, provider_attempts=1, reason="timeout")
                health.complete_failure(token, attempt)
            fallback = cached(item)
            if include_evidence:
                fallback = source_result(
                    fallback["payload"], {
                        **attempt,
                        "cache_used": fallback["live_attempt"].get(
                            "cache_used", False),
                        "stale": fallback["live_attempt"].get(
                            "stale", False),
                    })
            results.append((item, fallback))
        blocked_reason = health.live_block_reason(source)
        if blocked_reason:
            pending_reason = blocked_reason
        elif time.monotonic() >= live_deadline:
            pending_reason = deadline_reason or "deadline"
        else:
            pending_reason = "scheduler_capacity"
        for item in pending_items:
            results.append((item, cached(item, pending_reason)))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results
