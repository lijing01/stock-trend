"""Run-scoped source health, evidence, deadlines, and bounded scheduling."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable


SOURCES = (
    "sector_ranking", "sector_membership", "kline", "capital",
    "fundamental",
)
SCAN_DEADLINE_SECONDS = 110
FINALIZATION_RESERVE_SECONDS = 5
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
    "capital": 2,
    "fundamental": 2,
}
# A source only hard-stops after this many *consecutive* live failures.
# Below that it stays "degraded" and keeps retrying so a transient blip
# (e.g. 1-2 kline timeouts) never orphans the rest of the run to stale cache.
HARD_FAILURE_THRESHOLD = 8


def classify_failure(error: BaseException | str | None) -> str:
    """Map provider errors to stable, low-cardinality reason codes."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    message = str(error or "").lower()
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
                 stale: bool = False, subprocess_started: bool = False) -> dict:
    """Build the common evidence record used by every source adapter."""
    return {
        "attempted": bool(attempted),
        "reason": reason,
        "cache_used": bool(cache_used),
        "stale": bool(stale),
        "subprocess_started": bool(subprocess_started),
        "provider_attempts": max(0, int(provider_attempts or 0)),
    }


def source_result(payload: Any, attempt: dict | None = None) -> dict:
    """Return an internal result wrapper without changing public payload APIs."""
    return {
        "payload": payload,
        "live_attempt": attempt or live_attempt(attempted=False),
    }


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
        self._sequence = 0
        self.started_at = time.monotonic()
        self.live_deadline = (
            self.started_at + SCAN_DEADLINE_SECONDS
            - FINALIZATION_RESERVE_SECONDS)

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
            if state["state"] == "unavailable":
                self._events.append({
                    "event": "live_skipped", "source": source,
                    "reason": "source_unavailable",
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
                event = "success"
            else:
                state["failures"] += 1
                state["consecutive_live_failures"] += 1
                reason = evidence.get("reason") or "unknown"
                reasons = state["failure_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1
                crossed_threshold = (
                    state["consecutive_live_failures"]
                    == self.failure_threshold)
                if state["consecutive_live_failures"] >= self.hard_failure_threshold:
                    if state["state"] != "unavailable":
                        state["circuit_breaks"] += 1
                        self._events.append({
                            "event": "circuit_opened",
                            "source": token.source,
                            "reason": "source_unavailable",
                        })
                    state["state"] = "unavailable"
                else:
                    # Degrade (throttle concurrency) but keep retrying: a
                    # transient blip must not hard-stop the source for the
                    # rest of the run.
                    state["state"] = "degraded"
                    if crossed_threshold:
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

    def snapshot(self) -> dict:
        with self._lock:
            return {
                source: {
                    key: (dict(value) if isinstance(value, dict) else value)
                    for key, value in state.items()
                }
                for source, state in self._states.items()
            }

    def events(self) -> list[dict]:
        with self._lock:
            return [dict(event) for event in self._events]


def bounded_source_map(
        source: str, items: Iterable[Any], health: RunSourceHealth,
        live_fetch: Callable[[Any], dict], cache_fetch: Callable[[Any], Any],
        live_deadline: float, max_workers: int = 4,
        cache_usable: Callable[[Any], bool] | None = None,
        include_evidence: bool = False,
        cache_fetch_with_reason: Callable[[Any, str], Any] | None = None,
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
        return source_result(payload, live_attempt(
            attempted=False, cache_used=usable, stale=usable,
            reason=evidence_reason if evidence_reason else (
                "cache_only" if usable else "")))

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
        if health.unavailable(source):
            pending_reason = "source_unavailable"
        elif time.monotonic() >= live_deadline:
            pending_reason = "deadline"
        else:
            pending_reason = "scheduler_capacity"
        for item in pending_items:
            results.append((item, cached(item, pending_reason)))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results
