"""Build a research observation from the existing scan's frozen inputs only."""

import copy
import hashlib
import json
import time

from analysis.daily_weak_to_strong_v2 import (
    DEFAULT_CONFIG, DETECTOR_VERSION, SCHEMA_VERSION,
    analyze_daily_weak_to_strong_v2,
)
from reporting.weak_to_strong import sort_v2_rows


GROUPS = ("preparing", "early_repair", "local_confirmation", "range_breakout",
          "provisional", "diagnostics")


def build_v2_output(collected, v1_output, calendar, *, deadline=None):
    """One sequential detector pass per security; no fetching or universe expansion."""
    started = time.monotonic()
    as_of = v1_output.get("as_of_date")
    parameter_hash = hashlib.sha256(json.dumps(
        DEFAULT_CONFIG, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).hexdigest()
    output = {"schema_version": SCHEMA_VERSION, "as_of_date": as_of,
              "detector_version": DETECTOR_VERSION, "parameter_hash": parameter_hash,
              "parameters": copy.deepcopy(DEFAULT_CONFIG),
              "validation_status": "continue_accumulating", "display_mode": "research",
              "scope": copy.deepcopy(v1_output.get("scope") or {}),
              "tracking": {}, **{group: [] for group in GROUPS}}
    contexts = {str(row.get("code")): row for group in
                ("confirmed", "watching", "pullback", "provisional", "diagnostics")
                for row in v1_output.get(group, [])}
    sources = {str(entry.get("result", {}).get("code")): entry
               for entry in collected.values() if isinstance(entry, dict)}
    cycles, events, stocks = set(), set(), set()
    costs = []
    skipped = 0
    scope_codes = sorted(set(output["scope"].get("codes") or contexts))
    for code in scope_codes:
        old = contexts.get(code, {})
        entry = sources.get(code)
        reason = None
        if deadline is not None and time.monotonic() >= deadline:
            reason = "v2_budget_exhausted"
            skipped += 1
        elif not entry:
            reason = "collector_not_supported"
        if reason:
            item = {"code": code, "name": old.get("name", ""), "as_of_date": as_of,
                    "origin_pattern_type": None, "pattern_type": None, "stage": "NONE",
                    "confirmation": "evidence_incomplete", "evidence_status": "data_insufficient",
                    "cycle_id": None, "known_at": None, "stage_first_at": None,
                    "signal_date": None, "metrics": {}, "checks": {}, "risk_flags": [],
                    "structure": {}, "secondary_matches": [], "reason_codes": [reason]}
        else:
            tick = time.monotonic()
            item = analyze_daily_weak_to_strong_v2(
                entry.get("kline_payload"), as_of_date=as_of, code=code,
                name=old.get("name", entry.get("result", {}).get("name", "")),
                close_evidence=entry.get("close_evidence"), calendar_evidence=calendar,
                v1_result=entry.get("result"))
            costs.append(time.monotonic() - tick)
        item["context"] = {**item.get("context", {}), **copy.deepcopy(old.get("context") or {})}
        item["sector_memberships"] = copy.deepcopy(old.get("sector_memberships") or [])
        item["ts_code"] = old.get("ts_code", code)
        item["detector_version"] = DETECTOR_VERSION
        item["parameter_hash"] = parameter_hash
        ready = item.get("evidence_status") == "ready"
        stage = str(item.get("stage", "NONE")).lower()
        inactive = item.get("lifecycle_status") in {"invalidated", "expired", "completed"}
        if item.get("confirmation") == "provisional" and not inactive:
            group = "provisional"
        elif ready and stage in GROUPS[:4] and not inactive:
            group = stage
        else:
            group = "diagnostics"
        output[group].append(item)
        if group not in {"diagnostics", "provisional"}:
            stocks.add(code)
        for match in [item] + item.get("secondary_matches", []):
            if (match.get("cycle_id") and match.get("evidence_status") == "ready"
                    and match.get("lifecycle_status") not in {"invalidated", "expired", "completed"}):
                cycles.add(match["cycle_id"])
            for event in match.get("events", []):
                if event.get("date", event.get("signal_date")) == as_of:
                    events.add(event.get("event_id") or json.dumps(event, sort_keys=True))
    for group in GROUPS:
        output[group] = sort_v2_rows(output[group])
    if skipped:
        output["scope"].update(scope_complete=False, collector_status="partial")
    output["summary"] = {"stock_count": len(stocks), "active_cycle_count": len(cycles),
                         "new_event_count": len(events), "attempted": len(scope_codes),
                         "budget_skipped": skipped,
                         **{group: len(output[group]) for group in GROUPS}}
    output["performance"] = {"compute_seconds": time.monotonic() - started,
                             "securities_computed": len(costs),
                             "max_security_seconds": max(costs, default=0),
                             "network_requests": 0}
    return output
